"""The diagram extractor: ``writer/diagrams/graph.py``, what the model becomes before it is laid out or drawn.

The fixture is ``fixtures/diagrams/model`` (its README says what each element is for); it passes ``check-model``. Shapes
a valid model cannot hold are written into a copy under ``tmp_path``. The structure half needs no Node; the Node half
lays every graph out through the real bridge, the proof that the ids, options and wrapping root are ones ELK accepts (an
``INCLUDE_CHILDREN`` root carrying ports hangs; an edge to the root's own ports is refused) and that the ``swcraft``
metadata survives the round trip.

What these tests do not reach: whether the picture is right. They are regression checks derived from the implementation;
the correctness oracle is a reader's look at real tool exports.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
from math import ceil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.writer.diagrams import graph, layout

FIXTURE_MODEL = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams" / "model"
PACKAGE_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams"


@pytest.fixture(scope="module")
def graphs() -> graph.DiagramGraphs:
    return graph.build_graphs(str(FIXTURE_MODEL))


@pytest.fixture
def model_copy(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    shutil.copytree(FIXTURE_MODEL, root)
    return root


def _composition(graphs: graph.DiagramGraphs, name: str) -> graph.CompositionGraphs:
    return next(item for item in graphs.compositions if item.name == name)


def _frame(ibd: dict[str, Any]) -> dict[str, Any]:
    return ibd["children"][0]


def _member(frame: dict[str, Any], name: str) -> dict[str, Any]:
    return next(child for child in frame["children"] if child["id"] == f"member.{name}")


def _edge(frame: dict[str, Any], edge_id: str) -> dict[str, Any]:
    return next(edge for edge in frame["edges"] if edge["id"] == edge_id)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# The internal block diagram


def test_one_ibd_and_one_overview_per_composition_in_reader_order(graphs: graph.DiagramGraphs) -> None:
    assert [item.name for item in graphs.compositions] == ["Chassis", "Vehicle"]
    assert graph.view_ids(graphs) == [
        "ibd-Chassis",
        "overview-Chassis",
        "ibd-Vehicle",
        "overview-Vehicle",
        "nesting",
        "deployment",
        "sequence-speed_cycle",
    ]
    assert graphs.elk_graphs() == [
        _composition(graphs, "Chassis").ibd,
        _composition(graphs, "Chassis").overview,
        _composition(graphs, "Vehicle").ibd,
        _composition(graphs, "Vehicle").overview,
    ]


def test_the_ibd_wraps_the_composition_in_a_root_that_carries_no_port(graphs: graph.DiagramGraphs) -> None:
    ibd = _composition(graphs, "Chassis").ibd
    assert ibd["id"] == "root"
    assert ibd["layoutOptions"]["elk.hierarchyHandling"] == "INCLUDE_CHILDREN"
    assert ibd["layoutOptions"]["elk.algorithm"] == "layered"
    assert ibd["layoutOptions"]["elk.direction"] == "RIGHT"
    assert ibd["layoutOptions"]["elk.edgeRouting"] == "ORTHOGONAL"
    assert "ports" not in ibd and "edges" not in ibd
    assert [child["id"] for child in ibd["children"]] == ["frame"]
    frame = _frame(ibd)
    assert frame["layoutOptions"]["elk.portConstraints"] == "FIXED_SIDE"
    assert frame[graph.METADATA_KEY] == {
        "role": "frame",
        "name": "Chassis",
        "title": "Chassis",
        "description": "Three members, two boundary ports and a delegation in each direction.",
        "parents": [["Vehicle", "Chassis"]],
        "opens": {"file": "compositions/Chassis.yaml", "member": None},
    }


def test_every_member_is_drawn_with_every_port_of_its_type(graphs: graph.DiagramGraphs) -> None:
    frame = _frame(_composition(graphs, "Chassis").ibd)
    assert [child["id"] for child in frame["children"]] == ["member.Sensor", "member.Controller", "member.Logger"]
    controller = _member(frame, "Controller")
    assert [port["id"] for port in controller["ports"]] == [
        "port.Controller.rSpeed",
        "port.Controller.rRaw",
        "port.Controller.pLoop",
        "port.Controller.rLoop",
        "port.Controller.pSpeedOut",
        "port.Controller.rDiag",
    ]
    # Logger's one port is connected to nothing and is drawn all the same.
    assert [port["id"] for port in _member(frame, "Logger")["ports"]] == ["port.Logger.rLog"]
    assert controller["layoutOptions"]["elk.portConstraints"] == "FIXED_SIDE"
    assert controller[graph.METADATA_KEY] == {
        "role": "member",
        "name": "Controller",
        "type": "Controller",
        "kind": "component",
        "description": "Consumes the speed, feeds its own estimate back to itself and asks the diagnostics.",
        "opens": {"file": "swc/chassis/Controller.yaml", "member": None},
    }


def test_a_member_port_sits_on_the_side_of_its_direction_with_the_anchor_on_its_outer_end(
    graphs: graph.DiagramGraphs,
) -> None:
    frame = _frame(_composition(graphs, "Chassis").ibd)
    ports = {port["id"]: port for port in _member(frame, "Sensor")["ports"] + _member(frame, "Controller")["ports"]}
    provide = ports["port.Sensor.pSpeed"]
    require = ports["port.Controller.rDiag"]
    # A port is as wide as the glyph it carries: the whole square, centred on the owner's border, then the stem and the
    # shape beyond its outer edge. The offset stays the square's half; only the anchor, the box's outer end, moves.
    assert (provide["width"], provide["height"]) == (32, 14)
    assert (require["width"], require["height"]) == (26, 14)
    assert provide["layoutOptions"] == {
        "elk.port.side": "EAST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(32,7)",
    }
    assert require["layoutOptions"] == {
        "elk.port.side": "WEST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(0,7)",
    }
    assert graph.port_width("provide", "sender_receiver") == 32
    assert graph.port_width("require", "sender_receiver") == 30
    assert graph.port_width("provide", "client_server") == 32
    assert graph.port_width("require", "client_server") == 26
    # An undeclared interface kind draws no glyph, so the port is measured as the bare square: the renderer's rule and
    # the extractor's are one.
    assert graph.port_width("provide", None) == graph.port_width("require", None) == graph.PORT_SQUARE == 12
    assert provide[graph.METADATA_KEY] == {
        "role": "port",
        "owner": "Sensor",
        "name": "pSpeed",
        "direction": "provide",
        "interface": "ISpeed",
        "interfaceKind": "sender_receiver",
        "interfaceWord": "sender-receiver",
        "description": "The filtered speed.",
        "opens": {"file": "swc/chassis/Sensor.yaml", "member": "ports/pSpeed"},
    }
    assert require[graph.METADATA_KEY]["interfaceKind"] == "client_server"
    assert provide["labels"][0]["text"] == "pSpeed"


#: A 44-character all-capitals port name, a length that ran over its square, and a mixed-case name of the same length.
ALL_CAPITALS_44 = "SPEED_SENSOR_FRONT_LEFT_RAW_VALUE_STATE_FLAG"
MIXED_CASE_44 = "Speed_Sensor_Front_Left_Raw_Value_State_Flag"


def test_a_label_box_is_estimated_per_character_class() -> None:
    """A capital is about 0.67 em, so an all-capitals label box is honest.

    A flat 0.6 em undersized such a name by some 11 px at the port's font, and the text ran over the square.
    """
    assert len(ALL_CAPITALS_44) == len(MIXED_CASE_44) == 44
    capitals = sum(character.isupper() for character in ALL_CAPITALS_44)
    all_capitals = graph._label(ALL_CAPITALS_44, graph.PORT_FONT_SIZE)["width"]
    assert all_capitals >= capitals * 0.67 * graph.PORT_FONT_SIZE
    assert all_capitals > 44 * 0.6 * graph.PORT_FONT_SIZE + 4
    assert graph._label(MIXED_CASE_44, graph.PORT_FONT_SIZE)["width"] < all_capitals
    assert graph._label("A" * 44, graph.PORT_FONT_SIZE)["width"] == round(44 * 6.7 + 4, 2)
    assert graph._label("a1_" * 4, graph.PORT_FONT_SIZE)["width"] == round(12 * 5.5 + 4, 2)
    assert graph._label(" :-", graph.PORT_FONT_SIZE)["width"] == round(3 * 6 + 4, 2)


def test_the_compositions_own_ports_point_out_of_the_frame_and_the_delegation_arrives_behind(
    graphs: graph.DiagramGraphs,
) -> None:
    """A composition's port faces the environment: the **offset is the member's**, only the **anchor** is mirrored.

    The anchor sits on the inner face of the square, the far end of the port box: ``(width,7)`` on a west port,
    ``(0,7)`` on an east one; it moves with the port's own width.
    """
    frame = _frame(_composition(graphs, "Chassis").ibd)
    ports = {port["id"]: port for port in frame["ports"]}
    assert list(ports) == ["boundary.rDiag", "boundary.pSpeedOut"]
    assert ports["boundary.rDiag"]["layoutOptions"] == {
        "elk.port.side": "WEST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(26,7)",
    }
    assert ports["boundary.pSpeedOut"]["layoutOptions"] == {
        "elk.port.side": "EAST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(0,7)",
    }
    # The member's port, the other way round: the same offset, the anchor on the glyph's tip.
    member_ports = {port["id"]: port for port in _member(frame, "Controller")["ports"]}
    assert member_ports["port.Controller.rDiag"]["layoutOptions"] == {
        "elk.port.side": "WEST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(0,7)",
    }
    assert ports["boundary.pSpeedOut"][graph.METADATA_KEY] == {
        "role": "port",
        "owner": None,
        "name": "pSpeedOut",
        "direction": "provide",
        "interface": "ISpeed",
        "interfaceKind": "sender_receiver",
        "interfaceWord": "sender-receiver",
        "description": "The speed the chassis publishes.",
        "opens": {"file": "compositions/Chassis.yaml", "member": "ports/pSpeedOut"},
    }


#: A boundary port name long enough to cross the minimum frame padding; invented, as every name here is.
LONG_BOUNDARY_NAME = "C_Tsyn_CalibrationNotification_GRP_CHASSIS_0_CHANNEL_B_7b4e1a02"


def _boundary_port(side: str, width: int, label_width: float) -> dict[str, object]:
    """One boundary port node as :func:`graph.frame_padding` reads it."""
    return {
        "width": width,
        "labels": [{"width": label_width}],
        "layoutOptions": {"elk.port.side": side},
    }


def test_the_frames_two_sides_are_derived_apart_and_never_below_the_minimum(
    graphs: graph.DiagramGraphs,
) -> None:
    """Each side is its own boundary labels' end; a side whose labels are short keeps 90.

    Both of ``Chassis``'s boundary names are short of the minimum; the widening is the next test's, on a long name.
    """
    ibd = _composition(graphs, "Chassis").ibd
    for name in ("rDiag", "pSpeedOut"):
        label = graph._label(name, graph.PORT_FONT_SIZE)["width"]
        assert ceil(graph.PORT_SQUARE // 2 + graph.LABEL_PORT_SPACING + label + 12) < graph.FRAME_SIDE_PADDING
    assert _frame(ibd)["layoutOptions"]["elk.padding"] == "[top=40,left=90,bottom=40,right=90]"
    # The overview's frame draws no port at all and keeps the minimum whatever the names are.
    assert _frame(_composition(graphs, "Chassis").overview)["layoutOptions"]["elk.padding"] == (
        "[top=40,left=90,bottom=40,right=90]"
    )


def test_the_frames_side_padding_makes_room_for_a_long_boundary_label() -> None:
    """A boundary label is drawn inside the frame, so the members start after it, each side independently.

    ELK sizes a hierarchical node by its ports, not their inside labels, so a constant padding let a long name run
    across the first members.
    """
    label = graph._label(LONG_BOUNDARY_NAME, graph.PORT_FONT_SIZE)["width"]
    west = graph.port_width("require", "sender_receiver")
    padding = graph.frame_padding([_boundary_port("WEST", west, label)])
    # The port's inner edge is half the square: the glyph points outward.
    expected = ceil(graph.PORT_SQUARE // 2 + graph.LABEL_PORT_SPACING + label + graph.FRAME_LABEL_MARGIN)
    assert expected > graph.FRAME_SIDE_PADDING
    assert padding == f"[top=40,left={expected},bottom=40,right=90]"
    east = graph.port_width("provide", "client_server")
    mirrored = graph.frame_padding([_boundary_port("EAST", east, label)])
    assert mirrored == f"[top=40,left=90,bottom=40,right={ceil(6 + 1 + label + 12)}]"
    # The widest label on a side wins, and a port with no label costs nothing but its box.
    both = graph.frame_padding(
        [
            _boundary_port("WEST", west, label),
            _boundary_port("WEST", west, 8.0),
            {"width": 26, "labels": [], "layoutOptions": {"elk.port.side": "EAST"}},
        ]
    )
    assert both == f"[top=40,left={expected},bottom=40,right=90]"


def test_a_member_composition_is_a_box_carrying_that_compositions_boundary_ports(
    graphs: graph.DiagramGraphs,
) -> None:
    frame = _frame(_composition(graphs, "Vehicle").ibd)
    chassis = _member(frame, "Chassis")
    assert chassis[graph.METADATA_KEY]["kind"] == "composition"
    assert chassis[graph.METADATA_KEY]["type"] == "Chassis"
    assert "children" not in chassis
    assert [(port["id"], port["layoutOptions"]["elk.port.side"]) for port in chassis["ports"]] == [
        ("port.Chassis.rDiag", "WEST"),
        ("port.Chassis.pSpeedOut", "EAST"),
    ]
    diag = _member(frame, "Diag")
    assert diag[graph.METADATA_KEY]["type"] == "DiagServer"
    assert diag["labels"][0]["text"] == "Diag : DiagServer"
    assert chassis["labels"][0]["text"] == "Chassis"


def test_every_connector_is_an_edge_between_the_resolved_member_ports(graphs: graph.DiagramGraphs) -> None:
    chassis = _frame(_composition(graphs, "Chassis").ibd)
    speed = _edge(chassis, "connector.speed_link")
    assert (speed["sources"], speed["targets"]) == (["port.Sensor.pSpeed"], ["port.Controller.rSpeed"])
    assert speed[graph.METADATA_KEY] == {
        "role": "connector",
        "name": "speed_link",
        "description": "The filtered speed into the controller.",
        "loop": False,
        "opens": {"file": "connectors/speed_link.yaml", "member": None},
    }
    # The root's connector names the TYPE DiagServer; the member of that type is Diag.
    vehicle = _frame(_composition(graphs, "Vehicle").ibd)
    diag = _edge(vehicle, "connector.diag_link")
    assert (diag["sources"], diag["targets"]) == (["port.Diag.pDiag"], ["port.Chassis.rDiag"])
    assert {edge["id"] for edge in vehicle["edges"]} == {"connector.diag_link"}


def test_a_connector_is_oriented_provide_to_require_whichever_end_it_names_first(model_copy: Path) -> None:
    _write(
        model_copy / "connectors" / "raw_link.yaml",
        "name: raw_link\ncomposition: Chassis\n"
        "from:\n  swc: Controller\n  port: rRaw\nto:\n  swc: Sensor\n  port: pRaw\n",
    )
    frame = _frame(_composition(graph.build_graphs(str(model_copy)), "Chassis").ibd)
    raw = _edge(frame, "connector.raw_link")
    assert (raw["sources"], raw["targets"]) == (["port.Sensor.pRaw"], ["port.Controller.rRaw"])


def test_every_delegation_is_an_edge_to_the_boundary_port_in_the_direction_data_flows(
    graphs: graph.DiagramGraphs,
) -> None:
    frame = _frame(_composition(graphs, "Chassis").ibd)
    inward = _edge(frame, "delegation.Chassis_rDiag")
    assert (inward["sources"], inward["targets"]) == (["boundary.rDiag"], ["port.Controller.rDiag"])
    outward = _edge(frame, "delegation.Chassis_pSpeedOut")
    assert (outward["sources"], outward["targets"]) == (["port.Controller.pSpeedOut"], ["boundary.pSpeedOut"])
    assert outward[graph.METADATA_KEY] == {
        "role": "delegation",
        "name": "Chassis_pSpeedOut",
        "description": "The provide-side delegation.",
        "loop": False,
        "opens": {"file": "compositions/Chassis.yaml", "member": "delegations/Chassis_pSpeedOut"},
    }


def test_a_self_connector_is_an_edge_on_one_member_and_a_loop_record(graphs: graph.DiagramGraphs) -> None:
    frame = _frame(_composition(graphs, "Chassis").ibd)
    loop = _edge(frame, "connector.control_loop")
    assert (loop["sources"], loop["targets"]) == (["port.Controller.pLoop"], ["port.Controller.rLoop"])
    assert loop[graph.METADATA_KEY]["loop"] is True
    assert graph.NotDrawn("ibd-Chassis", "loop", "control_loop", "Controller.pLoop -> Controller.rLoop") in (
        graphs.not_drawn
    )


def test_the_valid_fixture_reports_the_loop_and_the_two_components_no_task_runs(graphs: graph.DiagramGraphs) -> None:
    """The fixture's one task runs two of its four components; the ``deployment`` view names the other two."""
    assert [(record.view, record.kind, record.subject) for record in graphs.not_drawn] == [
        ("ibd-Chassis", "loop", "control_loop"),
        ("deployment", "not-run", "DiagServer"),
        ("deployment", "not-run", "Logger"),
    ]


def test_an_endpoint_that_is_not_a_member_is_not_drawn_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "connectors" / "stray_link.yaml",
        "name: stray_link\ncomposition: Chassis\nfrom:\n  swc: DiagServer\n  port: pDiag\n"
        "to:\n  swc: Controller\n  port: rDiag\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    frame = _frame(_composition(graphs, "Chassis").ibd)
    assert "connector.stray_link" not in {edge["id"] for edge in frame["edges"]}
    assert (
        graph.NotDrawn(
            "ibd-Chassis", "endpoint-not-member", "stray_link", "DiagServer is not the type of a member of Chassis"
        )
        in graphs.not_drawn
    )


def test_an_endpoint_port_its_type_does_not_declare_is_not_drawn_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "connectors" / "ghost_link.yaml",
        "name: ghost_link\ncomposition: Chassis\nfrom:\n  swc: Sensor\n  port: pGhost\n"
        "to:\n  swc: Controller\n  port: rSpeed\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    frame = _frame(_composition(graphs, "Chassis").ibd)
    assert "connector.ghost_link" not in {edge["id"] for edge in frame["edges"]}
    assert graph.NotDrawn("ibd-Chassis", "port-not-declared", "ghost_link", "Sensor declares no port pGhost") in (
        graphs.not_drawn
    )


def test_a_connector_inside_an_undeclared_composition_is_reported_without_a_view(model_copy: Path) -> None:
    _write(
        model_copy / "connectors" / "lost_link.yaml",
        "name: lost_link\ncomposition: Nowhere\nfrom:\n  swc: Sensor\n  port: pSpeed\n"
        "to:\n  swc: Controller\n  port: rSpeed\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    assert graph.NotDrawn(None, "composition-not-declared", "lost_link", "no composition Nowhere") in graphs.not_drawn


def test_a_delegation_to_an_unknown_member_is_not_drawn_and_is_reported(model_copy: Path) -> None:
    chassis = model_copy / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8")
        + "  - name: Chassis_rGhost\n    port: rDiag\n    member: Ghost\n    member_port: rDiag\n",
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    frame = _frame(_composition(graphs, "Chassis").ibd)
    assert "delegation.Chassis_rGhost" not in {edge["id"] for edge in frame["edges"]}
    assert graph.NotDrawn("ibd-Chassis", "member-not-declared", "Chassis_rGhost", "Chassis has no member Ghost") in (
        graphs.not_drawn
    )


def test_a_member_of_an_undeclared_type_is_a_box_without_ports_and_is_reported(model_copy: Path) -> None:
    vehicle = model_copy / "compositions" / "Vehicle.yaml"
    vehicle.write_text(
        vehicle.read_text(encoding="utf-8") + "  - name: Phantom\n    type: Missing\n",
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    phantom = _member(_frame(_composition(graphs, "Vehicle").ibd), "Phantom")
    assert phantom["ports"] == []
    assert graph.NotDrawn("ibd-Vehicle", "type-not-declared", "Phantom", "no component Missing") in graphs.not_drawn


def test_a_member_name_declared_twice_is_drawn_once_and_reported(model_copy: Path) -> None:
    """ELK refuses two nodes of one id, so the second declaration is a record rather than a crash."""
    vehicle = model_copy / "compositions" / "Vehicle.yaml"
    vehicle.write_text(
        vehicle.read_text(encoding="utf-8") + "  - name: Diag\n    type: Logger\n",
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    frame = _frame(_composition(graphs, "Vehicle").ibd)
    assert [child["id"] for child in frame["children"]] == ["member.Chassis", "member.Diag"]
    assert _member(frame, "Diag")[graph.METADATA_KEY]["type"] == "DiagServer"
    assert graph.NotDrawn("ibd-Vehicle", "duplicate-name", "Diag", "Vehicle declares Diag twice") in graphs.not_drawn


# The bundled overview


def test_the_overview_has_one_node_per_member_and_one_edge_per_connected_pair(graphs: graph.DiagramGraphs) -> None:
    overview = _composition(graphs, "Chassis").overview
    assert overview["id"] == "root"
    # Orthogonal, like the IBD's: a bundle's last segment is perpendicular to the box it enters, so its arrowhead can
    # follow the line.
    assert overview["layoutOptions"]["elk.edgeRouting"] == "ORTHOGONAL"
    frame = _frame(overview)
    assert frame[graph.METADATA_KEY]["name"] == "Chassis"
    assert [child["id"] for child in frame["children"]] == ["member.Sensor", "member.Controller", "member.Logger"]
    assert all("ports" not in child for child in frame["children"])
    edges = [(edge["id"], edge["sources"], edge["targets"], edge[graph.METADATA_KEY]) for edge in frame["edges"]]
    # In the order each pair's first connector is read (by storage path). A bundle carries the whole connector: the
    # renderer builds the panel's row and the IBD line's hover from these facts.
    assert edges == [
        (
            "pair.Controller.Controller",
            ["member.Controller"],
            ["member.Controller"],
            {
                "role": "bundle",
                "count": 1,
                "connectors": [
                    {
                        "name": "control_loop",
                        "source": "Controller.pLoop",
                        "target": "Controller.rLoop",
                        "interface": "ISpeed",
                        "description": "",
                        "loop": True,
                    }
                ],
            },
        ),
        (
            "pair.Sensor.Controller",
            ["member.Sensor"],
            ["member.Controller"],
            {
                "role": "bundle",
                "count": 2,
                "connectors": [
                    {
                        "name": "raw_link",
                        "source": "Sensor.pRaw",
                        "target": "Controller.rRaw",
                        "interface": "ISpeed",
                        "description": "",
                        "loop": False,
                    },
                    {
                        "name": "speed_link",
                        "source": "Sensor.pSpeed",
                        "target": "Controller.rSpeed",
                        "interface": "ISpeed",
                        "description": "The filtered speed into the controller.",
                        "loop": False,
                    },
                ],
            },
        ),
    ]
    assert [edge["labels"][0]["text"] for edge in frame["edges"]] == ["1", "2"]


def test_the_overview_flags_a_member_composition(graphs: graph.DiagramGraphs) -> None:
    frame = _frame(_composition(graphs, "Vehicle").overview)
    chassis = _member(frame, "Chassis")
    assert chassis[graph.METADATA_KEY]["kind"] == "composition"
    assert _member(frame, "Diag")[graph.METADATA_KEY]["kind"] == "component"
    assert [(edge["sources"], edge["targets"]) for edge in frame["edges"]] == [(["member.Diag"], ["member.Chassis"])]


# The nesting overview


def _opens(file: str, member: str | None = None) -> dict[str, str | None]:
    """A model entry as the extractor writes it: the file, and the member line to reveal in it."""
    return {"file": file, "member": member}


def test_the_nesting_tree_starts_at_the_root_with_its_title(graphs: graph.DiagramGraphs) -> None:
    root = graphs.nesting
    assert root == graph.NestingNode(
        name="Vehicle",
        type="Vehicle",
        kind="composition",
        title="Vehicle",
        children=(
            graph.NestingNode(
                name="Chassis",
                type="Chassis",
                kind="composition",
                title="Chassis",
                children=(
                    graph.NestingNode("Sensor", "Sensor", "component", "", (), _opens("swc/chassis/Sensor.yaml")),
                    graph.NestingNode(
                        "Controller", "Controller", "component", "", (), _opens("swc/chassis/Controller.yaml")
                    ),
                    graph.NestingNode("Logger", "Logger", "component", "", (), _opens("swc/chassis/Logger.yaml")),
                ),
                opens=_opens("compositions/Chassis.yaml"),
            ),
            graph.NestingNode("Diag", "DiagServer", "component", "", (), _opens("swc/chassis/DiagServer.yaml")),
        ),
        opens=_opens("compositions/Vehicle.yaml"),
    )


def test_no_root_is_no_nesting_tree_and_a_record(model_copy: Path) -> None:
    vehicle = model_copy / "compositions" / "Vehicle.yaml"
    vehicle.write_text(vehicle.read_text(encoding="utf-8").replace("root: true\n", ""), encoding="utf-8", newline="\n")
    graphs = graph.build_graphs(str(model_copy))
    assert graphs.nesting is None
    assert graph.NotDrawn("nesting", "no-root", "", "no composition declares root: true") in graphs.not_drawn
    assert "nesting" not in graph.view_ids(graphs)


def test_a_second_root_is_not_the_nesting_root_and_is_reported(model_copy: Path) -> None:
    chassis = model_copy / "compositions" / "Chassis.yaml"
    chassis.write_text("root: true\n" + chassis.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    graphs = graph.build_graphs(str(model_copy))
    # Reader order is by storage path, so Chassis is the first root read.
    assert graphs.nesting.name == "Chassis"
    assert graph.NotDrawn("nesting", "root-not-unique", "Vehicle", "Chassis is drawn as the root") in graphs.not_drawn


def test_a_composition_that_contains_itself_is_cut_and_reported(model_copy: Path) -> None:
    chassis = model_copy / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8").replace(
            "members:\n", "members:\n  - name: Again\n    composition: Vehicle\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    again = graphs.nesting.children[0].children[0]
    assert (again.name, again.kind, again.children) == ("Again", "composition", ())
    assert graph.NotDrawn("nesting", "cycle", "Again", "Vehicle contains itself through Chassis") in graphs.not_drawn


# Where each composition is placed -- the way up


def _parents(graphs: graph.DiagramGraphs, name: str) -> dict[str, list[list[str]]]:
    item = _composition(graphs, name)
    return {
        "ibd": _frame(item.ibd)[graph.METADATA_KEY]["parents"],
        "overview": _frame(item.overview)[graph.METADATA_KEY]["parents"],
    }


def test_a_composition_placed_in_one_parent_names_it_and_the_member_on_both_frames(
    graphs: graph.DiagramGraphs,
) -> None:
    assert _parents(graphs, "Chassis") == {"ibd": [["Vehicle", "Chassis"]], "overview": [["Vehicle", "Chassis"]]}
    assert graphs.placements == {"Chassis": (("Vehicle", "Chassis"),)}


def test_the_root_and_a_composition_placed_nowhere_name_no_parent(model_copy: Path) -> None:
    _write(model_copy / "compositions" / "Loose.yaml", "name: Loose\ntitle: Placed nowhere\n")
    graphs = graph.build_graphs(str(model_copy))
    assert _parents(graphs, "Vehicle") == {"ibd": [], "overview": []}
    assert _parents(graphs, "Loose") == {"ibd": [], "overview": []}
    assert set(graphs.placements) == {"Chassis"}


def test_a_composition_placed_in_several_parents_names_each_in_reader_order(model_copy: Path) -> None:
    _write(
        model_copy / "compositions" / "Other.yaml",
        "name: Other\ntitle: A second parent\nmembers:\n  - name: SecondChassis\n    composition: Chassis\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    # Reader order is by storage path: Chassis, Other, Vehicle.
    assert _parents(graphs, "Chassis")["ibd"] == [["Other", "SecondChassis"], ["Vehicle", "Chassis"]]
    assert graphs.placements["Chassis"] == (("Other", "SecondChassis"), ("Vehicle", "Chassis"))


def test_a_parent_placing_one_type_twice_names_its_first_member_once(model_copy: Path) -> None:
    vehicle = model_copy / "compositions" / "Vehicle.yaml"
    vehicle.write_text(
        vehicle.read_text(encoding="utf-8").replace(
            "members:\n", "members:\n  - name: FirstChassis\n    composition: Chassis\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    assert graphs.placements == {"Chassis": (("Vehicle", "FirstChassis"),)}


def test_every_parent_a_frame_names_has_both_its_pages_among_the_views(model_copy: Path) -> None:
    """The frame's up arrow always has somewhere to go: every parent it names has both its pages among the views.

    Covered with a second parent and a case-only name twin, the one case that gives a parent a stem other than its name.
    """
    _write(
        model_copy / "compositions" / "Other.yaml",
        "name: Other\ntitle: A second parent\nmembers:\n  - name: SecondChassis\n    composition: Chassis\n",
    )
    _write(
        model_copy / "compositions" / "other~1.yaml",
        "name: other\ntitle: A case twin\nmembers:\n  - name: TwinChassis\n    composition: Chassis\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    ids = set(graph.view_ids(graphs))
    stems = graphs.composition_stems()
    assert len(graphs.placements["Chassis"]) == 3
    for parents in graphs.placements.values():
        for parent, _ in parents:
            assert {f"ibd-{stems[parent]}", f"overview-{stems[parent]}"} <= ids, parent


# The sequences


def _labelled(sequence: graph.SequenceView) -> tuple[graph.SequenceMessage, ...]:
    """*sequence*'s messages with each end's lifeline key replaced by that lifeline's label."""
    label_of = dict(zip(sequence.lifelines, sequence.labels, strict=True))
    return tuple(
        graph.SequenceMessage(message.kind, label_of[message.source], label_of[message.target], message.label)
        for message in sequence.messages
    )


def test_a_sequence_has_the_task_then_the_components_in_order_of_first_appearance(
    graphs: graph.DiagramGraphs,
) -> None:
    (sequence,) = graphs.sequences
    assert (sequence.name, sequence.title, sequence.task, sequence.task_period) == (
        "speed_cycle",
        "Speed cycle",
        "Task_10ms",
        0.01,
    )
    assert sequence.labels == ("Task_10ms", "Sensor", "Controller")
    # Each lifeline is keyed apart from its label: the task by its name, a component by the identity its selector
    # resolves to.
    assert sequence.lifelines == (
        ("task", "Task_10ms"),
        ("component", "instance", "Chassis/Sensor"),
        ("component", "instance", "Chassis/Controller"),
    )


def test_each_step_activates_its_runnable_and_sends_its_provide_port_connectors(graphs: graph.DiagramGraphs) -> None:
    (sequence,) = graphs.sequences
    assert _labelled(sequence) == (
        graph.SequenceMessage("activation", "Task_10ms", "Sensor", "SensorRun"),
        # SensorRun declares writes on pSpeed only, so pRaw's connector is narrowed away.
        graph.SequenceMessage("data", "Sensor", "Controller", "pSpeed -> rSpeed : ISpeed"),
        graph.SequenceMessage("activation", "Task_10ms", "Controller", "ControlRun — closes the loop"),
        # ControlRun declares no writes, so every provide-port connector is a message.
        graph.SequenceMessage("data", "Controller", "Controller", "pLoop -> rLoop : ISpeed"),
    )


def test_without_declared_writes_every_provide_port_connector_is_a_message(model_copy: Path) -> None:
    sensor = model_copy / "swc" / "chassis" / "Sensor.yaml"
    sensor.write_text(
        sensor.read_text(encoding="utf-8").replace("    writes:\n      - port: pSpeed\n        element: speed\n", ""),
        encoding="utf-8",
        newline="\n",
    )
    (sequence,) = graph.build_graphs(str(model_copy)).sequences
    data = [message.label for message in _labelled(sequence) if message.source == "Sensor"]
    assert data == ["pRaw -> rRaw : ISpeed", "pSpeed -> rSpeed : ISpeed"]


def test_an_undeclared_runnable_keeps_its_activation_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - swc: Sensor\n    runnable: Missing\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert sequence.title == ""
    assert _labelled(sequence) == (graph.SequenceMessage("activation", "Task_10ms", "Sensor", "Missing"),)
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle", "runnable-not-declared", "Sensor.Missing", "Sensor declares no runnable Missing"
        )
        in graphs.not_drawn
    )


def test_an_undeclared_task_has_no_period_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_20ms\nsteps:\n  - swc: Sensor\n    runnable: SensorRun\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert sequence.task_period is None
    assert graph.NotDrawn("sequence-speed_cycle", "task-not-declared", "Task_20ms", "no task Task_20ms") in (
        graphs.not_drawn
    )


# What a port's interface carries


def test_a_sender_receiver_interfaces_lines_name_each_element_with_its_type_in_model_order(
    model_copy: Path,
) -> None:
    """Either type field gives the line its type, with no layer word, and the order is the file's."""
    _write(
        model_copy / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n"
        "  - name: speed\n    implementation_type: uint8\n"
        "  - name: zeta\n    application_type: SpeedKmh\n"
        "  - name: alpha\n    implementation_type: boolean\n",
    )
    contents = graph.build_graphs(str(model_copy)).interface_contents
    assert contents["ISpeed"] == ("speed: uint8", "zeta: SpeedKmh", "alpha: boolean")


def test_a_client_server_interfaces_lines_name_each_operation_with_its_arguments(model_copy: Path) -> None:
    """Every direction is written, an operation with no argument is ``()``, and the order is the file's."""
    _write(
        model_copy / "interfaces" / "IDiag.yaml",
        "name: IDiag\nkind: client_server\noperations:\n"
        "  - name: Reset\n"
        "  - name: Clear\n    arguments:\n"
        "      - name: group\n        direction: in\n        application_type: DtcGroup\n"
        "      - name: result\n        direction: out\n        implementation_type: uint8\n"
        "      - name: count\n        direction: inout\n        implementation_type: uint16\n"
        "  - name: ReadStatus\n    arguments: []\n",
    )
    contents = graph.build_graphs(str(model_copy)).interface_contents
    assert contents["IDiag"] == (
        "Reset()",
        "Clear(in group: DtcGroup, out result: uint8, inout count: uint16)",
        "ReadStatus()",
    )


def test_the_fixtures_interfaces_each_have_their_lines(graphs: graph.DiagramGraphs) -> None:
    assert dict(graphs.interface_contents) == {
        "IDiag": ("ReadStatus(out status: uint8)",),
        "ISpeed": ("speed: uint8",),
    }


def test_an_external_interface_stub_lists_its_member_names_and_invents_no_type(model_copy: Path) -> None:
    """A stub holds names only: its data elements, then its operations; one listing none gives nothing."""
    _write(
        model_copy / "interfaces" / "IExtData.yaml",
        "name: IExtData\nkind: external_interface\nexternal_ref: /Foreign/IExtData\n"
        "external_dest: SENDER-RECEIVER-INTERFACE\ndata_elements: [rpm, torque]\n",
    )
    _write(
        model_copy / "interfaces" / "IExtOps.yaml",
        "name: IExtOps\nkind: external_interface\nexternal_ref: /Foreign/IExtOps\n"
        "external_dest: CLIENT-SERVER-INTERFACE\noperations: [Start, Stop]\n",
    )
    _write(
        model_copy / "interfaces" / "IExtMode.yaml",
        "name: IExtMode\nkind: external_interface\nexternal_ref: /Foreign/IExtMode\n"
        "external_dest: MODE-SWITCH-INTERFACE\n",
    )
    contents = graph.build_graphs(str(model_copy)).interface_contents
    assert contents["IExtData"] == ("rpm", "torque")
    assert contents["IExtOps"] == ("Start", "Stop")
    assert contents["IExtMode"] == ()


def test_an_interface_name_declared_twice_takes_its_lines_from_the_first_declaration(model_copy: Path) -> None:
    """The first file the reader returns wins, as for the port's glyph (``interface.duplicate-name``)."""
    _write(
        model_copy / "interfaces" / "ISpeed~1.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n  - name: other\n    implementation_type: uint32\n",
    )
    contents = graph.build_graphs(str(model_copy)).interface_contents
    assert contents["ISpeed"] == ("speed: uint8",)


def test_a_data_message_carries_its_ports_interface_and_an_activation_none(graphs: graph.DiagramGraphs) -> None:
    """Read off ``message.interface`` directly: ``_labelled`` rebuilds the messages without it."""
    (sequence,) = graphs.sequences
    assert [(message.kind, message.interface) for message in sequence.messages] == [
        ("activation", ""),
        ("data", "ISpeed"),
        ("activation", ""),
        ("data", "ISpeed"),
    ]


def test_a_data_message_from_a_port_without_an_interface_carries_the_empty_name(model_copy: Path) -> None:
    """A sending port that declares no interface gives ``""``, never ``None``: no lookup, no lines."""
    sensor = model_copy / "swc" / "chassis" / "Sensor.yaml"
    text = sensor.read_text(encoding="utf-8")
    _write(
        sensor,
        text.replace(
            "    direction: provide\n    interface: ISpeed\n    description: The filtered",
            "    direction: provide\n    description: The filtered",
            1,
        ),
    )
    (sequence,) = graph.build_graphs(str(model_copy)).sequences
    data = [message for message in sequence.messages if message.kind == "data"]
    assert data
    sensor_message = next(message for message in data if message.label.startswith("pSpeed -> "))
    assert sensor_message.interface == ""
    assert " : " not in sensor_message.label


def _elk_input(graphs: graph.DiagramGraphs) -> bytes:
    """The ELK graphs as ``layout.layout`` encodes them for Node: insertion order, ``ensure_ascii=False``."""
    return json.dumps(graphs.elk_graphs(), ensure_ascii=False).encode("utf-8")


def test_no_member_line_enters_the_elk_input(graphs: graph.DiagramGraphs, model_copy: Path) -> None:
    """The member lines travel beside the graphs, so the layout input is the same bytes."""
    _write(
        model_copy / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n"
        + "".join(f"  - name: element_{index}\n    implementation_type: uint8\n" for index in range(12)),
    )
    _write(
        model_copy / "interfaces" / "IDiag.yaml",
        "name: IDiag\nkind: client_server\noperations:\n  - name: ReadStatus\n  - name: Clear\n",
    )
    grown = graph.build_graphs(str(model_copy))
    assert grown.interface_contents["ISpeed"] != graphs.interface_contents["ISpeed"]
    assert _elk_input(grown) == _elk_input(graphs)


# Where the ELK options live


def test_every_elk_option_string_is_set_in_the_extractor_and_nowhere_else() -> None:
    """No other Python module of the package spells an ``elk.`` option key."""
    offenders = []
    for module in sorted(PACKAGE_DIR.glob("*.py"), key=lambda path: path.as_posix()):
        if module.name == "graph.py":
            continue
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("elk."):
                offenders.append(f"{module.name}:{node.lineno} {node.value}")
    assert offenders == []
    script = (PACKAGE_DIR / "elk-layout.mjs").read_text(encoding="utf-8")
    assert re.findall(r"""["'`]elk\.""", script) == []


def test_two_extractions_are_equal(graphs: graph.DiagramGraphs) -> None:
    assert graph.build_graphs(str(FIXTURE_MODEL)) == graphs


# The Node half: ELK accepts what the extractor builds


@pytest.fixture
def node() -> str:
    return requires_node()


def test_every_graph_of_the_fixture_is_laid_out_and_keeps_its_metadata(node: str, graphs: graph.DiagramGraphs) -> None:
    laid_out = layout.layout(graphs.elk_graphs(), node=node)
    assert len(laid_out) == 4
    chassis = _frame(laid_out[0])
    assert chassis[graph.METADATA_KEY]["name"] == "Chassis"
    assert all("x" in child and "y" in child for child in chassis["children"])
    loop = _edge(chassis, "connector.control_loop")
    assert loop["sections"] and loop[graph.METADATA_KEY]["loop"] is True
    boundary = {port["id"]: port for port in chassis["ports"]}
    assert boundary["boundary.pSpeedOut"]["x"] > boundary["boundary.rDiag"]["x"]
    assert [edge[graph.METADATA_KEY]["count"] for edge in _frame(laid_out[1])["edges"]] == [1, 2]


def test_boundary_ports_no_delegation_reaches_are_spread_along_the_frame_not_stacked(
    node: str, model_copy: Path
) -> None:
    # A composition with many boundary ports and no delegation was laid out in a frame sized by its members alone, its
    # ports stacked closer than their own boxes: with no edge pulling a boundary port, only the frame's size spreads
    # them.
    count = 30
    ports = "".join(
        f"  - name: r{index}\n    direction: require\n    interface: ISpeed\n"
        f"  - name: p{index}\n    direction: provide\n    interface: ISpeed\n"
        for index in range(count)
    )
    _write(
        model_copy / "compositions" / "Wide.yaml",
        "name: Wide\ntitle: Wide\nports:\n" + ports + "members:\n  - name: Logger\n    type: Logger\ndelegations: []\n",
    )
    wide = _composition(graph.build_graphs(str(model_copy)), "Wide")

    (laid_out,) = layout.layout([wide.ibd], node=node)

    frame = _frame(laid_out)
    for side in ("WEST", "EAST"):
        boxes = sorted(
            (port["y"], port["height"]) for port in frame["ports"] if port["layoutOptions"]["elk.port.side"] == side
        )
        assert len(boxes) == count
        for (upper, height), (lower, _) in zip(boxes, boxes[1:]):
            assert lower - upper >= height, f"two {side} boundary ports overlap: {upper} and {lower}"
        assert boxes[0][0] >= 0 and boxes[-1][0] + boxes[-1][1] <= frame["height"], "every port lies on the frame"


def test_a_long_boundary_label_is_laid_out_clear_of_every_member_box(node: str, model_copy: Path) -> None:
    """The derived side padding, through the real bridge: every boundary label's box ends before the members' area.

    A member's own port labels are ELK's business (``PORT_LABELS`` among its ``nodeSize.constraints``): the second half
    asserts each lies inside its own member's box.
    """
    ports = "".join(
        f"  - name: r{name}\n    direction: require\n    interface: ISpeed\n"
        f"  - name: p{name}\n    direction: provide\n    interface: ISpeed\n"
        for name in (LONG_BOUNDARY_NAME, "Short")
    )
    _write(
        model_copy / "compositions" / "Wide.yaml",
        "name: Wide\ntitle: Wide\nports:\n"
        + ports
        + "members:\n  - name: Logger\n    type: Logger\n  - name: Sensor\n    type: Sensor\ndelegations: []\n",
    )
    wide = _composition(graph.build_graphs(str(model_copy)), "Wide")
    assert _frame(wide.ibd)["layoutOptions"]["elk.padding"] != "[top=40,left=90,bottom=40,right=90]"

    (laid_out,) = layout.layout([wide.ibd], node=node)

    frame = _frame(laid_out)
    members = [(child["x"], child["x"] + child["width"], child["id"]) for child in frame["children"]]
    assert len(members) == 2
    for port in frame["ports"]:
        for label in port["labels"]:
            start = port["x"] + label["x"]
            end = start + label["width"]
            for left, right, member_id in members:
                assert end <= left or start >= right, (port["id"], member_id, start, end, left, right)
    for child in frame["children"]:
        for port in child.get("ports", []):
            for label in port["labels"]:
                start = child["x"] + port["x"] + label["x"]
                assert 0 <= start - child["x"], (child["id"], port["id"])
                assert start + label["width"] <= child["x"] + child["width"], (child["id"], port["id"])


# Every record a branch mints, and the element it keeps off the picture


def _append_to(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")


def test_a_boundary_port_declared_twice_is_drawn_once_and_reported(model_copy: Path) -> None:
    chassis = model_copy / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8").replace(
            "ports:\n", "ports:\n  - name: rDiag\n    direction: provide\n    interface: ISpeed\n", 1
        ),
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    ports = _frame(_composition(graphs, "Chassis").ibd)["ports"]
    assert [port["id"] for port in ports].count("boundary.rDiag") == 1
    assert ports[0][graph.METADATA_KEY]["direction"] == "provide", "the first declaration wins"
    assert graph.NotDrawn("ibd-Chassis", "duplicate-name", "rDiag", "Chassis declares rDiag twice") in graphs.not_drawn


def test_a_delegation_declared_twice_is_drawn_once_and_reported(model_copy: Path) -> None:
    _append_to(
        model_copy / "compositions" / "Chassis.yaml",
        "  - name: Chassis_rDiag\n    port: pSpeedOut\n    member: Controller\n    member_port: pSpeedOut\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    edges = [edge["id"] for edge in _frame(_composition(graphs, "Chassis").ibd)["edges"]]
    assert edges.count("delegation.Chassis_rDiag") == 1
    assert graph.NotDrawn("ibd-Chassis", "duplicate-name", "Chassis_rDiag", "Chassis declares Chassis_rDiag twice") in (
        graphs.not_drawn
    )


@pytest.mark.parametrize(
    ("port", "member_port", "detail"),
    [
        ("rGhost", "rDiag", "Chassis declares no port rGhost"),
        ("rDiag", "rGhost", "Controller declares no port rGhost"),
    ],
)
def test_a_delegation_naming_an_undeclared_port_is_not_drawn_and_is_reported(
    model_copy: Path, port: str, member_port: str, detail: str
) -> None:
    _append_to(
        model_copy / "compositions" / "Chassis.yaml",
        f"  - name: Chassis_ghost\n    port: {port}\n    member: Controller\n    member_port: {member_port}\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    edges = {edge["id"] for edge in _frame(_composition(graphs, "Chassis").ibd)["edges"]}
    assert "delegation.Chassis_ghost" not in edges
    assert graph.NotDrawn("ibd-Chassis", "port-not-declared", "Chassis_ghost", detail) in graphs.not_drawn


def test_a_member_port_its_type_declares_twice_is_drawn_once_and_reported(model_copy: Path) -> None:
    logger = model_copy / "swc" / "chassis" / "Logger.yaml"
    logger.write_text(
        logger.read_text(encoding="utf-8").replace(
            "ports:\n", "ports:\n  - name: rLog\n    direction: provide\n    interface: ISpeed\n", 1
        ),
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    ports = [port["id"] for port in _member(_frame(_composition(graphs, "Chassis").ibd), "Logger")["ports"]]
    assert ports == ["port.Logger.rLog"]
    assert graph.NotDrawn("ibd-Chassis", "duplicate-name", "Logger.rLog", "Logger declares rLog twice") in (
        graphs.not_drawn
    )


def test_a_nested_member_of_an_undeclared_composition_is_an_empty_box_and_is_reported(model_copy: Path) -> None:
    _append_to(model_copy / "compositions" / "Vehicle.yaml", "  - name: Phantom\n    composition: Missing\n")
    graphs = graph.build_graphs(str(model_copy))
    phantom = graphs.nesting.children[-1]
    assert (phantom.name, phantom.type, phantom.kind, phantom.children) == ("Phantom", "Missing", "composition", ())
    assert graph.NotDrawn("nesting", "type-not-declared", "Phantom", "no composition Missing") in graphs.not_drawn


def test_a_composition_that_places_itself_directly_is_cut_and_says_so(model_copy: Path) -> None:
    chassis = model_copy / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8").replace(
            "members:\n", "members:\n  - name: Self\n    composition: Chassis\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    cycles = [record for record in graphs.not_drawn if record.kind == "cycle"]
    assert cycles == [graph.NotDrawn("nesting", "cycle", "Self", "Chassis places itself as a member")]


def test_a_step_whose_component_has_the_tasks_name_is_not_drawn_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - swc: Task_10ms\n    runnable: Run\n"
        "  - swc: Sensor\n    runnable: SensorRun\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert sequence.labels == ("Task_10ms", "Sensor")
    # Sensor's data message goes to Controller, which is no step's component and so no lifeline.
    assert _labelled(sequence) == (graph.SequenceMessage("activation", "Task_10ms", "Sensor", "SensorRun"),)
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle",
            "lifeline-shared-with-task",
            "Task_10ms.Run",
            "the component Task_10ms has the task's name, and one lifeline cannot be both",
        )
        in graphs.not_drawn
    )


# The instance selector: a lifeline is keyed by the identity `scheduling_identity` resolves and labelled by the selector
# as written.


def _two_controllers(model: Path, steps: str) -> None:
    """The fixture's `Chassis` with a second `Controller` member, `Backup`, and *steps* as the sequence's."""
    chassis = model / "compositions" / "Chassis.yaml"
    chassis.write_text(
        chassis.read_text(encoding="utf-8").replace(
            "  - name: Logger\n    type: Logger\n",
            "  - name: Logger\n    type: Logger\n  - name: Backup\n    type: Controller\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    _write(model / "sequences" / "speed_cycle.yaml", "name: speed_cycle\ntask: Task_10ms\nsteps:\n" + steps)


def test_two_instance_steps_of_one_type_are_two_lifelines(model_copy: Path) -> None:
    _two_controllers(
        model_copy,
        "  - instance: Chassis/Controller\n    runnable: ControlRun\n"
        "  - instance: Chassis/Backup\n    runnable: ControlRun\n",
    )
    (sequence,) = graph.build_graphs(str(model_copy)).sequences
    assert sequence.labels == ("Task_10ms", "Chassis/Controller", "Chassis/Backup")
    assert sequence.lifelines == (
        ("task", "Task_10ms"),
        ("component", "instance", "Chassis/Controller"),
        ("component", "instance", "Chassis/Backup"),
    )
    activations = [message for message in _labelled(sequence) if message.kind == "activation"]
    assert [message.target for message in activations] == ["Chassis/Controller", "Chassis/Backup"]


def test_a_swc_step_and_an_instance_step_naming_one_occurrence_share_one_lifeline(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - swc: Sensor\n    runnable: SensorRun\n"
        "  - instance: Chassis/Sensor\n    runnable: SensorRun\n",
    )
    (sequence,) = graph.build_graphs(str(model_copy)).sequences
    # Labelled by the selector the step that opened it wrote: the first.
    assert sequence.labels == ("Task_10ms", "Sensor")
    # And one line: both activations end on the one key the two selectors resolve to.
    activations = [message for message in sequence.messages if message.kind == "activation"]
    assert [message.target for message in activations] == [("component", "instance", "Chassis/Sensor")] * 2


def test_a_data_message_from_an_instance_step_is_sent_by_its_resolved_type(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - instance: Chassis/Sensor\n    runnable: SensorRun\n"
        "  - swc: Controller\n    runnable: ControlRun\n",
    )
    (sequence,) = graph.build_graphs(str(model_copy)).sequences
    assert sequence.labels == ("Task_10ms", "Chassis/Sensor", "Controller")
    assert graph.SequenceMessage("data", "Chassis/Sensor", "Controller", "pSpeed -> rSpeed : ISpeed") in _labelled(
        sequence
    )


def test_a_data_message_to_a_type_on_two_lifelines_is_not_drawn_and_names_both(model_copy: Path) -> None:
    _two_controllers(
        model_copy,
        "  - swc: Sensor\n    runnable: SensorRun\n"
        "  - instance: Chassis/Controller\n    runnable: ControlRun\n"
        "  - instance: Chassis/Backup\n    runnable: ControlRun\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert [message.kind for message in sequence.messages] == ["activation"] * 3
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle",
            "receiver-ambiguous",
            "Sensor: pSpeed -> rSpeed : ISpeed",
            "Controller stands on 2 lifelines: Chassis/Controller, Chassis/Backup",
        )
        in graphs.not_drawn
    )


def test_an_instance_label_equal_to_the_tasks_name_is_not_drawn_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - instance: Task_10ms\n    runnable: Run\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert sequence.labels == ("Task_10ms",)
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle",
            "lifeline-shared-with-task",
            "Task_10ms.Run",
            "the component Task_10ms has the task's name, and one lifeline cannot be both",
        )
        in graphs.not_drawn
    )


def test_an_instance_step_under_an_undefined_walk_is_reported_as_unresolved_and_says_why(model_copy: Path) -> None:
    vehicle = model_copy / "compositions" / "Vehicle.yaml"
    vehicle.write_text(vehicle.read_text(encoding="utf-8").replace("root: true\n", ""), encoding="utf-8", newline="\n")
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - instance: Chassis/Sensor\n    runnable: SensorRun\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert _labelled(sequence) == (graph.SequenceMessage("activation", "Task_10ms", "Chassis/Sensor", "SensorRun"),)
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle",
            "instance-not-resolved",
            "Chassis/Sensor.SensorRun",
            "the instance Chassis/Sensor is not resolved: the composition walk is undefined "
            "(no root composition, several, or a cycle the root reaches)",
        )
        in graphs.not_drawn
    )


def test_an_instance_step_resolving_to_no_component_keeps_its_activation_and_is_reported(model_copy: Path) -> None:
    _write(
        model_copy / "sequences" / "speed_cycle.yaml",
        "name: speed_cycle\ntask: Task_10ms\nsteps:\n  - instance: Chassis/Nowhere\n    runnable: Run\n",
    )
    graphs = graph.build_graphs(str(model_copy))
    (sequence,) = graphs.sequences
    assert _labelled(sequence) == (graph.SequenceMessage("activation", "Task_10ms", "Chassis/Nowhere", "Run"),)
    assert (
        graph.NotDrawn(
            "sequence-speed_cycle",
            "instance-not-resolved",
            "Chassis/Nowhere.Run",
            "the instance Chassis/Nowhere resolves to no component",
        )
        in graphs.not_drawn
    )


# File stems


@pytest.mark.parametrize(
    ("names", "stems"),
    [
        (["Chassis", "Vehicle"], ["Chassis", "Vehicle"]),
        (["Chassis", "chassis"], ["Chassis", "chassis~1"]),
        (["Chassis", "chassis", "CHASSIS"], ["Chassis", "chassis~1", "CHASSIS~2"]),
        # A declared name spelled like a suffix is never taken by another's suffix.
        (["Chassis", "chassis", "Chassis~1"], ["Chassis", "chassis~2", "Chassis~1"]),
        # A name declared twice is one stem; the generator refuses it by name.
        (["Chassis", "Chassis"], ["Chassis", "Chassis"]),
    ],
)
def test_a_name_equal_to_an_earlier_one_but_for_case_takes_the_nearest_free_suffix(
    names: list[str], stems: list[str]
) -> None:
    assert graph.file_stems(names) == stems


def test_two_compositions_differing_only_in_case_get_two_sets_of_view_ids(model_copy: Path) -> None:
    _write(model_copy / "compositions" / "chassis~1.yaml", "name: chassis\ntitle: Lower-case twin\n")
    graphs = graph.build_graphs(str(model_copy))
    # Reader order compares storage paths as plain strings, so "Vehicle" sorts before "chassis~1".
    assert [(item.name, item.stem) for item in graphs.compositions] == [
        ("Chassis", "Chassis"),
        ("Vehicle", "Vehicle"),
        ("chassis", "chassis~1"),
    ]
    assert graph.view_ids(graphs)[:6] == [
        "ibd-Chassis",
        "overview-Chassis",
        "ibd-Vehicle",
        "overview-Vehicle",
        "ibd-chassis~1",
        "overview-chassis~1",
    ]
    assert graphs.composition_stems() == {"Chassis": "Chassis", "chassis": "chassis~1", "Vehicle": "Vehicle"}
