"""The sequence view: one scenario drawn on a grid laid out here, in Python (no ELK).

Lifelines stand in a row in the extractor's order (the task, then each component at its first
appearance) and messages run down the page one row each. The view is read through :class:`SequenceViewLike`, so
the renderer never imports the extractor.

The picture, UML's own: a frame headed ``sd <name>``; per lifeline a header box over a dashed line;
per step a solid activation arrow from the task's line to an activation bar on the component's
line, the bar covering the data messages that step sends; per data message a dashed arrow with an
open head, labelled ``port -> port : interface``, or a loop back onto its own bar for a
self-connector. A data message's hover lists its interface's contents (at most eight lines, then
``and N more``).

Endpoints are found by a lifeline's key, not its label, so two lifelines labelled alike are two endpoints; a
lifeline whose label is shared carries the id ``lifeline-<position>``, which its messages and bars name.

What it does not reach: real text metrics, and anything the extractor did not put into the view --
an unresolvable message is a ``NotDrawn`` record there, never a guess here.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from typing import Protocol

from swcraft_tools.writer.diagrams.svg import (
    ARROW_MARKER_ID,
    BOX_STROKE,
    COMPONENT_FILL,
    DASH,
    EDGE_FONT_SIZE,
    EDGE_STROKE,
    FRAME_FILL,
    HIGHLIGHT_STROKE,
    LINE_WIDTH,
    LIT_ARROW_MARKER_ID,
    NODE_FONT_SIZE,
    PORT_FONT_SIZE,
    PORT_LABEL_FILL,
    STROKE,
    ModelEntry,
    arrow_marker,
    attribute_text,
    document,
    empty_element,
    end_at_head,
    interface_lines,
    interface_members,
    join_lines,
    number,
    polyline,
    source_attribute,
    text_element,
    text_width,
    title_element,
)

#: The open arrowhead of a data message; an activation ends in ``svg.ARROW_MARKER_ID``'s filled one.
DATA_MARKER_ID = "swcraft-arrow-open"
#: The lit twin of :data:`DATA_MARKER_ID`, which the page's style swaps a lit data message's
#: ``marker-end`` to.
LIT_DATA_MARKER_ID = f"{DATA_MARKER_ID}-lit"

#: The frame's inset from the document's edge, and the content's inset from the frame.
FRAME_INSET = 10
FRAME_PADDING = 20
HEADING_HEIGHT = 34
#: The frame heading's start inside the frame, and its baseline below the frame's top.
HEADING_INSET = 8
HEADING_BASELINE = 20

HEADER_HEIGHT = 40
#: Baselines below a header box's top: the task's name over its period, a component's name alone.
TASK_NAME_BASELINE = 17
TASK_DETAIL_BASELINE = 32
COMPONENT_NAME_BASELINE = 24
HEADER_MIN_WIDTH = 100
HEADER_TEXT_PADDING = 16
#: The clear space between two neighbouring header boxes.
HEADER_GAP = 40

BAR_WIDTH = 10
#: A message's row: its label's baseline ``MESSAGE_LABEL_BASELINE`` and its arrow
#: ``ARROW_OFFSET`` below the row's top.
FIRST_ROW_OFFSET = 16
ROW_HEIGHT = 36
MESSAGE_LABEL_BASELINE = 14
ARROW_OFFSET = 22
#: A loop runs this far right of its bar and this far down before it returns.
LOOP_WIDTH = 30
LOOP_DROP = 14
LOOP_ROW_HEIGHT = 52
#: The space around a label: between a loop and its label, and past a label's end.
LABEL_GAP = 6
LABEL_CLEARANCE = 10
#: How far a bar runs past the last row it covers, and the lifeline's tail past the last bar.
BAR_TAIL = 10
LIFELINE_TAIL = 16


#: The task's header box (a component's takes the IBD's component fill), and a message label's colour.
TASK_FILL = "#eeeeee"
MESSAGE_LABEL_FILL = "#333333"


class SequenceMessageLike(Protocol):
    """What the renderer reads of one ``graph.SequenceMessage``."""

    @property
    def kind(self) -> str: ...
    @property
    def source(self) -> Hashable: ...
    @property
    def target(self) -> Hashable: ...
    @property
    def label(self) -> str: ...
    @property
    def interface(self) -> str: ...
    @property
    def opens(self) -> ModelEntry | None: ...


class SequenceViewLike(Protocol):
    """What the renderer reads of one ``graph.SequenceView``."""

    @property
    def name(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def task(self) -> str: ...
    @property
    def task_period(self) -> float | None: ...
    @property
    def lifelines(self) -> tuple[Hashable, ...]: ...
    @property
    def messages(self) -> tuple[SequenceMessageLike, ...]: ...
    @property
    def labels(self) -> tuple[str, ...]: ...
    @property
    def opens(self) -> ModelEntry | None: ...
    @property
    def lifeline_opens(self) -> tuple[ModelEntry | None, ...]: ...


def _period(view: SequenceViewLike) -> str:
    if view.task_period is None:
        return "period not declared"
    return f"every {number(view.task_period * 1000)} ms"


def _header_width(name: str, subtitle: str) -> float:
    widest = max(text_width(name, NODE_FONT_SIZE), text_width(subtitle, PORT_FONT_SIZE) if subtitle else 0)
    return max(widest + HEADER_TEXT_PADDING, HEADER_MIN_WIDTH)


class _Grid:
    """The lifelines' centres and the messages' rows, computed before anything is drawn."""

    def __init__(self, view: SequenceViewLike) -> None:
        self.view = view
        self.subtitles = [_period(view) if index == 0 else "" for index in range(len(view.lifelines))]
        assert len(view.labels) == len(view.lifelines), "a sequence view labels every lifeline once"
        self.labels = list(view.labels)
        # The page finds an end by name, so a shared label becomes ``lifeline-<position>``.
        shared = {label for label in self.labels if self.labels.count(label) > 1}
        self.ends = [f"lifeline-{position}" if label in shared else label for position, label in enumerate(self.labels)]
        self.widths = [_header_width(name, sub) for name, sub in zip(self.labels, self.subtitles, strict=True)]
        self.index = {key: position for position, key in reversed(list(enumerate(view.lifelines)))}
        gaps = [(left + right) / 2 + HEADER_GAP for left, right in zip(self.widths, self.widths[1:], strict=False)]
        # The room a loop's label needs to the right of the last lifeline, if any.
        tail = self.widths[-1] / 2 if self.widths else 0
        for message in view.messages:
            source, target = self.index.get(message.source), self.index.get(message.target)
            if source is None or target is None:
                continue
            label = text_width(message.label, EDGE_FONT_SIZE)
            if source == target:
                needed = BAR_WIDTH / 2 + LOOP_WIDTH + LABEL_GAP + label + LABEL_CLEARANCE
                if source < len(gaps):
                    gaps[source] = max(gaps[source], needed)
                else:
                    tail = max(tail, needed)
                continue
            low, high = min(source, target), max(source, target)
            # An arrow runs from a bar's edge to a line, so it is half a bar shorter.
            needed = label + 2 * LABEL_CLEARANCE + BAR_WIDTH / 2
            span = sum(gaps[low:high])
            if span < needed:
                extra = (needed - span) / (high - low)
                for position in range(low, high):
                    gaps[position] += extra
        # Rounded up to the hundredth, so no rounding takes back the room a label was given.
        gaps = [math.ceil(round(gap * 100, 6)) / 100 for gap in gaps]
        left = FRAME_INSET + FRAME_PADDING
        self.centres: list[float] = []
        for position, width in enumerate(self.widths):
            centre = left + width / 2 if position == 0 else self.centres[-1] + gaps[position - 1]
            self.centres.append(round(centre, 2))
        self.header_top = FRAME_INSET + HEADING_HEIGHT
        top = self.header_top + HEADER_HEIGHT + FIRST_ROW_OFFSET
        self.rows: list[float] = []
        for message in view.messages:
            self.rows.append(top)
            top += LOOP_ROW_HEIGHT if message.source == message.target else ROW_HEIGHT
        self.content_bottom = top
        right = max((centre + width / 2 for centre, width in zip(self.centres, self.widths, strict=True)), default=0)
        if self.centres:
            right = max(right, self.centres[-1] + tail)
        heading = FRAME_INSET + HEADING_INSET + text_width(_heading(view), NODE_FONT_SIZE)
        self.width = max(right, heading) + FRAME_PADDING + FRAME_INSET
        self.height = self.content_bottom + LIFELINE_TAIL + FRAME_INSET + FRAME_PADDING / 2

    def centre(self, key: Hashable) -> float:
        return self.centres[self.index[key]]

    def label(self, key: Hashable) -> str:
        """The text of the lifeline keyed *key*."""
        return self.labels[self.index[key]]

    def end(self, key: Hashable) -> str:
        """The page's name of the lifeline keyed *key*: its label unless another lifeline shares it."""
        return self.ends[self.index[key]]


def _heading(view: SequenceViewLike) -> str:
    return f"sd {view.name}" + (f" — {view.title}" if view.title else "")


def _arrow(points: list[tuple[float, float]], dashed: bool) -> str:
    """A message's line through *points*, which end at the head's tip; the line itself stops short.

    It stops ``svg.HEAD_RETREAT`` before the tip so its square end lies inside the head at every
    stroke width the page uses; the marker carries the tip back to the last point.
    """
    attributes: list[tuple[str, str | float]] = [
        ("d", polyline(end_at_head(points))),
        ("fill", "none"),
        ("stroke", EDGE_STROKE),
    ]
    attributes.append(("stroke-width", LINE_WIDTH))
    if dashed:
        attributes.append(("stroke-dasharray", DASH))
    attributes.append(("marker-end", f"url(#{DATA_MARKER_ID if dashed else ARROW_MARKER_ID})"))
    return empty_element("path", attributes)


def render_sequence(
    view: SequenceViewLike,
    interface_contents: Mapping[str, Sequence[str]] | None = None,
    *,
    sources: list[ModelEntry] | None = None,
) -> str:
    """The SVG of one sequence diagram.

    *interface_contents* (``graph.DiagramGraphs.interface_contents``) feeds a data message's
    title; ``None`` lists none. *sources*, when given, collects each drawn element's model entry.
    """
    grid = _Grid(view)
    labels: list[str] = []
    frame = _draw_frame(view, grid, labels, sources)
    lifelines = _draw_lifelines(view, grid, labels, sources)
    activations = _draw_bars(view, grid)
    messages = _draw_messages(view, grid, labels, interface_contents or {}, sources)
    return document(
        grid.width,
        grid.height,
        f"sequence-{view.name}",
        f"{view.name} — sequence diagram",
        [
            ("frame", frame),
            ("lifelines", lifelines),
            ("activations", activations),
            ("messages", messages),
            ("labels", labels),
        ],
        markers=(
            arrow_marker(ARROW_MARKER_ID),
            arrow_marker(DATA_MARKER_ID, open_head=True),
            arrow_marker(LIT_ARROW_MARKER_ID, stroke=HIGHLIGHT_STROKE),
            arrow_marker(LIT_DATA_MARKER_ID, open_head=True, stroke=HIGHLIGHT_STROKE),
        ),
    )


def _draw_frame(view: SequenceViewLike, grid: _Grid, labels: list[str], sources: list[ModelEntry] | None) -> list[str]:
    """The frame's ``<g>``; its heading goes to *labels*."""
    frame_attributes: list[tuple[str, str | float]] = [("class", "frame"), ("data-role", "frame")]
    frame_attributes.append(("data-name", view.name))
    frame_attributes.extend(source_attribute(sources, view.opens))
    frame_title = join_lines(f"sequence {view.name}", view.title, f"task {view.task}")
    frame_rect = empty_element(
        "rect",
        [
            ("x", FRAME_INSET),
            ("y", FRAME_INSET),
            ("width", grid.width - 2 * FRAME_INSET),
            ("height", grid.height - 2 * FRAME_INSET),
            ("rx", 4),
            ("fill", FRAME_FILL),
            ("stroke", BOX_STROKE),
            ("stroke-width", 1.5),
        ],
    )
    labels.append(
        text_element(
            "frame-label",
            FRAME_INSET + HEADING_INSET,
            FRAME_INSET + HEADING_BASELINE,
            NODE_FONT_SIZE,
            _heading(view),
            **{"data-kind": "sequence", "font-weight": "bold"},
        )
    )
    return [f"<g{attribute_text(frame_attributes)}>{title_element(frame_title)}{frame_rect}</g>"]


def _draw_lifelines(
    view: SequenceViewLike, grid: _Grid, labels: list[str], sources: list[ModelEntry] | None
) -> list[str]:
    """Each lifeline's ``<g>``, header box and dashed line; its name and detail go to *labels*."""
    lifelines: list[str] = []
    line_bottom = grid.content_bottom + LIFELINE_TAIL
    # A misaligned view raises rather than open the wrong file.
    if sources is not None and len(view.lifeline_opens) != len(grid.labels):
        raise ValueError("a sequence view's lifeline_opens does not hold one entry per lifeline")
    for position, name in enumerate(grid.labels):
        centre, width = grid.centres[position], grid.widths[position]
        is_task = position == 0
        kind = "task" if is_task else "component"
        detail = f"task, {grid.subtitles[0]}" if is_task else "component"
        attributes: list[tuple[str, str | float]] = [
            ("class", f"lifeline {kind}"),
            ("data-role", "lifeline"),
            ("data-name", name),
            ("data-kind", kind),
            *source_attribute(sources, None if sources is None else view.lifeline_opens[position]),
        ]
        if grid.ends[position] != name:
            attributes.insert(0, ("id", grid.ends[position]))
        box = empty_element(
            "rect",
            [
                ("x", centre - width / 2),
                ("y", grid.header_top),
                ("width", width),
                ("height", HEADER_HEIGHT),
                ("rx", 3),
                ("fill", TASK_FILL if is_task else COMPONENT_FILL),
                ("stroke", BOX_STROKE),
                ("stroke-width", 1),
            ],
        )
        line = empty_element(
            "line",
            [
                ("x1", centre),
                ("y1", grid.header_top + HEADER_HEIGHT),
                ("x2", centre),
                ("y2", line_bottom),
                ("stroke", EDGE_STROKE),
                ("stroke-width", 1),
                ("stroke-dasharray", DASH),
            ],
        )
        lifelines.append(f"<g{attribute_text(attributes)}>{title_element(f'{name} ({detail})')}{box}{line}</g>")
        name_y = grid.header_top + (TASK_NAME_BASELINE if is_task else COMPONENT_NAME_BASELINE)
        labels.append(
            text_element(
                "lifeline-label",
                centre,
                name_y,
                NODE_FONT_SIZE,
                name,
                **{"data-kind": kind, "text-anchor": "middle", "font-weight": "600"},
            )
        )
        if is_task:
            labels.append(
                text_element(
                    "lifeline-detail",
                    centre,
                    grid.header_top + TASK_DETAIL_BASELINE,
                    PORT_FONT_SIZE,
                    grid.subtitles[0],
                    **{"text-anchor": "middle", "fill": PORT_LABEL_FILL},
                )
            )
    return lifelines


def _draw_bars(view: SequenceViewLike, grid: _Grid) -> list[str]:
    """The activation bars: an activation opens one, the data messages after it fill it."""
    open_bar: tuple[Hashable, float, float] | None = None  # (lifeline key, top, bottom so far)
    bars: list[tuple[Hashable, float, float]] = []
    for message, row in zip(view.messages, grid.rows, strict=True):
        arrow_y = row + ARROW_OFFSET
        if message.kind == "activation":
            if open_bar is not None:
                bars.append(open_bar)
            open_bar = (message.target, arrow_y, arrow_y + BAR_TAIL) if message.target in grid.index else None
        elif open_bar is not None and message.source == open_bar[0]:
            bottom = arrow_y + (LOOP_DROP if message.source == message.target else 0) + BAR_TAIL
            open_bar = (open_bar[0], open_bar[1], bottom)
    if open_bar is not None:
        bars.append(open_bar)
    return [
        empty_element(
            "rect",
            [
                ("class", "activation-bar"),
                ("data-lifeline", grid.end(lifeline)),
                ("x", grid.centre(lifeline) - BAR_WIDTH / 2),
                ("y", top),
                ("width", BAR_WIDTH),
                ("height", bottom - top),
                ("fill", "white"),
                ("stroke", STROKE),
                ("stroke-width", 1),
            ],
        )
        for lifeline, top, bottom in bars
    ]


def _draw_messages(
    view: SequenceViewLike,
    grid: _Grid,
    labels: list[str],
    interface_contents: Mapping[str, Sequence[str]],
    sources: list[ModelEntry] | None,
) -> list[str]:
    """Each message's ``<g>`` and arrow; its label goes to *labels*."""
    messages: list[str] = []
    for message, row in zip(view.messages, grid.rows, strict=True):
        if message.source not in grid.index or message.target not in grid.index:
            continue
        arrow_y = row + ARROW_OFFSET
        source, target = grid.centre(message.source), grid.centre(message.target)
        label_y = row + MESSAGE_LABEL_BASELINE
        if message.kind == "activation":
            end = target - BAR_WIDTH / 2 if target >= source else target + BAR_WIDTH / 2
            points = [(source, arrow_y), (end, arrow_y)]
            path = _arrow(points, dashed=False)
            labels.append(_message_label((source + end) / 2, label_y, message.label))
        elif message.source == message.target:
            start = source + BAR_WIDTH / 2
            far = start + LOOP_WIDTH
            points = [(start, arrow_y), (far, arrow_y), (far, arrow_y + LOOP_DROP), (start, arrow_y + LOOP_DROP)]
            path = _arrow(points, dashed=True)
            labels.append(
                text_element(
                    "message-label", far + LABEL_GAP, label_y, EDGE_FONT_SIZE, message.label, fill=MESSAGE_LABEL_FILL
                )
            )
        else:
            start = source + BAR_WIDTH / 2 if target >= source else source - BAR_WIDTH / 2
            points = [(start, arrow_y), (target, arrow_y)]
            path = _arrow(points, dashed=True)
            labels.append(_message_label((start + target) / 2, label_y, message.label))
        attributes = [
            ("class", f"message {message.kind}"),
            ("data-role", message.kind),
            ("data-name", message.label),
            ("data-source", grid.end(message.source)),
            ("data-target", grid.end(message.target)),
            *source_attribute(sources, message.opens),
        ]
        head = f"{message.kind} {grid.label(message.source)} -> {grid.label(message.target)}: {message.label}"
        # A data message has no list to open, so its count line names no click.
        members = interface_members(interface_contents, message.interface)
        title = join_lines(head, *interface_lines(members, click_line=False))
        messages.append(f"<g{attribute_text(attributes)}>{title_element(title)}{path}</g>")
    return messages


def _message_label(x: float, y: float, text: str) -> str:
    extra = {"text-anchor": "middle", "fill": MESSAGE_LABEL_FILL}
    return text_element("message-label", x, y, EDGE_FONT_SIZE, text, **extra)
