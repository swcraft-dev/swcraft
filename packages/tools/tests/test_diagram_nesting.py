"""The nesting overview: ``writer/diagrams/nesting.py``, boxes in boxes packed in Python (no ELK, no Node).

Input is the nesting tree of ``fixtures/diagrams/model``; the golden ``fixtures/diagrams/svg/nesting.svg`` is written by
``python packages/tools/tests/test_diagram_svg.py --freeze-svg``, never by hand.

What these tests do not reach: whether the packing reads well on a real model. The golden is a change detector; the
label widths are estimates.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

import pytest
from conftest import REPO_ROOT
from swcraft_tools.writer.diagrams import graph, nesting, svg
from typing_extensions import get_protocol_members

FIXTURE_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams"
GOLDEN = FIXTURE_DIR / "svg" / "nesting.svg"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"

NS = "{http://www.w3.org/2000/svg}"

Box = tuple[float, float, float, float]


@pytest.fixture(scope="module")
def tree() -> graph.NestingNode:
    extracted = graph.build_graphs(str(FIXTURE_DIR / "model")).nesting
    assert extracted is not None
    return extracted


def _root(text: str) -> ET.Element:
    return ET.fromstring(text)


def _layer(root: ET.Element, name: str) -> ET.Element:
    return next(element for element in root if element.tag == f"{NS}g" and element.get("class") == name)


def _box(element: ET.Element) -> Box:
    rect = element.find(f"{NS}rect")
    x, y = float(rect.get("x", "")), float(rect.get("y", ""))
    return x, y, x + float(rect.get("width", "")), y + float(rect.get("height", ""))


def _inside(inner: Box, outer: Box) -> bool:
    return outer[0] < inner[0] and outer[1] < inner[1] and inner[2] < outer[2] and inner[3] < outer[3]


def _apart(left: Box, right: Box) -> bool:
    return left[2] < right[0] or right[2] < left[0] or left[3] < right[1] or right[3] < left[1]


def _preorder(node: graph.NestingNode) -> Iterator[tuple[graph.NestingNode, graph.NestingNode | None]]:
    def walk(
        current: graph.NestingNode, parent: graph.NestingNode | None
    ) -> Iterator[tuple[graph.NestingNode, graph.NestingNode | None]]:
        yield current, parent
        for child in current.children:
            yield from walk(child, current)

    return walk(node, None)


def _drawn(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter(f"{NS}g") if element.get("data-role")]


def _component(name: str, type_name: str | None = None) -> graph.NestingNode:
    return graph.NestingNode(name, type_name or name, "component", "", ())


# The document


def test_two_renders_are_byte_identical_and_equal_the_golden(tree: graph.NestingNode) -> None:
    first = nesting.render_nesting(tree)
    assert nesting.render_nesting(tree) == first
    assert first == GOLDEN.read_text(encoding="utf-8")
    assert first.endswith("</svg>\n")


def test_the_document_names_its_view_and_is_sized_by_its_viewbox(tree: graph.NestingNode) -> None:
    text = nesting.render_nesting(tree)
    root = _root(text)
    assert root.get("data-view") == "nesting"
    assert root.find(f"{NS}title").text == "Vehicle — nesting"
    assert root.get("viewBox") == f"0 0 {root.get('width')} {root.get('height')}"
    assert re.findall(r"\d+\.\d{3,}", text) == []
    outer = _box(_layer(root, "frame")[0])
    assert outer[2] < float(root.get("width", "")) and outer[3] < float(root.get("height", ""))


def test_the_nesting_is_drawn_frame_members_labels(tree: graph.NestingNode) -> None:
    root = _root(nesting.render_nesting(tree))
    assert [element.get("class") for element in root if element.tag == f"{NS}g"] == ["frame", "members", "labels"]


def test_every_drawn_element_carries_a_title_first_in_preorder(tree: graph.NestingNode) -> None:
    root = _root(nesting.render_nesting(tree))
    drawn = _drawn(root)
    assert [(element.get("data-name"), element.get("data-type")) for element in drawn] == [
        (node.name, node.type) for node, _ in _preorder(tree)
    ]
    for element in drawn:
        assert element[0].tag == f"{NS}title" and element[0].text
    assert [element[0].text for element in drawn] == [
        "composition Vehicle",
        "Chassis (composition)",
        "Sensor (component)",
        "Controller (component)",
        "Logger (component)",
        "Diag : DiagServer (component)",
    ]


# Boxes in boxes


def test_the_root_is_the_outermost_box_and_every_box_lies_inside_its_parent(tree: graph.NestingNode) -> None:
    root = _root(nesting.render_nesting(tree))
    drawn = _drawn(root)
    assert drawn[0].get("data-role") == "frame" and drawn[0] in list(_layer(root, "frame"))
    assert all(element.get("data-role") == "member" for element in drawn[1:])
    boxes = {id(node): _box(element) for (node, _), element in zip(_preorder(tree), drawn, strict=True)}
    for node, parent in _preorder(tree):
        if parent is not None:
            assert _inside(boxes[id(node)], boxes[id(parent)]), node.name
        for index, left in enumerate(node.children):
            for right in node.children[index + 1 :]:
                assert _apart(boxes[id(left)], boxes[id(right)]), (left.name, right.name)


def test_compositions_and_components_are_told_apart(tree: graph.NestingNode) -> None:
    root = _root(nesting.render_nesting(tree))
    members = {element.get("data-name"): element for element in _layer(root, "members")}
    chassis, diag = members["Chassis"], members["Diag"]
    assert (chassis.get("class"), chassis.get("data-kind")) == ("member composition", "composition")
    assert (diag.get("class"), diag.get("data-kind")) == ("member component", "component")
    assert chassis.find(f"{NS}rect").get("fill") == svg.COMPOSITION_FILL
    assert diag.find(f"{NS}rect").get("fill") == svg.COMPONENT_FILL
    assert _layer(root, "frame")[0].find(f"{NS}rect").get("fill") == svg.FRAME_FILL
    assert chassis.find(f"{NS}rect").get("stroke-dasharray") is None


def test_every_label_is_drawn_inside_its_box(tree: graph.NestingNode) -> None:
    root = _root(nesting.render_nesting(tree))
    labels = [element for element in _layer(root, "labels") if element.tag == f"{NS}text"]
    drawn = _drawn(root)
    assert [label.text for label in labels] == [
        "Vehicle",
        "Chassis",
        "Sensor",
        "Controller",
        "Logger",
        "Diag : DiagServer",
    ]
    for label, element in zip(labels, drawn, strict=True):
        left, top, right, bottom = _box(element)
        x, y = float(label.get("x", "")), float(label.get("y", ""))
        width = svg.text_width(label.text or "", svg.NODE_FONT_SIZE)
        if label.get("text-anchor") == "middle":
            assert left <= x - width / 2 and x + width / 2 <= right
        else:
            assert left < x and x + width <= right
        assert top < y < bottom


def test_the_root_and_a_composition_show_a_title_that_differs_from_the_type() -> None:
    chassis = graph.NestingNode("Body", "Chassis", "composition", "The chassis", (_component("Sensor"),))
    tree = graph.NestingNode("Vehicle", "Vehicle", "composition", "The vehicle", (chassis,))
    root = _root(nesting.render_nesting(tree))
    labels = [element.text for element in _layer(root, "labels") if element.tag == f"{NS}text"]
    assert labels[:2] == ["Vehicle — The vehicle", "Body : Chassis — The chassis"]
    drawn = _drawn(root)
    assert drawn[0][0].text == "composition Vehicle\nThe vehicle"
    assert drawn[1][0].text == "Body : Chassis (composition)\nThe chassis"


def test_a_wide_composition_wraps_its_members_into_rows() -> None:
    children = tuple(_component(f"AComponentWithAFairlyLongName{index:02d}") for index in range(12))
    tree = graph.NestingNode("Root", "Root", "composition", "", children)
    root = _root(nesting.render_nesting(tree))
    members = [_box(element) for element in _layer(root, "members")]
    assert len({top for _, top, _, _ in members}) > 1
    left, _, right, _ = _box(_layer(root, "frame")[0])
    assert right - left <= nesting.ROW_WIDTH + 2 * nesting.PADDING
    for index, box in enumerate(members):
        for other in members[index + 1 :]:
            assert _apart(box, other)
    tops = [top for _, top, _, _ in members]
    assert tops == sorted(tops)


def test_a_composition_without_members_is_drawn_as_an_empty_box() -> None:
    tree = graph.NestingNode(
        "Root", "Root", "composition", "", (graph.NestingNode("Again", "Root", "composition", "", ()), _component("A"))
    )
    root = _root(nesting.render_nesting(tree))
    members = list(_layer(root, "members"))
    assert [element.get("data-kind") for element in members] == ["composition", "component"]
    assert _inside(_box(members[0]), _box(_layer(root, "frame")[0]))
    assert _apart(_box(members[0]), _box(members[1]))


def test_text_is_escaped_and_the_document_stays_well_formed() -> None:
    tree = graph.NestingNode("R", "R", "composition", 'a <b> & "c"', (_component("A<1>"),))
    root = _root(nesting.render_nesting(tree))
    assert _drawn(root)[0][0].text == 'composition R\na <b> & "c"'
    assert _drawn(root)[1].get("data-name") == "A<1>"


# The module's boundary


def test_the_renderer_reads_the_extractors_nesting_shape_field_for_field() -> None:
    fields = {item.name for item in dataclasses.fields(graph.NestingNode)}
    assert set(get_protocol_members(nesting.NestingNodeLike)) == fields


def test_the_renderer_imports_only_the_svg_primitives_of_swcraft() -> None:
    tree = ast.parse((PACKAGE_DIR / "nesting.py").read_text(encoding="utf-8"))
    imported = sorted(
        {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0}
        | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    )
    assert [name for name in imported if "swcraft" in name] == ["swcraft_tools.writer.diagrams.svg"]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
