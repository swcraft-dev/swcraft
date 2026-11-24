"""The cross-element rules of application types and mapping sets.

`application_type` is a kind of its own, the `DATA-TYPE-MAPPING-SET` an element of its own layer, and a component
names the sets its behaviour maps with. Nine rules, minted by four checks:

* `check_mapping_set_references` — the layer's two reference roles, the component's reference into it, and a set
  name declared twice; each map rule covers both an undeclared name and a name of the wrong kind, one
  `broken-reference` with one repair;
* `check_application_type_mapping` — `[constr_1004]`'s direction: every application type and enumeration a
  component's ports speak is paired with an implementation type by a set that component names; one finding per
  `(component, type)`;
* `check_application_type_double_mapping` — the other half: the named sets pair no application type with two
  implementation types;
* `check_data_type_references` — the names an application type generates (`_Unit`, `_Compu`, `_Range`), each
  reserved **only** for a type that declares the field the writer emits that element for.

A composite's members resolve in `application_types` alone, so a member naming an implementation type is the
member's undeclared-name rule; its model is kept in :data:`RESTATED` under that rule.

**What this module does not reach.** The writer's refusals (the writer's tests); the budgets and length findings of
the three forms (`test_validator_identifier.py`); the element-local half, which is the loader's
(`test_schema_application_types.py`); and whether a pair is *meaningful*, which the model cannot decide.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import validator_package
import yaml
from conftest import model_element_storage_name, write_model_element
from model_contract_fixtures import load_binding
from swcraft_tools import validator
from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, SubjectStep
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, CONSISTENCY_VIOLATION, RULE_CODES, diagnose
from swcraft_tools.schema import ApplicationDataType
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    ConsistencyResult,
    check_application_type_mapping,
    check_cross_layer_consistency,
    check_data_type_references,
    check_mapping_set_references,
)
from swcraft_tools.validator.init_values import _init_application_violations
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

#: The nine summaries, written out by hand: the expected value may not be read
#: from the document it checks.
SUMMARIES: dict[str, str] = {
    # A map's `application` resolves in `application_types` alone, so a name of
    # the other side is an undeclared one.
    "mapping-set.unknown-application": (
        "A mapping set's `application` naming no declared application type, enumeration, record or array."
    ),
    "mapping-set.unknown-implementation": (
        "A mapping set's `implementation` naming no declared data type, or naming one that is neither a value "
        "type, a reference type, an implementation record, an implementation array nor a pointer."
    ),
    "swc.unknown-mapping-set": "A component's `data_type_mappings` naming no declared mapping set.",
    # A component's variable is mapped by the same sets.
    "data-type.application-type-unmapped": (
        "A component whose port, or whose variable, is typed by an application type or an enumeration that none "
        "of the mapping sets it names pairs with an implementation type."
    ),
    # A generated form lies in its owner's package, so each of the three
    # collides inside that package only.
    "data-type.unit-name-collision": (
        "A declared name colliding with an application type's generated `_Unit` in the type's package."
    ),
    "data-type.compu-name-collision": (
        "A declared name colliding with an application type's generated `_Compu` in the type's package."
    ),
    # A value type's bounds generate a `_Range` of their own.
    "data-type.range-name-collision": (
        "A declared name colliding with an application type's or a value type's generated `_Range` in the "
        "type's package."
    ),
    # A set shares a package with no data type, so a set may take a data
    # type's name (`TestASetMayTakeADataTypesName`).
    "mapping-set.duplicate-name": "The same mapping set name declared in more than one file.",
    # `[constr_1004]`'s other half.
    "data-type.application-type-double-mapped": (
        "A component whose mapping sets pair one application type or enumeration with more than one "
        "implementation type between them."
    ),
}

#: The protocol code each is answered with. The three references are
#: `broken-reference` — declaring the missing element clears every referrer at
#: once — and the five rules over an assembled, fully resolved model are
#: `consistency-violation`.
CODES: dict[str, str] = {
    "mapping-set.unknown-application": BROKEN_REFERENCE,
    "mapping-set.unknown-implementation": BROKEN_REFERENCE,
    "swc.unknown-mapping-set": BROKEN_REFERENCE,
    "data-type.application-type-unmapped": CONSISTENCY_VIOLATION,
    "data-type.unit-name-collision": CONSISTENCY_VIOLATION,
    "data-type.compu-name-collision": CONSISTENCY_VIOLATION,
    "data-type.range-name-collision": CONSISTENCY_VIOLATION,
    # A uniqueness rule over emitted names on an assembled model, so it takes
    # the tenth code beside `name.duplicate-component-type`.
    "mapping-set.duplicate-name": CONSISTENCY_VIOLATION,
    # Every name resolves; what fails is a rule over the union of the sets.
    "data-type.application-type-double-mapped": CONSISTENCY_VIOLATION,
}

#: Which function mints each, as `binding.json` declares it: a check, or a step
#: of the check `REPORTING_CHECKS` names for it.
BOUND_CALLABLES: dict[str, str] = {
    "mapping-set.unknown-application": "check_mapping_set_references",
    "mapping-set.unknown-implementation": "check_mapping_set_references",
    "swc.unknown-mapping-set": "check_mapping_set_references",
    "data-type.application-type-unmapped": "check_application_type_mapping",
    "data-type.unit-name-collision": "_generated_form_collision_records",
    "data-type.compu-name-collision": "_generated_form_collision_records",
    "data-type.range-name-collision": "_generated_form_collision_records",
    # Bound to the mapping-set check and not to `check_name_uniqueness`, where
    # the `/ComponentTypes` twin lives: that check already holds the three
    # layers these need, and the duplicate rule has to be minted in the same
    # function `check_application_type_mapping` abstains against.
    "mapping-set.duplicate-name": "check_mapping_set_references",
    "data-type.application-type-double-mapped": "check_application_type_double_mapping",
}

#: The check that reports a rule whose bound function is a step of it, not the check itself.
REPORTING_CHECKS: dict[str, str] = {
    "_generated_form_collision_records": "check_data_type_references",
}

#: A retired rule, asserted declared nowhere.
RETIRED_COMPOSITE_MEMBER_RULE = "data-type.composite-member-implementation-type"

_SR_INTERFACE = "interfaces/IExample.yaml"
_PROVIDER = "swc/example/ExampleSWC.yaml"
_CONSUMER = "swc/example/ExampleConsumerSWC.yaml"


# ---------------------------------------------------------------------------
# Model builders over the vector harness's base model (examples/basic)
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real, valid project whose `mapping_sets/` is present and empty."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _write(model_root: Path, relative: str, document: dict[str, Any]) -> None:
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _read(model_root: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _change(model_root: Path, relative: str, mutate: Callable[[dict[str, Any]], object]) -> None:
    document = _read(model_root, relative)
    mutate(document)
    _write(model_root, relative, document)


def _application_type(model_root: Path, name: str = "Speed", **physics: Any) -> None:
    """A declared application type, with whichever of the three fields *physics* names."""
    write_model_element(model_root, {"name": name, "kind": "application_type", **physics})


def _mapping_set(model_root: Path, name: str, *maps: tuple[str, str]) -> None:
    write_model_element(
        model_root,
        {"name": name, "maps": [{"application": a, "implementation": i} for a, i in maps]},
        layer="mapping_sets",
    )


def _retype(prototype: dict[str, Any], field: str, type_name: str, **extra: Any) -> None:
    """Type *prototype* by *type_name* through *field*, in the position its type field held.

    A data element and an argument set exactly one of `application_type` and
    `implementation_type`, so the other one is dropped.
    """
    items = [
        (field if key in ("implementation_type", "application_type") else key, value)
        for key, value in prototype.items()
    ]
    prototype.clear()
    prototype.update({key: (type_name if key == field else value) for key, value in items})
    prototype.update(extra)


def _typed_element(model_root: Path, type_name: str, *, field: str = "implementation_type") -> None:
    """Give `IExample.value` another type, which both components' ports then speak.

    *field* is `application_type` for an application type.
    """
    _change(model_root, _SR_INTERFACE, lambda d: _retype(d["data_elements"][0], field, type_name))


def _subject(layer: str, element: str, *steps: tuple[str, Any]) -> Subject:
    return Subject(layer, element, tuple(SubjectStep(kind, value) for kind, value in steps))


def _map_subject(set_name: str, application: str, side: str) -> Subject:
    return _subject("mapping_sets", set_name, ("field", "maps"), ("member", application), ("field", side))


# ---------------------------------------------------------------------------
# One break per rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Break:
    """One break of the base model, the records it must mint, and what they must say."""

    apply: Callable[[Path], None]
    #: The subjects of each expected record, in the order the records are minted.
    subjects: tuple[tuple[Subject, ...], ...]
    #: Substrings the joined messages must carry: the offender and the repair.
    fragments: tuple[str, ...]


def _unmapped_application_type(model_root: Path) -> None:
    _application_type(model_root, unit="Kph")
    _typed_element(model_root, "Speed", field="application_type")


def _map_over_nothing(model_root: Path) -> None:
    _mapping_set(model_root, "Probe", ("NoSuchType", "float32"))


def _map_onto_nothing(model_root: Path) -> None:
    _application_type(model_root)
    _mapping_set(model_root, "Probe", ("Speed", "NoSuchType"))


def _component_names_no_set(model_root: Path) -> None:
    _change(model_root, _PROVIDER, lambda d: d.update(data_type_mappings=["Nope"]))


def _record_field_over_a_value_type(model_root: Path) -> None:
    write_model_element(
        model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "float32"}]}
    )


def _unit_collision(model_root: Path) -> None:
    _application_type(model_root, unit="Kph")
    # A form collides inside its owner's package only.
    _application_type(model_root, "Speed_Unit")


def _compu_collision(model_root: Path) -> None:
    # A unit: a conversion without one is `data-type.conversion-without-unit`'s
    # finding.
    _application_type(model_root, unit="Kph", conversion={"factor": 0.1})
    # A form collides inside its owner's package only.
    _application_type(model_root, "Speed_Compu")


def _range_collision(model_root: Path) -> None:
    _application_type(model_root, range={"min": 0, "max": 400})
    # A form collides inside its owner's package only.
    _application_type(model_root, "Speed_Range")


#: One row per rule. Every break is over the base model, and every one of them
#: isolates its own rule: the assertions below are equalities over the rule set
#: the composed check reports, not memberships.
def _duplicate_set_name(model_root: Path) -> None:
    """Two files declaring one set name.

    The FILE names differ -- the identity is what a file declares -- so both load
    and the layer carries two.
    """
    _mapping_set(model_root, "Probe")
    _write(model_root, model_element_storage_name("Probe_again", layer="mapping_sets"), {"name": "Probe", "maps": []})


def _double_mapping(model_root: Path) -> None:
    """Two sets pairing one application type with two implementation types, both named by one component.

    No port speaks `Speed`, so `application-type-unmapped` cannot fire: the
    constraint is about the behaviour's mapping, not about what the ports use."""
    _application_type(model_root)
    _mapping_set(model_root, "ProbeA", ("Speed", "float32"))
    _mapping_set(model_root, "ProbeB", ("Speed", "uint16"))
    _change(model_root, _PROVIDER, lambda d: d.update(data_type_mappings=["ProbeA", "ProbeB"]))


BREAKS: dict[str, Break] = {
    "data-type.application-type-double-mapped": Break(
        _double_mapping,
        ((_subject("swc", "ExampleSWC", ("field", "data_type_mappings")),),),
        ("'Speed' onto 'float32' through set 'ProbeA' and onto 'uint16' through set 'ProbeB'", "[constr_1004]"),
    ),
    "data-type.application-type-unmapped": Break(
        _unmapped_application_type,
        # Both components' ports speak `IExample`, so both are reported — in the
        # `swc` layer's reader order, which is the storage path's.
        (
            (_subject("swc", "ExampleConsumerSWC", ("field", "data_type_mappings")),),
            (_subject("swc", "ExampleSWC", ("field", "data_type_mappings")),),
        ),
        ("Speed", "add the pair to a set the component names"),
    ),
    "mapping-set.unknown-application": Break(
        _map_over_nothing,
        ((_map_subject("Probe", "NoSuchType", "application"),),),
        ("NoSuchType", "must name a declared application_type, enum, record or array"),
    ),
    "mapping-set.unknown-implementation": Break(
        _map_onto_nothing,
        ((_map_subject("Probe", "Speed", "implementation"),),),
        (
            "NoSuchType",
            "must name a declared value_type, reference_type, implementation_record or implementation_array",
        ),
    ),
    "swc.unknown-mapping-set": Break(
        _component_names_no_set,
        ((_subject("swc", "ExampleSWC", ("field", "data_type_mappings"), ("member", "Nope")),),),
        ("Nope", "not declared in the mapping_sets layer"),
    ),
    "mapping-set.duplicate-name": Break(
        _duplicate_set_name,
        ((_subject("mapping_sets", "Probe"),),),
        ("Probe", "declared in multiple files"),
    ),
    "data-type.unit-name-collision": Break(
        _unit_collision,
        ((_subject("application_types", "Speed_Unit"),),),
        ("Speed_Unit", "auto-generated UNIT name"),
    ),
    "data-type.compu-name-collision": Break(
        _compu_collision,
        ((_subject("application_types", "Speed_Compu"),),),
        ("Speed_Compu", "auto-generated COMPU-METHOD name"),
    ),
    "data-type.range-name-collision": Break(
        _range_collision,
        ((_subject("application_types", "Speed_Range"),),),
        ("Speed_Range", "auto-generated DATA-CONSTR name"),
    ),
}


# ---------------------------------------------------------------------------
# The nine, registered once and emitted where the contract says
# ---------------------------------------------------------------------------


#: The retired rule's model, restated: a record field naming a value type names
#: nothing declared in `application_types`, where the field resolves.
RESTATED: dict[str, tuple[Callable[[Path], None], str, tuple[Subject, ...], tuple[str, ...]]] = {
    "a record field typed by a value type (was data-type.composite-member-implementation-type)": (
        _record_field_over_a_value_type,
        "data-type.record-field-undeclared-type",
        (_subject("application_types", "ProbeRecord", ("field", "fields"), ("member", "a"), ("field", "type")),),
        ("ProbeRecord", "'float32'"),
    ),
}


class TestTheEightAreRegisteredEverywhereOnce:
    def test_the_break_table_is_exactly_the_rules_this_slice_declared(self) -> None:
        assert set(BREAKS) == set(SUMMARIES) == set(CODES) == set(BOUND_CALLABLES)

    def test_the_retired_composite_member_rule_is_declared_nowhere(self) -> None:
        declared = {entry["id"] for entry in load_contract()["rules"]}

        assert f"rule:{RETIRED_COMPOSITE_MEMBER_RULE}" not in declared
        assert f"rule:{RETIRED_COMPOSITE_MEMBER_RULE}" not in load_binding()["rules"]
        assert RETIRED_COMPOSITE_MEMBER_RULE not in CONSISTENCY_RULES
        assert RETIRED_COMPOSITE_MEMBER_RULE not in RULE_CODES

    @pytest.mark.parametrize("rule", sorted(SUMMARIES))
    def test_the_inventory_carries_the_contracts_summary_word_for_word(self, rule: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}

        assert CONSISTENCY_RULES[rule] == SUMMARIES[rule] == declared[f"rule:{rule}"]["summary"]
        assert declared[f"rule:{rule}"]["family"] == "cross-element"

    @pytest.mark.parametrize("rule", sorted(CODES))
    def test_the_protocol_answers_it_with_its_code(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CODES[rule]

    @pytest.mark.parametrize("rule", sorted(BOUND_CALLABLES))
    def test_the_binding_names_the_check_that_mints_it(self, rule: str) -> None:
        bound = BOUND_CALLABLES[rule]
        # A bound step names the check that reaches it; only a bound check stands for itself.
        assert bound.startswith("check_") or bound in REPORTING_CHECKS, f"{bound} has no REPORTING_CHECKS entry"
        check = REPORTING_CHECKS.get(bound, bound)
        module = sys.modules[getattr(validator, check).__module__]
        assert load_binding()["rules"][f"rule:{rule}"] == {"module": module.__name__, "callable": bound}
        assert bound in validator_package.reached_functions(module, check)

    @pytest.mark.parametrize("rule", sorted(SUMMARIES))
    def test_the_emission_site_is_one_the_layer_scan_reads(self, rule: str) -> None:
        contract = load_contract()
        observed = emission_layers(contract)
        sites = observed[f"rule:{rule}"]

        assert sites and all(sites), sites
        assert [failure for failure in cross_element_layer_failures(contract, observed) if rule in failure] == []


# ---------------------------------------------------------------------------
# Each of the nine, driven through the composed check
# ---------------------------------------------------------------------------


class TestEachRuleFiresAlone:
    @pytest.mark.parametrize("rule", sorted(BREAKS))
    def test_the_composed_check_reports_exactly_this_rule(self, project: Path, rule: str) -> None:
        entry = BREAKS[rule]
        model_root = project / "model"
        entry.apply(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {rule}, sorted(r.rule for r in result.records)
        assert tuple(record.subjects for record in result.records) == entry.subjects
        joined = " ".join(record.message for record in result.records)
        for fragment in entry.fragments:
            assert fragment in joined, joined

    @pytest.mark.parametrize("rule", sorted(BREAKS))
    def test_the_translation_answers_with_the_code_the_table_assigns(self, project: Path, rule: str) -> None:
        model_root = project / "model"
        BREAKS[rule].apply(model_root)

        result = check_cross_layer_consistency(str(model_root))
        own = ConsistencyResult(consistent=False, records=tuple(r for r in result.records if r.rule == rule))

        assert {violation.error for violation in diagnose(own)} == {CODES[rule]}

    @pytest.mark.parametrize("label", sorted(RESTATED))
    def test_a_restated_model_reports_exactly_its_undeclared_type_rule(self, project: Path, label: str) -> None:
        apply, rule, subjects, fragments = RESTATED[label]
        model_root = project / "model"
        apply(model_root)

        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {rule}, sorted(r.rule for r in result.records)
        (record,) = result.records
        assert record.subjects == subjects
        for fragment in fragments:
            assert fragment in record.message, record.message
        assert {violation.error for violation in diagnose(result)} == {BROKEN_REFERENCE}
        assert RETIRED_COMPOSITE_MEMBER_RULE not in {entry.rule for entry in result.records}

    def test_the_base_model_reports_none_of_them(self, project: Path) -> None:
        result = check_cross_layer_consistency(str(project / "model"))

        assert result.consistent, result.issues


# ---------------------------------------------------------------------------
# `data-type.application-type-unmapped`, condition by condition
# ---------------------------------------------------------------------------


class TestTheUnmappedRuleFiresPerComponentAndType:
    def test_a_declared_pair_in_a_named_set_closes_it(self, project: Path) -> None:
        model_root = project / "model"
        _unmapped_application_type(model_root)
        _mapping_set(model_root, "Sets", ("Speed", "float32"))
        for relative in (_PROVIDER, _CONSUMER):
            _change(model_root, relative, lambda d: d.update(data_type_mappings=["Sets"]))

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_a_set_that_maps_it_but_is_not_named_by_the_component_does_not(self, project: Path) -> None:
        """The pairing is the component's behaviour's, so a set nothing names answers nothing."""
        model_root = project / "model"
        _unmapped_application_type(model_root)
        _mapping_set(model_root, "Sets", ("Speed", "float32"))

        reported = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert reported == ["data-type.application-type-unmapped"] * 2

    def test_a_component_with_no_such_port_is_silent(self, project: Path) -> None:
        """Declaring the type is not speaking it: only a port's own members count."""
        model_root = project / "model"
        _application_type(model_root, unit="Kph")

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_an_enum_on_a_port_is_counted(self, project: Path) -> None:
        """An enum (a VALUE application primitive over a TEXTTABLE method) is mapped as an application_type is."""
        model_root = project / "model"
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        _typed_element(model_root, "Mode", field="application_type")

        reported = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert reported == ["data-type.application-type-unmapped"] * 2

    def test_a_client_server_argument_is_counted_too(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root)
        _change(
            model_root,
            "interfaces/IDiagnosticService.yaml",
            lambda d: _retype(d["operations"][0]["arguments"][0], "application_type", "Speed"),
        )
        # No component declares a port on that interface in the base model, and
        # a type nothing's port speaks is not spoken.
        _change(
            model_root,
            _CONSUMER,
            lambda d: d["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}),
        )

        reported = {
            (record.rule, record.subjects[0].element)
            for record in check_cross_layer_consistency(str(model_root)).records
        }

        assert reported == {("data-type.application-type-unmapped", "ExampleConsumerSWC")}

    def test_one_finding_per_component_and_type_however_many_ports_reach_it(self, project: Path) -> None:
        """A second port on the same interface is a second reach and not a second finding."""
        model_root = project / "model"
        _unmapped_application_type(model_root)
        _change(
            model_root,
            _CONSUMER,
            lambda d: d["ports"].append({"name": "rSecond", "direction": "require", "interface": "IExample"}),
        )

        reported = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert reported == ["data-type.application-type-unmapped"] * 2

    def test_two_unmapped_types_on_one_component_are_two_findings(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root, "Speed")
        _application_type(model_root, "Torque")
        _change(
            model_root,
            _SR_INTERFACE,
            lambda d: d.update(
                data_elements=[
                    {"name": "value", "application_type": "Speed"},
                    {"name": "second", "application_type": "Torque"},
                ]
            ),
        )

        reported = [
            (record.subjects[0].element, record.message.split("'")[3])
            for record in check_cross_layer_consistency(str(model_root)).records
        ]

        assert reported == [
            ("ExampleConsumerSWC", "Speed"),
            ("ExampleConsumerSWC", "Torque"),
            ("ExampleSWC", "Speed"),
            ("ExampleSWC", "Torque"),
        ]


# ---------------------------------------------------------------------------
# The two `unknown-*` rules with a wrong KIND on each side
# ---------------------------------------------------------------------------


class TestAMapMayOnlyNameTheKindsItsSideAdmits:
    def test_a_record_onto_a_value_type_is_refused_by_the_composite_check(self, project: Path) -> None:
        """A record mapped onto a value type is refused by the category check.

        A record may be mapped — onto an implementation record — so
        ``mapping-set.unknown-application`` admits it, and
        ``mapping-set.incompatible-composite-map``'s category check refuses a record
        against a value type.
        """
        model_root = project / "model"
        _record_field_over_a_value_type(model_root)
        _change(
            model_root,
            model_element_storage_name("ProbeRecord", kind="record"),
            lambda d: d["fields"][0].update(type="uint8"),
        )
        _mapping_set(model_root, "Probe", ("ProbeRecord", "float32"))

        rules = [entry.rule for entry in check_cross_layer_consistency(str(model_root)).records]
        (record,) = [
            entry
            for entry in check_cross_layer_consistency(str(model_root)).records
            if entry.rule == "mapping-set.incompatible-composite-map"
        ]

        assert "mapping-set.unknown-application" not in rules
        assert "category record 'ProbeRecord' vs value_type 'float32'" in record.message
        assert record.target is None

    def test_a_base_type_on_the_implementation_side_is_refused(self, project: Path) -> None:
        """A base type on the implementation side is an undeclared name.

        A map's sides resolve in `implementation_types`, which holds no base type, so
        the finding carries its target like any other undeclared name.
        """
        model_root = project / "model"
        _application_type(model_root)
        _mapping_set(model_root, "Probe", ("Speed", "float32_base"))

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "mapping-set.unknown-implementation"
        assert "'float32_base', which is not declared in the implementation_types layer" in record.message
        assert record.target == "float32_base"

    def test_an_undeclared_name_carries_the_target_that_makes_two_referrers_one_repair(self, project: Path) -> None:
        model_root = project / "model"
        _mapping_set(model_root, "Probe", ("NoSuchType", "float32"))
        _mapping_set(model_root, "Second", ("NoSuchType", "uint8"))

        (violation,) = diagnose(check_mapping_set_references(str(model_root)))

        assert violation.error == BROKEN_REFERENCE
        assert len(violation.addresses) == 2, violation.addresses

    def test_an_application_type_on_the_implementation_side_is_refused(self, project: Path) -> None:
        """The two sides are not interchangeable: a physical type stores nothing.

        The `implementation` side resolves in `implementation_types` alone, so an
        application type there is an undeclared name of that layer.
        """
        model_root = project / "model"
        _application_type(model_root)
        _mapping_set(model_root, "Probe", ("Speed", "Speed"))

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "mapping-set.unknown-implementation"
        assert "'Speed', which is not declared in the implementation_types layer" in record.message
        assert record.target == "Speed"


# ---------------------------------------------------------------------------
# A composite member is the physical view
# ---------------------------------------------------------------------------


class TestACompositeMemberIsThePhysicalView:
    def test_an_array_element_over_a_reference_type_is_an_undeclared_element_type(self, project: Path) -> None:
        """An array's `element_type` resolves in `application_types` alone: a reference type there is undeclared."""
        model_root = project / "model"
        write_model_element(model_root, {"name": "ProbeRef", "kind": "reference_type", "base": "float32"})
        write_model_element(
            model_root, {"name": "ProbeArray", "kind": "array", "element_type": "ProbeRef", "length": 2}
        )

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "data-type.array-undeclared-element-type"
        assert "ProbeArray" in record.message and "ProbeRef" in record.message
        assert record.message.endswith("in the application_types layer."), record.message
        assert RETIRED_COMPOSITE_MEMBER_RULE not in RULE_CODES

    @pytest.mark.parametrize("member_type", ["Speed", "Mode", "ProbeInner"])
    def test_the_physical_kinds_are_accepted(self, project: Path, member_type: str) -> None:
        model_root = project / "model"
        _application_type(model_root)
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        write_model_element(
            model_root, {"name": "ProbeInner", "kind": "record", "fields": [{"name": "a", "type": "Speed"}]}
        )
        write_model_element(
            model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": member_type}]}
        )

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_a_base_type_member_stays_the_undeclared_type_rules_finding(self, project: Path) -> None:
        """A base-type member is an undeclared type.

        A record field's `type` resolves in `application_types`, which holds no base
        type.
        """
        model_root = project / "model"
        write_model_element(
            model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "float32_base"}]}
        )

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "data-type.record-field-undeclared-type"
        assert "data-type.record-field-base-type" not in RULE_CODES


# ---------------------------------------------------------------------------
# The three generated names, reserved only where they are generated
# ---------------------------------------------------------------------------


class TestAGeneratedNameIsReservedOnlyWhereItIsGenerated:
    @pytest.mark.parametrize(
        ("suffix", "physics"),
        [
            ("_Unit", {"unit": "Kph"}),
            # A unit beside the conversion (`data-type.conversion-without-unit`).
            ("_Compu", {"unit": "Kph", "conversion": {"factor": 0.1}}),
            ("_Range", {"range": {"max": 4}}),
        ],
    )
    def test_the_name_is_free_when_the_field_is_not_declared(
        self, project: Path, suffix: str, physics: dict[str, Any]
    ) -> None:
        """A type with no `unit` emits no `UNIT`, so `{name}_Unit` collides with nothing."""
        model_root = project / "model"
        _application_type(model_root)
        # An occupant of the owner's package, where the form collides.
        _application_type(model_root, f"Speed{suffix}")

        assert check_cross_layer_consistency(str(model_root)).consistent

        _application_type(model_root, **physics)

        assert not check_cross_layer_consistency(str(model_root)).consistent

    def test_a_blank_unit_is_no_unit(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root, unit="   ")
        _application_type(model_root, "Speed_Unit")

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_a_stem_that_is_not_an_application_type_reserves_nothing(self, project: Path) -> None:
        """`float32` is a value type, so `float32_Unit` is an ordinary declared name."""
        model_root = project / "model"
        _application_type(model_root, "float32_Unit")

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_an_empty_conversion_still_generates_the_compu_method(self, project: Path) -> None:
        """`conversion: {}` is the identity conversion and is written, so the name is taken."""
        model_root = project / "model"
        _application_type(model_root, unit="Kph", conversion={})
        _application_type(model_root, "Speed_Compu")

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "data-type.compu-name-collision"

    @pytest.mark.parametrize(
        ("suffix", "physics"),
        [
            ("_Unit", {"unit": "Kph"}),
            ("_Compu", {"unit": "Kph", "conversion": {"factor": 0.1}}),
            ("_Range", {"range": {"max": 4}}),
        ],
    )
    def test_a_type_of_another_package_under_the_name_collides_with_nothing(
        self, project: Path, suffix: str, physics: dict[str, Any]
    ) -> None:
        """The form lies in `/DataTypes/Application`, a value type in `/DataTypes/Implementation`: no siblings."""
        model_root = project / "model"
        _application_type(model_root, **physics)
        _write(
            model_root,
            model_element_storage_name(f"Speed{suffix}", kind="value_type"),
            {"name": f"Speed{suffix}", "kind": "value_type", "base": "float32_base"},
        )

        assert check_cross_layer_consistency(str(model_root)).consistent


class TestASetMayTakeADataTypesName:
    """A set may take a data type's name.

    A set lies in `/DataTypes/MappingSets` and a type, with every form it mints,
    in its own layer's package, so a set named like either is no sibling of it
    and the model is accepted.
    """

    @pytest.mark.parametrize("set_name", ["float32", "Speed", "Speed_Unit"])
    def test_the_model_is_consistent(self, project: Path, set_name: str) -> None:
        model_root = project / "model"
        _application_type(model_root, unit="Kph")
        _mapping_set(model_root, set_name)

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues
        assert "mapping-set.data-type-name-collision" not in CONSISTENCY_RULES


# ---------------------------------------------------------------------------
# Abstention: one defect, one finding
# ---------------------------------------------------------------------------


class TestEachCheckAbstainsWhereAnotherRuleOwnsTheDefect:
    def test_an_undeclared_element_type_is_the_reference_rules_finding_alone(self, project: Path) -> None:
        model_root = project / "model"
        _typed_element(model_root, "NoSuchType")

        reported = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert reported == {"interface.data-element-undeclared-type"}

    def test_a_port_with_no_declared_interface_is_the_port_rules_finding_alone(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root)
        _change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="Nope"))

        reported = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert "data-type.application-type-unmapped" not in reported
        assert "port.undeclared-interface" in reported

    def test_a_component_naming_an_undeclared_set_is_the_set_rules_finding_alone(self, project: Path) -> None:
        """The unmapped rule cannot answer for a set it cannot read, so it abstains."""
        model_root = project / "model"
        _unmapped_application_type(model_root)
        _change(model_root, _PROVIDER, lambda d: d.update(data_type_mappings=["Nope"]))

        reported = [
            (record.rule, record.subjects[0].element)
            for record in check_cross_layer_consistency(str(model_root)).records
        ]

        assert reported == [
            ("swc.unknown-mapping-set", "ExampleSWC"),
            ("data-type.application-type-unmapped", "ExampleConsumerSWC"),
        ]

    def test_a_double_mapping_through_a_duplicated_set_is_the_duplicate_rules_finding_alone(
        self, project: Path
    ) -> None:
        """A set two files declare reads last-wins, so the double-mapping rule abstains."""
        model_root = project / "model"
        _double_mapping(model_root)
        _write(
            model_root, model_element_storage_name("ProbeB_again", layer="mapping_sets"), {"name": "ProbeB", "maps": []}
        )

        reported = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert reported == {"mapping-set.duplicate-name"}

    def test_a_double_mapping_beside_an_undeclared_set_is_still_reported(self, project: Path) -> None:
        """Unlike the unmapped rule it does not abstain on an unknown name.

        No set the name could come to mean can undo a double mapping the declared ones make."""
        model_root = project / "model"
        _double_mapping(model_root)
        _change(model_root, _PROVIDER, lambda d: d.update(data_type_mappings=["ProbeA", "Nope", "ProbeB"]))

        reported = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert reported == ["swc.unknown-mapping-set", "data-type.application-type-double-mapped"]

    def test_two_sets_that_agree_are_not_reported(self, project: Path) -> None:
        model_root = project / "model"
        _double_mapping(model_root)
        _mapping_set(model_root, "ProbeB", ("Speed", "float32"))

        assert check_cross_layer_consistency(str(model_root)).consistent


# ---------------------------------------------------------------------------
# The editing protocol: pass D refuses a double mapping
# ---------------------------------------------------------------------------


class TestPassDRefusesADoubleMapping:
    """The editing protocol commits nothing `check-model` would report as a double mapping.

    Pass D runs the composed `check_cross_layer_consistency`; this is the
    witness, on a change set that CREATES the finding over a valid baseline.
    """

    _ADDRESS = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "data_type_mappings"}]}

    def _baseline(self, project: Path, second: str) -> Path:
        """`ExampleSWC` naming `ProbeA` alone, with `ProbeB` declared beside it pairing `Speed` onto *second*."""
        model_root = project / "model"
        _double_mapping(model_root)
        _mapping_set(model_root, "ProbeB", ("Speed", second))
        _change(model_root, _PROVIDER, lambda d: d.update(data_type_mappings=["ProbeA"]))
        assert check_cross_layer_consistency(str(model_root)).consistent
        return model_root

    def _name_both(self, project: Path) -> dict[str, Any]:
        from swcraft_tools.editing.engine import handle_request
        from swcraft_tools.editing.staging import baseline_token

        return handle_request(
            {
                "operation": "change_set",
                "baseline": baseline_token(project / "model"),
                "intent": "name a second mapping set",
                # `data_type_mappings` is a whole_value list: a member is added
                # by a `create` at its own address, never by an update of the
                # whole collection.
                "operations": [
                    {
                        "operation": "create",
                        "address": {**self._ADDRESS, "path": [*self._ADDRESS["path"], {"member": "ProbeB"}]},
                        "value": "ProbeB",
                    }
                ],
            },
            project=project,
        )

    def test_a_change_set_that_double_maps_a_type_is_refused_and_writes_nothing(self, project: Path) -> None:
        model_root = self._baseline(project, "uint16")
        before = {path: path.read_bytes() for path in model_root.rglob("*.yaml")}

        response = self._name_both(project)

        assert response["error"] == "consistency-violation", response
        assert "maps type 'Speed' onto 'float32' through set 'ProbeA'" in response["message"], response
        assert response["addresses"] == [self._ADDRESS], response
        assert {path: path.read_bytes() for path in model_root.rglob("*.yaml")} == before

    def test_the_same_change_set_over_two_sets_that_agree_is_not_refused(self, project: Path) -> None:
        self._baseline(project, "float32")

        response = self._name_both(project)

        assert "error" not in response, response


# ---------------------------------------------------------------------------
# The composition: appended last, and the order of every existing rule unmoved
# ---------------------------------------------------------------------------


#: A model breaking five data-type rules, and the order
#: `check_data_type_references` reports them in. The generated-name and
#: composite branches sit inside the record and array loops, so a mis-placed
#: `elif` would reorder what the check reports.
_EXISTING_ORDER: tuple[str, ...] = (
    "data-type.duplicate-name",
    "interface.data-element-undeclared-type",
    # The data-type loop is the LAYER's reader order, which is the storage
    # path's: `ProbeArray`, `ProbeRecord`, `ProbeValue`.
    "data-type.array-undeclared-element-type",
    "data-type.record-field-undeclared-type",
    "data-type.value-type-undeclared-base",
)

#: Retired ids the same model once reported, asserted absent.
_RETIRED_BY_THE_BASE_TYPES_LAYER: tuple[str, ...] = (
    "interface.data-element-base-type",
    "data-type.array-element-base-type",
    "data-type.record-field-base-type",
    "data-type.value-type-base-not-base-type",
)


def _break_five_existing_rules(model_root: Path) -> None:
    _change(
        model_root,
        model_element_storage_name("float32", kind="value_type"),
        lambda d: _write(model_root, model_element_storage_name("float32_again", kind=d["kind"]), d),
    )
    _typed_element(model_root, "float32_base")
    write_model_element(model_root, {"name": "ProbeValue", "kind": "value_type", "base": "float32"})
    write_model_element(
        model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "float32_base"}]}
    )
    write_model_element(
        model_root, {"name": "ProbeArray", "kind": "array", "element_type": "float32_base", "length": 2}
    )


class TestTheCompositionIsAppendedAndMovesNothing:
    def test_the_existing_rules_keep_the_order_they_had(self, project: Path) -> None:
        model_root = project / "model"
        _break_five_existing_rules(model_root)

        reported = tuple(record.rule for record in check_data_type_references(str(model_root)).records)

        assert reported == _EXISTING_ORDER
        assert set(reported).isdisjoint(_RETIRED_BY_THE_BASE_TYPES_LAYER)

    def test_the_new_branches_do_not_displace_them(self, project: Path) -> None:
        """A record field over a value type joins the five findings without splitting their order.

        The new finding is `data-type.record-field-undeclared-type`, one of the
        five's ids too, so the findings are compared by `(rule, element)`.
        """
        model_root = project / "model"
        _break_five_existing_rules(model_root)
        existing = [
            (record.rule, record.subjects[0].element) for record in check_data_type_references(str(model_root)).records
        ]
        write_model_element(
            model_root, {"name": "SecondRecord", "kind": "record", "fields": [{"name": "a", "type": "float32"}]}
        )

        reported = [
            (record.rule, record.subjects[0].element) for record in check_data_type_references(str(model_root)).records
        ]

        assert [rule for rule, _element in existing] == list(_EXISTING_ORDER)
        assert [pair for pair in reported if pair in existing] == existing
        assert [pair for pair in reported if pair not in existing] == [
            ("data-type.record-field-undeclared-type", "SecondRecord")
        ]
        assert RETIRED_COMPOSITE_MEMBER_RULE not in {rule for rule, _element in reported}

    def test_the_two_new_checks_answer_nothing_on_a_model_without_the_feature(self, project: Path) -> None:
        """A model with no mapping set and no application type: neither check finds anything."""
        model_root = project / "model"
        _break_five_existing_rules(model_root)

        assert check_mapping_set_references(str(model_root)).records == ()
        assert check_application_type_mapping(str(model_root)).records == ()


# ---------------------------------------------------------------------------
# `init` on an element typed by an application type: the same four identifiers
# ---------------------------------------------------------------------------


def _valued_element(model_root: Path, type_name: str, init: Any) -> None:
    """`IExample.value` typed by *type_name* and carrying *init*, with the pair mapped."""
    _change(
        model_root, _SR_INTERFACE, lambda d: _retype(d["data_elements"][0], "application_type", type_name, init=init)
    )
    _mapping_set(model_root, "Sets", (type_name, "float32"))
    for relative in (_PROVIDER, _CONSUMER):
        _change(model_root, relative, lambda d: d.update(data_type_mappings=["Sets"]))


class TestAnInitValueIsJudgedAgainstTheApplicationTypesRange:
    """`rule:interface.init-*` reads `range` exactly as it reads `value_min` / `value_max`.

    No rule is added: the four identifiers' `appliesTo` cover the application
    type, and this class is the behaviour that makes that true.
    """

    def test_a_value_inside_the_range_conforms(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root, unit="Kph", range={"min": 0, "max": 400})
        _valued_element(model_root, "Speed", 12.5)

        assert check_cross_layer_consistency(str(model_root)).consistent

    @pytest.mark.parametrize(("init", "bound"), [(-1, "range min"), (401, "range max")], ids=["below", "above"])
    def test_a_value_outside_it_is_out_of_range(self, project: Path, init: Any, bound: str) -> None:
        model_root = project / "model"
        _application_type(model_root, range={"min": 0, "max": 400})
        _valued_element(model_root, "Speed", init)

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "interface.init-out-of-range"
        assert f"declared {bound}" in record.message, record.message
        assert record.subjects[-1] == _subject(
            "application_types", "Speed", ("field", "range"), ("field", bound.split()[-1])
        )

    def test_a_type_declaring_no_range_bounds_nothing(self, project: Path) -> None:
        """A value type with neither bound is the same case, and for the same reason."""
        model_root = project / "model"
        _application_type(model_root, unit="Kph")
        _valued_element(model_root, "Speed", 10_000)

        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_a_list_is_a_shape_mismatch(self, project: Path) -> None:
        model_root = project / "model"
        _application_type(model_root)
        _valued_element(model_root, "Speed", [1.0, 2.0])

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "interface.init-shape-mismatch"

    def test_a_string_has_no_spelling(self, project: Path) -> None:
        """The exporter writes every scalar as a number, whatever the type's kind."""
        model_root = project / "model"
        _application_type(model_root)
        _valued_element(model_root, "Speed", "fast")

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "interface.init-string-scalar"

    def test_an_unwritable_integer_is_refused(self, project: Path) -> None:
        """In memory, not through a file: such an integer cannot be dumped to YAML at all.

        `yaml.safe_dump` raises CPython's own `ValueError` on it, so no
        file-built model reaches the walk — which is why
        `interface.init-unwritable-integer` is listed by id in
        `test_editing_validator_messages.py`'s `UNREACHED_BY_THE_MESSAGE_GATE` and why
        `test_validator_init.py` builds its value type's case in memory too.
        """
        declared = ApplicationDataType(name="Speed", kind="application_type")
        value = int("0x" + "f" * 4000, 16)

        (violation,) = _init_application_violations(value, "Speed", declared, DataDescription(()), "init")

        assert violation.rule == "interface.init-unwritable-integer"
        assert sys.get_int_max_str_digits() < 4817, "4 000 hexadecimal digits are 4 817 decimal ones"

    def test_a_composite_member_typed_by_one_is_walked_into(self, project: Path) -> None:
        """The record walk reaches the application type at its member, range and all."""
        model_root = project / "model"
        _application_type(model_root, range={"min": 0, "max": 400})
        write_model_element(
            model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "Speed"}]}
        )
        # The record itself is neither an application type nor an enumeration,
        # so no mapping is owed for it and the walk is the only thing judging.
        _change(
            model_root,
            _SR_INTERFACE,
            lambda d: _retype(d["data_elements"][0], "application_type", "ProbeRecord", init={"a": 900}),
        )

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "interface.init-out-of-range"


class TestAnApplicationTypeIsALegalPrototypeTarget:
    def test_a_base_typed_element_is_an_undeclared_type(self, project: Path) -> None:
        """A base-typed data element is an undeclared type.

        A data element's `implementation_type` resolves in `implementation_types`,
        which holds no base type; an application type stays a legal target
        (`test_the_physical_kinds_are_accepted`).
        """
        model_root = project / "model"
        _typed_element(model_root, "float32_base")

        (record,) = check_cross_layer_consistency(str(model_root)).records

        assert record.rule == "interface.data-element-undeclared-type"
        assert "undeclared type 'float32_base'" in record.message
        assert "interface.data-element-base-type" not in RULE_CODES

    def test_an_application_type_closes_no_cycle(self, project: Path) -> None:
        """It references no data type at all, so it is a sink in the dependency graph."""
        model_root = project / "model"
        _application_type(model_root)
        write_model_element(
            model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "a", "type": "Speed"}]}
        )

        reported = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert "data-type.cycle" not in reported
