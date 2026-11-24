"""Contract ``8.0.0``'s nine optional fields: a stated behaviour, event and access point name, and stub member lists.

The fields:

* a component an optional ``behavior_name``, and each timing, init and operation
  event and each read, write and call an optional ``name`` -- the SHORT-NAME the
  source stated, ``None`` meaning *derived*;
* an external interface stub an ``operations`` and a ``data_elements`` list --
  the **names** of the members its ports use, and nothing of their content.

This module holds the declaration half: the contract declares the nine fields and
the three element-local rules over the two lists; each field loads with its default and with a value;
an event's stated name joins its whole-value identity and a default does not --
through the reduction and through the three identities the validator and pass D
build by hand; the model writers write each field and leave it out unset, the
hand-written dumps equal to the generic one; a list is refused with a blank or
repeated name, with a name that is not an identifier (``check-model`` and pass D),
and on a stub whose ``DEST`` does not admit it; a mode-switch stub lists nothing;
an unknown key on a stub is still refused; and the two sites that read a
``data_elements`` attribute without asking the interface's kind read a stub's
list as no declared element.

What it does not reach: how the writer, the rules and the importers read the
fields -- the name rules, the member rules, the stated names in ARXML, the stub
members' paths and com-specs, and what the importers state and list are other
suites' (``test_stated_name_rules.py``, ``test_stub_member_rules.py``,
``test_stated_names_writer.py``, ``test_stated_names_import.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from model_contract_fixtures import load_binding, load_contract
from pydantic import ValidationError
from swcraft_tools.arxml_import.report import interface_to_dict
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep, member_identity_of, reduce_whole_value
from swcraft_tools.editing.diagnosis import _interface_identifiers, _operation_event_identifier
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.schema import (
    DataReadRef,
    DataWriteRef,
    ExternalInterface,
    InitEvent,
    OperationCallRef,
    OperationEvent,
    SWComponent,
    TimingEvent,
)
from swcraft_tools.validator import check_identifier_conformance, event_identity
from swcraft_tools.writer import write_element
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import swc_to_dict

#: ``field id -> (owning kind, type, default, immutable)``, each optional.
NAME_FIELDS: dict[str, tuple[str, str, Any, bool]] = {
    "field:swc.swc.behavior_name": ("kind:swc.swc", "string", None, False),
    "field:swc.timing_event.name": ("kind:swc.timing_event", "string", None, True),
    "field:swc.init_event.name": ("kind:swc.init_event", "string", None, True),
    "field:swc.operation_event.name": ("kind:swc.operation_event", "string", None, True),
    "field:swc.data_read.name": ("kind:swc.data_read", "string", None, False),
    "field:swc.data_write.name": ("kind:swc.data_write", "string", None, False),
    "field:swc.operation_call.name": ("kind:swc.operation_call", "string", None, False),
}
LIST_FIELDS: dict[str, tuple[str, str, Any, bool]] = {
    "field:interfaces.external_interface.operations": ("kind:interfaces.external_interface", "list[string]", [], False),
    "field:interfaces.external_interface.data_elements": (
        "kind:interfaces.external_interface",
        "list[string]",
        [],
        False,
    ),
}
NEW_RULES = {
    "rule:interfaces.external_interface.operations.no-duplicate-entry": "operations_must_not_repeat",
    "rule:interfaces.external_interface.data_elements.no-duplicate-entry": "data_elements_must_not_repeat",
    "rule:interfaces.external_interface.member-kind": "members_must_match_the_dest",
}
CS, SR, MS = "CLIENT-SERVER-INTERFACE", "SENDER-RECEIVER-INTERFACE", "MODE-SWITCH-INTERFACE"


def _fields() -> dict[str, dict[str, Any]]:
    return {
        field["id"]: {**field, "_kind": kind["id"]}
        for layer in load_contract()["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
    }


def _stub(dest: str = CS, **lists: list[str]) -> ExternalInterface:
    return ExternalInterface.model_validate(
        {"name": "IStub", "kind": "external_interface", "external_ref": "/Ext/IStub", "external_dest": dest, **lists}
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class TestTheContractDeclaresTheNineFields:
    @pytest.mark.parametrize("field_id", sorted({**NAME_FIELDS, **LIST_FIELDS}))
    def test_each_field_is_declared_optional(self, field_id: str) -> None:
        kind_id, field_type, default, immutable = {**NAME_FIELDS, **LIST_FIELDS}[field_id]
        declared = _fields()[field_id]
        assert declared["_kind"] == kind_id
        assert (declared["type"], declared["required"], declared["default"], declared["immutable"]) == (
            field_type,
            False,
            default,
            immutable,
        )

    def test_the_two_lists_are_whole_value_collections_omitted_while_empty(self) -> None:
        for field_id in LIST_FIELDS:
            declared = _fields()[field_id]
            assert declared["collection"] == {"kind": "list", "memberIdentity": "whole_value"}
            assert declared["omitAtDefault"] is True
            assert "relation" not in declared and "reference" not in declared

    def test_the_stub_s_fields_keep_the_lists_after_the_reference_pair(self) -> None:
        kind = next(
            kind
            for layer in load_contract()["layers"]
            for kind in layer["kinds"]
            if kind["id"] == "kind:interfaces.external_interface"
        )
        assert [field["name"] for field in kind["fields"]] == [
            "name",
            "kind",
            "external_ref",
            "external_dest",
            "operations",
            "data_elements",
            "description",
            # contract 11.0.0: the trace link, appended last
            "satisfies",
        ]

    def test_the_three_element_local_rules_and_their_bindings(self) -> None:
        contract = load_contract()
        rules = {rule["id"]: rule for rule in contract["rules"]}
        binding = load_binding()["rules"]
        for rule_id, method in NEW_RULES.items():
            assert rules[rule_id]["family"] == "element-local"
            assert binding[rule_id] == {"kind": "kind:interfaces.external_interface", "method": method}
            assert callable(getattr(ExternalInterface, method))
        # A model validator names no field, so its rule addresses its own kind.
        assert rules["rule:interfaces.external_interface.member-kind"]["appliesTo"] == [
            "kind:interfaces.external_interface"
        ]

    def test_identifier_declared_reaches_the_two_lists(self) -> None:
        rules = {rule["id"]: rule for rule in load_contract()["rules"]}
        assert set(LIST_FIELDS) <= set(rules["rule:identifier.declared"]["appliesTo"])

    def test_the_counts(self) -> None:
        contract = load_contract()
        kinds = [kind for layer in contract["layers"] for kind in layer["kinds"]]
        # The pinned figures move with every contract bump that adds or removes a field, kind or rule.
        assert contract["contractVersion"] == "12.7.1"
        # (layers, kinds, fields).
        assert (len(contract["layers"]), len(kinds), sum(len(kind["fields"]) for kind in kinds)) == (15, 67, 359)
        assert len(contract["rules"]) == 352


# ---------------------------------------------------------------------------
# Loading and defaults
# ---------------------------------------------------------------------------


class TestEachFieldLoadsAndDefaults:
    def test_the_defaults(self) -> None:
        assert SWComponent(name="C").behavior_name is None
        assert TimingEvent(type="timing", period=0.01).name is None
        assert InitEvent(type="init").name is None
        assert OperationEvent(type="operation", port="p", operation="o").name is None
        assert DataReadRef(port="p", element="e").name is None
        assert DataWriteRef(port="p", element="e").name is None
        assert OperationCallRef(port="p", operation="o").name is None
        stub = ExternalInterface(name="IStub", kind="external_interface")
        assert (stub.operations, stub.data_elements) == ([], [])

    def test_a_stated_value_loads_as_written(self) -> None:
        # Whether it is a legal identifier, and unique in its scope, is `check-model`'s.
        assert SWComponent(name="C", behavior_name="C_Ib").behavior_name == "C_Ib"
        assert TimingEvent(type="timing", period=0.01, name="Cyclic10ms").name == "Cyclic10ms"
        assert InitEvent(type="init", name="Startup").name == "Startup"
        assert OperationEvent(type="operation", port="p", operation="o", name="OnGet").name == "OnGet"
        assert DataReadRef(port="p", element="e", name="ReadSpeed").name == "ReadSpeed"
        assert DataWriteRef(port="p", element="e", name="WriteSpeed").name == "WriteSpeed"
        assert OperationCallRef(port="p", operation="o", name="CallGet").name == "CallGet"
        assert _stub(CS, operations=["Get", "Set"]).operations == ["Get", "Set"]
        assert _stub(SR, data_elements=["speed"]).data_elements == ["speed"]


# ---------------------------------------------------------------------------
# The stub's element-local rules
# ---------------------------------------------------------------------------


class TestTheStubListsAreRefused:
    @pytest.mark.parametrize(("dest", "field"), [(CS, "operations"), (SR, "data_elements")])
    @pytest.mark.parametrize("names", [["a", ""], ["a", "  "]])
    def test_a_blank_entry(self, dest: str, field: str, names: list[str]) -> None:
        with pytest.raises(ValidationError, match=f"{field} entries must not be blank"):
            _stub(dest, **{field: names})

    @pytest.mark.parametrize(("dest", "field"), [(CS, "operations"), (SR, "data_elements")])
    @pytest.mark.parametrize("names", [["Get", "Get"], ["Get", "get"], ["Straße", "STRASSE"]])
    def test_a_name_twice_ignoring_case(self, dest: str, field: str, names: list[str]) -> None:
        with pytest.raises(ValidationError, match=f"member named twice in {field}, ignoring case"):
            _stub(dest, **{field: names})

    @pytest.mark.parametrize(
        ("dest", "field"),
        [
            (SR, "operations"),
            (MS, "operations"),
            ("", "operations"),
            ("PORT-INTERFACE", "operations"),
            (CS, "data_elements"),
            (MS, "data_elements"),
            ("", "data_elements"),
        ],
    )
    def test_a_list_the_dest_does_not_admit(self, dest: str, field: str) -> None:
        with pytest.raises(ValidationError, match=f"lists {field} only when its external_dest is"):
            _stub(dest, **{field: ["member"]})

    @pytest.mark.parametrize("dest", [CS, SR, MS, "", "PORT-INTERFACE"])
    def test_an_empty_list_loads_on_every_dest(self, dest: str) -> None:
        stub = _stub(dest, operations=[], data_elements=[])
        assert (stub.operations, stub.data_elements) == ([], [])

    def test_a_mode_switch_stub_lists_nothing(self) -> None:
        with pytest.raises(ValidationError, match="lists operations only when its external_dest is"):
            _stub(MS, operations=["m"])
        with pytest.raises(ValidationError, match="lists data_elements only when its external_dest is"):
            _stub(MS, data_elements=["m"])

    @pytest.mark.parametrize("key", ["is_service", "base", "operation", "extrnal_ref"])
    def test_an_undeclared_key_is_still_refused(self, key: str) -> None:
        with pytest.raises(ValidationError, match=rf"{key}\n  Extra inputs are not permitted"):
            ExternalInterface.model_validate({"name": "IStub", "kind": "external_interface", key: []})


def _write_stub(model_root: Path, document: dict[str, Any]) -> None:
    directory = model_root / "interfaces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{document['name']}.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n"
    )


class TestAListedNameIsHeldToTheIdentifierRule:
    def test_check_model_reports_a_non_identifier_and_a_name_over_the_budget(self, tmp_path: Path) -> None:
        _write_stub(
            tmp_path,
            {
                "name": "IStub",
                "kind": "external_interface",
                "external_ref": "/Ext/IStub",
                "external_dest": CS,
                "operations": ["Get", "1bad", "x" * 129],
            },
        )
        _write_stub(
            tmp_path,
            {
                "name": "IData",
                "kind": "external_interface",
                "external_ref": "/Ext/IData",
                "external_dest": SR,
                "data_elements": ["speed", "bad name"],
            },
        )

        issues = [
            issue
            for issue in check_identifier_conformance(str(tmp_path)).records
            if issue.rule == "identifier.declared"
        ]

        stub, data = Subject("interfaces", "IStub"), Subject("interfaces", "IData")
        assert sorted(map(repr, (issue.subjects[0] for issue in issues))) == sorted(
            map(
                repr,
                [
                    stub.at(field_step("operations"), member_step("1bad")),
                    stub.at(field_step("operations"), member_step("x" * 129)),
                    data.at(field_step("data_elements"), member_step("bad name")),
                ],
            )
        )

    def test_pass_d_refuses_the_same_names(self) -> None:
        stub = {
            "name": "IStub",
            "kind": "external_interface",
            "external_ref": "/Ext/IStub",
            "external_dest": CS,
            "operations": ["Get", "1bad"],
        }
        violations = _interface_identifiers(stub)
        assert [violation.addresses[0] for violation in violations] == [
            Address("interfaces", "IStub", (FieldStep("operations"), MemberStep("1bad")))
        ]
        assert "is emitted as the last segment of an AUTOSAR reference path" in violations[0].message

    def test_a_conforming_list_is_silent(self, tmp_path: Path) -> None:
        _write_stub(
            tmp_path,
            {
                "name": "IStub",
                "kind": "external_interface",
                "external_ref": "/Ext/IStub",
                "external_dest": CS,
                "operations": ["Get", "Set_Value"],
            },
        )
        assert not [
            issue
            for issue in check_identifier_conformance(str(tmp_path)).records
            if issue.rule == "identifier.declared"
        ]


# ---------------------------------------------------------------------------
# An event's stated name joins its identity; a default does not
# ---------------------------------------------------------------------------


def _events() -> reg.CollectionDescriptor:
    collection = reg.REGISTRY.element_kind("runnable").get_field("events").collection
    assert collection is not None
    return collection


class TestAStatedEventNameJoinsTheIdentity:
    @pytest.mark.parametrize(
        "event",
        [
            {"type": "timing", "period": 0.01},
            {"type": "init"},
            {"type": "operation", "port": "pServe", "operation": "Get"},
        ],
        ids=["timing", "init", "operation"],
    )
    def test_through_the_reduction(self, event: dict[str, Any]) -> None:
        assert reduce_whole_value({**event, "name": None}, _events()) == event
        assert reduce_whole_value({**event, "name": "Stated"}, _events()) == {**event, "name": "Stated"}

    def test_through_the_member_identity(self) -> None:
        stated = TimingEvent(type="timing", period=0.01, name="Cyclic")
        assert member_identity_of(stated, _events()) == {"type": "timing", "period": 0.01, "name": "Cyclic"}
        assert member_identity_of(TimingEvent(type="timing", period=0.01), _events()) == {
            "type": "timing",
            "period": 0.01,
        }

    def test_the_one_identity_the_validator_builds_for_every_kind(self) -> None:
        assert event_identity(TimingEvent(type="timing", period=0.01)) == {"type": "timing", "period": 0.01}
        assert event_identity(TimingEvent(type="timing", period=0.01, name="Cyclic")) == {
            "type": "timing",
            "period": 0.01,
            "name": "Cyclic",
        }
        plain = OperationEvent(type="operation", port="pServe", operation="Get")
        assert event_identity(plain) == {"type": "operation", "port": "pServe", "operation": "Get"}
        assert event_identity(plain.model_copy(update={"name": "OnGet"})) == {
            "type": "operation",
            "port": "pServe",
            "operation": "Get",
            "name": "OnGet",
        }

    @pytest.mark.parametrize("name", [None, "OnGet"])
    def test_the_identity_pass_d_builds(self, name: str | None) -> None:
        port, operation = "p" * 70, "o" * 70  # over the joint budget, so the pair is reported
        event: dict[str, Any] = {"type": "operation", "port": port, "operation": operation}
        if name is not None:
            event["name"] = name
        (violation,) = _operation_event_identifier(
            Address("swc", "Server"), "Server", {"name": "Serve"}, event, f"OIT_{port}_{operation}"
        )
        member = violation.addresses[0].path[3]
        assert isinstance(member, MemberStep)
        assert member.member == event


# ---------------------------------------------------------------------------
# The model writers
# ---------------------------------------------------------------------------


def _named_component() -> SWComponent:
    return SWComponent.model_validate(
        {
            "name": "Carrier",
            "handle_termination_and_restart": "no_support",
            "ports": [
                {"name": "rIn", "direction": "require", "interface": "IIn"},
                {"name": "pOut", "direction": "provide", "interface": "IOut"},
                {"name": "pServe", "direction": "provide", "interface": "IServe"},
                {"name": "rCall", "direction": "require", "interface": "ICall"},
            ],
            "runnables": [
                {
                    "name": "Run",
                    "events": [
                        {"type": "timing", "period": 0.01, "name": "Cyclic10ms"},
                        {"type": "init", "name": "Startup"},
                        {"type": "operation", "port": "pServe", "operation": "Get", "name": "OnGet"},
                    ],
                    "reads": [{"port": "rIn", "element": "value", "by": "value", "name": "ReadValue"}],
                    "writes": [{"port": "pOut", "element": "value", "name": "WriteValue"}],
                    "calls": [{"port": "rCall", "operation": "Get", "name": "CallGet"}],
                }
            ],
            "data_type_mappings": ["Maps"],
            "behavior_name": "Carrier_Behaviour",
            "package": "Pkg",
        }
    )


class TestTheWritersWriteEachFieldAndOmitItUnset:
    def test_the_hand_written_component_dump_equals_the_generic_one(self, tmp_path: Path) -> None:
        component = _named_component()
        assert swc_to_dict(component) == element_to_dict(component)
        write_element(str(tmp_path), "swc/g/Carrier.yaml", component, create=True)
        generic = (tmp_path / "swc/g/Carrier.yaml").read_text(encoding="utf-8")
        assert yaml.safe_dump(swc_to_dict(component), default_flow_style=False, sort_keys=False) == generic
        (reloaded,) = read_swc_model(str(tmp_path))
        assert reloaded == component

    def test_each_name_is_written_last_in_its_entry(self) -> None:
        document = swc_to_dict(_named_component())
        assert list(document)[-2:] == ["behavior_name", "package"]
        runnable = document["runnables"][0]
        assert [list(event)[-1] for event in runnable["events"]] == ["name", "name", "name"]
        for entries in (runnable["reads"], runnable["writes"], runnable["calls"]):
            assert [list(entry)[-1] for entry in entries] == ["name"]

    def test_an_unset_name_is_left_out_by_both(self, tmp_path: Path) -> None:
        plain = SWComponent.model_validate(
            {
                "name": "Plain",
                "handle_termination_and_restart": "no_support",
                "runnables": [
                    {
                        "name": "Run",
                        "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
                        "reads": [{"port": "rIn", "element": "value"}],
                    }
                ],
            }
        )
        assert swc_to_dict(plain) == element_to_dict(plain)
        write_element(str(tmp_path), "swc/g/Plain.yaml", plain, create=True)
        text = (tmp_path / "swc/g/Plain.yaml").read_text(encoding="utf-8")
        assert "name: null" not in text and "behavior_name" not in text
        assert text.count("name:") == 2  # the component's and the runnable's

    @pytest.mark.parametrize(
        "lists",
        [
            {"external_dest": CS, "operations": ["Get", "Set"]},
            {"external_dest": SR, "data_elements": ["speed", "valid"]},
            {"external_dest": CS},
        ],
        ids=["operations", "data-elements", "neither"],
    )
    def test_the_stub_lists_through_the_import_serializer_and_the_generic_writer(
        self, lists: dict[str, Any], tmp_path: Path
    ) -> None:
        stub = ExternalInterface.model_validate(
            {"name": "IStub", "kind": "external_interface", "external_ref": "/Ext/IStub", "description": "d", **lists}
        )
        assert list(interface_to_dict(stub).items()) == list(element_to_dict(stub).items())
        assert list(interface_to_dict(stub))[4:-1] == [key for key in ("operations", "data_elements") if key in lists]
        write_element(str(tmp_path), "interfaces/IStub.yaml", stub, create=True)
        (reloaded,) = read_interfaces(str(tmp_path))
        assert reloaded == stub


# ---------------------------------------------------------------------------
# The two sites that read `data_elements` by attribute
# ---------------------------------------------------------------------------


class TestTheAttributeReadsTakeAStubListAsNoElement:
    def test_the_init_constant_guard_skips_a_stub_s_list(self, tmp_path: Path) -> None:
        """``writer.mutations.create_interface``'s constant guard: a stub listing ``speed`` reserves no constant."""
        from swcraft_tools.writer import create_interface

        _write_stub(
            tmp_path,
            {
                "name": "IStub",
                "kind": "external_interface",
                "external_ref": "/Ext/IStub",
                "external_dest": SR,
                "data_elements": ["speed"],
            },
        )
        (tmp_path / "data_types").mkdir()
        create_interface(str(tmp_path), "ISpeed", [{"name": "speed", "implementation_type": "uint8"}])
        assert {interface.name for interface in read_interfaces(str(tmp_path))} == {"IStub", "ISpeed"}

    def test_the_measure_reader_answers_no_element_for_a_stub(self) -> None:
        from swcraft_tools.editing.safety_guard import _data_element_names

        class _Snapshot:
            interfaces = (_stub(SR, data_elements=["speed"]),)

        assert _data_element_names(_Snapshot(), "IStub") == frozenset()  # type: ignore[arg-type]
