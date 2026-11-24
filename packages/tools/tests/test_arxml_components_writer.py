"""The software-component emitters, each tested directly against a named slice of a golden.

The rules held here:

* ``TMT_<runnable>`` / ``IT_<runnable>`` event names, and the de-duplication counter that renames the
  *first* colliding event retroactively;
* ``<swc>_InternalBehavior``, **nested** inside the component's ``INTERNAL-BEHAVIORS``;
* ``<swc>_Implementation``, a package-level sibling **interleaved** right after each SWC;
* ``PORTS`` provide-first in declaration order; no port API option is written;
* the four default com-specs, ``SERVER-COM-SPEC``/``QUEUE-LENGTH`` (in **neither** golden) included;
* ``SUPPORTS-MULTIPLE-INSTANTIATION`` as the behavior's last child;
* ``SYMBOL`` mirroring the runnable name unless the runnable states one;
* exact ``PERIOD`` and ``MINIMUM-START-INTERVAL``: a sub-millisecond value is written as itself.

The ``component_shapes`` fixture generates a throwaway model for the shapes the committed goldens leave
unreached, rather than editing a committed fixture, which would move a golden. A
``*_matches_the_generator_path`` test compares the emitter against the dispatcher's use of it, which is
not an independent oracle; the three byte goldens and ``TestSemanticRoundTrip`` are.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    REPO_ROOT,
    golden_slice,
    make_empty_model_root,
    read_golden,
    write_model_element,
)
from lxml import etree
from swcraft_tools.capability_markers import SDG_GID
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.schema import CSInterface, Port, Runnable, SRInterface, SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    _ACCESS_CONTAINER_ORDER,
    _check_swc_type,
    _emit_swc,
    _emit_swcs,
    _unique_event_names,
    build_workspace,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, ArxmlDocument, serialize_document, serialize_fragment

#: The type every ad-hoc (non-golden) interface in this module references: a value type over a
#: ``{name}_base`` base type, which every model root here declares beside it.
_UINT8_BASE = {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
_UINT8 = {"name": "uint8", "kind": "value_type", "base": "uint8_base"}

#: A sender-receiver interface with **two** data elements. Neither golden has
#: one, so without it the com-spec cardinality (one per data element, in
#: interface order) would be unpinned.
_MULTI_SR: dict = {
    "name": "IMulti",
    "kind": "sender_receiver",
    "data_elements": [{"name": "a", "implementation_type": "uint8"}, {"name": "b", "implementation_type": "uint8"}],
}

#: A sender-receiver interface whose **first** data element carries an ``init`` and whose second does
#: not: the ``INIT-VALUE`` branch is per data element, so an emitter writing the reference once per
#: com-spec *list* would pass a single-element interface.
_INIT_SR: dict = {
    "name": "IInit",
    "kind": "sender_receiver",
    "data_elements": [
        {"name": "valued", "implementation_type": "uint8", "init": 7},
        {"name": "bare", "implementation_type": "uint8"},
    ],
}

#: A client-server interface with two operations, so both the client and the
#: server com-spec cardinality are pinned.
_MULTI_CS: dict = {
    "name": "IMultiCs",
    "kind": "client_server",
    "operations": [
        {"name": "Op1", "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8"}]},
        {"name": "Op2", "arguments": []},
    ],
}

#: Every SWC the ``component_shapes`` fixture materializes. Each one exists for a
#: shape neither committed golden can reach.
_SHAPE_SWCS: list[dict] = [
    # A *provided* client-server port — SERVER-COM-SPEC + QUEUE-LENGTH, the two
    # tags in neither golden (golden README, "Reachable but not in the goldens").
    {
        "name": "CsServer",
        "type": "application",
        "ports": [{"name": "pCs", "direction": "provide", "interface": "IMultiCs"}],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    },
    # The required twin, for CLIENT-COM-SPEC's cardinality.
    {
        "name": "CsClient",
        "type": "application",
        "ports": [{"name": "rCs", "direction": "require", "interface": "IMultiCs"}],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    },
    # A require port declared *before* a provide port, so provide-first order is
    # tested against a declaration order that disagrees with it.
    {
        "name": "MixedOrder",
        "type": "application",
        "ports": [
            {"name": "Zebra", "direction": "require", "interface": "IMulti"},
            {"name": "apple", "direction": "provide", "interface": "IMulti"},
            {"name": "Mango", "direction": "require", "interface": "IMulti"},
        ],
        "runnables": [{"name": "Cyclic", "min_start_interval": 0.005, "events": [{"type": "timing", "period": 0.02}]}],
        "handle_termination_and_restart": "no_support",
    },
    # Two runnables, so the EVENTS list's interleaving (per runnable, in each
    # runnable's own event order) is pinned.
    {
        "name": "TwoRunnables",
        "type": "application",
        "ports": [],
        "runnables": [
            {"name": "R1", "events": [{"type": "timing", "period": 0.01}]},
            {"name": "R2", "events": [{"type": "init"}, {"type": "timing", "period": 0.03}]},
        ],
        "handle_termination_and_restart": "no_support",
    },
    # Two timing and two init events on one runnable. The MCP/writer surface
    # rejects this (at most one of each per runnable), but `generate_arxml()`
    # reads YAML directly and has no such guard, so hand-edited model files reach
    # the event-name de-duplication counter.
    {
        "name": "DupEvents",
        "type": "application",
        "ports": [],
        "runnables": [
            {
                "name": "R",
                "events": [
                    {"type": "timing", "period": 0.01},
                    {"type": "timing", "period": 0.02},
                    {"type": "init"},
                    {"type": "init"},
                ],
            }
        ],
        "handle_termination_and_restart": "no_support",
    },
    # Ports but no runnables: EVENTS and RUNNABLES are not emitted, and no
    # PORT-API-OPTIONS either.
    {
        "name": "PortsOnly",
        "type": "application",
        "ports": [{"name": "pOnly", "direction": "provide", "interface": "IMulti"}],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    },
    # Both directions on the interface that carries a value, so the sender and
    # the receiver com-spec each reach the INIT-VALUE branch.
    {
        "name": "InitPorts",
        "type": "application",
        "ports": [
            {"name": "pInit", "direction": "provide", "interface": "IInit"},
            {"name": "rInit", "direction": "require", "interface": "IInit"},
        ],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    },
    # Neither ports nor runnables: an empty PORTS is kept and the three behavior lists are
    # omitted.
    {
        "name": "Bare",
        "type": "application",
        "ports": [],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    },
    # A sub-millisecond period and interval, written exactly: a millisecond grid would write
    # them as 0 and 0.012.
    {
        "name": "SubMilli",
        "type": "application",
        "ports": [],
        "runnables": [{"name": "Fast", "min_start_interval": 0.0004, "events": [{"type": "timing", "period": 0.0125}]}],
        "handle_termination_and_restart": "no_support",
    },
]


def _write_model(model_root, subdir: str, data: dict) -> None:
    """Write one ``<subdir>/<name>.yaml`` element file into a materialized model root."""
    path = model_root / subdir / f"{data['name']}.yaml"
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _model_interfaces(model_root: str) -> dict:
    """A model root's interfaces, keyed by name (the registry shape the emitter takes)."""
    return {iface.name: iface for iface in read_interfaces(model_root)}


def _model_swcs(model_root: str) -> dict:
    """A model root's SWCs, keyed by name."""
    return {swc.name: swc for swc in read_swc_model(model_root)}


def _emit(swc, interfaces) -> tuple[str, str]:
    """Emit one SWC into a fresh document; return its and its implementation's fragments."""
    package = ArxmlDocument().component_types
    node, implementation = _emit_swc(package, swc, interfaces)
    return serialize_fragment(node), serialize_fragment(implementation)


def _package_short_names(package) -> list[str]:
    """The ``SHORT-NAME``s of a package's elements, in emission order."""
    return [element.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text for element in package.elements]


def _swc_names(golden: str) -> list[str]:
    """Every SWC name in a golden's model, in read order."""
    return [swc.name for swc in read_swc_model(str(GOLDEN_MODEL_ROOTS[golden]))]


#: Every SWC in each golden's model, as ``(golden, name)`` pairs.
GOLDEN_SWCS = [(golden, name) for golden in GOLDEN_MODEL_ROOTS for name in _swc_names(golden)]


@pytest.fixture(scope="module")
def component_shapes(tmp_path_factory) -> tuple[str, dict, dict]:
    """``_SHAPE_SWCS`` through ``generate_arxml()``: the ARXML text, and the SWCs and interfaces it was made from."""
    tmp_path = tmp_path_factory.mktemp("component_shapes")
    model_root = make_empty_model_root(tmp_path)
    write_model_element(model_root, _UINT8_BASE)
    _write_model(model_root, "data_types/implementation", _UINT8)
    for iface in (_MULTI_SR, _MULTI_CS, _INIT_SR):
        _write_model(model_root, "interfaces", iface)
    for swc in _SHAPE_SWCS:
        _write_model(model_root, "swc", swc)
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output))
    return (
        output.read_text(encoding="utf-8"),
        _model_swcs(str(model_root)),
        _model_interfaces(str(model_root)),
    )


class TestComponentEmitter:
    """``APPLICATION-SW-COMPONENT-TYPE`` as a whole, against both goldens."""

    @pytest.mark.parametrize(("golden", "name"), GOLDEN_SWCS)
    def test_component_matches_the_golden_slice(self, golden, name):
        model_root = str(GOLDEN_MODEL_ROOTS[golden])
        emitted, _implementation = _emit(_model_swcs(model_root)[name], _model_interfaces(model_root))
        assert emitted == golden_slice(read_golden(golden), name, tag="APPLICATION-SW-COMPONENT-TYPE")

    @pytest.mark.parametrize("name", [swc["name"] for swc in _SHAPE_SWCS])
    def test_component_matches_the_generator_path_for_every_shape(self, component_shapes, name):
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs[name], interfaces)
        assert emitted == golden_slice(text, name, tag="APPLICATION-SW-COMPONENT-TYPE")

    def test_ports_are_provide_first_regardless_of_declaration_order(self, component_shapes):
        # MixedOrder declares `Zebra` (require) first, so an
        # emitter preserving declaration order fails here.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["MixedOrder"], interfaces)
        prototypes = [line.strip() for line in emitted.splitlines() if line.strip().endswith("-PORT-PROTOTYPE>")]
        assert prototypes[0] == "<P-PORT-PROTOTYPE>"
        assert emitted.index("<SHORT-NAME>apple</SHORT-NAME>") < emitted.index("<SHORT-NAME>Zebra</SHORT-NAME>")

    def test_declaration_order_is_preserved_inside_each_direction_group(self, component_shapes):
        # Within the require group, `Zebra` was
        # declared before `Mango` and stays there (it is *not* sorted).
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["MixedOrder"], interfaces)
        assert emitted.index("<SHORT-NAME>Zebra</SHORT-NAME>") < emitted.index("<SHORT-NAME>Mango</SHORT-NAME>")

    def test_a_port_less_swc_keeps_an_empty_ports_element(self, component_shapes):
        # The opposite of the rule for an empty package's ELEMENTS, in the serializer's
        # two-line empty form.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["Bare"], interfaces)
        assert "  <PORTS>\n  </PORTS>\n" in emitted

    def test_a_description_is_written_as_its_desc(self, component_shapes):
        # Written exactly, directly after the SHORT-NAME.
        _text, swcs, interfaces = component_shapes
        swc = swcs["Bare"].model_copy(update={"description": "a component description"})
        emitted, _implementation = _emit(swc, interfaces)
        assert (
            '<SHORT-NAME>Bare</SHORT-NAME>\n  <DESC>\n    <L-2 L="FOR-ALL">a component description</L-2>\n  </DESC>'
            in emitted
        )

    def test_an_unknown_port_interface_raises(self, component_shapes):
        _text, _swcs, interfaces = component_shapes
        swc = SWComponent(name="Ghost", ports=[Port(name="pGhost", direction="provide", interface="INoSuchThing")])
        with pytest.raises(ValueError, match="INoSuchThing"):
            _emit_swc(ArxmlDocument().component_types, swc, interfaces)

    def test_a_rejected_component_leaves_nothing_behind_in_the_package(self, component_shapes):
        # The *second* port is the bad one, so a writer validating as it went
        # would already have emitted the component and its first port.
        _text, _swcs, interfaces = component_shapes
        document = ArxmlDocument()
        swc = SWComponent(
            name="Ghost",
            ports=[
                Port(name="pOk", direction="provide", interface="IMulti"),
                Port(name="rBad", direction="require", interface="INoSuchThing"),
            ],
        )
        with pytest.raises(ValueError):
            _emit_swc(document.component_types, swc, interfaces)
        assert not document.component_types.has_elements


class TestPortComSpecs:
    """The four default com-specs."""

    def test_a_provided_sender_receiver_port_emits_one_spec_per_data_element(self, component_shapes):
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["PortsOnly"], interfaces)
        port = golden_slice(emitted, "pOnly", tag="P-PORT-PROTOTYPE")
        assert port == golden_slice(text, "pOnly", tag="P-PORT-PROTOTYPE")
        assert port.count("<NONQUEUED-SENDER-COM-SPEC>") == len(_MULTI_SR["data_elements"])
        assert port.index("/PortInterfaces/IMulti/a") < port.index("/PortInterfaces/IMulti/b")

    def test_a_required_sender_receiver_port_emits_the_full_receiver_spec_body(self, component_shapes):
        # These seven children, *in this order*.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["MixedOrder"], interfaces)
        port = golden_slice(emitted, "Mango", tag="R-PORT-PROTOTYPE")
        body = [line.strip() for line in port.splitlines()]
        start = body.index("<NONQUEUED-RECEIVER-COM-SPEC>")
        assert body[start + 2 : start + 8] == [
            "<HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>",
            "<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>",
            "<ALIVE-TIMEOUT>0</ALIVE-TIMEOUT>",
            "<ENABLE-UPDATE>false</ENABLE-UPDATE>",
            "<HANDLE-NEVER-RECEIVED>false</HANDLE-NEVER-RECEIVED>",
            "<HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>",
        ]
        assert body[start + 1].startswith("<DATA-ELEMENT-REF")
        assert "FILTER" not in port

    def test_a_provided_client_server_port_emits_one_server_com_spec_per_operation(self, component_shapes):
        # SERVER-COM-SPEC/QUEUE-LENGTH are in NEITHER golden (README, "Reachable
        # but not in the goldens"), so this is their only byte-level oracle.
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["CsServer"], interfaces)
        port = golden_slice(emitted, "pCs", tag="P-PORT-PROTOTYPE")
        assert port == golden_slice(text, "pCs", tag="P-PORT-PROTOTYPE")
        assert port.count("<SERVER-COM-SPEC>") == len(_MULTI_CS["operations"])
        assert port.count("<QUEUE-LENGTH>1</QUEUE-LENGTH>") == len(_MULTI_CS["operations"])
        assert port.index("/PortInterfaces/IMultiCs/Op1") < port.index("/PortInterfaces/IMultiCs/Op2")

    def test_a_required_client_server_port_emits_one_client_com_spec_per_operation(self, component_shapes):
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["CsClient"], interfaces)
        port = golden_slice(emitted, "rCs", tag="R-PORT-PROTOTYPE")
        assert port == golden_slice(text, "rCs", tag="R-PORT-PROTOTYPE")
        assert port.count("<CLIENT-COM-SPEC>") == len(_MULTI_CS["operations"])
        assert "<QUEUE-LENGTH>" not in port

    def test_the_interface_tref_carries_the_interface_kind_as_its_dest(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        sr_emitted, _ = _emit(swcs["PortsOnly"], interfaces)
        cs_emitted, _ = _emit(swcs["CsClient"], interfaces)
        assert (
            '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/IMulti'
            "</PROVIDED-INTERFACE-TREF>" in sr_emitted
        )
        assert (
            '<REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/PortInterfaces/IMultiCs'
            "</REQUIRED-INTERFACE-TREF>" in cs_emitted
        )

    def test_a_sender_com_spec_carries_the_init_reference_after_its_protection_flag(self, component_shapes):
        """The ``INIT-VALUE`` position is the schema's.

        It is the one member of the ``NONQUEUED-SENDER-COM-SPEC`` group, after the ``SENDER-COM-SPEC`` group's
        ``USES-END-TO-END-PROTECTION``.
        """
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["InitPorts"], interfaces)
        port = golden_slice(emitted, "pInit", tag="P-PORT-PROTOTYPE")
        body = [line.strip() for line in port.splitlines()]

        assert port == golden_slice(text, "pInit", tag="P-PORT-PROTOTYPE")
        assert port.count("<INIT-VALUE>") == 1
        assert body.index("<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>") + 1 == body.index(
            "<INIT-VALUE>"
        )
        assert '<CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/PortInterfaces/IInit_valued_Init</CONSTANT-REF>' in port

    def test_a_receiver_com_spec_carries_it_after_handle_timeout_type(self, component_shapes):
        """The receiver's ``INIT-VALUE`` position, after ``HANDLE-TIMEOUT-TYPE``."""
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["InitPorts"], interfaces)
        port = golden_slice(emitted, "rInit", tag="R-PORT-PROTOTYPE")
        body = [line.strip() for line in port.splitlines()]

        assert port == golden_slice(text, "rInit", tag="R-PORT-PROTOTYPE")
        assert port.count("<INIT-VALUE>") == 1
        assert body.index("<HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>") + 1 == body.index("<INIT-VALUE>")

    def test_a_com_spec_for_an_element_without_init_carries_none(self, component_shapes):
        """One reference per *valued* element, not one per com-spec list."""
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["InitPorts"], interfaces)

        for tag in ("P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"):
            port = golden_slice(emitted, "pInit" if tag.startswith("P") else "rInit", tag=tag)
            specs = port.split("<DATA-ELEMENT-REF")
            # specs[1] is `valued`'s com-spec body, specs[2] is `bare`'s.
            assert "/PortInterfaces/IInit/valued" in specs[1] and "<INIT-VALUE>" in specs[1]
            assert "/PortInterfaces/IInit/bare" in specs[2] and "<INIT-VALUE>" not in specs[2]

    def test_a_port_on_a_value_less_interface_is_the_com_spec_0_7_0_emitted(self, component_shapes):
        """`IMulti` declares no `init`, so its com-spec carries no ``INIT-VALUE``."""
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["PortsOnly"], interfaces)

        assert "INIT-VALUE" not in golden_slice(emitted, "pOnly", tag="P-PORT-PROTOTYPE")

    def test_a_client_server_com_spec_never_carries_one(self, component_shapes):
        """A client-server interface has no data element, so no value can reach it."""
        _text, swcs, interfaces = component_shapes
        server, _ = _emit(swcs["CsServer"], interfaces)
        client, _ = _emit(swcs["CsClient"], interfaces)

        assert "INIT-VALUE" not in server
        assert "INIT-VALUE" not in client

    def test_an_empty_com_specs_wrapper_is_unreachable(self):
        # Unreachable: the schema refuses an interface with no data elements / no operations, so every
        # port has a com-spec. Asserted, because relaxing that validator needs a decision on the empty
        # wrapper's form.
        with pytest.raises(ValueError, match="data_elements must not be empty"):
            SRInterface(name="IEmpty", kind="sender_receiver", data_elements=[])
        with pytest.raises(ValueError, match="operations must not be empty"):
            CSInterface(name="IEmpty", kind="client_server", operations=[])


class TestNoPortApiOptions:
    """No ``PORT-API-OPTIONS`` is written.

    The model states no port API option, ``SwcInternalBehavior.portAPIOption`` is ``0..*`` (SWCT R4.2.2
    ``:23196``, R4.3.0 ``:25726``) and the RTE acts on a flag set to true only (``[SWS_Rte_02613]``,
    ``[SWS_Rte_03799]``, ``[SWS_Rte_07100]``, SWS RTE R22-11).
    """

    @pytest.mark.parametrize("name", ["MixedOrder", "PortsOnly", "TwoRunnables"])
    def test_no_option_is_emitted(self, component_shapes, name):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs[name], interfaces)
        for tag in ("PORT-API-OPTIONS", "PORT-API-OPTION", "ENABLE-TAKE-ADDRESS", "INDIRECT-API", "<PORT-REF "):
            assert tag not in emitted

    def test_ports_stay_provide_first_while_no_option_is_written(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["MixedOrder"], interfaces)
        ports = [line.strip().split(">")[1].split("<")[0] for line in emitted.splitlines() if "<SHORT-NAME>" in line]
        assert ports[1:4] == ["apple", "Zebra", "Mango"]

    def test_the_composite_golden_s_component_has_none(self):
        model_root = str(GOLDEN_MODEL_ROOTS["composite-4.2.2"])
        swc = _model_swcs(model_root)["SensorSwc"]
        emitted, _implementation = _emit(swc, _model_interfaces(model_root))
        assert "PORT-API-OPTIONS" not in emitted

    def test_a_runnable_less_component_with_ports_ends_its_behaviour_with_the_flag(self, component_shapes):
        # Neither EVENTS nor RUNNABLES, and no option container either.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["PortsOnly"], interfaces)
        assert "<RUNNABLES>" not in emitted
        assert "<EVENTS>" not in emitted
        assert "<SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>" in emitted


class TestInternalBehavior:
    """``SWC-INTERNAL-BEHAVIOR``: its name, its last child and its runnables."""

    def test_the_behavior_is_named_after_the_component_and_nested_inside_it(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        assert "  <INTERNAL-BEHAVIORS>\n    <SWC-INTERNAL-BEHAVIOR>\n" in emitted
        assert "<SHORT-NAME>TwoRunnables_InternalBehavior</SHORT-NAME>" in emitted

    def test_supports_multiple_instantiation_is_the_behavior_s_last_child(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        behavior = golden_slice(emitted, "TwoRunnables_InternalBehavior")
        assert behavior.splitlines()[-2].strip() == (
            "<SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>"
        )

    def test_a_runnable_carries_its_symbol_and_concurrency_flag(self, component_shapes):
        # A runnable that states no `symbol` writes its name as SYMBOL.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        runnable = golden_slice(emitted, "R1", tag="RUNNABLE-ENTITY")
        assert runnable == (
            "<RUNNABLE-ENTITY>\n"
            "  <SHORT-NAME>R1</SHORT-NAME>\n"
            "  <MINIMUM-START-INTERVAL>0</MINIMUM-START-INTERVAL>\n"
            "  <CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>\n"
            "  <SYMBOL>R1</SYMBOL>\n"
            "</RUNNABLE-ENTITY>"
        )

    def test_a_runnable_that_states_a_symbol_carries_it_as_symbol(self, component_shapes):
        # A stated `symbol` is the entry point's name, written at SYMBOL's own
        # position; the runnable's SHORT-NAME stays its name.
        _text, swcs, interfaces = component_shapes
        swc = swcs["TwoRunnables"]
        first, *rest = swc.runnables
        stated = swc.model_copy(update={"runnables": [first.model_copy(update={"symbol": "_R1_entry"}), *rest]})
        emitted, _implementation = _emit(stated, interfaces)
        runnable = golden_slice(emitted, "R1", tag="RUNNABLE-ENTITY")
        assert runnable == (
            "<RUNNABLE-ENTITY>\n"
            "  <SHORT-NAME>R1</SHORT-NAME>\n"
            "  <MINIMUM-START-INTERVAL>0</MINIMUM-START-INTERVAL>\n"
            "  <CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>\n"
            "  <SYMBOL>_R1_entry</SYMBOL>\n"
            "</RUNNABLE-ENTITY>"
        )
        assert "<SYMBOL>R2</SYMBOL>" in emitted

    def test_runnables_are_emitted_in_declaration_order(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        assert emitted.index("<SHORT-NAME>R1</SHORT-NAME>") < emitted.index("<SHORT-NAME>R2</SHORT-NAME>")

    def test_a_sub_millisecond_minimum_start_interval_is_written_exactly(self, component_shapes):
        # 0.0004 s is written as 0.0004, not rounded to 0 ms —
        # the writer never changes a model value.
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["SubMilli"], interfaces)
        assert "<MINIMUM-START-INTERVAL>0.0004</MINIMUM-START-INTERVAL>" in emitted
        assert emitted == golden_slice(text, "SubMilli", tag="APPLICATION-SW-COMPONENT-TYPE")

    def test_a_declared_whole_millisecond_interval_is_written_unchanged(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["MixedOrder"], interfaces)
        assert "<MINIMUM-START-INTERVAL>0.005</MINIMUM-START-INTERVAL>" in emitted

    def test_a_runnable_less_swc_omits_all_three_behavior_lists(self, component_shapes):
        # `Bare` declares `handle_termination_and_restart: no_support` (a component without it is
        # refused), so its one line sits between the short name and the multiple-instantiation
        # flag.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["Bare"], interfaces)
        behavior = golden_slice(emitted, "Bare_InternalBehavior")
        assert behavior == (
            "<SWC-INTERNAL-BEHAVIOR>\n"
            "  <SHORT-NAME>Bare_InternalBehavior</SHORT-NAME>\n"
            "  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\n"
            "  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>\n"
            "</SWC-INTERNAL-BEHAVIOR>"
        )


#: Every tag a runnable's three access lists can put into a document: the six containers
#: `_emit_access_points` places under a `RUNNABLE-ENTITY` (removing one removes its points) and the
#: capability marker, an `ADMIN-DATA` on the `SWC-INTERNAL-BEHAVIOR`. Hand-derived;
#: `test_the_added_tags_are_the_writers_own_container_set` pins the six against the writer's tuple. Not
#: here: `ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS` and the returns event, which no access list alone writes.
ADDED_TAGS = (
    "DATA-READ-ACCESSS",
    "DATA-RECEIVE-POINT-BY-ARGUMENTS",
    "DATA-RECEIVE-POINT-BY-VALUES",
    "DATA-SEND-POINTS",
    "DATA-WRITE-ACCESSS",
    "SERVER-CALL-POINTS",
    "ADMIN-DATA",
)

#: The project the additive-delta pair starts from: two SWCs, one SR interface
#: with one element, a require port on one component and a provide port on the
#: other -- so one `reads` entry and one `writes` entry reach two of the four
#: containers, and the model already passes `check-model`.
BASIC_PROJECT = REPO_ROOT / "examples" / "basic"


def _copy_basic(destination: Path) -> Path:
    """Copy `examples/basic`'s manifest and model into *destination*."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASIC_PROJECT / "swcraft.yaml", destination / "swcraft.yaml")
    shutil.copytree(BASIC_PROJECT / "model", destination / "model")
    return destination


def _generate(project: Path, version: str) -> bytes:
    """Generate *project*'s ARXML at *version* and return its bytes."""
    output = project / "dist" / "model.arxml"
    generate_arxml(str(project / "model"), str(output), version=version)
    return output.read_bytes()


def _strip_added_nodes(document: bytes) -> bytes:
    """Remove every node an access list can add and re-serialize with the writer's serializer.

    An lxml pass parsed with `remove_blank_text=True`, never a string edit. **What it does not reach:**
    `ADMIN-DATA` is removed wherever it appears, so over foreign vendor metadata the strip would be wider
    than the delta it measures.
    """
    root = etree.fromstring(document, etree.XMLParser(remove_blank_text=True))
    doomed = [node for tag in ADDED_TAGS for node in root.iter(f"{{{AR_NAMESPACE}}}{tag}")]
    for node in doomed:
        parent = node.getparent()
        assert parent is not None, "an added node cannot be the document root"
        parent.remove(node)
    return serialize_document(root).encode("utf-8")


#: The one `reads` entry and the one `writes` entry the declaring copy gains,
#: by SWC file stem: `examples/basic`'s require port `rIn` and provide port
#: `pOut` both name `IExample`, whose single data element is `value`.
DELTA_ACCESS: dict[str, tuple[str, dict[str, str]]] = {
    "ExampleConsumerSWC": ("reads", {"port": "rIn", "element": "value"}),
    "ExampleSWC": ("writes", {"port": "pOut", "element": "value"}),
}


def _declare_access(model_root: Path, declared: bool) -> None:
    """Rewrite both SWC files the same way, with the access entry when *declared*, so only the list differs."""
    for path in sorted((model_root / "swc").rglob("*.yaml"), key=lambda item: item.as_posix()):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        field, entry = DELTA_ACCESS[path.stem]
        for runnable in document["runnables"]:
            runnable.pop(field, None)
            if declared:
                runnable[field] = [dict(entry)]
        path.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )


class TestTheDeltaOfAnAccessIsExactlyTheNodesThatCarryIt:
    """The additive-delta gate, at both AUTOSAR versions.

    Remove the added nodes and the two documents are the same bytes again.
    **What this pair does not reach:** a `calls` entry (`examples/basic` has no client-server port, so
    `SERVER-CALL-POINTS` is in :data:`ADDED_TAGS` without being exercised) and the by-value receive
    container. Both are written over ``fixtures/access_points/model`` by the round-trip, 4.3.0 and XSD suites.
    """

    @pytest.fixture
    def without_access(self, tmp_path: Path) -> Path:
        project = _copy_basic(tmp_path / "without")
        _declare_access(project / "model", declared=False)
        return project

    @pytest.fixture
    def with_access(self, tmp_path: Path) -> Path:
        project = _copy_basic(tmp_path / "with")
        _declare_access(project / "model", declared=True)
        return project

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_stripping_the_added_nodes_restores_the_access_less_document(
        self, without_access: Path, with_access: Path, version: str
    ) -> None:
        bare = _generate(without_access, version)
        declaring = _generate(with_access, version)

        assert _strip_added_nodes(declaring) == bare

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_two_documents_really_differ(self, without_access: Path, with_access: Path, version: str) -> None:
        """Non-vacuity: without this the strip could be comparing two equal files."""
        bare = _generate(without_access, version)
        declaring = _generate(with_access, version)

        assert declaring != bare
        assert len(declaring) > len(bare)

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_strip_pass_itself_moves_no_byte(self, without_access: Path, version: str) -> None:
        """The control: the strip's parse and re-serialize is the identity on a document with nothing to remove.

        A strip re-indenting the whole file would otherwise pass, re-indenting both sides alike.
        """
        bare = _generate(without_access, version)

        assert _strip_added_nodes(bare) == bare

    def test_the_added_tags_are_the_writers_own_container_set(self) -> None:
        """Non-vacuity: the strip is over the containers the writer places, not a set covering today's two."""
        assert set(_ACCESS_CONTAINER_ORDER) | {"ADMIN-DATA"} == set(ADDED_TAGS)

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_declaring_document_carries_the_two_containers_and_the_marker(
        self, with_access: Path, version: str
    ) -> None:
        """Non-vacuity: the strip removes points *and* the capability marker, one per component with a point."""
        text = _generate(with_access, version).decode("utf-8")

        assert text.count("<DATA-RECEIVE-POINT-BY-ARGUMENTS>") == 1
        assert text.count("<DATA-SEND-POINTS>") == 1
        assert text.count("<VARIABLE-ACCESS>") == 2
        assert text.count(f'<SDG GID="{SDG_GID}">') == 2
        assert "<DATA-RECEIVE-POINT-BY-VALUES>" not in text
        assert "<SERVER-CALL-POINTS>" not in text

    def test_the_access_less_variant_carries_no_added_tag(self, without_access: Path) -> None:
        """The other half of "additive": a model with no list gains nothing."""
        text = _generate(without_access, "4.2.2").decode("utf-8")

        for tag in ADDED_TAGS:
            assert tag not in text

    def test_the_declaring_variant_really_declares_and_is_consistent(self, with_access: Path) -> None:
        """Non-vacuity: the second model declares the entries and passes `check-model`, so the delta is legal."""
        swcs = read_swc_model(str(with_access / "model"))
        declared = {
            (swc.name, runnable.name): (
                [(read.port, read.element) for read in runnable.reads],
                [(write.port, write.element) for write in runnable.writes],
            )
            for swc in swcs
            for runnable in swc.runnables
        }

        assert declared == {
            ("ExampleConsumerSWC", "ExampleConsumerRun"): ([("rIn", "value")], []),
            ("ExampleSWC", "ExampleRun"): ([], [("pOut", "value")]),
        }
        assert check_cross_layer_consistency(str(with_access / "model")).consistent


class TestExamplesBasicDeclaresNoAccessAndEmitsNone:
    """`examples/basic`, which declares no access, emits none: the half a released project can check itself.

    No added tag appears at either version and the 4.2.2 document is still the committed golden; the 4.3.0
    half is `test_arxml_4_3_0.py`'s substitution over the same root.
    """

    @pytest.fixture
    def basic(self, tmp_path: Path) -> Path:
        return _copy_basic(tmp_path / "basic")

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_no_added_tag_appears(self, basic: Path, version: str) -> None:
        text = _generate(basic, version).decode("utf-8")

        for tag in ADDED_TAGS:
            assert tag not in text

    def test_the_document_is_still_the_committed_golden(self, basic: Path) -> None:
        assert _generate(basic, "4.2.2").decode("utf-8") == read_golden("basic-4.2.2")

    def test_the_model_really_declares_no_access(self, basic: Path) -> None:
        """Non-vacuity: both components have a port on an interface with a member to name."""
        runnables = [
            runnable
            for path in sorted((basic / "model" / "swc").rglob("*.yaml"), key=lambda item: item.as_posix())
            for runnable in yaml.safe_load(path.read_text(encoding="utf-8"))["runnables"]
        ]

        assert runnables
        assert all(not set(runnable) & {"reads", "writes", "calls"} for runnable in runnables)


class TestEvents:
    """``TIMING-EVENT``/``INIT-EVENT``: names, exact periods, order and the de-duplication counter."""

    def test_a_timing_event_is_named_tmt_and_carries_the_period_on_the_grid(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        assert golden_slice(emitted, "TMT_R1") == (
            "<TIMING-EVENT>\n"
            "  <SHORT-NAME>TMT_R1</SHORT-NAME>\n"
            '  <START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">'
            "/ComponentTypes/TwoRunnables/TwoRunnables_InternalBehavior/R1</START-ON-EVENT-REF>\n"
            "  <PERIOD>0.01</PERIOD>\n"
            "</TIMING-EVENT>"
        )

    def test_an_init_event_is_named_it_and_has_no_period(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        assert golden_slice(emitted, "IT_R2") == (
            "<INIT-EVENT>\n"
            "  <SHORT-NAME>IT_R2</SHORT-NAME>\n"
            '  <START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">'
            "/ComponentTypes/TwoRunnables/TwoRunnables_InternalBehavior/R2</START-ON-EVENT-REF>\n"
            "</INIT-EVENT>"
        )

    def test_a_sub_millisecond_period_is_written_exactly(self, component_shapes):
        # 0.0125 s is written as 0.0125; a millisecond grid's half-to-even round()
        # would write 0.012, a value the model never held.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["SubMilli"], interfaces)
        assert "<PERIOD>0.0125</PERIOD>" in emitted

    def test_events_are_interleaved_per_runnable_in_declaration_order(self, component_shapes):
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        events = [line.strip() for line in golden_slice(emitted, "TwoRunnables_InternalBehavior").splitlines()]
        names = [line for line in events if line.startswith("<SHORT-NAME>") and ("TMT_" in line or "IT_" in line)]
        assert names == [
            "<SHORT-NAME>TMT_R1</SHORT-NAME>",
            "<SHORT-NAME>IT_R2</SHORT-NAME>",
            "<SHORT-NAME>TMT_R2</SHORT-NAME>",
        ]
        assert emitted == golden_slice(text, "TwoRunnables", tag="APPLICATION-SW-COMPONENT-TYPE")

    def test_colliding_event_names_get_the_deduplication_counter(self, component_shapes):
        # The counter renames the *first* colliding event
        # retroactively (TMT_R becomes TMT_R_0), which is why it cannot be
        # implemented as "append a suffix to the second one".
        text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["DupEvents"], interfaces)
        assert emitted == golden_slice(text, "DupEvents", tag="APPLICATION-SW-COMPONENT-TYPE")
        names = [line.strip() for line in emitted.splitlines() if "<SHORT-NAME>" in line]
        assert "<SHORT-NAME>TMT_R_0</SHORT-NAME>" in names
        assert "<SHORT-NAME>TMT_R_1</SHORT-NAME>" in names
        assert "<SHORT-NAME>IT_R_0</SHORT-NAME>" in names
        assert "<SHORT-NAME>IT_R_1</SHORT-NAME>" in names
        assert "<SHORT-NAME>TMT_R</SHORT-NAME>" not in names

    @pytest.mark.parametrize(
        ("bases", "expected"),
        [
            ([], []),
            (["TMT_A"], ["TMT_A"]),
            (["TMT_A", "TMT_B"], ["TMT_A", "TMT_B"]),
            (["TMT_A", "TMT_A"], ["TMT_A_0", "TMT_A_1"]),
            (["TMT_A", "TMT_A", "TMT_A"], ["TMT_A_0", "TMT_A_1", "TMT_A_2"]),
            (["TMT_A", "IT_A", "TMT_A"], ["TMT_A_0", "IT_A", "TMT_A_1"]),
            # The retroactive rename must skip an index that is already taken, or two
            # identical SHORT-NAMEs land in one EVENTS block -- exactly the
            # invalid ARXML the counter exists to prevent.
            (["TMT_A", "TMT_A_0", "TMT_A"], ["TMT_A_1", "TMT_A_0", "TMT_A_2"]),
            (["TMT_A", "TMT_A_1", "TMT_A"], ["TMT_A_0", "TMT_A_1", "TMT_A_2"]),
            # A neighbour that only looks indexed must not push the counter:
            # the digit test is anchored at both ends.
            (["TMT_A", "TMT_A_12x", "TMT_A"], ["TMT_A_0", "TMT_A_12x", "TMT_A_1"]),
        ],
    )
    def test_the_unique_name_helper_reproduces_the_counter(self, bases, expected):
        assert _unique_event_names(bases) == expected

    @pytest.mark.parametrize(
        "bases",
        [
            ["TMT_A", "TMT_A_0", "TMT_A"],
            ["TMT_A", "TMT_A", "TMT_A_0"],
            ["TMT_A", "TMT_A_0", "TMT_A_0", "TMT_A"],
            ["TMT_A", "TMT_A_1", "TMT_A_2", "TMT_A", "TMT_A"],
            ["TMT_A"] * 5,
            ["TMT_A", "IT_A", "TMT_A", "IT_A", "TMT_A_0"],
        ],
    )
    def test_the_helper_never_returns_a_duplicate(self, bases):
        # The function's whole purpose, asserted as a property rather than only
        # as a table of expected outputs: two events with the same SHORT-NAME in
        # one EVENTS block is invalid ARXML.
        names = _unique_event_names(bases)
        assert len(names) == len(bases)
        assert len(set(names)) == len(names), names

    def test_the_counter_is_scoped_to_one_component(self, component_shapes):
        # `DupEvents` has the collisions; `TwoRunnables` must be unaffected.
        _text, swcs, interfaces = component_shapes
        emitted, _implementation = _emit(swcs["TwoRunnables"], interfaces)
        assert "<SHORT-NAME>TMT_R1</SHORT-NAME>" in emitted
        assert "_0</SHORT-NAME>" not in emitted


class TestSwcImplementation:
    """``SWC-IMPLEMENTATION``, interleaved after its component."""

    @pytest.mark.parametrize(("golden", "name"), GOLDEN_SWCS)
    def test_implementation_matches_the_golden_slice(self, golden, name):
        model_root = str(GOLDEN_MODEL_ROOTS[golden])
        _emitted, implementation = _emit(_model_swcs(model_root)[name], _model_interfaces(model_root))
        assert implementation == golden_slice(read_golden(golden), f"{name}_Implementation")

    def test_the_behavior_ref_points_at_the_nested_internal_behavior(self, component_shapes):
        _text, swcs, interfaces = component_shapes
        _emitted, implementation = _emit(swcs["Bare"], interfaces)
        assert implementation == (
            "<SWC-IMPLEMENTATION>\n"
            "  <SHORT-NAME>Bare_Implementation</SHORT-NAME>\n"
            '  <BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR">/ComponentTypes/Bare/Bare_InternalBehavior</BEHAVIOR-REF>\n'
            "</SWC-IMPLEMENTATION>"
        )

    def test_the_implementation_is_interleaved_right_after_its_component(self, component_shapes):
        # A package-level sibling emitted after *each* SWC, not
        # collected at the end. Emitting them in two passes would still produce
        # valid ARXML and would not be byte-equal.
        _text, swcs, interfaces = component_shapes
        document = ArxmlDocument()
        ordered = [swcs[swc["name"]] for swc in _SHAPE_SWCS]
        _emit_swcs(document.component_types, ordered, interfaces)
        expected = []
        for swc in ordered:
            expected += [swc.name, f"{swc.name}_Implementation"]
        assert _package_short_names(document.component_types) == expected


class TestNonApplicationSwc:
    """Only ``application`` SWCs are emitted."""

    @pytest.mark.parametrize("swc_type", ["service", "ecu_abstraction"])
    def test_a_non_application_component_is_rejected(self, swc_type):
        swc = SWComponent(name="Odd", type=swc_type)
        with pytest.raises(ValueError, match="only 'application' SWCs are generated"):
            _emit_swc(ArxmlDocument().component_types, swc, {})

    @pytest.mark.parametrize("swc_type", ["service", "ecu_abstraction"])
    def test_the_message_matches_the_one_build_workspace_raises(self, tmp_path, swc_type):
        # One copy of the guard: the shared `_check_swc_type()` is
        # what both paths call, so the messages cannot drift apart.
        model_root = make_empty_model_root(tmp_path)
        _write_model(
            model_root,
            "swc",
            {
                "name": "Odd",
                "type": swc_type,
                "ports": [],
                "runnables": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        swc = _model_swcs(str(model_root))["Odd"]
        with pytest.raises(ValueError) as via_emitter:
            _emit_swc(ArxmlDocument().component_types, swc, {})
        with pytest.raises(ValueError) as via_workspace:
            build_workspace(str(model_root))
        assert str(via_emitter.value) == str(via_workspace.value)

    def test_the_message_no_longer_attributes_the_restriction_to_cogu(self):
        with pytest.raises(ValueError) as raised:
            _check_swc_type(SWComponent(name="Odd", type="service"))
        assert "cogu" not in str(raised.value)
        assert "autosar" not in str(raised.value).lower()

    def test_an_application_component_passes(self):
        assert _check_swc_type(SWComponent(name="Fine", type="application")) is None


class TestComponentDispatcher:
    """``_emit_swcs()`` — the twin of ``build_workspace()``'s SWC pass."""

    @pytest.mark.parametrize("golden", list(GOLDEN_MODEL_ROOTS))
    def test_every_component_of_a_golden_model_is_emitted_in_read_order(self, golden):
        model_root = str(GOLDEN_MODEL_ROOTS[golden])
        document = ArxmlDocument()
        swcs = read_swc_model(model_root)
        _emit_swcs(document.component_types, swcs, _model_interfaces(model_root))
        expected = []
        for swc in swcs:
            expected += [swc.name, f"{swc.name}_Implementation"]
        assert _package_short_names(document.component_types) == expected

    @pytest.mark.parametrize("golden", list(GOLDEN_MODEL_ROOTS))
    def test_every_emitted_component_matches_its_golden_slice(self, golden):
        model_root = str(GOLDEN_MODEL_ROOTS[golden])
        document = ArxmlDocument()
        _emit_swcs(document.component_types, read_swc_model(model_root), _model_interfaces(model_root))
        text = read_golden(golden)
        for element in document.component_types.elements:
            fragment = serialize_fragment(element)
            short_name = element.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text
            tag = fragment.splitlines()[0].strip("<>")
            assert fragment == golden_slice(text, short_name, tag=tag)

    def test_a_model_with_no_components_emits_nothing(self):
        document = ArxmlDocument()
        assert _emit_swcs(document.component_types, [], {}) == []
        assert not document.component_types.has_elements

    def test_a_non_application_component_aborts_the_pass(self):
        document = ArxmlDocument()
        swcs = [
            SWComponent(name="Fine", runnables=[Runnable(name="R")]),
            SWComponent(name="Odd", type="service"),
        ]
        with pytest.raises(ValueError, match="only 'application' SWCs are generated"):
            _emit_swcs(document.component_types, swcs, {})
