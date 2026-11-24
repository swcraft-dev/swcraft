"""The rules of the data description elements, and what a type states whichever form states it.

**The rules.** Ten cross-element rules over the three element layers (``units``, ``compu_methods``,
``data_constraints``), one predicate (``validator.data_description_element_records``) every reader
asks:

* a name repeated inside a layer: ``unit.duplicate-name``, ``compu-method.duplicate-name``,
  ``data-constr.duplicate-name``;
* a reference field naming no element of its layer: ``data-type.unknown-unit``,
  ``data-type.unknown-compu-method``, ``data-type.unknown-data-constr``, ``compu-method.unknown-unit``;
* the role a named compu method plays: ``data-type.compu-method-wrong-category`` (``[constr_1158]``
  for an implementation type), ``data-type.compu-method-on-unresolved-reference-type``
  (``[constr_1383]``) and ``compu-method.table-entry-repeated``.

Each is judged by ``check-model``, by ``validate_component_closure`` and by pass D of the editing
protocol (``consistency-violation``); the ARXML writer refuses the same model with the rule's own
message before it writes anything. ``data-type.conversion-without-unit`` takes ``unit`` **or**
``unit_ref``; ``text.xml-unrepresentable-character`` judges every free text of every declared
element; each element claims its name in its layer's package (``name.name-space-collision``).

**What a type states.** ``stated_unit_text``, ``stated_conversion``, ``stated_range`` and
``stated_literals`` answer the inline field or the referenced element's value; interface
compatibility and the ``init`` rules read them.

**Not reached:** the writer's emission of the elements (``test_data_description_writer.py``), the
component closure following the reference fields (a closure tree is handed to
``validate_component_closure`` directly), and every importer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from conftest import REPO_ROOT, model_element_path, write_model_element
from model_contract_fixtures import load_binding, load_contract
from project_support import files_of
from swcraft_tools.data_description import DataDescription
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.schema import (
    ApplicationDataType,
    Conversion,
    ConversionLimitBound,
    ConversionLimits,
    DataConstr,
    DataElement,
    EnumDataType,
    EnumLiteral,
    Linear,
    LinearLimitBound,
    LinearLimits,
    SRInterface,
    TableEntry,
    TextTable,
    Unit,
    ValueRange,
)

BASIC = REPO_ROOT / "examples" / "basic"

#: The ten element rules, all minted by the one predicate.
RULES = (
    "unit.duplicate-name",
    "compu-method.duplicate-name",
    "data-constr.duplicate-name",
    "data-type.unknown-unit",
    "data-type.unknown-compu-method",
    "data-type.unknown-data-constr",
    "compu-method.unknown-unit",
    "data-type.compu-method-wrong-category",
    "data-type.compu-method-on-unresolved-reference-type",
    "compu-method.table-entry-repeated",
)

#: The step of the predicate that mints each, the function `binding.json` names.
BOUND_STEPS: dict[str, str] = {
    "unit.duplicate-name": "_duplicate_form_name_records",
    "compu-method.duplicate-name": "_duplicate_form_name_records",
    "data-constr.duplicate-name": "_duplicate_form_name_records",
    "data-type.unknown-unit": "_application_reference_records",
    "data-type.unknown-compu-method": "_compu_method_reference_records",
    "data-type.unknown-data-constr": "_application_reference_records",
    "compu-method.unknown-unit": "_compu_method_records",
    "data-type.compu-method-wrong-category": "_compu_method_reference_records",
    "data-type.compu-method-on-unresolved-reference-type": "_compu_method_reference_records",
    "compu-method.table-entry-repeated": "_compu_method_records",
}


# Builders over a copy of examples/basic (invented names only)


def _basic(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _put(model: Path, relative: str, document: dict[str, Any]) -> None:
    path = model / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _unit(model: Path, name: str = "Kmh", **fields: Any) -> None:
    write_model_element(model, {"name": name, **fields}, layer="units")


def _texttable(model: Path, name: str = "ProbeGearTable", *entries: tuple[str, int], **fields: Any) -> None:
    rows = entries or (("PARK", 0), ("DRIVE", 1))
    write_model_element(
        model,
        {"name": name, "kind": "texttable", "entries": [{"name": n, "value": v} for n, v in rows], **fields},
    )


def _linear(model: Path, name: str = "ProbeLinearScale", unit: str = "Kmh", **fields: Any) -> None:
    write_model_element(model, {"name": name, "kind": "linear", "unit": unit, **fields})


def _constraint(model: Path, name: str = "ProbeLimits", **fields: Any) -> None:
    write_model_element(model, {"name": name, **fields}, layer="data_constraints")


def _type(model: Path, payload: dict[str, Any]) -> None:
    write_model_element(model, payload)


def _records(model: Path, rule: str) -> list[Any]:
    from swcraft_tools.validator import check_cross_layer_consistency

    return [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]


def _messages(model: Path, rule: str) -> list[str]:
    return [record.message for record in _records(model, rule)]


# Each rule's break: a copy of the example, broken once


def _break_unit_duplicate(model: Path) -> None:
    _unit(model, "Kmh")
    _put(model, "data_types/units/Kmh_again.yaml", {"name": "Kmh"})


def _break_compu_method_duplicate(model: Path) -> None:
    _texttable(model, "ProbeGearTable")
    _unit(model, "Kmh")
    _put(
        model,
        "data_types/compu_methods/ProbeGearTable_again.yaml",
        {"name": "ProbeGearTable", "kind": "linear", "unit": "Kmh"},
    )


def _break_data_constr_duplicate(model: Path) -> None:
    _constraint(model, "ProbeLimits", min=0)
    _put(model, "data_types/constraints/ProbeLimits_again.yaml", {"name": "ProbeLimits", "max": 9})


def _break_unknown_unit(model: Path) -> None:
    _type(model, {"name": "ProbeSpeed", "kind": "application_type", "unit_ref": "Kmh"})


def _break_unknown_compu_method(model: Path) -> None:
    _type(model, {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeGearTable"})


def _break_unknown_data_constr(model: Path) -> None:
    _type(model, {"name": "ProbeSpeed", "kind": "application_type", "data_constr_ref": "ProbeLimits"})


def _break_compu_method_unknown_unit(model: Path) -> None:
    _linear(model, "ProbeLinearScale", unit="Kmh")


def _break_wrong_category(model: Path) -> None:
    _texttable(model, "ProbeGearTable")
    _type(model, {"name": "ProbeSpeed", "kind": "application_type", "compu_method_ref": "ProbeGearTable"})


def _break_unresolved_reference_type(model: Path) -> None:
    _texttable(model, "ProbeGearTable")
    _type(model, {"name": "ProbeRecord", "kind": "implementation_record", "elements": [{"name": "a", "type": "uint8"}]})
    _type(
        model,
        {
            "name": "ProbeRecordRef",
            "kind": "reference_type",
            "base": "ProbeRecord",
            "compu_method_ref": "ProbeGearTable",
        },
    )


def _break_table_entry_repeated(model: Path) -> None:
    _texttable(model, "ProbeGearTable", ("PARK", 0), ("PARK", 1))


BREAKS: dict[str, tuple[Callable[[Path], None], str]] = {
    "unit.duplicate-name": (_break_unit_duplicate, "Duplicate unit name 'Kmh' declared in multiple files."),
    "compu-method.duplicate-name": (
        _break_compu_method_duplicate,
        "Duplicate compu method name 'ProbeGearTable' declared in multiple files.",
    ),
    "data-constr.duplicate-name": (
        _break_data_constr_duplicate,
        "Duplicate data constraint name 'ProbeLimits' declared in multiple files.",
    ),
    "data-type.unknown-unit": (
        _break_unknown_unit,
        "Application type 'ProbeSpeed' unit_ref 'Kmh' is not declared in the units layer.",
    ),
    "data-type.unknown-compu-method": (
        _break_unknown_compu_method,
        "Enum 'ProbeGear' compu_method_ref 'ProbeGearTable' is not declared in the compu_methods layer.",
    ),
    "data-type.unknown-data-constr": (
        _break_unknown_data_constr,
        "Application type 'ProbeSpeed' data_constr_ref 'ProbeLimits' is not declared in the data_constraints layer.",
    ),
    "compu-method.unknown-unit": (
        _break_compu_method_unknown_unit,
        "Linear compu method 'ProbeLinearScale' unit 'Kmh' is not declared in the units layer.",
    ),
    "data-type.compu-method-wrong-category": (
        _break_wrong_category,
        "Application type 'ProbeSpeed' compu_method_ref 'ProbeGearTable' names a texttable compu method; an "
        "application type's conversion is a linear compu method, and a texttable's entries are an enum's values "
        "(name the texttable from an enum).",
    ),
    "data-type.compu-method-on-unresolved-reference-type": (
        _break_unresolved_reference_type,
        "Reference type 'ProbeRecordRef' states compu_method_ref 'ProbeGearTable', but its reference chain "
        "'ProbeRecordRef' -> 'ProbeRecord' ends in an implementation record and not in a value type: a "
        "TYPE_REFERENCE type's compu method is allowed only where the resolved chain ends in a VALUE type "
        "([constr_1383]).",
    ),
    "compu-method.table-entry-repeated": (
        _break_table_entry_repeated,
        "Texttable compu method 'ProbeGearTable' has duplicate entry name(s) ['PARK']; entry names must be unique "
        "within a texttable.",
    ),
}


# The rules: declared, bound, answered


class TestTheRulesAreDeclaredBoundAndAnswered:
    def test_the_breaks_cover_exactly_the_ten(self) -> None:
        assert set(BREAKS) == set(RULES)

    @pytest.mark.parametrize("rule", RULES)
    def test_declared_bound_and_answered_with_consistency_violation(self, rule: str) -> None:
        from swcraft_tools.validator import CONSISTENCY_RULES, description_elements

        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == f"rule:{rule}"]
        assert declared["family"] == "cross-element"
        assert declared["summary"] == CONSISTENCY_RULES[rule]
        # The step of `data_description_element_records` that mints it, which the function reaches.
        assert load_binding()["rules"][f"rule:{rule}"] == {
            "module": "swcraft_tools.validator.description_elements",
            "callable": BOUND_STEPS[rule],
        }
        assert BOUND_STEPS[rule] in validator_package.reached_functions(
            description_elements, "data_description_element_records"
        )
        assert RULE_CODES[rule].code == "consistency-violation"

    def test_the_contract_counts(self) -> None:
        families = [entry["family"] for entry in load_contract()["rules"]]
        # The contract's rules in all and per family (the one other rule is schema-config); a rule added,
        # removed or moved between families moves them.
        assert len(families) == 352
        assert (families.count("cross-element"), families.count("element-local")) == (207, 144)

    def test_the_conversion_rule_names_the_reference_field(self) -> None:
        (declared,) = [
            entry for entry in load_contract()["rules"] if entry["id"] == "rule:data-type.conversion-without-unit"
        ]
        assert "field:application_types.application_type.unit_ref" in declared["appliesTo"]
        assert "neither a `unit` nor a `unit_ref`" in declared["summary"]

    def test_the_name_space_rule_names_each_elements_name_and_package(self) -> None:
        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == "rule:name.name-space-collision"]
        for kind in ("units.unit", "compu_methods.texttable", "compu_methods.linear", "data_constraints.data_constr"):
            assert f"field:{kind}.name" in declared["appliesTo"]
            assert f"field:{kind}.package" in declared["appliesTo"]


# Each rule through check-model, the closure check and the writer


@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_each_rule_fires_on_its_break_with_its_message(rule: str, tmp_path: Path) -> None:
    model = _basic(tmp_path) / "model"
    assert _records(model, rule) == []

    BREAKS[rule][0](model)

    assert _messages(model, rule) == [BREAKS[rule][1]]


def _check_model(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_check_model_lists_each_rule(rule: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    BREAKS[rule][0](project / "model")

    result = _check_model(project)

    assert result.returncode == 1, result.stdout
    assert BREAKS[rule][1] in result.stderr, result.stderr


@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_the_component_closure_check_refuses_each(rule: str, tmp_path: Path) -> None:
    from swcraft_tools.component_exchange.export import ClosureValidationError, validate_component_closure

    model = _basic(tmp_path) / "model"
    validate_component_closure(str(model))
    BREAKS[rule][0](model)

    with pytest.raises(ClosureValidationError) as refused:
        validate_component_closure(str(model))

    assert BREAKS[rule][1] in refused.value.issues


@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_the_writer_refuses_each_with_its_message_and_writes_nothing(rule: str, tmp_path: Path) -> None:
    from swcraft_tools.writer.arxml_generation import generate_arxml

    model = _basic(tmp_path) / "model"
    BREAKS[rule][0](model)
    out = tmp_path / "out" / "model.arxml"

    with pytest.raises(ValueError) as refused:
        generate_arxml(str(model), str(out))

    assert f"rule:{rule}: {BREAKS[rule][1]}" in str(refused.value)
    assert not out.exists()
    assert not out.parent.exists()
    assert str(tmp_path) not in str(refused.value)


def test_a_rule_clean_element_is_written(tmp_path: Path) -> None:
    """The rules come first; a model that breaks none is written, its element in its layer's package."""
    from swcraft_tools.writer.arxml_generation import generate_arxml

    model = _basic(tmp_path) / "model"
    _unit(model, "Kmh", display_name="km/h")
    out = tmp_path / "out" / "model.arxml"

    generate_arxml(str(model), str(out))

    assert "<SHORT-NAME>Units</SHORT-NAME>" in out.read_text(encoding="utf-8")
    assert "<SHORT-NAME>Kmh</SHORT-NAME>" in out.read_text(encoding="utf-8")


# The shapes of each rule


class TestTheUnknownTargets:
    @pytest.mark.parametrize(
        ("payload", "word"),
        [
            ({"name": "ProbeSpeed", "kind": "application_type", "compu_method_ref": "ProbeAbsent"}, "Application type"),
            ({"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeAbsent"}, "Enum"),
            (
                {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeAbsent"},
                "Value type",
            ),
            (
                {"name": "ProbeAlias", "kind": "reference_type", "base": "uint8", "compu_method_ref": "ProbeAbsent"},
                "Reference type",
            ),
        ],
        ids=["application_type", "enum", "value_type", "reference_type"],
    )
    def test_each_compu_method_ref_naming_nothing(self, payload: dict[str, Any], word: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, payload)

        assert _messages(model, "data-type.unknown-compu-method") == [
            f"{word} '{payload['name']}' compu_method_ref 'ProbeAbsent' is not declared in the compu_methods layer."
        ]

    def test_a_declared_target_of_each_layer_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _linear(model, "ProbeLinearScale", unit="Kmh")
        _texttable(model, "ProbeGearTable")
        _constraint(model, "ProbeLimits", min=0, max=10)
        _type(
            model,
            {
                "name": "ProbeSpeed",
                "kind": "application_type",
                "unit_ref": "Kmh",
                "compu_method_ref": "ProbeLinearScale",
                "data_constr_ref": "ProbeLimits",
            },
        )
        _type(model, {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeGearTable"})
        _type(
            model,
            {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeGearTable"},
        )
        _type(
            model,
            {"name": "ProbeAlias", "kind": "reference_type", "base": "ProbeRaw", "compu_method_ref": "ProbeGearTable"},
        )

        from swcraft_tools.validator import check_data_description_elements

        assert check_data_description_elements(str(model)).records == ()

    def test_the_referrers_of_one_missing_element_are_one_violation(self, tmp_path: Path) -> None:
        """The finding carries its target, so pass D merges every referrer of one missing unit."""
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "unit_ref": "Kmh"})
        _type(model, {"name": "ProbeTorque", "kind": "application_type", "unit_ref": "Kmh"})

        records = _records(model, "data-type.unknown-unit")

        assert [record.target for record in records] == ["Kmh", "Kmh"]


class TestTheWrongCategory:
    @pytest.mark.parametrize(
        ("payload", "tail"),
        [
            (
                {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeLinearScale"},
                "an enum's values are the entries of a texttable compu method.",
            ),
            (
                {
                    "name": "ProbeRaw",
                    "kind": "value_type",
                    "base": "uint8_base",
                    "compu_method_ref": "ProbeLinearScale",
                },
                "an implementation type's compu method is restricted to the category BITFIELD_TEXTTABLE or "
                "TEXTTABLE ([constr_1158]), so it names a texttable.",
            ),
            (
                {
                    "name": "ProbeAlias",
                    "kind": "reference_type",
                    "base": "uint8",
                    "compu_method_ref": "ProbeLinearScale",
                },
                "an implementation type's compu method is restricted to the category BITFIELD_TEXTTABLE or "
                "TEXTTABLE ([constr_1158]), so it names a texttable.",
            ),
        ],
        ids=["enum", "value_type", "reference_type"],
    )
    def test_a_linear_named_where_a_texttable_is_the_role(
        self, payload: dict[str, Any], tail: str, tmp_path: Path
    ) -> None:
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _linear(model, "ProbeLinearScale", unit="Kmh")
        _type(model, payload)

        (message,) = _messages(model, "data-type.compu-method-wrong-category")

        assert f"'{payload['name']}' compu_method_ref 'ProbeLinearScale' names a linear compu method; {tail}" in message

    def test_the_subjects_are_the_reference_and_the_compu_method(self, tmp_path: Path) -> None:
        from swcraft_tools.diagnostics import Subject, field_step

        model = _basic(tmp_path) / "model"
        _break_wrong_category(model)

        (record,) = _records(model, "data-type.compu-method-wrong-category")

        assert record.subjects == (
            Subject("application_types", "ProbeSpeed", (field_step("compu_method_ref"),)),
            Subject("compu_methods", "ProbeGearTable"),
        )


class TestTheReferenceTypeChain:
    def _table(self, model: Path) -> None:
        _texttable(model, "ProbeGearTable")

    def test_a_chain_ending_in_a_value_type_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._table(model)
        _type(
            model,
            {"name": "ProbeAlias", "kind": "reference_type", "base": "uint8", "compu_method_ref": "ProbeGearTable"},
        )
        _type(
            model,
            {
                "name": "ProbeAliasAlias",
                "kind": "reference_type",
                "base": "ProbeAlias",
                "compu_method_ref": "ProbeGearTable",
            },
        )

        assert _records(model, "data-type.compu-method-on-unresolved-reference-type") == []

    def test_a_chain_ending_at_a_stub_is_not_judged(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._table(model)
        _type(
            model,
            {
                "name": "ProbeStub",
                "kind": "external_type",
                "external_ref": "/Other/ProbeStub",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        _type(
            model,
            {"name": "ProbeAlias", "kind": "reference_type", "base": "ProbeStub", "compu_method_ref": "ProbeGearTable"},
        )

        assert _records(model, "data-type.compu-method-on-unresolved-reference-type") == []

    def test_a_broken_chain_is_the_reference_rules(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._table(model)
        _type(
            model,
            {
                "name": "ProbeAlias",
                "kind": "reference_type",
                "base": "ProbeAbsent",
                "compu_method_ref": "ProbeGearTable",
            },
        )

        assert _records(model, "data-type.compu-method-on-unresolved-reference-type") == []
        assert _records(model, "data-type.reference-type-undeclared-base") != []

    def test_a_chain_ending_in_a_pointer(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._table(model)
        _type(model, {"name": "ProbePointer", "kind": "pointer", "target": "uint8"})
        _type(
            model,
            {
                "name": "ProbeAlias",
                "kind": "reference_type",
                "base": "ProbePointer",
                "compu_method_ref": "ProbeGearTable",
            },
        )

        (message,) = _messages(model, "data-type.compu-method-on-unresolved-reference-type")

        assert "'ProbeAlias' -> 'ProbePointer' ends in a pointer and not in a value type" in message


class TestTheTableEntries:
    def test_a_repeated_value(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 0))

        assert _messages(model, "compu-method.table-entry-repeated") == [
            "Texttable compu method 'ProbeGearTable' has duplicate entry value(s) [0]; entry values must be unique "
            "within a texttable."
        ]

    def test_a_repeated_name_and_value_are_two_findings(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("PARK", 0))

        assert len(_records(model, "compu-method.table-entry-repeated")) == 2

    def test_distinct_entries_are_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1), ("REVERSE", 2))

        assert _records(model, "compu-method.table-entry-repeated") == []


class TestTheDuplicateNames:
    def test_a_texttable_and_a_linear_of_one_name_are_a_duplicate(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _break_compu_method_duplicate(model)

        assert len(_records(model, "compu-method.duplicate-name")) == 1

    def test_one_name_in_two_layers_is_no_duplicate(self, tmp_path: Path) -> None:
        """A unit, a compu method, a data constraint and a type of one name are four elements of four layers."""
        model = _basic(tmp_path) / "model"
        _unit(model, "ProbeShared")
        _texttable(model, "ProbeShared")
        _constraint(model, "ProbeShared")
        _type(model, {"name": "ProbeShared", "kind": "application_type"})

        for rule in ("unit.duplicate-name", "compu-method.duplicate-name", "data-constr.duplicate-name"):
            assert _records(model, rule) == []
        assert _records(model, "name.name-space-collision") == []


# The widened conversion rule


class TestAConversionNeedsAUnitOrAUnitRef:
    RULE = "data-type.conversion-without-unit"

    def test_a_unit_ref_is_a_unit(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "unit_ref": "Kmh", "conversion": {"factor": 2}})

        assert _records(model, self.RULE) == []

    def test_neither_is_still_the_finding(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "conversion": {"factor": 2}})

        assert len(_records(model, self.RULE)) == 1

    def test_a_unit_ref_naming_nothing_is_the_unknown_unit_rule_alone(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "unit_ref": "Kmh", "conversion": {"factor": 2}})

        assert _records(model, self.RULE) == []
        assert len(_records(model, "data-type.unknown-unit")) == 1

    def test_a_referenced_linear_is_not_an_inline_conversion(self, tmp_path: Path) -> None:
        """A linear compu method names its own unit, which its schema requires."""
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _linear(model, "ProbeLinearScale", unit="Kmh")
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "compu_method_ref": "ProbeLinearScale"})

        assert _records(model, self.RULE) == []


# The XML text of every declared element, referenced or not

#: One text field per case, on an element no type references: (layer writer, field path, owner words, field).
XML_TEXT_CASES: dict[str, tuple[Callable[[Path, str], None], str, str]] = {
    "unit.display_name": (lambda m, t: _unit(m, "Kmh", display_name=t), "Unit 'Kmh'", "display_name"),
    "unit.description": (lambda m, t: _unit(m, "Kmh", description=t), "Unit 'Kmh'", "description"),
    "texttable.description": (
        lambda m, t: _texttable(m, "ProbeGearTable", description=t),
        "Compu method 'ProbeGearTable'",
        "description",
    ),
    "table_entry.description": (
        lambda m, t: write_model_element(
            m,
            {
                "name": "ProbeGearTable",
                "kind": "texttable",
                "entries": [{"name": "PARK", "value": 0, "description": t}],
            },
        ),
        "Compu method 'ProbeGearTable' entry 'PARK'",
        "description",
    ),
    "linear.description": (
        lambda m, t: (_unit(m, "Kmh"), _linear(m, "ProbeLinearScale", unit="Kmh", description=t)),
        "Compu method 'ProbeLinearScale'",
        "description",
    ),
    "data_constr.description": (
        lambda m, t: _constraint(m, "ProbeLimits", description=t),
        "Data constraint 'ProbeLimits'",
        "description",
    ),
}


class TestTheXmlTextOfEveryDeclaredElement:
    RULE = "text.xml-unrepresentable-character"

    @pytest.mark.parametrize("case", sorted(XML_TEXT_CASES))
    def test_check_model_judges_it_on_an_unreferenced_element(self, case: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        write, owner, field = XML_TEXT_CASES[case]
        write(model, "bad\x01text")

        (message,) = _messages(model, self.RULE)

        assert message.startswith(f"{owner} `{field}`"), message

    @pytest.mark.parametrize("case", sorted(XML_TEXT_CASES))
    def test_the_writer_refuses_it_in_the_same_words_and_writes_nothing(self, case: str, tmp_path: Path) -> None:
        from swcraft_tools.writer.arxml_generation import generate_arxml

        model = _basic(tmp_path) / "model"
        write, _, _ = XML_TEXT_CASES[case]
        write(model, "bad\x01text")
        (message,) = _messages(model, self.RULE)
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as refused:
            generate_arxml(str(model), str(out))

        assert str(refused.value) == f"rule:{self.RULE}: {message}"
        assert not out.exists()

    def test_a_table_entrys_name_is_judged_as_an_enum_literals_is_by_the_identifier_rule(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("BAD\x01NAME", 0))

        assert _records(model, self.RULE) == []
        assert any("entry" in message for message in _messages(model, "identifier.declared"))


# Each element claims its name in its layer's package


class TestTheElementsClaimTheirNames:
    RULE = "name.name-space-collision"

    def test_a_case_only_pair_in_one_layer_package(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _put(model, "data_types/units/KMH_other.yaml", {"name": "KMH"})

        (message,) = _messages(model, self.RULE)

        assert "/DataTypes/Units" in message

    def test_a_sub_package_named_like_an_element(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _constraint(model, "ProbeLimits")
        _constraint(model, "ProbeOther", package="ProbeLimits")

        assert len(_records(model, self.RULE)) == 1

    def test_the_writers_guard_asks_the_same_claims(self, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description
        from swcraft_tools.reader import (
            read_compositions,
            read_connectors,
            read_interfaces,
            read_mapping_sets,
            read_swc_model,
        )
        from swcraft_tools.writer.arxml_generation import _check_name_space_collisions

        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _put(model, "data_types/units/KMH_other.yaml", {"name": "KMH"})
        description = read_data_description(str(model))

        with pytest.raises(ValueError, match="rule:name.name-space-collision") as refused:
            _check_name_space_collisions(
                read_swc_model(str(model)),
                read_interfaces(str(model)),
                list(description.data_types),
                read_compositions(str(model)),
                read_connectors(str(model)),
                read_mapping_sets(str(model)),
                description.elements,
            )

        assert _messages(model, self.RULE)[0] in str(refused.value)


# Pass D


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "data description elements", "operations": operations},
        project=str(project),
        review=_approve,
    )


def _create(layer: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": value["name"]}, "value": value}


_TABLE = {"name": "ProbeGearTable", "kind": "texttable", "entries": [{"name": "PARK", "value": 0}]}

#: One change set per rule that introduces it into the example. The three
#: duplicate-name rules are introduced on disk instead (below): one change set
#: cannot create a second element of an identity its layer already declares.
CHANGE_SETS: dict[str, list[dict[str, Any]]] = {
    "data-type.unknown-unit": [
        _create("application_types", {"name": "ProbeSpeed", "kind": "application_type", "unit_ref": "Kmh"})
    ],
    "data-type.unknown-compu-method": [
        _create("application_types", {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeGearTable"})
    ],
    "data-type.unknown-data-constr": [
        _create(
            "application_types", {"name": "ProbeSpeed", "kind": "application_type", "data_constr_ref": "ProbeLimits"}
        )
    ],
    "compu-method.unknown-unit": [
        _create("compu_methods", {"name": "ProbeLinearScale", "kind": "linear", "unit": "Kmh"})
    ],
    "data-type.compu-method-wrong-category": [
        _create("compu_methods", _TABLE),
        _create(
            "application_types",
            {"name": "ProbeSpeed", "kind": "application_type", "compu_method_ref": "ProbeGearTable"},
        ),
    ],
    "data-type.compu-method-on-unresolved-reference-type": [
        _create("compu_methods", _TABLE),
        _create(
            "implementation_types",
            {"name": "ProbeRecord", "kind": "implementation_record", "elements": [{"name": "a", "type": "uint8"}]},
        ),
        _create(
            "implementation_types",
            {
                "name": "ProbeRecordRef",
                "kind": "reference_type",
                "base": "ProbeRecord",
                "compu_method_ref": "ProbeGearTable",
            },
        ),
    ],
    "compu-method.table-entry-repeated": [
        _create(
            "compu_methods",
            {
                "name": "ProbeGearTable",
                "kind": "texttable",
                "entries": [{"name": "PARK", "value": 0}, {"name": "PARK", "value": 1}],
            },
        )
    ],
}

DUPLICATES = ("unit.duplicate-name", "compu-method.duplicate-name", "data-constr.duplicate-name")


def test_the_change_sets_and_the_duplicates_cover_exactly_the_ten() -> None:
    assert set(CHANGE_SETS) | set(DUPLICATES) == set(RULES)
    assert not set(CHANGE_SETS) & set(DUPLICATES)


def _refused_with(response: dict[str, Any], expected: str) -> None:
    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    messages = [response["message"], *(entry["message"] for entry in response.get("additional", []))]
    assert any(message.startswith(expected) for message in messages), response


@pytest.mark.parametrize("rule", sorted(CHANGE_SETS))
def test_pass_d_refuses_a_change_set_that_introduces_each(rule: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    before = files_of(project / "model")

    response = _change_set(project, CHANGE_SETS[rule])

    _refused_with(response, BREAKS[rule][1])
    assert files_of(project / "model") == before


_DUPLICATED = {
    "unit.duplicate-name": ("units", "Kmh"),
    "compu-method.duplicate-name": ("compu_methods", "ProbeGearTable"),
    "data-constr.duplicate-name": ("data_constraints", "ProbeLimits"),
}


@pytest.mark.parametrize("rule", DUPLICATES)
def test_a_change_set_against_a_repeated_name_is_refused_before_pass_d(rule: str, tmp_path: Path) -> None:
    """A repeated name already on disk refuses every change set, ``consistency-violation``, until repaired by hand.

    The engine refuses it before binding any operation, so pass D never sees such a tree.
    """
    project = _basic(tmp_path)
    BREAKS[rule][0](project / "model")
    before = files_of(project / "model")

    response = _change_set(project, [_create("units", {"name": "ProbeOtherUnit"})])

    layer, name = _DUPLICATED[rule]
    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    assert response["addresses"] == [{"layer": layer, "element": name}]
    assert files_of(project / "model") == before


@pytest.mark.parametrize("rule", RULES)
def test_pass_d_answers_each_rules_finding_with_consistency_violation(rule: str, tmp_path: Path) -> None:
    from swcraft_tools.editing.diagnosis import diagnose
    from swcraft_tools.validator import check_cross_layer_consistency

    model = _basic(tmp_path) / "model"
    BREAKS[rule][0](model)

    violations = [
        violation
        for violation in diagnose(check_cross_layer_consistency(str(model)))
        if violation.message.startswith(BREAKS[rule][1])
    ]

    assert [violation.error for violation in violations] == ["consistency-violation"]


def test_pass_d_commits_a_rule_clean_change_set_of_the_elements(tmp_path: Path) -> None:
    project = _basic(tmp_path)

    response = _change_set(
        project,
        [
            _create("units", {"name": "Kmh", "display_name": "km/h"}),
            _create("compu_methods", {"name": "ProbeLinearScale", "kind": "linear", "unit": "Kmh", "factor": 0.1}),
            _create("data_constraints", {"name": "ProbeLimits", "min": 0, "max": 400}),
            _create(
                "application_types",
                {
                    "name": "ProbeSpeed",
                    "kind": "application_type",
                    "unit_ref": "Kmh",
                    "compu_method_ref": "ProbeLinearScale",
                    "data_constr_ref": "ProbeLimits",
                },
            ),
        ],
    )

    assert response["response"] != "error", response


def test_a_change_set_carries_a_linears_scale_limits(tmp_path: Path) -> None:
    """``limits`` through the editing protocol: committed as stated, each bound with its interval."""
    project = _basic(tmp_path)
    limits = {"lower": {"value": 0, "interval": "closed"}, "upper": {"value": 100, "interval": "open"}}

    response = _change_set(
        project,
        [
            _create("units", {"name": "Kmh", "display_name": "km/h"}),
            _create(
                "compu_methods",
                {
                    "name": "ProbeLinearScale",
                    "kind": "linear",
                    "unit": "Kmh",
                    "limits": {**limits, "lower": {"value": 0}},
                },
            ),
        ],
    )

    assert response["status"] == "committed", response
    written = yaml.safe_load(
        model_element_path(project / "model", "ProbeLinearScale", kind="linear").read_text(encoding="utf-8")
    )
    assert written["limits"] == limits


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        ({"lower": {"value": 10}, "upper": {"value": 5}}, "linear limits 'lower' must be <= 'upper'"),
        (
            {"lower": {"value": 5, "interval": "open"}, "upper": {"value": 5}},
            "linear limits 'lower' and 'upper' may be equal only when both are 'closed'",
        ),
    ],
    ids=["reversed", "equal-and-open"],
)
def test_a_change_set_stating_an_empty_scope_is_refused(tmp_path: Path, limits: dict[str, Any], reason: str) -> None:
    """``rule:compu_methods.linear.limits-coherent`` via the editing protocol: ``schema-violation``, nothing written."""
    project = _basic(tmp_path)
    before = files_of(project / "model")

    response = _change_set(
        project,
        [
            _create("units", {"name": "Kmh", "display_name": "km/h"}),
            _create("compu_methods", {"name": "ProbeLinearScale", "kind": "linear", "unit": "Kmh", "limits": limits}),
        ],
    )

    assert (response["response"], response["error"]) == ("error", "schema-violation"), response
    assert response["addresses"] == [{"layer": "compu_methods", "element": "ProbeLinearScale"}]
    assert reason in response["message"]
    assert files_of(project / "model") == before


# What a type states, whichever form states it


def _view(*elements: Any) -> DataDescription:
    return DataDescription(elements)


_KMH = Unit(name="Kmh", display_name="km/h")
_KMH_BARE = Unit(name="Kmh")
_SCALE = Linear(name="ProbeLinearScale", kind="linear", unit="Kmh", factor=0.1, offset=-40)
_TABLE_ELEMENT = TextTable(
    name="ProbeGearTable",
    kind="texttable",
    entries=[TableEntry(name="PARK", value=0), TableEntry(name="DRIVE", value=1)],
)
_LIMITS = DataConstr(name="ProbeLimits", min=0, max=400, level=0)


def _app(name: str = "ProbeSpeed", **fields: Any) -> ApplicationDataType:
    return ApplicationDataType(name=name, kind="application_type", **fields)


class TestStatedUnitText:
    def test_the_inline_unit_stripped(self) -> None:
        from swcraft_tools.validator import stated_unit_text

        assert stated_unit_text(_app(unit=" km/h "), _view()) == "km/h"
        assert stated_unit_text(_app(unit="   "), _view()) == ""
        assert stated_unit_text(_app(), _view()) == ""

    def test_the_referenced_units_display_name(self) -> None:
        from swcraft_tools.validator import stated_unit_text

        assert stated_unit_text(_app(unit_ref="Kmh"), _view(_KMH)) == "km/h"

    def test_the_referenced_units_name_where_the_display_name_is_empty(self) -> None:
        from swcraft_tools.validator import stated_unit_text

        assert stated_unit_text(_app(unit_ref="Kmh"), _view(_KMH_BARE)) == "Kmh"
        assert stated_unit_text(_app(unit_ref="Kmh"), _view(Unit(name="Kmh", display_name="  "))) == "Kmh"

    def test_a_reference_naming_nothing_is_not_resolved(self) -> None:
        from swcraft_tools.validator import NOT_RESOLVED, stated_unit_text

        assert stated_unit_text(_app(unit_ref="Kmh"), _view()) is NOT_RESOLVED


class TestStatedConversion:
    def test_the_inline_conversion(self) -> None:
        from swcraft_tools.validator import stated_conversion

        conversion = Conversion(factor=2, offset=1)
        assert stated_conversion(_app(conversion=conversion), _view()) == conversion
        assert stated_conversion(_app(), _view()) is None

    def test_the_referenced_linears_coefficients(self) -> None:
        from swcraft_tools.validator import stated_conversion

        assert stated_conversion(_app(compu_method_ref="ProbeLinearScale"), _view(_SCALE)) == Conversion(
            factor=0.1, offset=-40
        )

    def test_the_referenced_linears_limits(self) -> None:
        """The referenced ``linear``'s ``limits`` arrive in the inline field's shape."""
        from swcraft_tools.validator import stated_conversion
        from swcraft_tools.validator.compatibility import _conversions_compatible

        bounded = _SCALE.model_copy(
            update={
                "limits": LinearLimits(
                    lower=LinearLimitBound(value=0), upper=LinearLimitBound(value=4000, interval="open")
                )
            }
        )
        limits = ConversionLimits(
            lower=ConversionLimitBound(value=0), upper=ConversionLimitBound(value=4000, interval="open")
        )
        stated = stated_conversion(_app(compu_method_ref="ProbeLinearScale"), _view(bounded))
        assert stated == Conversion(factor=0.1, offset=-40, limits=limits)
        assert _conversions_compatible(stated, Conversion(factor=0.1, offset=-40, limits=limits)) is None
        assert _conversions_compatible(stated, Conversion(factor=0.1, offset=-40)) == (
            "application type conversion limits [0, 4000) vs none"
        )

    @pytest.mark.parametrize("elements", [(), (_TABLE_ELEMENT,)], ids=["absent", "texttable"])
    def test_no_linear_of_that_name_is_not_resolved(self, elements: tuple[Any, ...]) -> None:
        from swcraft_tools.validator import NOT_RESOLVED, stated_conversion

        app = _app(compu_method_ref="ProbeGearTable" if elements else "ProbeAbsent")
        assert stated_conversion(app, _view(*elements)) is NOT_RESOLVED


class TestStatedRange:
    def test_the_inline_range(self) -> None:
        from swcraft_tools.validator import stated_range

        bounds = ValueRange(min=0, max=10)
        assert stated_range(_app(range=bounds), _view()) == bounds
        assert stated_range(_app(), _view()) is None

    def test_the_referenced_constraints_limits(self) -> None:
        from swcraft_tools.validator import stated_range

        stated = stated_range(_app(data_constr_ref="ProbeLimits"), _view(_LIMITS))
        assert (stated.min, stated.max) == (0, 400)

    def test_a_reference_naming_nothing_is_not_resolved(self) -> None:
        from swcraft_tools.validator import NOT_RESOLVED, stated_range

        assert stated_range(_app(data_constr_ref="ProbeLimits"), _view()) is NOT_RESOLVED


class TestStatedLiterals:
    def test_the_inline_literals(self) -> None:
        from swcraft_tools.validator import stated_literals

        enum = EnumDataType(name="ProbeGear", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])
        assert stated_literals(enum, _view()) == (("PARK", 0),)

    def test_the_referenced_tables_entries(self) -> None:
        from swcraft_tools.validator import stated_literals

        enum = EnumDataType(name="ProbeGear", kind="enum", compu_method_ref="ProbeGearTable")
        assert stated_literals(enum, _view(_TABLE_ELEMENT)) == (("PARK", 0), ("DRIVE", 1))

    def test_a_linear_of_that_name_is_not_resolved(self) -> None:
        from swcraft_tools.validator import NOT_RESOLVED, stated_literals

        enum = EnumDataType(name="ProbeGear", kind="enum", compu_method_ref="ProbeLinearScale")
        assert stated_literals(enum, _view(_SCALE)) is NOT_RESOLVED


# Interface compatibility across the two forms


def _compatibility(required: Any, provided: Any, *elements: Any) -> Any:
    from swcraft_tools.validator import interface_compatibility

    interfaces = {
        "R": SRInterface(
            name="R", kind="sender_receiver", data_elements=[DataElement(name="a", application_type=required.name)]
        ),
        "P": SRInterface(
            name="P", kind="sender_receiver", data_elements=[DataElement(name="a", application_type=provided.name)]
        ),
    }
    return interface_compatibility(
        "R", "P", context="subset", interfaces=interfaces, data_types=_view(required, provided, *elements)
    )


_INLINE = _app(
    "ProbeInline", unit="km/h", conversion=Conversion(factor=0.1, offset=-40), range=ValueRange(min=0, max=400)
)
_REFERENCING = _app(
    "ProbeReferencing", unit_ref="Kmh", compu_method_ref="ProbeLinearScale", data_constr_ref="ProbeLimits"
)


class TestInterfaceCompatibilityAcrossTheTwoForms:
    def test_an_inline_type_and_one_referencing_equal_content_are_compatible(self) -> None:
        assert _compatibility(_INLINE, _REFERENCING, _KMH, _SCALE, _LIMITS) is None
        assert _compatibility(_REFERENCING, _INLINE, _KMH, _SCALE, _LIMITS) is None

    def test_a_unit_compares_by_its_display_name(self) -> None:
        answer = _compatibility(_INLINE, _REFERENCING, Unit(name="Kmh", display_name="m/s"), _SCALE, _LIMITS)

        assert answer is not None
        assert answer.subject.endswith("application type unit 'km/h' vs 'm/s'")

    def test_a_unit_with_no_display_name_compares_by_its_name(self) -> None:
        inline = _app("ProbeInline", unit="Kmh")
        referencing = _app("ProbeReferencing", unit_ref="Kmh")

        assert _compatibility(inline, referencing, _KMH_BARE) is None

    def test_a_different_factor(self) -> None:
        other = Linear(name="ProbeLinearScale", kind="linear", unit="Kmh", factor=0.01, offset=-40)
        answer = _compatibility(_INLINE, _REFERENCING, _KMH, other, _LIMITS)

        assert answer is not None
        assert answer.subject.endswith("application type conversion factor 0.1 vs 0.01")

    def test_a_different_limit(self) -> None:
        other = DataConstr(name="ProbeLimits", min=0, max=300)
        answer = _compatibility(_INLINE, _REFERENCING, _KMH, _SCALE, other)

        assert answer is not None
        assert answer.subject.endswith("application type range max 400 vs 300")

    def test_a_constraints_level_is_not_compared(self) -> None:
        other = DataConstr(name="ProbeLimits", min=0, max=400, level=3)

        assert _compatibility(_INLINE, _REFERENCING, _KMH, _SCALE, other) is None

    def test_a_reference_naming_nothing_is_an_abstention(self) -> None:
        assert _compatibility(_INLINE, _REFERENCING, _KMH, _SCALE) is None

    def test_an_inline_enum_and_one_naming_a_table_of_equal_entries_are_compatible(self) -> None:
        inline = EnumDataType(
            name="ProbeGearInline",
            kind="enum",
            literals=[EnumLiteral(name="PARK", value=0), EnumLiteral(name="DRIVE", value=1)],
        )
        referencing = EnumDataType(name="ProbeGearReferencing", kind="enum", compu_method_ref="ProbeGearTable")

        assert _compatibility(inline, referencing, _TABLE_ELEMENT) is None

    def test_an_enum_naming_a_table_of_other_entries_is_incompatible(self) -> None:
        inline = EnumDataType(name="ProbeGearInline", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])
        referencing = EnumDataType(name="ProbeGearReferencing", kind="enum", compu_method_ref="ProbeGearTable")

        answer = _compatibility(inline, referencing, _TABLE_ELEMENT)

        assert answer is not None
        assert answer.subject.endswith("enum literals 1 vs 2")


# The init rules across the two forms


def _init_records(model: Path) -> list[Any]:
    from swcraft_tools.validator import check_init_conformance

    return list(check_init_conformance(str(model)).records)


def _valued(model: Path, type_name: str, init: Any) -> None:
    """An interface no port uses, its one element typed by the application type *type_name* with *init*."""
    _put(
        model,
        "interfaces/IProbe.yaml",
        {
            "name": "IProbe",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "application_type": type_name, "init": init}],
        },
    )


class TestTheInitRulesAcrossTheTwoForms:
    def _gear(self, model: Path, *, inline: bool) -> None:
        if inline:
            _type(
                model,
                {
                    "name": "ProbeGear",
                    "kind": "enum",
                    "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
                },
            )
        else:
            _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1))
            _type(model, {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeGearTable"})

    @pytest.mark.parametrize("inline", [True, False], ids=["literals", "table"])
    def test_an_enum_init_naming_a_value_of_either_form_conforms(self, inline: bool, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._gear(model, inline=inline)
        _valued(model, "ProbeGear", "DRIVE")

        assert _init_records(model) == []

    @pytest.mark.parametrize(
        ("inline", "words", "step"),
        [
            (True, "it declares ['PARK', 'DRIVE']", "literals"),
            (False, "its compu method 'ProbeGearTable' declares ['PARK', 'DRIVE']", "compu_method_ref"),
        ],
        ids=["literals", "table"],
    )
    def test_an_enum_init_naming_no_value_is_refused_alike(
        self, inline: bool, words: str, step: str, tmp_path: Path
    ) -> None:
        from swcraft_tools.diagnostics import field_step

        model = _basic(tmp_path) / "model"
        self._gear(model, inline=inline)
        _valued(model, "ProbeGear", "REVERSE")

        (record,) = _init_records(model)

        assert record.rule == "interface.init-unknown-enum-literal"
        assert words in record.message
        assert any(subject.path == (field_step(step),) for subject in record.subjects)

    def test_an_enum_naming_no_table_judges_nothing(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeAbsent"})
        _valued(model, "ProbeGear", "REVERSE")

        assert _init_records(model) == []

    @staticmethod
    def _speed(model: Path, *, inline: bool) -> None:
        if inline:
            _type(model, {"name": "ProbeSpeed", "kind": "application_type", "range": {"min": 0, "max": 400}})
        else:
            _constraint(model, "ProbeLimits", min=0, max=400)
            _type(model, {"name": "ProbeSpeed", "kind": "application_type", "data_constr_ref": "ProbeLimits"})

    @pytest.mark.parametrize(
        ("init", "words"),
        [
            (500, "above type 'ProbeSpeed's declared {where} max 400."),
            (-5, "below type 'ProbeSpeed's declared {where} min 0."),
        ],
        ids=["above-max", "below-min"],
    )
    @pytest.mark.parametrize("inline", [True, False], ids=["range", "data_constr"])
    def test_a_bound_of_either_form_refuses_alike(self, inline: bool, init: int, words: str, tmp_path: Path) -> None:
        """Both limits, each form; the subject names the repair place (the inline limit or ``data_constr_ref``)."""
        from swcraft_tools.diagnostics import field_step

        model = _basic(tmp_path) / "model"
        self._speed(model, inline=inline)
        _valued(model, "ProbeSpeed", init)

        (record,) = _init_records(model)

        assert record.rule == "interface.init-out-of-range"
        where = "range" if inline else "data constraint 'ProbeLimits'"
        assert words.format(where=where) in record.message
        side = "max" if init > 0 else "min"
        repair = (field_step("range"), field_step(side)) if inline else (field_step("data_constr_ref"),)
        assert [subject.path for subject in record.subjects if subject.layer == "application_types"] == [repair]

    @pytest.mark.parametrize("inline", [True, False], ids=["range", "data_constr"])
    def test_a_shape_mismatch_is_refused_alike(self, inline: bool, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        self._speed(model, inline=inline)
        _valued(model, "ProbeSpeed", [1, 2])

        (record,) = _init_records(model)

        assert record.rule == "interface.init-shape-mismatch"
        assert record.message == (
            "Interface 'IProbe' element 'v' init value at 'init' is a list, but type 'ProbeSpeed' is a "
            "application_type, which takes a scalar."
        )

    def test_within_a_referenced_constraint_conforms(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _constraint(model, "ProbeLimits", min=0, max=400)
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "data_constr_ref": "ProbeLimits"})
        _valued(model, "ProbeSpeed", 300)

        assert _init_records(model) == []

    def test_a_constraint_naming_nothing_bounds_nothing(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "data_constr_ref": "ProbeLimits"})
        _valued(model, "ProbeSpeed", 500)

        assert _init_records(model) == []

    def test_a_referenced_linear_bounds_nothing(self, tmp_path: Path) -> None:
        """A linear compu method holds no limits, as an inline conversion does not."""
        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _linear(model, "ProbeLinearScale", unit="Kmh")
        _type(model, {"name": "ProbeSpeed", "kind": "application_type", "compu_method_ref": "ProbeLinearScale"})
        _valued(model, "ProbeSpeed", 1e9)

        assert _init_records(model) == []

    @staticmethod
    def _table_typed(model: Path, init: Any, *, through_a_reference: bool) -> None:
        """A value type that references a table, and the element typed by it or by a reference type over it."""
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1))
        _type(
            model,
            {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeGearTable"},
        )
        typed = "ProbeRaw"
        if through_a_reference:
            _type(model, {"name": "ProbeAlias", "kind": "reference_type", "base": "ProbeRaw"})
            typed = "ProbeAlias"
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": typed, "init": init}],
            },
        )

    @pytest.mark.parametrize("through_a_reference", [False, True], ids=["value_type", "reference_type"])
    def test_a_table_typed_implementation_types_init_is_an_entrys_name(
        self, through_a_reference: bool, tmp_path: Path
    ) -> None:
        """A table-typed implementation type's init is the name of one of its entries.

        The application value form (``[constr_1225]``): a value or reference type that states a table, on the type or
        along its chain, takes the name of one of its entries.
        """
        model = _basic(tmp_path) / "model"
        self._table_typed(model, "DRIVE", through_a_reference=through_a_reference)

        assert _init_records(model) == []

    @pytest.mark.parametrize("init", [1, "REVERSE", 0], ids=["a number", "no entry", "an entry's value"])
    @pytest.mark.parametrize("through_a_reference", [False, True], ids=["value_type", "reference_type"])
    def test_a_number_or_a_name_of_no_entry_is_refused(
        self, through_a_reference: bool, init: Any, tmp_path: Path
    ) -> None:
        """A number is refused there, even one equal to an entry's value: SWCraft reads no number as a literal."""
        from swcraft_tools.diagnostics import Subject, field_step

        model = _basic(tmp_path) / "model"
        self._table_typed(model, init, through_a_reference=through_a_reference)

        (record,) = _init_records(model)

        assert record.rule == "interface.init-unknown-enum-literal"
        assert "names no entry of the table 'ProbeGearTable' that type 'ProbeRaw' references" in record.message
        # The declaration is an implementation type: the address names its own layer.
        assert record.subjects[1] == Subject("implementation_types", "ProbeRaw", (field_step("compu_method_ref"),))

    @pytest.mark.parametrize("through_a_reference", [False, True], ids=["value_type", "reference_type"])
    def test_a_ports_number_names_the_implementation_types_layer(
        self, through_a_reference: bool, tmp_path: Path
    ) -> None:
        """At a port's ``com_specs`` ``init`` the second subject is the implementation type, as at the element."""
        from swcraft_tools.diagnostics import Subject, field_step

        model = _basic(tmp_path) / "model"
        self._table_typed(model, "DRIVE", through_a_reference=through_a_reference)
        consumer = model / "swc" / "example" / "ExampleConsumerSWC.yaml"
        document = yaml.safe_load(consumer.read_text(encoding="utf-8"))
        document["ports"].append(
            {
                "name": "rProbe",
                "direction": "require",
                "interface": "IProbe",
                "com_specs": [{"data_element": "v", "init": 1}],
            }
        )
        _put(model, "swc/example/ExampleConsumerSWC.yaml", document)

        (record,) = _init_records(model)

        assert record.rule == "interface.init-unknown-enum-literal"
        assert record.message.startswith("SWC 'ExampleConsumerSWC' port 'rProbe' com_specs init value")
        assert record.subjects[1] == Subject("implementation_types", "ProbeRaw", (field_step("compu_method_ref"),))

    def test_a_reference_types_own_table_decides(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1))
        _texttable(model, "ProbeOtherTable", ("LOW", 0), ("HIGH", 1))
        _type(
            model,
            {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeGearTable"},
        )
        _type(
            model,
            {"name": "ProbeAlias", "kind": "reference_type", "base": "ProbeRaw", "compu_method_ref": "ProbeOtherTable"},
        )
        for init, findings in (("HIGH", 0), ("DRIVE", 1)):
            _put(
                model,
                "interfaces/IProbe.yaml",
                {
                    "name": "IProbe",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "v", "implementation_type": "ProbeAlias", "init": init}],
                },
            )
            assert len(_init_records(model)) == findings, init

    def test_a_text_under_a_value_type_with_no_table_is_still_the_string_rule(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _type(model, {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base"})
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbeRaw", "init": "DRIVE"}],
            },
        )

        (record,) = _init_records(model)

        assert record.rule == "interface.init-string-scalar"
        assert "that references no table, so its values are numbers" in record.message

    def test_a_table_the_type_names_but_the_model_does_not_hold_judges_as_no_table(self, tmp_path: Path) -> None:
        """A reference naming nothing is ``data-type.unknown-compu-method``'s; a number is no second finding."""
        model = _basic(tmp_path) / "model"
        _type(
            model, {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeAbsent"}
        )
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbeRaw", "init": 1}],
            },
        )

        assert _init_records(model) == []

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    @pytest.mark.parametrize("through_a_reference", [False, True], ids=["value_type", "reference_type"])
    def test_the_writer_writes_the_entrys_name_as_a_text_value(
        self, through_a_reference: bool, version: str, tmp_path: Path
    ) -> None:
        """The entry's name is written as a ``TEXT-VALUE-SPECIFICATION``'s ``VALUE``."""
        from lxml import etree
        from swcraft_tools.writer import generate_arxml

        project = _basic(tmp_path)
        model = project / "model"
        self._table_typed(model, "DRIVE", through_a_reference=through_a_reference)
        output = tmp_path / "out.arxml"
        generate_arxml(str(model), str(output), version=version)
        ns = {"a": "http://autosar.org/schema/r4.0"}
        root = etree.parse(str(output)).getroot()
        (constant,) = [
            c
            for c in root.iterfind(".//a:CONSTANT-SPECIFICATION", ns)
            if c.findtext("a:SHORT-NAME", namespaces=ns) == "IProbe_v_Init"
        ]
        (spec,) = constant.find("a:VALUE-SPEC", ns)
        assert etree.QName(spec).localname == "TEXT-VALUE-SPECIFICATION"
        assert [etree.QName(child).localname for child in spec] == ["VALUE"]
        assert spec.findtext("a:VALUE", namespaces=ns) == "DRIVE"

    @pytest.mark.parametrize("init", [1, "REVERSE"], ids=["a number", "no entry"])
    def test_the_writer_refuses_what_check_model_refuses(self, init: Any, tmp_path: Path) -> None:
        from swcraft_tools.writer import generate_arxml

        project = _basic(tmp_path)
        model = project / "model"
        self._table_typed(model, init, through_a_reference=True)
        output = tmp_path / "out.arxml"
        with pytest.raises(
            ValueError, match=r"names no entry of the table 'ProbeGearTable' that 'ProbeRaw' references"
        ):
            generate_arxml(str(model), str(output), version="4.3.0")
        assert not output.exists()

    @staticmethod
    def _wide() -> str:
        """A hexadecimal integer above this interpreter's integer-to-string limit, or a skip where it has none.

        ``0x…`` loads through ``int(v, 16)``, which the limit exempts.
        """
        if not sys.get_int_max_str_digits():
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")
        return "0x" + "f" * sys.get_int_max_str_digits()

    @pytest.mark.parametrize(
        ("init", "rule"),
        [
            ("[1, 2]", "interface.init-shape-mismatch"),
            ("{a: 1}", "interface.init-shape-mismatch"),
            ("true", "interface.init-unknown-enum-literal"),
            ("ON", "interface.init-unknown-enum-literal"),
            (None, "interface.init-unwritable-integer"),
        ],
        ids=["a list", "a map", "a bool", "an unquoted ON", "an unwritable integer"],
    )
    def test_check_model_and_the_writer_refuse_alike_under_a_table(
        self, init: str | None, rule: str, tmp_path: Path
    ) -> None:
        """Every refusal of a value under a table-typed type, in both surfaces, by the same rule.

        A composite is the shape rule's, a ``bool`` (an unquoted ``ON``) names no entry, and an integer too
        wide to write is the unwritable rule's, decided first."""
        from swcraft_tools.writer import generate_arxml

        project = _basic(tmp_path)
        model = project / "model"
        self._table_typed(model, "DRIVE", through_a_reference=True)
        interface = model / "interfaces" / "IProbe.yaml"
        text = interface.read_text(encoding="utf-8")
        assert "init: DRIVE" in text
        interface.write_text(
            text.replace("init: DRIVE", f"init: {init if init is not None else self._wide()}"),
            encoding="utf-8",
            newline="\n",
        )

        (record,) = _init_records(model)
        assert record.rule == rule

        output = tmp_path / "out.arxml"
        with pytest.raises(ValueError, match=rf"\({rule}\)"):
            generate_arxml(str(model), str(output), version="4.3.0")
        assert not output.exists()

    def test_a_members_number_under_a_table_is_refused_at_the_member(self, tmp_path: Path) -> None:
        """A member's number under a table is refused at the member's path, by check-model and the writer."""
        from swcraft_tools.diagnostics import Subject, field_step
        from swcraft_tools.writer import generate_arxml

        project = _basic(tmp_path)
        model = project / "model"
        self._table_typed(model, "DRIVE", through_a_reference=True)
        _type(model, {"name": "ProbeCount", "kind": "value_type", "base": "uint8_base"})
        _type(
            model,
            {
                "name": "ProbePair",
                "kind": "implementation_record",
                "elements": [{"name": "mode", "type": "ProbeAlias"}, {"name": "count", "type": "ProbeCount"}],
            },
        )
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbePair", "init": {"mode": 1, "count": 3}}],
            },
        )

        (record,) = _init_records(model)
        assert record.rule == "interface.init-unknown-enum-literal"
        assert record.message.startswith("Interface 'IProbe' element 'v' init value at 'init.mode' ")
        assert record.subjects[1] == Subject("implementation_types", "ProbeRaw", (field_step("compu_method_ref"),))

        output = tmp_path / "out.arxml"
        with pytest.raises(ValueError, match=r"init\.mode names no entry of the table 'ProbeGearTable'"):
            generate_arxml(str(model), str(output), version="4.3.0")
        assert not output.exists()

    def test_a_table_the_type_names_as_a_linear_judges_as_no_table(self, tmp_path: Path) -> None:
        """A ``compu_method_ref`` naming a ``linear`` is the wrong-category rule's; a number is no second finding."""
        from swcraft_tools.data_description import read_data_description
        from swcraft_tools.validator import implementation_table

        model = _basic(tmp_path) / "model"
        _unit(model, "Kmh")
        _linear(model, "ProbeLinearScale", unit="Kmh")
        _type(
            model,
            {"name": "ProbeRaw", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeLinearScale"},
        )
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbeRaw", "init": 1}],
            },
        )

        assert _init_records(model) == []
        assert [record.rule for record in _records(model, "data-type.compu-method-wrong-category")] == [
            "data-type.compu-method-wrong-category"
        ]
        description = read_data_description(str(model))
        assert implementation_table(description.implementation("ProbeRaw"), description) is None

    def test_a_reference_chain_that_closes_on_itself_states_no_table(self, tmp_path: Path) -> None:
        """``data-type.cycle`` owns the cycle; the init rules find nothing to judge against."""
        from swcraft_tools.data_description import read_data_description
        from swcraft_tools.validator import implementation_table

        model = _basic(tmp_path) / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1))
        _type(
            model,
            {
                "name": "ProbeLoopA",
                "kind": "reference_type",
                "base": "ProbeLoopB",
                "compu_method_ref": "ProbeGearTable",
            },
        )
        _type(model, {"name": "ProbeLoopB", "kind": "reference_type", "base": "ProbeLoopA"})
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbeLoopA", "init": 1}],
            },
        )

        assert _init_records(model) == []
        description = read_data_description(str(model))
        assert implementation_table(description.implementation("ProbeLoopA"), description) is None

    def test_a_chain_ending_at_an_implementation_record_states_no_table(self, tmp_path: Path) -> None:
        """An alias over an implementation record stating a ``compu_method_ref`` takes the record's shape.

        ``data-type.compu-method-on-unresolved-reference-type`` owns the misplaced reference, and the
        writer refuses the model by that rule alone."""
        from swcraft_tools.data_description import read_data_description
        from swcraft_tools.validator import implementation_table
        from swcraft_tools.writer import generate_arxml

        project = _basic(tmp_path)
        model = project / "model"
        _texttable(model, "ProbeGearTable", ("PARK", 0), ("DRIVE", 1))
        _type(model, {"name": "ProbeCount", "kind": "value_type", "base": "uint8_base"})
        _type(
            model,
            {"name": "ProbeRec", "kind": "implementation_record", "elements": [{"name": "n", "type": "ProbeCount"}]},
        )
        _type(
            model,
            {
                "name": "ProbeRecAlias",
                "kind": "reference_type",
                "base": "ProbeRec",
                "compu_method_ref": "ProbeGearTable",
            },
        )
        _put(
            model,
            "interfaces/IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "ProbeRecAlias", "init": {"n": 3}}],
            },
        )

        assert _init_records(model) == []
        description = read_data_description(str(model))
        assert implementation_table(description.implementation("ProbeRecAlias"), description) is None
        output = tmp_path / "out.arxml"
        with pytest.raises(ValueError, match=r"data-type\.compu-method-on-unresolved-reference-type") as raised:
            generate_arxml(str(model), str(output), version="4.3.0")
        assert "interface.init" not in str(raised.value)
