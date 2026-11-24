"""The runnable's access points and the capability marker.

The expected fragments are hand-derived, not captured, from two sources the writer had no part in: the
licensed schemas' sequences (``RUNNABLE-ENTITY``'s four access containers sit between
``CAN-BE-INVOKED-CONCURRENTLY`` and ``SYMBOL`` in the order ``DATA-RECEIVE-POINT-BY-ARGUMENTS``,
``DATA-RECEIVE-POINT-BY-VALUES``, ``DATA-SEND-POINTS``, ``SERVER-CALL-POINTS``; ``VARIABLE-ACCESS`` is
``SHORT-NAME`` then ``ACCESSED-VARIABLE``; ``SYNCHRONOUS-SERVER-CALL-POINT`` is ``SHORT-NAME``,
``OPERATION-IREF``, ``TIMEOUT``; ``ADMIN-DATA`` follows an ``IDENTIFIABLE``'s ``SHORT-NAME`` and holds
``SDGS/SDG/SD``) and the chosen shapes (an ``AUTOSAR-VARIABLE-IREF`` of exactly ``PORT-PROTOTYPE-REF`` +
``TARGET-DATA-PROTOTYPE-REF``; a call point's ``TIMEOUT`` ``0``). The serialization rules are
``writer/arxml_xml.py``'s, pinned by ``test_arxml_xml.py``. Slices are addressed by ``SHORT-NAME``
through ``conftest.golden_slice()``, never by line number.

Not reached here: XSD validity (the ``test-xsd`` stage); the additive-delta gate
(``test_arxml_components_writer.py``); the round trip (the round-trip oracle's typed comparison).

The refusal tests drive ``_emit_swc`` directly against a fresh ``ArxmlDocument``, so they pin that the
package holds nothing when the refusal is raised; a whole-document run only witnesses "no file on disk".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import golden_slice, make_empty_model_root, model_element_storage_name
from lxml import etree
from swcraft_tools.capability_markers import ACCESS_POINTS, SDG_GID
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    READ_ACCESS_PREFIX,
    WRITE_ACCESS_PREFIX,
    _emit_swc,
    access_point_name,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument, find_element

#: The AUTOSAR 4.x namespace every generated element carries.
_AR = "http://autosar.org/schema/r4.0"

# The throwaway model root the whole module generates from

#: A data prototype names a value type, which rests on a ``{name}_base`` base type.
DATA_TYPES: list[dict[str, Any]] = [
    {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
    {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
]

#: Two data elements and two operations, so an emitter writing every element rather than every declared
#: entry fails.
INTERFACES: list[dict[str, Any]] = [
    {
        "name": "ISignals",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": "value", "implementation_type": "uint8"},
            {"name": "other", "implementation_type": "uint8"},
        ],
    },
    {
        "name": "ICompute",
        "kind": "client_server",
        "operations": [
            {"name": "run", "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8"}]},
            {"name": "idle", "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8"}]},
        ],
    },
]

#: One component carrying every shape: a runnable declaring all three lists including a by-value read, a
#: runnable declaring none (inside a declaring component), and a port no runnable touches.
CONSUMER: dict[str, Any] = {
    "name": "Consumer",
    "type": "application",
    "ports": [
        {"name": "pOut", "direction": "provide", "interface": "ISignals"},
        {"name": "rIn", "direction": "require", "interface": "ISignals"},
        {"name": "rCompute", "direction": "require", "interface": "ICompute"},
        {"name": "rIdle", "direction": "require", "interface": "ISignals"},
    ],
    "runnables": [
        {
            "name": "Run",
            "reads": [
                {"port": "rIn", "element": "value"},
                {"port": "rIn", "element": "other", "by": "value"},
            ],
            "writes": [{"port": "pOut", "element": "value"}],
            "calls": [{"port": "rCompute", "operation": "run"}],
        },
        {"name": "Quiet"},
    ],
}

#: The same component with every list emptied — the "declares nothing" control
#: the marker and the containers are measured against.
QUIET: dict[str, Any] = {
    "name": "Consumer",
    "type": "application",
    "ports": CONSUMER["ports"],
    "runnables": [{"name": "Run"}, {"name": "Quiet"}],
}


def _write(model_root: Path, layer: str, stem: str, document: Any) -> None:
    """Write one element file in the canonical writer form."""
    directory = model_root / layer
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{stem}.yaml", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(yaml.safe_dump(document, default_flow_style=False, sort_keys=False))


def _write_model(base: Path, swcs: list[dict[str, Any]], interfaces: list[dict[str, Any]] | None = None) -> Path:
    """Create a throwaway model root under *base* carrying *swcs*.

    Each component declares ``handle_termination_and_restart: no_support`` unless its document states the
    key (``None`` leaves it absent): a component without it is refused.
    """
    model_root = make_empty_model_root(base)
    for data_type in DATA_TYPES:
        directory = model_element_storage_name(data_type["name"], kind=data_type["kind"]).rpartition("/")[0]
        _write(model_root, directory, data_type["name"], data_type)
    for interface in interfaces if interfaces is not None else INTERFACES:
        _write(model_root, "interfaces", interface["name"], interface)
    for swc in swcs:
        document = {**swc, "handle_termination_and_restart": swc.get("handle_termination_and_restart", "no_support")}
        if document["handle_termination_and_restart"] is None:
            del document["handle_termination_and_restart"]
        _write(model_root, "swc", swc["name"], document)
    return model_root


def _generate(base: Path, swcs: list[dict[str, Any]], **kwargs: Any) -> str:
    """Generate a whole document from a throwaway root and return its text."""
    model_root = _write_model(base, swcs, **kwargs)
    output = base / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


@pytest.fixture
def consumer_document(tmp_path: Path) -> str:
    """The whole document generated from the declaring component."""
    return _generate(tmp_path / "consumer", [CONSUMER])


@pytest.fixture
def quiet_document(tmp_path: Path) -> str:
    """The same component with no list declared anywhere."""
    return _generate(tmp_path / "quiet", [QUIET])


def _emit(base: Path, swc_document: dict[str, Any]) -> tuple[ArxmlDocument, Any]:
    """Emit one component into a fresh in-memory document; return both."""
    model_root = _write_model(base, [swc_document])
    interfaces = {iface.name: iface for iface in read_interfaces(str(model_root))}
    swc = next(swc for swc in read_swc_model(str(model_root)) if swc.name == swc_document["name"])
    document = ArxmlDocument()
    return document, _emit_swc(document.component_types, swc, interfaces)


def _refuse(base: Path, swc_document: dict[str, Any], **kwargs: Any) -> str:
    """Emit *swc_document* expecting a refusal; return the message, the package asserted still empty."""
    model_root = _write_model(base, [swc_document], **kwargs)
    interfaces = {iface.name: iface for iface in read_interfaces(str(model_root))}
    swc = next(swc for swc in read_swc_model(str(model_root)) if swc.name == swc_document["name"])
    document = ArxmlDocument()
    with pytest.raises(ValueError) as excinfo:
        _emit_swc(document.component_types, swc, interfaces)
    assert not document.component_types.has_elements
    return str(excinfo.value)


# The three generated names


class TestTheThreeGeneratedNames:
    """`R_<port>_<element>`, `W_<port>_<element>`, `Call_<port>_<operation>` — a common naming rule of tool exports."""

    def test_the_three_prefixes_are_the_constants_the_forms_are_built_from(self) -> None:
        assert READ_ACCESS_PREFIX == "R_"
        assert WRITE_ACCESS_PREFIX == "W_"
        assert CALL_POINT_PREFIX == "Call_"

    def test_the_name_joins_the_prefix_and_the_two_declared_names(self) -> None:
        assert access_point_name(READ_ACCESS_PREFIX, "rIn", "value") == "R_rIn_value"
        assert access_point_name(WRITE_ACCESS_PREFIX, "pOut", "value") == "W_pOut_value"
        assert access_point_name(CALL_POINT_PREFIX, "rCompute", "run") == "Call_rCompute_run"

    @pytest.mark.parametrize(
        ("prefix", "cost"),
        [(READ_ACCESS_PREFIX, 3), (WRITE_ACCESS_PREFIX, 3), (CALL_POINT_PREFIX, 6)],
    )
    def test_each_form_costs_the_joint_budget_the_plan_states(self, prefix: str, cost: int) -> None:
        """`R_`/`W_` spend three characters and `Call_` six."""
        assert len(access_point_name(prefix, "", "")) == cost

    def test_a_by_value_read_derives_the_same_name_as_a_by_argument_one(self) -> None:
        """`by` is no part of the identity, so it is no part of the name."""
        assert access_point_name(READ_ACCESS_PREFIX, "rIn", "other") == "R_rIn_other"


# Frozen fragments — the two element shapes


class TestTheVariableAccessShape:
    """The one shape the writer gives every variable access.

    `SHORT-NAME` + `ACCESSED-VARIABLE/AUTOSAR-VARIABLE-IREF/{PORT-PROTOTYPE-REF, TARGET-DATA-PROTOTYPE-REF}`.
    """

    def test_a_read_by_argument(self, consumer_document: str) -> None:
        assert golden_slice(consumer_document, "R_rIn_value") == (
            "<VARIABLE-ACCESS>\n"
            "  <SHORT-NAME>R_rIn_value</SHORT-NAME>\n"
            "  <ACCESSED-VARIABLE>\n"
            "    <AUTOSAR-VARIABLE-IREF>\n"
            '      <PORT-PROTOTYPE-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Consumer/rIn</PORT-PROTOTYPE-REF>\n'
            '      <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
            "/PortInterfaces/ISignals/value</TARGET-DATA-PROTOTYPE-REF>\n"
            "    </AUTOSAR-VARIABLE-IREF>\n"
            "  </ACCESSED-VARIABLE>\n"
            "</VARIABLE-ACCESS>"
        )

    def test_a_read_by_value_is_the_identical_element(self, consumer_document: str) -> None:
        """Only the *container* differs; the `VARIABLE-ACCESS` is the same."""
        assert golden_slice(consumer_document, "R_rIn_other") == (
            "<VARIABLE-ACCESS>\n"
            "  <SHORT-NAME>R_rIn_other</SHORT-NAME>\n"
            "  <ACCESSED-VARIABLE>\n"
            "    <AUTOSAR-VARIABLE-IREF>\n"
            '      <PORT-PROTOTYPE-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Consumer/rIn</PORT-PROTOTYPE-REF>\n'
            '      <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
            "/PortInterfaces/ISignals/other</TARGET-DATA-PROTOTYPE-REF>\n"
            "    </AUTOSAR-VARIABLE-IREF>\n"
            "  </ACCESSED-VARIABLE>\n"
            "</VARIABLE-ACCESS>"
        )

    def test_a_write_differs_only_in_the_prefix_and_the_port_dest(self, consumer_document: str) -> None:
        assert golden_slice(consumer_document, "W_pOut_value") == (
            "<VARIABLE-ACCESS>\n"
            "  <SHORT-NAME>W_pOut_value</SHORT-NAME>\n"
            "  <ACCESSED-VARIABLE>\n"
            "    <AUTOSAR-VARIABLE-IREF>\n"
            '      <PORT-PROTOTYPE-REF DEST="P-PORT-PROTOTYPE">/ComponentTypes/Consumer/pOut</PORT-PROTOTYPE-REF>\n'
            '      <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
            "/PortInterfaces/ISignals/value</TARGET-DATA-PROTOTYPE-REF>\n"
            "    </AUTOSAR-VARIABLE-IREF>\n"
            "  </ACCESSED-VARIABLE>\n"
            "</VARIABLE-ACCESS>"
        )

    @pytest.mark.parametrize("absent", ["<SCOPE>", "ROOT-VARIABLE-DATA-PROTOTYPE-REF", "CONTEXT-DATA-PROTOTYPE-REF"])
    def test_the_optional_children_the_export_never_carries_are_never_emitted(
        self, consumer_document: str, absent: str
    ) -> None:
        assert absent not in consumer_document


class TestTheServerCallPointShape:
    """The one shape the writer gives every server call point, with `TIMEOUT` `0` on all of them.

    `SHORT-NAME` + `OPERATION-IREF/{CONTEXT-R-PORT-REF, TARGET-REQUIRED-OPERATION-REF}` + `TIMEOUT`.
    """

    def test_a_synchronous_call_point(self, consumer_document: str) -> None:
        assert golden_slice(consumer_document, "Call_rCompute_run") == (
            "<SYNCHRONOUS-SERVER-CALL-POINT>\n"
            "  <SHORT-NAME>Call_rCompute_run</SHORT-NAME>\n"
            "  <OPERATION-IREF>\n"
            '    <CONTEXT-R-PORT-REF DEST="R-PORT-PROTOTYPE">'
            "/ComponentTypes/Consumer/rCompute</CONTEXT-R-PORT-REF>\n"
            '    <TARGET-REQUIRED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
            "/PortInterfaces/ICompute/run</TARGET-REQUIRED-OPERATION-REF>\n"
            "  </OPERATION-IREF>\n"
            "  <TIMEOUT>0</TIMEOUT>\n"
            "</SYNCHRONOUS-SERVER-CALL-POINT>"
        )

    def test_no_asynchronous_call_point_and_no_exclusive_area_reference(self, consumer_document: str) -> None:
        assert "ASYNCHRONOUS-SERVER-CALL-POINT" not in consumer_document
        assert "CALLED-FROM-WITHIN-EXCLUSIVE-AREA-REF" not in consumer_document


# The capability marker


class TestTheCapabilityMarker:
    """The capability marker is written immediately after the behaviour's `SHORT-NAME`.

    `<ADMIN-DATA><SDGS><SDG GID="SWCRAFT"><SD GID="ACCESS-POINTS">explicit</SD>…`, measured valid at both versions in
    that position and refused after `EVENTS`.
    """

    def test_a_declaring_component_carries_the_marker_after_the_short_name(self, consumer_document: str) -> None:
        behavior = golden_slice(consumer_document, "Consumer_InternalBehavior")
        assert behavior.startswith(
            "<SWC-INTERNAL-BEHAVIOR>\n"
            "  <SHORT-NAME>Consumer_InternalBehavior</SHORT-NAME>\n"
            "  <ADMIN-DATA>\n"
            "    <SDGS>\n"
            '      <SDG GID="SWCRAFT">\n'
            '        <SD GID="ACCESS-POINTS">explicit</SD>\n'
            "      </SDG>\n"
            "    </SDGS>\n"
            "  </ADMIN-DATA>\n"
            # The component has no events, so the `no_support` line follows the marker directly.
            "  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\n"
            # No PORT-API-OPTIONS is written, so the runnables follow.
            "  <RUNNABLES>\n"
        )

    def test_a_component_declaring_nothing_carries_no_marker(self, quiet_document: str) -> None:
        assert "ADMIN-DATA" not in quiet_document
        assert "SWCRAFT" not in quiet_document

    def test_the_marker_carries_no_version_string(self, consumer_document: str) -> None:
        """A capability token, never a version: a release must not move these bytes."""
        assert SDG_GID == "SWCRAFT"
        assert ACCESS_POINTS.sd_gid == "ACCESS-POINTS"
        assert ACCESS_POINTS.text == "explicit"
        from swcraft_tools._version import __version__

        assert __version__ not in golden_slice(consumer_document, "Consumer_InternalBehavior")

    def test_it_is_emitted_once_per_declaring_component(self, consumer_document: str) -> None:
        assert consumer_document.count("<ADMIN-DATA>") == 1


# Placement and order

#: The four containers, in the order both licensed schemas give them.
CONTAINER_ORDER = (
    "DATA-RECEIVE-POINT-BY-ARGUMENTS",
    "DATA-RECEIVE-POINT-BY-VALUES",
    "DATA-SEND-POINTS",
    "SERVER-CALL-POINTS",
)


def _child_tags(document: str, short_name: str) -> list[str]:
    """The local names of the direct children of the element named *short_name*."""
    root = etree.fromstring(document.encode("utf-8"))
    node = None
    for candidate in root.iter():
        name = candidate.find(f"{{{_AR}}}SHORT-NAME")
        if name is not None and name.text == short_name:
            node = candidate
            break
    assert node is not None, short_name
    return [etree.QName(child).localname for child in node]


class TestPlacementAndOrder:
    def test_the_containers_sit_between_can_be_invoked_concurrently_and_symbol(self, consumer_document: str) -> None:
        assert _child_tags(consumer_document, "Run") == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            *CONTAINER_ORDER,
            "SYMBOL",
        ]

    def test_a_runnable_declaring_nothing_is_byte_identical_to_today(
        self, consumer_document: str, quiet_document: str
    ) -> None:
        """The `Quiet` runnable, inside a declaring component, carries no container for its empty lists."""
        expected = (
            "<RUNNABLE-ENTITY>\n"
            "  <SHORT-NAME>Quiet</SHORT-NAME>\n"
            "  <MINIMUM-START-INTERVAL>0</MINIMUM-START-INTERVAL>\n"
            "  <CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>\n"
            "  <SYMBOL>Quiet</SYMBOL>\n"
            "</RUNNABLE-ENTITY>"
        )
        assert golden_slice(consumer_document, "Quiet") == expected
        assert golden_slice(quiet_document, "Quiet") == expected
        assert golden_slice(quiet_document, "Run") == expected.replace("Quiet", "Run")

    def test_a_container_is_emitted_only_when_its_list_is_non_empty(self, tmp_path: Path) -> None:
        one_read = {
            "name": "Consumer",
            "type": "application",
            "ports": CONSUMER["ports"],
            "runnables": [{"name": "Run", "reads": [{"port": "rIn", "element": "value"}]}],
        }
        document = _generate(tmp_path / "one", [one_read])
        assert _child_tags(document, "Run") == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "SYMBOL",
        ]

    def test_entries_follow_the_lists_declaration_order(self, tmp_path: Path) -> None:
        """Entries follow the model's file order: the lists carry no order of their own."""
        reversed_reads = {
            "name": "Consumer",
            "type": "application",
            "ports": CONSUMER["ports"],
            "runnables": [
                {
                    "name": "Run",
                    "reads": [{"port": "rIdle", "element": "value"}, {"port": "rIn", "element": "value"}],
                }
            ],
        }
        document = _generate(tmp_path / "order", [reversed_reads])
        first = document.index("R_rIdle_value")
        second = document.index("R_rIn_value")
        assert first < second

    def test_no_implicit_access_container_is_ever_emitted(self, consumer_document: str) -> None:
        """An empty `DATA-READ-ACCESSS`/`DATA-WRITE-ACCESSS` container is never emitted.

        A tool may write one; SWCraft deliberately does not.
        """
        assert "DATA-READ-ACCESSS" not in consumer_document
        assert "DATA-WRITE-ACCESSS" not in consumer_document

    def test_an_untouched_port_contributes_no_point(self, consumer_document: str) -> None:
        """The silence rule, witnessed: `rIdle` is declared and unused."""
        assert "R_rIdle" not in consumer_document
        assert "W_rIdle" not in consumer_document

    @pytest.mark.parametrize(
        "path",
        [
            "/ComponentTypes/Consumer/rIn",
            "/ComponentTypes/Consumer/pOut",
            "/ComponentTypes/Consumer/rCompute",
            "/PortInterfaces/ISignals/value",
            "/PortInterfaces/ISignals/other",
            "/PortInterfaces/ICompute/run",
        ],
    )
    def test_every_reference_resolves_inside_the_same_document(self, consumer_document: str, path: str) -> None:
        root = etree.fromstring(consumer_document.encode("utf-8"))
        assert find_element(root, path) is not None, path

    def test_the_references_the_points_carry_are_exactly_those_paths(self, consumer_document: str) -> None:
        """A walk, so a reference this module forgot to list is still checked."""
        root = etree.fromstring(consumer_document.encode("utf-8"))
        tags = {
            f"{{{_AR}}}PORT-PROTOTYPE-REF",
            f"{{{_AR}}}TARGET-DATA-PROTOTYPE-REF",
            f"{{{_AR}}}CONTEXT-R-PORT-REF",
            f"{{{_AR}}}TARGET-REQUIRED-OPERATION-REF",
        }
        references = [node.text for node in root.iter() if node.tag in tags]
        assert references
        for reference in references:
            assert find_element(root, reference) is not None, reference


# The refusals — the writer's resolution agrees with the five `swc.access.*` rules


def _with_runnable(runnable: dict[str, Any]) -> dict[str, Any]:
    return {"name": "Consumer", "type": "application", "ports": CONSUMER["ports"], "runnables": [runnable]}


class TestTheWriterRefusesWhatCheckModelReports:
    """The writer refuses each of the five `rule:swc.access.*` findings `check-model` reports.

    `generate arxml` has no `check-model` gate in front of it. Not claimed: the same sentence (the gate
    reports one record per entry, the writer batches into one `ValueError`).
    """

    def test_an_undeclared_port_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, _with_runnable({"name": "Run", "reads": [{"port": "rGhost", "element": "value"}]}))
        assert "rGhost" in message
        assert "Run" in message

    def test_an_element_the_ports_interface_does_not_declare_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, _with_runnable({"name": "Run", "reads": [{"port": "rIn", "element": "ghost"}]}))
        assert "ghost" in message
        assert "ISignals" in message

    def test_an_operation_the_ports_interface_does_not_declare_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(
            tmp_path, _with_runnable({"name": "Run", "calls": [{"port": "rCompute", "operation": "ghost"}]})
        )
        assert "ghost" in message
        assert "ICompute" in message

    def test_a_read_through_a_provide_port_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, _with_runnable({"name": "Run", "reads": [{"port": "pOut", "element": "value"}]}))
        assert "pOut" in message
        assert "provide" in message

    def test_a_write_through_a_require_port_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, _with_runnable({"name": "Run", "writes": [{"port": "rIn", "element": "value"}]}))
        assert "rIn" in message
        assert "require" in message

    def test_a_read_through_a_client_server_port_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(
            tmp_path, _with_runnable({"name": "Run", "reads": [{"port": "rCompute", "element": "value"}]})
        )
        assert "rCompute" in message
        assert "client/server" in message

    def test_a_call_through_a_sender_receiver_port_is_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, _with_runnable({"name": "Run", "calls": [{"port": "rIn", "operation": "run"}]}))
        assert "rIn" in message
        assert "sender/receiver" in message

    def test_a_whole_generate_run_writes_no_file(self, tmp_path: Path) -> None:
        """No file at the command's own boundary, not only at the emitter's."""
        model_root = _write_model(tmp_path, [_with_runnable({"name": "Run", "reads": [{"port": "x", "element": "y"}]})])
        output = tmp_path / "model.arxml"
        with pytest.raises(ValueError):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()


class TestTwoEntriesDerivingOneName:
    """The writer refuses two entries deriving one `SHORT-NAME` rather than emitting both.

    Port `a_b` element `c` and port `a` element `b_c` both give `R_a_b_c`; both names are declared and
    legal, and neither is the writer's to rename.
    """

    COLLIDING_INTERFACES: list[dict[str, Any]] = [
        {
            "name": "ISignals",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "c", "implementation_type": "uint8"},
                {"name": "b_c", "implementation_type": "uint8"},
            ],
        },
    ]

    COLLIDING_SWC: dict[str, Any] = {
        "name": "Consumer",
        "type": "application",
        "ports": [
            {"name": "a_b", "direction": "require", "interface": "ISignals"},
            {"name": "a", "direction": "require", "interface": "ISignals"},
        ],
        "runnables": [
            {"name": "Run", "reads": [{"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"}]},
        ],
    }

    def test_two_pairs_deriving_one_name_are_refused(self, tmp_path: Path) -> None:
        message = _refuse(tmp_path, self.COLLIDING_SWC, interfaces=self.COLLIDING_INTERFACES)
        assert "R_a_b_c" in message
        assert "'a_b'" in message
        assert "'a'" in message
        assert "'c'" in message
        assert "'b_c'" in message

    def test_the_same_pair_in_two_containers_cannot_arise(self, tmp_path: Path) -> None:
        """A by-value and a by-argument read of one pair never reach the writer: the model does not load."""
        model_root = _write_model(
            tmp_path,
            [
                _with_runnable(
                    {
                        "name": "Run",
                        "reads": [
                            {"port": "rIn", "element": "value"},
                            {"port": "rIn", "element": "value", "by": "value"},
                        ],
                    }
                )
            ],
        )
        with pytest.raises(Exception, match="duplicate reads entry"):
            read_swc_model(str(model_root))


# The scope of a `SHORT-NAME` under a runnable


class TestTheShortNameScopeIsTheRunnable:
    """The four containers share one namespace, the runnable's.

    An access point's path `/ComponentTypes/{swc}/{swc}_InternalBehavior/{runnable}/{name}` has no container segment.
    """

    def test_two_runnables_may_carry_the_same_point_name(self, tmp_path: Path) -> None:
        twice = {
            "name": "Consumer",
            "type": "application",
            "ports": CONSUMER["ports"],
            "runnables": [
                {"name": "A", "reads": [{"port": "rIn", "element": "value"}]},
                {"name": "B", "reads": [{"port": "rIn", "element": "value"}]},
            ],
        }
        document = _generate(tmp_path / "twice", [twice])
        assert document.count("<SHORT-NAME>R_rIn_value</SHORT-NAME>") == 2

    def test_a_point_and_a_generated_event_do_not_share_a_scope(self, tmp_path: Path) -> None:
        """A runnable's events live under `EVENTS`, a different parent."""
        with_event = {
            "name": "Consumer",
            "type": "application",
            "ports": CONSUMER["ports"],
            "runnables": [
                {
                    "name": "Run",
                    "events": [{"type": "init"}],
                    "reads": [{"port": "rIn", "element": "value"}],
                }
            ],
        }
        document = _generate(tmp_path / "event", [with_event])
        assert "<SHORT-NAME>IT_Run</SHORT-NAME>" in document
        assert "<SHORT-NAME>R_rIn_value</SHORT-NAME>" in document
