"""The model's reference graph: the declared edge table, the graphs built over it, and the walks.

``build_graphs`` turns the declared reference and relation edges into adjacency maps, and
``dependents``, ``dependencies`` and ``related`` walk them, bounded by a depth and a limit. Each edge
carries an ``EdgeDetail`` that names the field holding the reference.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from swcraft_tools.differ import build_key_map
from swcraft_tools.model_registry import (
    KindFacts,
    LayerFacts,
    ModelRegistry,
    ReferenceEdge,
    RelationEdge,
    current_registry,
)
from swcraft_tools.model_state import ModelState


@dataclass(frozen=True)
class ElementRef:
    """Identity of one element in the model, for graph nodes and lookups."""

    # The layer's singular token (`layer_token`).
    layer: str
    name: str


#: The shapes an edge detail takes, as ``kind -> (one value, several values)`` templates, **ordered**: a
#: row's kinds render in this order, joined by ``"; "``; several values of one kind render in the order met,
#: joined by ``", "``. ``endpoint`` renders like ``port`` but its value is qualified by the endpoint's
#: owner, because port names repeat across the two elements a connector joins.
#:
#: **A detail is kept per edge, not per text.** When one kind's equal value reached a row from two or more
#: parents, every value of that kind on that row is qualified by its parent (``runnables SwcA.Run,
#: SwcB.Run``), and additionally by the parent's layer token where two such parents share a name across
#: layers (``via ports swc X.p, composition X.p``). Otherwise a value is written bare and equal values are
#: one. Each key is public output: a new kind changes every report and walk document.
_DETAIL_RENDERING: dict[str, tuple[str, str]] = {
    "port": ("via port {}", "via ports {}"),
    "endpoint": ("via port {}", "via ports {}"),
    "runnable": ("runnable {}", "runnables {}"),
    "member": ("member {}", "members {}"),
    "step": ("step: {}", "steps: {}"),
    "budget": ("memory budget {} bytes", "memory budgets {} bytes"),
    "core": ("core {}", "cores {}"),
    "access": ("accessible from {}", "accessible from {}"),
    # A partition is not a core, so it has a kind of its own.
    "partition": ("partition {}", "partitions {}"),
}


@dataclass(frozen=True)
class EdgeDetail:
    """What one resolved edge says about itself: a *kind* and the name or key it carries.

    ``kind`` is one of :data:`_DETAIL_RENDERING`'s keys (refused otherwise, so no detail is minted that no
    renderer prints); ``value`` is the port, runnable, member or core name, the budget's byte count, or an
    ``endpoint``'s owner and port joined by ``.``, always as text. ``owner`` is the parent the edge came from,
    set only when an equal value of the kind reached the row from another parent; ``owner_layer`` only when
    another such parent shares that name in another layer.
    """

    kind: str
    value: str
    owner: str | None = None
    owner_layer: str | None = None

    @property
    def text(self) -> str:
        """The value as a line prints it: ``value``, ``owner.value``, or ``owner_layer owner.value``."""
        if self.owner is None:
            return self.value
        if self.owner_layer is None:
            return f"{self.owner}.{self.value}"
        return f"{self.owner_layer} {self.owner}.{self.value}"

    def __post_init__(self) -> None:
        if self.kind not in _DETAIL_RENDERING:
            raise ValueError(f"unknown edge detail kind '{self.kind}': expected one of {', '.join(_DETAIL_RENDERING)}")


def _render_details(details: Sequence[EdgeDetail]) -> str:
    """The parenthesis text a report line carries for *details*; ``""`` for none.

    Kinds in :data:`_DETAIL_RENDERING`'s order, values in the order the tuple holds them.
    """
    grouped: dict[str, list[str]] = {}
    for detail in details:
        grouped.setdefault(detail.kind, []).append(detail.text)
    parts = []
    for kind, (one, several) in _DETAIL_RENDERING.items():
        values = grouped.get(kind)
        if values:
            parts.append((one if len(values) == 1 else several).format(", ".join(values)))
    return "; ".join(parts)


@dataclass(frozen=True)
class Dependent:
    """One element reached from an ``ElementRef`` by a walk, with every detail of the edges that reached it.

    Named for the reverse direction and used for both. ``details`` holds every distinct detail of every edge
    into this element from the matched element or an element the walk **expanded**, in the order met and kept
    per edge (:data:`_DETAIL_RENDERING`). An element is marked visited when first pushed, which fixes the set
    and order of rows; a later edge into it only adds details.

    What it does not reach: the bounded walk does not expand an element at the bound, so an edge from it is not
    read. A bare detail does not name its parent. A connector reached only from the composition it is drawn
    inside renders no port.
    """

    ref: ElementRef
    details: tuple[EdgeDetail, ...] = ()

    def __post_init__(self) -> None:
        # A bare string iterates as characters; refuse it rather than read it as "no detail".
        if not isinstance(self.details, tuple) or not all(isinstance(d, EdgeDetail) for d in self.details):
            raise TypeError(f"Dependent.details is a tuple of EdgeDetail, not {self.details!r}")

    @property
    def detail(self) -> str:
        """The rendered parenthesis text — e.g. ``via ports pA, pB`` — or ``""``."""
        return _render_details(self.details)


@dataclass(frozen=True)
class EdgeContext:
    """What a :data:`_EDGE_DETAILS` formatter is handed for one emitted edge.

    ``member`` is the element that carries the declared field (the top-level element itself for a field on a
    top-level kind); ``key`` is the target key resolved from it. The owning top-level element is not passed: no
    detail names it, and the traversal carries it as the edge's dependent node.
    """

    member: Any
    key: Any


def _no_detail(context: EdgeContext) -> EdgeDetail | None:
    """The default: an edge whose dependent line carries nothing but the element."""
    return None


def _endpoint_detail(context: EdgeContext) -> EdgeDetail:
    """A connector endpoint's detail, for both of its rows: the port it wires, qualified by its owner type."""
    return EdgeDetail("endpoint", f"{context.key}.{context.member.port}")


#: One row per declared edge, keyed by the contract's field id, **ordered**: the row order is the order
#: edges enter the graph and so the order of every report and walk, so a new row is **appended**, in
#: contract field order. A declared edge with no row renders with :func:`_no_detail` and is emitted after
#: every listed row. A row returns an :class:`EdgeDetail` or ``None``, never text, and may read a
#: non-reference field of the member, because a detail describes the edge and never adds or removes one.
_EDGE_DETAILS: dict[str, Callable[[EdgeContext], EdgeDetail | None]] = {
    "field:swc.port.interface": lambda context: EdgeDetail("port", context.member.name),
    "field:tasks.task_runnable_ref.swc": lambda context: EdgeDetail("runnable", context.member.runnable),
    "field:interfaces.data_element.application_type": _no_detail,
    "field:interfaces.data_element.implementation_type": _no_detail,
    "field:interfaces.argument.application_type": _no_detail,
    "field:interfaces.argument.implementation_type": _no_detail,
    # Together, so every implementation-type reference is walked element-outer in one pass.
    "field:implementation_types.value_type.base": _no_detail,
    "field:implementation_types.reference_type.base": _no_detail,
    "field:application_types.record_field.type": _no_detail,
    "field:application_types.array.element_type": _no_detail,
    # An endpoint names a type; the detail is the port it wires, qualified by that type, because port names
    # repeat across the two elements a connector joins.
    "field:connectors.connector_endpoint.swc": _endpoint_detail,
    "field:connectors.connector_endpoint.composition": _endpoint_detail,
    "field:connectors.connector.composition": _no_detail,
    "field:compositions.composition_port.interface": lambda context: EdgeDetail("port", context.member.name),
    "field:compositions.composition_member.type": lambda context: EdgeDetail("member", context.member.name),
    "field:compositions.composition_member.composition": lambda context: EdgeDetail("member", context.member.name),
    "field:sequences.sequence.task": _no_detail,
    "field:sequences.sequence_step.swc": lambda context: EdgeDetail("step", context.member.runnable),
    "field:swc.swc.memory": lambda context: EdgeDetail("budget", str(context.member.memory[context.key])),
    # The key rather than the resolved element keeps the formatter total for an unresolved edge.
    "field:tasks.task.partition": lambda context: EdgeDetail("partition", str(context.key)),
    # A same-layer edge: the detail names the core key so the row is distinguishable from any other
    # dependency on the node.
    "field:hardware.memory.accessible": lambda context: EdgeDetail("access", str(context.key)),
    # The rows from here on are appended in contract field order (an inserted row would reorder output).
    # `data_type_mappings`' resolved key is the node itself, so a detail would repeat it; a map entry carries
    # no name. What that does not reach: `impact <data type>` does not say whether the type is the
    # application or the implementation side of the mapping.
    "field:swc.swc.data_type_mappings": _no_detail,
    "field:mapping_sets.map.application": _no_detail,
    "field:mapping_sets.map.implementation": _no_detail,
    "field:implementation_types.implementation_element.type": _no_detail,
    "field:implementation_types.implementation_array.element_type": _no_detail,
    "field:implementation_types.pointer.target": _no_detail,
    "field:application_types.enum.compu_method_ref": _no_detail,
    "field:application_types.application_type.unit_ref": _no_detail,
    "field:application_types.application_type.compu_method_ref": _no_detail,
    "field:application_types.application_type.data_constr_ref": _no_detail,
    "field:implementation_types.value_type.compu_method_ref": _no_detail,
    "field:implementation_types.reference_type.compu_method_ref": _no_detail,
    "field:compu_methods.linear.unit": _no_detail,
    # No derived task -> core edge: `impact` reaches a task's core in two hops, through its partition.
    "field:hardware.partition.core": lambda context: EdgeDetail("core", str(context.key)),
    # The requirement is the depended-upon node; a link on a port or runnable names that member.
    "field:swc.swc.satisfies": _no_detail,
    "field:swc.port.satisfies": lambda context: EdgeDetail("port", context.member.name),
    "field:swc.runnable.satisfies": lambda context: EdgeDetail("runnable", context.member.name),
    "field:interfaces.sender_receiver.satisfies": _no_detail,
    "field:interfaces.client_server.satisfies": _no_detail,
    "field:interfaces.external_interface.satisfies": _no_detail,
    "field:requirements.requirement.derived_from": _no_detail,
    "field:compositions.composition.satisfies": _no_detail,
    "field:tasks.task.satisfies": _no_detail,
    "field:hardware.processor.satisfies": _no_detail,
    "field:hardware.core.satisfies": _no_detail,
    "field:hardware.memory.satisfies": _no_detail,
    "field:hardware.partition.satisfies": _no_detail,
    "field:swc.variable.application_type": _no_detail,
    "field:swc.variable.implementation_type": _no_detail,
}


@dataclass(frozen=True)
class _Edge:
    """A ``reference`` or a ``relation``, in the one shape the traversal walks.

    :attr:`resolver` is ``None`` for a reference, whose value is the target's identity.
    """

    field_id: str
    source_layer: str
    source_kind: str
    source_field: str
    target_layer: str
    target_kind: str | None
    target_field: str
    resolver: Callable[[Any], Iterable[Any]] | None
    detail: Callable[[EdgeContext], EdgeDetail | None]


def _detail_for(field_id: str) -> Callable[[EdgeContext], EdgeDetail | None]:
    return _EDGE_DETAILS.get(field_id, _no_detail)


def _from_reference(edge: ReferenceEdge) -> _Edge:
    return _Edge(
        field_id=edge.field_id,
        source_layer=edge.source_layer,
        source_kind=edge.source_kind,
        source_field=edge.source_field,
        target_layer=edge.target_layer,
        target_kind=None,
        target_field=edge.target_identity_field,
        resolver=None,
        detail=_detail_for(edge.field_id),
    )


def _from_relation(edge: RelationEdge) -> _Edge:
    return _Edge(
        field_id=edge.field_id,
        source_layer=edge.source_layer,
        source_kind=edge.source_kind,
        source_field=edge.source_field,
        target_layer=edge.target_layer,
        target_kind=edge.target_kind,
        target_field=edge.target_field,
        resolver=edge.resolver,
        detail=_detail_for(edge.field_id),
    )


def _declared_edges(registry: ModelRegistry) -> dict[str, _Edge]:
    """Every declared edge, keyed by field id, in **contract field order**.

    Walked field by field so the unlisted edges come out in one order rather than references and relations
    apart.
    """
    edges: dict[str, _Edge] = {}
    for layer in registry.layers:
        for kind in layer.kinds:
            for facts in kind.fields:
                if facts.reference is not None:
                    edges[facts.id] = _from_reference(facts.reference)
                elif facts.relation is not None:
                    edges[facts.id] = _from_relation(facts.relation)
    return edges


def _emission_order(registry: ModelRegistry) -> tuple[_Edge, ...]:
    """The declared edges in emission order: listed rows first, then the rest."""
    declared = _declared_edges(registry)
    listed = [declared[field_id] for field_id in _EDGE_DETAILS if field_id in declared]
    unlisted = [edge for field_id, edge in declared.items() if field_id not in _EDGE_DETAILS]
    return (*listed, *unlisted)


def _emission_plan(registry: ModelRegistry) -> tuple[tuple[str, tuple[_Edge, ...]], ...]:
    """The emission order as ``(source layer, edges)`` passes.

    Consecutive edges of one source layer form one pass, walked element by element with every edge applied to
    each element in turn. The order is observable: a pass per edge would group a node's dependents by kind
    instead of the model's element order.
    """
    passes: list[tuple[str, list[_Edge]]] = []
    for edge in _emission_order(registry):
        if passes and passes[-1][0] == edge.source_layer:
            passes[-1][1].append(edge)
        else:
            passes.append((edge.source_layer, [edge]))
    return tuple((layer, tuple(edges)) for layer, edges in passes)


@functools.lru_cache(maxsize=512)
def field_attribute(model: type, published: str) -> str:
    """The Python attribute a contract field's published name is stored under.

    The published name is the serialization alias, which differs from the attribute only for
    ``connector.from``. Tolerates a non-Pydantic object, returning *published* unchanged.
    """
    fields = getattr(model, "model_fields", None)
    if fields:
        for attribute, info in fields.items():
            if (info.alias or attribute) == published:
                return attribute
    return published


def read(element: Any, published: str) -> Any:
    """Return *element*'s value of the field published as *published*, or ``None`` when it has none."""
    return getattr(element, field_attribute(type(element), published), None)  # type: ignore[arg-type]  # mypy limitation: type[Any] is not Hashable for lru_cache


def layer_token(layer: LayerFacts) -> str:
    """``ElementRef.layer`` for one layer: the contract's declared token, or the layer's name."""
    return layer.token


def _identity_key(layer: LayerFacts) -> Callable[[Any], str]:
    """A key function reading *layer*'s declared identity field."""
    field = layer.identity_field

    def key(element: Any) -> str:
        return getattr(element, field_attribute(type(element), field))  # type: ignore[arg-type]  # mypy limitation: type[Any] is not Hashable for lru_cache

    return key


def _kind_of(registry: ModelRegistry, element: Any) -> KindFacts | None:
    return registry.kind_of_model(type(element))


def _field_members(parent: Any, published: str) -> Iterator[Any]:
    """The member elements one collection or nested-object field holds.

    A list field yields its items; a nested object field (``connector.from``) yields itself.
    """
    value = read(parent, published)
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        yield from value
    else:
        yield value


def _members(
    registry: ModelRegistry,
    owner: Any,
    owner_kind: KindFacts | None,
    kind_name: str,
    seen: frozenset[str] = frozenset(),
) -> Iterator[Any]:
    """Every instance of *kind_name* reachable from one top-level element.

    Follows ``KindFacts.member_of`` upward, so a two-level member (``argument`` inside ``operation`` inside
    ``client_server``) is reached without a per-layer rule; an owner of the wrong kind yields nothing. *seen*
    guards against a cyclic ``member_of`` graph, which the kernel does not refuse and which would otherwise end
    in ``RecursionError``.
    """
    if owner_kind is None or kind_name in seen:
        return
    if owner_kind.kind == kind_name:
        yield owner
        return
    facts = registry.kinds.get(kind_name)
    if facts is None:
        return
    deeper = seen | {kind_name}
    for parent_kind, field_name in facts.member_of:
        for parent in _members(registry, owner, owner_kind, parent_kind, deeper):
            for candidate in _field_members(parent, field_name):
                member = _kind_of(registry, candidate)
                if member is not None and member.kind == kind_name:
                    yield candidate


def _keys(edge: _Edge, member: Any) -> Iterable[Any]:
    """The target keys one member element names through one edge.

    A reference's key is the field's own value; a relation's are whatever its resolver yields, unfiltered, so a
    ``check_hardware_topology`` finding stays with that check.
    """
    if edge.resolver is not None:
        return edge.resolver(member)
    value = read(member, edge.source_field)
    return () if value is None else (value,)


def _targets(registry: ModelRegistry, state: ModelState, edge: _Edge, key: Any) -> Iterable[ElementRef]:
    """The graph nodes one resolved key names.

    When the key is the target's identity (every reference and every shipped relation), the node is the key
    itself whether or not an element answers to it, so a dangling reference stays visible to ``impact``.
    Otherwise the nodes are the elements whose declared target field carries the key, possibly several; the
    shipped contract declares no such edge.
    """
    layer = registry.layer(edge.target_layer)
    token = layer_token(layer)
    if edge.target_field == layer.identity_field:
        return (ElementRef(token, key),)

    identity = _identity_key(layer)
    matched = []
    for element in state.layers.get(edge.target_layer, ()):
        kind = _kind_of(registry, element)
        if edge.target_kind is not None and (kind is None or kind.kind != edge.target_kind):
            continue
        if read(element, edge.target_field) == key:
            matched.append(ElementRef(token, identity(element)))
    return matched


#: One direction of the resolved topology: ``node -> {reached node: details}``,
#: every distinct :class:`EdgeDetail` of the edges between the two, in emission
#: order, and ``()`` for edges that carry none.
AdjacencyMap = dict[ElementRef, dict[ElementRef, tuple[EdgeDetail, ...]]]


#: The closed vocabulary :meth:`Graphs.direction` resolves, in the order a refusal lists it.
DIRECTIONS: tuple[str, ...] = ("dependents", "dependencies")


#: The direction a caller that names none gets: "what breaks if I change this".
DEFAULT_DIRECTION = "dependents"


@dataclass(frozen=True)
class Graphs:
    """The declared topology, resolved once, in both directions.

    ``reverse`` and ``forward`` hold the same edges, keyed by the end each direction walks from. ``key_maps`` is
    one identity-key map per layer token, built before any edge so a duplicate identity is refused first.
    """

    reverse: AdjacencyMap
    forward: AdjacencyMap
    key_maps: dict[str, dict[str, Any]]

    def direction(self, name: str) -> AdjacencyMap:
        """The map *name* walks — one of :data:`DIRECTIONS`.

        Raises:
            ValueError: *name* is neither, naming both accepted values; no default is assumed.
        """
        if name == "dependents":
            return self.reverse
        if name == "dependencies":
            return self.forward
        raise ValueError(f"unknown direction '{name}': expected 'dependents' or 'dependencies'")


def _all_key_maps(state: ModelState, registry: ModelRegistry) -> dict[str, dict[str, Any]]:
    """One identity-key map per layer, keyed by the layer's singular token.

    Built in ``registry.display_order``, which fixes the walks' layer grouping order. A duplicate identity key
    within one layer raises, the same refusal ``diff`` gives.
    """
    maps: dict[str, dict[str, Any]] = {}
    for name in registry.display_order:
        layer = registry.layer(name)
        token = layer_token(layer)
        maps[token] = build_key_map(list(state.layers.get(name, ())), _identity_key(layer), token)
    return maps


def _add_edge(
    graph: AdjacencyMap,
    key: ElementRef,
    reached: ElementRef,
    detail: EdgeDetail | None,
) -> None:
    """Record key -> reached, **appending** *detail* when it is not yet recorded.

    The first edge into a pair fixes the pair's position; later edges only extend its tuple, in emission
    order.
    """
    inner = graph.setdefault(key, {})
    held = inner.setdefault(reached, ())
    if detail is not None and detail not in held:
        inner[reached] = (*held, detail)


def _emit_both(
    state: ModelState,
    registry: ModelRegistry,
    plan: Sequence[tuple[str, Sequence[_Edge]]],
) -> tuple[AdjacencyMap, AdjacencyMap]:
    """Walk *plan* over *state* once and return ``(reverse, forward)``.

    Both maps are written from the same resolved triple, so they cannot disagree about which edges exist.
    """
    reverse: AdjacencyMap = {}
    forward: AdjacencyMap = {}
    for layer_name, edges in plan:
        try:
            layer = registry.layer(layer_name)
        except KeyError:  # pragma: no cover - an edge on a layer the registry lost
            continue
        token = layer_token(layer)
        identity = _identity_key(layer)
        for element in state.layers.get(layer_name, ()):
            owner_kind = _kind_of(registry, element)
            dependent = ElementRef(token, identity(element))
            for edge in edges:
                for member in _members(registry, element, owner_kind, edge.source_kind):
                    for key in _keys(edge, member):
                        detail = edge.detail(EdgeContext(member=member, key=key))
                        for target in _targets(registry, state, edge, key):
                            _add_edge(reverse, target, dependent, detail)
                            _add_edge(forward, dependent, target, detail)
    return reverse, forward


def build_graphs(state: ModelState) -> Graphs:
    """Resolve the declared topology of *state* into both adjacency maps.

    Raises:
        ValueError: two elements in the same layer share an identity key, naming the layer and the key.
    """
    registry = current_registry()

    # Refuse duplicate identity keys before any edge: a graph over them would silently drop data.
    key_maps = _all_key_maps(state, registry)

    reverse, forward = _emit_both(state, registry, _emission_plan(registry))
    return Graphs(reverse=reverse, forward=forward, key_maps=key_maps)


#: What a walk holds per row: every ``(parent, detail)`` pair of the edges that
#: reached it, in the order met. Keyed by the pair, so an edge is never lost to
#: an equal text from another parent (:func:`_row_details`).
_Held = dict[ElementRef, dict[tuple[ElementRef, EdgeDetail], None]]


def _reach(
    held: _Held,
    start: ElementRef,
    parent: ElementRef,
    ref: ElementRef,
    details: tuple[EdgeDetail, ...],
) -> bool:
    """Record one edge into *ref*; ``True`` when *ref* is newly reached and must be pushed.

    The first edge into an element fixes its row's position; a later edge adds the details the row lacks and
    pushes nothing. The start is never a row. Each detail is held with its *parent*.
    """
    if ref == start:
        return False
    pairs = dict.fromkeys((parent, detail) for detail in details)
    row = held.get(ref)
    if row is not None:
        row.update(pairs)
        return False
    held[ref] = pairs
    return True


def _row_details(pairs: Iterable[tuple[ElementRef, EdgeDetail]]) -> tuple[EdgeDetail, ...]:
    """One row's details, one per edge — the rule :data:`_DETAIL_RENDERING` states.

    A kind in which an equal detail came from two parents is written per ``(parent, detail)`` pair with
    ``owner`` (and ``owner_layer`` where two such parents share a name); every other kind is written bare,
    equal details once, in the order met.
    """
    edges = list(pairs)
    parents: dict[EdgeDetail, set[ElementRef]] = {}
    for parent, detail in edges:
        parents.setdefault(detail, set()).add(parent)
    shared = {detail.kind for detail, reached_from in parents.items() if len(reached_from) > 1}
    # A component and a composition may share a name: such values name the layer too.
    layers: dict[tuple[str, str], set[str]] = {}
    for parent, detail in edges:
        if detail.kind in shared:
            layers.setdefault((detail.kind, parent.name), set()).add(parent.layer)
    written: list[EdgeDetail] = []
    seen: set[EdgeDetail] = set()
    for parent, detail in edges:
        if detail.kind in shared:
            # One per pair, never deduplicated by text.
            owner_layer = parent.layer if len(layers[(detail.kind, parent.name)]) > 1 else None
            written.append(EdgeDetail(detail.kind, detail.value, owner=parent.name, owner_layer=owner_layer))
        elif detail not in seen:
            seen.add(detail)
            written.append(detail)
    return tuple(written)


def _rows(held: _Held) -> list[Dependent]:
    """The walk's rows in the order their elements were first reached (a dict keeps insertion order)."""
    return [Dependent(ref=ref, details=_row_details(pairs)) for ref, pairs in held.items()]


def _walk_transitive(graph: AdjacencyMap, start: ElementRef) -> list[Dependent]:
    """The unbounded walk: LIFO, visited marked at push, insertion order per node.

    The emission order is printed verbatim, so it is public output. Every element reached is expanded, so its
    rows are complete.
    """
    held: _Held = {}
    stack = [start]
    while stack:
        current = stack.pop()
        for ref, details in graph.get(current, {}).items():
            if _reach(held, start, current, ref, details):
                stack.append(ref)
    return _rows(held)


def _walk_bounded(graph: AdjacencyMap, start: ElementRef, depth: int) -> list[Dependent]:
    """The bounded walk: breadth-first, at most *depth* levels out from *start*.

    Breadth-first rather than a depth-limited DFS, so two calls differing only in ``depth`` agree on which
    elements are within N hops (of this resolved graph). An element at the bound is a row but is not expanded:
    an edge from it would describe a hop past ``depth: N``.
    """
    held: _Held = {}
    frontier = [start]
    for _level in range(depth):
        if not frontier:
            break
        following: list[ElementRef] = []
        for current in frontier:
            for ref, details in graph.get(current, {}).items():
                if _reach(held, start, current, ref, details):
                    following.append(ref)
        frontier = following
    return _rows(held)


def integral(value: Any, minimum: int | None = None) -> int | None:
    """*value* as a JSON integer of at least *minimum*, normalised to an ``int``; ``None`` when it is not one.

    The validator for every integer field of the editing protocol and for ``swcraft query --request``'s
    ``limit``. It lives in this module because ``editing/protocol.py`` may import it and nothing here may import
    ``editing/`` back. It reads an integer as JSON Schema 2020-12 does: ``2`` and ``2.0`` are both ``2``, while ``2.5``,
    ``inf`` and ``nan`` are not. ``bool`` is refused although it is an ``int`` subclass. The returned ``int`` is
    the value the caller stores, so no caller checks one value and keeps another.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def _is_whole_number(value: Any) -> bool:
    """A plain integer — which ``True`` and ``1.0`` both are **not**.

    ``bool`` would otherwise walk one hop or page one row, and a ``float`` would escape as a ``TypeError`` from
    ``range()`` or a slice. Unlike :func:`integral`'s JSON reading, this Python caller's guard keeps an exact
    ``int``. What this does not reach: a value already coerced before it (FastMCP's lax validation, argparse's
    ``type=int``). ``swcraft impact --depth`` below 1 is refused by the CLI before this module runs, a usage
    error (exit 2), where ``query`` refuses it here (exit 1).
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _check_depth(depth: Any) -> None:
    """Refuse a *depth* no walk could run, before anything is walked.

    Asked before the matches are known, so an invalid argument is never reported as "not found". Two refusals:
    a non-integer (``True`` included) is not a hop count, and an integer below 1 is out of range.
    """
    if depth is None:
        return
    if not _is_whole_number(depth):
        raise ValueError(
            f"depth is a hop count, so it is a whole number or None for the transitive walk, not {depth!r}"
        )
    if depth < 1:
        raise ValueError(f"depth must be at least 1 or None for the transitive walk, got {depth}")


def check_limit(limit: Any) -> None:
    """Refuse a page size no window could be: not a whole number, or below 1.

    Called only with a non-``None`` value; each caller handles "no page" itself.
    """
    if not _is_whole_number(limit):
        raise ValueError(f"limit is a page size, so it is a whole number of rows, not {limit!r}")
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")


def _walk(graph: AdjacencyMap, start: ElementRef, depth: int | None) -> list[Dependent]:
    if depth is None:
        return _walk_transitive(graph, start)
    _check_depth(depth)
    return _walk_bounded(graph, start, depth)


def _matches(graphs: Graphs, name: str) -> list[ElementRef]:
    """Every declared layer's element named *name*, in ``display_order``; each match is walked separately."""
    return [ElementRef(layer, name) for layer, key_map in graphs.key_maps.items() if name in key_map]


def _walk_from(graphs: Graphs, name: str, direction: str, depth: int | None) -> dict[str, list[Dependent]]:
    graph = graphs.direction(direction)
    _check_depth(depth)
    return {start.layer: _walk(graph, start, depth) for start in _matches(graphs, name)}


def dependents(
    state: ModelState,
    name: str,
    depth: int | None = None,
) -> dict[str, list[Dependent]]:
    """Every element that transitively depends on the element(s) named *name*.

    A bare name is searched in every declared layer; each match's dependents are reported separately, grouped by
    the **matched** element's layer in the contract's display order.

    Args:
        state: the model to read.
        name: the bare identity to match in every layer.
        depth: ``None`` for the unbounded walk; a positive integer for the bounded breadth-first walk.

    Returns:
        ``{layer: [Dependent, ...]}`` for every layer with a matching element (an empty list for a match with
        no dependents); an empty dict if no layer has an element named *name*.

    Raises:
        ValueError: As :func:`related`, for *depth* or a shared identity key.
    """
    return related(state, name, "dependents", depth)


def dependencies(
    state: ModelState,
    name: str,
    depth: int | None = None,
) -> dict[str, list[Dependent]]:
    """Every element the element(s) named *name* transitively depend **on**.

    The mirror of :func:`dependents` over the forward map. A row's details are those of the edges that reached
    it, so a detail names the edge, not the end (``via port pOut`` in both directions).

    Raises:
        ValueError: As :func:`related`, for *depth* or a shared identity key.
    """
    return related(state, name, "dependencies", depth)


def related(
    state: ModelState,
    name: str,
    direction: str = DEFAULT_DIRECTION,
    depth: int | None = None,
) -> dict[str, list[Dependent]]:
    """:func:`dependents` or :func:`dependencies`, chosen by *direction*.

    The graph is built before the direction is resolved, so a duplicate identity key is reported whichever
    direction was asked for.

    Raises:
        ValueError: *direction* is not one of :data:`DIRECTIONS`; *depth* is not a whole number or is below 1;
            or two elements in the same layer share an identity key.
    """
    return _walk_from(build_graphs(state), name, direction, depth)
