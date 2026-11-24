"""A free text XML 1.0 cannot carry is reported by ``check-model`` and refused by the writer.

``rule:text.xml-unrepresentable-character`` and the writer's refusal ask **one** predicate,
:func:`swcraft_tools.xml_text.xml_unrepresentable_character`, so ``check-model`` reports every text ``generate
arxml`` refuses. At 4.2.2 and 4.3.0:

* **the predicate** is XML 1.0's ``Char`` production exactly (a NUL, every other C0 control but tab, line feed and
  carriage return, a lone surrogate, ``U+FFFE`` and ``U+FFFF``; the first one returned), agrees with ``lxml`` over
  the Basic Multilingual Plane, and lives in a leaf importing nothing from ``swcraft_tools``;
* **one function, read by both**: swapping the predicate makes both sides report and refuse;
* **the rule, both directions**: a NUL and a bell in each ``appliesTo`` field are reported with the element named and
  no storage path, and refused by the writer with nothing written; tab, line feed, carriage return and non-ASCII
  text are neither;
* **the declaration**: the ``appliesTo`` is derived from the contract and compared with the judged fields;
* **pass D** refuses such a text as ``consistency-violation``;
* **a composition's blank ``title``** differing from its ``name`` is written as its ``LONG-NAME`` and imported back
  exactly; with no ``LONG-NAME`` the ``SHORT-NAME`` is its title.

What it does **not** reach: a free text the ARXML writer writes nowhere, and the diagram writer's and the
``needs.json`` export's own escaping.
"""

from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from conftest import COMPOSITIONS_MODEL_ROOT, REPO_ROOT
from lxml import etree
from model_contract_fixtures import load_binding, load_contract
from project_support import files_of, init_project
from swcraft_tools import xml_text
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_interfaces, read_layer, read_swc_model
from swcraft_tools.schema import EnumDataType
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_xml_representable_text,
    representable_text,
    xml_unrepresentable_text_message,
)
from swcraft_tools.writer import arxml_generation, arxml_xml
from swcraft_tools.writer.arxml_generation import (
    _emit_application_type,
    _emit_enum_type,
    _emit_swc,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument
from swcraft_tools.xml_text import code_point, xml_unrepresentable_character
from test_description_emission import _describe_model

NS = f"{{{NAMESPACE}}}"
VERSIONS = ("4.2.2", "4.3.0")
RULE = "text.xml-unrepresentable-character"
BASIC = REPO_ROOT / "examples" / "basic"
PACKAGE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"

#: The characters the rule is tested with: a NUL and a C0 control other than tab, line feed and carriage return.
BELL = "\x07"
REFUSED = {"nul": "\x00", "bell": BELL}
#: Characters XML 1.0 carries: neither reported nor refused.
CARRIED = {
    "tab": "\t",
    "line-feed": "\n",
    "carriage-return": "\r",
    "latin": "é",
    "cjk": "中",
    "astral": "\U0001f600",
    "delete": "\x7f",
    "next-line": "\x85",
}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `yaml.safe_dump` escapes a control character in a double-quoted scalar,
    # which is how a model file can hold one.
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


class TestThePredicate:
    @pytest.mark.parametrize(
        "character",
        ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\ud800", "\udfff", "￾", "￿"],
        ids=code_point,
    )
    def test_a_character_outside_xml_1_0_is_found(self, character: str) -> None:
        assert xml_unrepresentable_character(f"before{character}after") == character

    @pytest.mark.parametrize("character", sorted(CARRIED.values()), ids=code_point)
    def test_a_character_xml_1_0_carries_is_not(self, character: str) -> None:
        assert xml_unrepresentable_character(f"before{character}after") is None

    def test_the_first_one_is_returned(self) -> None:
        assert xml_unrepresentable_character("a\x07b\x00c") == "\x07"

    def test_the_empty_text_holds_none(self) -> None:
        assert xml_unrepresentable_character("") is None

    def test_it_agrees_with_lxml_over_the_basic_multilingual_plane(self) -> None:
        """The oracle is the XML library the writer serializes with, not a second copy of the production."""
        disagreements = []
        for number in range(0x10000):
            character = chr(number)
            try:
                etree.Element("a").text = character
                lxml_accepts = True
            except ValueError:
                lxml_accepts = False
            if lxml_accepts != (xml_unrepresentable_character(character) is None):
                disagreements.append(code_point(character))
        assert disagreements == []

    def test_the_code_point_spelling(self) -> None:
        assert (code_point("\x00"), code_point("\x07"), code_point("\U0001f600")) == ("U+0000", "U+0007", "U+1F600")

    def test_the_leaf_imports_nothing_from_swcraft_tools(self) -> None:
        tree = ast.parse((PACKAGE / "xml_text.py").read_text(encoding="utf-8"))
        imported = [
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        assert [name for name in imported if name and name.startswith("swcraft_tools")] == []
        # A relative import is one from the package too, whatever it names.
        assert [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0] == []


# ---------------------------------------------------------------------------
# One function, read by both
# ---------------------------------------------------------------------------


#: Every function that decides whether a text is written or reported, by module.
DECIDERS = {
    "validator/representable_text.py": "check_xml_representable_text",
    "writer/arxml_xml.py": "_language_paragraph",
    "writer/arxml_generation.py": "_refuse_unrepresentable_text",
}


def _calls_in(path: Path, function: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (definition,) = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function]
    return {
        node.func.id for node in ast.walk(definition) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _imports_from_the_leaf(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.xml_text"
        for alias in node.names
    }


class TestOneFunctionReadByBoth:
    @pytest.mark.parametrize(("module", "function"), sorted(DECIDERS.items()))
    def test_each_decider_calls_the_leafs_predicate(self, module: str, function: str) -> None:
        path = PACKAGE / module
        assert "xml_unrepresentable_character" in _imports_from_the_leaf(path)
        # The decider, or a function of its own module it reaches, calls the predicate by its bare name.
        decider_module = importlib.import_module("swcraft_tools." + module.removesuffix(".py").replace("/", "."))
        defined = validator_package.module_functions(decider_module)
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "xml_unrepresentable_character"
            for each in validator_package.reached_functions(decider_module, function)
            for node in ast.walk(defined[each])
        )

    def test_the_name_each_module_calls_is_the_one_function(self) -> None:
        assert (
            representable_text.xml_unrepresentable_character
            is arxml_xml.xml_unrepresentable_character
            is arxml_generation.xml_unrepresentable_character
            is xml_text.xml_unrepresentable_character
        )

    def test_the_writer_refuses_with_the_validators_sentence(self) -> None:
        assert "xml_unrepresentable_text_message" in _calls_in(
            PACKAGE / "writer" / "arxml_generation.py", "_refuse_unrepresentable_text"
        )

    def test_a_swapped_predicate_moves_both_sides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe that bites: a predicate that finds ``§`` makes both sides report and refuse it.

        ``§`` is a character XML 1.0 carries, so without the swap the model is
        consistent and generates; with it, the rule reports the text and the
        writer refuses it -- which it could only do by asking the swapped name.

        The one deliberate exception to *no mocks in generator tests*: the swapped
        predicate is the test's subject, and the file is still generated and refused
        for real. The identity tests above hold the same fact without a patch.
        """
        model = _described(tmp_path)
        _set(model, "field:swc.swc.description", "the § sign")
        assert check_xml_representable_text(str(model)).records == ()
        generate_arxml(str(model), str(tmp_path / "before.arxml"))

        def finds_the_section_sign(text: str) -> str | None:
            return "§" if "§" in text else None

        for module in (representable_text, arxml_xml, arxml_generation):
            monkeypatch.setattr(module, "xml_unrepresentable_character", finds_the_section_sign)

        (record,) = check_xml_representable_text(str(model)).records
        assert record.subjects == (Subject("swc", "Gamma", (field_step("description"),)),)
        with pytest.raises(ValueError, match=r"holds U\+00A7, a character XML 1\.0 cannot carry"):
            generate_arxml(str(model), str(tmp_path / "after.arxml"))
        assert not (tmp_path / "after.arxml").exists()


# ---------------------------------------------------------------------------
# The fields the rule judges
# ---------------------------------------------------------------------------


def _described(tmp_path: Path) -> Path:
    """The described model of ``test_description_emission`` plus a task, a partition and a memory, each with a text.

    Plus one described inter-runnable variable on ``Gamma`` that nothing reads or
    writes, and one described exclusive area on ``Gamma`` that no runnable uses.
    """
    root = _describe_model(tmp_path / "model")
    gamma = root / "swc" / "core" / "Gamma.yaml"
    document = _load(gamma)
    document["variables"] = [
        {"name": "Shared", "access": "implicit", "implementation_type": "uint8", "description": "The variable."}
    ]
    document["exclusive_areas"] = [{"name": "Guarded", "description": "The area."}]
    _dump(gamma, document)
    _dump(
        root / "tasks" / "T10.yaml",
        {"name": "T10", "period": 0.01, "priority": 1, "partition": "PARTITION0", "description": "The task."},
    )
    _dump(
        root / "hardware" / "PARTITION0.yaml",
        {"name": "PARTITION0", "kind": "partition", "core": "CORE0", "description": "The partition."},
    )
    _dump(
        root / "hardware" / "RAM.yaml",
        {
            "name": "RAM",
            "kind": "memory",
            "type": "RAM",
            "size": 1024,
            "accessible": ["CORE0"],
            "threshold": 0.8,
            "description": "The memory.",
        },
    )
    return root


#: ``field id -> (file, key path, subject, owner, written)``: where the described
#: model holds one value of the field, the subject the rule reports it at, the
#: owner its sentence names, and whether the ARXML writer writes the value.
#: A stub, a requirement, a task and the hardware layer are written nowhere.
FIELDS: dict[str, tuple[str, tuple[Any, ...], Subject, str, bool]] = {
    "field:swc.swc.description": (
        "swc/core/Gamma.yaml",
        ("description",),
        Subject("swc", "Gamma"),
        "SWC 'Gamma'",
        True,
    ),
    "field:swc.port.description": (
        "swc/core/Gamma.yaml",
        ("ports", 0, "description"),
        Subject("swc", "Gamma", (field_step("ports"), member_step("rInner"))),
        "SWC 'Gamma' port 'rInner'",
        True,
    ),
    "field:swc.runnable.description": (
        "swc/core/Gamma.yaml",
        ("runnables", 0, "description"),
        Subject("swc", "Gamma", (field_step("runnables"), member_step("GammaMain"))),
        "SWC 'Gamma' runnable 'GammaMain'",
        True,
    ),
    # Written as the variable's DESC.
    "field:swc.variable.description": (
        "swc/core/Gamma.yaml",
        ("variables", 0, "description"),
        Subject("swc", "Gamma", (field_step("variables"), member_step("Shared"))),
        "SWC 'Gamma' variable 'Shared'",
        True,
    ),
    # Written as the area's DESC.
    "field:swc.exclusive_area.description": (
        "swc/core/Gamma.yaml",
        ("exclusive_areas", 0, "description"),
        Subject("swc", "Gamma", (field_step("exclusive_areas"), member_step("Guarded"))),
        "SWC 'Gamma' exclusive area 'Guarded'",
        True,
    ),
    "field:interfaces.sender_receiver.description": (
        "interfaces/ISignal.yaml",
        ("description",),
        Subject("interfaces", "ISignal"),
        "Interface 'ISignal'",
        True,
    ),
    "field:interfaces.data_element.description": (
        "interfaces/ISignal.yaml",
        ("data_elements", 0, "description"),
        Subject("interfaces", "ISignal", (field_step("data_elements"), member_step("value"))),
        "Interface 'ISignal' data element 'value'",
        True,
    ),
    "field:interfaces.client_server.description": (
        "interfaces/ICalc.yaml",
        ("description",),
        Subject("interfaces", "ICalc"),
        "Interface 'ICalc'",
        True,
    ),
    "field:interfaces.operation.description": (
        "interfaces/ICalc.yaml",
        ("operations", 0, "description"),
        Subject("interfaces", "ICalc", (field_step("operations"), member_step("Op"))),
        "Interface 'ICalc' operation 'Op'",
        True,
    ),
    "field:interfaces.argument.description": (
        "interfaces/ICalc.yaml",
        ("operations", 0, "arguments", 0, "description"),
        Subject(
            "interfaces",
            "ICalc",
            (field_step("operations"), member_step("Op"), field_step("arguments"), member_step("x")),
        ),
        "Interface 'ICalc' operation 'Op' argument 'x'",
        True,
    ),
    "field:interfaces.external_interface.description": (
        "interfaces/IExt.yaml",
        ("description",),
        Subject("interfaces", "IExt"),
        "Interface 'IExt'",
        False,
    ),
    "field:connectors.connector.description": (
        "connectors/sub_link.yaml",
        ("description",),
        Subject("connectors", "sub_link"),
        "Connector 'sub_link'",
        True,
    ),
    "field:base_types.base_type.description": (
        "data_types/base/uint8_base.yaml",
        ("description",),
        Subject("base_types", "uint8_base"),
        "Data type 'uint8_base'",
        True,
    ),
    "field:base_types.base_type.native_declaration": (
        "data_types/base/uint8_base.yaml",
        ("native_declaration",),
        Subject("base_types", "uint8_base"),
        "Base type 'uint8_base'",
        True,
    ),
    "field:base_types.base_type.encoding": (
        "data_types/base/uint8_base.yaml",
        ("encoding",),
        Subject("base_types", "uint8_base"),
        "Base type 'uint8_base'",
        True,
    ),
    "field:implementation_types.value_type.description": (
        "data_types/implementation/uint8.yaml",
        ("description",),
        Subject("implementation_types", "uint8"),
        "Data type 'uint8'",
        True,
    ),
    "field:implementation_types.reference_type.description": (
        "data_types/implementation/U8Ref.yaml",
        ("description",),
        Subject("implementation_types", "U8Ref"),
        "Data type 'U8Ref'",
        True,
    ),
    "field:application_types.enum.description": (
        "data_types/application/Mode_T.yaml",
        ("description",),
        Subject("application_types", "Mode_T"),
        "Data type 'Mode_T'",
        True,
    ),
    "field:application_types.enum_literal.description": (
        "data_types/application/Mode_T.yaml",
        ("literals", 0, "description"),
        Subject("application_types", "Mode_T", (field_step("literals"), member_step("OFF"))),
        "Enum 'Mode_T' literal 'OFF'",
        True,
    ),
    "field:application_types.record.description": (
        "data_types/application/Pair_T.yaml",
        ("description",),
        Subject("application_types", "Pair_T"),
        "Data type 'Pair_T'",
        True,
    ),
    "field:application_types.record_field.description": (
        "data_types/application/Pair_T.yaml",
        ("fields", 0, "description"),
        Subject("application_types", "Pair_T", (field_step("fields"), member_step("level"))),
        "Record 'Pair_T' field 'level'",
        True,
    ),
    "field:application_types.array.description": (
        "data_types/application/Levels_T.yaml",
        ("description",),
        Subject("application_types", "Levels_T"),
        "Data type 'Levels_T'",
        True,
    ),
    "field:application_types.application_type.description": (
        "data_types/application/Level_T.yaml",
        ("description",),
        Subject("application_types", "Level_T"),
        "Data type 'Level_T'",
        True,
    ),
    "field:application_types.application_type.unit": (
        "data_types/application/Level_T.yaml",
        ("unit",),
        Subject("application_types", "Level_T"),
        "Application type 'Level_T'",
        True,
    ),
    "field:implementation_types.external_type.description": (
        "data_types/implementation/Counter_T.yaml",
        ("description",),
        Subject("implementation_types", "Counter_T"),
        "Data type 'Counter_T'",
        False,
    ),
    "field:implementation_types.implementation_record.description": (
        "data_types/implementation/Frame_T.yaml",
        ("description",),
        Subject("implementation_types", "Frame_T"),
        "Data type 'Frame_T'",
        True,
    ),
    "field:implementation_types.implementation_element.description": (
        "data_types/implementation/Frame_T.yaml",
        ("elements", 0, "description"),
        Subject("implementation_types", "Frame_T", (field_step("elements"), member_step("head"))),
        "Implementation record 'Frame_T' element 'head'",
        True,
    ),
    "field:implementation_types.implementation_array.description": (
        "data_types/implementation/Buf_T.yaml",
        ("description",),
        Subject("implementation_types", "Buf_T"),
        "Data type 'Buf_T'",
        True,
    ),
    "field:implementation_types.pointer.description": (
        "data_types/implementation/Buf_P.yaml",
        ("description",),
        Subject("implementation_types", "Buf_P"),
        "Data type 'Buf_P'",
        True,
    ),
    "field:mapping_sets.mapping_set.description": (
        "data_types/mapping_sets/Maps.yaml",
        ("description",),
        Subject("mapping_sets", "Maps"),
        "Mapping set 'Maps'",
        True,
    ),
    "field:compositions.composition.title": (
        "compositions/Sub.yaml",
        ("title",),
        Subject("compositions", "Sub"),
        "Composition 'Sub'",
        True,
    ),
    "field:compositions.composition.description": (
        "compositions/Sub.yaml",
        ("description",),
        Subject("compositions", "Sub"),
        "Composition 'Sub'",
        True,
    ),
    "field:compositions.composition_port.description": (
        "compositions/Sub.yaml",
        ("ports", 1, "description"),
        Subject("compositions", "Sub", (field_step("ports"), member_step("pSignalOut"))),
        "Composition 'Sub' port 'pSignalOut'",
        True,
    ),
    "field:compositions.composition_member.description": (
        "compositions/Sub.yaml",
        ("members", 0, "description"),
        Subject("compositions", "Sub", (field_step("members"), member_step("Alpha"))),
        "Composition 'Sub' member 'Alpha'",
        True,
    ),
    "field:compositions.delegation.description": (
        "compositions/Sub.yaml",
        ("delegations", 0, "description"),
        Subject("compositions", "Sub", (field_step("delegations"), member_step("Sub_pSignalOut_Alpha"))),
        "Composition 'Sub' delegation 'Sub_pSignalOut_Alpha'",
        True,
    ),
    "field:requirements.requirement.description": (
        "requirements/REQ_COMPOSITIONS_001.yaml",
        ("description",),
        Subject("requirements", "REQ_COMPOSITIONS_001"),
        "Requirement 'REQ_COMPOSITIONS_001'",
        False,
    ),
    "field:tasks.task.description": (
        "tasks/T10.yaml",
        ("description",),
        Subject("tasks", "T10"),
        "Task 'T10'",
        False,
    ),
    "field:hardware.processor.description": (
        "hardware/CPU.yaml",
        ("description",),
        Subject("hardware", "CPU"),
        "Hardware processor 'CPU'",
        False,
    ),
    "field:hardware.core.description": (
        "hardware/CORE0.yaml",
        ("description",),
        Subject("hardware", "CORE0"),
        "Hardware core 'CORE0'",
        False,
    ),
    "field:hardware.memory.description": (
        "hardware/RAM.yaml",
        ("description",),
        Subject("hardware", "RAM"),
        "Hardware memory 'RAM'",
        False,
    ),
    "field:hardware.partition.description": (
        "hardware/PARTITION0.yaml",
        ("description",),
        Subject("hardware", "PARTITION0"),
        "Hardware partition 'PARTITION0'",
        False,
    ),
}


#: The texts of the unit, compu-method and data-constraint layers, in the same
#: ``(file, key path, subject, owner)`` shape. Kept out of :data:`FIELDS` because the
#: described model declares no element of these layers: each is planted into a copy
#: by :func:`_with_elements`.
ELEMENT_FIELDS: dict[str, tuple[str, tuple[Any, ...], Subject, str]] = {
    "field:units.unit.display_name": (
        "data_types/units/Kmh.yaml",
        ("display_name",),
        Subject("units", "Kmh"),
        "Unit 'Kmh'",
    ),
    "field:units.unit.description": (
        "data_types/units/Kmh.yaml",
        ("description",),
        Subject("units", "Kmh"),
        "Unit 'Kmh'",
    ),
    "field:compu_methods.texttable.description": (
        "data_types/compu_methods/GearTable.yaml",
        ("description",),
        Subject("compu_methods", "GearTable"),
        "Compu method 'GearTable'",
    ),
    "field:compu_methods.table_entry.description": (
        "data_types/compu_methods/GearTable.yaml",
        ("entries", 0, "description"),
        Subject("compu_methods", "GearTable", (field_step("entries"), member_step("Park"))),
        "Compu method 'GearTable' entry 'Park'",
    ),
    "field:compu_methods.linear.description": (
        "data_types/compu_methods/SpeedCompu.yaml",
        ("description",),
        Subject("compu_methods", "SpeedCompu"),
        "Compu method 'SpeedCompu'",
    ),
    "field:data_constraints.data_constr.description": (
        "data_types/constraints/SpeedLimits.yaml",
        ("description",),
        Subject("data_constraints", "SpeedLimits"),
        "Data constraint 'SpeedLimits'",
    ),
}


def _with_elements(model: Path) -> Path:
    """*model* with one element of each of the three layers, every text of them stated and representable."""
    _dump(model / "data_types/units/Kmh.yaml", {"name": "Kmh", "display_name": "km/h", "description": "Speed."})
    _dump(
        model / "data_types/compu_methods/GearTable.yaml",
        {
            "name": "GearTable",
            "kind": "texttable",
            "entries": [{"name": "Park", "value": 0, "description": "Parked."}],
            "description": "The gears.",
        },
    )
    _dump(
        model / "data_types/compu_methods/SpeedCompu.yaml",
        {"name": "SpeedCompu", "kind": "linear", "unit": "Kmh", "description": "The scaling."},
    )
    _dump(
        model / "data_types/constraints/SpeedLimits.yaml", {"name": "SpeedLimits", "max": 400, "description": "Limits."}
    )
    return model


def _set_element_text(model: Path, field_id: str, text: str) -> None:
    relative, keys, *_ = ELEMENT_FIELDS[field_id]
    document = _load(model / relative)
    holder: Any = document
    for key in keys[:-1]:
        holder = holder[key]
    holder[keys[-1]] = text
    _dump(model / relative, document)


#: The fields an emitter refuses **before its first element**, in ``check-model``'s
#: own sentence: the texts written as plain element content, and every text of an
#: emitter that puts more than one element into its package.
REFUSED_UP_FRONT = frozenset(
    {
        "field:swc.swc.description",
        "field:swc.port.description",
        "field:swc.runnable.description",
        # `_emit_swc` judges an area's description before the component's first element.
        "field:swc.exclusive_area.description",
        "field:base_types.base_type.native_declaration",
        "field:base_types.base_type.encoding",
        "field:implementation_types.value_type.description",
        "field:application_types.enum.description",
        "field:application_types.enum_literal.description",
        "field:application_types.application_type.description",
        "field:application_types.application_type.unit",
    }
)


def _field_name(field_id: str) -> str:
    return field_id.rsplit(".", 1)[-1]


def _set(model: Path, field_id: str, text: str) -> None:
    relative, keys, *_ = FIELDS[field_id]
    document = _load(model / relative)
    holder: Any = document
    for key in keys[:-1]:
        holder = holder[key]
    holder[keys[-1]] = text
    _dump(model / relative, document)


def _applies_to() -> set[str]:
    (declared,) = [rule for rule in load_contract()["rules"] if rule["id"] == f"rule:{RULE}"]
    return set(declared["appliesTo"])


class TestTheDeclaration:
    def test_declared_bound_and_answered_with_consistency_violation(self) -> None:
        (declared,) = [rule for rule in load_contract()["rules"] if rule["id"] == f"rule:{RULE}"]
        assert declared["family"] == "cross-element"
        assert declared["summary"] == CONSISTENCY_RULES[RULE]
        # The one emission site, which every phase of `check_xml_representable_text` judges its texts through.
        assert load_binding()["rules"][f"rule:{RULE}"] == {
            "module": "swcraft_tools.validator.representable_text",
            "callable": "_judge_text",
        }
        assert "_judge_text" in validator_package.reached_functions(representable_text, "check_xml_representable_text")
        assert RULE_CODES[RULE].code == "consistency-violation"

    def test_the_applies_to_is_every_free_text_the_writer_writes_and_every_description(self) -> None:
        """Derived from the contract and compared in both directions."""
        derived = {
            field["id"]
            for layer in load_contract()["layers"]
            for kind in layer["kinds"]
            for field in kind["fields"]
            if _field_name(field["id"]) in {"description", "native_declaration"}
        } | {
            "field:compositions.composition.title",
            "field:base_types.base_type.encoding",
            "field:application_types.application_type.unit",
            # Written as the `UNIT`'s `DISPLAY-NAME`.
            "field:units.unit.display_name",
        }
        assert _applies_to() == derived

    def test_the_check_judges_exactly_the_applies_to(self) -> None:
        assert set(FIELDS) | set(ELEMENT_FIELDS) == _applies_to()
        assert not set(FIELDS) & set(ELEMENT_FIELDS)

    def test_the_contract_rule_counts_by_family(self) -> None:
        families = [rule["family"] for rule in load_contract()["rules"]]
        # The contract's rule count: total, cross-element and element-local. A rule the
        # contract adds or retires moves them.
        assert len(families) == 352
        assert (families.count("cross-element"), families.count("element-local")) == (207, 144)


class TestTheRuleBothDirections:
    @pytest.fixture(scope="class")
    @staticmethod
    def described(tmp_path_factory: pytest.TempPathFactory) -> Path:
        return _described(tmp_path_factory.mktemp("described"))

    @pytest.fixture
    @staticmethod
    def model(described: Path, tmp_path: Path) -> Path:
        copy = tmp_path / "model"
        shutil.copytree(described, copy)
        return copy

    def test_the_described_model_reports_nothing_and_generates(self, described: Path, tmp_path: Path) -> None:
        assert check_xml_representable_text(str(described)).records == ()
        for version in VERSIONS:
            generate_arxml(str(described), str(tmp_path / f"{version}.arxml"), version=version)

    @pytest.mark.parametrize("character", sorted(REFUSED), ids=sorted(REFUSED))
    @pytest.mark.parametrize("field_id", sorted(FIELDS))
    def test_reported_with_the_element_named_and_no_storage_path(
        self, model: Path, field_id: str, character: str
    ) -> None:
        _relative, _keys, element, owner, _written = FIELDS[field_id]
        _set(model, field_id, f"before{REFUSED[character]}after")

        (record,) = check_xml_representable_text(str(model)).records

        assert record.rule == RULE
        assert record.subjects == (element.at(field_step(_field_name(field_id))),)
        assert record.message == xml_unrepresentable_text_message(owner, _field_name(field_id), REFUSED[character])
        assert f"{owner} `{_field_name(field_id)}` holds {code_point(REFUSED[character])}" in record.message
        assert str(model) not in record.message
        assert ".yaml" not in record.message
        assert record.message in [issue.message for issue in check_cross_layer_consistency(str(model)).records]

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("field_id", sorted(FIELDS))
    def test_the_writer_refuses_every_field_it_writes_and_writes_nothing(
        self, model: Path, tmp_path: Path, field_id: str, version: str
    ) -> None:
        _relative, _keys, _element, owner, written = FIELDS[field_id]
        _set(model, field_id, f"before{BELL}after")
        out = tmp_path / "out" / "model.arxml"

        if not written:
            # Written nowhere, so nothing to refuse: `check-model` reports more
            # than the writer refuses, never less.
            generate_arxml(str(model), str(out), version=version)
            assert BELL not in out.read_text(encoding="utf-8")
            return

        with pytest.raises(ValueError) as refused:
            generate_arxml(str(model), str(out), version=version)
        field = _field_name(field_id)
        if field_id in REFUSED_UP_FRONT:
            assert str(refused.value) == f"rule:{RULE}: {xml_unrepresentable_text_message(owner, field, BELL)}"
        else:
            # Every other DESC / LONG-NAME is refused by the paragraph builder,
            # in the XML element's terms; the rule and the sentence after the
            # subject are the same either way.
            assert str(refused.value).startswith(f"rule:{RULE}: ")
            assert str(refused.value).endswith(xml_unrepresentable_text_message("", "", BELL).split(" holds ", 1)[1])
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("character", sorted(REFUSED), ids=sorted(REFUSED))
    @pytest.mark.parametrize("field_id", sorted(ELEMENT_FIELDS))
    def test_an_element_text_is_reported_whether_or_not_a_type_names_the_element(
        self, model: Path, tmp_path: Path, field_id: str, character: str
    ) -> None:
        """Each element layer's text is judged where it is declared; the writer refuses the same text first.

        Before any element is emitted, in ``check-model``'s own sentence.
        """
        _with_elements(model)
        assert check_xml_representable_text(str(model)).records == ()
        _relative, _keys, element, owner = ELEMENT_FIELDS[field_id]
        _set_element_text(model, field_id, f"before{REFUSED[character]}after")

        (record,) = check_xml_representable_text(str(model)).records

        assert record.subjects == (element.at(field_step(_field_name(field_id))),)
        assert record.message == xml_unrepresentable_text_message(owner, _field_name(field_id), REFUSED[character])
        assert str(model) not in record.message
        out = tmp_path / "out" / "model.arxml"
        with pytest.raises(ValueError) as refused:
            generate_arxml(str(model), str(out))
        assert str(refused.value) == f"rule:{RULE}: {record.message}"
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("character", sorted(CARRIED), ids=sorted(CARRIED))
    def test_a_character_xml_carries_is_neither_reported_nor_refused(
        self, model: Path, tmp_path: Path, character: str
    ) -> None:
        text = f"before{CARRIED[character]}after"
        for field_id in FIELDS:
            _set(model, field_id, text)

        assert check_xml_representable_text(str(model)).records == ()
        for version in VERSIONS:
            generate_arxml(str(model), str(tmp_path / f"{version}.arxml"), version=version)

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_unit_is_judged_as_the_writer_writes_it_exactly(self, model: Path, tmp_path: Path, version: str) -> None:
        """A unit is judged as the writer writes it, exactly.

        ``\\x1f`` is outside XML 1.0 and Python whitespace: a trailing one is written with the unit, which is written
        exactly, so it is reported and refused like one in the middle."""
        _set(model, "field:application_types.application_type.unit", "km/h\x1f")

        (record,) = check_xml_representable_text(str(model)).records
        assert record.rule == RULE
        with pytest.raises(ValueError, match=f"rule:{RULE}: "):
            generate_arxml(str(model), str(tmp_path / "out" / "model.arxml"), version=version)
        assert not (tmp_path / "out").exists()


class TestARefusedEmitterLeavesNothingInItsPackage:
    """An emitter that puts several elements into its package refuses a text before the first one.

    ``generate arxml`` writes nothing on any refusal; this is the emitter's own
    property, for a caller that catches the ``ValueError`` and serializes the
    document: no generated sibling of a refused element is left in the package.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def described(tmp_path_factory: pytest.TempPathFactory) -> Path:
        return _described(tmp_path_factory.mktemp("described"))

    @pytest.fixture
    @staticmethod
    def model(described: Path, tmp_path: Path) -> Path:
        copy = tmp_path / "model"
        shutil.copytree(described, copy)
        return copy

    @pytest.mark.parametrize(
        "field_id",
        [
            "field:application_types.enum.description",
            "field:application_types.enum_literal.description",
            "field:application_types.application_type.description",
            "field:application_types.application_type.unit",
        ],
    )
    def test_a_data_type_with_generated_forms(self, model: Path, field_id: str) -> None:
        relative, _keys, _element, owner, _written = FIELDS[field_id]
        # A unit gives the application type a generated sibling emitted before it.
        _set(model, "field:application_types.application_type.unit", "km/h")
        _set(model, field_id, f"before{BELL}after")
        name = Path(relative).stem
        (declared,) = [data_type for data_type in read_layer(str(model), "application_types") if data_type.name == name]
        emit = _emit_enum_type if isinstance(declared, EnumDataType) else _emit_application_type
        document = ArxmlDocument()

        with pytest.raises(ValueError) as refused:
            emit(document.data_types, declared)

        message = xml_unrepresentable_text_message(owner, _field_name(field_id), BELL)
        assert str(refused.value) == f"rule:{RULE}: {message}"
        assert not document.data_types.has_elements

    def test_the_application_type_of_the_cases_has_a_generated_sibling(self, model: Path) -> None:
        """The control: carried, the type the cases above refuse emits its ``UNIT`` before itself."""
        _set(model, "field:application_types.application_type.unit", "km/h")
        (declared,) = [
            data_type for data_type in read_layer(str(model), "application_types") if data_type.name == "Level_T"
        ]
        document = ArxmlDocument()
        _emit_application_type(document.data_types, declared)
        assert [etree.QName(node).localname for node in document.data_types.elements] == [
            "UNIT",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        ]

    @pytest.mark.parametrize(
        "field_id",
        ["field:swc.swc.description", "field:swc.port.description", "field:swc.runnable.description"],
    )
    def test_a_component(self, model: Path, field_id: str) -> None:
        _relative, _keys, _element, owner, _written = FIELDS[field_id]
        _set(model, field_id, f"before{BELL}after")
        (swc,) = [swc for swc in read_swc_model(str(model)) if swc.name == "Gamma"]
        interfaces = {interface.name: interface for interface in read_interfaces(str(model))}
        document = ArxmlDocument()

        with pytest.raises(ValueError) as refused:
            _emit_swc(document.component_types, swc, interfaces)

        assert str(refused.value) == f"rule:{RULE}: {xml_unrepresentable_text_message(owner, 'description', BELL)}"
        assert not document.component_types.has_elements


# ---------------------------------------------------------------------------
# check-model, generate arxml and pass D over examples/basic
# ---------------------------------------------------------------------------


def _basic(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _swcraft(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *arguments, "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


BASIC_MESSAGE = xml_unrepresentable_text_message("SWC 'ExampleSWC'", "description", "\x07")


@pytest.mark.parametrize("version", VERSIONS)
def test_check_model_reports_it_and_generate_arxml_writes_nothing(version: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    manifest = _load(project / "swcraft.yaml")
    manifest["arxml_version"] = version
    _dump(project / "swcraft.yaml", manifest)
    swc = project / "model" / "swc" / "example" / "ExampleSWC.yaml"
    document = _load(swc)
    document["description"] = "Rings\x07 the bell."
    _dump(swc, document)

    checked = _swcraft(project, "check-model")
    assert checked.returncode == 1, checked.stdout
    assert BASIC_MESSAGE in checked.stderr, checked.stderr
    assert str(project) not in checked.stderr

    before = files_of(project)
    generated = _swcraft(project, "generate", "arxml")
    assert generated.returncode != 0, generated.stdout
    assert files_of(project) == before


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


CHANGES: dict[str, tuple[dict[str, Any], str]] = {
    "description": (
        {
            "operation": "update",
            "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
            "value": "Rings\x07 the bell.",
        },
        BASIC_MESSAGE,
    ),
    "native_declaration": (
        {
            "operation": "update",
            "address": {"layer": "base_types", "element": "uint8_base", "path": [{"field": "native_declaration"}]},
            "value": "uint8_t\x00",
        },
        xml_unrepresentable_text_message("Base type 'uint8_base'", "native_declaration", "\x00"),
    ),
    "title": (
        {
            "operation": "update",
            "address": {"layer": "compositions", "element": "Root", "path": [{"field": "title"}]},
            "value": "The\x1b root",
        },
        xml_unrepresentable_text_message("Composition 'Root'", "title", "\x1b"),
    ),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_pass_d_refuses_it_as_a_consistency_violation(change: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    before = files_of(project / "model")
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    operation, message = CHANGES[change]

    response = handle_request(
        {"operation": "change_set", "baseline": token, "intent": "a text XML cannot carry", "operations": [operation]},
        project=str(project),
        review=_approve,
    )

    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    messages = [response["message"], *(entry["message"] for entry in response.get("additional", []))]
    assert any(entry.startswith(message) for entry in messages), response
    assert str(project) not in str(response)
    assert files_of(project / "model") == before


# ---------------------------------------------------------------------------
# A composition's title, read verbatim
# ---------------------------------------------------------------------------


def _round_trip(
    tmp_path: Path, version: str, edit: Callable[[etree._ElementTree], None] | None = None, **titles: str
) -> dict[str, str]:
    """Generate the ``compositions`` fixture with *titles* at *version*, import it back; every title by name."""
    model = tmp_path / "model"
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, model)
    for name, title in titles.items():
        path = model / "compositions" / f"{name}.yaml"
        document = _load(path)
        document["title"] = title
        _dump(path, document)
    out = tmp_path / "written.arxml"
    generate_arxml(str(model), str(out), version=version)
    if edit is not None:
        tree = etree.parse(str(out))
        edit(tree)
        tree.write(str(out), xml_declaration=True, encoding="utf-8")
    project = init_project(tmp_path / "imported")
    run_import(str(out), str(project / "swcraft.yaml"), force=True)
    return {
        document["name"]: document["title"]
        for document in (_load(path) for path in (project / "model" / "compositions").rglob("*.yaml"))
    }


def _long_name_texts(path: Path) -> dict[str, str | None]:
    root = etree.parse(str(path)).getroot()
    return {
        node.findtext(f"{NS}SHORT-NAME"): node.findtext(f"{NS}LONG-NAME/{NS}L-4")
        for node in root.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE")
        if node.find(f"{NS}LONG-NAME") is not None
    }


class TestABlankTitleRoundTrips:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("title", ["  ", ""], ids=["whitespace", "empty"])
    def test_written_as_its_long_name_and_imported_back_exactly(self, tmp_path: Path, version: str, title: str) -> None:
        titles = _round_trip(tmp_path, version, Sub=title)

        assert _long_name_texts(tmp_path / "written.arxml")["Sub"] == title
        assert titles["Sub"] == title

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_composition_without_a_long_name_takes_its_short_name(self, tmp_path: Path, version: str) -> None:
        def strip_long_names(tree: etree._ElementTree) -> None:
            for node in list(tree.iter(f"{NS}LONG-NAME")):
                node.getparent().remove(node)

        titles = _round_trip(tmp_path, version, strip_long_names, Sub="  ")

        assert titles and all(title == name for name, title in titles.items())

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_title_equal_to_the_name_writes_no_long_name_and_comes_back(self, tmp_path: Path, version: str) -> None:
        titles = _round_trip(tmp_path, version, Sub="Sub")

        assert "Sub" not in _long_name_texts(tmp_path / "written.arxml")
        assert titles["Sub"] == "Sub"
