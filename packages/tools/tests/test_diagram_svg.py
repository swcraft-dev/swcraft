"""The SVG renderer: ``writer/diagrams/svg.py``, over recorded layouts.

The renderer never calls the bridge, so this module needs Node in one case only. Its inputs are the four graphs of
``fixtures/diagrams/model`` laid out once through the real bridge and recorded under ``fixtures/diagrams/laid-out/``;
its goldens are the SVG files under ``fixtures/diagrams/svg/``. Both are written by this module run as a script
(``python packages/tools/tests/test_diagram_svg.py --record-layouts`` needs Node; ``--freeze-svg`` does not), never by
hand. Two checks keep the recordings honest: without Node, every key and value the extractor builds is still in the
recorded graph (ELK only adds); with Node, laying the graphs out again answers the recorded bytes.

What these tests do not reach: whether the picture is right. The goldens are change detectors frozen by the renderer;
the correctness oracle is a reader's look at real tool exports.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.schema import CompositionMember
from swcraft_tools.writer.diagrams import deployment, graph, layout, nesting, page, sequence, svg, text_metrics

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
FIXTURE_MODEL = FIXTURE_DIR / "model"
LAID_OUT_DIR = FIXTURE_DIR / "laid-out"
SVG_DIR = FIXTURE_DIR / "svg"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"

#: The recorded views, in the extractor's view order.
VIEWS = ("ibd-Chassis", "overview-Chassis", "ibd-Vehicle", "overview-Vehicle")

#: The views drawn from Python grids rather than ELK layouts: no recording, only a golden,
#: tested by ``test_diagram_nesting.py``, ``test_diagram_deployment.py`` and ``test_diagram_sequence.py``.
GRID_VIEWS = ("nesting", "deployment", "sequence-speed_cycle")

NS = "{http://www.w3.org/2000/svg}"


def _laid_out(view: str) -> dict[str, Any]:
    return json.loads((LAID_OUT_DIR / f"{view}.json").read_text(encoding="utf-8"))


@functools.cache
def _fixture_contents() -> Mapping[str, tuple[str, ...]]:
    """The fixture model's interface member lines, as the generator hands them to the renderers."""
    return graph.build_graphs(str(FIXTURE_MODEL)).interface_contents


def _render(view: str, laid_out: dict[str, Any] | None = None) -> str:
    graph_document = _laid_out(view) if laid_out is None else laid_out
    if view.startswith("ibd-"):
        return svg.render_ibd(graph_document, _fixture_contents())
    return svg.render_overview(graph_document)


def _root(text: str) -> ET.Element:
    return ET.fromstring(text)


def _layers(root: ET.Element) -> list[str]:
    return [element.get("class", "") for element in root if element.tag == f"{NS}g"]


def _layer(root: ET.Element, name: str) -> ET.Element:
    return next(element for element in root if element.tag == f"{NS}g" and element.get("class") == name)


def _drawn(root: ET.Element, role: str, name: str, owner: str | None = None) -> ET.Element:
    matches = [
        element
        for element in root.iter(f"{NS}g")
        if element.get("data-role") == role
        and element.get("data-name") == name
        and (owner is None or element.get("data-owner") == owner)
    ]
    assert len(matches) == 1, (role, name, owner, len(matches))
    return matches[0]


def _numbers(text: str) -> list[float]:
    return [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", text)]


def _path_points(d: str) -> list[tuple[float, float]]:
    values = _numbers(d)
    return list(zip(values[0::2], values[1::2], strict=True))


def _frame(laid_out: dict[str, Any]) -> dict[str, Any]:
    return laid_out["children"][0]


def _absolute_port(laid_out: dict[str, Any], port_id: str) -> tuple[dict[str, Any], float, float, float, float]:
    """A port of the frame or of a member, with its owner's absolute origin and its own."""
    frame = _frame(laid_out)
    fx, fy = frame["x"], frame["y"]
    for port in frame.get("ports", []):
        if port["id"] == port_id:
            return port, fx, fy, fx + port["x"], fy + port["y"]
    for member in frame["children"]:
        mx, my = fx + member["x"], fy + member["y"]
        for port in member.get("ports", []):
            if port["id"] == port_id:
                return port, mx, my, mx + port["x"], my + port["y"]
    raise AssertionError(port_id)


def _tip(laid_out: dict[str, Any], port_id: str) -> tuple[float, float]:
    """The end the port's **edge** is anchored on: the glyph's tip, or a boundary square's inner face.

    A member's port anchors on its glyph's tip, outside its box; the frame's boundary port, whose glyph points outward,
    anchors on the inner face of its square, where the delegation drawn inside the frame arrives.
    """
    port, _, _, px, py = _absolute_port(laid_out, port_id)
    meta = port[graph.METADATA_KEY]
    west = meta["direction"] == "require"
    boundary = meta["owner"] is None
    at_west_end = west != boundary
    return (round(px if at_west_end else px + port["width"], 2), round(py + port["height"] / 2, 2))


def _glyph_tip(laid_out: dict[str, Any], port_id: str) -> tuple[float, float]:
    """The point the port's glyph points at: the far end of its box on the side it faces.

    Every port faces out of the box it belongs to, the frame's boundary ports included.
    """
    port, _, _, px, py = _absolute_port(laid_out, port_id)
    west = port[graph.METADATA_KEY]["direction"] == "require"
    return (round(px if west else px + port["width"], 2), round(py + port["height"] / 2, 2))


# The recordings


def _contains(recorded: Any, built: Any, path: str) -> None:
    if isinstance(built, dict):
        assert isinstance(recorded, dict), path
        for key, value in built.items():
            assert key in recorded, f"{path}.{key}"
            _contains(recorded[key], value, f"{path}.{key}")
    elif isinstance(built, list):
        assert isinstance(recorded, list) and len(recorded) == len(built), path
        for index, (left, right) in enumerate(zip(recorded, built, strict=True)):
            _contains(left, right, f"{path}[{index}]")
    else:
        assert recorded == built, path


def test_the_recorded_layouts_hold_everything_the_extractor_builds_today() -> None:
    built = graph.build_graphs(str(FIXTURE_MODEL)).elk_graphs()
    assert [path.stem for path in sorted(LAID_OUT_DIR.glob("*.json"), key=lambda p: p.as_posix())] == sorted(VIEWS)
    for view, graph_document in zip(VIEWS, built, strict=True):
        _contains(_laid_out(view), graph_document, view)


@pytest.fixture
def node() -> str:
    return requires_node()


def test_laying_the_extractors_graphs_out_again_answers_the_recorded_layouts(node: str) -> None:
    built = graph.build_graphs(str(FIXTURE_MODEL)).elk_graphs()
    assert layout.layout(built, node=node) == [_laid_out(view) for view in VIEWS]


def test_the_goldens_are_exactly_the_recorded_views_and_the_grid_views() -> None:
    goldens = [path.stem for path in sorted(SVG_DIR.glob("*.svg"), key=lambda p: p.as_posix())]
    assert goldens == sorted(VIEWS + GRID_VIEWS)
    assert list(graph.view_ids(graph.build_graphs(str(FIXTURE_MODEL)))) == [*VIEWS, *GRID_VIEWS]


# The document


@pytest.mark.parametrize("view", VIEWS)
def test_two_renders_are_byte_identical_and_equal_the_golden(view: str) -> None:
    first = _render(view)
    assert _render(view) == first
    assert first == (SVG_DIR / f"{view}.svg").read_text(encoding="utf-8")
    assert first.endswith("</svg>\n")


@pytest.mark.parametrize("view", VIEWS)
def test_the_svg_takes_the_size_of_the_laid_out_root(view: str) -> None:
    laid_out = _laid_out(view)
    root = _root(_render(view))
    assert root.tag == f"{NS}svg"
    assert float(root.get("width", "")) == laid_out["width"]
    assert float(root.get("height", "")) == laid_out["height"]
    assert root.get("viewBox") == f"0 0 {root.get('width')} {root.get('height')}"
    assert root.get("data-view") == view
    kind = "internal block diagram" if view.startswith("ibd-") else "overview"
    assert root.find(f"{NS}title").text == f"{view.split('-', 1)[1]} — {kind}"


@pytest.mark.parametrize("view", VIEWS)
def test_every_number_in_the_output_has_at_most_two_decimals(view: str) -> None:
    assert re.findall(r"\d+\.\d{3,}", _render(view)) == []


def test_a_sum_of_rounded_coordinates_is_rounded_again() -> None:
    assert svg.number(184.4 + 18) == "202.4"
    assert svg.number(0.1 + 0.2) == "0.3"
    assert svg.number(-0.001) == "0"
    assert svg.number(28) == "28"
    assert svg.number(12.5) == "12.5"


def _swcraft_imports(module: str) -> list[str]:
    tree = ast.parse((PACKAGE_DIR / module).read_text(encoding="utf-8"))
    imported = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.level > 0 or (node.module or "").startswith("swcraft"))
    ]
    imported += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if "swcraft" in alias.name
    ]
    return imported


def test_the_renderer_reads_laid_out_json_and_imports_nothing_of_swcraft_but_the_width_estimate() -> None:
    """The one estimate both sides share lives in a leaf the renderer may import; nothing else."""
    assert _swcraft_imports("svg.py") == ["swcraft_tools.writer.diagrams.text_metrics"]
    assert _swcraft_imports("text_metrics.py") == []


def test_the_renderer_draws_with_the_sizes_the_extractor_estimated() -> None:
    assert svg.METADATA_KEY == graph.METADATA_KEY
    assert (svg.NODE_FONT_SIZE, svg.PORT_FONT_SIZE, svg.EDGE_FONT_SIZE) == (
        graph.NODE_FONT_SIZE,
        graph.PORT_FONT_SIZE,
        graph.EDGE_FONT_SIZE,
    )
    assert svg.SQUARE == graph.PORT_SQUARE
    # The glyph's reach is the renderer's table; the extractor's copy sizes the port box.
    assert graph.PORT_REACH == {
        ("provide", "sender_receiver"): sum(svg.GLYPH_STEM_AND_SHAPE[("provide", False)]),
        ("require", "sender_receiver"): sum(svg.GLYPH_STEM_AND_SHAPE[("require", False)]),
        ("provide", "client_server"): sum(svg.GLYPH_STEM_AND_SHAPE[("provide", True)]),
        ("require", "client_server"): sum(svg.GLYPH_STEM_AND_SHAPE[("require", True)]),
    }
    # The kinds that draw a glyph are the declared kinds the extractor measures a reach for.
    assert svg.GLYPH_KINDS == set(graph.INTERFACE_WORDS) == {kind for _, kind in graph.PORT_REACH}


#: Texts of every character class the estimate knows, at every font size a view draws with.
SAMPLED_TEXTS = (
    "pSpeed",
    "Sensor : Controller",
    "SPEED_SENSOR_FRONT_LEFT_RAW_VALUE_STATE_FLAG",
    "rDiag_2",
    "sd speed_cycle — Speed cycle",
    "Ärger › Übersicht",
    "",
)


@pytest.mark.parametrize("size", [graph.NODE_FONT_SIZE, graph.PORT_FONT_SIZE, graph.EDGE_FONT_SIZE])
def test_the_extractor_and_the_renderers_estimate_a_width_by_one_function(size: int) -> None:
    """One shared width estimate, never two constants held equal by a test.

    ``svg.text_width`` *is* the leaf's function, and every label box the extractor hands ELK is its answer.
    """
    assert svg.text_width is text_metrics.text_width
    assert not hasattr(svg, "CHARACTER_WIDTH")
    assert not hasattr(graph, "_CHARACTER_WIDTH")
    for text in SAMPLED_TEXTS:
        assert svg.text_width(text, size) == graph._label(text, size)["width"]


def test_the_border_offsets_and_anchors_are_the_ones_the_recorded_layouts_were_computed_with() -> None:
    """Derived from the port size, so a derivation slip must not move a layout.

    The port box's width depends on the glyph, so both east anchors do; the one offset and the west anchor do not,
    because the square is centred on the border. A boundary port uses the member's offset; only the anchor is mirrored.
    """
    assert graph._BORDER_OFFSET == "-6"
    assert graph._WEST_END == "(0,7)"
    assert not hasattr(graph, "_MEMBER_BORDER_OFFSET"), "one offset, not a member's and a frame's"
    boundary = {port["id"]: port["layoutOptions"] for port in _frame(_laid_out("ibd-Chassis"))["ports"]}
    assert boundary["boundary.rDiag"]["elk.port.borderOffset"] == "-6"
    assert boundary["boundary.rDiag"]["elk.port.anchor"] == "(26,7)"
    assert boundary["boundary.pSpeedOut"]["elk.port.borderOffset"] == "-6"
    assert boundary["boundary.pSpeedOut"]["elk.port.anchor"] == "(0,7)"
    assert graph.ROOT_PADDING == 22
    assert graph.GLYPH_STROKE_WIDTH == svg.LINE_WIDTH
    assert graph._IBD_ROOT_OPTIONS["elk.padding"] == "[top=22,left=22,bottom=22,right=22]"
    assert "elk.padding" not in graph._OVERVIEW_ROOT_OPTIONS, "an overview's frame draws no glyph"


@pytest.mark.parametrize(("name", "type_name"), [("Sensor", "Sensor"), ("front", "Sensor"), ("sensor", "Sensor")])
def test_a_member_is_labelled_by_one_rule_in_the_extractor_and_the_renderer(name: str, type_name: str) -> None:
    member = CompositionMember(name=name, type=type_name)
    assert graph._member_label(member) == svg.member_label(name, type_name)


# The draw order


def test_the_ibd_is_drawn_frame_edges_members_ports_labels() -> None:
    root = _root(_render("ibd-Chassis"))
    assert _layers(root) == ["frame", "edges", "members", "ports", "labels"]


def test_the_overview_is_drawn_frame_edges_members_labels() -> None:
    root = _root(_render("overview-Chassis"))
    assert _layers(root) == ["frame", "edges", "members", "labels"]


def test_each_port_draws_its_box_before_its_glyph() -> None:
    """The title, the square, the glyph, and last the hit box the whole port takes the pointer with."""
    root = _root(_render("ibd-Chassis"))
    ports = list(_layer(root, "ports"))
    assert len(ports) == 2 + 2 + 6 + 1  # two boundary ports, Sensor's, Controller's, Logger's
    for port in ports:
        children = list(port)
        assert [child.tag for child in children] == [f"{NS}title", f"{NS}rect", f"{NS}g", f"{NS}rect"]
        assert children[1].get("class") == "port-box"
        assert children[2].get("class") == "glyph"
        assert children[3].get("class") == svg.PORT_HIT_CLASS == "port-hit"


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_every_port_is_rested_on_whole_through_one_hit_box_equal_to_its_laid_out_box(view: str) -> None:
    """The whole port area is the hit target: one ``rect.port-hit`` per port, last, covering its laid-out box.

    It is drawn at ``opacity`` 0 with the **default** fill: ``fill="none"`` would take no pointer inside it. Whether a
    browser hit-tests it so is not measured here; the markup is.
    """
    laid_out = _laid_out(view)
    root = _root(_render(view))
    ports = [element for element in root.iter(f"{NS}g") if element.get("data-role") == "port"]
    assert ports
    for element in ports:
        hits = [child for child in element if child.get("class") == svg.PORT_HIT_CLASS]
        assert len(hits) == 1 and hits[0] is element[-1], element.get("data-name")
        hit = hits[0]
        assert hit.tag == f"{NS}rect"
        assert hit.get("opacity") == "0"
        assert hit.get("fill") is None, "the default fill takes the pointer; fill=none would not"
        owner = element.get("data-owner") or ""
        port_id = f"port.{owner}.{element.get('data-name')}" if owner else f"boundary.{element.get('data-name')}"
        port, _, _, px, py = _absolute_port(laid_out, port_id)
        cy = py + port["height"] / 2
        assert (float(hit.get("x", "")), float(hit.get("y", ""))) == (round(px, 2), round(cy - svg.SQUARE / 2, 2))
        assert (float(hit.get("width", "")), float(hit.get("height", ""))) == (port["width"], svg.SQUARE)


def test_every_drawn_element_carries_a_title_first() -> None:
    for view in VIEWS:
        laid_out = _laid_out(view)
        frame = _frame(laid_out)
        expected = 1 + len(frame["children"]) + len(frame["edges"]) + len(frame.get("ports", []))
        expected += sum(len(member.get("ports", [])) for member in frame["children"])
        drawn = [element for element in _root(_render(view)).iter(f"{NS}g") if element.get("data-role")]
        assert len(drawn) == expected, view
        for element in drawn:
            first = element[0]
            assert first.tag == f"{NS}title" and first.text, (view, element.get("data-name"))


def test_the_titles_carry_the_models_names() -> None:
    root = _root(_render("ibd-Chassis"))
    assert _drawn(root, "frame", "Chassis")[0].text == (
        "composition Chassis\nThree members, two boundary ports and a delegation in each direction."
    )
    assert _drawn(root, "member", "Sensor")[0].text == (
        "Sensor (component)\nMeasures the speed and provides it twice, filtered and raw."
    )
    assert _drawn(root, "port", "pSpeed", owner="Sensor")[0].text == (
        "Sensor.pSpeed: provide ISpeed (sender-receiver)\n· speed: uint8\nThe filtered speed."
    )
    assert _drawn(root, "port", "rDiag", owner="")[0].text == (
        "Chassis.rDiag: require IDiag (client-server)\n· ReadStatus(out status: uint8)"
    )
    assert _drawn(root, "connector", "raw_link")[0].text == "connector raw_link: Sensor.pRaw -> Controller.rRaw"
    assert _drawn(root, "delegation", "Chassis_rDiag")[0].text == (
        "delegation Chassis_rDiag: Chassis.rDiag -> Controller.rDiag"
    )
    vehicle = _root(_render("ibd-Vehicle"))
    assert _drawn(vehicle, "member", "Diag")[0].text == "Diag : DiagServer (component)\nServes the diagnostic status."
    assert _drawn(vehicle, "member", "Chassis")[0].text == "Chassis (composition)\nThe nested composition."
    assert _drawn(vehicle, "member", "Chassis").get("data-kind") == "composition"
    assert _drawn(vehicle, "member", "Diag").get("data-type") == "DiagServer"


# A port's hover lists its interface's content


def _members(count: int) -> tuple[str, ...]:
    return tuple(f"e{index}: uint8" for index in range(1, count + 1))


@pytest.mark.parametrize("click_line", [True, False], ids=["port", "data-message"])
def test_the_interface_lines_are_prefixed_and_up_to_the_cap_alone(click_line: bool) -> None:
    assert svg.interface_lines((), click_line=click_line) == []
    assert svg.interface_lines(("speed: uint8",), click_line=click_line) == ["· speed: uint8"]
    eight = svg.interface_lines(_members(8), click_line=click_line)
    assert eight == [f"· e{index}: uint8" for index in range(1, 9)]
    assert svg.INTERFACE_LINES_CAP == 8


def test_a_ninth_line_counts_the_rest_and_names_the_click_on_a_port_alone() -> None:
    nine = svg.interface_lines(_members(9), click_line=True)
    assert nine[:8] == [f"· e{index}: uint8" for index in range(1, 9)]
    assert nine[8:] == ["and 1 more — click the port to list all 9"]
    assert svg.interface_lines(_members(9), click_line=False)[8:] == ["and 1 more"]
    many = svg.interface_lines(_members(124), click_line=True)
    assert len(many) == 9
    assert many[-1] == "and 116 more — click the port to list all 124"


def test_a_ports_title_is_its_head_then_its_interfaces_lines_then_its_description() -> None:
    contents = {"ISpeed": _members(9), "IDiag": ("ReadStatus(out status: uint8)", "Reset()")}
    root = _root(svg.render_ibd(_laid_out("ibd-Chassis"), contents))
    assert _drawn(root, "port", "pSpeed", owner="Sensor")[0].text.split("\n") == [
        "Sensor.pSpeed: provide ISpeed (sender-receiver)",
        *(f"· e{index}: uint8" for index in range(1, 9)),
        "and 1 more — click the port to list all 9",
        "The filtered speed.",
    ]
    # A boundary port takes the same lines (one function draws both).
    assert _drawn(root, "port", "rDiag", owner="")[0].text == (
        "Chassis.rDiag: require IDiag (client-server)\n· ReadStatus(out status: uint8)\n· Reset()"
    )


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_every_port_of_the_fixture_model_lists_its_interfaces_lines_under_its_head(view: str) -> None:
    """Each port's title, second line on, starts with its own interface's lines and with no other."""
    contents = _fixture_contents()
    root = _root(svg.render_ibd(_laid_out(view), contents))
    ports = [element for element in root.iter(f"{NS}g") if element.get("data-role") == "port"]
    assert ports
    for port in ports:
        lines = (port[0].text or "").split("\n")
        members = [f"{svg.MEMBER_LINE_PREFIX}{line}" for line in contents.get(port.get("data-interface") or "", ())]
        assert lines[1 : 1 + len(members)] == members, port.get("data-name")
        assert not any(line.startswith(svg.MEMBER_LINE_PREFIX) for line in lines[1 + len(members) :])


def test_a_capped_port_carries_every_line_in_a_member_list_right_after_its_title() -> None:
    lines = (*_members(10), "odd & <name>: uint8")
    contents = {"ISpeed": lines, "IDiag": _members(8)}
    text = svg.render_ibd(_laid_out("ibd-Chassis"), contents)
    root = _root(text)
    port = _drawn(root, "port", "pSpeedOut", owner="")
    assert [child.tag for child in port[:2]] == [f"{NS}title", f"{NS}desc"], "the title stays first"
    assert port[1].get("class") == svg.MEMBER_LIST_CLASS == "member-list"
    assert port[1].text == "\n".join(lines), "every line, unprefixed, in the model's order"
    assert "odd &amp; &lt;name&gt;: uint8" in text, "escaped as every title is"
    # Every ISpeed port carries it; a port of exactly the cap carries none.
    ispeed = [element for element in root.iter(f"{NS}g") if element.get("data-interface") == "ISpeed"]
    assert ispeed and all(element.find(f"{NS}desc") is not None for element in ispeed)
    assert _drawn(root, "port", "rDiag", owner="").find(f"{NS}desc") is None
    assert text.count(f'class="{svg.MEMBER_LIST_CLASS}"') == len(ispeed)


def test_no_port_within_the_cap_carries_a_member_list() -> None:
    assert svg.member_list(_members(8)) == "" and svg.member_list(()) == ""
    text = svg.render_ibd(_laid_out("ibd-Chassis"), {"ISpeed": _members(8), "IDiag": _members(1)})
    assert "<desc" not in text


def test_a_port_with_no_or_an_unresolved_interface_lists_no_line() -> None:
    laid_out = _laid_out("ibd-Chassis")
    ports = _frame(laid_out)["children"][0]["ports"]
    ports[0][graph.METADATA_KEY]["interface"] = "INowhere"
    ports[0][graph.METADATA_KEY]["interfaceKind"] = None
    ports[0][graph.METADATA_KEY]["interfaceWord"] = None
    ports[1][graph.METADATA_KEY]["interface"] = None
    contents = {"ISpeed": ("speed: uint8",), "": ("never: drawn",)}
    root = _root(svg.render_ibd(laid_out, contents))
    for port in ports[:2]:
        meta = port[graph.METADATA_KEY]
        text = _drawn(root, "port", meta["name"], owner=meta["owner"])[0].text
        assert "·" not in text, text


def test_no_map_an_empty_map_and_a_map_naming_no_drawn_interface_draw_alike() -> None:
    """``None`` is no lines, so a caller passing nothing draws the titles without member lines."""
    for view in ("ibd-Chassis", "ibd-Vehicle"):
        laid_out = _laid_out(view)
        plain = svg.render_ibd(laid_out)
        assert svg.render_ibd(laid_out, {}) == plain
        assert svg.render_ibd(laid_out, {"INowhere": _members(12)}) == plain
        assert svg.render_ibd(laid_out, {"ISpeed": _members(2)}) != plain


def test_the_labels_are_the_laid_out_labels_drawn_last() -> None:
    root = _root(_render("ibd-Vehicle"))
    texts = [element.text for element in _layer(root, "labels") if element.tag == f"{NS}text"]
    assert texts == ["Vehicle", "Chassis", "rDiag", "pSpeedOut", "Diag : DiagServer", "pDiag"]


def _port_labels(view: str) -> list[tuple[str, float, str]]:
    """Every port label drawn, as (text, x, text-anchor), in document order."""
    root = _root(_render(view))
    return [
        (element.text or "", float(element.get("x", "")), element.get("text-anchor", ""))
        for element in _layer(root, "labels")
        if element.tag == f"{NS}text" and "port-label" in (element.get("class") or "").split()
    ]


def _every_port(laid_out: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    """Every port of the frame and of its members, with its absolute x, in document order."""
    frame = _frame(laid_out)
    fx = frame["x"]
    ports = [(port, fx + port["x"]) for port in frame.get("ports", [])]
    for member in frame["children"]:
        ports += [(port, fx + member["x"] + port["x"]) for port in member.get("ports", [])]
    return ports


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_a_members_port_label_is_marked_in_member_and_a_frames_is_not(view: str) -> None:
    """A member's port label lies inside the member's box and says so with the class ``in-member``.

    The labels are drawn in a layer of their own, so the tab's stylesheet can colour one as its box's text only by a
    mark the renderer writes.
    """
    laid_out = _laid_out(view)
    frame = _frame(laid_out)
    in_member = sorted(port["labels"][0]["text"] for member in frame["children"] for port in member.get("ports", []))
    on_frame = sorted(port["labels"][0]["text"] for port in frame.get("ports", []))
    root = _root(_render(view, laid_out))
    classes = [
        (element.text or "", element.get("class"))
        for element in _layer(root, "labels").iter(f"{NS}text")
        if "port-label" in (element.get("class") or "").split()
    ]
    assert sorted(text for text, css in classes if css == f"port-label {svg.IN_MEMBER_CLASS}") == in_member
    assert sorted(text for text, css in classes if css == "port-label") == on_frame
    assert in_member


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_a_members_port_label_carries_its_members_kind(view: str) -> None:
    """An in-member port label carries its member's ``data-kind``; a frame's port label carries none.

    ``ibd-Vehicle`` holds a composition member and a component member, so both kinds are read.
    """
    laid_out = _laid_out(view)
    frame = _frame(laid_out)
    expected = sorted(
        (port["labels"][0]["text"], str(member[svg.METADATA_KEY]["kind"]))
        for member in frame["children"]
        for port in member.get("ports", [])
    )
    root = _root(_render(view, laid_out))
    labels = [
        element
        for element in _layer(root, "labels").iter(f"{NS}text")
        if "port-label" in (element.get("class") or "").split()
    ]
    marked = sorted(
        (element.text or "", element.get("data-kind") or "")
        for element in labels
        if svg.IN_MEMBER_CLASS in (element.get("class") or "").split()
    )
    assert marked == expected
    assert all(
        element.get("data-kind") is None
        for element in labels
        if svg.IN_MEMBER_CLASS not in (element.get("class") or "").split()
    )
    if view == "ibd-Vehicle":
        assert {kind for _, kind in marked} == {"composition", "component"}


def test_a_member_without_a_kind_gives_its_labels_no_empty_kind() -> None:
    """A member whose metadata names no kind writes no ``data-kind`` at all.

    Neither on its own label nor on its ports' labels, which keep the in-member class.
    """
    laid_out = _laid_out("ibd-Vehicle")
    for member in _frame(laid_out)["children"]:
        member[svg.METADATA_KEY].pop("kind", None)
    root = _root(_render("ibd-Vehicle", laid_out))
    labels = list(_layer(root, "labels").iter(f"{NS}text"))
    in_member = [element for element in labels if svg.IN_MEMBER_CLASS in (element.get("class") or "").split()]
    members = [element for element in labels if element.get("class") == "member-label"]
    assert in_member, "the view drew no member's port label; this case would assert nothing"
    assert members, "the view drew no member label; this case would assert nothing"
    assert all(element.get("data-kind") is None for element in in_member + members)


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle", "overview-Chassis", "overview-Vehicle"])
def test_every_member_and_frame_label_carries_its_elements_kind(view: str) -> None:
    """A box's label names the kind of the box it labels (``data-kind``); a block diagram's frame is a composition.

    Only the VS Code tab's stylesheet reads the mark; the file's own colours do not change.
    """
    laid_out = _laid_out(view)
    frame = _frame(laid_out)
    kinds = sorted(str(member[svg.METADATA_KEY]["kind"]) for member in frame["children"])
    root = _root(_render(view, laid_out))
    labels = list(_layer(root, "labels").iter(f"{NS}text"))
    members = [element for element in labels if element.get("class") == "member-label"]
    frames = [element for element in labels if element.get("class") == "frame-label"]
    assert sorted(element.get("data-kind") or "" for element in members) == kinds
    assert [element.get("data-kind") for element in frames] == ["composition"]


def test_a_nesting_label_carries_the_kind_of_the_box_it_heads() -> None:
    """A container's heading and a leaf's label are told apart by kind."""
    root = _root((REPO_ROOT / "packages/tools/tests/fixtures/diagrams/svg/nesting.svg").read_text(encoding="utf-8"))
    labels = list(_layer(root, "labels").iter(f"{NS}text"))
    boxes = [element for element in root.iter(f"{NS}g") if element.get("data-role") in {"frame", "member"}]
    assert len(labels) == len(boxes)
    assert all(element.get("data-kind") in {"composition", "component"} for element in labels)
    assert [element.get("data-kind") for element in labels if element.get("text-anchor") == "middle"] == [
        "component"
    ] * sum(1 for element in boxes if element.get("data-kind") == "component")


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_a_port_label_is_anchored_at_its_ports_side_and_never_reaches_the_glyph(view: str) -> None:
    """An east port's label ends at the port's inner edge, a west port's starts there.

    The label lies inside the owner, so an overrun runs **into** the box, never over the square and the glyph.
    """
    laid_out = _laid_out(view)
    drawn = sorted(_port_labels(view))
    expected: list[tuple[str, float, str]] = []
    for port, px in _every_port(laid_out):
        label = port["labels"][0]
        box_start, box_end = px + label["x"], px + label["x"] + label["width"]
        if port[svg.METADATA_KEY]["direction"] == "provide":
            assert box_end <= px, port["id"]
            expected.append((label["text"], float(svg.number(box_end - 2)), "end"))
        else:
            assert box_start >= px + port["width"], port["id"]
            expected.append((label["text"], float(svg.number(box_start + 2)), "start"))
    assert drawn == sorted(expected)


# The glyphs

#: (view, owner, port, direction, interface kind) -- the four glyph shapes of the fixture.
GLYPHS = (
    ("ibd-Chassis", "Sensor", "pSpeed", "provide", "sender_receiver"),
    ("ibd-Chassis", "Controller", "rSpeed", "require", "sender_receiver"),
    ("ibd-Vehicle", "Diag", "pDiag", "provide", "client_server"),
    ("ibd-Chassis", "Controller", "rDiag", "require", "client_server"),
)

#: How the sketch seats each glyph between the square's outer edge and the tip: the stem, the shape's width and its
#: height (the 12 px square's). Every glyph has a stem, so the four reaches (stem + shape) differ, and the port box is
#: as wide as half the square and its own.
GLYPH_BOXES = {
    ("provide", "sender_receiver"): (4, 16, 12),
    ("require", "sender_receiver"): (10, 8, 12),
    ("provide", "client_server"): (8, 12, 12),
    ("require", "client_server"): (8, 6, 12),
}


def _glyph_parts(element: ET.Element) -> tuple[ET.Element, ET.Element]:
    """A port's stem and its one shape."""
    glyph = element[2]
    children = list(glyph)
    assert len(children) == 2, [child.tag for child in children]
    assert children[0].tag == f"{NS}line"
    return children[0], children[1]


def _shape_x_span(shape: ET.Element, outward: int) -> tuple[float, float, float]:
    """The shape's (near x, far x, height): near is the end towards the square."""
    if shape.tag == f"{NS}circle":
        cx, r = float(shape.get("cx", "")), float(shape.get("r", ""))
        return round(cx - outward * r, 2), round(cx + outward * r, 2), 2 * r
    d = shape.get("d", "")
    if " A " in d:
        # M tip,top A r r 0 0 sweep tip,bottom: the mouth on the tip, the bulge r towards the box.
        values = _numbers(d)
        radius = values[2]
        assert values[0] == values[-2]
        return round(values[0] - outward * radius, 2), values[0], round(values[-1] - values[1], 2)
    points = _path_points(d.replace("Z", ""))
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    near = min(xs) if outward > 0 else max(xs)
    far = max(xs) if outward > 0 else min(xs)
    return near, far, round(max(ys) - min(ys), 2)


def _all_ports(view: str) -> list[tuple[str, str, str, str, str]]:
    """(port id, owner as drawn, name, direction, kind) for every port of a recorded IBD."""
    frame = _frame(_laid_out(view))
    ports = [(port, "") for port in frame.get("ports", [])]
    ports += [(port, member["id"].split(".", 1)[1]) for member in frame["children"] for port in member.get("ports", [])]
    rows = []
    for port, owner in ports:
        meta = port[graph.METADATA_KEY]
        kind = meta["interfaceKind"] or "sender_receiver"
        rows.append((port["id"], owner, meta["name"], meta["direction"], kind))
    return rows


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_every_glyph_is_seated_as_the_sketch_seats_it(view: str) -> None:
    """A stem of the sketch's own length under every shape, from the square's outer edge to the tip.

    The sender's arrow is never glued to the square with no stem.
    """
    laid_out = _laid_out(view)
    root = _root(_render(view))
    rows = _all_ports(view)
    assert rows
    for port_id, owner, name, direction, kind in rows:
        port, _, _, _, py = _absolute_port(laid_out, port_id)
        tip_x, tip_y = _glyph_tip(laid_out, port_id)
        element = _drawn(root, "port", name, owner=owner)
        square_left = float(element[1].get("x", ""))
        # The glyph points away from the square, towards the tip.
        outward = 1 if tip_x > square_left else -1
        square_edge = round(square_left + (12 if outward > 0 else 0), 2)
        stem, shape = _glyph_parts(element)
        near, far, height = _shape_x_span(shape, outward)
        stem_width, width, expected_height = GLYPH_BOXES[(direction, kind)]
        assert far == tip_x, (view, port_id)
        assert round(abs(far - near), 2) == width, (view, port_id)
        assert height == expected_height, (view, port_id)
        assert float(stem.get("x1", "")) == square_edge, (view, port_id)
        assert float(stem.get("x2", "")) == near, (view, port_id)
        assert round(abs(near - square_edge), 2) == stem_width, (view, port_id)
        assert float(stem.get("y1", "")) == float(stem.get("y2", "")) == tip_y, (view, port_id)
        assert tip_y == round(py + port["height"] / 2, 2)


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_a_ports_laid_out_box_is_the_square_and_the_glyph_it_carries(view: str) -> None:
    """Every port's laid-out box is exactly half its square plus its own glyph's stem and shape.

    The tip the edge ends on is the glyph's far end, with no part of the box left over: the agreement the per-glyph box
    widths rest on.
    """
    laid_out = _laid_out(view)
    rows = _all_ports(view)
    assert rows
    for port_id, _, _, direction, kind in rows:
        port, _, _, _, _ = _absolute_port(laid_out, port_id)
        stem_width, shape_width, _ = GLYPH_BOXES[(direction, kind)]
        assert port["width"] == svg.SQUARE + stem_width + shape_width, (view, port_id)
        assert port["height"] == graph.PORT_HEIGHT, (view, port_id)


@pytest.mark.parametrize(("view", "owner", "name", "direction", "kind"), GLYPHS)
def test_each_port_kind_draws_the_vfb_shape(view: str, owner: str, name: str, direction: str, kind: str) -> None:
    laid_out = _laid_out(view)
    port, mx, _, _, py = _absolute_port(laid_out, f"port.{owner}.{name}")
    member = next(child for child in _frame(laid_out)["children"] if child["id"] == f"member.{owner}")
    border = mx if direction == "require" else mx + member["width"]
    tip_x, tip_y = _glyph_tip(laid_out, f"port.{owner}.{name}")
    outward = -1 if direction == "require" else 1
    # The border to the tip: the square's outer half and then the glyph the sketch seats.
    reach = svg.SQUARE / 2 + sum(GLYPH_BOXES[(direction, kind)][:2])
    assert tip_x == round(border + outward * reach, 2)

    element = _drawn(_root(_render(view)), "port", name, owner=owner)
    assert element.get("class") == f"port {direction} {kind.replace('_', '-')}"
    box = element[1]
    assert (float(box.get("width", "")), float(box.get("height", ""))) == (12, 12)
    assert round(float(box.get("x", "")) + 6, 2) == round(border, 2)
    assert round(float(box.get("y", "")) + 6, 2) == round(py + port["height"] / 2, 2)
    _, shape = _glyph_parts(element)
    d = shape.get("d", "")
    if (direction, kind) == ("provide", "sender_receiver"):
        # A pentagon arrow, its point on the tip: back, shoulder, point, shoulder, back.
        assert shape.tag == f"{NS}path"
        assert d.rstrip().endswith("Z")
        back, shoulder = round(tip_x - outward * 16, 2), round(tip_x - outward * 6, 2)
        assert _path_points(d.replace("Z", "")) == [
            (back, round(tip_y - 6, 2)),
            (shoulder, round(tip_y - 6, 2)),
            (tip_x, tip_y),
            (shoulder, round(tip_y + 6, 2)),
            (back, round(tip_y + 6, 2)),
        ]
    elif (direction, kind) == ("require", "sender_receiver"):
        # A fork of two prongs from one root, opening on the tip.
        assert shape.tag == f"{NS}path"
        assert "A" not in d
        assert "Z" not in d
        root_x = round(tip_x - outward * 8, 2)
        assert _path_points(d) == [
            (root_x, tip_y),
            (tip_x, round(tip_y - 6, 2)),
            (root_x, tip_y),
            (tip_x, round(tip_y + 6, 2)),
        ]
    elif (direction, kind) == ("provide", "client_server"):
        assert shape.tag == f"{NS}circle"
        assert float(shape.get("r", "")) == 6
        assert round(float(shape.get("cx", "")) + outward * 6, 2) == tip_x
        assert float(shape.get("cy", "")) == tip_y
    else:
        # A half circle whose mouth lies on the tip.
        assert shape.tag == f"{NS}path"
        points = _numbers(d)
        assert (points[0], points[1]) == (tip_x, round(tip_y - 6, 2))
        assert points[2:4] == [6, 6]
        assert (points[-2], points[-1]) == (tip_x, round(tip_y + 6, 2))


def test_the_client_side_half_circle_carries_no_dot() -> None:
    glyph = _drawn(_root(_render("ibd-Chassis")), "port", "rDiag", owner="Controller")[2]
    assert glyph.findall(f"{NS}circle") == []
    assert [child.tag for child in glyph] == [f"{NS}line", f"{NS}path"]  # the stem and the half circle


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_no_glyph_is_filled_to_tell_a_kind_apart(view: str) -> None:
    root = _root(_render(view))
    fills = {
        shape.get("fill")
        for glyph in root.iter(f"{NS}g")
        if glyph.get("class") == "glyph"
        for shape in glyph
        if shape.tag != f"{NS}line"
    }
    assert fills
    assert fills <= {"white", "none"}, fills


def test_a_half_circle_bulges_towards_its_box() -> None:
    root = _root(_render("ibd-Chassis"))
    west = _drawn(root, "port", "rDiag", owner="Controller")[2][-1].get("d", "")
    boundary = _drawn(root, "port", "rDiag", owner="")[2][-1].get("d", "")
    # A west half circle opens west, so the arc runs through its east side (sweep 1); the frame's own west port draws
    # the same one: it faces the environment, not the frame.
    assert " 0 0 1 " in west
    assert " 0 0 1 " in boundary


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_every_edge_ends_on_the_anchors_of_its_two_ports(view: str) -> None:
    laid_out = _laid_out(view)
    root = _root(_render(view))
    edges = _frame(laid_out)["edges"]
    assert edges
    for edge in edges:
        meta = edge[graph.METADATA_KEY]
        points = _path_points(_drawn(root, meta["role"], meta["name"]).find(f"{NS}path").get("d", ""))
        assert points[0] == _tip(laid_out, edge["sources"][0]), edge["id"]
        assert points[-1] == _tip(laid_out, edge["targets"][0]), edge["id"]


def test_the_compositions_own_ports_point_out_of_the_frame() -> None:
    """A composition's port faces the environment: its glyph points out as a member's does.

    The square stays astride the border, the stem starts on its outer edge, and no glyph lies inside the frame.
    """
    laid_out = _laid_out("ibd-Chassis")
    frame = _frame(laid_out)
    root = _root(_render("ibd-Chassis"))
    left, right = frame["x"], frame["x"] + frame["width"]
    for name, border, outward, key in (
        ("rDiag", left, -1, ("require", "client_server")),
        ("pSpeedOut", right, 1, ("provide", "sender_receiver")),
    ):
        element = _drawn(root, "port", name, owner="")
        assert element.get("data-owner") == ""
        box = element[1]
        assert round(float(box.get("x", "")) + 6, 2) == round(border, 2)
        tip_x, _ = _glyph_tip(laid_out, f"boundary.{name}")
        assert tip_x == round(border + outward * (svg.SQUARE / 2 + sum(GLYPH_BOXES[key][:2])), 2)
        # Outside the frame's own rectangle.
        assert (tip_x < left) if outward < 0 else (tip_x > right), name
        stem, shape = _glyph_parts(element)
        _, far, _ = _shape_x_span(shape, outward)
        assert far == tip_x
        assert float(stem.get("x1", "")) == round(border + outward * 6, 2)
        # Nothing of the glyph lies inside the frame: the square's inner half is the port box.
        xs = [float(stem.get("x1", "")), float(stem.get("x2", "")), *_shape_x_span(shape, outward)[:2]]
        assert all((x <= round(border + outward * 6, 2)) if outward < 0 else (x >= round(border - 6, 2)) for x in xs)
    # The boundary sender's arrow points out of the frame: its point is its eastmost x.
    arrow = _drawn(root, "port", "pSpeedOut", owner="")[2][-1]
    eastmost = max(x for x, _ in _path_points(arrow.get("d", "").replace("Z", "")))
    assert eastmost == _glyph_tip(laid_out, "boundary.pSpeedOut")[0]


def test_a_delegation_arrives_on_the_inner_face_of_its_boundary_square() -> None:
    """A delegation runs inside the frame and stops on the inner face of its boundary port's square.

    Both directions: from the boundary port on a require one, to it on a provide one.
    """
    laid_out = _laid_out("ibd-Chassis")
    frame = _frame(laid_out)
    root = _root(_render("ibd-Chassis"))
    left, right = frame["x"], frame["x"] + frame["width"]
    delegations = [edge for edge in frame["edges"] if edge[graph.METADATA_KEY]["role"] == "delegation"]
    assert [edge["id"] for edge in delegations] == ["delegation.Chassis_rDiag", "delegation.Chassis_pSpeedOut"]
    for edge in delegations:
        meta = edge[graph.METADATA_KEY]
        points = _path_points(_drawn(root, "delegation", meta["name"]).find(f"{NS}path").get("d", ""))
        boundary_id = next(end for end in (edge["sources"][0], edge["targets"][0]) if end.startswith("boundary."))
        at_boundary = points[0] if edge["sources"][0] == boundary_id else points[-1]
        port, _, _, px, _ = _absolute_port(laid_out, boundary_id)
        border = left if port[graph.METADATA_KEY]["direction"] == "require" else right
        inner_face = round(border + (6 if border == left else -6), 2)
        assert at_boundary[0] == inner_face, meta["name"]
        assert at_boundary == _tip(laid_out, boundary_id), "the anchor is that face"
        assert at_boundary[0] != _glyph_tip(laid_out, boundary_id)[0], "and never the glyph's tip"
        assert round(px + port["width"], 2) == inner_face if border == left else round(px, 2) == inner_face


def test_no_glyph_of_the_outermost_frame_is_drawn_outside_the_canvas() -> None:
    """Every boundary glyph of the wrapping root lies within the ``viewBox``.

    The root reserves the reach its outward glyphs stand in: ELK's default padding (12) is less than every reach;
    :data:`graph.ROOT_PADDING` is the widest reach plus the glyph stroke.

    What this does not reach: a **label**, drawn from an estimated width, which can still run past its box; and the
    three views drawn from Python grids, which have no ELK root.
    """
    laid_out, view = _laid_out("ibd-Chassis"), "ibd-Chassis"
    root = _root(_render(view, laid_out))
    width, height = float(root.get("width", "")), float(root.get("height", ""))
    assert root.get("viewBox") == f"0 0 {svg.number(width)} {svg.number(height)}"
    frame = _frame(laid_out)
    assert frame["x"] >= graph.ROOT_PADDING
    assert width - (frame["x"] + frame["width"]) >= graph.ROOT_PADDING
    boundary = [port for port in _layer(root, "ports") if port.get("data-owner") == ""]
    assert boundary
    for port in boundary:
        box = port[1]
        outward = -1 if "require" in str(port.get("class", "")) else 1
        stem, shape = _glyph_parts(port)
        near, far, _ = _shape_x_span(shape, outward)
        xs = [
            float(box.get("x", "")),
            float(box.get("x", "")) + float(box.get("width", "")),
            float(stem.get("x1", "")),
            float(stem.get("x2", "")),
            near,
            far,
        ]
        assert far == _glyph_tip(laid_out, f"boundary.{port.get('data-name')}")[0]
        assert min(xs) - svg.LINE_WIDTH / 2 >= 0, (view, port.get("data-name"), min(xs))
        assert max(xs) + svg.LINE_WIDTH / 2 <= width, (view, port.get("data-name"), max(xs))


def test_a_port_of_an_undeclared_interface_is_the_bare_square_and_says_so() -> None:
    """A glyph only for a declared type: no kind, no glyph."""
    laid_out = _laid_out("ibd-Chassis")
    port = _frame(laid_out)["children"][0]["ports"][0]
    port[graph.METADATA_KEY]["interfaceKind"] = None
    port[graph.METADATA_KEY]["interfaceWord"] = None
    port[graph.METADATA_KEY]["interface"] = "INowhere"
    element = _drawn(_root(_render("ibd-Chassis", laid_out)), "port", "pSpeed", owner="Sensor")
    assert element.get("class") == "port provide unknown-interface"
    assert element[0].text.startswith("Sensor.pSpeed: provide INowhere (undeclared interface)")
    assert [child for child in element if child.tag == f"{NS}g" and child.get("class") == "glyph"] == []
    assert element[1].get("class") == "port-box"
    # The bare square is still rested on whole: its hit box is the square, last.
    assert element[-1].get("class") == svg.PORT_HIT_CLASS


# Edges and the overview


def test_the_self_connector_is_drawn_as_elk_routed_it_and_marked_a_loop() -> None:
    laid_out = _laid_out("ibd-Chassis")
    edge = next(item for item in _frame(laid_out)["edges"] if item["id"] == "connector.control_loop")
    element = _drawn(_root(_render("ibd-Chassis")), "connector", "control_loop")
    assert element.get("class") == "edge connector loop"
    assert element[0].text == "connector control_loop: Controller.pLoop -> Controller.rLoop (self-connector)"
    section = edge["sections"][0]
    expected = [section["startPoint"], *section["bendPoints"], section["endPoint"]]
    frame = _frame(laid_out)
    assert _path_points(element.find(f"{NS}path").get("d", "")) == [
        (round(frame["x"] + point["x"], 2), round(frame["y"] + point["y"], 2)) for point in expected
    ]


def test_the_overview_labels_each_bundle_with_its_count_on_a_background() -> None:
    root = _root(_render("overview-Chassis"))
    bundle = _drawn(root, "bundle", "Sensor -> Controller")
    # The head line alone; the names sit in the square's <desc>.
    assert bundle[0].text == "2 connectors Sensor -> Controller"
    assert bundle.find(f"{NS}path").get("marker-end") == f"url(#{svg.ARROW_MARKER_ID})"
    count = next(item for item in _layer(root, "labels") if item.get("data-name") == "Sensor -> Controller")
    assert [child.tag for child in count] == [f"{NS}title", f"{NS}desc", f"{NS}rect", f"{NS}text"]
    assert count[1].get("class") == svg.CONNECTOR_LIST_CLASS
    # A whole row per connector, the description after a tab.
    assert count[1].text == (
        "raw_link: Sensor.pRaw -> Controller.rRaw — ISpeed\n"
        "speed_link: Sensor.pSpeed -> Controller.rSpeed — ISpeed"
        "\tThe filtered speed into the controller."
    )
    assert count[2].get("class") == "edge-label" and count[3].text == "2"
    assert _drawn(root, "bundle", "Controller -> Controller")[0].text == "1 connector Controller -> Controller"
    assert [marker.get("id") for marker in root.find(f"{NS}defs")] == [
        svg.ARROW_MARKER_ID,
        svg.LIT_ARROW_MARKER_ID,
    ]


@pytest.mark.parametrize("view", ["overview-Chassis", "overview-Vehicle"])
def test_a_bundles_count_square_is_a_group_carrying_its_edges_title(view: str) -> None:
    """A bundle's count square is one group whose first child is the edge's own title.

    The title is the head line, the names beside it. Chrome truncates a native tooltip of many names and can neither
    wrap nor scroll it, so the names sit in a ``<desc>`` the page lists in a panel, carried once in the document.
    """
    root = _root(_render(view))
    bundles = [element for element in _layer(root, "edges") if element.get("data-role") == "bundle"]
    counts = [element for element in _layer(root, "labels") if element.get("class") == "edge-count"]
    assert bundles
    assert [count.get("data-name") for count in counts] == [bundle.get("data-name") for bundle in bundles]
    for bundle, count in zip(bundles, counts, strict=True):
        assert count.tag == f"{NS}g"
        assert count[0].tag == f"{NS}title" and count[0].text == bundle[0].text
        assert ":" not in count[0].text, "the title is the head line, never the list"
        assert [child.tag for child in count[1:]] == [f"{NS}desc", f"{NS}rect", f"{NS}text"]
        listing = count[1]
        assert listing.get("class") == svg.CONNECTOR_LIST_CLASS
        names = (listing.text or "").split("\n")
        assert names and all(names), (view, count.get("data-name"))
        assert count[0].text.startswith(f"{len(names)} connector"), "the head counts what the list holds"
        assert count.get("data-role") is None, "a count square is not a drawn element of its own"
    assert _render(view).count(svg.CONNECTOR_LIST_CLASS) == len(counts), "the list is carried once per bundle"


def _rows(root: ET.Element) -> list[str]:
    """Every row of every count square's connector list, in document order."""
    rows = []
    for count in _layer(root, "labels"):
        if count.get("class") != "edge-count":
            continue
        listing = count[1]
        assert listing.get("class") == svg.CONNECTOR_LIST_CLASS
        rows.extend((listing.text or "").split("\n"))
    return rows


@pytest.mark.parametrize("stem", ["Chassis", "Vehicle"])
def test_every_row_of_a_bundles_list_is_that_connectors_own_ibd_line_and_its_interface(stem: str) -> None:
    """A row is that connector's own IBD line, ``— <interface>`` after it and the description after a tab.

    Both texts go through the one :func:`svg.link_line`; this asserts them equal row by row, so they cannot drift.
    """
    ibd = _root(_render(f"ibd-{stem}"))
    titles = {
        str(element.get("data-name")): (element[0].text or "")
        for element in _layer(ibd, "edges")
        if element.get("data-role") == "connector"
    }
    interfaces = {
        f"{port.get('data-owner')}.{port.get('data-name')}": str(port.get("data-interface") or "")
        for port in _layer(ibd, "ports")
    }
    rows = _rows(_root(_render(f"overview-{stem}")))
    assert rows, stem
    for row in rows:
        line, tab, note = row.partition("\t")
        head, _, interface = line.rpartition(" — ")
        head, interface = (head, interface) if head else (line, "")
        name = head.split(":", 1)[0]
        title = titles[name].split("\n")
        assert title[0] == f"connector {head}", "the row is the IBD line's own words"
        assert note == " ".join(" ".join(title[1:]).split()), "the description is the title's second line"
        assert tab == ("\t" if note else ""), "a row with no description carries no separator"
        source = head.split(": ", 1)[1].split(" -> ")[0]
        assert interface == interfaces[source], "the interface is the source end's own"
    assert len(rows) == len(set(rows)), "a bundle lists each connector once"


#: The width a hit area's stroke may take: wide enough to rest a pointer on, narrow enough not to swallow the
#: neighbouring line.
HIT_AREA_WIDTHS = range(8, 11)


@pytest.mark.parametrize("view", VIEWS)
def test_every_edge_path_has_an_id_and_its_hit_area_is_a_use_of_it(view: str) -> None:
    """A line can be rested on through a ``<use>`` of its own path, never a copy.

    A ``<use>`` costs a few bytes an edge; a second path would repeat the whole path data. The hit area draws nothing
    (``opacity`` 0, or the clone would repeat its arrowhead), and its stroke width reaches the clone only because the
    path carries none. The width is one rule of the document's own ``<style>``, never an attribute on each ``<use>``:
    per ``<use>`` it spent the size budget.
    """
    text = _render(view)
    root = _root(text)
    styles = root.findall(f"{NS}style")
    assert len(styles) == 1, view
    # The hit area undoes the dash an unverified line's group would hand its ``<use>``.
    rule = re.fullmatch(r"g\.edges use\{stroke-width:(\d+)px;stroke-dasharray:none\}", styles[0].text or "")
    assert rule is not None and int(rule.group(1)) in HIT_AREA_WIDTHS, styles[0].text
    ids = [element.get("id") for element in root.iter() if element.get("id") is not None]
    assert len(ids) == len(set(ids)), view
    edges = [element for element in _layer(root, "edges") if element.get("data-role")]
    assert edges
    for edge in edges:
        assert edge.get("stroke-width") == "1", edge.get("data-name")
        paths = edge.findall(f"{NS}path")
        uses = edge.findall(f"{NS}use")
        assert paths and len(uses) == len(paths), edge.get("data-name")
        for path, use in zip(paths, uses, strict=True):
            assert path.get("id"), edge.get("data-name")
            assert path.get("stroke-width") is None, edge.get("data-name")
            assert use.get("href") == f"#{path.get('id')}"
            assert use.get("class") is None, "a hit area is known by its tag: no class, fewer bytes an edge"
            assert use.get("stroke-width") is None, "the width is the document's one style rule"
            assert use.get("opacity") == "0"
            assert use.get("d") is None
    every_d = [element.get("d") for element in root.iter() if element.get("d") is not None]
    drawn_d = [path.get("d") for edge in edges for path in edge.findall(f"{NS}path")]
    assert all(every_d.count(d) == 1 for d in drawn_d), "no second element repeats a line's path data"


def _with_edge_meta(view: str, name: str, **meta: Any) -> dict[str, Any]:
    laid_out = _laid_out(view)
    edge = next(item for item in _frame(laid_out)["edges"] if item[graph.METADATA_KEY]["name"] == name)
    edge[graph.METADATA_KEY].update(meta)
    return laid_out


def test_an_unverified_line_is_dashed_on_its_group_and_says_why() -> None:
    """*Dashed = not verified*, by the validator's own reason.

    The dash is :data:`svg.DASH` on the **edge group**, the hit area's rule undoes it for the ``<use>``, and the reason
    is the title's second line, verbatim. Every other line stays solid.
    """
    reason = "not judged: external component 'Ext' (port 'pExt')"
    laid_out = _with_edge_meta("ibd-Chassis", "raw_link", unverified=reason, description="The raw speed.")
    root = _root(_render("ibd-Chassis", laid_out))
    edge = _drawn(root, "connector", "raw_link")
    assert edge.get("stroke-dasharray") == svg.DASH == "5 4"
    assert edge.get("stroke-width") == "1"
    for child in edge:
        assert child.get("stroke-dasharray") is None, child.tag
    assert edge[0].text == f"connector raw_link: Sensor.pRaw -> Controller.rRaw\n{reason}\nThe raw speed."
    others = [
        element
        for element in _layer(root, "edges")
        if element.get("data-role") and element.get("data-name") != "raw_link"
    ]
    assert others
    assert all(element.get("stroke-dasharray") is None for element in others)
    assert 'stroke-dasharray="' not in _render("ibd-Chassis"), "a view with no unverified line draws no dash"


def test_an_unverified_delegation_is_dashed_too() -> None:
    reason = "not judged: external interface 'IForeign'"
    laid_out = _with_edge_meta("ibd-Chassis", "Chassis_rDiag", unverified=reason)
    edge = _drawn(_root(_render("ibd-Chassis", laid_out)), "delegation", "Chassis_rDiag")
    assert edge.get("stroke-dasharray") == svg.DASH
    assert edge[0].text == f"delegation Chassis_rDiag: Chassis.rDiag -> Controller.rDiag\n{reason}"


def test_the_diagrams_draw_one_dash() -> None:
    """The sequence view's dash is the renderer's, imported, never a second constant."""
    assert sequence.DASH is svg.DASH
    assert "DASH = " not in (PACKAGE_DIR / "sequence.py").read_text(encoding="utf-8")


def test_a_view_without_a_line_carries_no_style() -> None:
    """The hit areas' rule is emitted only where a hit area is: the document stays as it was."""
    text = svg.document(10, 10, "v", "v", [("edges", [])])
    assert "<style" not in text


def _pair(title: str) -> tuple[str, str]:
    """The two ends an edge's title names, ``A.p -> B.r`` (or ``A -> B`` for a bundle)."""
    head = title.splitlines()[0]
    ends = head.split(": ", 1)[1] if not head[0].isdigit() else head.split(" ", 2)[2].split(": ", 1)[0]
    source, target = ends.removesuffix(" (self-connector)").split(" -> ")
    return source, target


@pytest.mark.parametrize("view", ["ibd-Chassis", "ibd-Vehicle"])
def test_every_ibd_edge_names_the_ids_of_its_two_ports(view: str) -> None:
    """An IBD edge's ``data-source`` and ``data-target`` are the ids of exactly the two ports its title names.

    The page lights them with the line.
    """
    root = _root(_render(view))
    frame_name = _drawn(root, "frame", view.removeprefix("ibd-")).get("data-name")
    ports = {element.get("id"): element for element in _layer(root, "ports") if element.get("data-role") == "port"}
    assert ports and None not in ports, "every port carries an id"
    edges = [element for element in _layer(root, "edges") if element.get("data-role")]
    assert edges
    for edge in edges:
        source, target = edge.get("data-source"), edge.get("data-target")
        assert source in ports and target in ports, edge.get("data-name")
        shown = tuple(
            f"{ports[end].get('data-owner') or frame_name}.{ports[end].get('data-name')}" for end in (source, target)
        )
        assert shown == _pair(edge[0].text or ""), edge.get("data-name")


@pytest.mark.parametrize("view", ["overview-Chassis", "overview-Vehicle"])
def test_every_overview_bundle_names_the_ids_of_its_two_boxes(view: str) -> None:
    """On an overview a pair's line knows its two member boxes."""
    root = _root(_render(view))
    members = {element.get("id"): element for element in _layer(root, "members") if element.get("data-role")}
    assert members and None not in members, "every member box carries an id"
    bundles = [element for element in _layer(root, "edges") if element.get("data-role") == "bundle"]
    assert bundles
    for bundle in bundles:
        source, target = bundle.get("data-source"), bundle.get("data-target")
        assert source in members and target in members, bundle.get("data-name")
        shown = (members[source].get("data-name"), members[target].get("data-name"))
        assert shown == _pair(bundle[0].text or "") == tuple(str(bundle.get("data-name")).split(" -> "))


def test_the_ids_an_edge_names_are_short_ordinals_whatever_the_names_spell() -> None:
    """An end's id is its role and its ordinal, never a model name.

    The role is one letter (``e`` a line, ``p`` a port, ``m`` a member box): the ids are spelled many times in a large
    view, and the long spellings spent the size budget.
    """
    for view in VIEWS:
        root = _root(_render(view))
        ids = [
            element.get("id")
            for element in root.iter()
            if element.get("id") is not None and element.tag != f"{NS}marker"
        ]
        assert ids and all(re.fullmatch(r"[epm][1-9][0-9]*", str(value)) for value in ids), view


def _member_box(laid_out: dict[str, Any], name: str) -> tuple[float, float, float, float]:
    frame = _frame(laid_out)
    member = next(child for child in frame["children"] if child["id"] == f"member.{name}")
    return frame["x"] + member["x"], frame["y"] + member["y"], member["width"], member["height"]


@pytest.mark.parametrize("view", ["overview-Chassis", "overview-Vehicle"])
def test_an_overview_line_is_orthogonal_and_enters_its_box_perpendicular(view: str) -> None:
    """An overview is routed ``ORTHOGONAL``, so the last segment enters its box perpendicular.

    ``orient="auto-start-reverse"`` is then square-on by construction; a spline's end met the box skewed.
    """
    laid_out = _laid_out(view)
    root = _root(_render(view))
    bundles = [element for element in _layer(root, "edges") if element.get("data-role") == "bundle"]
    assert bundles
    targets = {
        edge["id"]: _member_box(laid_out, str(edge["targets"][0]).removeprefix("member."))
        for edge in _frame(laid_out)["edges"]
    }
    assert len(targets) == len(bundles)
    for bundle, box in zip(bundles, targets.values(), strict=True):
        d = bundle.find(f"{NS}path").get("d", "")
        assert "C" not in d and re.fullmatch(r"M [-\d.]+ [-\d.]+( L [-\d.]+ [-\d.]+)+", d), (view, d)
        points = _path_points(d)
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
            assert x1 == x2 or y1 == y2, (view, d)
        # The line stops svg.HEAD_RETREAT short of the box; its head's tip is on the border.
        points = [*points[:-1], _head_tip(points)]
        before_x, (end_x, end_y) = points[-2][0], points[-1]
        left, top, width, height = box
        if before_x == end_x:
            assert end_y in (top, top + height), (view, d)
        else:
            assert end_x in (left, left + width), (view, d)
        assert bundle.find(f"{NS}path").get("marker-end") == f"url(#{svg.ARROW_MARKER_ID})"


def test_an_overview_arrowhead_follows_its_line_and_does_not_grow_with_the_stroke() -> None:
    """The head is sized in canvas pixels, never in stroke widths.

    ``markerUnits`` defaults to ``strokeWidth``, which grows the head with a highlighted line. Both markers (the grey
    one and the blue one a lit line points with) declare ``userSpaceOnUse``.
    """
    for view in ("overview-Chassis", "overview-Vehicle"):
        root = _root(_render(view))
        markers = list(root.find(f"{NS}defs"))
        assert [marker.get("id") for marker in markers] == [svg.ARROW_MARKER_ID, svg.LIT_ARROW_MARKER_ID]
        for marker in markers:
            assert marker.get("markerUnits") == "userSpaceOnUse", view
            assert marker.get("orient") == "auto-start-reverse", view
            assert (marker.get("markerWidth"), marker.get("markerHeight")) == ("8", "8")
        assert markers[0][0].get("fill") == svg.EDGE_STROKE
        assert markers[1][0].get("fill") == svg.HIGHLIGHT_STROKE
        # The two heads are one geometry: only the colour differs.
        assert markers[0][0].get("d") == markers[1][0].get("d")


def _head_tip(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Where a pointing line's head puts its tip: the line's end, carried svg.HEAD_RETREAT ahead."""
    (x0, y0), (x1, y1) = points[-2:]
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    return (
        round(x1 + (x1 - x0) / length * svg.HEAD_RETREAT, 2),
        round(y1 + (y1 - y0) / length * svg.HEAD_RETREAT, 2),
    )


def _page_widths() -> set[float]:
    """Every stroke width the page's style sets, but the hit band's.

    The hit band is a transparent stroke that points with no head and draws nothing (``page.HIT_BAND_CLASS``,
    the pointer's target alone).
    """
    style = re.sub(rf"svg \.{page.HIT_BAND_CLASS} \{{[^}}]*\}}", "", page.PAGE_STYLE)
    assert style != page.PAGE_STYLE, "the band's rule is where this reader expects it"
    return {float(width) for width in re.findall(r"stroke-width:\s*(\d+(?:\.\d+)?)px", style)}


def test_no_stroke_a_pointing_line_can_take_is_wider_than_the_head_was_sized_for() -> None:
    """A guard on ``svg.HEAD_RETREAT``'s input: ``svg.WIDEST_POINTING_STROKE`` is the widest line.

    A wider stroke would show the line's end past the head again, so it fails here first.
    """
    widths = {*_page_widths(), svg.CHANGE_STROKE_WIDTH, svg.LINE_WIDTH, 1}
    assert max(widths) == svg.WIDEST_POINTING_STROKE
    assert svg.HEAD_RETREAT == svg.HEAD_LENGTH * svg.WIDEST_POINTING_STROKE / svg.HEAD_WIDTH == 4


@pytest.mark.parametrize("view", ["overview-Chassis", "overview-Vehicle"])
def test_an_overview_line_ends_inside_its_head_at_every_width_lit_or_not(view: str) -> None:
    """A bundle's line never shows past its head, lit or not.

    Its last segment stops ``svg.HEAD_RETREAT`` short of the box, where the head is as wide as the widest stroke; both
    markers are anchored there. The tip on the border is
    ``test_an_overview_line_is_orthogonal_and_enters_its_box_perpendicular``'s.
    """
    root = _root(_render(view))
    markers = list(root.find(f"{NS}defs"))
    anchors = {(marker.get("refX"), marker.get("viewBox"), marker.get("markerWidth")) for marker in markers}
    assert anchors == {(svg.number(svg.HEAD_LENGTH - svg.HEAD_RETREAT), "0 0 8 8", "8")}
    half_per_length = svg.HEAD_WIDTH / 2 / svg.HEAD_LENGTH
    for width in {1, svg.CHANGE_STROKE_WIDTH, *_page_widths()}:
        assert width / 2 <= svg.HEAD_RETREAT * half_per_length, width
    # Every pointing line stops short: none is left whole by end_at_head's short-segment edge,
    # which test_an_overview_line_is_orthogonal_and_enters_its_box_perpendicular proves by the
    # tip it carries back landing on the target box's border.
    bundles = [element for element in _layer(root, "edges") if element.get("data-role") == "bundle"]
    assert bundles and all(bundle.find(f"{NS}path[@marker-end]") is not None for bundle in bundles)


def test_end_at_head_carries_the_end_back_along_the_last_segment_and_keeps_the_tip() -> None:
    points = [(0.0, 0.0), (0.0, 30.0), (50.0, 30.0)]
    ended = svg.end_at_head(points)
    assert ended[:-1] == points[:-1]
    assert ended[-1] == (50.0 - svg.HEAD_RETREAT, 30.0)
    assert _head_tip(ended) == (50.0, 30.0)
    leftward = svg.end_at_head([(30.0, 5.0), (10.0, 5.0)])
    assert leftward == [(30.0, 5.0), (10.0 + svg.HEAD_RETREAT, 5.0)]


def test_end_at_head_leaves_a_last_segment_no_longer_than_the_retreat_whole() -> None:
    """The edge the docstring names: shortening it would turn the head around, so it is left whole."""
    short = [(0.0, 0.0), (20.0, 0.0), (20.0, svg.HEAD_RETREAT)]
    assert svg.end_at_head(short) == short


def test_the_ibd_draws_no_arrowheads() -> None:
    for view in ("ibd-Chassis", "ibd-Vehicle"):
        text = _render(view)
        assert "marker" not in text
        assert _root(text).find(f"{NS}defs") is None


def test_text_is_escaped_and_the_document_stays_well_formed() -> None:
    laid_out = _laid_out("ibd-Chassis")
    frame = _frame(laid_out)
    frame[graph.METADATA_KEY]["description"] = 'a <b> & "c"'
    frame["labels"][0]["text"] = "<Chassis & co>"
    frame[graph.METADATA_KEY]["parents"] = [["<P & co>", 'm "1"']]
    text = _render("ibd-Chassis", laid_out)
    root = _root(text)
    assert _drawn(root, "frame", "Chassis")[0].text == 'composition Chassis\na <b> & "c"'
    assert _frame_label(root).text == "\u2191 <Chassis & co>"
    assert _drawn(root, "frame", "Chassis").get("data-parents") == "%3CP%20%26%20co%3E#m%20%221%22"
    assert _up(root)[0].text == "up to <P & co>"


def test_a_frame_whose_title_differs_from_its_name_shows_both() -> None:
    laid_out = _laid_out("ibd-Chassis")
    _frame(laid_out)[graph.METADATA_KEY]["title"] = "The chassis domain"
    root = _root(_render("ibd-Chassis", laid_out))
    assert _drawn(root, "frame", "Chassis")[0].text.startswith("composition Chassis\nThe chassis domain\n")
    assert _frame_label(root).text == "\u2191 Chassis — The chassis domain"


# The frame's name is the way up


def _frame_label(root: ET.Element) -> ET.Element:
    labels = [element for element in _layer(root, "labels").iter(f"{NS}text") if element.get("class") == "frame-label"]
    assert len(labels) == 1
    return labels[0]


def _up(root: ET.Element) -> ET.Element:
    groups = [element for element in _layer(root, "labels") if element.get("class") == svg.FRAME_UP_CLASS]
    assert len(groups) == 1
    return groups[0]


@pytest.mark.parametrize("view", ["ibd-Chassis", "overview-Chassis"])
def test_a_composition_placed_in_one_parent_names_it_and_its_frame_name_leads_up(view: str) -> None:
    root = _root(_render(view))
    assert _drawn(root, "frame", "Chassis").get("data-parents") == "Vehicle#Chassis"
    up = _up(root)
    assert [child.tag for child in up] == [f"{NS}title", f"{NS}text"]
    assert up[0].text == "up to Vehicle"
    assert up[1] is _frame_label(root)
    assert up[1].text == "\u2191 Chassis"


@pytest.mark.parametrize("view", ["ibd-Vehicle", "overview-Vehicle"])
def test_the_root_carries_no_parents_no_arrow_and_nothing_to_press(view: str) -> None:
    text = _render(view)
    root = _root(text)
    assert _drawn(root, "frame", "Vehicle").get("data-parents") is None
    assert _frame_label(root).text == "Vehicle"
    assert svg.FRAME_UP_CLASS not in text and "\u2191" not in text


def test_a_composition_placed_nowhere_draws_as_the_root_does() -> None:
    laid_out = _laid_out("ibd-Chassis")
    _frame(laid_out)[graph.METADATA_KEY]["parents"] = []
    text = _render("ibd-Chassis", laid_out)
    assert "data-parents" not in text and svg.FRAME_UP_CLASS not in text
    assert _frame_label(_root(text)).text == "Chassis"


def test_a_composition_placed_in_several_parents_lists_them_and_leads_to_none() -> None:
    laid_out = _laid_out("ibd-Chassis")
    _frame(laid_out)[graph.METADATA_KEY]["parents"] = [["Other", "Second"], ["Vehicle", "Chassis"]]
    root = _root(_render("ibd-Chassis", laid_out))
    assert _drawn(root, "frame", "Chassis").get("data-parents") == "Other#Second Vehicle#Chassis"
    assert _up(root)[0].text == "placed in: Other, Vehicle"


def test_the_way_up_moves_no_coordinate() -> None:
    """The frame label keeps its laid-out point; only its text and its wrapping group are new."""
    placed = _frame_label(_root(_render("ibd-Chassis")))
    laid_out = _laid_out("ibd-Chassis")
    _frame(laid_out)[graph.METADATA_KEY]["parents"] = []
    unplaced = _frame_label(_root(_render("ibd-Chassis", laid_out)))
    assert placed.attrib == unplaced.attrib


# Recording and freezing -- run as a script, never by the gate


def _record_layouts() -> None:
    built = graph.build_graphs(str(FIXTURE_MODEL)).elk_graphs()
    LAID_OUT_DIR.mkdir(parents=True, exist_ok=True)
    for view, laid_out in zip(VIEWS, layout.layout(built), strict=True):
        text = json.dumps(laid_out, indent=1, ensure_ascii=False) + "\n"
        (LAID_OUT_DIR / f"{view}.json").write_text(text, encoding="utf-8", newline="\n")


def _freeze_svg() -> None:
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    for view in VIEWS:
        (SVG_DIR / f"{view}.svg").write_text(_render(view), encoding="utf-8", newline="\n")
    extracted = graph.build_graphs(str(FIXTURE_MODEL))
    assert extracted.nesting is not None and extracted.deployment is not None
    grids = {
        "nesting": nesting.render_nesting(extracted.nesting),
        "deployment": deployment.render_deployment(extracted.deployment),
    }
    contents = extracted.interface_contents
    grids.update({f"sequence-{view.name}": sequence.render_sequence(view, contents) for view in extracted.sequences})
    assert tuple(grids) == GRID_VIEWS
    for view, text in grids.items():
        (SVG_DIR / f"{view}.svg").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if "--record-layouts" in sys.argv:
        _record_layouts()
    if "--freeze-svg" in sys.argv:
        _freeze_svg()
