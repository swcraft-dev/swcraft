"""`check_operation_events` and the `OIT_` form: operation events read against the model.

An operation event names the component's own **provide** port and an operation of that port's **client/server**
interface; ``schema.py`` refuses only a blank name. This module covers:

* **the resolution family**, in ``swc.access.*``'s shape: ``unknown-port`` (one finding per unresolved port,
  carrying every event naming it, nothing further for any of them), ``interface-kind-mismatch`` (instead of an
  operation finding), ``direction-mismatch`` (an independent fact) and ``unknown-operation``; a port on an external
  interface stub is decided before any kind prose or member lookup;
* **``duplicate-binding``**: one ``(port, operation)`` pair named by two events of one component;
* **``incompatible-signatures``**: a *partial* ``[constr_2000]`` over the operations one runnable serves (argument
  count, direction, each position's resolved implementation type). A position is judged only when both types
  resolve completely, an application type or enumeration only through exactly one mapped implementation type;
  anything else is *not judged*, never *compatible*;
* **the ``OIT_{port}_{operation}`` form**: its port and operation names share 123 characters before the writer's
  collision suffix, reported under ``identifier.generated`` with the event as the address, and its element-writer
  and pass D twins.

Mixed event kinds are not judged (the RTE specification calls mixing unsupported in explanatory text only).

What this module does **not** reach: the ARXML writer, the importer, component exchange and ``runnable_to_dict``;
possible errors, the return value, ``errorHandling``, ``serverArgumentImplPolicy`` and
``PortDefinedArgumentValues``; an unserved provided operation; any model shape outside the examples below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.registry as reg
import yaml
from conftest import model_element_path, write_model_element
from operation_event_support import (
    INTERFACE,
    INTERFACE_FILE,
    OPERATION,
    OPERATION_EVENT,
    PORT,
    RUNNABLE,
    SERVER,
    SERVER_FILE,
    TIMING_EVENT,
    change,
    load,
    mixed_event_model,
    put,
)
from swcraft_tools import validator
from swcraft_tools.data_description import read_data_description
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.address import FieldStep, MemberStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    INVALID_IDENTIFIER,
    RULE_CODES,
    diagnose,
    emitted_identifier_violations,
)
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.reader import read_interfaces, read_mapping_sets, read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import (
    GENERATED_OPERATION_EVENT_PREFIX,
    ConsistencyIssue,
    check_cross_layer_consistency,
    check_identifier_conformance,
    check_operation_events,
    operation_event_records,
)
from swcraft_tools.validator.compatibility import ResolvedType, _types_compatible
from swcraft_tools.validator.events import _completely_resolved
from swcraft_tools.writer.elements import write_element

PREFIX = "swc.operation-event."

#: Written out rather than derived from the prefix: a rule moving between codes
#: changes what a client is told to repair, and a derivation would follow it.
OPERATION_EVENT_CODES: dict[str, str] = {
    "swc.operation-event.unknown-port": BROKEN_REFERENCE,
    "swc.operation-event.unknown-operation": BROKEN_REFERENCE,
    "swc.operation-event.direction-mismatch": CONSISTENCY_VIOLATION,
    "swc.operation-event.interface-kind-mismatch": CONSISTENCY_VIOLATION,
    "swc.operation-event.duplicate-binding": CONSISTENCY_VIOLATION,
    "swc.operation-event.incompatible-signatures": CONSISTENCY_VIOLATION,
}


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A copy of the mixed-event fixture, with its root composition: clean."""
    return mixed_event_model(tmp_path / "model")


def _event(port: str = PORT, operation: str = OPERATION) -> dict[str, Any]:
    return {"type": "operation", "port": port, "operation": operation}


def _event_subject(event: dict[str, Any], runnable: str = RUNNABLE, swc: str = SERVER) -> Subject:
    return Subject("swc", swc).at(
        field_step("runnables"), member_step(runnable), field_step("events"), member_step(event)
    )


def _port_subject(port: str, field: str, swc: str = SERVER) -> Subject:
    return Subject("swc", swc).at(field_step("ports"), member_step(port), field_step(field))


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
    sided = [_sided(root, argument) for argument in arguments]
    change(root, INTERFACE_FILE, lambda d: d["operations"].append({"name": name, "arguments": sided}))


def _sided(root: Path, argument: dict[str, str]) -> dict[str, str]:
    """*argument* with `implementation_type` moved to `application_type` when it names a declared application type."""
    if not model_element_path(root, argument["implementation_type"], kind="enum").is_file():
        return argument
    return {("application_type" if key == "implementation_type" else key): value for key, value in argument.items()}


def _arguments(*types: str, directions: tuple[str, ...] = ()) -> list[dict[str, str]]:
    return [
        {"name": f"a{index}", "direction": directions[index] if directions else "in", "implementation_type": type_name}
        for index, type_name in enumerate(types)
    ]


def _records(root: Path) -> list[ConsistencyIssue]:
    return list(check_cross_layer_consistency(str(root)).records)


def _ours(root: Path) -> list[ConsistencyIssue]:
    return [record for record in _records(root) if record.rule.startswith(PREFIX)]


def _value_type(root: Path, name: str, base: str = "uint8_base") -> None:
    write_model_element(root, {"name": name, "kind": "value_type", "base": base})


def _uint16(root: Path) -> None:
    write_model_element(
        root,
        {"name": "uint16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
    )
    _value_type(root, "uint16", "uint16_base")


def _two_operations(root: Path, first: list[dict[str, str]], second: list[dict[str, str]]) -> None:
    """``IRecords`` declares ``OpA`` and ``OpB``, and the runnable serves both."""
    _add_operation(root, "OpA", first)
    _add_operation(root, "OpB", second)
    _set_events(root, [TIMING_EVENT, _event(operation="OpA"), _event(operation="OpB")])


# ---------------------------------------------------------------------------
# The mixed-event fixture: no finding, and pass D maps none
# ---------------------------------------------------------------------------


class TestTheMixedEventFixtureIsNotJudged:
    def test_the_runnable_carries_both_kinds(self, model: Path) -> None:
        (runnable,) = load(model, SERVER_FILE)["runnables"]
        assert runnable["events"] == [TIMING_EVENT, OPERATION_EVENT]

    def test_the_committed_tree_declares_no_compositions_layer(self) -> None:
        from operation_event_support import MIXED_EVENT_MODEL_ROOT

        assert not (MIXED_EVENT_MODEL_ROOT / "compositions").exists()

    def test_check_model_reports_nothing(self, model: Path) -> None:
        assert _records(model) == []

    def test_pass_d_maps_nothing(self, model: Path) -> None:
        assert diagnose(check_cross_layer_consistency(str(model))) == ()

    def test_no_rule_names_mixed_event_kinds(self) -> None:
        assert not [rule for rule in validator.CONSISTENCY_RULES if "mixed-event" in rule or "event-kind" in rule]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestTheCheckIsRegistered:
    def test_it_is_composed_last(self, model: Path) -> None:
        """Every operation-event finding follows every finding of three earlier members.

        Read off the aggregate's answer over an access list, a declared name and a
        mapping-set reference. Example-based: it shows the order over these rules only."""

        def break_earlier_rules(document: dict[str, Any]) -> None:
            document["data_type_mappings"] = ["NoSuchSet"]
            runnable = document["runnables"][0]
            runnable["reads"] = [{"port": "ghost", "element": "speed"}]
            document["ports"].append({"name": "p Bad", "direction": "provide", "interface": INTERFACE})

        change(model, SERVER_FILE, break_earlier_rules)
        _set_events(model, [_event(port="nope")])

        assert [record.rule for record in _records(model)] == [
            "swc.access.unknown-port",
            "identifier.declared",
            "swc.unknown-mapping-set",
            "swc.operation-event.unknown-port",
        ]

    def test_every_rule_is_mapped_to_its_code(self) -> None:
        assert {rule: RULE_CODES[rule].code for rule in OPERATION_EVENT_CODES} == OPERATION_EVENT_CODES
        assert {rule for rule in validator.CONSISTENCY_RULES if rule.startswith(PREFIX)} == set(OPERATION_EVENT_CODES)

    def test_the_check_alone_agrees_with_the_aggregate(self, model: Path) -> None:
        _set_events(model, [_event(port="nope")])
        assert list(check_operation_events(str(model)).records) == _ours(model)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestUnknownPort:
    def test_one_finding_per_port_carrying_every_event(self, model: Path) -> None:
        _set_events(model, [TIMING_EVENT, _event(port="nope")])
        _add_runnable(model, "Second", [_event(port="nope", operation="Other")])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.unknown-port"
        assert finding.message == (
            "SWC 'ServerSwc' declares no port 'nope', which the operation events of runnables 'ServeAndTick' and "
            "'Second' name (2 events)."
        )
        assert finding.subjects == (
            _event_subject(_event(port="nope")),
            _event_subject(_event(port="nope", operation="Other"), runnable="Second"),
        )
        assert finding.target == "ServerSwc.nope"

    def test_one_event_is_worded_in_the_singular(self, model: Path) -> None:
        _set_events(model, [_event(port="nope")])
        (finding,) = _ours(model)
        assert finding.message.endswith("which the operation event of runnable 'ServeAndTick' names (1 event).")


class TestInterfaceKindMismatch:
    def test_a_sender_receiver_port_is_reported_instead_of_the_operation(self, model: Path) -> None:
        put(
            model,
            "interfaces/ISignals.yaml",
            {
                "name": "ISignals",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "uint8"}],
            },
        )
        _add_port(model, "pSignals", "provide", "ISignals")
        _set_events(model, [_event(port="pSignals")])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.interface-kind-mismatch"
        assert "interface 'ISignals' is sender/receiver" in finding.message
        assert finding.subjects == (_event_subject(_event(port="pSignals")), _port_subject("pSignals", "interface"))


class TestDirectionMismatch:
    def test_a_require_port_is_reported(self, model: Path) -> None:
        _add_port(model, "rRecords", "require", INTERFACE)
        _set_events(model, [_event(port="rRecords")])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.direction-mismatch"
        assert "which is a require port; an operation event needs a provide port" in finding.message
        assert finding.subjects == (_event_subject(_event(port="rRecords")), _port_subject("rRecords", "direction"))

    def test_it_is_judged_beside_an_unknown_operation(self, model: Path) -> None:
        _add_port(model, "rRecords", "require", INTERFACE)
        _set_events(model, [_event(port="rRecords", operation="Nope")])

        assert [finding.rule for finding in _ours(model)] == [
            "swc.operation-event.direction-mismatch",
            "swc.operation-event.unknown-operation",
        ]


class TestUnknownOperation:
    def test_an_operation_the_interface_does_not_declare(self, model: Path) -> None:
        _set_events(model, [_event(operation="Nope")])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.unknown-operation"
        assert finding.message == (
            "SWC 'ServerSwc' runnable 'ServeAndTick' has an operation event naming operation 'Nope' of port "
            "'pRecords', which interface 'IRecords' does not declare."
        )
        assert finding.subjects == (_event_subject(_event(operation="Nope")),)
        assert finding.target == "IRecords.Nope"

    @pytest.mark.parametrize("dest", ["CLIENT-SERVER-INTERFACE", "SENDER-RECEIVER-INTERFACE"])
    def test_a_port_on_an_interface_stub_is_decided_before_any_kind(self, model: Path, dest: str) -> None:
        """A stub of any ``DEST`` that does not list the operation gets the member rule, never the kind rule."""
        put(
            model,
            "interfaces/IForeign.yaml",
            {
                "name": "IForeign",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IForeign",
                "external_dest": dest,
            },
        )
        _add_port(model, "pForeign", "provide", "IForeign")
        _set_events(model, [_event(port="pForeign")])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.unknown-operation"
        assert "external interface stub 'IForeign'" in finding.message
        assert finding.target == "IForeign.ReadRecord"

    def test_a_require_port_on_a_stub_is_judged_for_its_direction_too(self, model: Path) -> None:
        put(
            model,
            "interfaces/IForeign.yaml",
            {
                "name": "IForeign",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IForeign",
                "external_dest": "CLIENT-SERVER-INTERFACE",
            },
        )
        _add_port(model, "rForeign", "require", "IForeign")
        _set_events(model, [_event(port="rForeign")])

        assert [finding.rule for finding in _ours(model)] == [
            "swc.operation-event.direction-mismatch",
            "swc.operation-event.unknown-operation",
        ]


class TestWhatTheResolutionDefersTo:
    def test_a_port_naming_no_declared_interface_is_the_port_rules(self, model: Path) -> None:
        _add_port(model, "pLost", "provide", "INope")
        _set_events(model, [_event(port="pLost")])

        assert [record.rule for record in _records(model)] == ["port.undeclared-interface"]

    def test_a_duplicated_port_name_resolves_to_the_first(self, model: Path) -> None:
        _add_port(model, PORT, "require", INTERFACE)
        rules = [record.rule for record in _ours(model)]
        assert rules == []


# ---------------------------------------------------------------------------
# duplicate-binding
# ---------------------------------------------------------------------------


class TestDuplicateBinding:
    def test_a_pair_bound_on_two_runnables(self, model: Path) -> None:
        _add_runnable(model, "Second", [OPERATION_EVENT])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.duplicate-binding"
        assert finding.message == (
            "SWC 'ServerSwc' binds operation 'ReadRecord' of port 'pRecords' in 2 operation events, on runnables "
            "'ServeAndTick' and 'Second'; a call starts one runnable, so each (port, operation) pair is named once."
        )
        assert finding.subjects == (_event_subject(OPERATION_EVENT), _event_subject(OPERATION_EVENT, "Second"))

    def test_a_pair_repeated_on_one_runnable(self, model: Path) -> None:
        _set_events(model, [TIMING_EVENT, OPERATION_EVENT, OPERATION_EVENT])

        (finding,) = _ours(model)

        assert finding.rule == "swc.operation-event.duplicate-binding"
        assert "in 2 operation events, twice on runnable 'ServeAndTick'" in finding.message

    def test_it_is_decided_on_the_names_whether_or_not_they_resolve(self, model: Path) -> None:
        """An unresolved port named twice yields both ``unknown-port`` and a duplicate: neither swallows the other."""
        _set_events(model, [TIMING_EVENT, _event(port="nope", operation="X"), _event(port="nope", operation="X")])

        findings = {finding.rule: finding for finding in _ours(model)}

        assert set(findings) == {"swc.operation-event.unknown-port", "swc.operation-event.duplicate-binding"}
        assert (
            "binds operation 'X' of port 'nope' in 2 operation events"
            in findings["swc.operation-event.duplicate-binding"].message
        )

    def test_an_unknown_operation_bound_twice_is_a_duplicate_too(self, model: Path) -> None:
        """A resolved port with an unknown operation bound twice is still a duplicate."""
        _set_events(model, [TIMING_EVENT, _event(operation="Nope"), _event(operation="Nope")])

        rules = {finding.rule for finding in _ours(model)}

        assert rules == {"swc.operation-event.duplicate-binding", "swc.operation-event.unknown-operation"}

    def test_two_pairs_deriving_one_event_name_are_no_duplicate(self, model: Path) -> None:
        _add_port(model, "p_R", "provide", INTERFACE)
        _add_port(model, "p", "provide", INTERFACE)
        _add_operation(model, "R_X", [])
        _add_operation(model, "X", [])
        _set_events(model, [_event(port="p_R", operation="X")])
        _add_runnable(model, "Second", [_event(port="p", operation="R_X")])

        assert _ours(model) == []


# ---------------------------------------------------------------------------
# incompatible-signatures
# ---------------------------------------------------------------------------


def _signature_findings(root: Path) -> list[ConsistencyIssue]:
    return [record for record in _ours(root) if record.rule == "swc.operation-event.incompatible-signatures"]


class TestIncompatibleSignaturesJudged:
    def test_compatible_differently_named_types_are_accepted(self, model: Path) -> None:
        """Three operations with types named three ways and one structure are accepted."""
        _value_type(model, "byte_t")
        write_model_element(model, {"name": "ref_byte", "kind": "reference_type", "base": "byte_t"})
        _add_operation(model, "OpA", _arguments("uint8", "uint8", directions=("in", "out")))
        _add_operation(model, "OpB", _arguments("byte_t", "ref_byte", directions=("in", "out")))
        _set_events(model, [_event(), _event(operation="OpA"), _event(operation="OpB")])

        assert _records(model) == []

    def test_argument_count(self, model: Path) -> None:
        _two_operations(model, _arguments("uint8"), _arguments("uint8", "uint8"))

        (finding,) = _signature_findings(model)

        assert "'IRecords.OpA' vs 'IRecords.OpB' — argument count 1 vs 2" in finding.message
        assert finding.subjects == (_event_subject(_event(operation="OpA")), _event_subject(_event(operation="OpB")))

    def test_direction(self, model: Path) -> None:
        _two_operations(model, _arguments("uint8"), _arguments("uint8", directions=("out",)))
        (finding,) = _signature_findings(model)
        assert "argument 1 direction in vs out" in finding.message

    def test_structure(self, model: Path) -> None:
        _uint16(model)
        _two_operations(model, _arguments("uint8"), _arguments("uint16"))
        (finding,) = _signature_findings(model)
        assert "argument 1 type 'uint8' vs 'uint16' — base type size 8 vs 16" in finding.message

    def test_every_operation_is_compared_with_the_first(self, model: Path) -> None:
        _uint16(model)
        _add_operation(model, "OpA", _arguments("uint16", "uint8", directions=("in", "out")))
        _set_events(model, [_event(), _event(operation="OpA")])
        (finding,) = _signature_findings(model)
        assert "'IRecords.ReadRecord' vs 'IRecords.OpA'" in finding.message

    def test_a_judged_position_after_an_unjudged_one_is_still_judged(self, model: Path) -> None:
        _uint16(model)
        _two_operations(model, _arguments("Undeclared", "uint8"), _arguments("uint8", "uint16"))
        (finding,) = _signature_findings(model)
        assert "argument 2 type 'uint8' vs 'uint16'" in finding.message

    def test_an_unjudged_first_operation_does_not_shield_the_later_ones(self, model: Path) -> None:
        """An unjudged first operation differs from nothing; the later two are judged against each other."""
        _uint16(model)
        _add_operation(model, "OpUnjudged", _arguments("Undeclared"))
        _add_operation(model, "OpA", _arguments("uint8"))
        _add_operation(model, "OpB", _arguments("uint16"))
        _set_events(model, [_event(operation="OpUnjudged"), _event(operation="OpA"), _event(operation="OpB")])

        (finding,) = _signature_findings(model)

        assert "'IRecords.OpA' vs 'IRecords.OpB' — argument 1 type 'uint8' vs 'uint16'" in finding.message
        assert finding.subjects == (_event_subject(_event(operation="OpA")), _event_subject(_event(operation="OpB")))

    def test_one_finding_per_differing_operation_naming_the_earliest_it_differs_from(self, model: Path) -> None:
        """``OpB`` differs from ``OpA`` and ``OpC``, which agree: one finding, ``OpB`` against ``OpA``."""
        _uint16(model)
        _add_operation(model, "OpA", _arguments("uint8"))
        _add_operation(model, "OpB", _arguments("uint16"))
        _add_operation(model, "OpC", _arguments("uint8"))
        _set_events(model, [_event(operation="OpA"), _event(operation="OpB"), _event(operation="OpC")])

        (finding,) = _signature_findings(model)

        assert "'IRecords.OpA' vs 'IRecords.OpB'" in finding.message

    def test_a_later_operation_is_reported_once_against_the_earliest_it_differs_from(self, model: Path) -> None:
        """``OpC`` differs from both ``OpA`` and ``OpB``: one finding for it, against ``OpA``."""
        _uint16(model)
        _add_operation(model, "OpA", _arguments("uint8"))
        _add_operation(model, "OpB", _arguments("uint16"))
        _add_operation(model, "OpC", _arguments("uint8", "uint8"))
        _set_events(model, [_event(operation="OpA"), _event(operation="OpB"), _event(operation="OpC")])

        messages = sorted(finding.message for finding in _signature_findings(model))

        assert len(messages) == 2
        assert "'IRecords.OpA' vs 'IRecords.OpB'" in messages[0]
        assert "'IRecords.OpA' vs 'IRecords.OpC' — argument count 1 vs 2" in messages[1]

    def test_an_operation_reached_through_two_ports_is_compared_once(self, model: Path) -> None:
        """An operation reached through two provide ports is one finding, at the first event reaching it.

        Asked of the check alone: the aggregate drops a repeated message, so it
        could not witness the dedup."""
        _uint16(model)
        _add_port(model, "pRecords2", "provide", INTERFACE)
        _add_operation(model, "OpA", _arguments("uint8"))
        _add_operation(model, "OpB", _arguments("uint16"))
        _set_events(
            model, [_event(operation="OpA"), _event(operation="OpB"), _event(port="pRecords2", operation="OpB")]
        )

        (finding,) = [
            record
            for record in check_operation_events(str(model)).records
            if record.rule == "swc.operation-event.incompatible-signatures"
        ]

        assert "'IRecords.OpA' vs 'IRecords.OpB'" in finding.message
        assert finding.subjects == (_event_subject(_event(operation="OpA")), _event_subject(_event(operation="OpB")))

    def test_an_event_with_a_resolution_finding_takes_no_part(self, model: Path) -> None:
        _add_port(model, "rRecords", "require", INTERFACE)
        _add_operation(model, "OpA", _arguments("uint8"))
        _set_events(model, [OPERATION_EVENT, _event(port="rRecords", operation="OpA")])
        assert [record.rule for record in _ours(model)] == ["swc.operation-event.direction-mismatch"]

    def test_a_mapped_enumeration_is_judged(self, model: Path) -> None:
        _uint16(model)
        write_model_element(model, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        write_model_element(
            model, {"name": "Maps", "maps": [{"application": "Gear", "implementation": "uint8"}]}, layer="mapping_sets"
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(model, _arguments("Gear"), _arguments("uint16"))

        (finding,) = _signature_findings(model)
        assert "argument 1 type 'uint8' vs 'uint16'" in finding.message

    def test_a_mapped_enumeration_against_its_implementation_type_is_compatible(self, model: Path) -> None:
        write_model_element(model, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        write_model_element(
            model, {"name": "Maps", "maps": [{"application": "Gear", "implementation": "uint8"}]}, layer="mapping_sets"
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(model, _arguments("Gear"), _arguments("uint8"))
        assert _signature_findings(model) == []

    def test_a_mapped_application_type_is_judged(self, model: Path) -> None:
        _uint16(model)
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(
            model,
            {"name": "Maps", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))

        (finding,) = _signature_findings(model)
        assert "argument 1 type 'uint16' vs 'uint8' — base type size 16 vs 8" in finding.message


class TestIncompatibleSignaturesNotJudged:
    """Each case would be a finding if judged; each is a position the check abstains on."""

    def test_a_nested_abstention(self, model: Path) -> None:
        """A record field typed by an enumeration is not judged.

        ``_types_compatible`` asked directly would answer *kind enum vs value_type*;
        the completeness pass abstains before it asks.
        """
        write_model_element(model, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        # A record field names an application type, so the other side's member is
        # one too -- asked directly, *kind enum vs application_type*.
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(model, {"name": "RecA", "kind": "record", "fields": [{"name": "f", "type": "Gear"}]})
        write_model_element(model, {"name": "RecB", "kind": "record", "fields": [{"name": "f", "type": "Speed"}]})
        _two_operations(model, _arguments("RecA"), _arguments("RecB"))

        assert _signature_findings(model) == []

    def test_the_nested_control_is_judged(self, model: Path) -> None:
        """The nested control (two implementation types in implementation records) is judged."""
        _uint16(model)
        write_model_element(
            model, {"name": "RecA", "kind": "implementation_record", "elements": [{"name": "f", "type": "uint8"}]}
        )
        write_model_element(
            model, {"name": "RecB", "kind": "implementation_record", "elements": [{"name": "f", "type": "uint16"}]}
        )
        _two_operations(model, _arguments("RecA"), _arguments("RecB"))

        (finding,) = _signature_findings(model)
        assert "implementation record element 'f'" in finding.message

    def test_an_unresolved_type(self, model: Path) -> None:
        _two_operations(model, _arguments("Undeclared"), _arguments("uint8"))
        assert _signature_findings(model) == []

    def test_an_unjudged_position_alone_yields_nothing_across_many_operations(self, model: Path) -> None:
        """Three operations whose every pair meets only an unjudged position yield no finding."""
        _add_operation(model, "OpA", _arguments("Undeclared"))
        _add_operation(model, "OpB", _arguments("uint8"))
        _add_operation(model, "OpC", _arguments("Undeclared"))
        _set_events(model, [_event(operation="OpA"), _event(operation="OpB"), _event(operation="OpC")])
        assert _signature_findings(model) == []

    def test_an_external_type_stub(self, model: Path) -> None:
        write_model_element(
            model,
            {
                "name": "ForeignT",
                "kind": "external_type",
                "external_ref": "/Foreign/ImplementationDataTypes/ForeignT",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        _uint16(model)
        _two_operations(model, _arguments("ForeignT"), _arguments("uint16"))
        assert _signature_findings(model) == []

    def test_an_unmapped_application_type(self, model: Path) -> None:
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))

        rules = [record.rule for record in _records(model)]
        assert "data-type.application-type-unmapped" in rules
        assert _signature_findings(model) == []

    def test_an_ambiguously_mapped_application_type(self, model: Path) -> None:
        _uint16(model)
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(
            model, {"name": "Maps", "maps": [{"application": "Speed", "implementation": "uint8"}]}, layer="mapping_sets"
        )
        write_model_element(
            model,
            {"name": "Wide", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps", "Wide"]))
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))

        rules = [record.rule for record in _records(model)]
        assert "data-type.application-type-double-mapped" in rules
        assert _signature_findings(model) == []

    def test_a_component_naming_an_undeclared_set(self, model: Path) -> None:
        _uint16(model)
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(
            model,
            {"name": "Maps", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps", "Missing"]))
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))
        assert _signature_findings(model) == []


def _core_over(root: Path) -> list[ConsistencyIssue]:
    """:func:`operation_event_records` over the four layers the disk check reads, read here."""
    model_root = str(root)
    return operation_event_records(
        read_swc_model(model_root),
        read_interfaces(model_root),
        read_data_description(model_root),
        read_mapping_sets(model_root),
    )


class TestTheInMemoryCoreIsTheDiskCheck:
    """The disk check is its in-memory core over what it reads.

    ``import component --update`` asks :func:`operation_event_records` before
    anything is written; ``check-model`` and the writer ask
    :func:`check_operation_events` of a model root. Each case pins the two to one answer.
    """

    def test_a_clean_model(self, model: Path) -> None:
        assert _core_over(model) == []
        assert list(check_operation_events(str(model)).records) == _core_over(model)

    def test_an_unknown_port_and_a_duplicate_binding(self, model: Path) -> None:
        _set_events(model, [_event(port="nope"), OPERATION_EVENT, OPERATION_EVENT])
        rules = [record.rule for record in _core_over(model)]
        assert "swc.operation-event.unknown-port" in rules
        assert "swc.operation-event.duplicate-binding" in rules
        assert list(check_operation_events(str(model)).records) == _core_over(model)

    def test_an_interface_kind_mismatch(self, model: Path) -> None:
        put(
            model,
            "interfaces/ISignals.yaml",
            {
                "name": "ISignals",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "uint8"}],
            },
        )
        _add_port(model, "pSignals", "provide", "ISignals")
        _set_events(model, [_event(port="pSignals")])
        assert [record.rule for record in _core_over(model)] == ["swc.operation-event.interface-kind-mismatch"]
        assert list(check_operation_events(str(model)).records) == _core_over(model)

    def test_an_incompatible_signature_through_an_application_mapping(self, model: Path) -> None:
        """The data description and the mapping sets both reach the core."""
        _uint16(model)
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(
            model,
            {"name": "Maps", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))

        (finding,) = [r for r in _core_over(model) if r.rule == "swc.operation-event.incompatible-signatures"]
        assert "argument 1 type 'uint16' vs 'uint8' — base type size 16 vs 8" in finding.message
        assert list(check_operation_events(str(model)).records) == _core_over(model)

    def test_without_the_mapping_set_the_core_does_not_judge_the_position(self, model: Path) -> None:
        """The mapping sets are an input of the core: withheld, the position is not judged."""
        _uint16(model)
        write_model_element(model, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(
            model,
            {"name": "Maps", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        change(model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(model, _arguments("Speed"), _arguments("uint8"))
        model_root = str(model)

        records = operation_event_records(
            read_swc_model(model_root), read_interfaces(model_root), read_data_description(model_root), []
        )
        assert [r for r in records if r.rule == "swc.operation-event.incompatible-signatures"] == []

    def test_the_first_declared_interface_answers(self, model: Path) -> None:
        """The first declared interface of a name answers; a second is not read."""
        _set_events(model, [OPERATION_EVENT])
        model_root = str(model)
        interfaces = read_interfaces(model_root)
        declared = next(interface for interface in interfaces if interface.name == INTERFACE)
        emptied = declared.model_copy(update={"operations": []})
        swcs = read_swc_model(model_root)
        description = read_data_description(model_root)
        sets = read_mapping_sets(model_root)

        assert operation_event_records(swcs, [*interfaces, emptied], description, sets) == []
        assert [r.rule for r in operation_event_records(swcs, [emptied, *interfaces], description, sets)] == [
            "swc.operation-event.unknown-operation"
        ]


class TestTheExistingComparisonIsUnchanged:
    """``_types_compatible`` keeps its abstentions; the new check asks it only after completeness."""

    def test_its_internal_abstentions_still_answer_none(self) -> None:
        assert _types_compatible(ResolvedType("value_type"), ResolvedType("value_type")) is None
        assert _types_compatible(ResolvedType("application_type"), ResolvedType("application_type")) is None
        assert _types_compatible(ResolvedType("array"), ResolvedType("array")) is None

    def test_the_completeness_pass_refuses_a_physical_type_at_any_depth(self) -> None:
        """An ``application_type`` or ``enum`` is never judged, at the top or nested in a record or array.

        The two other abstentions are states ``resolve_type`` never builds."""
        for shape in ("application_type", "enum"):
            physical = ResolvedType(shape)
            assert _completely_resolved(physical) is False, shape
            record = ResolvedType("record", fields=(("f", "T", physical),))
            assert _completely_resolved(record) is False, shape
            array = ResolvedType("array", element=("T", physical), length=2)
            assert _completely_resolved(array) is False, shape


# ---------------------------------------------------------------------------
# The OIT_ form: the event-kind switch and the pair-shaped identifier loop
# ---------------------------------------------------------------------------


def _long_pair(total: int) -> tuple[str, str]:
    port = "P" * (total // 2)
    return port, "O" * (total - len(port))


def _long_model(model: Path, pairs: list[tuple[str, str, str]]) -> None:
    """Every ``(runnable, port, operation)`` bound; each port and operation declared."""

    def mutate_interface(document: dict[str, Any]) -> None:
        for _runnable, _port, operation in pairs:
            if operation not in {entry["name"] for entry in document["operations"]}:
                document["operations"].append({"name": operation, "arguments": _arguments("uint8")})

    def mutate_server(document: dict[str, Any]) -> None:
        document["ports"] = [
            {"name": port, "direction": "provide", "interface": INTERFACE}
            for port in dict.fromkeys(port for _runnable, port, _operation in pairs)
        ]
        document["runnables"] = []
        for runnable, port, operation in pairs:
            existing = next((entry for entry in document["runnables"] if entry["name"] == runnable), None)
            if existing is None:
                existing = {"name": runnable, "events": []}
                document["runnables"].append(existing)
            existing["events"].append(_event(port, operation))

    change(model, INTERFACE_FILE, mutate_interface)
    change(model, SERVER_FILE, mutate_server)


class TestTheEventNames:
    def test_an_operation_event_is_named_for_its_pair(self) -> None:
        swc = SWComponent.model_validate(
            {
                "name": "S",
                "type": "application",
                "runnables": [
                    {"name": "R", "events": [TIMING_EVENT, {"type": "init"}, _event("p", "o")]},
                ],
            }
        )
        assert validator.derived_event_names(swc) == ["TMT_R", "IT_R", "OIT_p_o"]
        assert GENERATED_OPERATION_EVENT_PREFIX == "OIT_"

    def test_two_pairs_deriving_one_base_are_suffixed(self) -> None:
        swc = SWComponent.model_validate(
            {
                "name": "S",
                "type": "application",
                "runnables": [
                    {"name": "R1", "events": [_event("A_B", "C")]},
                    {"name": "R2", "events": [_event("A", "B_C")]},
                ],
            }
        )
        assert validator.derived_event_names(swc) == ["OIT_A_B_C_0", "OIT_A_B_C_1"]


class TestTheOitBudget:
    def test_a_pair_of_123_characters_is_legal(self, model: Path) -> None:
        port, operation = _long_pair(MAX_IDENTIFIER_LENGTH - len("OIT_") - 1)
        _long_model(model, [(RUNNABLE, port, operation)])

        assert list(check_identifier_conformance(str(model)).records) == []

    def test_a_pair_of_124_characters_is_reported_at_the_event(self, model: Path) -> None:
        port, operation = _long_pair(MAX_IDENTIFIER_LENGTH - len("OIT_"))
        _long_model(model, [(RUNNABLE, port, operation)])

        (finding,) = check_identifier_conformance(str(model)).records

        assert finding.rule == "identifier.generated"
        assert f"'OIT_{port}_{operation}' (129 characters)" in finding.message
        assert f"'{port}' and '{operation}' get 123 characters between them" in finding.message
        assert f"operation event '{port}'.'{operation}'" in finding.message
        assert finding.subjects == (_event_subject(_event(port, operation)),)

    def test_the_overhead_is_read_off_the_suffixed_name(self, model: Path) -> None:
        """``(X_Y, Z)`` and ``(X, Y_Z)`` derive one base; the suffix pushes a legal 127 to an illegal 129."""
        head = "X" * 60
        tail = "Z" * 60
        pairs = [("First", f"{head}_Y", tail), ("Second", head, f"Y_{tail}")]
        assert len(f"OIT_{head}_Y_{tail}") == MAX_IDENTIFIER_LENGTH - 1
        _long_model(model, pairs)

        findings = check_identifier_conformance(str(model)).records

        assert [finding.rule for finding in findings] == ["identifier.generated", "identifier.generated"]
        first, second = findings
        assert f"'OIT_{head}_Y_{tail}_0' (129 characters)" in first.message
        # 4 for `OIT_`, 1 separator and 2 for `_0`: 121 left between the two names.
        assert f"'{head}_Y' and '{tail}' get 121 characters between them" in first.message
        assert f"'{head}' and 'Y_{tail}' get 121 characters between them" in second.message
        assert first.subjects == (_event_subject(_event(f"{head}_Y", tail), runnable="First"),)
        assert second.subjects == (_event_subject(_event(head, f"Y_{tail}"), runnable="Second"),)

    def test_an_illegal_declared_name_is_reported_once(self, model: Path) -> None:
        _add_port(model, "p Bad", "provide", INTERFACE)
        _set_events(model, [_event(port="p Bad")])
        rules = [finding.rule for finding in check_identifier_conformance(str(model)).records]
        assert rules == ["identifier.declared"]


class TestTheTwoPreWriteGuardsAgree:
    """The element writer's guard and pass D's pre-materialization twin read the same pair."""

    def _component(self, port: str, operation: str) -> SWComponent:
        return SWComponent.model_validate(
            {
                "name": "S",
                "type": "application",
                "ports": [{"name": port, "direction": "provide", "interface": INTERFACE}],
                "runnables": [{"name": "R", "events": [_event(port, operation)]}],
            }
        )

    def test_the_element_writer_refuses_an_over_budget_pair(self, tmp_path: Path) -> None:
        port, operation = _long_pair(MAX_IDENTIFIER_LENGTH - len("OIT_"))
        with pytest.raises(ValueError, match="generated OPERATION-INVOKED-EVENT name"):
            write_element(str(tmp_path), "swc/core/S.yaml", self._component(port, operation), create=True)
        assert not (tmp_path / "swc" / "core" / "S.yaml").exists()

    def test_the_element_writer_writes_a_legal_pair(self, tmp_path: Path) -> None:
        port, operation = _long_pair(MAX_IDENTIFIER_LENGTH - len("OIT_") - 1)
        write_element(str(tmp_path), "swc/core/S.yaml", self._component(port, operation), create=True)
        (written,) = read_swc_model(str(tmp_path))
        assert written.runnables[0].events[0].port == port

    def test_the_pre_materialization_guard_addresses_the_event(self) -> None:
        port, operation = _long_pair(MAX_IDENTIFIER_LENGTH - len("OIT_"))
        component = self._component(port, operation)
        staged = StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})

        (violation,) = emitted_identifier_violations(staged)

        assert violation.error == INVALID_IDENTIFIER
        assert f"OIT_{port}_{operation}" in violation.message
        assert violation.addresses[0].path[-2:] == (
            FieldStep("events"),
            MemberStep(_event(port, operation)),
        )

    def test_the_pre_materialization_guard_reads_the_suffixed_name(self) -> None:
        """Pass D's twin, like ``check-model``, reads the budget off the suffixed name and addresses each event."""
        head, tail = "X" * 60, "Z" * 60
        pairs = [("First", f"{head}_Y", tail), ("Second", head, f"Y_{tail}")]
        component = SWComponent.model_validate(
            {
                "name": "S",
                "type": "application",
                "ports": [{"name": port, "direction": "provide", "interface": INTERFACE} for _r, port, _o in pairs],
                "runnables": [
                    {"name": runnable, "events": [_event(port, operation)]} for runnable, port, operation in pairs
                ],
            }
        )
        staged = StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})

        first, second = emitted_identifier_violations(staged)

        assert f"'OIT_{head}_Y_{tail}_0' (129 characters)" in first.message
        assert f"'OIT_{head}_Y_{tail}_1' (129 characters)" in second.message
        assert f"'{head}_Y' and '{tail}' get 121 characters between them" in first.message
        assert f"'{head}' and 'Y_{tail}' get 121 characters between them" in second.message
        assert first.addresses[0].path[1] == MemberStep("First")
        assert first.addresses[0].path[-1] == MemberStep(_event(f"{head}_Y", tail))
        assert second.addresses[0].path[1] == MemberStep("Second")
        assert second.addresses[0].path[-1] == MemberStep(_event(head, f"Y_{tail}"))


# ---------------------------------------------------------------------------
# Pass D: one protocol test per rule
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The mixed-event model as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "operation-event-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    mixed_event_model(root / "model")
    return root


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "operation event edit", "operations": operations},
        project=str(project),
        review=_approve,
    )


def _events_address(runnable: str = RUNNABLE, member: dict[str, Any] | None = None) -> dict[str, Any]:
    path: list[dict[str, Any]] = [{"field": "runnables"}, {"member": runnable}, {"field": "events"}]
    if member is not None:
        path.append({"member": member})
    return {"layer": "swc", "element": SERVER, "path": path}


def _create_event(event: dict[str, Any], runnable: str = RUNNABLE) -> dict[str, Any]:
    return {"operation": "create", "address": _events_address(runnable, event), "value": event}


def _create(layer: str, element: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": element}, "value": value}


def _create_port(name: str, direction: str, interface: str) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "swc", "element": SERVER, "path": [{"field": "ports"}, {"member": name}]},
        "value": {"name": name, "direction": direction, "interface": interface},
    }


def _create_operation(name: str, arguments: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "interfaces", "element": INTERFACE, "path": [{"field": "operations"}, {"member": name}]},
        "value": {"name": name, "arguments": arguments},
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _event_address(event: dict[str, Any], runnable: str = RUNNABLE) -> dict[str, Any]:
    return _events_address(runnable, event)


_SR = {
    "name": "ISignals",
    "kind": "sender_receiver",
    "data_elements": [{"name": "speed", "implementation_type": "uint8"}],
}

#: Rule -> the change set that breaks exactly it, and the address its violation must carry first.
PROTOCOL_BREAKS: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
    "swc.operation-event.unknown-port": (
        [_create_event(_event(port="nope"))],
        _event_address(_event(port="nope")),
    ),
    "swc.operation-event.direction-mismatch": (
        [_create_port("rRecords", "require", INTERFACE), _create_event(_event(port="rRecords"))],
        _event_address(_event(port="rRecords")),
    ),
    "swc.operation-event.interface-kind-mismatch": (
        [
            _create("interfaces", "ISignals", _SR),
            _create_port("pSignals", "provide", "ISignals"),
            _create_event(_event(port="pSignals")),
        ],
        _event_address(_event(port="pSignals")),
    ),
    "swc.operation-event.unknown-operation": (
        [_create_event(_event(operation="Nope"))],
        _event_address(_event(operation="Nope")),
    ),
    "swc.operation-event.duplicate-binding": (
        [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": SERVER, "path": [{"field": "runnables"}, {"member": "Second"}]},
                "value": {"name": "Second", "events": [OPERATION_EVENT]},
            }
        ],
        _event_address(OPERATION_EVENT),
    ),
    "swc.operation-event.incompatible-signatures": (
        [_create_operation("OpA", _arguments("uint8")), _create_event(_event(operation="OpA"))],
        _event_address(OPERATION_EVENT),
    ),
}


class TestPassDMapsEachFinding:
    def test_every_rule_has_a_break(self) -> None:
        assert set(PROTOCOL_BREAKS) == set(OPERATION_EVENT_CODES)

    @pytest.mark.parametrize("rule", sorted(PROTOCOL_BREAKS))
    def test_the_change_set_is_refused_with_the_rules_code(self, project: Path, rule: str) -> None:
        operations, address = PROTOCOL_BREAKS[rule]
        before = _tree(project / "model")

        response = _change_set(project, operations)

        assert response.get("error") == OPERATION_EVENT_CODES[rule], response
        assert response["addresses"][0] == address, response
        assert "operation event" in response["message"]
        assert _tree(project / "model") == before

    def test_the_mixed_event_shape_commits(self, project: Path) -> None:
        """A compatible operation event beside the timing event commits."""
        response = _change_set(
            project,
            [
                _create_operation("OpA", _arguments("uint8", "uint8", directions=("in", "out"))),
                _create_event(_event(operation="OpA")),
            ],
        )
        assert response["status"] == "committed", response
        events = load(project / "model", SERVER_FILE)["runnables"][0]["events"]
        assert events == [TIMING_EVENT, OPERATION_EVENT, _event(operation="OpA")]
