"""The diagrams draw a component stub's ports and the lines to them.

A component stub (``external: true``) declares its ports by name and direction, with no ``interface``; a port may
carry ``observed_interface``, the importer's one-hop hint. The diagrams draw a glyph only for a declared type, invent
none, and read the hint nowhere:

* a stub's port is the bare square (:data:`graph.PORT_SQUARE` wide) saying ``external component, interface not
  declared``, whatever its hint;
* a declared component's port is drawn from its declared interface alone, a missing or unresolved one as the bare
  square worded *undeclared interface*;
* a hint changes nothing but ``query``'s output: validation, the compatibility pre-flight, the ARXML and the diagrams
  are the same with it, without it and with another;
* a line is dashed exactly where the validator says *not judged*, the edge carrying its reason as ``unverified``;
* no ``None`` is ever written as text (hover text, ``data-interface``, an overview row, a sequence message).

Three halves, as in ``test_external_stubs_diagrams.py``: the graph half and the renderer half without Node, and the
Node half through the real bridge.

What these tests do not reach: whether the picture is right, which only a human reading judges; and a stub-free
model's bytes (``test_stub_ports_byte_identity.py`` and the diagram goldens).
"""

from __future__ import annotations

import copy
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import external_stubs_support as stubs
import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.reader import read_interfaces
from swcraft_tools.schema import ExternalInterface
from swcraft_tools.validator import check_cross_layer_consistency, check_port_compatibility
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.diagrams import graph, layout, svg

LAID_OUT_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams" / "laid-out"
DIAGRAMS_MODEL = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams" / "model"
NS = "{http://www.w3.org/2000/svg}"

STUB = "swc/external/ExternalStub.yaml"
OTHER_STUB = "swc/external/OtherStub.yaml"
BETA = "swc/core/Beta.yaml"

#: A client-server interface the compositions fixture does not hold, written by :func:`_add_cs_interface`.
CS_INTERFACE = "IServe"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    root = stubs.component_stub_ports_model_root(tmp_path / "model")
    _add_cs_interface(root)
    return root


def _add_cs_interface(model_root: Path) -> None:
    stubs.put(
        model_root,
        f"interfaces/{CS_INTERFACE}.yaml",
        {
            "name": CS_INTERFACE,
            "kind": "client_server",
            "operations": [
                {"name": "Serve", "arguments": [{"name": "value", "direction": "in", "implementation_type": "uint8"}]}
            ],
        },
    )


def _sub_ibd(model_root: Path) -> dict[str, Any]:
    (sub,) = [item for item in graph.build_graphs(str(model_root)).compositions if item.name == "Sub"]
    return sub.ibd


def _ports(ibd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    frame = ibd["children"][0]
    ports = {port["id"]: port for port in frame.get("ports", [])}
    for member in frame["children"]:
        ports.update({port["id"]: port for port in member.get("ports", [])})
    return ports


def _port(model_root: Path, port_id: str) -> dict[str, Any]:
    return _ports(_sub_ibd(model_root))[port_id]


def _meta(port: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    meta = port[graph.METADATA_KEY]
    return meta["interface"], meta["interfaceKind"], meta["interfaceWord"]


# ---------------------------------------------------------------------------
# The graph: a stub's port is the bare square, whatever its hint
# ---------------------------------------------------------------------------


class TestAStubsPortIsTheBareSquare:
    """``("", None, STUB_PORT_WORD)`` and :data:`graph.PORT_SQUARE` wide, whatever ``observed_interface`` says."""

    def test_the_word(self) -> None:
        assert graph.STUB_PORT_WORD == "external component, interface not declared"

    @pytest.mark.parametrize(
        ("port", "direction", "hint"),
        [
            pytest.param("pExt", "provide", "ITrigger", id="sender-receiver-hint"),
            pytest.param("rExt", "require", None, id="no-hint"),
            pytest.param("rExt", "require", CS_INTERFACE, id="client-server-hint"),
            pytest.param("pExt", "provide", CS_INTERFACE, id="client-server-hint-on-provide"),
            pytest.param("rExt", "require", "IForeignCS", id="interface-stub-hint"),
            pytest.param("pExt", "provide", "INoLongerDeclared", id="hint-naming-nothing"),
        ],
    )
    def test_whatever_the_hint(self, model_root: Path, port: str, direction: str, hint: str | None) -> None:
        stubs.interface_stub(model_root, "IForeignCS", "/Foreign/IForeignCS", "CLIENT-SERVER-INTERFACE")
        stubs.set_port(model_root, STUB, port, observed_interface=hint)
        drawn = _port(model_root, f"port.Ext.{port}")
        assert _meta(drawn) == ("", None, graph.STUB_PORT_WORD)
        assert drawn["width"] == graph.port_width(direction, None) == graph.PORT_SQUARE
        # The anchor is the box's outer end, so a line ends on the square's outer edge.
        expected_anchor = "(0,7)" if direction == "require" else f"({graph.PORT_SQUARE},7)"
        assert drawn["layoutOptions"]["elk.port.anchor"] == expected_anchor

    def test_the_second_stubs_bare_port(self, model_root: Path) -> None:
        assert _meta(_port(model_root, "port.Ext2.rOther")) == ("", None, graph.STUB_PORT_WORD)


# ---------------------------------------------------------------------------
# Owner-sensitive: a declared owner never takes a hint
# ---------------------------------------------------------------------------


class TestADeclaredOwnerNeverTakesAHint:
    def test_a_declared_client_server_interface_with_a_sender_receiver_hint_keeps_client_server(
        self, model_root: Path
    ) -> None:
        stubs.set_port(model_root, BETA, "pInner", interface=CS_INTERFACE, observed_interface="ITrigger")
        port = _port(model_root, "port.Alpha.pInner")
        assert _meta(port) == (CS_INTERFACE, "client_server", "client-server")

    def test_a_missing_interface_with_a_valid_hint_is_drawn_undeclared_and_keeps_its_finding(
        self, model_root: Path
    ) -> None:
        stubs.set_port(model_root, BETA, "pInner", interface=None, observed_interface="ITrigger")
        port = _port(model_root, "port.Alpha.pInner")
        assert _meta(port) == ("", None, None)
        assert "port.undeclared-interface" in stubs.rules(model_root)

    def test_an_unresolved_interface_with_a_valid_hint_keeps_the_unresolved_name(self, model_root: Path) -> None:
        stubs.set_port(model_root, BETA, "pInner", interface="INowhere", observed_interface="ITrigger")
        port = _port(model_root, "port.Alpha.pInner")
        assert _meta(port) == ("INowhere", None, None)
        assert "port.undeclared-interface" in stubs.rules(model_root)

    def test_an_external_owner_converted_to_declared_leaves_its_hints_inert(self, model_root: Path) -> None:
        def declare(document: dict[str, Any]) -> None:
            document["external"] = False
            document.pop("external_ref")
            document.pop("external_dest")

        stubs.change(model_root, STUB, declare)
        ports = _ports(_sub_ibd(model_root))
        # pExt still carries ``observed_interface: ITrigger`` in the file; nothing reads it now.
        assert _meta(ports["port.Ext.pExt"]) == ("", None, None)
        assert _meta(ports["port.Ext.rExt"]) == ("", None, None)

    def test_the_premise_the_converted_file_still_carries_the_hint(self, model_root: Path) -> None:
        text = (model_root / STUB).read_text(encoding="utf-8")
        assert "observed_interface: ITrigger" in text


def _records(model_root: Path) -> list[tuple[str, str, str]]:
    return sorted(
        (record.rule, str(record.subject), record.message)
        for record in check_cross_layer_consistency(str(model_root)).records
    )


class TestAHintChangesNothingButQuery:
    """Validation, the compatibility pre-flight, the ARXML and the diagrams are the same whatever the hints say.

    Only ``query`` reads the hint. The non-vacuity witness is that the YAML text really changed.
    """

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param(((STUB, "pExt", None),), id="stub-hint-removed"),
            pytest.param(((STUB, "pExt", CS_INTERFACE),), id="stub-hint-changed"),
            pytest.param(((STUB, "rExt", "ITrigger"),), id="stub-hint-added"),
            pytest.param(((BETA, "pInner", CS_INTERFACE),), id="declared-owner-hint-added"),
        ],
    )
    def test_validation_compatibility_and_arxml(self, model_root: Path, tmp_path: Path, changes: tuple) -> None:
        pairs = (("ExternalStub", "pExt", "Beta", "rFromExt"), ("Beta", "pInner", "ExternalStub", "rExt"))
        before = (
            _records(model_root),
            [check_port_compatibility(str(model_root), *pair) for pair in pairs],
            _arxml(model_root, tmp_path / "before"),
        )
        picture_before = graph.build_graphs(str(model_root))
        texts_before = {relative: (model_root / relative).read_text(encoding="utf-8") for relative, _, _ in changes}
        for relative, port, hint in changes:
            stubs.set_port(model_root, relative, port, observed_interface=hint)
        after = (
            _records(model_root),
            [check_port_compatibility(str(model_root), *pair) for pair in pairs],
            _arxml(model_root, tmp_path / "after"),
        )
        assert after == before
        assert graph.build_graphs(str(model_root)) == picture_before

        for relative, text in texts_before.items():
            assert (model_root / relative).read_text(encoding="utf-8") != text


def _arxml(model_root: Path, directory: Path) -> bytes:
    output = directory / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_bytes()


# ---------------------------------------------------------------------------
# No ``None`` as text: the bundle rows and the sequence messages
# ---------------------------------------------------------------------------


class TestNoNoneIsWritten:
    def test_a_bundle_row_from_a_stubs_port_names_no_interface(self, model_root: Path) -> None:
        """A row names the interface its source end **declares**; a stub's port declares none."""
        (sub,) = [item for item in graph.build_graphs(str(model_root)).compositions if item.name == "Sub"]
        rows = {
            row["name"]: row["interface"]
            for edge in sub.overview["children"][0]["edges"]
            for row in edge[graph.METADATA_KEY]["connectors"]
        }
        assert rows["ext_link"] == ""
        assert rows["stub_link"] == ""

    def test_a_sequence_message_over_a_port_without_an_interface(self, tmp_path: Path) -> None:
        model_copy = tmp_path / "model"
        shutil.copytree(DIAGRAMS_MODEL, model_copy)
        stubs.set_port(model_copy, "swc/chassis/Sensor.yaml", "pSpeed", interface=None)
        (sequence,) = graph.build_graphs(str(model_copy)).sequences
        sensor = sequence.lifelines[sequence.labels.index("Sensor")]
        data = [message.label for message in sequence.messages if message.source == sensor]
        assert data == ["pSpeed -> rSpeed"]


# ---------------------------------------------------------------------------
# The renderer: the word in the hover text and the class, never ``None``
# ---------------------------------------------------------------------------


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


def _restyled(laid_out: dict[str, Any], owner: str, name: str, **meta: Any) -> dict[str, Any]:
    changed = copy.deepcopy(laid_out)
    member = next(child for child in changed["children"][0]["children"] if child["id"] == f"member.{owner}")
    port = next(port for port in member["ports"] if port["id"] == f"port.{owner}.{name}")
    port[graph.METADATA_KEY].update(meta)
    return changed


def _glyphs(element: ET.Element) -> list[ET.Element]:
    return [child for child in element if child.tag == f"{NS}g" and child.get("class") == "glyph"]


class TestTheRenderer:
    def test_a_stubs_port_is_the_square_alone_with_its_word_and_never_none(self) -> None:
        laid_out = _restyled(
            _laid_out("ibd-Chassis"),
            "Sensor",
            "pSpeed",
            interface="",
            interfaceKind=None,
            interfaceWord=graph.STUB_PORT_WORD,
        )
        text = svg.render_ibd(laid_out)
        element = _drawn_port(text, "pSpeed", "Sensor")
        assert element.get("class") == "port provide external-component-interface-not-declared"
        assert element.get("data-interface") == ""
        assert element[0].text.startswith("Sensor.pSpeed: provide (external component, interface not declared)")
        assert _glyphs(element) == []
        assert element[1].get("class") == "port-box"
        assert [child.get("class") for child in element[1:]] == ["port-box", svg.PORT_HIT_CLASS]
        assert "None" not in text

    def test_a_stubs_port_lists_no_interface_line(self) -> None:
        """A component stub's port declares no interface, so it lists none, whatever the map holds."""
        laid_out = _restyled(
            _laid_out("ibd-Chassis"),
            "Sensor",
            "pSpeed",
            interface="",
            interfaceKind=None,
            interfaceWord=graph.STUB_PORT_WORD,
        )
        contents = {"ISpeed": ("speed: uint8",), "": ("never: drawn",)}
        element = _drawn_port(svg.render_ibd(laid_out, contents), "pSpeed", "Sensor")
        assert element[0].text == _drawn_port(svg.render_ibd(laid_out), "pSpeed", "Sensor")[0].text
        assert "·" not in element[0].text

    def test_a_declared_port_is_drawn_as_before(self) -> None:
        element = _drawn_port(svg.render_ibd(_laid_out("ibd-Chassis")), "pSpeed", "Sensor")
        assert element.get("class") == "port provide sender-receiver"
        assert element[0].text.startswith("Sensor.pSpeed: provide ISpeed (sender-receiver)")


# ---------------------------------------------------------------------------
# Through the real bridge: the stubs' ports and the lines to them
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wired_svgs(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    node = requires_node()
    model_root = stubs.component_stub_ports_model_root(tmp_path_factory.mktemp("stub-ports") / "model")
    graphs = graph.build_graphs(str(model_root))
    (index,) = [i for i, item in enumerate(graphs.compositions) if item.name == "Sub"]
    laid_out = layout.layout(graphs.elk_graphs(), node=node)
    return svg.render_ibd(laid_out[2 * index]), svg.render_overview(laid_out[2 * index + 1])


STUB_PORT_CLASS = "external-component-interface-not-declared"


@pytest.mark.parametrize(
    ("owner", "name", "css", "title"),
    [
        (
            "Ext",
            "pExt",
            f"port provide {STUB_PORT_CLASS}",
            "Ext.pExt: provide (external component, interface not declared)",
        ),
        (
            "Ext",
            "rExt",
            f"port require {STUB_PORT_CLASS}",
            "Ext.rExt: require (external component, interface not declared)",
        ),
        (
            "Ext2",
            "rOther",
            f"port require {STUB_PORT_CLASS}",
            "Ext2.rOther: require (external component, interface not declared)",
        ),
    ],
)
def test_the_stubs_ports_are_drawn(wired_svgs: tuple[str, str], owner: str, name: str, css: str, title: str) -> None:
    """Each stub port is the square alone: no ``g.glyph``, whatever its hint (``pExt`` carries one)."""
    port = _drawn_port(wired_svgs[0], name, owner)
    assert port.get("class") == css
    assert port[0].text.startswith(title)
    assert _glyphs(port) == []
    assert port[-1].get("class") == svg.PORT_HIT_CLASS, "the bare square is rested on whole too"


def _edge_ends(text: str) -> dict[str, tuple[str, str]]:
    """Every drawn edge's name -> its two ends as ``owner.port``, read through the port ids."""
    root = ET.fromstring(text)
    names = {
        element.get("id"): f"{element.get('data-owner') or '(frame)'}.{element.get('data-name')}"
        for element in root.iter(f"{NS}g")
        if element.get("data-role") == "port"
    }
    return {
        str(element.get("data-name")): (names[element.get("data-source")], names[element.get("data-target")])
        for element in root.iter(f"{NS}g")
        if element.get("data-source") and element.get("data-target")
    }


def test_the_lines_to_the_stubs_are_drawn(wired_svgs: tuple[str, str]) -> None:
    ends = _edge_ends(wired_svgs[0])
    assert ends["ext_link"] == ("Ext.pExt", "Alpha.rFromExt")
    assert ends["stub_link"] == ("Ext.pExt", "Ext2.rOther")
    assert ends["Sub_rExtIn_Ext"] == ("(frame).rExtIn", "Ext.rExt")


@pytest.fixture(scope="module")
def wired_reasons(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str | None]:
    """Each of ``Sub``'s lines -> the ``unverified`` reason the extractor gave it, or ``None``."""
    model_root = stubs.component_stub_ports_model_root(tmp_path_factory.mktemp("stub-reasons") / "model")
    edges = _edges(_ibd(model_root, "Sub"))
    return {name: meta.get("unverified") for name, meta in edges.items()}


def test_the_lines_to_the_stubs_are_drawn_dashed_with_the_validators_reason(
    wired_svgs: tuple[str, str], wired_reasons: dict[str, str | None]
) -> None:
    """Every line the extractor marks ``unverified`` is dashed on its group and says why; no other is.

    The three lines to the component stubs are all case (1), so all three are dashed; the
    reason is the title's second line, verbatim. The port squares stay solid.
    """
    assert {name for name, reason in wired_reasons.items() if reason} >= {"ext_link", "stub_link", "Sub_rExtIn_Ext"}
    root = ET.fromstring(wired_svgs[0])
    drawn = {
        str(element.get("data-name")): element
        for element in root.iter(f"{NS}g")
        if element.get("data-source") and element.get("data-target")
    }
    assert set(drawn) == set(wired_reasons)
    for name, reason in wired_reasons.items():
        edge = drawn[name]
        if reason:
            assert edge.get("stroke-dasharray") == svg.DASH, name
            assert edge.find(f"{NS}path").get("stroke-dasharray") is None, name
            assert edge[0].text.splitlines()[1] == reason, name
        else:
            assert edge.get("stroke-dasharray") is None, name
    for element in root.iter(f"{NS}g"):
        if element.get("data-role") == "port":
            assert element[-1].get("class") == svg.PORT_HIT_CLASS
            assert all(child.get("stroke-dasharray") is None for child in element)


def test_the_overview_of_the_wired_fixture_is_never_dashed(
    wired_svgs: tuple[str, str], wired_reasons: dict[str, str | None]
) -> None:
    """The IBD's lines to the stubs are dashed; the overview's bundles over them are not."""
    assert any(wired_reasons.values())
    assert 'stroke-dasharray="' not in wired_svgs[1]


def test_no_view_of_the_wired_fixture_writes_none(wired_svgs: tuple[str, str]) -> None:
    for text in wired_svgs:
        assert "None" not in text


# ---------------------------------------------------------------------------
# Dashed exactly where the validator says *not judged*
# ---------------------------------------------------------------------------


def _edges(ibd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {edge[graph.METADATA_KEY]["name"]: edge[graph.METADATA_KEY] for edge in ibd["children"][0]["edges"]}


def _ibd(model_root: Path, composition: str) -> dict[str, Any]:
    (item,) = [item for item in graph.build_graphs(str(model_root)).compositions if item.name == composition]
    return item.ibd


def _reason(model_root: Path, provider: tuple[str, str], requirer: tuple[str, str]) -> str:
    """The pre-flight's own *not judged* reason for the pair, asserted to be one."""
    result = check_port_compatibility(str(model_root), *provider, *requirer)
    assert result.compatible
    assert result.reason.startswith("not judged: ")
    return result.reason


class TestAComponentStubsLinesAreUnverified:
    """Case (1): an end on an external component stub's port, the compositions fixture's ``Sub``."""

    def test_a_connector_with_one_stub_end(self, model_root: Path) -> None:
        edges = _edges(_ibd(model_root, "Sub"))
        assert edges["ext_link"]["unverified"] == _reason(model_root, ("ExternalStub", "pExt"), ("Beta", "rFromExt"))

    def test_a_connector_between_two_stubs_names_the_first(self, model_root: Path) -> None:
        edges = _edges(_ibd(model_root, "Sub"))
        reason = _reason(model_root, ("ExternalStub", "pExt"), ("OtherStub", "rOther"))
        assert edges["stub_link"]["unverified"] == reason
        assert "'ExternalStub' port 'pExt'" in reason

    def test_a_delegation_onto_a_stubs_port(self, model_root: Path) -> None:
        edges = _edges(_ibd(model_root, "Sub"))
        assert edges["Sub_rExtIn_Ext"]["unverified"] == (
            "not judged: external component 'ExternalStub' port 'rExt' is a port of a stub whose type is"
            " outside the project; it declares no interface, so only the direction can be compared."
        )

    def test_a_reversed_connector_with_a_stub_end_stays_solid(self, model_root: Path) -> None:
        """Written require -> provide: ``check-model`` refuses that orientation, so it is a finding."""
        stubs.change(
            model_root,
            "connectors/ext_link.yaml",
            lambda d: d.update(
                {"from": {"swc": "Beta", "port": "rFromExt"}, "to": {"swc": "ExternalStub", "port": "pExt"}}
            ),
        )
        ibd = _ibd(model_root, "Sub")
        edge = next(edge for edge in ibd["children"][0]["edges"] if edge["id"] == "connector.ext_link")
        assert "unverified" not in edge[graph.METADATA_KEY]
        # Its drawing orientation is unchanged: provide to require.
        assert edge["sources"] == ["port.Ext.pExt"]
        assert edge["targets"] == ["port.Alpha.rFromExt"]

    def test_a_connector_whose_directions_disagree_stays_solid(self, model_root: Path) -> None:
        stubs.change(
            model_root,
            "connectors/stub_link.yaml",
            lambda d: d.update({"from": {"swc": "ExternalStub", "port": "rExt"}}),
        )
        assert "unverified" not in _edges(_ibd(model_root, "Sub"))["stub_link"]

    def test_a_delegation_whose_directions_disagree_stays_solid(self, model_root: Path) -> None:
        def flip(document: dict[str, Any]) -> None:
            document["delegations"][-1]["member_port"] = "pExt"

        stubs.change(model_root, "compositions/Sub.yaml", flip)
        assert "unverified" not in _edges(_ibd(model_root, "Sub"))["Sub_rExtIn_Ext"]


PROVIDER_SWC = "ExampleSWC"
CONSUMER_SWC = "ExampleConsumerSWC"


def _stub_reason(name: str) -> str:
    """The validator's *not judged* sentence for an SR interface stub *name* whose kind agrees."""
    return (
        f"not judged: external interface '{name}' is a stub whose content is outside the project; its kind"
        " (SENDER-RECEIVER-INTERFACE) agrees, and nothing beyond an external interface's kind can be compared."
    )


@pytest.fixture
def interface_stub_model(tmp_path: Path) -> Path:
    """The interface stubs model, with a second SR stub and the ports and connectors cases (2)-(4) need.

    Each ``Sr_to_*`` connector leaves ``ExampleSWC.pSr`` (on ``IForeignSR``) for a require port of
    ``ExampleConsumerSWC``: ``rSr2`` on a different SR stub (case 2), ``rSrDeclared`` on the declared
    ``IExample`` (case 3, the mixed-ends port), ``rUnset`` with no interface and ``rNoSuch`` on an
    undeclared interface (case 4); ``Sr_from_unresolved`` is case 4 reversed; ``pCs`` -> ``rSr``
    crosses two differing kinds (a finding, solid). ``Root_pExtSr2`` delegates between two
    different stubs, so its reason tells the inner (required) one from the outer.
    """
    model_root = stubs.external_stubs_model_root(tmp_path / "project")
    stubs.interface_stub(model_root, "IForeignSR2", "/Foreign/PortInterfaces/IForeignSR2", "SENDER-RECEIVER-INTERFACE")
    stubs.change(
        model_root,
        stubs.CONSUMER,
        lambda d: d["ports"].extend(
            [
                {"name": "rSr2", "direction": "require", "interface": "IForeignSR2"},
                {"name": "rSrDeclared", "direction": "require", "interface": "IExample"},
                {"name": "rUnset", "direction": "require"},
                {"name": "rNoSuch", "direction": "require", "interface": "INoSuch"},
            ]
        ),
    )
    stubs.change(
        model_root,
        stubs.PROVIDER,
        lambda d: d["ports"].append({"name": "pNoSuch", "direction": "provide", "interface": "INoSuch"}),
    )

    def second_stub_delegation(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "pExtSr2", "direction": "provide", "interface": "IForeignSR2"})
        document["delegations"].append(
            {"name": "Root_pExtSr2", "port": "pExtSr2", "member": PROVIDER_SWC, "member_port": "pSr"}
        )

    stubs.change(model_root, stubs.ROOT, second_stub_delegation)
    for name, source, target in (
        ("Sr_to_other_stub", "pSr", "rSr2"),
        ("Sr_to_declared", "pSr", "rSrDeclared"),
        ("Sr_to_unset", "pSr", "rUnset"),
        ("Sr_to_unresolved", "pSr", "rNoSuch"),
        ("Sr_from_unresolved", "pNoSuch", "rSr"),
        ("Cs_to_sr", "pCs", "rSr"),
    ):
        stubs.put(
            model_root,
            f"connectors/{name}.yaml",
            {"name": name, "from": {"swc": PROVIDER_SWC, "port": source}, "to": {"swc": CONSUMER_SWC, "port": target}},
        )
    return model_root


class TestAnInterfaceStubsLinesAreUnverified:
    """Cases (2), (3) and (4), and the solid pairs, on the interface stubs model's root."""

    @pytest.mark.parametrize(
        ("connector", "source", "target", "stub"),
        [
            # Two different stubs: the required (``to``) end's stub is named first.
            pytest.param("Sr_to_other_stub", "pSr", "rSr2", "IForeignSR2", id="two-different-stubs"),
            pytest.param("Sr_to_declared", "pSr", "rSrDeclared", "IForeignSR", id="declared-against-a-stub"),
            pytest.param("Sr_to_unset", "pSr", "rUnset", "IForeignSR", id="unset-against-a-stub"),
            pytest.param("Sr_to_unresolved", "pSr", "rNoSuch", "IForeignSR", id="unresolved-against-a-stub"),
            pytest.param("Sr_from_unresolved", "pNoSuch", "rSr", "IForeignSR", id="a-stub-against-unresolved"),
        ],
    )
    def test_a_connector_the_validator_does_not_judge(
        self, interface_stub_model: Path, connector: str, source: str, target: str, stub: str
    ) -> None:
        edges = _edges(_ibd(interface_stub_model, "Root"))
        reason = _reason(interface_stub_model, (PROVIDER_SWC, source), (CONSUMER_SWC, target))
        assert edges[connector]["unverified"] == reason == _stub_reason(stub)

    def test_a_delegation_from_a_declared_inner_port_to_a_stub_outer_one(self, interface_stub_model: Path) -> None:
        """``Root_pExtSr``: the inner ``pOut`` on ``IExample``, the outer ``pExtSr`` on the SR stub."""
        edges = _edges(_ibd(interface_stub_model, "Root"))
        assert edges["Root_pExtSr"]["unverified"] == _stub_reason("IForeignSR")

    def test_a_delegation_between_two_stubs_names_the_inner_required_one(self, interface_stub_model: Path) -> None:
        """``Root_pExtSr2``: the inner ``pSr`` on ``IForeignSR`` is the required role, the outer on ``IForeignSR2``.

        The validator names the first stub of ``(required, provided)``, so a delegation whose
        roles were swapped would name ``IForeignSR2``.
        """
        edges = _edges(_ibd(interface_stub_model, "Root"))
        assert edges["Root_pExtSr2"]["unverified"] == _stub_reason("IForeignSR")

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("Sr_across_stub", id="one-stub-on-both-ends"),
            pytest.param("Cs_across_stub", id="one-cs-stub-on-both-ends"),
            pytest.param("Root_rExtCs", id="a-delegation-on-one-stub"),
            pytest.param("Cs_to_sr", id="a-kind-mismatch-is-a-finding"),
        ],
    )
    def test_a_line_the_validator_judges_stays_solid(self, interface_stub_model: Path, name: str) -> None:
        assert "unverified" not in _edges(_ibd(interface_stub_model, "Root"))[name]

    def test_declared_interfaces_stay_solid(self, interface_stub_model: Path) -> None:
        edges = _edges(_ibd(interface_stub_model, "Root"))
        declared = [
            name for name in edges if "stub" not in name.lower() and not name.startswith(("Sr_", "Cs_", "Root_"))
        ]
        assert declared
        for name in declared:
            assert "unverified" not in edges[name], name

    def test_a_kind_mismatch_is_a_finding_of_the_pre_flight(self, interface_stub_model: Path) -> None:
        result = check_port_compatibility(str(interface_stub_model), PROVIDER_SWC, "pCs", CONSUMER_SWC, "rSr")
        assert not result.compatible

    def test_mixed_ends_one_port_a_solid_and_a_dashed_line(self, interface_stub_model: Path) -> None:
        ibd = _ibd(interface_stub_model, "Root")
        from_p_sr = {
            edge[graph.METADATA_KEY]["name"]: "unverified" in edge[graph.METADATA_KEY]
            for edge in ibd["children"][0]["edges"]
            if edge["sources"] == ["port.ExampleSWC.pSr"]
        }
        assert from_p_sr["Sr_across_stub"] is False
        assert from_p_sr["Sr_to_declared"] is True
        # The port draws its own declared stub's glyph, whatever its lines are.
        port = _ports(ibd)["port.ExampleSWC.pSr"][graph.METADATA_KEY]
        assert (port["interfaceKind"], port["interfaceWord"]) == ("sender_receiver", "external")


def test_a_duplicated_interface_name_takes_glyph_and_judgement_from_the_first_file(tmp_path: Path) -> None:
    """``interface.duplicate-name`` is refused by ``check-model``; the diagrams still read one declaration.

    One name declared twice, once as a declared SR interface and once as an SR stub: the
    port's word and the line's reason both follow the first declaration the reader returns.
    """
    model_root = stubs.external_stubs_model_root(tmp_path / "project")
    stubs.put(
        model_root,
        "interfaces/IDup.yaml",
        {
            "name": "IDup",
            "kind": "external_interface",
            "external_ref": "/Foreign/PortInterfaces/IDup",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
        },
    )
    stubs.put(
        model_root,
        "interfaces/IDup~1.yaml",
        {
            "name": "IDup",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint8"}],
        },
    )
    stubs.change(
        model_root,
        stubs.CONSUMER,
        lambda d: d["ports"].append({"name": "rDup", "direction": "require", "interface": "IDup"}),
    )
    stubs.put(
        model_root,
        "connectors/Sr_to_dup.yaml",
        {
            "name": "Sr_to_dup",
            "from": {"swc": PROVIDER_SWC, "port": "pSr"},
            "to": {"swc": CONSUMER_SWC, "port": "rDup"},
        },
    )
    first = next(interface for interface in read_interfaces(str(model_root)) if interface.name == "IDup")
    ibd = _ibd(model_root, "Root")
    word = _ports(ibd)["port.ExampleConsumerSWC.rDup"][graph.METADATA_KEY]["interfaceWord"]
    reason = _edges(ibd)["Sr_to_dup"]["unverified"]
    if isinstance(first, ExternalInterface):
        assert word == "external"
        assert reason.startswith("not judged: external interface 'IDup'")
    else:
        assert word == "sender-receiver"
        assert reason.startswith("not judged: external interface 'IForeignSR'")
