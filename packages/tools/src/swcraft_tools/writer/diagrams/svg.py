"""The SVG renderer: a laid-out ELK graph in, SVG text out.

It reads positions, sizes and the facts under :data:`METADATA_KEY`, never a layout option, imports only the leaf
``text_metrics.py``, and calls no process. Deterministic: fixed attribute order, every number rounded again by
:func:`number`. Five layers, DOM order being paint order: frame, edges, members, ports, labels.

Glyphs follow AUTOSAR's VFB port figure: kind and direction are in the shape, never a fill
(:data:`GLYPH_STEM_AND_SHAPE`). Every port points out of its box, the boundary port included; a delegation ends on
the inner face of the boundary square, with no glyph. A port of no declared type draws the bare square.

Every drawn element carries a ``<title>`` first: the hover text and the page's search text. A port's title lists its
interface's members (at most :data:`INTERFACE_LINES_CAP`, the full list in a :data:`MEMBER_LIST_CLASS` ``<desc>``);
an overview's count square lists its connectors in a :data:`CONNECTOR_LIST_CLASS` ``<desc>``. A line the validator
does not judge is dashed (:data:`DASH`) and carries the reason in its title. Edges and ends carry ordinal ids and
``data-source``/``data-target``. A change report's marks colour members and bundles (:data:`CHANGE_STROKES`).

Not reached: real text metrics (a port label overrunning runs into its member's box, never over
the glyph), and a change value other than the named ones, drawn unmarked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TypedDict
from urllib.parse import quote

from swcraft_tools.writer.diagrams.text_metrics import text_width as text_width  # re-exported: the grids draw with it

#: Equal to ``graph.METADATA_KEY``, not imported: this module imports no model reader.
METADATA_KEY = "swcraft"

#: The font sizes the extractor estimated label boxes with.
NODE_FONT_SIZE = 12
PORT_FONT_SIZE = 10
EDGE_FONT_SIZE = 10

FONT_FAMILY = "Helvetica, Arial, sans-serif"
STROKE = "#222222"
EDGE_STROKE = "#555555"
#: The outline of a frame, a member box and a lifeline header.
BOX_STROKE = "#333333"
#: The width of a line, a glyph and an arrow; a box outline is 1, a frame's 1.5.
LINE_WIDTH = 1.2
FRAME_FILL = "#fafafa"
COMPONENT_FILL = "#eef3fb"
COMPOSITION_FILL = "#fff4d6"
PORT_LABEL_FILL = "#444444"

#: The hollow square on the border.
SQUARE = 12
#: The port glyphs' shapes, in VFB figure proportions: the sender's pentagon arrow, the
#: receiver's fork, the server's circle and the client's half circle.
GLYPH_HEIGHT = 12
SENDER_ARROW_WIDTH = 16
SENDER_ARROW_POINT = 6
RECEIVER_FORK_WIDTH = 8
SERVER_CIRCLE_WIDTH = 12
CLIENT_HALF_CIRCLE_WIDTH = 6

#: Each glyph's stem and shape, in pixels from the square's outer edge to the tip, keyed by
#: ``(direction, client-server)``; their sums equal ``graph.PORT_REACH``.
GLYPH_STEM_AND_SHAPE: dict[tuple[str, bool], tuple[int, int]] = {
    ("provide", False): (4, SENDER_ARROW_WIDTH),
    ("require", False): (10, RECEIVER_FORK_WIDTH),
    ("provide", True): (8, SERVER_CIRCLE_WIDTH),
    ("require", True): (8, CLIENT_HALF_CIRCLE_WIDTH),
}

#: The interface kinds that draw a glyph; any other kind is the bare square.
GLYPH_KINDS = frozenset({"sender_receiver", "client_server"})

#: The arrowhead a sequence message and an overview bundle end in, following its line; the
#: IBD's glyphs carry direction themselves, so an IBD draws no marker at all.
ARROW_MARKER_ID = "swcraft-arrow"

#: An arrowhead's figures in canvas pixels (``markerUnits="userSpaceOnUse"``, so they hold at
#: every stroke width): its length along the line, tip to base, and its width across the base.
HEAD_LENGTH = 8
HEAD_WIDTH = 8
#: The widest stroke a line that points can take (the page's current search hit).
WIDEST_POINTING_STROKE = 4
#: How far short of its tip a line that points ends (:func:`end_at_head`). A butt line end is a
#: square as wide as the stroke; a line of width ``w`` lies inside the head wherever it stops
#: ``w * HEAD_LENGTH / HEAD_WIDTH`` behind the tip, so at the widest stroke the line stops there
#: and the marker's ``refX`` puts the tip back where the line was drawn to.
HEAD_RETREAT = HEAD_LENGTH * WIDEST_POINTING_STROKE / HEAD_WIDTH

#: The same head in the highlight's colour. A marker's fill is out of CSS's reach, so the page
#: swaps ``marker-end`` to this one rather than recolouring the head.
LIT_ARROW_MARKER_ID = f"{ARROW_MARKER_ID}-lit"

#: The colour of a lit line, its ends and its head: the page's link blue, so a lit element stands
#: out from the near-black strokes. ``page.PAGE_STYLE`` uses the same value.
HIGHLIGHT_STROKE = "#1a5fb4"

#: An edge path's ``id`` is this prefix and its ordinal; its hit area is a ``<use>`` of it. One
#: letter, because ids repeat on every edge and end and their bytes count on a large model.
EDGE_PATH_ID_PREFIX = "e"
#: The ``id`` prefixes of the two kinds of end a line names: an IBD's port, an overview's box.
PORT_ID_PREFIX = "p"
MEMBER_ID_PREFIX = "m"
#: The stroke width a hit area gives the clone of its path: a line a pointer can rest on.
HIT_AREA_WIDTH = 10
#: The one dash the diagrams draw: an unjudged line here, a sequence's message and lifeline there.
DASH = "5 4"
#: The class of a port's hit box, last in its group; the page's highlight rules exclude it.
PORT_HIT_CLASS = "port-hit"
#: The hit areas' width, declared once per document and in the SVG's own ``<style>`` so a view
#: opened alone keeps it. It undoes an inherited dash, which would take the pointer on dashes only.
HIT_AREA_STYLE = f"g.edges use{{stroke-width:{HIT_AREA_WIDTH}px;stroke-dasharray:none}}"
#: What :func:`_class_slug` folds to one ``-``: each run of characters outside ``[a-z0-9]``.
_NOT_A_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")

#: The class of the ``<desc>`` in an overview's count square listing its connectors, one row per line.
CONNECTOR_LIST_CLASS = "connector-list"

#: What divides a row's line from its description; whitespace inside both is collapsed first.
CONNECTOR_ROW_SEPARATOR = "\t"

#: A hover lists at most this many of an interface's member lines, then one line counting the rest.
INTERFACE_LINES_CAP = 8

#: The class of the ``<desc>`` in a port's group listing every member line of its interface, only
#: on a port over :data:`INTERFACE_LINES_CAP`.
MEMBER_LIST_CLASS = "member-list"

#: Marks a member line in a hover; a prefix, not an indent, since ``pre-line`` collapses spaces.
MEMBER_LINE_PREFIX = "· "

#: The frame label of a composition some composition places is the way up: the frame's
#: name behind :data:`UP_ARROW`, in a group of this class carrying its own ``<title>``.
FRAME_UP_CLASS = "frame-up"
UP_ARROW = "\u2191"

#: The metadata key a change report sets on a member or a bundle of an overview (``change.py``).
CHANGE_KEY = "change"
#: Each change's stroke: added green, removed red, both (a line that gained and lost) blue.
CHANGE_ADDED_STROKE = "#1a7f37"
CHANGE_REMOVED_STROKE = "#cf222e"
CHANGE_BOTH_STROKE = "#0969da"
CHANGE_STROKES: dict[str, str] = {
    "added": CHANGE_ADDED_STROKE,
    "removed": CHANGE_REMOVED_STROKE,
    "both": CHANGE_BOTH_STROKE,
}
#: The width of a marked outline or line.
CHANGE_STROKE_WIDTH = 3
#: The head a marked bundle points with, one per change, in its colour.
CHANGE_MARKER_IDS: dict[str, str] = {change: f"{ARROW_MARKER_ID}-{change}" for change in CHANGE_STROKES}
#: The changes a member can carry; ``both`` is a line's alone.
_MEMBER_CHANGES = frozenset({"added", "removed"})

#: The attributes a member's or a frame's label carries beside its position: its kind and its weight.
_LabelAttributes = TypedDict("_LabelAttributes", {"data-kind": str, "font-weight": str}, total=False)

_BOLD: _LabelAttributes = {"font-weight": "bold"}
#: A block diagram's frame label: a composition's kind, bold.
_FRAME_LABEL: _LabelAttributes = {"data-kind": "composition", **_BOLD}
#: The second class of a port label drawn inside its member's box (:func:`_port_label`).
IN_MEMBER_CLASS = "in-member"

#: Equal to ``graph.OPENS_KEY``, not imported.
OPENS_KEY = "opens"

#: The temporary attribute naming a drawn element's model entry by its index in the caller's
#: list. ``page.strip_sources`` removes every one before anything reads the text, so no written
#: file carries it.
SOURCE_ATTRIBUTE = "data-swcraft-source"

#: ``graph.ModelEntry``, spelled again here because no renderer imports the extractor.
ModelEntry = Mapping[str, str | None]


def source_attribute(sources: list[ModelEntry] | None, entry: ModelEntry | None) -> list[tuple[str, str | float]]:
    """The temporary :data:`SOURCE_ATTRIBUTE` naming *entry*'s index in *sources*, or nothing.

    Nothing when *sources* or *entry* is ``None``; otherwise *entry* is appended to *sources*.
    """
    if sources is None or entry is None:
        return []
    sources.append(entry)
    return [(SOURCE_ATTRIBUTE, str(len(sources) - 1))]


def number(value: float) -> str:
    """*value* rounded to two decimals, without trailing zeros, and never ``-0``."""
    text = f"{round(float(value), 2):.2f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def escape(text: str) -> str:
    """*text* safe inside an element and inside a double-quoted attribute."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def attribute_text(attributes: Sequence[tuple[str, str | float]]) -> str:
    """The attributes in the order given: a string escaped, a number through :func:`number`."""
    parts = []
    for name, value in attributes:
        rendered = escape(value) if isinstance(value, str) else number(value)
        parts.append(f' {name}="{rendered}"')
    return "".join(parts)


def empty_element(tag: str, attributes: Sequence[tuple[str, str | float]]) -> str:
    """One self-closing element."""
    return f"<{tag}{attribute_text(attributes)}/>"


def member_label(name: str, type_name: str) -> str:
    """A member's label: its name, or ``name : type`` when they differ; ``graph.py``'s rule."""
    return name if name == type_name else f"{name} : {type_name}"


def title_element(text: str) -> str:
    """The ``<title>`` a drawn element carries first: its hover text and its search text."""
    return f"<title>{escape(text)}</title>"


def _desc_list(css_class: str, rows: Sequence[str]) -> str:
    """A ``<desc>`` of class *css_class* carrying *rows*, one per line: it draws nothing."""
    return f'<desc class="{css_class}">{escape(chr(10).join(rows))}</desc>'


def join_lines(*lines: str) -> str:
    """The non-empty *lines*, one per line."""
    return "\n".join(line for line in lines if line)


def text_element(css_class: str, x: float, y: float, size: float, text: str, **extra: str) -> str:
    """One ``<text>`` at a baseline point; *extra* attributes follow the four fixed ones."""
    attributes: list[tuple[str, str | float]] = [("class", css_class), ("x", x), ("y", y), ("font-size", size)]
    attributes.extend(extra.items())
    return f"<text{attribute_text(attributes)}>{escape(text)}</text>"


def arrow_marker(marker_id: str, *, open_head: bool = False, stroke: str = EDGE_STROKE) -> str:
    """An arrowhead marker: a filled triangle, or an open ``V`` drawn as a line.

    The head follows the line and is sized in canvas pixels, so it does not grow when the page
    thickens a lit line. *stroke* is the head's own colour. It is anchored :data:`HEAD_RETREAT`
    behind its tip, where the line it ends stops (:func:`end_at_head`).
    """
    length, width, half = number(HEAD_LENGTH), number(HEAD_WIDTH), number(HEAD_WIDTH / 2)
    outline = f"M 0 0 L {length} {half} L 0 {width}"
    if open_head:
        head = empty_element(
            "path",
            [("d", outline), ("fill", "none"), ("stroke", stroke), ("stroke-width", LINE_WIDTH)],
        )
    else:
        head = empty_element("path", [("d", f"{outline} z"), ("fill", stroke)])
    return (
        f'<marker id="{marker_id}" viewBox="0 0 {length} {width}" refX="{number(HEAD_LENGTH - HEAD_RETREAT)}"'
        f' refY="{half}" markerWidth="{length}" markerHeight="{width}" markerUnits="userSpaceOnUse"'
        f' orient="auto-start-reverse">{head}</marker>'
    )


def end_at_head(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """*points* with the last one moved :data:`HEAD_RETREAT` back along the last segment.

    *points* end at the tip, where the marker puts its tip back. Not reached: a last segment no
    longer than :data:`HEAD_RETREAT` is left whole, since shortening it would turn the head around.
    """
    *body, (x0, y0), (x1, y1) = points
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if length <= HEAD_RETREAT:
        return list(points)
    share = (length - HEAD_RETREAT) / length
    return [*body, (x0, y0), (x0 + (x1 - x0) * share, y0 + (y1 - y0) * share)]


def document(
    width: float,
    height: float,
    view: str,
    heading: str,
    layers: Sequence[tuple[str, Sequence[str]]],
    markers: Sequence[str] = (),
    style: str = "",
) -> str:
    """One SVG document: the root, its title, its style, the markers, a white background, then *layers*.

    Each layer is one ``<g class="<name>">`` in the order given, which is the paint order.
    A document given no *style* carries no ``<style>`` element.
    """
    head = attribute_text(
        [
            ("xmlns", "http://www.w3.org/2000/svg"),
            ("width", width),
            ("height", height),
            ("viewBox", f"0 0 {number(width)} {number(height)}"),
            ("font-family", FONT_FAMILY),
            ("data-view", view),
        ]
    )
    lines = [f"<svg{head}>", title_element(heading)]
    if style:
        lines.append(f"<style>{style}</style>")
    if markers:
        lines.append("<defs>" + "".join(markers) + "</defs>")
    lines.append(
        empty_element("rect", [("class", "background"), ("width", "100%"), ("height", "100%"), ("fill", "white")])
    )
    for name, items in layers:
        lines.append(f'<g class="{name}">')
        lines.extend(items)
        lines.append("</g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _meta(element: Mapping[str, Any]) -> Mapping[str, Any]:
    return element.get(METADATA_KEY) or {}


class _Drawing:
    """The layers of one view, filled in document order and joined once.

    *sources* is the caller's list of model entries, or ``None``.
    """

    def __init__(self, sources: list[ModelEntry] | None = None) -> None:
        self.sources = sources
        self.frame: list[str] = []
        self.edges: list[str] = []
        self.members: list[str] = []
        self.ports: list[str] = []
        self.labels: list[str] = []
        # element id -> the name its title shows (``Owner.port`` for a port).
        self.names: dict[str, str] = {}
        # element id -> the ``id`` it carries in the SVG, for every end a line can name.
        self.ids: dict[str, str] = {}
        # Whether any element carries a change mark: the coloured markers are emitted only then.
        self.marked = False
        self._paths = 0

    def path_id(self) -> str:
        """The next edge path's ``id``: an ordinal, a legal fragment whatever the model's names spell."""
        self._paths += 1
        return f"{EDGE_PATH_ID_PREFIX}{self._paths}"

    @property
    def path_count(self) -> int:
        """How many edge paths -- and so hit areas -- the view has drawn."""
        return self._paths

    def node_label(
        self,
        css_class: str,
        ox: float,
        oy: float,
        label: Mapping[str, Any],
        size: int,
        *,
        east: bool = False,
        **extra: str,
    ) -> None:
        """:meth:`label_text`, appended to the view's labels."""
        self.labels.append(self.label_text(css_class, ox, oy, label, size, east=east, **extra))

    @staticmethod
    def label_text(
        css_class: str,
        ox: float,
        oy: float,
        label: Mapping[str, Any],
        size: int,
        *,
        east: bool = False,
        **extra: str,
    ) -> str:
        """A label at its laid-out box: two pixels in, the baseline one font size below its top.

        *east* places the text two pixels short of the box's east end instead of two
        pixels past its west one; the caller names the matching ``text-anchor``.
        """
        x = ox + label.get("x", 0)
        x = x + label.get("width", 0) - 2 if east else x + 2
        y = oy + label.get("y", 0) + (label.get("height", size + 4) - size) / 2 + size * 0.8
        return text_element(css_class, x, y, size, str(label.get("text", "")), **extra)


# ---------------------------------------------------------------------------
# Ports and glyphs
# ---------------------------------------------------------------------------


def interface_lines(lines: Sequence[str], *, click_line: bool) -> list[str]:
    """An interface's member *lines* as a hover shows them: capped, each behind :data:`MEMBER_LINE_PREFIX`.

    Over the cap, one line counts the rest, naming the click only on a port (*click_line*),
    since a data message has no list to open.
    """
    shown = [f"{MEMBER_LINE_PREFIX}{line}" for line in lines[:INTERFACE_LINES_CAP]]
    rest = len(lines) - INTERFACE_LINES_CAP
    if rest > 0:
        more = f"and {rest} more"
        shown.append(f"{more} — click the port to list all {len(lines)}" if click_line else more)
    return shown


def interface_members(interface_contents: Mapping[str, Sequence[str]], interface: str) -> Sequence[str]:
    """The full member lines of *interface*; none for ``""``, the name of no interface.

    An empty name never looks the map up, so an entry keyed ``""`` lists nothing.
    """
    return interface_contents.get(interface, ()) if interface else ()


def _port_title(meta: Mapping[str, Any], owner_name: str, interface: str, members: Sequence[str]) -> str:
    words = meta.get("interfaceWord") or "undeclared interface"
    direction = str(meta.get("direction", ""))
    joined = f"{direction} {interface}" if interface else direction
    head = f"{owner_name}.{meta.get('name', '')}: {joined} ({words})"
    return join_lines(head, *interface_lines(members, click_line=True), str(meta.get("description") or ""))


def member_list(members: Sequence[str]) -> str:
    """The ``<desc>`` listing a capped port's every member line, or ``""`` for a port within the cap.

    One unprefixed line per row, in the model's order, so the cap hides nothing from the page's
    panel and search.
    """
    if len(members) <= INTERFACE_LINES_CAP:
        return ""
    return _desc_list(MEMBER_LIST_CLASS, members)


def _glyph(tip_x: float, border_x: float, cy: float, outward: int, direction: str, client_server: bool) -> str:
    """The stem and the VFB shape, from the square's outer edge to the tip.

    The stem is drawn from the square rather than from the table's figure, so it always starts
    on the square whatever the laid-out box leaves.
    """
    stem_start = border_x + outward * SQUARE / 2
    half = GLYPH_HEIGHT / 2
    _, width = GLYPH_STEM_AND_SHAPE[("provide" if direction == "provide" else "require", client_server)]
    near = tip_x - outward * width
    shapes = [_line(stem_start, cy, near, cy)]
    if client_server and direction == "provide":
        shape = empty_element(
            "circle",
            [
                ("cx", tip_x - outward * half),
                ("cy", cy),
                ("r", half),
                ("fill", "white"),
                ("stroke", STROKE),
                ("stroke-width", LINE_WIDTH),
            ],
        )
    else:
        if client_server:
            # From the mouth's top end to its bottom end through the side nearer the box:
            # a half circle opening west runs through its east side (clockwise, sweep 1).
            sweep = 1 if outward < 0 else 0
            d = (
                f"M {number(tip_x)} {number(cy - half)} "
                f"A {number(half)} {number(half)} 0 0 {sweep} {number(tip_x)} {number(cy + half)}"
            )
            fill = "none"
        elif direction == "provide":
            shoulder = tip_x - outward * SENDER_ARROW_POINT
            d = (
                f"M {number(near)} {number(cy - half)} L {number(shoulder)} {number(cy - half)} "
                f"L {number(tip_x)} {number(cy)} L {number(shoulder)} {number(cy + half)} "
                f"L {number(near)} {number(cy + half)} Z"
            )
            fill = "white"
        else:
            d = (
                f"M {number(near)} {number(cy)} L {number(tip_x)} {number(cy - half)} "
                f"M {number(near)} {number(cy)} L {number(tip_x)} {number(cy + half)}"
            )
            fill = "none"
        shape = empty_element("path", [("d", d), ("fill", fill), ("stroke", STROKE), ("stroke-width", LINE_WIDTH)])
    shapes.append(shape)
    return '<g class="glyph">' + "".join(shapes) + "</g>"


def _line(x1: float, y1: float, x2: float, y2: float) -> str:
    return empty_element(
        "line", [("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2), ("stroke", STROKE), ("stroke-width", LINE_WIDTH)]
    )


def _port_tip(port: Mapping[str, Any], px: float) -> tuple[float, int]:
    """The port's glyph tip and the direction it points, ``-1`` west or ``+1`` east.

    Every port points out of its box, the frame's boundary port included; the tip is the far end
    of the laid-out box on that side.
    """
    outward = -1 if _meta(port).get("direction") == "require" else 1
    width = port.get("width", 0)
    return (px if outward < 0 else px + width), outward


def _draw_port(
    drawing: _Drawing,
    port: Mapping[str, Any],
    ox: float,
    oy: float,
    owner_name: str,
    interface_contents: Mapping[str, Sequence[str]],
    *,
    in_member: bool = False,
    member_kind: str | None = None,
) -> None:
    meta = _meta(port)
    px, py = ox + port.get("x", 0), oy + port.get("y", 0)
    tip_x, outward = _port_tip(port, px)
    reach = port.get("width", 0) - SQUARE / 2
    border_x = tip_x - outward * reach
    cy = py + port.get("height", 0) / 2
    kind = meta.get("interfaceKind")
    kind_class = _class_slug(str(meta.get("interfaceWord") or "unknown-interface"))
    direction = str(meta.get("direction", ""))
    interface = str(meta.get("interface") or "")
    attributes: list[tuple[str, str | float]] = [
        ("id", drawing.ids[str(port.get("id", ""))]),
        ("class", f"port {direction} {kind_class}"),
        ("data-role", "port"),
        ("data-owner", str(meta.get("owner") or "")),
        ("data-name", str(meta.get("name", ""))),
        ("data-interface", interface),
        *source_attribute(drawing.sources, meta.get(OPENS_KEY)),
    ]
    box = empty_element(
        "rect",
        [
            ("class", "port-box"),
            ("x", border_x - SQUARE / 2),
            ("y", cy - SQUARE / 2),
            ("width", SQUARE),
            ("height", SQUARE),
            ("fill", "white"),
            ("stroke", STROKE),
            ("stroke-width", 1),
        ],
    )
    # A glyph only for a declared type: any other kind is the bare square.
    glyph = _glyph(tip_x, border_x, cy, outward, direction, kind == "client_server") if kind in GLYPH_KINDS else ""
    # The whole port takes the pointer: its laid-out box, drawn at opacity 0, last.
    hit = empty_element(
        "rect",
        [
            ("class", PORT_HIT_CLASS),
            ("x", px),
            ("y", cy - SQUARE / 2),
            ("width", port.get("width", 0)),
            ("height", SQUARE),
            ("opacity", "0"),
        ],
    )
    members = interface_members(interface_contents, interface)
    title = title_element(_port_title(meta, owner_name, interface, members)) + member_list(members)
    drawing.ports.append(f"<g{attribute_text(attributes)}>{title}{box}{glyph}{hit}</g>")
    for label in port.get("labels", []):
        _port_label(drawing, label, px, py, east=direction == "provide", in_member=in_member, member_kind=member_kind)


def _class_slug(word: str) -> str:
    """*word* as one class name: lowercased, each run of characters outside ``[a-z0-9]`` one ``-``."""
    return _NOT_A_SLUG_CHARACTER.sub("-", word.lower())


def _port_label(
    drawing: _Drawing,
    label: Mapping[str, Any],
    px: float,
    py: float,
    *,
    east: bool,
    in_member: bool,
    member_kind: str | None,
) -> None:
    """A port's label anchored at the port's side of its box, so an overrun runs away from the glyph.

    A label inside a member's box (*in_member*) carries :data:`IN_MEMBER_CLASS` and the member's
    ``data-kind`` (when known), so a stylesheet can colour it on that box.
    """
    anchor = "end" if east else "start"
    extra = {"text-anchor": anchor}
    css_class = f"port-label {IN_MEMBER_CLASS}" if in_member else "port-label"
    if member_kind is not None:
        extra = {"data-kind": member_kind, **extra}
    drawing.node_label(css_class, px, py, label, PORT_FONT_SIZE, east=east, fill=PORT_LABEL_FILL, **extra)


# ---------------------------------------------------------------------------
# Frames, members and edges
# ---------------------------------------------------------------------------


def _frame_heading(meta: Mapping[str, Any], label_text: str) -> str:
    title = str(meta.get("title") or "")
    name = str(meta.get("name", ""))
    return f"{label_text} — {title}" if title and title != name else label_text


def parent_token(parent: str, member: str) -> str:
    """One entry of a frame's ``data-parents``: the parent's name, ``#``, and the member placing it there.

    Both percent-encoded, so an entry holds no space and no ``#`` of its own; the part after
    ``#`` is the fragment the parent's page opens at.
    """
    return f"{quote(parent, safe='')}#{quote(member, safe='')}"


def _up_title(parents: Sequence[tuple[str, str]]) -> str:
    """The way up's hover text: where one parent leads, or the list several make."""
    if len(parents) == 1:
        return f"up to {parents[0][0]}"
    return "placed in: " + ", ".join(parent for parent, _ in parents)


def _draw_frame(drawing: _Drawing, frame: Mapping[str, Any], fx: float, fy: float) -> None:
    meta = _meta(frame)
    name = str(meta.get("name", ""))
    title = str(meta.get("title") or "")
    text = join_lines(f"composition {name}", title if title != name else "", str(meta.get("description") or ""))
    rect = empty_element(
        "rect",
        [
            ("x", fx),
            ("y", fy),
            ("width", frame.get("width", 0)),
            ("height", frame.get("height", 0)),
            ("rx", 4),
            ("fill", FRAME_FILL),
            ("stroke", BOX_STROKE),
            ("stroke-width", 1.5),
        ],
    )
    parents = [(str(parent), str(member)) for parent, member in meta.get("parents") or []]
    attributes: list[tuple[str, str | float]] = [("class", "frame"), ("data-role", "frame"), ("data-name", name)]
    if parents:
        attributes.append(("data-parents", " ".join(parent_token(parent, member) for parent, member in parents)))
    attributes.extend(source_attribute(drawing.sources, meta.get(OPENS_KEY)))
    drawing.frame.append(f"<g{attribute_text(attributes)}>{title_element(text)}{rect}</g>")
    for label in frame.get("labels", []):
        heading = _frame_heading(meta, str(label.get("text", "")))
        if not parents:
            drawing.node_label("frame-label", fx, fy, dict(label, text=heading), NODE_FONT_SIZE, **_FRAME_LABEL)
            continue
        # The way up: the frame's name behind the arrow, in a group carrying where it leads.
        up = title_element(_up_title(parents)) + drawing.label_text(
            "frame-label", fx, fy, dict(label, text=f"{UP_ARROW} {heading}"), NODE_FONT_SIZE, **_FRAME_LABEL
        )
        drawing.labels.append(f'<g class="{FRAME_UP_CLASS}">{up}</g>')


def _member_kind(meta: Mapping[str, Any]) -> str | None:
    """A member's kind, or ``None`` when its metadata names none, so no label carries an empty ``data-kind``."""
    kind = meta.get("kind")
    return str(kind) if kind else None


def _draw_member(drawing: _Drawing, member: Mapping[str, Any], mx: float, my: float) -> None:
    meta = _meta(member)
    name, type_name, kind = str(meta.get("name", "")), str(meta.get("type", "")), str(meta.get("kind", ""))
    label = member_label(name, type_name)
    fill = COMPOSITION_FILL if kind == "composition" else COMPONENT_FILL
    change = meta.get(CHANGE_KEY)
    outline: list[tuple[str, str | float]] = [("stroke", BOX_STROKE), ("stroke-width", 1)]
    if change in _MEMBER_CHANGES:
        drawing.marked = True
        outline = [("stroke", CHANGE_STROKES[change]), ("stroke-width", CHANGE_STROKE_WIDTH)]
        if change == "removed":
            outline.append(("stroke-dasharray", DASH))
    rect = empty_element(
        "rect",
        [
            ("x", mx),
            ("y", my),
            ("width", member.get("width", 0)),
            ("height", member.get("height", 0)),
            ("rx", 3),
            ("fill", fill),
            *outline,
        ],
    )
    attributes: list[tuple[str, str | float]] = [
        ("class", f"member {kind}"),
        ("data-role", "member"),
        ("data-name", name),
        ("data-type", type_name),
        ("data-kind", kind),
        *source_attribute(drawing.sources, meta.get(OPENS_KEY)),
    ]
    element_id = drawing.ids.get(str(member.get("id", "")))
    if element_id is not None:
        attributes.insert(0, ("id", element_id))
    text = join_lines(f"{label} ({kind})", str(meta.get("description") or ""))
    drawing.members.append(f"<g{attribute_text(attributes)}>{title_element(text)}{rect}</g>")
    member_kind = _member_kind(meta)
    label_attributes: _LabelAttributes = {"font-weight": "600"}
    if member_kind is not None:
        label_attributes = {"data-kind": member_kind, **label_attributes}
    for node_label in member.get("labels", []):
        drawing.node_label("member-label", mx, my, node_label, NODE_FONT_SIZE, **label_attributes)


def _points(section: Mapping[str, Any], ox: float, oy: float) -> list[tuple[float, float]]:
    raw = [section.get("startPoint", {}), *section.get("bendPoints", []), section.get("endPoint", {})]
    return [(ox + point.get("x", 0), oy + point.get("y", 0)) for point in raw]


def polyline(points: Sequence[tuple[float, float]]) -> str:
    """The path data of a straight-segment line through *points*."""
    head, *rest = points
    return " ".join([f"M {number(head[0])} {number(head[1])}", *(f"L {number(x)} {number(y)}" for x, y in rest)])


def link_line(name: str, source: str, target: str, *, loop: bool = False) -> str:
    """The one line that says what a connector or a delegation joins, and nothing else.

    ``<name>: <owner>.<port> -> <owner>.<port>``, with ``(self-connector)`` where the two ends
    are one member. Shared by an IBD line's hover and an overview panel's row, so they agree.
    """
    return f"{name}: {source} -> {target}" + (" (self-connector)" if loop else "")


def connector_row(connector: Mapping[str, Any]) -> str:
    """One row of an overview bundle's connector list: its line, its interface, its description.

    The head is :func:`link_line`'s, with ``— <interface>`` when the source end declares one. A
    description follows after a tab; its own whitespace is collapsed, so the row stays one line
    and the separator unambiguous.
    """
    line = link_line(
        str(connector.get("name", "")),
        str(connector.get("source", "")),
        str(connector.get("target", "")),
        loop=bool(connector.get("loop")),
    )
    interface = str(connector.get("interface") or "")
    head = f"{line} — {interface}" if interface else line
    note = " ".join(str(connector.get("description") or "").split())
    return f"{head}{CONNECTOR_ROW_SEPARATOR}{note}" if note else head


def _edge_title(edge: Mapping[str, Any], names: Mapping[str, str]) -> tuple[str, str]:
    """The edge's ``data-name`` and its title."""
    meta = _meta(edge)
    role = str(meta.get("role", ""))
    source = names.get(str((edge.get("sources") or [""])[0]), "")
    target = names.get(str((edge.get("targets") or [""])[0]), "")
    if role == "bundle":
        count = int(meta.get("count", len(meta.get("connectors", []))))
        noun = "connector" if count == 1 else "connectors"
        pair = f"{source} -> {target}"
        # The head line alone: a browser's native tooltip truncates a long list and cannot
        # scroll, so the list is the count square's ``<desc>``, shown in the page's panel.
        return pair, f"{count} {noun} {pair}"
    name = str(meta.get("name", ""))
    head = f"{role} {link_line(name, source, target, loop=bool(meta.get('loop')))}"
    # A line the validator does not judge says why, verbatim, before its description.
    return name, join_lines(head, str(meta.get("unverified") or ""), str(meta.get("description") or ""))


def _draw_edge(
    drawing: _Drawing,
    edge: Mapping[str, Any],
    origins: Mapping[str, tuple[float, float]],
) -> None:
    meta = _meta(edge)
    role = str(meta.get("role", ""))
    ox, oy = origins.get(str(edge.get("container", "")), origins["frame"])
    name, text = _edge_title(edge, drawing.names)
    classes = f"edge {role}" + (" loop" if meta.get("loop") else "")
    attributes: list[tuple[str, str | float]] = [("class", classes), ("data-role", role), ("data-name", name)]
    source_id, target_id = (drawing.ids.get(str((edge.get(key) or [""])[0])) for key in ("sources", "targets"))
    if source_id is not None and target_id is not None:
        attributes.extend([("data-source", source_id), ("data-target", target_id)])
    change = meta.get(CHANGE_KEY) if role == "bundle" else None
    marked = change in CHANGE_STROKES
    if marked:
        drawing.marked = True
    attributes.append(("stroke-width", CHANGE_STROKE_WIDTH if marked else 1))
    if meta.get("unverified"):
        attributes.append(("stroke-dasharray", DASH))
    attributes.extend(source_attribute(drawing.sources, meta.get(OPENS_KEY)))
    stroke = CHANGE_STROKES[change] if change in CHANGE_STROKES else EDGE_STROKE
    marker = CHANGE_MARKER_IDS[change] if change in CHANGE_MARKER_IDS else ARROW_MARKER_ID
    paths = []
    sections = edge.get("sections", [])
    for index, section in enumerate(sections):
        points = _points(section, ox, oy)
        pointing = role == "bundle" and index == len(sections) - 1
        if pointing:
            points = end_at_head(points)
        path_id = drawing.path_id()
        path: list[tuple[str, str | float]] = [
            ("id", path_id),
            ("d", polyline(points)),
            ("fill", "none"),
            ("stroke", stroke),
        ]
        if pointing:
            path.append(("marker-end", f"url(#{marker})"))
        paths.append(empty_element("path", path))
        paths.append(_hit_area(path_id))
    drawing.edges.append(f"<g{attribute_text(attributes)}>{title_element(text)}{''.join(paths)}</g>")
    connectors = [connector_row(connector) for connector in meta.get("connectors", [])]
    for label in edge.get("labels", []):
        drawing.labels.append(_edge_count(label, ox, oy, name, text, connectors))


def _hit_area(path_id: str) -> str:
    """A line's hit area: a ``<use>`` of its own path, wide (:data:`HIT_AREA_STYLE`) and drawing nothing.

    Never a second path repeating ``d``. The clone takes the hit width because the path
    declares none of its own; an element at ``opacity`` 0 still takes the pointer.
    """
    return empty_element("use", [("href", f"#{path_id}"), ("opacity", "0")])


def _edge_count(label: Mapping[str, Any], ox: float, oy: float, name: str, text: str, connectors: Sequence[str]) -> str:
    """The overview's count square: one group carrying its edge's own ``<title>`` and its list.

    The title is the edge's head line alone; the connectors travel in a
    :data:`CONNECTOR_LIST_CLASS` ``<desc>``, one :func:`connector_row` per line, which renders
    nothing, keeps a row's spaces and carries the list once. The page's panel and search read it.
    """
    x, y = ox + label.get("x", 0), oy + label.get("y", 0)
    width, height = label.get("width", 0), label.get("height", 0)
    square = empty_element(
        "rect",
        [
            ("class", "edge-label"),
            ("x", x - 2),
            ("y", y - 1),
            ("width", width + 4),
            ("height", height + 2),
            ("rx", 2),
            ("fill", "white"),
            ("stroke", "#999999"),
        ],
    )
    count = text_element(
        "edge-label-text",
        x + width / 2,
        y + (height - EDGE_FONT_SIZE) / 2 + EDGE_FONT_SIZE * 0.8,
        EDGE_FONT_SIZE,
        str(label.get("text", "")),
        **{"text-anchor": "middle", "font-weight": "600"},
    )
    attributes: list[tuple[str, str | float]] = [("class", "edge-count"), ("data-name", name)]
    listing = _desc_list(CONNECTOR_LIST_CLASS, connectors) if connectors else ""
    return f"<g{attribute_text(attributes)}>{title_element(text)}{listing}{square}{count}</g>"


# ---------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------


def _collect(
    drawing: _Drawing, frame: Mapping[str, Any], fx: float, fy: float, with_ports: bool
) -> dict[str, tuple[float, float]]:
    """Every node's absolute origin, the name each port and member shows in an edge title, and the ids.

    The ends a line can name take an ordinal ``id``: on an IBD every port (in drawing order), on
    an overview every member box.
    """
    frame_name = str(_meta(frame).get("name", ""))
    origins = {str(frame.get("id", "frame")): (fx, fy), "frame": (fx, fy)}
    ports: list[Mapping[str, Any]] = []
    for port in frame.get("ports", []):
        drawing.names[str(port.get("id", ""))] = f"{frame_name}.{_meta(port).get('name', '')}"
        ports.append(port)
    for index, member in enumerate(frame.get("children", []), start=1):
        member_name = str(_meta(member).get("name", ""))
        origins[str(member.get("id", ""))] = (fx + member.get("x", 0), fy + member.get("y", 0))
        drawing.names[str(member.get("id", ""))] = member_name
        if not with_ports:
            drawing.ids[str(member.get("id", ""))] = f"{MEMBER_ID_PREFIX}{index}"
        for port in member.get("ports", []):
            drawing.names[str(port.get("id", ""))] = f"{member_name}.{_meta(port).get('name', '')}"
            ports.append(port)
    if with_ports:
        for index, port in enumerate(ports, start=1):
            drawing.ids[str(port.get("id", ""))] = f"{PORT_ID_PREFIX}{index}"
    return origins


def _render(
    graph: Mapping[str, Any],
    kind: str,
    prefix: str,
    with_ports: bool,
    interface_contents: Mapping[str, Sequence[str]] | None = None,
    sources: list[ModelEntry] | None = None,
) -> str:
    contents = interface_contents or {}
    drawing = _Drawing(sources)
    frames: Iterable[Mapping[str, Any]] = graph.get("children", [])
    names = []
    for frame in frames:
        fx, fy = graph.get("x", 0) + frame.get("x", 0), graph.get("y", 0) + frame.get("y", 0)
        frame_name = str(_meta(frame).get("name", ""))
        names.append(frame_name)
        origins = _collect(drawing, frame, fx, fy, with_ports)
        _draw_frame(drawing, frame, fx, fy)
        for port in frame.get("ports", []) if with_ports else []:
            _draw_port(drawing, port, fx, fy, frame_name, contents)
        for edge in frame.get("edges", []):
            _draw_edge(drawing, edge, origins)
        for member in frame.get("children", []):
            mx, my = origins[str(member.get("id", ""))]
            _draw_member(drawing, member, mx, my)
            member_meta = _meta(member)
            member_name = str(member_meta.get("name", ""))
            for port in member.get("ports", []) if with_ports else []:
                _draw_port(
                    drawing, port, mx, my, member_name, contents, in_member=True, member_kind=_member_kind(member_meta)
                )
    view_name = names[0] if names else ""
    markers: list[str] = []
    if not with_ports:
        markers = [arrow_marker(ARROW_MARKER_ID), arrow_marker(LIT_ARROW_MARKER_ID, stroke=HIGHLIGHT_STROKE)]
        if drawing.marked:
            markers.extend(
                arrow_marker(CHANGE_MARKER_IDS[change], stroke=CHANGE_STROKES[change]) for change in CHANGE_STROKES
            )
    layers: list[tuple[str, Sequence[str]]] = [
        ("frame", drawing.frame),
        ("edges", drawing.edges),
        ("members", drawing.members),
    ]
    if with_ports:
        layers.append(("ports", drawing.ports))
    layers.append(("labels", drawing.labels))
    return document(
        graph.get("width", 0),
        graph.get("height", 0),
        f"{prefix}-{view_name}",
        f"{view_name} — {kind}",
        layers,
        markers=markers,
        style=HIT_AREA_STYLE if drawing.path_count else "",
    )


def render_ibd(
    graph: Mapping[str, Any],
    interface_contents: Mapping[str, Sequence[str]] | None = None,
    *,
    sources: list[ModelEntry] | None = None,
) -> str:
    """The SVG of one composition's internal block diagram, laid out.

    *interface_contents* (``graph.DiagramGraphs.interface_contents``) feeds each port's title;
    ``None`` lists no member lines. *sources*, when given, collects each element's model entry.
    """
    return _render(
        graph, "internal block diagram", "ibd", with_ports=True, interface_contents=interface_contents, sources=sources
    )


def render_overview(graph: Mapping[str, Any], *, sources: list[ModelEntry] | None = None) -> str:
    """The SVG of one composition's bundled overview, laid out; *sources* as :func:`render_ibd`'s."""
    return _render(graph, "overview", "overview", with_ports=False, sources=sources)
