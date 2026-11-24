"""The rules resolve a member against an external interface stub's list.

Contract ``8.0.0`` lets an external interface stub list the members its ports
use, by name: ``operations`` on a client-server stub, ``data_elements`` on a
sender-receiver one. Every rule that resolves a member name asks the stub's list
where it asked a declared interface's members --

* ``swc.access.unknown-element`` / ``unknown-operation``,
  ``swc.operation-event.unknown-operation``, ``swc.port.com-spec-unknown-element``
  and ``safety.measure-unknown-element`` accept a listed member and refuse an
  unlisted one, under the same rule id, with a repair that names the stub;
* the kind across a stub is still **not judged**: a
  member of the wrong kind is not in the stub's list of that kind, so the
  member rule names it; the direction rules and the direction half of
  ``swc.port.com-spec-inapplicable`` are judged as for a declared interface;
* the rules that need a member's content abstain: the signature comparison,
  the policy half, the queue rules, the init rules and the ASIL level rules;
* the new ``swc.port.com-spec-on-stub`` refuses an ``init`` and, on a require
  port, a ``queue_length`` on an entry whose element the stub lists, and
  abstains where ``com-spec-unknown-element`` already names the entry.

Every member name here is invented. What this module does **not** reach: what
the writer writes for a listed member and a stub port's derived com-specs
(``test_external_stubs_arxml.py`` reads those); what the importers list
(``test_external_stubs_import.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import validator_package
from external_stubs_support import CONSUMER, PROVIDER, change, external_stubs_model_root, rules
from model_contract_fixtures import load_binding, load_contract
from swcraft_tools.diagnostics import field_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES, diagnose
from swcraft_tools.reader import (
    read_compositions,
    read_hardware,
    read_interfaces,
    read_requirements,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.schema import DataReadRef, ExternalInterface
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    init_values,
    safety_reading,
    stub_member_miss,
    stub_member_names,
)
from swcraft_tools.writer.arxml_generation import _access_point_plan, generate_arxml

CS_STUB = "interfaces/IForeignCS.yaml"
SR_STUB = "interfaces/IForeignSR.yaml"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    """The stub-bearing model, the client-server stub listing ``Get`` and ``Set``, the sender-receiver ``speed``."""
    root = external_stubs_model_root(tmp_path / "project")
    change(root, CS_STUB, lambda d: d.update(operations=["Get", "Set"]))
    change(root, SR_STUB, lambda d: d.update(data_elements=["speed"]))
    assert rules(root) == [], "listing members must leave the stub model one check-model accepts"
    return root


def _records(model_root: Path) -> list[Any]:
    return check_cross_layer_consistency(str(model_root)).records


def _one(model_root: Path, rule: str) -> Any:
    records = _records(model_root)
    assert [record.rule for record in records] == [rule], [(r.rule, r.message) for r in records]
    return records[0]


def _runnable_lists(relative: str, **lists: list[dict[str, str]]) -> Any:
    return lambda root: change(root, relative, lambda d: d["runnables"][0].update(lists))


def _serve(model_root: Path, *events: dict[str, str], relative: str = PROVIDER) -> None:
    """A runnable ``Serve`` on *relative*'s component, started by *events*."""
    change(model_root, relative, lambda d: d["runnables"].append({"name": "Serve", "events": list(events)}))


def _com_specs(model_root: Path, relative: str, port: str, *entries: dict[str, Any]) -> None:
    def mutate(document: dict[str, Any]) -> None:
        (target,) = [entry for entry in document["ports"] if entry["name"] == port]
        target["com_specs"] = list(entries)

    change(model_root, relative, mutate)


# ---------------------------------------------------------------------------
# The two functions every member rule asks
# ---------------------------------------------------------------------------


class TestTheStubsList:
    def test_the_names_are_the_list_of_the_member_kind(self) -> None:
        cs = ExternalInterface(
            name="I", kind="external_interface", external_dest="CLIENT-SERVER-INTERFACE", operations=["Get"]
        )
        assert stub_member_names(cs, "operation") == {"Get"}
        assert stub_member_names(cs, "element") == frozenset()

    def test_the_miss_names_the_list_and_the_repair(self) -> None:
        sr = ExternalInterface(
            name="I", kind="external_interface", external_dest="SENDER-RECEIVER-INTERFACE", data_elements=["a"]
        )
        assert stub_member_miss(sr, "element", "b") == (
            "external interface stub 'I' does not list data element 'b' in its `data_elements`: list it there, "
            "or name a data element the stub lists"
        )

    def test_a_stub_of_another_dest_lists_no_member_of_that_kind(self) -> None:
        mode = ExternalInterface(name="I", kind="external_interface", external_dest="MODE-SWITCH-INTERFACE")
        assert stub_member_miss(mode, "operation", "x") == (
            "external interface stub 'I' has external_dest 'MODE-SWITCH-INTERFACE', which lists no operation: "
            "only a CLIENT-SERVER-INTERFACE stub lists `operations`"
        )


# ---------------------------------------------------------------------------
# swc.access.unknown-element / unknown-operation
# ---------------------------------------------------------------------------


class TestAccessThroughAStub:
    @pytest.mark.parametrize(
        ("relative", "list_name", "entry"),
        [
            (CONSUMER, "reads", {"port": "rSr", "element": "speed"}),
            (PROVIDER, "writes", {"port": "pSr", "element": "speed"}),
            (CONSUMER, "calls", {"port": "rCs", "operation": "Get"}),
        ],
        ids=["read", "write", "call"],
    )
    def test_a_listed_member_resolves(self, model: Path, relative: str, list_name: str, entry: dict[str, str]) -> None:
        _runnable_lists(relative, **{list_name: [entry]})(model)

        assert rules(model) == []

    @pytest.mark.parametrize(
        ("relative", "list_name", "entry", "rule", "listed"),
        [
            (CONSUMER, "reads", {"port": "rSr", "element": "other"}, "swc.access.unknown-element", "`data_elements`"),
            (PROVIDER, "writes", {"port": "pSr", "element": "other"}, "swc.access.unknown-element", "`data_elements`"),
            (CONSUMER, "calls", {"port": "rCs", "operation": "Other"}, "swc.access.unknown-operation", "`operations`"),
        ],
        ids=["read", "write", "call"],
    )
    def test_an_unlisted_member_is_the_member_rule_naming_the_stub(
        self, model: Path, relative: str, list_name: str, entry: dict[str, str], rule: str, listed: str
    ) -> None:
        _runnable_lists(relative, **{list_name: [entry]})(model)

        finding = _one(model, rule)
        member = next(value for key, value in entry.items() if key != "port")
        assert "does not list" in finding.message
        assert listed in finding.message
        assert finding.target.endswith(f".{member}")

    @pytest.mark.parametrize(
        ("list_name", "entry", "rule", "dest"),
        [
            ("reads", {"port": "rCs", "element": "Get"}, "swc.access.unknown-element", "CLIENT-SERVER-INTERFACE"),
            (
                "calls",
                {"port": "rSr", "operation": "speed"},
                "swc.access.unknown-operation",
                "SENDER-RECEIVER-INTERFACE",
            ),
            ("reads", {"port": "rMode", "element": "m"}, "swc.access.unknown-element", "MODE-SWITCH-INTERFACE"),
        ],
        ids=["read-through-cs", "call-through-sr", "read-through-mode"],
    )
    def test_the_kind_across_a_stub_is_still_the_member_rule(
        self, model: Path, list_name: str, entry: dict[str, str], rule: str, dest: str
    ) -> None:
        """A name listed under the other kind is not listed under this one: never the kind rule."""
        _runnable_lists(CONSUMER, **{list_name: [entry]})(model)

        finding = _one(model, rule)
        assert f"has external_dest '{dest}', which lists no" in finding.message

    def test_a_listed_member_through_the_wrong_direction_is_the_direction_rule_alone(self, model: Path) -> None:
        _runnable_lists(CONSUMER, writes=[{"port": "rSr", "element": "speed"}])(model)

        assert rules(model) == ["swc.access.direction-mismatch"]

    def test_an_unlisted_member_through_the_wrong_direction_is_both(self, model: Path) -> None:
        _runnable_lists(CONSUMER, writes=[{"port": "rSr", "element": "other"}])(model)

        assert sorted(rules(model)) == ["swc.access.direction-mismatch", "swc.access.unknown-element"]


# ---------------------------------------------------------------------------
# swc.operation-event.unknown-operation
# ---------------------------------------------------------------------------


class TestOperationEventThroughAStub:
    def test_a_listed_operation_resolves(self, model: Path) -> None:
        _serve(model, {"type": "operation", "port": "pCs", "operation": "Get"})

        assert rules(model) == []

    def test_an_unlisted_operation_is_the_member_rule_naming_the_stub(self, model: Path) -> None:
        _serve(model, {"type": "operation", "port": "pCs", "operation": "Other"})

        finding = _one(model, "swc.operation-event.unknown-operation")
        assert "external interface stub 'IForeignCS' does not list operation 'Other' in its `operations`" in (
            finding.message
        )
        assert finding.target == "IForeignCS.Other"

    def test_a_listed_operation_through_a_require_port_is_the_direction_rule_alone(self, model: Path) -> None:
        _serve(model, {"type": "operation", "port": "rCs", "operation": "Get"}, relative=CONSUMER)

        assert rules(model) == ["swc.operation-event.direction-mismatch"]

    def test_through_a_sender_receiver_stub_is_the_member_rule_never_the_kind_rule(self, model: Path) -> None:
        _serve(model, {"type": "operation", "port": "pSr", "operation": "speed"})

        finding = _one(model, "swc.operation-event.unknown-operation")
        assert "which lists no operation" in finding.message

    def test_the_signature_comparison_abstains_on_listed_operations(self, model: Path) -> None:
        """Two operations of one runnable, both listed: a stub holds no argument, so nothing is compared."""
        _serve(
            model,
            {"type": "operation", "port": "pCs", "operation": "Get"},
            {"type": "operation", "port": "pCs", "operation": "Set"},
        )

        assert rules(model) == []


# ---------------------------------------------------------------------------
# The com-spec rules on a stub port
# ---------------------------------------------------------------------------


class TestComSpecsOnAStubPort:
    @pytest.mark.parametrize(
        "entry",
        [
            {"data_element": "speed", "enable_update": True},
            {"data_element": "speed", "alive_timeout": 0.5},
            {"data_element": "speed", "handle_never_received": True, "handle_timeout_type": "replace"},
            {"data_element": "speed", "handle_out_of_range": "saturate", "uses_end_to_end_protection": True},
        ],
        ids=["enable_update", "alive_timeout", "never-received", "out-of-range"],
    )
    def test_a_representable_attribute_on_a_listed_element_is_accepted(
        self, model: Path, entry: dict[str, Any]
    ) -> None:
        _com_specs(model, CONSUMER, "rSr", entry)

        assert rules(model) == []

    @pytest.mark.parametrize(
        "entry",
        [
            {"data_element": "other"},
            {"data_element": "other", "init": 0, "queue_length": 2},
        ],
        ids=["bare", "with-init-and-queue-length"],
    )
    def test_an_unlisted_element_is_the_unknown_element_rule_alone(self, model: Path, entry: dict[str, Any]) -> None:
        """One root cause: ``com-spec-on-stub`` abstains where the entry names nothing listed."""
        _com_specs(model, CONSUMER, "rSr", entry)

        finding = _one(model, "swc.port.com-spec-unknown-element")
        assert "external interface stub 'IForeignSR' does not list data element 'other'" in finding.message

    @pytest.mark.parametrize(
        ("entry", "stated"),
        [
            ({"data_element": "speed", "init": 0}, ("init",)),
            ({"data_element": "speed", "queue_length": 2}, ("queue_length",)),
            ({"data_element": "speed", "init": 0, "queue_length": 2}, ("init", "queue_length")),
        ],
        ids=["init", "queue_length", "both"],
    )
    def test_init_and_queue_length_on_a_require_port_are_the_on_stub_rule(
        self, model: Path, entry: dict[str, Any], stated: tuple[str, ...]
    ) -> None:
        _com_specs(model, CONSUMER, "rSr", entry)

        finding = _one(model, "swc.port.com-spec-on-stub")
        assert "lists the element by name only" in finding.message
        assert [subject.path[-1] for subject in finding.subjects] == [field_step(name) for name in stated]

    def test_an_init_on_a_provide_port_is_the_on_stub_rule(self, model: Path) -> None:
        _com_specs(model, PROVIDER, "pSr", {"data_element": "speed", "init": 0})

        finding = _one(model, "swc.port.com-spec-on-stub")
        assert "an init value has no type to be shaped against" in finding.message

    @pytest.mark.parametrize(
        "entry",
        [{"data_element": "speed", "queue_length": 2}, {"data_element": "speed", "enable_update": True}],
        ids=["queue_length", "enable_update"],
    )
    def test_a_receiver_attribute_on_a_provide_port_is_the_direction_half_alone(
        self, model: Path, entry: dict[str, Any]
    ) -> None:
        """``queue_length`` on a provide port is the direction half's, and is not named twice."""
        _com_specs(model, PROVIDER, "pSr", entry)

        finding = _one(model, "swc.port.com-spec-inapplicable")
        assert "a provide port's sender com-spec has no place for a receiver attribute" in finding.message

    @pytest.mark.parametrize(("port", "dest"), [("rCs", "CLIENT-SERVER"), ("rMode", "MODE-SWITCH")])
    def test_an_entry_on_a_stub_of_another_dest_is_unknown(self, model: Path, port: str, dest: str) -> None:
        _com_specs(model, CONSUMER, port, {"data_element": "speed"})

        finding = _one(model, "swc.port.com-spec-unknown-element")
        assert f"has external_dest '{dest}-INTERFACE', which lists no data element" in finding.message


# ---------------------------------------------------------------------------
# safety.measure-unknown-element, and the level rules' abstention
# ---------------------------------------------------------------------------


class TestMeasuresOnAStubPort:
    def test_a_listed_key_is_accepted(self, model: Path) -> None:
        change(model, CONSUMER, lambda d: _measure(d, "speed"))

        assert rules(model) == []

    def test_an_unlisted_key_is_the_finding_naming_the_stub(self, model: Path) -> None:
        change(model, CONSUMER, lambda d: _measure(d, "other"))

        finding = _one(model, "safety.measure-unknown-element")
        assert "external interface stub 'IForeignSR' does not list data element 'other'" in finding.message

    def test_no_level_rule_reads_a_listed_key(self, model: Path) -> None:
        """The editing guard's reading abstains with the rules: a stub holds no level."""
        change(model, CONSUMER, lambda d: _measure(d, "speed"))
        root = str(model)
        reading = safety_reading(
            read_swc_model(root),
            read_interfaces(root),
            read_tasks(root),
            read_requirements(root),
            read_hardware(root),
            read_compositions(root),
        )

        assert set(reading.readable(("ExampleConsumerSWC", "rSr"))) == set()


def _measure(document: dict[str, Any], key: str) -> None:
    (port,) = [entry for entry in document["ports"] if entry["name"] == "rSr"]
    port["measures"] = {key: "end-to-end protection"}


# ---------------------------------------------------------------------------
# Pass D
# ---------------------------------------------------------------------------


class TestPassD:
    def test_a_listed_member_is_no_violation(self, model: Path) -> None:
        _runnable_lists(CONSUMER, calls=[{"port": "rCs", "operation": "Get"}])(model)

        assert tuple(diagnose(check_cross_layer_consistency(str(model)))) == ()

    def test_the_on_stub_rule_is_a_consistency_violation(self, model: Path) -> None:
        _com_specs(model, CONSUMER, "rSr", {"data_element": "speed", "init": 0})

        (violation,) = diagnose(check_cross_layer_consistency(str(model)))
        assert violation.error == CONSISTENCY_VIOLATION
        assert RULE_CODES["swc.port.com-spec-on-stub"].code == CONSISTENCY_VIOLATION


# ---------------------------------------------------------------------------
# The writer's twins
# ---------------------------------------------------------------------------


def _refused(model_root: Path, tmp_path: Path, *fragments: str) -> str:
    output_dir = tmp_path / "fresh-output"
    with pytest.raises(ValueError) as caught:
        generate_arxml(str(model_root), str(output_dir / "model.arxml"))
    message = str(caught.value)
    for fragment in fragments:
        assert fragment in message, message
    assert not output_dir.exists(), "a refused generation wrote something"
    return message


class TestTheWritersTwins:
    def test_an_unlisted_access_is_refused_under_the_member_rule(self, model: Path, tmp_path: Path) -> None:
        """An unlisted member on a stub that lists another (``speed``)."""
        _runnable_lists(CONSUMER, reads=[{"port": "rSr", "element": "other"}])(model)

        _refused(model, tmp_path, "does not list data element 'other'", "swc.access.unknown-element")

    def test_a_listed_member_through_the_wrong_direction_is_refused(self, model: Path, tmp_path: Path) -> None:
        """The writer's twin of the direction rule: a write needs a provide port, and ``rSr`` is a require port."""
        _runnable_lists(CONSUMER, writes=[{"port": "rSr", "element": "speed"}])(model)

        message = _refused(model, tmp_path, "which is a require port; `writes` needs a provide port")
        assert "does not list" not in message

    def test_an_unlisted_operation_event_is_refused_in_check_models_words(self, model: Path, tmp_path: Path) -> None:
        _serve(model, {"type": "operation", "port": "pCs", "operation": "Other"})

        _refused(model, tmp_path, "does not list operation 'Other'")

    def test_the_on_stub_rule_is_refused_in_its_own_words(self, model: Path, tmp_path: Path) -> None:
        _com_specs(model, CONSUMER, "rSr", {"data_element": "speed", "init": 0})

        _refused(model, tmp_path, "rule:swc.port.com-spec-on-stub")

    def test_a_port_on_a_stub_that_lists_a_member_is_written(self, model: Path, tmp_path: Path) -> None:
        """The member rules accept the model, and so does the writer: it writes the list's com-specs."""
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(model), str(output))

        text = output.read_text(encoding="utf-8")
        assert "/Foreign/PortInterfaces/IForeignCS/Get" in text
        assert "/Foreign/PortInterfaces/IForeignSR/speed" in text

    def test_a_listed_access_is_planned_under_the_stub(self, model: Path) -> None:
        """The access emitter's own arm: a listed member is planned, through the stub, under its derived name."""
        swc = next(swc for swc in read_swc_model(str(model)) if swc.name == "ExampleConsumerSWC")
        stub = next(iface for iface in read_interfaces(str(model)) if iface.name == "IForeignSR")
        runnable = swc.runnables[0].model_copy(update={"reads": [DataReadRef(port="rSr", element="speed")]})
        port = next(port for port in swc.ports if port.name == "rSr")

        ((container, name, planned_port, iface, member),) = _access_point_plan(swc, runnable, {"rSr": (port, stub)})
        assert (container, name, planned_port, iface, member) == (
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "R_rSr_speed",
            port,
            stub,
            "speed",
        )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

_MEMBER_RULES = {
    "rule:swc.access.unknown-element": "field:interfaces.external_interface.data_elements",
    "rule:swc.access.unknown-operation": "field:interfaces.external_interface.operations",
    "rule:swc.operation-event.unknown-operation": "field:interfaces.external_interface.operations",
    "rule:swc.port.com-spec-unknown-element": "field:interfaces.external_interface.data_elements",
    "rule:safety.measure-unknown-element": "field:interfaces.external_interface.data_elements",
}


class TestTheContract:
    @pytest.mark.parametrize(("rule", "field"), sorted(_MEMBER_RULES.items()))
    def test_each_member_rule_names_the_list_it_reads(self, rule: str, field: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}[rule]
        assert field in declared["appliesTo"]
        assert "no name the stub lists" in declared["summary"]
        assert CONSISTENCY_RULES[rule.removeprefix("rule:")] == declared["summary"]

    def test_the_inapplicable_rule_names_the_list_it_resolves_through(self) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}["rule:swc.port.com-spec-inapplicable"]
        assert "field:interfaces.external_interface.data_elements" in declared["appliesTo"]

    def test_the_on_stub_rule_is_declared_bound_and_diagnosed(self) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}["rule:swc.port.com-spec-on-stub"]
        assert declared["family"] == "cross-element"
        assert CONSISTENCY_RULES["swc.port.com-spec-on-stub"] == declared["summary"]
        # The step of `port_com_spec_records` that mints it, which the predicate reaches.
        assert load_binding()["rules"]["rule:swc.port.com-spec-on-stub"] == {
            "module": "swcraft_tools.validator.init_values",
            "callable": "_com_spec_entry_records",
        }
        assert "_com_spec_entry_records" in validator_package.reached_functions(init_values, "port_com_spec_records")
        assert "swc.port.com-spec-on-stub" in RULE_CODES
