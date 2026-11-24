"""The diagrams draw a port on an external interface stub.

A glyph is drawn only for a declared type: ``CLIENT-SERVER-INTERFACE`` draws (and so sizes
and anchors) the client-server glyph, ``SENDER-RECEIVER-INTERFACE`` the sender-receiver
one, and every other ``DEST`` (the abstract values included) the bare square, as a value
``check-model`` refuses does. The port's hover text says ``external`` whatever is drawn,
so a reader is never told a stub is an interface the model declares. The extractor
carries ``interfaceKind`` (the glyph) and ``interfaceWord`` (the word) apart.

Three halves: the **graph** half reads the port boxes of ``external_stubs_support``'s
stub model as data (no Node); the **renderer** half draws a recorded layout with one port
turned into a stub's (no Node); the **Node** half lays the stub model out through the real bridge and
reads the SVG, the overview's connector list included.

What these tests do not reach: whether the picture is right (a human reading of real
exports), and a stub-free model's bytes, which are the byte-identity gate's and the
diagram goldens'.
"""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import external_stubs_support as stubs
import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.writer.diagrams import graph, layout, svg

LAID_OUT_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams" / "laid-out"
NS = "{http://www.w3.org/2000/svg}"

#: Every ``DEST`` an interface stub admits (the validator's ``EXTERNAL_INTERFACE_DESTS``) and
#: the glyph kind it draws, ``None`` for the bare square.
GLYPH_BY_DEST: dict[str, str | None] = {
    "CLIENT-SERVER-INTERFACE": "client_server",
    "DATA-INTERFACE": None,
    "MODE-SWITCH-INTERFACE": None,
    "NV-DATA-INTERFACE": None,
    "PARAMETER-INTERFACE": None,
    "PORT-INTERFACE": None,
    "SENDER-RECEIVER-INTERFACE": "sender_receiver",
    "TRIGGER-INTERFACE": None,
}

#: ``(port id, direction, glyph kind)`` for every port the stub model puts on a stub.
STUB_PORT_GLYPHS: tuple[tuple[str, str, str | None], ...] = (
    ("port.ExampleSWC.pCs", "provide", "client_server"),
    ("port.ExampleSWC.pSr", "provide", "sender_receiver"),
    ("port.ExampleSWC.pMode", "provide", None),
    ("port.ExampleConsumerSWC.rCs", "require", "client_server"),
    ("port.ExampleConsumerSWC.rSr", "require", "sender_receiver"),
    ("port.ExampleConsumerSWC.rMode", "require", None),
    ("port.ExampleConsumerSWC.rAny", "require", None),
    ("boundary.pExtSr", "provide", "sender_receiver"),
    ("boundary.rExtCs", "require", "client_server"),
)


@pytest.fixture(scope="module")
def stub_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stubs.external_stubs_model_root(tmp_path_factory.mktemp("stubs") / "project")


@pytest.fixture(scope="module")
def stub_graphs(stub_model: Path) -> graph.DiagramGraphs:
    return graph.build_graphs(str(stub_model))


def _frame(ibd: dict[str, Any]) -> dict[str, Any]:
    return ibd["children"][0]


def _ports(ibd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    frame = _frame(ibd)
    ports = {port["id"]: port for port in frame.get("ports", [])}
    for member in frame["children"]:
        ports.update({port["id"]: port for port in member.get("ports", [])})
    return ports


def _root_ibd(stub_graphs: graph.DiagramGraphs) -> dict[str, Any]:
    (root,) = [item for item in stub_graphs.compositions if item.name == "Root"]
    return root.ibd


# The graph: the glyph kind sizes and anchors the port, the word travels beside it


def test_a_required_client_server_stub_port_is_sized_and_anchored_as_a_client_server_port(
    stub_graphs: graph.DiagramGraphs,
) -> None:
    """A CS stub's box is the CS box (half circle 6 wide), not the SR one (fork 8 wide).

    A require boundary port anchors on the inner face of its square, so a stub sized as
    sender-receiver would put the delegation 4 px off.
    """
    ports = _root_ibd(stub_graphs)
    ports = _ports(ports)
    member = ports["port.ExampleConsumerSWC.rCs"]
    assert member["width"] == graph.port_width("require", "client_server") == 26
    assert member["layoutOptions"]["elk.port.anchor"] == "(0,7)"
    boundary = ports["boundary.rExtCs"]
    assert boundary["width"] == 26
    assert boundary["layoutOptions"] == {
        "elk.port.side": "WEST",
        "elk.port.borderOffset": "-6",
        "elk.port.anchor": "(26,7)",
    }


@pytest.mark.parametrize(("port_id", "direction", "glyph"), STUB_PORT_GLYPHS)
def test_every_stub_port_is_sized_by_the_glyph_its_dest_names(
    stub_graphs: graph.DiagramGraphs, port_id: str, direction: str, glyph: str | None
) -> None:
    port = _ports(_root_ibd(stub_graphs))[port_id]
    assert port["width"] == graph.port_width(direction, glyph)
    if glyph is None:
        assert port["width"] == graph.PORT_SQUARE
    assert port[graph.METADATA_KEY]["interfaceKind"] == glyph
    assert port[graph.METADATA_KEY]["interfaceWord"] == "external"


def test_a_declared_interfaces_port_keeps_its_kind_and_its_word(stub_graphs: graph.DiagramGraphs) -> None:
    ports = _ports(_root_ibd(stub_graphs))
    meta = ports["port.ExampleSWC.pOut"][graph.METADATA_KEY]
    assert (meta["interfaceKind"], meta["interfaceWord"]) == ("sender_receiver", "sender-receiver")


@pytest.mark.parametrize(("dest", "glyph"), sorted(GLYPH_BY_DEST.items()))
def test_each_admitted_dest_names_one_glyph(tmp_path: Path, dest: str, glyph: str | None) -> None:
    """Client-server and sender-receiver for the two ``DEST``s that declare them, the bare square for the other six."""
    model_root = stubs.baseline_model_root(tmp_path / "project")
    stubs.interface_stub(model_root, "IStub", "/Elsewhere/IStub", dest)
    assert graph._interface_kinds(str(model_root))[0]["IStub"] == (glyph, "external")


def test_a_dest_check_model_refuses_draws_the_bare_square_and_still_says_external(tmp_path: Path) -> None:
    """The one branch outside the admitted set: no glyph, and the word stays."""
    model_root = stubs.baseline_model_root(tmp_path / "project")
    stubs.interface_stub(model_root, "IStub", "/Elsewhere/IStub", "FOREIGN-SENDER-RECEIVER-INTERFACE")
    assert graph._interface_kinds(str(model_root))[0]["IStub"] == (None, "external")


def test_the_glyph_table_covers_every_admitted_dest() -> None:
    from swcraft_tools.validator import EXTERNAL_INTERFACE_DESTS

    assert set(GLYPH_BY_DEST) == set(EXTERNAL_INTERFACE_DESTS)


def test_an_undeclared_interface_has_neither_a_kind_nor_a_word(tmp_path: Path) -> None:
    model_root = stubs.baseline_model_root(tmp_path / "project")
    kinds, interfaces = graph._interface_kinds(str(model_root))
    assert "INowhere" not in kinds
    assert "INowhere" not in interfaces
    stubs.change(
        model_root,
        stubs.PROVIDER,
        lambda d: d["ports"].append({"name": "pNowhere", "direction": "provide", "interface": "INowhere"}),
    )
    (root,) = [item for item in graph.build_graphs(str(model_root)).compositions if item.name == "Root"]
    meta = _ports(root.ibd)["port.ExampleSWC.pNowhere"][graph.METADATA_KEY]
    assert (meta["interfaceKind"], meta["interfaceWord"]) == (None, None)


# The renderer: the glyph from the kind, the title and the class from the word


def _laid_out(view: str) -> dict[str, Any]:
    return json.loads((LAID_OUT_DIR / f"{view}.json").read_text(encoding="utf-8"))


def _drawn_port(text: str, name: str, owner: str) -> ET.Element:
    (element,) = [
        element
        for element in ET.fromstring(text).iter(f"{NS}g")
        if element.get("data-role") == "port"
        and element.get("data-name") == name
        and element.get("data-owner") == owner
    ]
    return element


def _as_stub(laid_out: dict[str, Any], owner: str, name: str, interface: str) -> dict[str, Any]:
    stubbed = copy.deepcopy(laid_out)
    member = next(child for child in _frame(stubbed)["children"] if child["id"] == f"member.{owner}")
    port = next(port for port in member["ports"] if port["id"] == f"port.{owner}.{name}")
    port[graph.METADATA_KEY]["interface"] = interface
    port[graph.METADATA_KEY]["interfaceWord"] = "external"
    return stubbed


@pytest.mark.parametrize(
    ("owner", "name", "direction"),
    [("Controller", "rDiag", "require"), ("Sensor", "pSpeed", "provide")],
    ids=["client-server", "sender-receiver"],
)
def test_a_stub_port_draws_its_glyph_and_says_external(owner: str, name: str, direction: str) -> None:
    laid_out = _laid_out("ibd-Chassis")
    declared = _drawn_port(svg.render_ibd(laid_out), name, owner)
    stubbed = _drawn_port(svg.render_ibd(_as_stub(laid_out, owner, name, "IElsewhere")), name, owner)
    assert stubbed.get("class") == f"port {direction} external"
    assert stubbed[0].text.startswith(f"{owner}.{name}: {direction} IElsewhere (external)")
    # The square and the glyph are the declared kind's, byte for byte.
    assert ET.tostring(stubbed[1]) == ET.tostring(declared[1])
    assert ET.tostring(stubbed[2]) == ET.tostring(declared[2])


def test_a_stub_port_lists_its_member_names_without_types() -> None:
    """A stub holds member names only; its port's hover lists them as they are and invents no type."""
    laid_out = _as_stub(_laid_out("ibd-Chassis"), "Controller", "rDiag", "IElsewhere")
    element = _drawn_port(svg.render_ibd(laid_out, {"IElsewhere": ("Read", "Write")}), "rDiag", "Controller")
    assert element[0].text.split("\n")[1:3] == ["· Read", "· Write"]
    # A stub listing no member adds no line.
    bare = _drawn_port(svg.render_ibd(laid_out, {"IElsewhere": ()}), "rDiag", "Controller")
    assert bare[0].text == _drawn_port(svg.render_ibd(laid_out), "rDiag", "Controller")[0].text


def test_a_declared_port_still_says_its_kind() -> None:
    element = _drawn_port(svg.render_ibd(_laid_out("ibd-Chassis")), "rDiag", "Controller")
    assert element.get("class") == "port require client-server"
    assert "(client-server)" in element[0].text


# Through the real bridge


@pytest.fixture(scope="module")
def stub_svgs(stub_graphs: graph.DiagramGraphs) -> tuple[str, str]:
    node = requires_node()
    (index,) = [i for i, item in enumerate(stub_graphs.compositions) if item.name == "Root"]
    laid_out = layout.layout(stub_graphs.elk_graphs(), node=node)
    return svg.render_ibd(laid_out[2 * index]), svg.render_overview(laid_out[2 * index + 1])


#: ``(owner as drawn, port name, direction, interface, glyph kind)`` for every stub port.
DRAWN_STUB_PORTS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("ExampleSWC", "pCs", "provide", "IForeignCS", "client_server"),
    ("ExampleSWC", "pSr", "provide", "IForeignSR", "sender_receiver"),
    ("ExampleSWC", "pMode", "provide", "IForeignMode", None),
    ("ExampleConsumerSWC", "rCs", "require", "IForeignCS", "client_server"),
    ("ExampleConsumerSWC", "rSr", "require", "IForeignSR", "sender_receiver"),
    ("ExampleConsumerSWC", "rMode", "require", "IForeignMode", None),
    ("ExampleConsumerSWC", "rAny", "require", "IForeignAny", None),
    ("", "pExtSr", "provide", "IForeignSR", "sender_receiver"),
    ("", "rExtCs", "require", "IForeignCS", "client_server"),
)


def _shape(port: ET.Element) -> str:
    """Which of the four VFB shapes the port's glyph draws, or ``square`` for none.

    Found by its ``g.glyph`` class, never by child index: the port group holds other children.
    """
    glyphs = [child for child in port if child.tag == f"{NS}g" and child.get("class") == "glyph"]
    if not glyphs:
        return "square"
    (glyph,) = glyphs
    shape = glyph[1]
    if shape.tag == f"{NS}circle":
        return "circle"
    d = shape.get("d", "")
    if " A " in d:
        return "half-circle"
    return "arrow" if d.rstrip().endswith("Z") else "fork"


@pytest.mark.parametrize(("owner", "name", "direction", "interface", "glyph"), DRAWN_STUB_PORTS)
def test_the_drawn_stub_port_carries_its_dest_glyph_and_the_word_external(
    stub_svgs: tuple[str, str], owner: str, name: str, direction: str, interface: str, glyph: str | None
) -> None:
    port = _drawn_port(stub_svgs[0], name, owner)
    assert port.get("class") == f"port {direction} external"
    assert f"{name}: {direction} {interface} (external)" in port[0].text
    expected = {
        ("provide", "client_server"): "circle",
        ("require", "client_server"): "half-circle",
        ("provide", "sender_receiver"): "arrow",
        ("require", "sender_receiver"): "fork",
        ("provide", None): "square",
        ("require", None): "square",
    }[(direction, glyph)]
    assert _shape(port) == expected


def test_a_line_the_validator_does_not_judge_is_drawn_dashed_with_its_reason(
    stub_graphs: graph.DiagramGraphs, stub_svgs: tuple[str, str]
) -> None:
    """Each line the extractor marks ``unverified`` is dashed and says why; no other is.

    The reason is the validator's ``not_judged_reason``, the title's second line verbatim.
    """
    reasons = {
        edge[graph.METADATA_KEY]["name"]: edge[graph.METADATA_KEY].get("unverified")
        for edge in _frame(_root_ibd(stub_graphs))["edges"]
    }
    assert any(reasons.values()), "the stub model draws at least one unverified line"
    root = ET.fromstring(stub_svgs[0])
    drawn = {
        str(element.get("data-name")): element
        for element in root.iter(f"{NS}g")
        if element.get("data-source") and element.get("data-target")
    }
    assert set(drawn) == set(reasons)
    for name, reason in reasons.items():
        if reason:
            assert drawn[name].get("stroke-dasharray") == svg.DASH, name
            assert drawn[name][0].text.splitlines()[1] == reason, name
        else:
            assert drawn[name].get("stroke-dasharray") is None, name


def test_the_overview_is_never_dashed(stub_graphs: graph.DiagramGraphs, stub_svgs: tuple[str, str]) -> None:
    """A bundle can mix judged and unjudged connectors, so the overview asks nothing and stays solid."""
    assert any(edge[graph.METADATA_KEY].get("unverified") for edge in _frame(_root_ibd(stub_graphs))["edges"])
    assert 'stroke-dasharray="' not in stub_svgs[1]


def test_the_overviews_connector_list_names_the_stub(stub_svgs: tuple[str, str]) -> None:
    """A bundle row is the IBD line's words and the interface its source end declares -- the stub's name."""
    (listing,) = [
        element
        for element in ET.fromstring(stub_svgs[1]).iter(f"{NS}desc")
        if element.get("class") == svg.CONNECTOR_LIST_CLASS
    ]
    rows = (listing.text or "").split("\n")
    assert "Cs_across_stub: ExampleSWC.pCs -> ExampleConsumerSWC.rCs — IForeignCS" in rows
    assert "Sr_across_stub: ExampleSWC.pSr -> ExampleConsumerSWC.rSr — IForeignSR" in rows
