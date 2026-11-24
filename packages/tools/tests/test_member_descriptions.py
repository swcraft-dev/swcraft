"""Seven member kinds carry an optional ``description``.

A runnable, a data element, an operation, an argument, an enum literal, a record field and an
implementation element each have ``description``: a string defaulting to ``""``, declared **last**
among its kind's original fields so existing files' key order and the published read order keep
their place. This module holds:

* **the contract and the schema**: each field is declared with the port's shape and sentence, last in
  its kind, and each class loads it and defaults it to ``""``;
* **the derived serializer**: ``writer/elements.write_element`` writes a described member with the key
  last and leaves an empty one out;
* **every hand-written dump**: ``writer/yaml_documents.py``'s ``runnable_to_dict``,
  ``data_type_to_dict`` (the literals, the fields and the implementation elements),
  ``data_element_to_dict`` and ``cs_operation_to_dict``, and ``arxml_import/report.py``'s
  ``_data_element_to_dict`` and ``interface_to_dict``, each writing the key last, omitting it when
  ``""``, and agreeing with the derived serializer;
* **every calling mutation**: the six that rewrite a component through ``swc_to_dict`` keep a runnable
  description they do not touch, and ``create_interface``, ``create_cs_interface`` and
  ``create_data_type`` write the descriptions they are given;
* **the component import's projection** of the seven.

What it does **not** reach: the importers' reading of a ``DESC`` into these fields, and the ARXML
writer, which writes each as its element's ``DESC`` (pinned by ``test_description_emission.py`` and
``test_model_contract_coverage.py``'s
``test_every_declared_description_is_written_and_only_the_stubs_rows_say_otherwise``). The byte
identity of the two data-element dumps over a described element is ``test_arxml_import_cli.py``'s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from external_stubs_support import change, put
from model_contract_fixtures import load_contract
from operation_event_support import INTERFACE, SERVER, SERVER_FILE, load, mixed_event_model
from swcraft_tools.arxml_import.report import _data_element_to_dict as report_data_element_to_dict
from swcraft_tools.arxml_import.report import interface_to_dict
from swcraft_tools.component_exchange.divergence import (
    projected_data_type,
    projected_interface,
    projected_swc,
)
from swcraft_tools.component_exchange.target_carry import carry_target_descriptions
from swcraft_tools.schema import (
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    EnumDataType,
    EnumLiteral,
    ImplementationElement,
    ImplementationRecordDataType,
    RecordDataType,
    RecordField,
    Runnable,
    SRInterface,
    SWComponent,
)
from swcraft_tools.writer import (
    create_cs_interface,
    create_data_type,
    create_interface,
    mutations,
    write_element,
)
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import (
    cs_operation_to_dict,
    data_element_to_dict,
    data_type_to_dict,
    runnable_to_dict,
)

#: The seven fields, with the class each is bound to and the noun its sentence
#: names -- written out rather than read off the contract, since the contract
#: is what is under test.
FIELDS: dict[str, tuple[str, type, str]] = {
    "field:swc.runnable.description": ("kind:swc.runnable", Runnable, "runnable"),
    "field:interfaces.data_element.description": ("kind:interfaces.data_element", DataElement, "data element"),
    "field:interfaces.operation.description": ("kind:interfaces.operation", CSOperation, "operation"),
    "field:interfaces.argument.description": ("kind:interfaces.argument", CSArgument, "argument"),
    "field:application_types.enum_literal.description": (
        "kind:application_types.enum_literal",
        EnumLiteral,
        "enum literal",
    ),
    "field:application_types.record_field.description": (
        "kind:application_types.record_field",
        RecordField,
        "record field",
    ),
    "field:implementation_types.implementation_element.description": (
        "kind:implementation_types.implementation_element",
        ImplementationElement,
        "implementation element",
    ),
}

#: The smallest valid instance of each member class, before a description.
MINIMAL: dict[type, dict[str, Any]] = {
    Runnable: {"name": "Run"},
    DataElement: {"name": "speed", "implementation_type": "uint8"},
    CSOperation: {"name": "Read"},
    CSArgument: {"name": "value", "direction": "out", "implementation_type": "uint8"},
    EnumLiteral: {"name": "OFF", "value": 0},
    RecordField: {"name": "x", "type": "uint8"},
    ImplementationElement: {"name": "x", "type": "uint8"},
}

TEXT = "Free text, kept."


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _dump(document: dict[str, Any]) -> str:
    """The dump call every writer in the tree makes."""
    return yaml.safe_dump(document, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# The contract and the schema
# ---------------------------------------------------------------------------


#: The fields appended after a ``description``: a data element's two properties, and a runnable's
#: ``symbol``, budget (``wcet``, ``wcet_type``) and trace link ``satisfies``. ``description`` stays the
#: last field of the ones declared before them, which is what "last in its kind" asserts below.
_APPENDED_AFTER_DESCRIPTION = frozenset(
    {"sw_impl_policy", "sw_calibration_access", "symbol", "wcet", "wcet_type", "satisfies"}
)


#: What the contract says each is written as.
_WRITTEN = dict.fromkeys(
    FIELDS, "Written into the generated ARXML as the element's `DESC`, exactly as held; the empty default writes none."
) | {
    "field:application_types.enum_literal.description": (
        "Written into the generated ARXML as its `COMPU-SCALE`'s `DESC`, exactly as held; the empty default "
        "writes none."
    )
}


class TestTheContractDeclaresTheSevenFields:
    @pytest.mark.parametrize("field_id", sorted(FIELDS))
    def test_each_is_the_ports_shape_and_last_in_its_kind(self, field_id: str) -> None:
        kind_id, _cls, noun = FIELDS[field_id]
        fields = [f for f in _kinds()[kind_id]["fields"] if f["name"] not in _APPENDED_AFTER_DESCRIPTION]
        assert fields[-1] == {
            "id": field_id,
            "name": "description",
            "type": "string",
            "required": False,
            "default": "",
            "immutable": False,
            # The writer writes the text as a DESC.
            "description": f"Free text about the {noun}. {_WRITTEN[field_id]}",
        }

    def test_the_seven_are_every_description_the_minor_adds(self) -> None:
        declared = {
            field["id"]
            for kind in _kinds().values()
            for field in kind["fields"]
            if field["name"] == "description" and kind["id"] in {value[0] for value in FIELDS.values()}
        }
        assert declared == set(FIELDS)


class TestTheSchemaLoadsTheField:
    @pytest.mark.parametrize("field_id", sorted(FIELDS))
    def test_it_defaults_to_empty_and_loads_a_text(self, field_id: str) -> None:
        cls = FIELDS[field_id][1]
        assert cls.model_validate(MINIMAL[cls]).description == ""
        assert cls.model_validate({**MINIMAL[cls], "description": TEXT}).description == TEXT

    @pytest.mark.parametrize("field_id", sorted(FIELDS))
    def test_it_is_the_last_model_field(self, field_id: str) -> None:
        cls = FIELDS[field_id][1]
        assert [name for name in cls.model_fields if name not in _APPENDED_AFTER_DESCRIPTION][-1] == "description"


# ---------------------------------------------------------------------------
# The owners: one described member of each kind, and its undescribed twin
# ---------------------------------------------------------------------------


def _runnable(description: str = "") -> Runnable:
    return Runnable(name="Run", events=[], description=description)


def _sr(description: str = "") -> SRInterface:
    return SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="uint8", init=0, description=description)],
    )


def _cs(description: str = "") -> CSInterface:
    return CSInterface(
        name="IRead",
        kind="client_server",
        operations=[
            CSOperation(
                name="Read",
                arguments=[
                    CSArgument(name="value", direction="out", implementation_type="uint8", description=description)
                ],
                description=description,
            )
        ],
    )


def _enum(description: str = "") -> EnumDataType:
    return EnumDataType(name="OnOff", kind="enum", literals=[EnumLiteral(name="OFF", value=0, description=description)])


def _record(description: str = "") -> RecordDataType:
    return RecordDataType(
        name="Point", kind="record", fields=[RecordField(name="x", type="uint8", description=description)]
    )


def _implementation_record(description: str = "") -> ImplementationRecordDataType:
    return ImplementationRecordDataType(
        name="Point_impl",
        kind="implementation_record",
        elements=[ImplementationElement(name="x", type="uint8", description=description)],
    )


def _described_members(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every member dict of a dumped owner that carries the member kinds' fields."""
    for key in ("data_elements", "literals", "fields", "elements"):
        if key in document:
            return list(document[key])
    operations = document.get("operations", [document] if "arguments" in document else None)
    if operations is not None:
        members: list[dict[str, Any]] = []
        for operation in operations:
            members.append(operation)
            members.extend(operation["arguments"])
        return members
    return [document]


# ---------------------------------------------------------------------------
# The derived serializer
# ---------------------------------------------------------------------------


OWNERS = {
    "runnable": _runnable,
    "data element": _sr,
    "operation and argument": _cs,
    "enum literal": _enum,
    "record field": _record,
    "implementation element": _implementation_record,
}


class TestTheDerivedSerializer:
    @pytest.mark.parametrize("owner", sorted(OWNERS))
    def test_a_described_member_writes_the_key_last_and_an_empty_one_none(self, owner: str) -> None:
        described = _described_members(element_to_dict(OWNERS[owner](TEXT)))
        assert described, owner
        for member in described:
            assert list(member)[-1] == "description"
            assert member["description"] == TEXT
        empty = _described_members(element_to_dict(OWNERS[owner]()))
        assert len(empty) == len(described), owner
        for member in empty:
            assert "description" not in member

    def test_write_element_round_trips_a_described_interface(self, tmp_path: Path) -> None:
        write_element(str(tmp_path), "interfaces/IRead.yaml", _cs(TEXT), create=True)
        text = (tmp_path / "interfaces" / "IRead.yaml").read_text(encoding="utf-8")
        assert CSInterface.model_validate(yaml.safe_load(text)) == _cs(TEXT)
        assert text.count("description: Free text, kept.") == 2


# ---------------------------------------------------------------------------
# The hand-written dumps
# ---------------------------------------------------------------------------


#: Each hand-written dump, the owner it writes and how to read its member dicts back out of its result.
HAND_WRITTEN: dict[str, tuple[Any, Any]] = {
    "yaml_documents.runnable_to_dict": (runnable_to_dict, _runnable),
    "yaml_documents.data_element_to_dict": (lambda iface: data_element_to_dict(iface.data_elements[0]), _sr),
    "yaml_documents.cs_operation_to_dict": (lambda iface: cs_operation_to_dict(iface.operations[0]), _cs),
    "yaml_documents.data_type_to_dict[enum]": (data_type_to_dict, _enum),
    "yaml_documents.data_type_to_dict[record]": (data_type_to_dict, _record),
    "yaml_documents.data_type_to_dict[implementation_record]": (data_type_to_dict, _implementation_record),
    "report._data_element_to_dict": (lambda iface: report_data_element_to_dict(iface.data_elements[0]), _sr),
    "report.interface_to_dict[sender_receiver]": (interface_to_dict, _sr),
    "report.interface_to_dict[client_server]": (interface_to_dict, _cs),
}


class TestEveryHandWrittenDump:
    @pytest.mark.parametrize("dump", sorted(HAND_WRITTEN))
    def test_the_key_is_last_and_the_text_is_kept(self, dump: str) -> None:
        serializer, owner = HAND_WRITTEN[dump]
        written = serializer(owner(TEXT))
        members = _described_members(written)
        assert members, dump
        for member in members:
            assert list(member)[-1] == "description", (dump, member)
            assert member["description"] == TEXT, (dump, member)

    @pytest.mark.parametrize("dump", sorted(HAND_WRITTEN))
    def test_an_empty_description_writes_no_key(self, dump: str) -> None:
        serializer, owner = HAND_WRITTEN[dump]
        members = _described_members(serializer(owner()))
        assert len(members) == len(_described_members(serializer(owner(TEXT)))) > 0, dump
        for member in members:
            assert "description" not in member, (dump, member)

    @pytest.mark.parametrize("dump", sorted(HAND_WRITTEN))
    def test_it_agrees_with_the_derived_serializer(self, dump: str) -> None:
        """The key order of every member dict, and so the bytes, the derived walk writes."""
        serializer, owner = HAND_WRITTEN[dump]
        for description in ("", TEXT):
            hand = _described_members(serializer(owner(description)))
            derived = _described_members(element_to_dict(owner(description)))
            assert hand == derived, dump
            assert [list(member) for member in hand] == [list(member) for member in derived], dump

    def test_the_owner_reloads_equal_through_each_mutations_dump(self) -> None:
        for build in (_enum, _record, _implementation_record):
            element = build(TEXT)
            assert type(element).model_validate(yaml.safe_load(_dump(data_type_to_dict(element)))) == element
        runnable = _runnable(TEXT)
        assert Runnable.model_validate(yaml.safe_load(_dump(runnable_to_dict(runnable)))) == runnable
        for build in (_sr, _cs):
            element = build(TEXT)
            assert type(element).model_validate(yaml.safe_load(_dump(interface_to_dict(element)))) == element


# ---------------------------------------------------------------------------
# The calling mutations
# ---------------------------------------------------------------------------


RAM0: dict[str, Any] = {
    "name": "RAM0",
    "kind": "memory",
    "type": "RAM",
    "size": 4096,
    "accessible": ["CORE0"],
    "threshold": 0.8,
}


def _with_other_runnable(model_root: Path) -> None:
    change(model_root, SERVER_FILE, lambda document: document["runnables"].append({"name": "Other", "events": []}))


def _with_a_budget(model_root: Path) -> None:
    put(model_root, "hardware/RAM0.yaml", RAM0)
    change(model_root, SERVER_FILE, lambda document: document.update({"memory": {"RAM0": 64}}))


#: Every mutation that rewrites an existing component through `swc_to_dict` (`create_swc` writes a new
#: file with no runnable, so it has nothing to keep): ``(prepare, call)``.
REWRITING_MUTATIONS: dict[str, tuple[Any, Any]] = {
    "add_port": (lambda _root: None, lambda root: mutations.add_port(root, SERVER, "pSecond", "provide", INTERFACE)),
    "add_runnable": (lambda _root: None, lambda root: mutations.add_runnable(root, SERVER, "Other")),
    "add_timing_event": (_with_other_runnable, lambda root: mutations.add_timing_event(root, SERVER, "Other", 0.02)),
    "add_init_event": (_with_other_runnable, lambda root: mutations.add_init_event(root, SERVER, "Other")),
    "set_swc_memory_budget": (_with_a_budget, lambda root: mutations.set_swc_memory_budget(root, SERVER, "RAM0", 128)),
    "unset_swc_memory_budget": (_with_a_budget, lambda root: mutations.unset_swc_memory_budget(root, SERVER, "RAM0")),
}


class TestEveryCallingMutationKeepsTheRunnablesDescription:
    """A preservation test per mutation that rewrites a component."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_the_text_last(self, tmp_path: Path, mutation: str) -> None:
        model = mixed_event_model(tmp_path / "model")
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        change(model, SERVER_FILE, lambda document: document["runnables"][0].update({"description": TEXT}))

        call(str(model))

        runnable = load(model, SERVER_FILE)["runnables"][0]
        assert runnable["description"] == TEXT
        assert list(runnable)[-1] == "description"


# ---------------------------------------------------------------------------
# A runnable's declared cost
# ---------------------------------------------------------------------------
#
# Not a description, but the same erasure risk on the same dump: `runnable_to_dict`, which every
# mutation and both importers share through `swc_to_dict`, must name the field or erase it.


def _costed_runnable(wcet: str | None = "250us", wcet_type: str | None = "net") -> Runnable:
    return Runnable(name="Run", events=[], description=TEXT, symbol="Run_Entry", wcet=wcet, wcet_type=wcet_type)


class TestTheRunnablesCostThroughTheHandWrittenDump:
    def test_it_agrees_with_the_derived_serializer_and_is_written_last(self) -> None:
        runnable = _costed_runnable()
        hand = runnable_to_dict(runnable)
        derived = element_to_dict(runnable)
        assert hand == derived
        assert list(hand) == list(derived)
        assert list(hand)[-3:] == ["symbol", "wcet", "wcet_type"]
        assert (hand["wcet"], hand["wcet_type"]) == ("250us", "net")

    def test_a_runnable_without_one_writes_no_key(self) -> None:
        hand = runnable_to_dict(_costed_runnable(None, None))
        assert "wcet" not in hand and "wcet_type" not in hand
        assert hand == element_to_dict(_costed_runnable(None, None))

    def test_the_runnable_reloads_equal(self) -> None:
        runnable = _costed_runnable()
        assert Runnable.model_validate(yaml.safe_load(_dump(runnable_to_dict(runnable)))) == runnable


class TestEveryCallingMutationKeepsTheRunnablesCost:
    """A preservation test per mutation that rewrites a component, for ``wcet`` and ``wcet_type``."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_the_cost_last(self, tmp_path: Path, mutation: str) -> None:
        model = mixed_event_model(tmp_path / "model")
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        change(
            model,
            SERVER_FILE,
            lambda document: document["runnables"][0].update({"wcet": "250us", "wcet_type": "gross"}),
        )

        call(str(model))

        runnable = load(model, SERVER_FILE)["runnables"][0]
        assert (runnable["wcet"], runnable["wcet_type"]) == ("250us", "gross")
        assert list(runnable)[-2:] == ["wcet", "wcet_type"]


class TestTheCreatingMutationsWriteTheTexts:
    def test_create_interface(self, tmp_path: Path) -> None:
        path = create_interface(
            str(tmp_path),
            "ISpeed",
            [
                {"name": "speed", "implementation_type": "uint8", "description": TEXT},
                {"name": "raw", "implementation_type": "uint8"},
            ],
        )
        written = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert written["data_elements"] == [
            {"name": "speed", "implementation_type": "uint8", "description": TEXT},
            {"name": "raw", "implementation_type": "uint8"},
        ]

    def test_create_cs_interface(self, tmp_path: Path) -> None:
        path = create_cs_interface(
            str(tmp_path),
            "IRead",
            [
                {
                    "name": "Read",
                    "arguments": [
                        {"name": "value", "direction": "out", "implementation_type": "uint8", "description": TEXT}
                    ],
                    "description": TEXT,
                },
                {"name": "Reset"},
            ],
        )
        written = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert written["operations"] == [
            {
                "name": "Read",
                "arguments": [
                    {"name": "value", "direction": "out", "implementation_type": "uint8", "description": TEXT}
                ],
                "description": TEXT,
            },
            {"name": "Reset", "arguments": []},
        ]

    @pytest.mark.parametrize(
        ("kind", "keyword", "members"),
        [
            ("enum", "literals", [{"name": "OFF", "value": 0, "description": TEXT}, {"name": "ON", "value": 1}]),
            ("record", "fields", [{"name": "x", "type": "uint8", "description": TEXT}, {"name": "y", "type": "uint8"}]),
        ],
    )
    def test_create_data_type(self, tmp_path: Path, kind: str, keyword: str, members: list[dict[str, Any]]) -> None:
        path = create_data_type(str(tmp_path), "Thing", kind, **{keyword: members})
        assert yaml.safe_load(Path(path).read_text(encoding="utf-8"))[keyword] == members


# ---------------------------------------------------------------------------
# The component import's projection
# ---------------------------------------------------------------------------


class TestTheComponentImportProjectsTheSevenOut:
    """A target that describes a shared definition's member compares equal to a file stating another text or none.

    A runnable's text is **compared** (the writer writes it as a ``DESC``), an empty incoming one keeping the target's.
    """

    def test_the_runnables(self) -> None:
        described = SWComponent(name="C", type="application", runnables=[_runnable(TEXT)])
        plain = SWComponent(name="C", type="application", runnables=[_runnable()])
        assert projected_swc(described) != projected_swc(plain)
        carried, labels = carry_target_descriptions(described, plain)
        assert labels == ["runnable 'Run'"]
        assert projected_swc(described) == projected_swc(carried)

    @pytest.mark.parametrize("build", [_sr, _cs], ids=["data_element", "operation_and_argument"])
    def test_the_interface_members(self, build: Any) -> None:
        assert projected_interface(build(TEXT)) == projected_interface(build())

    @pytest.mark.parametrize(
        "build",
        [_enum, _record, _implementation_record],
        ids=["enum_literal", "record_field", "implementation_element"],
    )
    def test_the_data_type_members(self, build: Any) -> None:
        assert projected_data_type(build(TEXT)) == projected_data_type(build())

    def test_the_projection_leaves_every_other_member_field_compared(self) -> None:
        renamed = EnumDataType(name="OnOff", kind="enum", literals=[EnumLiteral(name="OFF", value=1)])
        assert projected_data_type(_enum(TEXT)) != projected_data_type(renamed)
