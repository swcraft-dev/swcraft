"""`check_data_type_references`' reference-role rules over `base_type`, `value_type` and `reference_type`.

The rule is keyed on **the reference's role and its owner's kind**. Half of it is the
loader's — a `base` on a `base_type` does not load (`extra="forbid"`,
`packages/tools/tests/test_schema.py`) — and the cross-file half is two rules about a
`base` that names **no declared type**, answered by pass D as `broken-reference` in the
`data-type` group every undeclared-type rule shares.

A value type's `base` resolves in `base_types` alone, a reference type's in
`implementation_types` alone, a record field's `type` and an array's `element_type` in
`application_types` alone. The ids of rules that judged a name of the wrong side
(:data:`RETIRED`, :data:`RETIRED_BY_THE_BASE_TYPES_LAYER`,
:data:`RETIRED_BY_THE_APPLICATION_TYPES_LAYER`) are asserted gone, and their models are
kept in :data:`RESTATED` under the undeclared-type rule each now reports. A type over a
type is **legal**.

Also here: `data-type.cycle` over a `reference_type` chain, `init` under a reference
chain (judged at the chain's value type), and the shapes still refused under the rule
that owns them now.

**What this module does not reach**: the writer's "not declared" refusal of a
base-typed prototype, a base type nothing references (legal, no rule to test), and the
loader half (`test_schema.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import validator_package
import yaml
from conftest import model_element_storage_name, write_model_element
from model_contract_fixtures import load_binding
from swcraft_tools.diagnostics import Subject, SubjectStep
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, RULE_CODES, diagnose
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    ConsistencyResult,
    check_cross_layer_consistency,
    check_data_type_cycles,
    check_init_conformance,
    data_types,
)
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

#: The two summaries, written out by hand: the expected value may not be read
#: from the document it checks.
SUMMARIES: dict[str, str] = {
    "data-type.value-type-undeclared-base": "A value type whose `base` names no declared base type.",
    "data-type.reference-type-undeclared-base": (
        "A reference type whose `base` names no declared type (an external data type stub counts as declared)."
    ),
}

#: The protocol code each is answered with: an undeclared target is a broken reference.
CODES: dict[str, str] = {
    "data-type.value-type-undeclared-base": BROKEN_REFERENCE,
    "data-type.reference-type-undeclared-base": BROKEN_REFERENCE,
}

#: The step of `check_data_type_references` that mints each, the function `binding.json` names.
BOUND_STEPS: dict[str, str] = {
    "data-type.value-type-undeclared-base": "_value_type_records",
    "data-type.reference-type-undeclared-base": "_reference_type_records",
}

RETIRED = ("data-type.impl-name-collision", "data-type.alias-over-alias", "data-type.alias-undeclared-base")

#: The ids retired with the base types' own layer: each model now names an undeclared type.
RETIRED_BY_THE_BASE_TYPES_LAYER = (
    "interface.data-element-base-type",
    "interface.argument-base-type",
    "data-type.record-field-base-type",
    "data-type.array-element-base-type",
    "data-type.value-type-base-not-base-type",
)

#: The ids retired with the application types' own layer: each judged a name of the
#: other side, which is now an undeclared name.
RETIRED_BY_THE_APPLICATION_TYPES_LAYER = (
    "data-type.composite-member-implementation-type",
    "data-type.reference-type-base-not-implementation-type",
    "data-type.implementation-element-not-implementation-type",
    "data-type.implementation-array-element-not-implementation-type",
    "data-type.pointer-target-not-implementation-type",
)


# ---------------------------------------------------------------------------
# Model builders over the vector harness's base model (examples/basic)
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real, valid project: `float32` a value type over `float32_base`, and so on."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _write(model_root: Path, relative: str, document: dict[str, Any]) -> None:
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _read(model_root: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _value_over_nothing(model_root: Path) -> None:
    write_model_element(model_root, {"name": "ProbeValue", "kind": "value_type", "base": "NoSuchBase"})


def _value_over_value(model_root: Path) -> None:
    write_model_element(model_root, {"name": "ProbeValue", "kind": "value_type", "base": "float32"})


def _reference_over_nothing(model_root: Path) -> None:
    write_model_element(model_root, {"name": "ProbeRef", "kind": "reference_type", "base": "NoSuchType"})


def _reference_over_base(model_root: Path) -> None:
    write_model_element(model_root, {"name": "ProbeRef", "kind": "reference_type", "base": "float32_base"})


def _reference_over_enum(model_root: Path) -> None:
    write_model_element(model_root, {"name": "ProbeEnum", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
    write_model_element(model_root, {"name": "ProbeRef", "kind": "reference_type", "base": "ProbeEnum"})


def _data_element_on_base(model_root: Path) -> None:
    document = _read(model_root, "interfaces/IExample.yaml")
    document["data_elements"][0]["implementation_type"] = "float32_base"
    _write(model_root, "interfaces/IExample.yaml", document)


def _argument_on_base(model_root: Path) -> None:
    document = _read(model_root, "interfaces/IDiagnosticService.yaml")
    (read_dtc,) = [operation for operation in document["operations"] if operation["name"] == "ReadDtc"]
    (dtc_id,) = [argument for argument in read_dtc["arguments"] if argument["name"] == "dtc_id"]
    dtc_id["implementation_type"] = "uint16_base"
    _write(model_root, "interfaces/IDiagnosticService.yaml", document)


def _record_field_on_base(model_root: Path) -> None:
    write_model_element(
        model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "float32_base"}]}
    )


def _array_element_on_base(model_root: Path) -> None:
    write_model_element(
        model_root, {"name": "ProbeArray", "kind": "array", "element_type": "float32_base", "length": 2}
    )


def _subject(layer: str, element: str, *steps: tuple[str, Any]) -> Subject:
    return Subject(layer, element, tuple(SubjectStep(kind, value) for kind, value in steps))


#: One row per rule: the break, the exact subjects its one record carries, the
#: offender the message must name, and the legal target kind it must name.
BREAKS: dict[str, tuple[Callable[[Path], None], tuple[Subject, ...], str, str]] = {
    "data-type.value-type-undeclared-base": (
        _value_over_nothing,
        (_subject("implementation_types", "ProbeValue", ("field", "base")),),
        "ProbeValue",
        "base type",
    ),
    "data-type.reference-type-undeclared-base": (
        _reference_over_nothing,
        (_subject("implementation_types", "ProbeRef", ("field", "base")),),
        "ProbeRef",
        "value_type, reference_type, implementation_record, implementation_array, pointer",
    ),
}

_ELEMENT_SUBJECT = _subject(
    "interfaces", "IExample", ("field", "data_elements"), ("member", "value"), ("field", "implementation_type")
)
_ARGUMENT_SUBJECT = _subject(
    "interfaces",
    "IDiagnosticService",
    ("field", "operations"),
    ("member", "ReadDtc"),
    ("field", "arguments"),
    ("member", "dtc_id"),
    ("field", "implementation_type"),
)

#: The retired rules' models, each restated as one record of its role's
#: undeclared-type rule on the same subject. Keyed by the model; the value is the
#: break, the rule it now reports, its subjects, the offender and the unresolved name.
RESTATED: dict[str, tuple[Callable[[Path], None], str, tuple[Subject, ...], str, str]] = {
    "a value type over a value type (was data-type.value-type-base-not-base-type)": (
        _value_over_value,
        "data-type.value-type-undeclared-base",
        (_subject("implementation_types", "ProbeValue", ("field", "base")),),
        "ProbeValue",
        "float32",
    ),
    "a reference type over a base type (was data-type.reference-type-base-not-implementation-type)": (
        _reference_over_base,
        "data-type.reference-type-undeclared-base",
        (_subject("implementation_types", "ProbeRef", ("field", "base")),),
        "ProbeRef",
        "float32_base",
    ),
    "a reference type over an enumeration (was data-type.reference-type-base-not-implementation-type)": (
        _reference_over_enum,
        "data-type.reference-type-undeclared-base",
        (_subject("implementation_types", "ProbeRef", ("field", "base")),),
        "ProbeRef",
        "ProbeEnum",
    ),
    "a data element typed by a base type (was interface.data-element-base-type)": (
        _data_element_on_base,
        "interface.data-element-undeclared-type",
        (_ELEMENT_SUBJECT,),
        "IExample",
        "float32_base",
    ),
    "an argument typed by a base type (was interface.argument-base-type)": (
        _argument_on_base,
        "interface.argument-undeclared-type",
        (_ARGUMENT_SUBJECT,),
        "IDiagnosticService",
        "uint16_base",
    ),
    "a record field typed by a base type (was data-type.record-field-base-type)": (
        _record_field_on_base,
        "data-type.record-field-undeclared-type",
        (_subject("application_types", "ProbeRecord", ("field", "fields"), ("member", "a"), ("field", "type")),),
        "ProbeRecord",
        "float32_base",
    ),
    "an array element typed by a base type (was data-type.array-element-base-type)": (
        _array_element_on_base,
        "data-type.array-undeclared-element-type",
        (_subject("application_types", "ProbeArray", ("field", "element_type")),),
        "ProbeArray",
        "float32_base",
    ),
}


# ---------------------------------------------------------------------------
# The two, registered once and emitted where the contract says
# ---------------------------------------------------------------------------


class TestTheTwoAreRegisteredEverywhereOnce:
    def test_the_break_table_is_exactly_the_two(self) -> None:
        assert set(BREAKS) == set(SUMMARIES) == set(CODES)

    @pytest.mark.parametrize("rule", sorted(SUMMARIES))
    def test_the_inventory_carries_the_contracts_summary_word_for_word(self, rule: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}

        assert CONSISTENCY_RULES[rule] == SUMMARIES[rule] == declared[f"rule:{rule}"]["summary"]
        assert declared[f"rule:{rule}"]["family"] == "cross-element"

    @pytest.mark.parametrize("rule", sorted(CODES))
    def test_the_protocol_answers_it_with_its_code(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CODES[rule]

    @pytest.mark.parametrize("rule", sorted(SUMMARIES))
    def test_the_binding_names_the_reference_check(self, rule: str) -> None:
        """Bound to the step of `check_data_type_references` that mints it, which the check reaches."""
        assert load_binding()["rules"][f"rule:{rule}"] == {
            "module": "swcraft_tools.validator.data_types",
            "callable": BOUND_STEPS[rule],
        }
        assert BOUND_STEPS[rule] in validator_package.reached_functions(data_types, "check_data_type_references")

    @pytest.mark.parametrize("rule", sorted(SUMMARIES))
    def test_the_emission_site_is_one_the_layer_scan_reads(self, rule: str) -> None:
        contract = load_contract()
        observed = emission_layers(contract)
        sites = observed[f"rule:{rule}"]

        assert sites and all(sites), sites
        assert [failure for failure in cross_element_layer_failures(contract, observed) if rule in failure] == []

    def test_the_retired_ids_are_gone_from_the_inventory_and_the_table(self) -> None:
        for rule in RETIRED:
            assert rule not in CONSISTENCY_RULES
            assert rule not in RULE_CODES
        assert not validator_package.defines("GENERATED_IMPL_SUFFIX")
        assert not validator_package.binding_modules("GENERATED_IMPL_SUFFIX")

    @pytest.mark.parametrize("rule", RETIRED_BY_THE_BASE_TYPES_LAYER)
    def test_the_five_ids_the_base_types_layer_retired_are_declared_nowhere(self, rule: str) -> None:
        declared = {entry["id"] for entry in load_contract()["rules"]}

        assert f"rule:{rule}" not in declared
        assert f"rule:{rule}" not in load_binding()["rules"]
        assert rule not in CONSISTENCY_RULES
        assert rule not in RULE_CODES

    @pytest.mark.parametrize("rule", RETIRED_BY_THE_APPLICATION_TYPES_LAYER)
    def test_the_five_ids_the_application_types_layer_retired_are_declared_nowhere(self, rule: str) -> None:
        declared = {entry["id"] for entry in load_contract()["rules"]}

        assert f"rule:{rule}" not in declared
        assert f"rule:{rule}" not in load_binding()["rules"]
        assert rule not in CONSISTENCY_RULES
        assert rule not in RULE_CODES

    @pytest.mark.parametrize("rule", sorted(BREAKS))
    def test_no_break_mints_a_retired_id(self, project: Path, rule: str) -> None:
        """Behaviour rather than source text: every model one of the two fires on mints no retired id."""
        break_it, _subjects, _offender, _legal = BREAKS[rule]
        model_root = project / "model"
        break_it(model_root)

        minted = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert minted.isdisjoint(RETIRED), minted

    @pytest.mark.parametrize("label", sorted(RESTATED))
    def test_no_restated_model_mints_a_retired_id(self, project: Path, label: str) -> None:
        break_it, _rule, _subjects, _offender, _name = RESTATED[label]
        model_root = project / "model"
        break_it(model_root)

        minted = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert minted.isdisjoint(RETIRED), minted
        assert minted.isdisjoint(RETIRED_BY_THE_BASE_TYPES_LAYER), minted
        assert minted.isdisjoint(RETIRED_BY_THE_APPLICATION_TYPES_LAYER), minted

    def test_every_undeclared_rule_shares_the_data_type_group(self) -> None:
        """The undeclared-base rules and the restated models' rules share the `data-type` group.

        One repair for every referrer of one missing name."""
        assert set(CODES.values()) == {BROKEN_REFERENCE}
        assert {RULE_CODES[rule].group for rule in CODES} == {"data-type"}
        assert {RULE_CODES[rule].group for _b, rule, _s, _o, _n in RESTATED.values()} == {"data-type"}
        assert "data-type.reference-type-base-not-implementation-type" not in RULE_CODES


# ---------------------------------------------------------------------------
# Each of the two, driven through the composed check
# ---------------------------------------------------------------------------


class TestEachRuleFiresAlone:
    """Each break isolates its rule: equality over the rule set, not membership."""

    @pytest.mark.parametrize("rule", sorted(BREAKS))
    def test_the_composed_check_reports_exactly_this_rule(self, project: Path, rule: str) -> None:
        break_it, subjects, offender, legal = BREAKS[rule]
        model_root = project / "model"
        break_it(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {rule}, sorted(r.rule for r in result.records)
        (record,) = result.records
        assert record.subjects == subjects
        assert offender in record.message and legal in record.message, record.message

    @pytest.mark.parametrize("rule", sorted(BREAKS))
    def test_the_translation_answers_with_the_code_the_table_assigns(self, project: Path, rule: str) -> None:
        break_it, _subjects, _offender, _legal = BREAKS[rule]
        model_root = project / "model"
        break_it(model_root)

        result = check_cross_layer_consistency(str(model_root))
        own = ConsistencyResult(consistent=False, records=tuple(r for r in result.records if r.rule == rule))

        assert {violation.error for violation in diagnose(own)} == {CODES[rule]}

    @pytest.mark.parametrize("label", sorted(RESTATED))
    def test_a_restated_model_reports_exactly_its_undeclared_type_rule(self, project: Path, label: str) -> None:
        """A retired rule's model reports its role's undeclared-type rule and nothing else."""
        break_it, rule, subjects, offender, name = RESTATED[label]
        model_root = project / "model"
        break_it(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {rule}, sorted(r.rule for r in result.records)
        (record,) = result.records
        assert record.subjects == subjects
        assert offender in record.message and repr(name) in record.message, record.message
        own = ConsistencyResult(consistent=False, records=result.records)
        assert {violation.error for violation in diagnose(own)} == {BROKEN_REFERENCE}

    def test_the_base_model_reports_none_of_them(self, project: Path) -> None:
        result = check_cross_layer_consistency(str(project / "model"))

        assert result.consistent, result.issues


# ---------------------------------------------------------------------------
# Pass D, through the engine, with no second wiring
# ---------------------------------------------------------------------------


def _dispatch(project_root: Path, *operations: dict[str, Any]) -> dict[str, Any]:
    return handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project_root / "model"),
            "intent": "a reference-role probe",
            "operations": list(operations),
        },
        project=project_root,
    )


def _address(layer: str, element: str, *path: dict[str, Any]) -> dict[str, Any]:
    address: dict[str, Any] = {"layer": layer, "element": element}
    if path:
        address["path"] = list(path)
    return address


def _create(layer: str, element: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": element}, "value": value}


def _update(address: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "update", "address": address, "value": value}


_ELEMENT = _address("interfaces", "IExample", {"field": "data_elements"}, {"member": "value"})
_ELEMENT_TYPE = _address(
    "interfaces", "IExample", {"field": "data_elements"}, {"member": "value"}, {"field": "implementation_type"}
)
_ARGUMENT = _address(
    "interfaces",
    "IDiagnosticService",
    {"field": "operations"},
    {"member": "ReadDtc"},
    {"field": "arguments"},
    {"member": "dtc_id"},
)
_ARGUMENT_TYPE = _address(
    "interfaces",
    "IDiagnosticService",
    {"field": "operations"},
    {"member": "ReadDtc"},
    {"field": "arguments"},
    {"member": "dtc_id"},
    {"field": "implementation_type"},
)

#: One staged change set per rule, and the addresses the refusal must carry.
CHANGE_SETS: dict[str, tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]] = {
    "data-type.value-type-undeclared-base": (
        (
            _create(
                "implementation_types", "ProbeValue", {"name": "ProbeValue", "kind": "value_type", "base": "NoSuchBase"}
            ),
        ),
        [_address("implementation_types", "ProbeValue", {"field": "base"})],
    ),
    "data-type.reference-type-undeclared-base": (
        (
            _create(
                "implementation_types", "ProbeRef", {"name": "ProbeRef", "kind": "reference_type", "base": "NoSuchType"}
            ),
        ),
        [_address("implementation_types", "ProbeRef", {"field": "base"})],
    ),
}

#: The retired rules' change sets (see :data:`RESTATED`), each refused now as a
#: broken reference at the same address.
RESTATED_CHANGE_SETS: dict[str, tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]] = {
    "a value type over a value type (was data-type.value-type-base-not-base-type)": (
        (
            _create(
                "implementation_types", "ProbeValue", {"name": "ProbeValue", "kind": "value_type", "base": "float32"}
            ),
        ),
        [_address("implementation_types", "ProbeValue", {"field": "base"})],
    ),
    "a reference type over a base type (was data-type.reference-type-base-not-implementation-type)": (
        (
            _create(
                "implementation_types",
                "ProbeRef",
                {"name": "ProbeRef", "kind": "reference_type", "base": "float32_base"},
            ),
        ),
        [_address("implementation_types", "ProbeRef", {"field": "base"})],
    ),
    "a reference type over an enumeration (was data-type.reference-type-base-not-implementation-type)": (
        (
            _create(
                "application_types",
                "ProbeEnum",
                {"name": "ProbeEnum", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
            ),
            _create(
                "implementation_types", "ProbeRef", {"name": "ProbeRef", "kind": "reference_type", "base": "ProbeEnum"}
            ),
        ),
        [_address("implementation_types", "ProbeRef", {"field": "base"})],
    ),
    "a data element typed by a base type (was interface.data-element-base-type)": (
        (_update(_ELEMENT_TYPE, "float32_base"),),
        [_ELEMENT_TYPE],
    ),
    "an argument typed by a base type (was interface.argument-base-type)": (
        (_update(_ARGUMENT_TYPE, "uint16_base"),),
        [_ARGUMENT_TYPE],
    ),
    "a record field typed by a base type (was data-type.record-field-base-type)": (
        (
            _create(
                "application_types",
                "ProbeRecord",
                {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "float32_base"}]},
            ),
        ),
        [_address("application_types", "ProbeRecord", {"field": "fields"}, {"member": "a"}, {"field": "type"})],
    ),
    "an array element typed by a base type (was data-type.array-element-base-type)": (
        (
            _create(
                "application_types",
                "ProbeArray",
                {"name": "ProbeArray", "kind": "array", "element_type": "float32_base", "length": 2},
            ),
        ),
        [_address("application_types", "ProbeArray", {"field": "element_type"})],
    ),
}


class TestPassDRefusesEachOneOnTheWire:
    """The staged change set is refused under the code `RULE_CODES` assigns.

    At the addresses the rule's subjects name — and the model byte-identical."""

    def test_the_change_set_table_is_exactly_the_two(self) -> None:
        assert set(CHANGE_SETS) == set(SUMMARIES)
        assert set(RESTATED_CHANGE_SETS) == set(RESTATED)

    @pytest.mark.parametrize("label", sorted(RESTATED_CHANGE_SETS))
    def test_a_restated_change_set_is_refused_as_a_broken_reference(self, project: Path, label: str) -> None:
        operations, addresses = RESTATED_CHANGE_SETS[label]
        before = {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")}

        response = _dispatch(project, *operations)

        assert response.get("error") == BROKEN_REFERENCE, response
        assert sorted(map(repr, response["addresses"])) == sorted(map(repr, addresses)), response
        assert {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")} == before

    @pytest.mark.parametrize("rule", sorted(CHANGE_SETS))
    def test_the_staged_change_set_is_refused_with_the_rules_code(self, project: Path, rule: str) -> None:
        operations, addresses = CHANGE_SETS[rule]
        before = {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")}

        response = _dispatch(project, *operations)

        assert response.get("error") == CODES[rule], response
        assert sorted(map(repr, response["addresses"])) == sorted(map(repr, addresses)), response
        assert {path: path.read_bytes() for path in (project / "model").rglob("*.yaml")} == before

    @pytest.mark.parametrize("rule", sorted(CHANGE_SETS))
    def test_with_the_reference_check_silenced_pass_d_stages_it(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, rule: str
    ) -> None:
        """No second wiring, measured: with ``check_data_type_references`` silenced, every change set stages."""
        silent = ConsistencyResult(consistent=True, records=())
        validator_package.patch_everywhere(monkeypatch, "check_data_type_references", lambda *_args, **_kwargs: silent)
        operations, _addresses = CHANGE_SETS[rule]

        response = _dispatch(project, *operations)

        assert "error" not in response and response.get("status") == "staged", response


# ---------------------------------------------------------------------------
# What the rules do NOT fire on: every prototype role, every legal target kind
# ---------------------------------------------------------------------------

#: The legal targets of a data prototype, one of each kind, declared beside the
#: base model's value type ``float32`` (over ``float32_base``). The composites are
#: built over the enumeration: a record field and an array element name the physical
#: view, resolved in ``application_types`` alone.
_TARGET_TYPES: dict[str, dict[str, Any]] = {
    "value_type": {"name": "float32", "kind": "value_type", "base": "float32_base"},
    "reference_type": {"name": "ProbeRef", "kind": "reference_type", "base": "float32"},
    "enum": {
        "name": "ProbeEnum",
        "kind": "enum",
        "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}],
    },
    "record": {"name": "ProbeTargetRecord", "kind": "record", "fields": [{"name": "x", "type": "ProbeEnum"}]},
    "array": {"name": "ProbeTargetArray", "kind": "array", "element_type": "ProbeEnum", "length": 2},
}

#: The target kinds a COMPOSITE member may name: the physical view and the two
#: composites, never an implementation type. A data
#: prototype — a data element, an argument — still names any of the five here.
_COMPOSITE_MEMBER_KINDS: tuple[str, ...] = ("enum", "record", "array")

#: The application-side kinds: a data prototype naming one states it in ``application_type``.
_APPLICATION_KINDS: frozenset[str] = frozenset({"enum", "record", "array"})


def _layer_of(kind: str) -> str:
    return "application_types" if kind in _APPLICATION_KINDS else "implementation_types"


def _type_field_of(target: str) -> str:
    (kind,) = [document["kind"] for document in _TARGET_TYPES.values() if document["name"] == target]
    return "application_type" if kind in _APPLICATION_KINDS else "implementation_type"


#: The four roles a data prototype plays, each as the change set that types it by *target*.
#: A data element and an argument are replaced whole, so the one type field set is the one
#: the target's side asks for.
_ROLES: dict[str, Callable[[str], tuple[dict[str, Any], ...]]] = {
    "data element": lambda target: (_update(_ELEMENT, {"name": "value", _type_field_of(target): target}),),
    "argument": lambda target: (
        _update(_ARGUMENT, {"name": "dtc_id", "direction": "in", _type_field_of(target): target}),
    ),
    "record field": lambda target: (
        _create(
            "application_types",
            "ProbeHolderRecord",
            {"name": "ProbeHolderRecord", "kind": "record", "fields": [{"name": "held", "type": target}]},
        ),
    ),
    "array element": lambda target: (
        _create(
            "application_types",
            "ProbeHolderArray",
            {"name": "ProbeHolderArray", "kind": "array", "element_type": target, "length": 2},
        ),
    ),
}


class TestNoneOfTheEightFiresOnALegalTarget:
    """Every prototype role crossed with every legal target kind: no finding, and pass D stages it.

    A rule that starts refusing a reference-typed argument, record field or array element
    fails here rather than only in an XSD test's incidental precondition. ``float32`` is the
    base model's own value type; the four other targets are created in the same change set.

    **The crossing is not square**: a data element and an argument name any of the five
    kinds, a composite's members the three of :data:`_COMPOSITE_MEMBER_KINDS`. The two cells
    that leave are the member's undeclared-name rule, which
    ``packages/tools/tests/test_validator_application_types.py`` asserts.
    """

    @staticmethod
    def _kinds(role: str) -> tuple[str, ...]:
        """The target kinds *role* may legally name."""
        return _COMPOSITE_MEMBER_KINDS if role in {"record field", "array element"} else tuple(sorted(_TARGET_TYPES))

    @staticmethod
    def _pair_the_enum(model_root: Path) -> None:
        """Give the enumeration a mapping set: `rule:data-type.application-type-unmapped`'s precondition."""
        write_model_element(
            model_root,
            {"name": "ProbeSets", "maps": [{"application": "ProbeEnum", "implementation": "float32"}]},
            layer="mapping_sets",
        )
        for relative in ("swc/example/ExampleSWC.yaml", "swc/example/ExampleConsumerSWC.yaml"):
            document = _read(model_root, relative)
            document["data_type_mappings"] = ["ProbeSets"]
            _write(model_root, relative, document)

    @staticmethod
    def _operations(role: str, kind: str) -> tuple[dict[str, Any], ...]:
        creates = tuple(
            _create(_layer_of(target_kind), document["name"], document)
            for target_kind, document in _TARGET_TYPES.items()
            if target_kind != "value_type"
        )
        return (*creates, *_ROLES[role](_TARGET_TYPES[kind]["name"]))

    @pytest.mark.parametrize("kind", sorted(_TARGET_TYPES))
    @pytest.mark.parametrize("role", sorted(_ROLES))
    def test_pass_d_stages_it_and_none_of_the_rules_is_minted(self, project: Path, role: str, kind: str) -> None:
        if kind not in self._kinds(role):
            pytest.skip(f"a {role} may not name a {kind} since contract 7.0.0; that cell is the new rule's")
        self._pair_the_enum(project / "model")
        response = _dispatch(project, *self._operations(role, kind))

        assert "error" not in response and response.get("status") == "staged", response

    @pytest.mark.parametrize("kind", _COMPOSITE_MEMBER_KINDS)
    @pytest.mark.parametrize("role", ["record field", "array element"])
    def test_the_composed_check_mints_none_of_the_rules_on_the_written_model(
        self, project: Path, role: str, kind: str
    ) -> None:
        model_root = project / "model"
        for target_kind, document in _TARGET_TYPES.items():
            if target_kind != "value_type":
                _write(model_root, model_element_storage_name(document["name"], kind=document["kind"]), document)
        self._pair_the_enum(model_root)
        (holder,) = _ROLES[role](_TARGET_TYPES[kind]["name"])
        _write(
            model_root,
            model_element_storage_name(holder["value"]["name"], kind=holder["value"]["kind"]),
            holder["value"],
        )

        minted = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert minted.isdisjoint(SUMMARIES), minted
        assert minted.isdisjoint(RETIRED_BY_THE_BASE_TYPES_LAYER), minted
        assert minted.isdisjoint(RETIRED_BY_THE_APPLICATION_TYPES_LAYER), minted


# ---------------------------------------------------------------------------
# data-type.cycle over a reference chain
# ---------------------------------------------------------------------------


class TestACycleOfReferenceTypesIsACycle:
    def test_a_reference_chain_that_closes_on_itself_is_reported(self, project: Path) -> None:
        model_root = project / "model"
        write_model_element(model_root, {"name": "ProbeRefA", "kind": "reference_type", "base": "ProbeRefB"})
        write_model_element(model_root, {"name": "ProbeRefB", "kind": "reference_type", "base": "ProbeRefA"})

        result = check_cross_layer_consistency(str(model_root))

        # The two bases resolve, each to a reference type, so no role rule fires:
        # what is wrong is the chain as a whole.
        assert {record.rule for record in result.records} == {"data-type.cycle"}
        (record,) = result.records
        assert record.subjects == (
            Subject("implementation_types", "ProbeRefA"),
            Subject("implementation_types", "ProbeRefB"),
        )

    def test_the_composite_graph_is_still_walked_beside_it(self, project: Path) -> None:
        """A composite's members resolve in `application_types` alone, so that layer closes the cycle on its own."""
        model_root = project / "model"
        write_model_element(model_root, {"name": "ProbeRefA", "kind": "reference_type", "base": "ProbeRefB"})
        write_model_element(model_root, {"name": "ProbeRefB", "kind": "reference_type", "base": "float32"})
        write_model_element(
            model_root, {"name": "ProbeRec", "kind": "record", "fields": [{"name": "back", "type": "ProbeArr"}]}
        )
        write_model_element(model_root, {"name": "ProbeArr", "kind": "array", "element_type": "ProbeRec", "length": 2})

        assert [record.rule for record in check_data_type_cycles(str(model_root)).records] == ["data-type.cycle"]

    def test_a_reference_type_and_a_record_naming_each_other_are_two_undeclared_names(self, project: Path) -> None:
        """A reference type and a record naming each other are two undeclared names, not a cycle.

        A reference type's `base` and a record field's `type` resolve in different layers, so neither edge resolves —
        each is its role's undeclared name."""
        model_root = project / "model"
        write_model_element(model_root, {"name": "ProbeRefA", "kind": "reference_type", "base": "ProbeRec"})
        write_model_element(
            model_root, {"name": "ProbeRec", "kind": "record", "fields": [{"name": "back", "type": "ProbeRefA"}]}
        )

        assert check_data_type_cycles(str(model_root)).records == ()
        assert {record.rule for record in check_cross_layer_consistency(str(model_root)).records} == {
            "data-type.reference-type-undeclared-base",
            "data-type.record-field-undeclared-type",
        }

    def test_a_chain_ending_in_a_value_type_is_no_cycle(self, project: Path) -> None:
        model_root = project / "model"
        write_model_element(model_root, {"name": "ProbeRefA", "kind": "reference_type", "base": "ProbeRefB"})
        write_model_element(model_root, {"name": "ProbeRefB", "kind": "reference_type", "base": "float32"})

        assert check_cross_layer_consistency(str(model_root)).consistent


# ---------------------------------------------------------------------------
# An init under a reference chain is judged at the chain's value type
# ---------------------------------------------------------------------------


def _chain(model_root: Path) -> None:
    """`ProbePercent` (value, 0..100) <- `ProbeRef1` <- `ProbeRef2`."""
    write_model_element(
        model_root,
        {"name": "ProbePercent", "kind": "value_type", "base": "float32_base", "value_min": 0.0, "value_max": 100.0},
    )
    write_model_element(model_root, {"name": "ProbeRef1", "kind": "reference_type", "base": "ProbePercent"})
    write_model_element(model_root, {"name": "ProbeRef2", "kind": "reference_type", "base": "ProbeRef1"})


def _interface(model_root: Path, type_name: str, init: Any) -> None:
    _write(
        model_root,
        "interfaces/IProbeInit.yaml",
        {
            "name": "IProbeInit",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": type_name, "init": init}],
        },
    )


_INIT = _subject("interfaces", "IProbeInit", ("field", "data_elements"), ("member", "v"), ("field", "init"))


class TestAnInitUnderAReferenceChainIsJudgedAtItsValueType:
    @pytest.mark.parametrize(
        ("type_name", "init", "rule", "steps"),
        [
            pytest.param("ProbeRef1", 50.0, None, (), id="one-step-in-range"),
            pytest.param(
                "ProbeRef1", 150.0, "interface.init-out-of-range", (("field", "value_max"),), id="one-step-above"
            ),
            pytest.param("ProbeRef2", 50.0, None, (), id="two-step-in-range"),
            pytest.param(
                "ProbeRef2", -1.0, "interface.init-out-of-range", (("field", "value_min"),), id="two-step-below"
            ),
            pytest.param("ProbeRef2", "abc", "interface.init-string-scalar", (), id="two-step-string"),
        ],
    )
    def test_the_value_is_judged_against_the_bottom_value_type(
        self, project: Path, type_name: str, init: Any, rule: str | None, steps: tuple[tuple[str, Any], ...]
    ) -> None:
        model_root = project / "model"
        _chain(model_root)
        _interface(model_root, type_name, init)

        records = check_init_conformance(str(model_root)).records

        if rule is None:
            assert records == ()
            assert check_cross_layer_consistency(str(model_root)).consistent
        else:
            assert [record.rule for record in records] == [rule]
            assert records[0].subjects == (_INIT, _subject("implementation_types", "ProbePercent", *steps))

    @pytest.mark.parametrize(
        "bottom",
        [
            pytest.param({"name": "ProbeRef1", "kind": "reference_type", "base": "NoSuchType"}, id="unresolved"),
            pytest.param({"name": "ProbeRef1", "kind": "reference_type", "base": "float32_base"}, id="base-type"),
            pytest.param({"name": "ProbeRef1", "kind": "reference_type", "base": "ProbeRef2"}, id="cycle"),
        ],
    )
    def test_the_walk_abstains_where_the_chain_has_no_value_type(self, project: Path, bottom: dict[str, Any]) -> None:
        """Each of these is another rule's finding, so no init finding is added."""
        model_root = project / "model"
        _chain(model_root)
        _write(model_root, model_element_storage_name("ProbeRef1", kind=bottom["kind"]), bottom)
        _interface(model_root, "ProbeRef2", "abc")

        assert check_init_conformance(str(model_root)).records == ()
        assert not check_cross_layer_consistency(str(model_root)).consistent

    def test_a_base_typed_element_is_the_undeclared_type_rules_and_not_the_walks(self, project: Path) -> None:
        """A base type is not declared in `implementation_types`.

        That is where a data element's `implementation_type` resolves."""
        model_root = project / "model"
        _interface(model_root, "float32_base", "abc")

        assert check_init_conformance(str(model_root)).records == ()
        assert {record.rule for record in check_cross_layer_consistency(str(model_root)).records} == {
            "interface.data-element-undeclared-type"
        }


# ---------------------------------------------------------------------------
# Shapes still refused, under the rule that owns each now
# ---------------------------------------------------------------------------


def _impl_twin_name(model_root: Path) -> None:
    write_model_element(model_root, {"name": "float32_base_impl", "kind": "value_type", "base": "float32_base"})


#: Shapes once refused under a retired rule, each still refused under the rule that owns it now.
STILL_REFUSED: dict[str, tuple[Callable[[Path], None], str]] = {
    # A value type's `base` resolves in `base_types` alone.
    "a value type over a value type (was alias-over-alias)": (
        _value_over_value,
        "data-type.value-type-undeclared-base",
    ),
    "a value type over nothing (was alias-undeclared-base)": (
        _value_over_nothing,
        "data-type.value-type-undeclared-base",
    ),
    "a value type over an enum (was alias-over-alias)": (
        lambda root: (
            _write(
                root,
                model_element_storage_name("ProbeEnum", kind="enum"),
                {"name": "ProbeEnum", "kind": "enum", "literals": [{"name": "A", "value": 0}]},
            ),
            _write(
                root,
                model_element_storage_name("ProbeValue", kind="value_type"),
                {"name": "ProbeValue", "kind": "value_type", "base": "ProbeEnum"},
            ),
        ),
        "data-type.value-type-undeclared-base",
    ),
    "a string init under a base-typed element (was init-string-scalar)": (
        lambda root: _interface(root, "float32_base", "abc"),
        "interface.data-element-undeclared-type",
    ),
    # The value type -> base edge is not in the cycle graph: each `base` names no
    # declared base type, and that is the refusal.
    "two value types over each other (was alias-over-alias and a cycle)": (
        lambda root: (
            _write(
                root,
                model_element_storage_name("ProbeA", kind="value_type"),
                {"name": "ProbeA", "kind": "value_type", "base": "ProbeB"},
            ),
            _write(
                root,
                model_element_storage_name("ProbeB", kind="value_type"),
                {"name": "ProbeB", "kind": "value_type", "base": "ProbeA"},
            ),
        ),
        "data-type.value-type-undeclared-base",
    ),
}


class TestTheOnlyWideningIsTheWithdrawnImplReservation:
    def test_a_model_refused_only_for_an_impl_collision_is_now_accepted(self, project: Path) -> None:
        model_root = project / "model"
        _impl_twin_name(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("label", sorted(STILL_REFUSED))
    def test_every_other_previously_refused_shape_is_still_refused(self, project: Path, label: str) -> None:
        break_it, rule = STILL_REFUSED[label]
        model_root = project / "model"
        break_it(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert not result.consistent, label
        assert rule in {record.rule for record in result.records}, sorted(r.rule for r in result.records)
