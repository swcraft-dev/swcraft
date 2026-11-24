"""The ARXML writer: ``OPERATION-INVOKED-EVENT`` and ``CAN-BE-INVOKED-CONCURRENTLY``.

An operation event ``{type: operation, port, operation}`` is emitted as one ``OPERATION-INVOKED-EVENT`` in its
component's ``EVENTS``, in the runnable's own declaration order beside its timing and init events:

* its ``SHORT-NAME`` is ``OIT_{port}_{operation}``, through the same retroactive ``_unique_event_names`` as every
  event, so two pairs deriving one base are suffixed ``_0`` / ``_1``;
* ``START-ON-EVENT-REF`` names the runnable, and ``OPERATION-IREF`` the component's **own** ``P-PORT-PROTOTYPE``
  (``CONTEXT-P-PORT-REF``) and its interface's ``CLIENT-SERVER-OPERATION`` (``TARGET-PROVIDED-OPERATION-REF``);
* a runnable's ``CAN-BE-INVOKED-CONCURRENTLY`` carries its ``concurrent`` field.

**The writer refuses, with nothing written, every class ``check-model`` reports**, through the same predicate
(``validator.check_operation_events``) and the identifier guard's ``OIT_`` budget over the final name; each negative
class is asked of both. Mixed event kinds are not judged: the fixture generates with no refusal, which says the model
is kept as written, never that an RTE generator accepts it.

What this module does **not** reach: XSD conformance (``TestTheOperationEventShapesConform``, ``test-xsd``); the
importer and component exchange; the parts of ``[constr_2000]`` the predicate does not read (possible errors, the
return value, ``errorHandling``, ``serverArgumentImplPolicy``, ``PortDefinedArgumentValues``); the queue length,
which stays 1; and any model shape outside the examples below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import write_model_element
from lxml import etree
from operation_event_support import (
    BOUNDARY_OPERATION,
    BOUNDARY_PORT,
    INTERFACE,
    INTERFACE_FILE,
    OPERATION,
    PORT,
    RUNNABLE,
    SERVER,
    SERVER_FILE,
    TIMING_EVENT,
    change,
    every_event_shape_model,
    mixed_event_model,
    put,
)
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.validator import check_cross_layer_consistency, check_operation_events
from swcraft_tools.writer.arxml_generation import (
    INTERNAL_BEHAVIOR_SUFFIX,
    OPERATION_EVENT_PREFIX,
    generate_arxml,
)

NS = {"ar": "http://autosar.org/schema/r4.0"}

BEHAVIOR_PATH = f"/ComponentTypes/{SERVER}/{SERVER}{INTERNAL_BEHAVIOR_SUFFIX}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A copy of the mixed-event fixture, with its root composition: clean."""
    return mixed_event_model(tmp_path / "model")


def _event(port: str = PORT, operation: str = OPERATION) -> dict[str, Any]:
    return {"type": "operation", "port": port, "operation": operation}


def _set_events(root: Path, events: list[dict[str, Any]], runnable: str = RUNNABLE) -> None:
    def mutate(document: dict[str, Any]) -> None:
        next(entry for entry in document["runnables"] if entry["name"] == runnable)["events"] = events

    change(root, SERVER_FILE, mutate)


def _add_runnable(root: Path, name: str, events: list[dict[str, Any]]) -> None:
    change(root, SERVER_FILE, lambda d: d["runnables"].append({"name": name, "events": events}))


def _add_port(root: Path, name: str, direction: str, interface: str) -> None:
    change(
        root, SERVER_FILE, lambda d: d["ports"].append({"name": name, "direction": direction, "interface": interface})
    )


def _add_operation(root: Path, name: str, arguments: list[dict[str, str]]) -> None:
    change(root, INTERFACE_FILE, lambda d: d["operations"].append({"name": name, "arguments": arguments}))


def _generate(root: Path, output: Path) -> etree._Element:
    generate_arxml(str(root), str(output))
    return etree.parse(str(output)).getroot()


def _component(document: etree._Element, name: str = SERVER) -> etree._Element:
    (node,) = document.xpath("//ar:APPLICATION-SW-COMPONENT-TYPE[ar:SHORT-NAME=$name]", namespaces=NS, name=name)
    return node


def _events(document: etree._Element) -> list[etree._Element]:
    (events,) = _component(document).xpath(".//ar:SWC-INTERNAL-BEHAVIOR/ar:EVENTS", namespaces=NS)
    return list(events)


def _tag(node: etree._Element) -> str:
    return etree.QName(node).localname


def _short_name(node: etree._Element) -> str:
    return node.findtext("ar:SHORT-NAME", namespaces=NS)


def _reference(node: etree._Element, tag: str) -> tuple[str, str]:
    (reference,) = node.xpath(f".//ar:{tag}", namespaces=NS)
    return reference.text, reference.get("DEST")


def _concurrency(document: etree._Element) -> dict[str, str]:
    return {
        _short_name(runnable): runnable.findtext("ar:CAN-BE-INVOKED-CONCURRENTLY", namespaces=NS)
        for runnable in _component(document).xpath(".//ar:RUNNABLES/ar:RUNNABLE-ENTITY", namespaces=NS)
    }


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


class TestOneOperationEvent:
    def test_it_is_emitted_as_an_operation_invoked_event(self, model: Path, tmp_path: Path) -> None:
        _set_events(model, [_event()])

        (event,) = _events(_generate(model, tmp_path / "out.arxml"))

        assert _tag(event) == "OPERATION-INVOKED-EVENT"
        assert _short_name(event) == f"{OPERATION_EVENT_PREFIX}{PORT}_{OPERATION}"

    def test_its_children_are_in_the_schema_order(self, model: Path, tmp_path: Path) -> None:
        _set_events(model, [_event()])

        (event,) = _events(_generate(model, tmp_path / "out.arxml"))

        assert [_tag(child) for child in event] == ["SHORT-NAME", "START-ON-EVENT-REF", "OPERATION-IREF"]
        assert [_tag(child) for child in event.find("ar:OPERATION-IREF", namespaces=NS)] == [
            "CONTEXT-P-PORT-REF",
            "TARGET-PROVIDED-OPERATION-REF",
        ]

    def test_it_starts_its_runnable(self, model: Path, tmp_path: Path) -> None:
        _set_events(model, [_event()])

        (event,) = _events(_generate(model, tmp_path / "out.arxml"))

        assert _reference(event, "START-ON-EVENT-REF") == (f"{BEHAVIOR_PATH}/{RUNNABLE}", "RUNNABLE-ENTITY")

    def test_its_iref_names_the_components_own_port_and_the_interfaces_operation(
        self, model: Path, tmp_path: Path
    ) -> None:
        _set_events(model, [_event()])

        (event,) = _events(_generate(model, tmp_path / "out.arxml"))

        assert _reference(event, "CONTEXT-P-PORT-REF") == (f"/ComponentTypes/{SERVER}/{PORT}", "P-PORT-PROTOTYPE")
        assert _reference(event, "TARGET-PROVIDED-OPERATION-REF") == (
            f"/PortInterfaces/{INTERFACE}/{OPERATION}",
            "CLIENT-SERVER-OPERATION",
        )

    def test_both_references_resolve_inside_the_document(self, model: Path, tmp_path: Path) -> None:
        """The two halves point at elements the same document emits, by their SHORT-NAME paths."""
        _set_events(model, [_event()])
        document = _generate(model, tmp_path / "out.arxml")

        ports = document.xpath(
            "//ar:APPLICATION-SW-COMPONENT-TYPE[ar:SHORT-NAME=$swc]/ar:PORTS/ar:P-PORT-PROTOTYPE/ar:SHORT-NAME/text()",
            namespaces=NS,
            swc=SERVER,
        )
        operations = document.xpath(
            "//ar:CLIENT-SERVER-INTERFACE[ar:SHORT-NAME=$iface]/ar:OPERATIONS/ar:CLIENT-SERVER-OPERATION"
            "/ar:SHORT-NAME/text()",
            namespaces=NS,
            iface=INTERFACE,
        )

        assert PORT in ports
        assert OPERATION in operations

    def test_no_init_event_is_emitted_in_its_place(self, model: Path, tmp_path: Path) -> None:
        """The ``else`` that meant ``init`` is gone at both emission sites."""
        _set_events(model, [_event()])
        output = tmp_path / "out.arxml"

        generate_arxml(str(model), str(output))

        text = output.read_text(encoding="utf-8")
        assert "INIT-EVENT" not in text
        assert "IT_" + RUNNABLE not in text


class TestSeveralOperationsOnOneRunnable:
    def test_each_operation_is_one_event_in_declaration_order(self, model: Path, tmp_path: Path) -> None:
        signature = [
            {"name": "id", "direction": "in", "implementation_type": "uint8"},
            {"name": "value", "direction": "out", "implementation_type": "uint8"},
        ]
        _add_operation(model, "ReadOther", signature)
        _set_events(model, [_event(), _event(operation="ReadOther")])

        events = _events(_generate(model, tmp_path / "out.arxml"))

        assert [(_tag(event), _short_name(event)) for event in events] == [
            ("OPERATION-INVOKED-EVENT", f"OIT_{PORT}_{OPERATION}"),
            ("OPERATION-INVOKED-EVENT", f"OIT_{PORT}_ReadOther"),
        ]
        assert [_reference(event, "TARGET-PROVIDED-OPERATION-REF")[0] for event in events] == [
            f"/PortInterfaces/{INTERFACE}/{OPERATION}",
            f"/PortInterfaces/{INTERFACE}/ReadOther",
        ]
        assert {_reference(event, "START-ON-EVENT-REF")[0] for event in events} == {f"{BEHAVIOR_PATH}/{RUNNABLE}"}


class TestConcurrency:
    def test_the_default_is_emitted_as_false(self, model: Path, tmp_path: Path) -> None:
        assert _concurrency(_generate(model, tmp_path / "out.arxml")) == {RUNNABLE: "false"}

    def test_concurrent_true_is_emitted_as_true(self, model: Path, tmp_path: Path) -> None:
        """A source ``true`` is emitted as ``true``, not a constant ``false``."""
        change(model, SERVER_FILE, lambda d: d["runnables"][0].update(concurrent=True))

        assert _concurrency(_generate(model, tmp_path / "out.arxml")) == {RUNNABLE: "true"}

    def test_it_is_per_runnable(self, model: Path, tmp_path: Path) -> None:
        _add_runnable(model, "Other", [])
        change(model, SERVER_FILE, lambda d: d["runnables"][1].update(concurrent=True))

        assert _concurrency(_generate(model, tmp_path / "out.arxml")) == {RUNNABLE: "false", "Other": "true"}


class TestTheName:
    def test_two_pairs_deriving_one_base_are_suffixed_by_the_shared_counter(self, tmp_path: Path) -> None:
        """``(p_A, B)`` and ``(p, A_B)`` both derive ``OIT_p_A_B``: the retroactive counter makes ``_0`` / ``_1``."""
        root = every_event_shape_model(tmp_path / "model")

        names = [_short_name(event) for event in _events(_generate(root, tmp_path / "out.arxml"))]

        assert "OIT_p_A_B" not in names
        assert names.count("OIT_p_A_B_0") == 1
        assert names.count("OIT_p_A_B_1") == 1
        assert len(names) == len(set(names))

    def test_a_pair_on_the_joint_budget_emits_exactly_the_limit(self, tmp_path: Path) -> None:
        root = every_event_shape_model(tmp_path / "model")

        names = [_short_name(event) for event in _events(_generate(root, tmp_path / "out.arxml"))]

        boundary = f"OIT_{BOUNDARY_PORT}_{BOUNDARY_OPERATION}"
        assert len(boundary) == MAX_IDENTIFIER_LENGTH
        assert boundary in names

    def test_an_operation_event_does_not_collide_with_the_timing_event_of_its_runnable(
        self, model: Path, tmp_path: Path
    ) -> None:
        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert names == [f"TMT_{RUNNABLE}", f"OIT_{PORT}_{OPERATION}"]


class TestTheMixedEventFixture:
    """Generated as written, with no refusal and no export gate, never labelled RTE-conformant."""

    def test_it_generates_without_a_refusal(self, model: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.arxml"

        generate_arxml(str(model), str(output))

        assert output.is_file()

    def test_both_events_start_the_one_runnable(self, model: Path, tmp_path: Path) -> None:
        events = _events(_generate(model, tmp_path / "out.arxml"))

        assert [_tag(event) for event in events] == ["TIMING-EVENT", "OPERATION-INVOKED-EVENT"]
        assert {_reference(event, "START-ON-EVENT-REF")[0] for event in events} == {f"{BEHAVIOR_PATH}/{RUNNABLE}"}

    def test_check_model_reports_nothing_about_it(self, model: Path) -> None:
        assert check_cross_layer_consistency(str(model)).records == ()


class TestEveryEventShape:
    def test_the_positive_fixture_is_clean_under_check_model(self, tmp_path: Path) -> None:
        assert check_cross_layer_consistency(str(every_event_shape_model(tmp_path / "model"))).records == ()

    def test_every_kind_is_emitted(self, tmp_path: Path) -> None:
        root = every_event_shape_model(tmp_path / "model")

        tags = [_tag(event) for event in _events(_generate(root, tmp_path / "out.arxml"))]

        assert tags.count("TIMING-EVENT") == 1
        assert tags.count("INIT-EVENT") == 1
        assert tags.count("OPERATION-INVOKED-EVENT") == 6

    def test_the_concurrent_runnable_alone_says_true(self, tmp_path: Path) -> None:
        root = every_event_shape_model(tmp_path / "model")

        concurrency = _concurrency(_generate(root, tmp_path / "out.arxml"))

        assert {name for name, value in concurrency.items() if value == "true"} == {"Serve"}


# ---------------------------------------------------------------------------
# The refusal matrix: every class check-model reports, through the same predicate
# ---------------------------------------------------------------------------


def _unknown_port(root: Path) -> None:
    _set_events(root, [TIMING_EVENT, _event(port="pMissing")])


def _interface_kind_mismatch(root: Path) -> None:
    put(
        root,
        "interfaces/IData.yaml",
        {
            "name": "IData",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint8"}],
        },
    )
    _add_port(root, "pData", "provide", "IData")
    _set_events(root, [TIMING_EVENT, _event(port="pData", operation="value")])


def _direction_mismatch(root: Path) -> None:
    _add_port(root, "rRecords", "require", INTERFACE)
    _set_events(root, [TIMING_EVENT, _event(port="rRecords")])


def _unknown_operation(root: Path) -> None:
    _set_events(root, [TIMING_EVENT, _event(operation="Nope")])


def _through_an_interface_stub(root: Path) -> None:
    put(
        root,
        "interfaces/IForeign.yaml",
        {
            "name": "IForeign",
            "kind": "external_interface",
            "external_ref": "/Foreign/PortInterfaces/IForeign",
            "external_dest": "CLIENT-SERVER-INTERFACE",
        },
    )
    _add_port(root, "pForeign", "provide", "IForeign")
    _set_events(root, [TIMING_EVENT, _event(port="pForeign")])


def _bound_on_two_runnables(root: Path) -> None:
    _add_runnable(root, "Second", [_event()])


def _bound_twice_on_one(root: Path) -> None:
    _set_events(root, [TIMING_EVENT, _event(), _event()])


def _incompatible_mapped_signatures(root: Path) -> None:
    """``OpB`` takes one argument where ``ReadRecord`` takes two: the argument count differs."""
    _add_operation(root, "OpB", [{"name": "id", "direction": "in", "implementation_type": "uint8"}])
    _set_events(root, [TIMING_EVENT, _event(), _event(operation="OpB")])


def _incompatible_directions(root: Path) -> None:
    _add_operation(
        root,
        "OpB",
        [
            {"name": "id", "direction": "out", "implementation_type": "uint8"},
            {"name": "value", "direction": "out", "implementation_type": "uint8"},
        ],
    )
    _set_events(root, [TIMING_EVENT, _event(), _event(operation="OpB")])


def _incompatible_behind_an_unjudged_first(root: Path) -> None:
    """``OpU``'s one argument is a record whose field is an enumeration, a position no side judges.

    ``OpA`` and ``OpB`` behind it differ in their one argument's base type size, and are compared."""
    write_model_element(root, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
    write_model_element(root, {"name": "RecU", "kind": "record", "fields": [{"name": "f", "type": "Gear"}]})
    write_model_element(
        root,
        {"name": "uint16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
    )
    write_model_element(root, {"name": "uint16", "kind": "value_type", "base": "uint16_base"})
    _add_operation(root, "OpU", [{"name": "v", "direction": "in", "implementation_type": "RecU"}])
    _add_operation(root, "OpA", [{"name": "v", "direction": "in", "implementation_type": "uint8"}])
    _add_operation(root, "OpB", [{"name": "v", "direction": "in", "implementation_type": "uint16"}])
    _set_events(root, [_event(operation="OpU"), _event(operation="OpA"), _event(operation="OpB")])


#: Each negative class, the change that produces it and the rule ``check-model``
#: reports it under — the same predicate the writer calls.
REFUSED_BY_THE_SHARED_PREDICATE = {
    "unknown-port": (_unknown_port, ["swc.operation-event.unknown-port"]),
    "interface-kind-mismatch": (_interface_kind_mismatch, ["swc.operation-event.interface-kind-mismatch"]),
    "direction-mismatch": (_direction_mismatch, ["swc.operation-event.direction-mismatch"]),
    "unknown-operation": (_unknown_operation, ["swc.operation-event.unknown-operation"]),
    "through-an-interface-stub": (_through_an_interface_stub, ["swc.operation-event.unknown-operation"]),
    "duplicate-across-runnables": (_bound_on_two_runnables, ["swc.operation-event.duplicate-binding"]),
    "duplicate-on-one-runnable": (_bound_twice_on_one, ["swc.operation-event.duplicate-binding"]),
    "incompatible-count": (_incompatible_mapped_signatures, ["swc.operation-event.incompatible-signatures"]),
    "incompatible-direction": (_incompatible_directions, ["swc.operation-event.incompatible-signatures"]),
    "incompatible-behind-an-unjudged-first": (
        _incompatible_behind_an_unjudged_first,
        ["swc.operation-event.incompatible-signatures"],
    ),
}


def _refusal(root: Path, tmp_path: Path) -> str:
    """Generate into a fresh directory, which must stay empty; the refusal's text."""
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError) as refusal:
        generate_arxml(str(root), str(out / "model.arxml"))
    assert sorted(entry.name for entry in out.iterdir()) == [], "a refused model must leave no output"
    return str(refusal.value)


class TestTheRefusalMatrix:
    @pytest.mark.parametrize("case", sorted(REFUSED_BY_THE_SHARED_PREDICATE))
    def test_check_model_reports_the_class(self, case: str, model: Path) -> None:
        mutate, rules = REFUSED_BY_THE_SHARED_PREDICATE[case]
        mutate(model)

        assert [record.rule for record in check_operation_events(str(model)).records] == rules

    @pytest.mark.parametrize("case", sorted(REFUSED_BY_THE_SHARED_PREDICATE))
    def test_generate_arxml_refuses_it_and_writes_nothing(self, case: str, model: Path, tmp_path: Path) -> None:
        mutate, _rules = REFUSED_BY_THE_SHARED_PREDICATE[case]
        mutate(model)

        message = _refusal(model, tmp_path)

        # The same predicate: every finding check-model reports is in the refusal, word for word.
        issues = check_operation_events(str(model)).issues
        assert issues, "the case must produce a finding, or the loop below proves nothing"
        for issue in issues:
            assert issue in message
        assert "check-model" in message

    @pytest.mark.parametrize("interface", ["INope", None], ids=["undeclared-interface", "no-interface"])
    def test_an_event_through_a_port_whose_interface_does_not_resolve_is_refused(
        self, interface: str | None, model: Path, tmp_path: Path
    ) -> None:
        """The writer refuses an operation event whose operation path names no interface.

        It is not an operation-event finding -- ``port.undeclared-interface`` owns the port, and
        ``check_operation_events`` skips its events -- but the writer must still refuse, and never write such an
        ``OPERATION-INVOKED-EVENT``."""

        def add_port(document: dict[str, Any]) -> None:
            port: dict[str, Any] = {"name": "pLoose", "direction": "provide"}
            if interface is not None:
                port["interface"] = interface
            document["ports"].append(port)

        change(model, SERVER_FILE, add_port)
        _set_events(model, [TIMING_EVENT, _event(port="pLoose")])
        assert check_operation_events(str(model)).records == ()

        message = _refusal(model, tmp_path)

        assert "pLoose" in message

    def test_the_stub_refusal_names_the_stub(self, model: Path, tmp_path: Path) -> None:
        _through_an_interface_stub(model)

        # The stub lists no operation, so the event names one it does not list.
        assert "external interface stub 'IForeign'" in _refusal(model, tmp_path)

    def test_every_finding_is_named_in_one_refusal(self, model: Path, tmp_path: Path) -> None:
        """Two classes at once: the refusal batches them, as check-model reports both."""
        _unknown_operation(model)
        _add_runnable(model, "Second", [_event(port="pMissing")])

        message = _refusal(model, tmp_path)

        assert "operation 'Nope'" in message
        assert "no port 'pMissing'" in message


class TestTheOverBudgetName:
    """``OIT_{port}_{operation}``: the port and operation share 123 characters before any suffix."""

    @staticmethod
    def _pair(root: Path, port_length: int, operation_length: int) -> tuple[str, str]:
        port = "p" + "P" * (port_length - 1)
        operation = "o" + "O" * (operation_length - 1)
        put(
            root,
            "interfaces/ILong.yaml",
            {"name": "ILong", "kind": "client_server", "operations": [{"name": operation, "arguments": []}]},
        )
        _add_port(root, port, "provide", "ILong")
        _set_events(root, [TIMING_EVENT, _event(port=port, operation=operation)])
        return port, operation

    def test_123_characters_between_them_generate(self, model: Path, tmp_path: Path) -> None:
        port, operation = self._pair(model, 61, 62)

        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert f"OIT_{port}_{operation}" in names

    def test_124_characters_between_them_are_refused_and_write_nothing(self, model: Path, tmp_path: Path) -> None:
        port, operation = self._pair(model, 62, 62)
        emitted = f"OIT_{port}_{operation}"

        message = _refusal(model, tmp_path)

        assert emitted in message
        assert "123 characters between them" in message
        # The access-point pair shape: which of the two to shorten, and to how many (a tie is the operation).
        assert f"so shorten '{operation}' to at most 61 characters" in message
        assert [record.rule for record in check_cross_layer_consistency(str(model)).records] == ["identifier.generated"]

    def test_the_overhead_is_read_off_the_suffixed_name(self, model: Path, tmp_path: Path) -> None:
        """Two pairs deriving one 127-character base: each suffixed name is 129, refused at 121 between them."""
        first_port, first_operation = "p_" + "A" * 60, "B" * 60
        second_port, second_operation = "p", "A" * 60 + "_" + "B" * 60
        assert f"{first_port}_{first_operation}" == f"{second_port}_{second_operation}"
        for name, (port, operation) in {
            "IFirst": (first_port, first_operation),
            "ISecond": (second_port, second_operation),
        }.items():
            put(
                model,
                f"interfaces/{name}.yaml",
                {"name": name, "kind": "client_server", "operations": [{"name": operation, "arguments": []}]},
            )
            _add_port(model, port, "provide", name)
        _set_events(model, [TIMING_EVENT, _event(port=first_port, operation=first_operation)])
        _add_runnable(model, "Second", [_event(port=second_port, operation=second_operation)])
        base = f"OIT_{first_port}_{first_operation}"
        assert len(base) == 127

        message = _refusal(model, tmp_path)

        assert f"'{base}_0'" in message
        assert f"'{base}_1'" in message
        assert "121 characters between them" in message
