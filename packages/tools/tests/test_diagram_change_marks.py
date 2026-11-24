"""The change report's overviews: the marks ``svg.py`` draws and the pictures ``change.py`` builds.

The renderer half reads each ``change`` mark of a hand-built laid-out overview back with ``lxml`` (no Node); that an
element without the key renders with no change mark is proven by the goldens of ``test_diagram_svg.py`` and
``test_diagram_switch_byte_identity.py``. The change half edits a copy of ``fixtures/diagrams/model`` and checks the
marks of the merged after picture against expectations worked out by hand from the edit.

What these tests do not reach: whether the picture is right to a reader, and a mark ELK would place differently on
another layout.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, model_element_path, write_model_element
from lxml import etree
from node_support import requires_node
from swcraft_tools.writer.diagrams import change, graph, svg
from swcraft_tools.writer.diagrams.layout import DiagramLayoutError

FIXTURE_MODEL = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams" / "model"

NS = {"s": "http://www.w3.org/2000/svg"}

GREEN, RED, BLUE = "#1a7f37", "#cf222e", "#0969da"
CHANGE_MARKERS = ("swcraft-arrow-added", "swcraft-arrow-removed", "swcraft-arrow-both")


def _parse(text: str) -> etree._Element:
    return etree.fromstring(text.encode("utf-8"))


def _group(root: etree._Element, role: str, name: str) -> etree._Element:
    found = root.xpath(f"//s:g[@data-role='{role}'][@data-name=$name]", namespaces=NS, name=name)
    assert len(found) == 1, (role, name, len(found))
    return found[0]


def _member_outline(root: etree._Element, name: str) -> tuple[str, str, str | None]:
    rect = _group(root, "member", name).find("s:rect", NS)
    return rect.get("stroke"), rect.get("stroke-width"), rect.get("stroke-dasharray")


def _bundle(root: etree._Element, name: str) -> tuple[str, str | None, str, str]:
    """A bundle's group stroke width and dash, its last path's stroke and its arrowhead."""
    group = _group(root, "bundle", name)
    path = group.findall("s:path", NS)[-1]
    return group.get("stroke-width"), group.get("stroke-dasharray"), path.get("stroke"), path.get("marker-end")


def _marker_ids(root: etree._Element) -> list[str]:
    return [marker.get("id") for marker in root.findall("s:defs/s:marker", NS)]


def _connector_rows(root: etree._Element, name: str) -> tuple[list[str], str]:
    """The ``<desc>`` rows' connector names and the count label of the bundle *name*."""
    found = root.xpath("//s:g[@class='edge-count'][@data-name=$name]", namespaces=NS, name=name)
    assert len(found) == 1
    rows = found[0].find("s:desc", NS).text.split("\n")
    count = found[0].find("s:text", NS).text
    return [row.split(":", 1)[0] for row in rows], count


# The renderer: a hand-built laid-out overview


def _member(name: str, x: float, change_mark: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"role": "member", "name": name, "type": name, "kind": "component", "description": ""}
    if change_mark is not None:
        meta["change"] = change_mark
    return {
        "id": f"member.{name}",
        "x": x,
        "y": 40,
        "width": 120,
        "height": 50,
        "labels": [{"text": name, "x": 10, "y": 17, "width": 40, "height": 16}],
        svg.METADATA_KEY: meta,
    }


def _bundle_edge(source: str, target: str, y: float, change_mark: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "role": "bundle",
        "count": 1,
        "connectors": [
            {
                "name": f"{source}_{target}",
                "source": f"{source}.p",
                "target": f"{target}.r",
                "interface": "I",
                "description": "",
                "loop": False,
            }
        ],
    }
    if change_mark is not None:
        meta["change"] = change_mark
    return {
        "id": f"pair.{source}.{target}",
        "sources": [f"member.{source}"],
        "targets": [f"member.{target}"],
        "sections": [{"startPoint": {"x": 130, "y": y}, "endPoint": {"x": 300, "y": y}}],
        "labels": [{"text": "1", "x": 200, "y": y - 8, "width": 8, "height": 14}],
        svg.METADATA_KEY: meta,
    }


def _laid_out(marked: bool) -> dict[str, Any]:
    def mark(value: str) -> str | None:
        return value if marked else None

    return {
        "id": "root",
        "x": 0,
        "y": 0,
        "width": 700,
        "height": 200,
        "children": [
            {
                "id": "frame",
                "x": 12,
                "y": 12,
                "width": 676,
                "height": 176,
                "labels": [{"text": "Marks", "x": 5, "y": 5, "width": 40, "height": 16}],
                svg.METADATA_KEY: {
                    "role": "frame",
                    "name": "Marks",
                    "title": "Marks",
                    "description": "",
                    "parents": [],
                },
                "children": [
                    _member("Added", 10, mark("added")),
                    _member("Removed", 300, mark("removed")),
                    _member("Kept", 550),
                ],
                "edges": [
                    _bundle_edge("Added", "Removed", 50, mark("added")),
                    _bundle_edge("Removed", "Kept", 60, mark("removed")),
                    _bundle_edge("Kept", "Added", 70, mark("both")),
                    _bundle_edge("Added", "Kept", 80),
                ],
            }
        ],
    }


def test_each_member_mark_is_its_outline() -> None:
    root = _parse(svg.render_overview(_laid_out(marked=True)))
    assert _member_outline(root, "Added") == (GREEN, "3", None)
    assert _member_outline(root, "Removed") == (RED, "3", svg.DASH)
    assert _member_outline(root, "Kept") == (svg.BOX_STROKE, "1", None)


def test_each_bundle_mark_is_its_line_and_its_head() -> None:
    root = _parse(svg.render_overview(_laid_out(marked=True)))
    assert _bundle(root, "Added -> Removed") == ("3", None, GREEN, "url(#swcraft-arrow-added)")
    assert _bundle(root, "Removed -> Kept") == ("3", None, RED, "url(#swcraft-arrow-removed)")
    assert _bundle(root, "Kept -> Added") == ("3", None, BLUE, "url(#swcraft-arrow-both)")
    assert _bundle(root, "Added -> Kept") == ("1", None, svg.EDGE_STROKE, "url(#swcraft-arrow)")


def test_the_three_coloured_heads_are_emitted_with_their_colours() -> None:
    root = _parse(svg.render_overview(_laid_out(marked=True)))
    assert _marker_ids(root) == ["swcraft-arrow", "swcraft-arrow-lit", *CHANGE_MARKERS]
    fills = {marker.get("id"): marker.find("s:path", NS).get("fill") for marker in root.findall("s:defs/s:marker", NS)}
    assert [fills[marker] for marker in CHANGE_MARKERS] == [GREEN, RED, BLUE]


def test_the_frame_and_the_count_labels_are_never_marked() -> None:
    marked = _parse(svg.render_overview(_laid_out(marked=True)))
    frame = _group(marked, "frame", "Marks").find("s:rect", NS)
    assert (frame.get("stroke"), frame.get("stroke-width")) == (svg.BOX_STROKE, "1.5")
    squares = marked.xpath("//s:g[@class='edge-count']/s:rect", namespaces=NS)
    assert len(squares) == 4
    assert {(square.get("stroke"), square.get("stroke-width")) for square in squares} == {("#999999", None)}
    # No label is added: the labels layer holds the same texts with and without the marks.
    unmarked = _parse(svg.render_overview(_laid_out(marked=False)))
    texts = [[text.text for text in tree.findall("s:g[@class='labels']//s:text", NS)] for tree in (marked, unmarked)]
    assert texts[0] == texts[1]


def test_a_document_without_a_mark_emits_no_coloured_head() -> None:
    text = svg.render_overview(_laid_out(marked=False))
    assert _marker_ids(_parse(text)) == ["swcraft-arrow", "swcraft-arrow-lit"]
    for colour in (GREEN, RED, BLUE):
        assert colour not in text


def test_a_mark_on_one_member_alone_emits_the_heads() -> None:
    graph = _laid_out(marked=False)
    graph["children"][0]["children"][0][svg.METADATA_KEY]["change"] = "removed"
    assert _marker_ids(_parse(svg.render_overview(graph)))[2:] == list(CHANGE_MARKERS)


# The change: two revisions of the diagrams fixture


def _copy(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURE_MODEL, root)
    return root


def _composition(root: Path, payload: dict[str, Any]) -> None:
    write_model_element(root, payload, layer="compositions")


def _connector(root: Path, payload: dict[str, Any]) -> None:
    write_model_element(root, payload, layer="connectors")


@pytest.fixture
def revisions(tmp_path: Path) -> tuple[str, str]:
    """The fixture as the old revision, and a new one with every mark's case.

    * ``Chassis``: the member ``Logger`` removed (red, dashed) and ``raw_link`` removed, so the
      pair ``Sensor -> Controller`` lost one of its two connectors (red).
    * ``Vehicle``: a member ``Logger`` added (green) with ``log_link`` from ``Chassis`` to it
      (green), and ``diag_link`` replaced by ``diag_link_v2`` on ``Diag -> Chassis`` (blue).
    """
    old = _copy(tmp_path, "old")
    new = _copy(tmp_path, "new")
    _composition(
        new,
        {
            "name": "Chassis",
            "title": "Chassis",
            "description": "Three members, two boundary ports and a delegation in each direction.",
            "ports": [
                {"name": "rDiag", "direction": "require", "interface": "IDiag"},
                {"name": "pSpeedOut", "direction": "provide", "interface": "ISpeed"},
            ],
            "members": [{"name": "Sensor", "type": "Sensor"}, {"name": "Controller", "type": "Controller"}],
            "delegations": [
                {"name": "Chassis_rDiag", "port": "rDiag", "member": "Controller", "member_port": "rDiag"},
                {"name": "Chassis_pSpeedOut", "port": "pSpeedOut", "member": "Controller", "member_port": "pSpeedOut"},
            ],
        },
    )
    _composition(
        new,
        {
            "name": "Vehicle",
            "title": "Vehicle",
            "root": True,
            "members": [
                {"name": "Chassis", "composition": "Chassis"},
                {"name": "Diag", "type": "DiagServer"},
                {"name": "Logger", "type": "Logger"},
            ],
        },
    )
    model_element_path(new, "raw_link", layer="connectors").unlink()
    model_element_path(new, "diag_link", layer="connectors").unlink()
    _connector(
        new,
        {
            "name": "diag_link_v2",
            "from": {"swc": "DiagServer", "port": "pDiag"},
            "to": {"composition": "Chassis", "port": "rDiag"},
        },
    )
    _connector(
        new,
        {
            "name": "log_link",
            "from": {"composition": "Chassis", "port": "pSpeedOut"},
            "to": {"swc": "Logger", "port": "rLog"},
        },
    )
    return str(old), str(new)


@pytest.fixture
def node() -> str:
    return requires_node()


def test_the_compositions_holding_a_change_are_shown(revisions: tuple[str, str]) -> None:
    old, new = revisions
    assert change.shown_compositions(old, new, []) == ("Chassis", "Vehicle")
    assert change.shown_compositions(old, old, []) == ()
    # A name the differ reports is shown although its overview did not move; one neither
    # revision declares is ignored.
    assert change.shown_compositions(old, old, ["Vehicle", "Nowhere"]) == ("Vehicle",)


def test_a_delegation_only_change_is_shown_when_the_differ_names_it_and_draws_no_mark(
    tmp_path: Path, node: str
) -> None:
    old = _copy(tmp_path, "old")
    new = _copy(tmp_path, "new")
    path = model_element_path(new, "Chassis", layer="compositions")
    text = path.read_text(encoding="utf-8").replace("The provide-side delegation.", "Moved by the change.")
    path.write_text(text, encoding="utf-8", newline="\n")
    assert change.shown_compositions(str(old), str(new), []) == ()
    (overview,) = change.change_overviews(str(old), str(new), ["Chassis"])
    assert (overview.composition, overview.stem) == ("Chassis", "Chassis")
    assert overview.before_svg is not None
    for text in (overview.before_svg, overview.after_svg):
        assert _marker_ids(_parse(text)) == ["swcraft-arrow", "swcraft-arrow-lit"]
        for colour in (GREEN, RED, BLUE):
            assert colour not in text


def test_the_after_picture_marks_every_case(revisions: tuple[str, str], node: str) -> None:
    old, new = revisions
    overviews = change.change_overviews(old, new, [])
    assert [(item.composition, item.stem) for item in overviews] == [("Chassis", "Chassis"), ("Vehicle", "Vehicle")]
    chassis, vehicle = (_parse(item.after_svg) for item in overviews)

    assert _member_outline(chassis, "Logger") == (RED, "3", svg.DASH)
    assert _member_outline(chassis, "Sensor") == (svg.BOX_STROKE, "1", None)
    assert _member_outline(chassis, "Controller") == (svg.BOX_STROKE, "1", None)
    assert _bundle(chassis, "Sensor -> Controller") == ("3", None, RED, "url(#swcraft-arrow-removed)")
    # The merged rows: the after side's connectors, then the removed one; the after count.
    assert _connector_rows(chassis, "Sensor -> Controller") == (["speed_link", "raw_link"], "1")
    assert _bundle(chassis, "Controller -> Controller") == ("1", None, svg.EDGE_STROKE, "url(#swcraft-arrow)")
    frame = _group(chassis, "frame", "Chassis").find("s:rect", NS)
    assert frame.get("stroke") == svg.BOX_STROKE

    assert _member_outline(vehicle, "Logger") == (GREEN, "3", None)
    assert _member_outline(vehicle, "Chassis") == (svg.BOX_STROKE, "1", None)
    assert _member_outline(vehicle, "Diag") == (svg.BOX_STROKE, "1", None)
    assert _bundle(vehicle, "Chassis -> Logger") == ("3", None, GREEN, "url(#swcraft-arrow-added)")
    assert _bundle(vehicle, "Diag -> Chassis") == ("3", None, BLUE, "url(#swcraft-arrow-both)")
    assert _connector_rows(vehicle, "Diag -> Chassis") == (["diag_link_v2", "diag_link"], "1")


def test_the_before_picture_carries_no_mark(revisions: tuple[str, str], node: str) -> None:
    old, new = revisions
    for item in change.change_overviews(old, new, []):
        assert item.before_svg is not None
        before = _parse(item.before_svg)
        assert _marker_ids(before) == ["swcraft-arrow", "swcraft-arrow-lit"]
        for colour in (GREEN, RED, BLUE):
            assert colour not in item.before_svg
    chassis_before = _parse(change.change_overviews(old, new, [])[0].before_svg)
    # The before picture is the old overview: Logger is there, unmarked, and raw_link is listed.
    assert _member_outline(chassis_before, "Logger") == (svg.BOX_STROKE, "1", None)
    assert _connector_rows(chassis_before, "Sensor -> Controller") == (["raw_link", "speed_link"], "2")


def test_a_case_only_pair_of_compositions_gets_two_stems(tmp_path: Path, node: str) -> None:
    old = _copy(tmp_path, "old")
    new = _copy(tmp_path, "new")
    _composition(old, {"name": "Spare", "title": "Spare", "members": [{"name": "Logger", "type": "Logger"}]})
    _composition(new, {"name": "spare", "title": "spare", "members": [{"name": "Logger", "type": "Logger"}]})
    overviews = {item.composition: item for item in change.change_overviews(str(old), str(new), [])}
    assert list(overviews) == ["Spare", "spare"]
    assert (overviews["Spare"].stem, overviews["spare"].stem) == ("Spare", "spare~1")
    # Removed: the before picture is the old overview, the after one the same members marked removed.
    assert overviews["Spare"].before_svg is not None
    assert _member_outline(_parse(overviews["Spare"].after_svg), "Logger") == (RED, "3", svg.DASH)
    # Added: no before picture; every member of the after one is added.
    assert overviews["spare"].before_svg is None
    assert _member_outline(_parse(overviews["spare"].after_svg), "Logger") == (GREEN, "3", None)


def test_a_layout_failure_propagates(revisions: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(graphs: object) -> list[dict[str, Any]]:
        raise DiagramLayoutError("the diagram layout failed (exit 1): the layout input was refused")

    monkeypatch.setattr(change.layout, "layout", refuse)
    old, new = revisions
    with pytest.raises(DiagramLayoutError, match="the layout input was refused"):
        change.change_overviews(old, new, [])


def test_the_merge_leaves_the_extractor_graphs_untouched(revisions: tuple[str, str]) -> None:
    from swcraft_tools.writer.diagrams import graph

    old, new = revisions
    before = next(item.overview for item in graph.build_graphs(old).compositions if item.name == "Chassis")
    after = next(item.overview for item in graph.build_graphs(new).compositions if item.name == "Chassis")
    snapshot = (repr(before), repr(after))
    merged = change.merged_overview(before, after)
    assert (repr(before), repr(after)) == snapshot
    assert [child["id"] for child in merged["children"][0]["children"]] == [
        "member.Sensor",
        "member.Controller",
        "member.Logger",
    ]


def _without_opens(value: Any) -> Any:
    """*value* with every metadata's model entry taken out."""
    if isinstance(value, dict):
        stripped = {key: _without_opens(item) for key, item in value.items()}
        meta = stripped.get(graph.METADATA_KEY)
        if isinstance(meta, dict):
            meta.pop(graph.OPENS_KEY, None)
        return stripped
    if isinstance(value, list):
        return [_without_opens(item) for item in value]
    return value


def test_the_change_reports_pictures_carry_no_model_entry_and_did_not_move(
    revisions: tuple[str, str], node: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change report's SVGs equal those drawn from graphs with every model entry stripped.

    The model entries are opt-in, and the change report passes no ``sources``.
    """
    old, new = revisions
    shown = change.change_overviews(old, new, [])
    building = graph.build_graphs
    frame = building(old).compositions[0].overview["children"][0]
    assert frame[graph.METADATA_KEY][graph.OPENS_KEY] == {"file": "compositions/Chassis.yaml", "member": None}, (
        "the graphs carry entries, so stripping them is a real comparison"
    )

    def build_without_opens(root: str) -> graph.DiagramGraphs:
        built = building(root)
        compositions = tuple(
            dataclasses.replace(item, ibd=_without_opens(item.ibd), overview=_without_opens(item.overview))
            for item in built.compositions
        )
        return dataclasses.replace(built, compositions=compositions)

    monkeypatch.setattr(graph, "build_graphs", build_without_opens)
    stripped = change.change_overviews(old, new, [])
    assert [(item.before_svg, item.after_svg) for item in shown] == [
        (item.before_svg, item.after_svg) for item in stripped
    ]
    chassis_after = _parse(shown[0].after_svg)
    assert _member_outline(chassis_after, "Logger") == (RED, "3", svg.DASH), "the removed member is among them"
    for item in shown:
        for text in (item.before_svg, item.after_svg):
            assert text is not None
            assert svg.SOURCE_ATTRIBUTE not in text
