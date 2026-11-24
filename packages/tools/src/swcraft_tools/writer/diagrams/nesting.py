"""The nesting overview: the composition hierarchy as boxes in boxes, packed here in Python (no ELK).

The root composition is outermost, headed with its name and its ``title`` when that differs; a
composition member is a box of its own members headed at its top left, a component a smaller box
with its label centred, labelled ``name : type`` when the two differ. A composition the extractor
drew without members (a cut cycle, a missing type) is an empty box. A composition places its members left to right
in declared order, wrapping at :data:`ROW_WIDTH`; every label carries its box's ``data-kind``.

:func:`render_nesting` also draws the ``deployment`` view, with its own view id, fills, leaf
kinds and leaf titles; its defaults are the nesting overview's.

What it does not reach: real text metrics, a balanced packing (rows fill greedily), and anything
but the tree -- ports and connectors are the IBD's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from swcraft_tools.writer.diagrams.svg import (
    BOX_STROKE,
    COMPONENT_FILL,
    COMPOSITION_FILL,
    FRAME_FILL,
    NODE_FONT_SIZE,
    ModelEntry,
    attribute_text,
    document,
    empty_element,
    join_lines,
    member_label,
    source_attribute,
    text_element,
    text_width,
    title_element,
)

#: The widest a row of members may grow before the next member starts a new row.
ROW_WIDTH = 960
#: The space inside a composition around its members, and between two members.
PADDING = 12
GAP = 12
#: A composition's heading band, and where its label's baseline sits in it.
HEADING_HEIGHT = 26
HEADING_BASELINE = 17
COMPONENT_HEIGHT = 36
MINIMUM_WIDTH = 120
#: The document's margin around the root.
MARGIN = 20

#: The nesting overview's view id, leaf kinds and fills below the root (the root takes ``FRAME_FILL``).
NESTING_VIEW = "nesting"
NESTING_LEAVES: frozenset[str] = frozenset({"component"})
NESTING_FILLS: Mapping[str, str] = {"composition": COMPOSITION_FILL, "component": COMPONENT_FILL}


class NestingNodeLike(Protocol):
    """What the renderer reads of one ``graph.NestingNode``."""

    @property
    def name(self) -> str: ...
    @property
    def type(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def children(self) -> Sequence[NestingNodeLike]: ...
    @property
    def opens(self) -> ModelEntry | None: ...


@dataclass
class _Placed:
    """One box, sized and then placed; its children placed relative to the document."""

    node: NestingNodeLike
    width: float
    height: float
    children: list[_Placed]
    x: float = 0
    y: float = 0


@dataclass(frozen=True)
class _Drawing:
    """What one view makes of the tree: which kinds are leaves, and whether a leaf shows its title."""

    leaves: frozenset[str]
    leaf_titles: bool

    def is_leaf(self, node: NestingNodeLike) -> bool:
        return node.kind in self.leaves

    def shows_title(self, node: NestingNodeLike) -> bool:
        return bool(node.title) and node.title != node.type and (self.leaf_titles or not self.is_leaf(node))


def _label(node: NestingNodeLike, drawing: _Drawing) -> str:
    text = member_label(node.name, node.type)
    if drawing.shows_title(node):
        text = f"{text} — {node.title}"
    return text


def _size(node: NestingNodeLike, drawing: _Drawing) -> _Placed:
    label = text_width(_label(node, drawing), NODE_FONT_SIZE) + 2 * PADDING
    if drawing.is_leaf(node):
        return _Placed(node, max(label, MINIMUM_WIDTH), COMPONENT_HEIGHT, [])
    children = [_size(child, drawing) for child in node.children]
    rows = _rows(children)
    inner_width = max((sum(child.width for child in row) + GAP * (len(row) - 1) for row in rows), default=0)
    inner_height = sum(max(child.height for child in row) for row in rows) + GAP * max(len(rows) - 1, 0)
    width = max(label, inner_width + 2 * PADDING, MINIMUM_WIDTH)
    height = HEADING_HEIGHT + inner_height + PADDING
    return _Placed(node, round(width, 2), round(height, 2), children)


def _rows(children: list[_Placed]) -> list[list[_Placed]]:
    rows: list[list[_Placed]] = []
    width = 0.0
    for child in children:
        if rows and width + GAP + child.width <= ROW_WIDTH:
            rows[-1].append(child)
            width += GAP + child.width
        else:
            rows.append([child])
            width = child.width
    return rows


def _place(box: _Placed, x: float, y: float) -> None:
    box.x, box.y = round(x, 2), round(y, 2)
    top = y + HEADING_HEIGHT
    for row in _rows(box.children):
        left = x + PADDING
        for child in row:
            _place(child, left, top)
            left += child.width + GAP
        top += max(child.height for child in row) + GAP


def _preorder(box: _Placed) -> list[_Placed]:
    return [box, *(item for child in box.children for item in _preorder(child))]


def _title(box: _Placed, root: bool, drawing: _Drawing) -> str:
    node = box.node
    extra = node.title if node.title and node.title != node.type else ""
    if root:
        return join_lines(f"{node.kind} {node.name}", extra)
    return join_lines(f"{member_label(node.name, node.type)} ({node.kind})", extra if drawing.shows_title(node) else "")


def render_nesting(
    root: NestingNodeLike,
    *,
    view: str = NESTING_VIEW,
    fills: Mapping[str, str] = NESTING_FILLS,
    leaves: frozenset[str] = NESTING_LEAVES,
    leaf_titles: bool = False,
    sources: list[ModelEntry] | None = None,
) -> str:
    """The SVG of boxes in boxes under *root*: by default the nesting overview of the root composition.

    *view* is the document's ``data-view``; *fills* the fill of each kind below the root, never
    defaulted, so a kind the caller did not name fails loudly; *leaves* the kinds drawn as a leaf;
    *leaf_titles* whether a leaf's ``title`` joins its label, as a container's always does.
    *sources*, when given, collects each box's model entry.
    """
    drawing = _Drawing(leaves, leaf_titles)
    placed = _size(root, drawing)
    _place(placed, MARGIN, MARGIN)
    frame: list[str] = []
    members: list[str] = []
    labels: list[str] = []
    for index, box in enumerate(_preorder(placed)):
        node, is_root = box.node, index == 0
        container = not drawing.is_leaf(node)
        fill = FRAME_FILL if is_root else fills[node.kind]
        rect = empty_element(
            "rect",
            [
                ("x", box.x),
                ("y", box.y),
                ("width", box.width),
                ("height", box.height),
                ("rx", 4 if is_root else 3),
                ("fill", fill),
                ("stroke", BOX_STROKE),
                ("stroke-width", 1.5 if is_root else 1),
            ],
        )
        attributes: list[tuple[str, str | float]] = [
            ("class", "frame" if is_root else f"member {node.kind}"),
            ("data-role", "frame" if is_root else "member"),
            ("data-name", node.name),
            ("data-type", node.type),
            ("data-kind", node.kind),
            *source_attribute(sources, node.opens),
        ]
        element = f"<g{attribute_text(attributes)}>{title_element(_title(box, is_root, drawing))}{rect}</g>"
        (frame if is_root else members).append(element)
        if container:
            labels.append(
                text_element(
                    "frame-label" if is_root else "member-label",
                    box.x + PADDING,
                    box.y + HEADING_BASELINE,
                    NODE_FONT_SIZE,
                    _label(node, drawing),
                    **{"data-kind": node.kind, "font-weight": "bold" if is_root else "600"},
                )
            )
        else:
            labels.append(
                text_element(
                    "member-label",
                    box.x + box.width / 2,
                    box.y + box.height / 2 + NODE_FONT_SIZE * 0.35,
                    NODE_FONT_SIZE,
                    _label(node, drawing),
                    **{"data-kind": node.kind, "text-anchor": "middle"},
                )
            )
    return document(
        placed.width + 2 * MARGIN,
        placed.height + 2 * MARGIN,
        view,
        f"{root.name} — {view}",
        [("frame", frame), ("members", members), ("labels", labels)],
    )
