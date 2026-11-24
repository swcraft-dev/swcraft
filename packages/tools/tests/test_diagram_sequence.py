"""The sequence view: ``writer/diagrams/sequence.py``, a grid laid out in Python (no ELK, no Node).

Input is the one scenario of ``fixtures/diagrams/model``; the golden ``fixtures/diagrams/svg/sequence-speed_cycle.svg``
is written by ``python packages/tools/tests/test_diagram_svg.py --freeze-svg``, never by hand.

What these tests do not reach: whether the picture reads well. The golden is a change detector, and the label widths are
the extractor's per-character estimate, not measured text.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import xml.etree.ElementTree as ET

import pytest
from conftest import REPO_ROOT
from swcraft_tools.writer.diagrams import graph, page, sequence, svg
from typing_extensions import get_protocol_members

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
GOLDEN = FIXTURE_DIR / "svg" / "sequence-speed_cycle.svg"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"

NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def view() -> graph.SequenceView:
    (extracted,) = graph.build_graphs(str(FIXTURE_DIR / "model")).sequences
    return extracted


def _root(text: str) -> ET.Element:
    return ET.fromstring(text)


def _layer(root: ET.Element, name: str) -> ET.Element:
    return next(element for element in root if element.tag == f"{NS}g" and element.get("class") == name)


def _numbers(text: str) -> list[float]:
    return [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", text)]


def _lifelines(root: ET.Element) -> list[ET.Element]:
    return list(_layer(root, "lifelines"))


def _centre(lifeline: ET.Element) -> float:
    line = lifeline.find(f"{NS}line")
    assert line.get("x1") == line.get("x2")
    return float(line.get("x1", ""))


def _messages(root: ET.Element) -> list[ET.Element]:
    return list(_layer(root, "messages"))


def _path_points(element: ET.Element) -> list[tuple[float, float]]:
    values = _numbers(element.find(f"{NS}path").get("d", ""))
    return list(zip(values[0::2], values[1::2], strict=True))


def _head(root: ET.Element, marker_id: str) -> tuple[float, float, float]:
    """The head *marker_id* names, read from the emitted marker, in canvas pixels.

    ``(ahead, length, half)``: how far the tip lies ahead of the line's end, the head's length tip to base, and its
    half-width at the base.
    """
    marker = next(element for element in root.find(f"{NS}defs") if element.get("id") == marker_id)
    assert marker.get("markerUnits") == "userSpaceOnUse"
    _, _, box_width, box_height = _numbers(marker.get("viewBox", ""))
    scale = float(marker.get("markerWidth", "")) / box_width
    assert float(marker.get("markerHeight", "")) / box_height == scale
    values = _numbers(marker[0].get("d", ""))
    outline = list(zip(values[0::2], values[1::2], strict=True))
    tip = max(outline)
    assert tip[1] == float(marker.get("refY", ""))  # the tip sits on the line's axis
    base = min(x for x, _ in outline)
    ys = [y for _, y in outline]
    return (tip[0] - float(marker.get("refX", ""))) * scale, (tip[0] - base) * scale, (max(ys) - min(ys)) / 2 * scale


def _direction(points: list[tuple[float, float]]) -> tuple[float, float]:
    (x0, y0), (x1, y1) = points[-2:]
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    return (x1 - x0) / length, (y1 - y0) / length


def _marker_id(element: ET.Element) -> str:
    return element.find(f"{NS}path").get("marker-end", "").removeprefix("url(#").removesuffix(")")


def _tip(root: ET.Element, element: ET.Element, marker_id: str | None = None) -> tuple[float, float]:
    """Where the head of *element*'s line puts its tip: the line's end, carried ahead along its last segment."""
    points = _path_points(element)
    ahead, _, _ = _head(root, marker_id or _marker_id(element))
    dx, dy = _direction(points)
    x, y = points[-1]
    return round(x + dx * ahead, 2), round(y + dy * ahead, 2)


def _view(**changes: object) -> graph.SequenceView:
    base = graph.SequenceView(
        name="s",
        title="",
        task="T",
        task_period=0.005,
        lifelines=("T", "A", "B"),
        messages=(
            graph.SequenceMessage("activation", "T", "A", "run"),
            graph.SequenceMessage("data", "A", "B", "p -> r : I"),
        ),
        labels=("T", "A", "B"),
    )
    if "lifelines" in changes and "labels" not in changes:
        changes["labels"] = tuple(str(key) for key in changes["lifelines"])  # type: ignore[union-attr]
    return dataclasses.replace(base, **changes)


# The document


def test_two_renders_are_byte_identical_and_equal_the_golden(view: graph.SequenceView) -> None:
    # The golden is rendered as the generator renders it: with the fixture model's member lines.
    contents = graph.build_graphs(str(FIXTURE_DIR / "model")).interface_contents
    first = sequence.render_sequence(view, contents)
    assert sequence.render_sequence(view, contents) == first
    assert first == GOLDEN.read_text(encoding="utf-8")
    assert first.endswith("</svg>\n")


def test_a_lifeline_opens_tuple_out_of_step_with_the_lifelines_raises_only_when_entries_are_collected() -> None:
    """``lifeline_opens`` is read by position; a tuple out of step raises only when entries are collected.

    A view whose tuple does not hold one entry per lifeline raises when the caller collects model entries, as
    ``page.strip_sources`` does, rather than open the wrong file; without collection (the change report, a
    drawing test) it is never read.
    """
    opens = ({"file": "tasks/T.yaml", "member": None}, {"file": "swc/A.yaml", "member": None}, None)
    sources: list[svg.ModelEntry] = []
    sequence.render_sequence(_view(lifeline_opens=opens), sources=sources)
    assert sources[-2:] == [opens[0], opens[1]]
    with pytest.raises(ValueError, match="one entry per lifeline"):
        sequence.render_sequence(_view(lifeline_opens=opens[:2]), sources=[])
    assert sequence.render_sequence(_view()) == sequence.render_sequence(_view(lifeline_opens=opens))


def test_the_document_names_its_view_and_is_sized_by_its_viewbox(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    assert root.get("data-view") == "sequence-speed_cycle"
    assert root.find(f"{NS}title").text == "speed_cycle — sequence diagram"
    assert root.get("viewBox") == f"0 0 {root.get('width')} {root.get('height')}"
    assert re.findall(r"\d+\.\d{3,}", sequence.render_sequence(view)) == []


def test_the_sequence_is_drawn_frame_lifelines_activations_messages_labels(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    layers = [element.get("class") for element in root if element.tag == f"{NS}g"]
    assert layers == ["frame", "lifelines", "activations", "messages", "labels"]


def test_every_drawn_element_carries_a_title_first(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    drawn = [element for element in root.iter(f"{NS}g") if element.get("data-role")]
    assert len(drawn) == 1 + len(view.lifelines) + len(view.messages)
    for element in drawn:
        assert element[0].tag == f"{NS}title" and element[0].text


def test_the_frame_is_headed_with_the_sequence_and_its_title(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    frame = _layer(root, "frame")[0]
    assert (frame.get("data-role"), frame.get("data-name")) == ("frame", "speed_cycle")
    assert frame[0].text == "sequence speed_cycle\nSpeed cycle\ntask Task_10ms"
    texts = [element.text for element in _layer(root, "labels") if element.tag == f"{NS}text"]
    assert texts[0] == "sd speed_cycle — Speed cycle"
    untitled = _root(sequence.render_sequence(_view()))
    assert [element.text for element in _layer(untitled, "labels")][0] == "sd s"
    assert _layer(untitled, "frame")[0][0].text == "sequence s\ntask T"


def test_the_frame_encloses_every_lifeline_and_message(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    rect = _layer(root, "frame")[0].find(f"{NS}rect")
    left, top = float(rect.get("x", "")), float(rect.get("y", ""))
    right, bottom = left + float(rect.get("width", "")), top + float(rect.get("height", ""))
    assert right <= float(root.get("width", "")) and bottom <= float(root.get("height", ""))
    for name in ("lifelines", "activations", "messages"):
        for element in _layer(root, name).iter():
            for attribute in ("x", "x1", "x2"):
                if element.get(attribute) is not None:
                    assert left < float(element.get(attribute, "")) < right, (name, attribute)
            for attribute in ("y", "y1", "y2"):
                if element.get(attribute) is not None:
                    assert top < float(element.get(attribute, "")) < bottom, (name, attribute)


# The lifelines


def test_the_lifelines_run_left_to_right_in_the_extractors_order(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    lifelines = _lifelines(root)
    # The ``data-*`` names read a lifeline's label; its key places it.
    assert [element.get("data-name") for element in lifelines] == list(view.labels)
    assert [element.get("data-kind") for element in lifelines] == ["task", "component", "component"]
    assert [element[0].text for element in lifelines] == [
        "Task_10ms (task, every 10 ms)",
        "Sensor (component)",
        "Controller (component)",
    ]
    centres = [_centre(element) for element in lifelines]
    assert centres == sorted(centres) and len(set(centres)) == len(centres)
    boxes = [element.find(f"{NS}rect") for element in lifelines]
    for left, right in zip(boxes, boxes[1:], strict=False):
        assert float(left.get("x", "")) + float(left.get("width", "")) < float(right.get("x", ""))


def test_a_lifeline_is_a_header_box_over_a_dashed_line_below_the_last_message(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    lowest = max(y for message in _messages(root) for _, y in _path_points(message))
    for lifeline in _lifelines(root):
        box, line = lifeline.find(f"{NS}rect"), lifeline.find(f"{NS}line")
        assert round(float(box.get("x", "")) + float(box.get("width", "")) / 2, 2) == _centre(lifeline)
        assert float(line.get("y1", "")) == round(float(box.get("y", "")) + float(box.get("height", "")), 2)
        assert float(line.get("y2", "")) > lowest
        assert line.get("stroke-dasharray")


def test_a_task_without_a_declared_period_says_so() -> None:
    root = _root(sequence.render_sequence(_view(task_period=None)))
    assert _lifelines(root)[0][0].text == "T (task, period not declared)"
    assert _lifelines(_root(sequence.render_sequence(_view())))[0][0].text == "T (task, every 5 ms)"


def test_a_sequence_without_steps_draws_the_task_alone() -> None:
    root = _root(sequence.render_sequence(_view(lifelines=("T",), messages=())))
    assert [element.get("data-name") for element in _lifelines(root)] == ["T"]
    assert _messages(root) == [] and list(_layer(root, "activations")) == []


# The messages


def test_the_messages_run_top_to_bottom_in_order_with_their_titles(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    messages = _messages(root)
    assert [(element.get("data-role"), element.get("data-name")) for element in messages] == [
        (message.kind, message.label) for message in view.messages
    ]
    label_of = dict(zip(view.lifelines, view.labels, strict=True))
    assert [(element.get("data-source"), element.get("data-target")) for element in messages] == [
        (label_of[message.source], label_of[message.target]) for message in view.messages
    ]
    assert [element[0].text for element in messages] == [
        "activation Task_10ms -> Sensor: SensorRun",
        "data Sensor -> Controller: pSpeed -> rSpeed : ISpeed",
        "activation Task_10ms -> Controller: ControlRun — closes the loop",
        "data Controller -> Controller: pLoop -> rLoop : ISpeed",
    ]
    starts = [_path_points(element)[0][1] for element in messages]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_an_activation_is_solid_and_a_data_message_dashed(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    for element in _messages(root):
        path = element.find(f"{NS}path")
        if element.get("data-role") == "activation":
            assert path.get("stroke-dasharray") is None
            assert path.get("marker-end") == f"url(#{svg.ARROW_MARKER_ID})"
        else:
            assert path.get("stroke-dasharray")
            assert path.get("marker-end") == f"url(#{sequence.DATA_MARKER_ID})"
    markers = [marker.get("id") for marker in root.find(f"{NS}defs")]
    assert markers == [
        svg.ARROW_MARKER_ID,
        sequence.DATA_MARKER_ID,
        svg.LIT_ARROW_MARKER_ID,
        sequence.LIT_DATA_MARKER_ID,
    ]


def test_an_unlit_message_keeps_its_own_black_head_whatever_its_kind(view: graph.SequenceView) -> None:
    """The lit twins are only defined; every message's ``marker-end`` names its unlit head.

    The page's style swaps a lit message's head, so a page with no highlight draws what it drew before.
    """
    root = _root(sequence.render_sequence(view))
    heads = {
        (element.get("data-role"), element.get("data-source") == element.get("data-target")): element.find(
            f"{NS}path"
        ).get("marker-end")
        for element in _messages(root)
    }
    assert heads == {
        ("activation", False): f"url(#{svg.ARROW_MARKER_ID})",
        ("data", False): f"url(#{sequence.DATA_MARKER_ID})",
        ("data", True): f"url(#{sequence.DATA_MARKER_ID})",
    }


def test_the_lit_twins_share_their_heads_geometry_and_differ_only_in_the_highlight_colour(
    view: graph.SequenceView,
) -> None:
    """A lit message points with a blue head of the same form as the one it carries unlit.

    A marker's colour is a hard attribute inside it (``fill`` or ``stroke``), which CSS on the path cannot reach.
    """
    assert sequence.LIT_DATA_MARKER_ID == f"{sequence.DATA_MARKER_ID}-lit"
    defs = _root(sequence.render_sequence(view)).find(f"{NS}defs")
    heads = {marker.get("id"): marker.find(f"{NS}path") for marker in defs}
    filled, lit_filled = heads[svg.ARROW_MARKER_ID], heads[svg.LIT_ARROW_MARKER_ID]
    opened, lit_open = heads[sequence.DATA_MARKER_ID], heads[sequence.LIT_DATA_MARKER_ID]
    assert lit_filled.get("fill") == svg.HIGHLIGHT_STROKE
    assert lit_open.get("stroke") == svg.HIGHLIGHT_STROKE
    assert lit_open.get("fill") == "none"
    assert {**lit_filled.attrib, "fill": filled.get("fill")} == filled.attrib
    assert {**lit_open.attrib, "stroke": opened.get("stroke")} == opened.attrib


#: The four markers a sequence view defines: the filled and the open head, and their lit twins in the highlight's blue
#: that the page's style swaps a lit message's ``marker-end`` to. ``markerUnits="userSpaceOnUse"`` because the page
#: thickens a lit line and the default ``strokeWidth`` unit made the head grow with it; all four are anchored at
#: ``refX="4"``, half a head behind the tip, because a message's line stops there (its square end showed past the
#: point).
SEQUENCE_MARKERS = (
    '<defs><marker id="swcraft-arrow" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="8"'
    ' markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
    '<path d="M 0 0 L 8 4 L 0 8 z" fill="#555555"/>'
    '</marker><marker id="swcraft-arrow-open" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="8"'
    ' markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
    '<path d="M 0 0 L 8 4 L 0 8" fill="none"'
    ' stroke="#555555" stroke-width="1.2"/>'
    '</marker><marker id="swcraft-arrow-lit" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="8"'
    ' markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
    '<path d="M 0 0 L 8 4 L 0 8 z" fill="#1a5fb4"/>'
    '</marker><marker id="swcraft-arrow-open-lit" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="8"'
    ' markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
    '<path d="M 0 0 L 8 4 L 0 8" fill="none"'
    ' stroke="#1a5fb4" stroke-width="1.2"/></marker></defs>'
)


#: The head a lit message points with, by the head it carries unlit: the page's swap.
LIT_TWIN = {svg.ARROW_MARKER_ID: svg.LIT_ARROW_MARKER_ID, sequence.DATA_MARKER_ID: sequence.LIT_DATA_MARKER_ID}


def _page_widths() -> set[float]:
    """Every stroke width the page's style can give a line: the highlight's, a hit's, the current hit's.

    Not the hit band's (``page.HIT_BAND_CLASS``): a headless transparent clone that only takes the pointer.
    """
    style = re.sub(rf"svg \.{page.HIT_BAND_CLASS} \{{[^}}]*\}}", "", page.PAGE_STYLE)
    assert style != page.PAGE_STYLE, "the band's rule is where this reader expects it"
    return {float(width) for width in re.findall(r"stroke-width:\s*(\d+(?:\.\d+)?)px", style)}


def test_every_message_line_ends_inside_its_head_at_every_width_lit_or_not(view: graph.SequenceView) -> None:
    """A message's line never shows past its head, lit or not, at every stroke width the page can set.

    Every figure is read from the emitted markers.
    """
    root = _root(sequence.render_sequence(view))
    widths = _page_widths()
    assert 2.5 in widths and 4 in widths
    kinds = set()
    for message in _messages(root):
        path = message.find(f"{NS}path")
        own = float(path.get("stroke-width", ""))
        unlit = _marker_id(message)
        kinds.add((message.get("data-role"), message.get("data-source") == message.get("data-target")))
        for marker_id in (unlit, LIT_TWIN[unlit]):
            ahead, length, half = _head(root, marker_id)
            assert 0 < ahead <= length, (message.get("data-name"), marker_id)
            for width in {own, *widths}:
                assert width / 2 <= ahead * half / length, (message.get("data-name"), marker_id, width)
    assert kinds == {("activation", False), ("data", False), ("data", True)}


def test_a_heads_tip_still_touches_the_message_target_lit_or_not(view: graph.SequenceView) -> None:
    """The line stops short, the head does not: its tip is where the message arrives, lit or not."""
    root = _root(sequence.render_sequence(view))
    centres = {element.get("data-name"): _centre(element) for element in _lifelines(root)}
    bars = list(_layer(root, "activations"))
    edges = {
        bar.get("data-lifeline"): (float(bar.get("x", "")), float(bar.get("x", "")) + float(bar.get("width", "")))
        for bar in bars
    }
    for message in _messages(root):
        points = _path_points(message)
        target, source = message.get("data-target"), message.get("data-source")
        tip = _tip(root, message)
        assert _tip(root, message, LIT_TWIN[_marker_id(message)]) == tip
        if message.get("data-role") == "activation":
            expected = round(edges[target][0], 2)
        elif source == target:
            expected = round(edges[target][1], 2)
        else:
            expected = centres[target]
        assert tip == (expected, points[-1][1]), message.get("data-name")
        # The line itself ends svg.HEAD_RETREAT short of that point.
        assert abs(abs(tip[0] - points[-1][0]) - svg.HEAD_RETREAT) < 0.01


def test_the_four_markers_are_one_anchor_and_one_size(view: graph.SequenceView) -> None:
    """The lit twins share their heads' anchor and size, so unlit and lit end alike."""
    defs = _root(sequence.render_sequence(view)).find(f"{NS}defs")
    shapes = {tuple(sorted((key, value) for key, value in marker.attrib.items() if key != "id")) for marker in defs}
    assert len(shapes) == 1
    (shape,) = shapes
    attributes = dict(shape)
    assert float(attributes["refX"]) == svg.HEAD_LENGTH - svg.HEAD_RETREAT
    assert float(attributes["markerWidth"]) == svg.HEAD_LENGTH
    assert float(attributes["markerHeight"]) == svg.HEAD_WIDTH


def test_the_sequence_views_four_markers_are_sized_in_canvas_pixels_and_follow_their_lines(
    view: graph.SequenceView,
) -> None:
    lines = sequence.render_sequence(view).splitlines()
    assert [line for line in lines if line.startswith("<defs>")] == [SEQUENCE_MARKERS]


def test_each_step_has_an_activation_bar_on_its_lifeline_covering_what_it_sends(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    centres = {element.get("data-name"): _centre(element) for element in _lifelines(root)}
    bars = list(_layer(root, "activations"))
    messages = _messages(root)
    activations = [element for element in messages if element.get("data-role") == "activation"]
    assert len(bars) == len(activations) == 2
    for bar, activation in zip(bars, activations, strict=True):
        target = activation.get("data-target")
        x, width = float(bar.get("x", "")), float(bar.get("width", ""))
        assert round(x + width / 2, 2) == centres[target]
        assert bar.get("data-lifeline") == target
        top, bottom = float(bar.get("y", "")), float(bar.get("y", "")) + float(bar.get("height", ""))
        arrow = _path_points(activation)
        assert arrow[0] == (centres["Task_10ms"], top)
        assert _tip(root, activation) == (round(x, 2), top)  # the head's tip is on the bar's near edge
        sent = messages[messages.index(activation) + 1]
        assert sent.get("data-source") == target
        assert all(top < y < bottom for _, y in _path_points(sent))
        assert _path_points(sent)[0][0] == round(x + width, 2)  # leaves from the bar's edge


def test_a_data_message_ends_on_its_target_lifeline(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    centres = {element.get("data-name"): _centre(element) for element in _lifelines(root)}
    data = _messages(root)[1]
    assert _tip(root, data)[0] == centres["Controller"]


def test_a_self_message_is_a_loop_back_onto_its_own_bar(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    loop = _messages(root)[3]
    assert loop.get("data-source") == loop.get("data-target") == "Controller"
    points = _path_points(loop)
    assert len(points) == 4
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    # The loop's line stops short of the bar; its head's tip lands back on the bar's edge.
    assert _tip(root, loop) == (x0, y3)
    assert x0 < x3 < x1 and x1 == x2
    assert y0 == y1 and y2 == y3 and y2 > y0
    bar = list(_layer(root, "activations"))[1]
    assert x0 == round(float(bar.get("x", "")) + float(bar.get("width", "")), 2)
    assert y3 < float(bar.get("y", "")) + float(bar.get("height", ""))


def test_every_message_label_fits_between_the_ends_of_its_arrow(view: graph.SequenceView) -> None:
    root = _root(sequence.render_sequence(view))
    labels = [element for element in _layer(root, "labels") if element.get("class") == "message-label"]
    messages = _messages(root)
    assert [label.text for label in labels] == [message.label for message in view.messages]
    for label, message in zip(labels, messages, strict=True):
        # The arrow's ends are its start and its head's tip, not where its line stops.
        points = [*_path_points(message)[:-1], _tip(root, message)]
        xs = [x for x, _ in points]
        width = svg.text_width(label.text or "", svg.EDGE_FONT_SIZE)
        assert float(label.get("y", "")) < points[0][1]
        if message.get("data-source") == message.get("data-target"):
            assert label.get("text-anchor") is None
            assert float(label.get("x", "")) >= max(xs)
        else:
            assert label.get("text-anchor") == "middle"
            assert float(label.get("x", "")) == round((xs[0] + xs[-1]) / 2, 2)
            assert abs(xs[-1] - xs[0]) >= width


def test_a_long_label_widens_every_gap_it_spans() -> None:
    label = "pAVeryLongProvidePortName -> rAnEvenLongerRequirePortName : IAnInterfaceWithALongName"
    view = _view(
        lifelines=("T", "A", "B", "C"),
        messages=(
            graph.SequenceMessage("activation", "T", "A", "run"),
            graph.SequenceMessage("data", "A", "C", label),
        ),
    )
    root = _root(sequence.render_sequence(view))
    centres = [_centre(element) for element in _lifelines(root)]
    width = svg.text_width(label, svg.EDGE_FONT_SIZE)
    assert centres[3] - centres[1] >= width
    assert round(centres[2] - centres[1], 2) == round(centres[3] - centres[2], 2)
    short = _root(sequence.render_sequence(_view(lifelines=("T", "A", "B", "C"))))
    short_centres = [_centre(element) for element in _lifelines(short)]
    assert short_centres[3] - short_centres[1] < width


def test_a_long_self_message_label_widens_the_gap_to_the_next_lifeline() -> None:
    label = "pALongSelfPortName -> rALongSelfPortName : ISelf"
    view = _view(
        messages=(
            graph.SequenceMessage("activation", "T", "A", "run"),
            graph.SequenceMessage("data", "A", "A", label),
        )
    )
    root = _root(sequence.render_sequence(view))
    centres = [_centre(element) for element in _lifelines(root)]
    text = next(element for element in _layer(root, "labels") if element.text == label)
    assert float(text.get("x", "")) + svg.text_width(label, svg.EDGE_FONT_SIZE) < centres[2]


def test_text_is_escaped_and_the_document_stays_well_formed() -> None:
    view = _view(title='a <b> & "c"', messages=(graph.SequenceMessage("activation", "T", "A", "run <1>"),))
    root = _root(sequence.render_sequence(view))
    assert _layer(root, "frame")[0][0].text == 'sequence s\na <b> & "c"\ntask T'
    assert _messages(root)[0].get("data-name") == "run <1>"


def test_a_data_message_lists_its_interfaces_lines_and_an_activation_none(view: graph.SequenceView) -> None:
    """A data message's hover names what its port's interface carries; an activation's names nothing."""
    contents = graph.build_graphs(str(FIXTURE_DIR / "model")).interface_contents
    root = _root(sequence.render_sequence(view, contents))
    assert [element[0].text for element in _messages(root)] == [
        "activation Task_10ms -> Sensor: SensorRun",
        "data Sensor -> Controller: pSpeed -> rSpeed : ISpeed\n· speed: uint8",
        "activation Task_10ms -> Controller: ControlRun — closes the loop",
        "data Controller -> Controller: pLoop -> rLoop : ISpeed\n· speed: uint8",
    ]


def test_a_capped_data_message_counts_the_rest_and_names_no_click() -> None:
    """A message has no list to open (only a port has), so its ninth line is the count alone."""
    lines = tuple(f"e{index}: uint8" for index in range(1, 12))
    view = _view(
        messages=(
            graph.SequenceMessage("activation", "T", "A", "run"),
            graph.SequenceMessage("data", "A", "B", "p -> r : I", "I"),
        )
    )
    activation, data = _messages(_root(sequence.render_sequence(view, {"I": lines})))
    assert data[0].text.split("\n") == [
        "data A -> B: p -> r : I",
        *(f"· e{index}: uint8" for index in range(1, 9)),
        "and 3 more",
    ]
    assert activation[0].text == "activation T -> A: run"


def test_a_message_without_a_map_or_an_interface_draws_the_maps_absence(view: graph.SequenceView) -> None:
    """``None`` is no lines: a caller passing nothing, an empty map or a map naming no drawn interface draws alike."""
    contents = graph.build_graphs(str(FIXTURE_DIR / "model")).interface_contents
    plain = sequence.render_sequence(view)
    assert sequence.render_sequence(view, {}) == plain
    assert sequence.render_sequence(view, {"INowhere": ("x: uint8",)}) == plain
    unnamed = _view()  # its data message carries no interface
    assert sequence.render_sequence(unnamed, {"I": ("x: uint8",)}) == sequence.render_sequence(unnamed)
    # An entry keyed ``""`` is never looked up: a message naming no interface lists nothing.
    assert sequence.render_sequence(unnamed, {"": ("never: drawn",)}) == sequence.render_sequence(unnamed)
    assert sequence.render_sequence(view, contents) != plain


def test_two_lifelines_with_one_label_are_two_endpoints() -> None:
    """``swc: A`` and ``instance: A`` resolving to two occurrences are two keys labelled ``A``.

    The renderer places each endpoint by its key.
    """
    first, second = ("component", "instance", "a"), ("component", "instance", "A")
    view = _view(
        lifelines=(("task", "T"), first, second),
        labels=("T", "A", "A"),
        messages=(
            graph.SequenceMessage("activation", ("task", "T"), first, "run"),
            graph.SequenceMessage("data", first, second, "p -> r : I"),
        ),
    )
    root = _root(sequence.render_sequence(view))
    lifelines = _lifelines(root)
    assert [element.get("data-name") for element in lifelines] == ["T", "A", "A"]
    # The page finds an end by its name, so the two lifelines labelled alike carry an id each
    # and the message's ends name the ids; the task's line, labelled alone, carries none.
    assert [element.get("id") for element in lifelines] == [None, "lifeline-1", "lifeline-2"]
    centres = [_centre(element) for element in lifelines]
    assert len(set(centres)) == 3
    activation, data = _messages(root)
    assert (activation.get("data-source"), activation.get("data-target")) == ("T", "lifeline-1")
    assert (data.get("data-source"), data.get("data-target")) == ("lifeline-1", "lifeline-2")
    assert [bar.get("data-lifeline") for bar in _layer(root, "activations")] == ["lifeline-1"]
    assert data[0].text == "data A -> A: p -> r : I"
    # An arrow between two lines, not a loop on one: it starts at the first bar and ends on the second line.
    points = _path_points(data)
    assert len(points) == 2
    assert points[0][0] == centres[1] + sequence.BAR_WIDTH / 2
    assert _tip(root, data)[0] == centres[2]


# The module's boundary


def test_the_renderer_reads_the_extractors_sequence_shape_field_for_field() -> None:
    for protocol, dataclass in (
        (sequence.SequenceViewLike, graph.SequenceView),
        (sequence.SequenceMessageLike, graph.SequenceMessage),
    ):
        assert set(get_protocol_members(protocol)) == {item.name for item in dataclasses.fields(dataclass)}


def test_the_renderer_imports_only_the_svg_primitives_of_swcraft() -> None:
    tree = ast.parse((PACKAGE_DIR / "sequence.py").read_text(encoding="utf-8"))
    imported = sorted(
        {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0}
        | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    )
    assert [name for name in imported if "swcraft" in name] == ["swcraft_tools.writer.diagrams.svg"]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
