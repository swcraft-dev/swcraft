"""The composition rules, the identifier scope, and the write-path agreement.

* **the rules.** Each rule of ``check_composition_consistency`` and ``check_connector_consistency`` gets a case
  built by perturbing exactly one element of ``fixtures/compositions/model``, a model the gate calls clean, so "and
  nothing else" is assertable;
* **the identifier scope.** The composition, each boundary port, each member and each delegation are ``SHORT-NAME``s
  the AUTOSAR ``Identifier`` rule reaches: ``check_identifier_conformance`` reports, the two
  ``_check_composition_names`` write-path twins refuse, and ``editing/diagnosis.emitted_identifier_violations`` is
  the protocol's pre-materialization half;
* **the write-path agreement class.** The writer *refuses* what ``check-model`` *reports*, asserted as a pair.

**What the agreement class does not reach:**

* the member and endpoint type-form shapes have no writer half: ``schema.py``'s two ``@model_validator``s refuse them
  at load;
* ``rule:composition.no-root`` is reported unconditionally and refused by the writer only when a connector leaves
  ``composition`` unset, so a member-less, connector-less composition layer with no root is reported and not
  refused;
* the two directions are compared on the rule identifier and the element, never on the message.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.registry as reg
import yaml
from conftest import (
    COMPOSITIONS_MODEL_ROOT,
    INTERFACE_COMPATIBILITY_MODEL_ROOT,
    REPO_ROOT,
    _write_manifest,
    model_element_path,
    write_model_element,
)
from swcraft_tools import validator
from swcraft_tools.cli import main
from swcraft_tools.contract import load_contract
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import RULE_CODES, diagnose, emitted_identifier_violations
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.identifier import REASON_PATTERN
from swcraft_tools.schema import Composition, CompositionMember, ConnectorEndpoint
from swcraft_tools.validator import (
    check_composition_consistency,
    check_connector_consistency,
    check_cross_layer_consistency,
    check_identifier_conformance,
)
from swcraft_tools.validator.compositions import _member_type_field
from swcraft_tools.validator.connectors import endpoint_type_field
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import _check_composition_names
from swcraft_tools.writer.elements import write_element

# The layer-read counter the connector half's guards use, so both callers of the
# predicate are held to one discipline by one helper.
from test_validator_scale import _counting_layer_reader

# The migrated roots, from the module that owns the enumeration.
from test_writer_compositions import (
    COMPOSITION_NAME_SITES,
    MIGRATED_MODEL_ROOTS,
    CompositionNameSite,
    one_of_each_site,
)

# ---------------------------------------------------------------------------
# The fixture, and the one-element perturbations
# ---------------------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """A writable copy of ``fixtures/compositions/model``; every case here breaks the model."""
    destination = tmp_path / "model"
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, destination)
    return destination


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


def _edit(model_root: Path, relative: str, **changes: Any) -> dict[str, Any]:
    """Apply *changes* to one element file and return the written document."""
    path = model_root / relative
    document = _load(path)
    document.update(changes)
    _write(path, document)
    return document


def _rules(model_root: Path) -> list[str]:
    """Every rule ``check_composition_consistency`` reports, in order."""
    return [record.rule for record in check_composition_consistency(str(model_root)).records]


def _connector_rules(model_root: Path) -> list[str]:
    return [record.rule for record in check_connector_consistency(str(model_root)).records]


def _records(model_root: Path, rule: str) -> list[validator.ConsistencyIssue]:
    return [record for record in check_composition_consistency(str(model_root)).records if record.rule == rule]


class TestTheFixtureIsCleanBeforeAnythingIsBroken:
    """The negative control: "this rule and nothing else" needs an unperturbed model that reports nothing."""

    def test_the_composition_check_reports_nothing(self, model_root: Path) -> None:
        result = check_composition_consistency(str(model_root))
        assert result.consistent, result.issues

    def test_the_connector_check_reports_nothing(self, model_root: Path) -> None:
        result = check_connector_consistency(str(model_root))
        assert result.consistent, result.issues

    def test_the_composed_gate_reports_nothing(self, model_root: Path) -> None:
        result = check_cross_layer_consistency(str(model_root))
        assert result.consistent, result.issues


# ---------------------------------------------------------------------------
# The layer's own shape: the root, the duplicated identities
# ---------------------------------------------------------------------------


class TestTheRootCardinality:
    """Exactly one root composition, both ways."""

    def test_no_root_is_reported_when_the_layer_declares_none(self, model_root: Path) -> None:
        _edit(model_root, "compositions/Root.yaml", root=False)

        assert _rules(model_root) == ["composition.no-root"]

    def test_no_root_names_the_layer_because_there_is_no_element_to_point_at(self, model_root: Path) -> None:
        _edit(model_root, "compositions/Root.yaml", root=False)

        (record,) = _records(model_root, "composition.no-root")
        assert record.subjects == (Subject("compositions"),)

    def test_no_root_is_reported_for_a_layer_that_declares_nothing(self, tmp_path: Path) -> None:
        """The mandatory layer, absent."""
        (tmp_path / "swc").mkdir()

        assert _rules(tmp_path) == ["composition.no-root"]

    def test_two_roots_are_reported_with_both_named(self, model_root: Path) -> None:
        _edit(model_root, "compositions/Sub.yaml", root=True)

        (record,) = _records(model_root, "composition.multiple-roots")
        assert _rules(model_root) == ["composition.multiple-roots"]
        assert record.subjects == (
            Subject("compositions", "Root"),
            Subject("compositions", "Sub"),
        )
        assert "Root" in record.message and "Sub" in record.message


class TestTheDuplicatedIdentities:
    def test_one_name_declared_in_two_files(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["name"] = "Sub"
        _write(model_root / "compositions" / "Duplicate.yaml", document)

        reported = _rules(model_root)
        assert "composition.duplicate-name" in reported
        (record,) = _records(model_root, "composition.duplicate-name")
        assert record.subjects == (Subject("compositions", "Sub"),)

    def test_one_composition_declaring_a_member_name_twice(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "Alpha", "type": "Gamma"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.duplicate-member")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("members"), member_step("Alpha")),
            ),
        )

    def test_one_composition_declaring_a_delegation_name_twice(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"].append(
            {"name": "Sub_pSignalOut_Alpha", "port": "rTrigger", "member": "Gamma", "member_port": "rTrig"}
        )
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.duplicate-delegation")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("delegations"), member_step("Sub_pSignalOut_Alpha")),
            ),
        )

    def test_one_composition_wiring_one_triple_under_two_delegation_names(self, model_root: Path) -> None:
        """The delegation counterpart of ``rule:connector.duplicate-link``.

        Two delegations differing only in ``name`` wire one ``(port, member, member_port)``
        triple; the name rule stays silent, so this case exercises the link rule alone.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"].append(
            {
                "name": "Sub_pSignalOut_Alpha_Again",
                "port": "pSignalOut",
                "member": "Alpha",
                "member_port": "pOut",
            }
        )
        _write(model_root / "compositions" / "Sub.yaml", document)

        reported = _rules(model_root)
        assert "composition.duplicate-delegation" not in reported
        (record,) = _records(model_root, "composition.duplicate-delegation-link")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("delegations"), member_step("Sub_pSignalOut_Alpha_Again")),
            ),
        )


# ---------------------------------------------------------------------------
# The members: the two type fields, their resolution, one placement per type
# ---------------------------------------------------------------------------


class TestTheMemberTypeResolves:
    def test_a_member_naming_no_declared_component(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"][1]["type"] = "Absent"
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.member-unknown-type")
        assert record.target == "Absent"
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("members"),
                    member_step("Gamma"),
                    field_step("type"),
                ),
            ),
        )

    def test_a_member_naming_no_declared_composition(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Root.yaml")
        document["members"][0]["composition"] = "Absent"
        _write(model_root / "compositions" / "Root.yaml", document)

        reported = _records(model_root, "composition.member-unknown-composition")
        assert [record.target for record in reported] == ["Absent"]
        assert reported[0].subjects == (
            Subject(
                "compositions",
                "Root",
                (
                    field_step("members"),
                    member_step("Sub"),
                    field_step("composition"),
                ),
            ),
        )

    def test_one_type_placed_twice_in_one_composition_is_refused(self, model_root: Path) -> None:
        """One type placed twice in one composition: a connector endpoint names a type, so this is ambiguous.

        The same type in several compositions stays legal (the next case).
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "SecondGamma", "type": "Gamma"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.member-type-repeated")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("members"), member_step("Gamma")),
            ),
            Subject(
                "compositions",
                "Sub",
                (field_step("members"), member_step("SecondGamma")),
            ),
        )

    def test_one_type_placed_in_two_compositions_stays_legal(self, model_root: Path) -> None:
        """The reserve the ``{name, type}`` pair exists for, asserted as a pass."""
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["members"].append({"name": "AlsoGamma", "type": "Gamma"})
        _write(model_root / "compositions" / "Sink.yaml", document)

        assert "composition.member-type-repeated" not in _rules(model_root)

    def test_neither_type_field_is_refused_by_the_LOADER_and_by_no_rule_here(self, model_root: Path) -> None:
        """A member with neither type field is refused by the loader, and no cross-element rule exists for it.

        Every path into this check (``check-model``, ``generate *``, the protocol's pass D) reads the
        layer through ``CompositionMember``'s ``@model_validator``, so
        ``rule:compositions.composition_member.type-form`` is the whole answer.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"][0].pop("type")
        _write(model_root / "compositions" / "Sub.yaml", document)

        with pytest.raises(ValueError, match="exactly one of 'type' and 'composition'"):
            check_composition_consistency(str(model_root))

    def test_both_type_fields_are_refused_by_the_loader_too(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"][0]["composition"] = "Sink"
        _write(model_root / "compositions" / "Sub.yaml", document)

        with pytest.raises(ValueError, match="exactly one of 'type' and 'composition'"):
            check_composition_consistency(str(model_root))

    def test_the_accessor_is_total_like_the_writers_twin(self) -> None:
        """``_member_type_field`` answers a pair for both shapes and never ``None``."""
        assert _member_type_field(CompositionMember(name="m", type="A")) == ("type", "A")
        assert _member_type_field(CompositionMember(name="m", composition="R")) == ("composition", "R")


class TestTheHierarchyIsAcyclic:
    def test_a_cycle_at_depth_three_is_reported_with_every_composition_on_it(self, model_root: Path) -> None:
        """``Root`` → ``Sub`` → ``Sink`` → ``Root``.

        Depth three rather than a self-loop, because a one-element cycle is
        reachable by a colouring that never pushes a path at all.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "Down", "composition": "Sink"})
        _write(model_root / "compositions" / "Sub.yaml", document)
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["members"].append({"name": "Up", "composition": "Root"})
        _write(model_root / "compositions" / "Sink.yaml", document)

        (record,) = _records(model_root, "composition.cycle")
        assert {subject.element for subject in record.subjects} == {"Root", "Sub", "Sink"}
        assert {subject.layer for subject in record.subjects} == {"compositions"}
        assert "Root" in record.message

    def test_a_composition_placed_inside_itself_is_a_cycle(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["members"].append({"name": "Self", "composition": "Sink"})
        _write(model_root / "compositions" / "Sink.yaml", document)

        (record,) = _records(model_root, "composition.cycle")
        assert {subject.element for subject in record.subjects} == {"Sink"}


# ---------------------------------------------------------------------------
# The delegations
# ---------------------------------------------------------------------------


class TestTheDelegations:
    def test_a_delegation_naming_no_member_of_its_composition(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["delegations"][0]["member"] = "Absent"
        _write(model_root / "compositions" / "Sink.yaml", document)

        (record,) = _records(model_root, "composition.delegation-unknown-member")
        assert record.target == "Absent"
        assert record.subjects == (
            Subject(
                "compositions",
                "Sink",
                (
                    field_step("delegations"),
                    member_step("Sink_rSignalIn_Consumer"),
                    field_step("member"),
                ),
            ),
        )

    def test_a_delegation_naming_an_undeclared_boundary_port(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["delegations"][0]["port"] = "absent"
        _write(model_root / "compositions" / "Sink.yaml", document)

        (record,) = _records(model_root, "composition.delegation-unknown-port")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sink",
                (
                    field_step("delegations"),
                    member_step("Sink_rSignalIn_Consumer"),
                    field_step("port"),
                ),
            ),
        )

    def test_a_delegation_naming_a_port_the_members_type_does_not_declare(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["delegations"][0]["member_port"] = "absent"
        _write(model_root / "compositions" / "Sink.yaml", document)

        (record,) = _records(model_root, "composition.delegation-unknown-port")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sink",
                (
                    field_step("delegations"),
                    member_step("Sink_rSignalIn_Consumer"),
                    field_step("member_port"),
                ),
            ),
        )

    def test_two_ports_of_disagreeing_direction(self, model_root: Path) -> None:
        """``Sub``'s provide-side delegation, re-pointed at a require port.

        A delegation joins one direction on both sides; the writer picks the
        ``INNER-PORT-IREF`` variant from them.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"][0]["member"] = "Gamma"
        document["delegations"][0]["member_port"] = "rTrig"
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.delegation-port-direction-mismatch")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("delegations"),
                    member_step("Sub_pSignalOut_Alpha"),
                    field_step("port"),
                ),
            ),
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("delegations"),
                    member_step("Sub_pSignalOut_Alpha"),
                    field_step("member_port"),
                ),
            ),
        )

    def test_two_ports_of_disagreeing_interface(self, model_root: Path) -> None:
        """A delegation joining ports on incompatible interfaces, which the XSD cannot express.

        ``Sub``'s provide-side delegation, re-pointed at ``Beta.pInner`` — also a
        provide port, so the direction rule abstains, and on ``ITrigger`` where
        the boundary port ``pSignalOut`` is on ``ISignal``. AUTOSAR requires the
        inner and the outer port of a ``DELEGATION-SW-CONNECTOR`` to reference
        compatible port interfaces; the licensed schema accepts the mismatch at
        4.2.2 **and** 4.3.0.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"][0]["member_port"] = "pInner"
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.delegation-interface-mismatch")
        assert "ISignal" in record.message and "ITrigger" in record.message
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("delegations"),
                    member_step("Sub_pSignalOut_Alpha"),
                    field_step("port"),
                ),
            ),
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("delegations"),
                    member_step("Sub_pSignalOut_Alpha"),
                    field_step("member_port"),
                ),
            ),
        )

    def test_a_direction_disagreement_is_not_also_an_interface_finding(self, model_root: Path) -> None:
        """A direction disagreement is reported alone: one root cause, one issue.

        ``Gamma.rTrig`` is on ``ITrigger`` and faces the other way, so both rules
        could fire; the direction's repair decides the interface question too.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"][0]["member"] = "Gamma"
        document["delegations"][0]["member_port"] = "rTrig"
        _write(model_root / "compositions" / "Sub.yaml", document)

        assert _records(model_root, "composition.delegation-interface-mismatch") == []
        assert len(_records(model_root, "composition.delegation-port-direction-mismatch")) == 1

    def test_the_clean_fixture_reports_no_interface_mismatch(self, model_root: Path) -> None:
        """Three delegations that all agree stay silent."""
        assert _records(model_root, "composition.delegation-interface-mismatch") == []


# ---------------------------------------------------------------------------
# The delegation rule judges COMPATIBILITY, by two clauses
# ---------------------------------------------------------------------------
#
# `rule:composition.delegation-interface-mismatch` asks
# `validator.interface_compatibility`, and which of the predicate's two
# computations it asks for is decided by the OUTER port's direction — the two
# ports face the same way here, because the direction rule owns the case where
# they do not:
#
# * a `require` delegation is `[constr_1070]`/`[constr_1080]` 1(a), the SUBSET —
#   every member of the INNER port's interface has a compatible counterpart of
#   the same name in the OUTER port's;
# * a `provide` delegation is clause 2(a), EXISTENTIAL — AT LEAST ONE member of
#   the inner port's interface has such a counterpart in the outer's. One shared
#   member is the standard's whole condition there; 2(a) reads the same in
#   R4.2.2, R4.3.0 and R22-11.
#
# The INNER side is the required one in both clauses, which is what lets the two
# cases below discriminate the contexts rather than merely exercise them: an
# inner interface of two elements delegated onto an outer of one of them is a
# finding under `subset` and clean under `shared-member`, and the pair is
# asserted both ways.


def _interface(model_root: Path, name: str, *elements: str, type_name: str = "uint8") -> None:
    """Declare one sender-receiver interface of *elements*, all of one type.

    ``uint8`` — the fixture's one value type — is the default, so such a case differs in member
    NAMES alone; a case whose subject is the members' **types** names a second one.
    """
    _write(
        model_root / "interfaces" / f"{name}.yaml",
        {
            "name": name,
            "kind": "sender_receiver",
            "data_elements": [{"name": element, "implementation_type": type_name} for element in elements],
        },
    )


def _second_value_type(model_root: Path, name: str, base: str, size: int) -> None:
    """A value type over a base type of its own, so two element types can differ after resolution."""
    write_model_element(
        model_root,
        {"name": base, "kind": "base_type", "size": size, "encoding": "unsigned", "native_declaration": f"uint{size}"},
    )
    write_model_element(model_root, {"name": name, "kind": "value_type", "base": base})


def _delegation_subjects(composition: str, delegation: str) -> tuple[Subject, ...]:
    """The rule's two subjects: the delegation's two port fields, since either is a legal repair."""
    return tuple(
        Subject(
            "compositions",
            composition,
            (field_step("delegations"), member_step(delegation), field_step(side)),
        )
        for side in ("port", "member_port")
    )


class TestADelegationTheStandardJoins:
    """Two ports facing one way, naming two interfaces, and joined by the standard."""

    def test_a_require_delegation_whose_inner_interface_is_a_subset(self, model_root: Path) -> None:
        """1(a): the inner asks for ``trigger``, the outer offers ``trigger`` and ``extra``.

        The composed gate, not just this rule: a condition that silenced one rule
        by breaking another would pass a narrower assertion.
        """
        _interface(model_root, "ITriggerFull", "trigger", "extra")
        _set_member(model_root, "compositions/Sub.yaml", "ports", 0, interface="ITriggerFull")

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues

    def test_a_provide_delegation_sharing_one_member(self, model_root: Path) -> None:
        """2(a): the inner offers ``value`` and ``extra``, the outer carries ``value`` — enough.

        Under ``subset`` this same model is a finding (the case below), so the
        silence here is the existential clause being chosen and not the predicate
        being lenient.
        """
        _interface(model_root, "ISignalPlus", "value", "extra")
        _set_member(model_root, "swc/core/Beta.yaml", "ports", 0, interface="ISignalPlus")

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues

    def test_two_element_types_that_differ_only_in_name_are_joined(self, model_root: Path) -> None:
        """The type half of the judgement, positively: one structure under two names.

        ``byte`` rests on ``uint8_base`` like ``uint8``, so the interfaces differ in a
        declared type **name** alone, which the standard says takes no part.
        """
        write_model_element(model_root, {"name": "byte", "kind": "value_type", "base": "uint8_base"})
        _interface(model_root, "ITriggerByte", "trigger", type_name="byte")
        _set_member(model_root, "swc/core/Gamma.yaml", "ports", 1, interface="ITriggerByte")

        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent, result.issues

    def test_the_two_clauses_are_chosen_by_the_outer_ports_direction(self, model_root: Path) -> None:
        """One shape, two directions, two verdicts: the require delegation is a finding, the provide one not.

        A branch that asked for ``subset`` everywhere, or for ``shared-member``
        everywhere, fails exactly one half of this.
        """
        _interface(model_root, "ITriggerFull", "trigger", "extra")
        _interface(model_root, "ISignalPlus", "value", "extra")
        _set_member(model_root, "swc/core/Gamma.yaml", "ports", 1, interface="ITriggerFull")
        _set_member(model_root, "swc/core/Beta.yaml", "ports", 0, interface="ISignalPlus")

        assert _rules(model_root) == ["composition.delegation-interface-mismatch"]
        assert "Sub_rTrigger_Gamma" in _records(model_root, "composition.delegation-interface-mismatch")[0].message


class TestADelegationTheStandardRefuses:
    """One case per shape the subject takes, over the same clean root."""

    def test_a_require_delegation_whose_inner_interface_exceeds_the_outer(self, model_root: Path) -> None:
        """1(a) unsatisfied: the inner asks for ``extra`` and the outer has not got it."""
        _interface(model_root, "ITriggerFull", "trigger", "extra")
        _set_member(model_root, "swc/core/Gamma.yaml", "ports", 1, interface="ITriggerFull")

        (record,) = _records(model_root, "composition.delegation-interface-mismatch")

        assert record.message == (
            "Delegation 'Sub_rTrigger_Gamma' of composition 'Sub' joins 'rTrigger' (ITrigger) to "
            "'Gamma.rTrig' (ITriggerFull): data element 'extra' has no counterpart in 'ITrigger'."
        )
        assert record.subjects == _delegation_subjects("Sub", "Sub_rTrigger_Gamma")
        assert _rules(model_root) == ["composition.delegation-interface-mismatch"]

    def test_a_require_delegation_whose_shared_member_resolves_to_another_structure(self, model_root: Path) -> None:
        """The type half of the judgement, negatively: one member name, two structures.

        A judgement that had lost its type half (a data type map that never
        arrived) would call this pair compatible.
        """
        _second_value_type(model_root, "uint16", "uint16_base", 16)
        _interface(model_root, "ITriggerWide", "trigger", type_name="uint16")
        _set_member(model_root, "swc/core/Gamma.yaml", "ports", 1, interface="ITriggerWide")

        (record,) = _records(model_root, "composition.delegation-interface-mismatch")

        assert record.message == (
            "Delegation 'Sub_rTrigger_Gamma' of composition 'Sub' joins 'rTrigger' (ITrigger) to "
            "'Gamma.rTrig' (ITriggerWide): data element 'trigger': type 'uint16' vs 'uint8' — "
            "base type size 16 vs 8."
        )
        assert _rules(model_root) == ["composition.delegation-interface-mismatch"]

    def test_a_provide_delegation_sharing_no_member(self, model_root: Path) -> None:
        """2(a) unsatisfied, and the shape the emission table perturbs.

        ``Sub``'s ``pSignalOut`` is on ``ISignal`` and ``Beta.pInner`` on
        ``ITrigger``; both are provide ports, so the direction rule abstains and
        not one element name is shared.
        """
        _set_member(model_root, "compositions/Sub.yaml", "delegations", 0, member_port="pInner")

        (record,) = _records(model_root, "composition.delegation-interface-mismatch")

        assert record.message == (
            "Delegation 'Sub_pSignalOut_Alpha' of composition 'Sub' joins 'pSignalOut' (ISignal) to "
            "'Alpha.pInner' (ITrigger): no data element of 'ITrigger' has a counterpart in 'ISignal'."
        )
        assert record.subjects == _delegation_subjects("Sub", "Sub_pSignalOut_Alpha")

    def test_a_direction_disagreement_still_wins_with_one_finding(self, model_root: Path) -> None:
        """A pair that disagrees in direction AND interface is reported once, as the direction.

        Only ``Sub``'s FIRST delegation moves; the second keeps naming one interface
        on both sides, which makes the single-item rule list the right oracle.
        """
        _set_member(model_root, "compositions/Sub.yaml", "delegations", 0, member="Gamma", member_port="rTrig")

        assert _rules(model_root) == ["composition.delegation-port-direction-mismatch"]


class TestTheDelegationJudgementReadsEachLayerAtMostOnce:
    """``check_connector_consistency``'s read discipline, at the other caller.

    The predicate reads no layer, so this pass reads ``interfaces/`` and
    ``data_types/`` at most once each per run, and not at all for a model whose
    delegations name one interface per pair.
    """

    def test_a_model_whose_delegations_all_agree_reads_neither_layer(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interfaces = _counting_layer_reader(monkeypatch, "read_interfaces")
        data_types = _counting_layer_reader(monkeypatch, "read_data_description")

        assert check_composition_consistency(str(model_root)).consistent
        assert (interfaces, data_types) == ([], [])

    def test_two_disagreeing_delegations_read_each_layer_once(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two, so that "once" is a count and not the only opportunity to read."""
        _interface(model_root, "ITriggerFull", "trigger", "extra")
        _interface(model_root, "ISignalPlus", "value", "extra")
        _set_member(model_root, "compositions/Sub.yaml", "ports", 0, interface="ITriggerFull")
        _set_member(model_root, "swc/core/Beta.yaml", "ports", 0, interface="ISignalPlus")
        interfaces = _counting_layer_reader(monkeypatch, "read_interfaces")
        data_types = _counting_layer_reader(monkeypatch, "read_data_description")

        assert check_composition_consistency(str(model_root)).consistent
        assert (len(interfaces), len(data_types)) == (1, 1), (interfaces, data_types)


# ---------------------------------------------------------------------------
# The connectors: their composition, and the endpoint that must be a member
# ---------------------------------------------------------------------------


class TestTheConnectorsComposition:
    def test_a_connector_naming_no_declared_composition(self, model_root: Path) -> None:
        _edit(model_root, "connectors/sub_link.yaml", composition="Absent")

        records = [
            record
            for record in check_connector_consistency(str(model_root)).records
            if record.rule == "connector.unknown-composition"
        ]
        assert [record.target for record in records] == ["Absent"]
        assert records[0].subjects == (Subject("connectors", "sub_link", (field_step("composition"),)),)

    def test_an_endpoint_whose_type_is_not_a_member_of_the_connectors_composition(self, model_root: Path) -> None:
        """The endpoint's type resolves, but is placed in another composition.

        ``Consumer`` is a member of ``Sink``; the connector is drawn inside ``Sub``.
        """
        _edit(
            model_root,
            "connectors/sub_link.yaml",
            **{"to": {"swc": "Consumer", "port": "rIn"}},
        )

        (record,) = _records(model_root, "composition.connector-endpoint-not-a-member")
        assert record.subjects == (Subject("connectors", "sub_link", (field_step("to"),)),)
        assert "Sub" in record.message and "Consumer" in record.message

    def test_a_composition_less_connector_is_resolved_against_the_root(self, model_root: Path) -> None:
        """A connector with no ``composition`` belongs to the root, in the rules as in the writer.

        ``Beta`` is a member of ``Sub`` and not of the root, so both endpoints become non-members.
        """
        document = _load(model_root / "connectors" / "sub_link.yaml")
        del document["composition"]
        _write(model_root / "connectors" / "sub_link.yaml", document)

        records = _records(model_root, "composition.connector-endpoint-not-a-member")
        assert [record.subjects[0].element for record in records] == ["sub_link", "sub_link"]
        assert all("Root" in record.message for record in records)

    def test_an_endpoints_two_type_fields_are_the_LOADERs_rule_and_no_rule_here(self, model_root: Path) -> None:
        """An endpoint with both type fields is refused by the loader, and no cross-element rule exists for it.

        ``rule:connectors.connector_endpoint.type-form`` is the whole answer.
        """
        document = _load(model_root / "connectors" / "sub_link.yaml")
        document["from"]["composition"] = "Sub"
        _write(model_root / "connectors" / "sub_link.yaml", document)

        with pytest.raises(ValueError, match="exactly one of 'swc' and 'composition'"):
            check_connector_consistency(str(model_root))

    def test_the_endpoint_accessor_is_total_too(self) -> None:
        assert endpoint_type_field(ConnectorEndpoint(swc="A", port="p")) == ("swc", "A")
        assert endpoint_type_field(ConnectorEndpoint(composition="R", port="p")) == ("composition", "R")


# ---------------------------------------------------------------------------
# The external stub
# ---------------------------------------------------------------------------


class TestTheExternalStub:
    """A stub is a reference the exporter writes, never a component it emits.

    It may declare ports by name and direction, but a port **with an interface**
    is this rule's finding; the ports it may carry are ``test_stub_ports_validator.py``'s.
    """

    def test_a_stub_with_a_port_is_reported(self, model_root: Path) -> None:
        _edit(
            model_root,
            "swc/external/ExternalStub.yaml",
            ports=[{"name": "pOut", "direction": "provide", "interface": "ISignal"}],
        )

        (record,) = _records(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("ports"),)),)
        assert "ExternalStub" in record.message

    def test_a_stub_with_a_runnable_is_reported(self, model_root: Path) -> None:
        _edit(model_root, "swc/external/ExternalStub.yaml", runnables=[{"name": "Run"}])

        (record,) = _records(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("runnables"),)),)

    def test_an_ordinary_component_with_ports_is_not_reported(self, model_root: Path) -> None:
        assert "swc.external-with-ports" not in _rules(model_root)


# ---------------------------------------------------------------------------
# Shapes the writer refuses and the gate must report too
# ---------------------------------------------------------------------------


def _name_rules(model_root: Path) -> list[str]:
    """Every rule ``check_name_uniqueness`` reports, in order."""
    return [record.rule for record in validator.check_name_uniqueness(str(model_root)).records]


def _interface_reference_rules(model_root: Path) -> list[str]:
    return [record.rule for record in validator.check_port_interface_references(str(model_root)).records]


class TestACompositionMayNotClaimAComponentsShortName:
    """Components and compositions are one ``/ComponentTypes`` ``SHORT-NAME`` namespace.

    An ``external: true`` stub emits no element for the package scope to collide
    with, so the gate is the only check that sees a composition named after one.
    Asserted as a pair per shape.
    """

    def test_a_composition_named_after_a_component(self, model_root: Path, tmp_path: Path) -> None:
        _edit(model_root, "compositions/Sink.yaml", name="Gamma")

        assert "name.duplicate-component-type" in _name_rules(model_root)
        (record,) = [
            record
            for record in validator.check_name_uniqueness(str(model_root)).records
            if record.rule == "name.duplicate-component-type"
        ]
        assert record.subjects == (Subject("compositions", "Gamma"),)
        assert "Colliding /ComponentTypes SHORT-NAME" in _generation_refusal(model_root, tmp_path)

    def test_a_composition_named_after_an_external_stub(self, model_root: Path, tmp_path: Path) -> None:
        """The half the ``ArxmlPackage`` scope could not see."""
        _edit(model_root, "compositions/Sink.yaml", name="ExternalStub")

        assert "name.duplicate-component-type" in _name_rules(model_root)
        assert "Colliding /ComponentTypes SHORT-NAME" in _generation_refusal(model_root, tmp_path)

    def test_a_composition_named_after_a_generated_implementation(self, model_root: Path, tmp_path: Path) -> None:
        _edit(model_root, "compositions/Sink.yaml", name="Gamma_Implementation")

        assert "name.composition-implementation-collision" in _name_rules(model_root)
        assert "Colliding /ComponentTypes SHORT-NAME" in _generation_refusal(model_root, tmp_path)

    def test_a_composition_named_after_a_stubs_implementation_is_a_free_name(self, model_root: Path) -> None:
        """Nothing emits it, so reserving it would refuse a name generation accepts."""
        _edit(model_root, "compositions/Sink.yaml", name="ExternalStub_Implementation")

        assert _name_rules(model_root) == []

    def test_the_unperturbed_fixture_reports_neither(self, model_root: Path) -> None:
        assert _name_rules(model_root) == []


class TestTheCompositionsOwnSiblingScopes:
    """A composition's ``PORTS`` scope, and its ``CONNECTORS`` scope across delegations and connectors.

    ``CONNECTORS`` holds the delegations *and* the assembly connectors drawn at that level.
    """

    def test_two_boundary_ports_of_one_name(self, model_root: Path, tmp_path: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["ports"].append({"name": "rTrigger", "direction": "require", "interface": "ITrigger"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.duplicate-port")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("ports"), member_step("rTrigger")),
            ),
        )
        assert "rTrigger" in _generation_refusal(model_root, tmp_path)

    def test_a_delegation_named_after_a_connector_drawn_inside_the_same_composition(
        self, model_root: Path, tmp_path: Path
    ) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["delegations"][0]["name"] = "sub_link"
        _write(model_root / "compositions" / "Sub.yaml", document)

        (record,) = _records(model_root, "composition.delegation-connector-name-collision")
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (field_step("delegations"), member_step("sub_link")),
            ),
        )
        assert "sub_link" in _generation_refusal(model_root, tmp_path)

    def test_a_delegation_named_after_a_connector_of_another_composition_is_legal(self, model_root: Path) -> None:
        """The scope is one composition's CONNECTORS block, not the connector layer."""
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["delegations"][0]["name"] = "sub_link"
        _write(model_root / "compositions" / "Sink.yaml", document)

        assert "composition.delegation-connector-name-collision" not in _rules(model_root)


class TestABoundaryPortsInterfaceReferenceIsChecked:
    """``field:compositions.composition_port.interface`` is checked even on an unconnected boundary port."""

    def test_a_boundary_port_naming_no_declared_interface(self, model_root: Path, tmp_path: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["ports"][0]["interface"] = "NoSuchIface"
        _write(model_root / "compositions" / "Sub.yaml", document)

        assert "composition.port-undeclared-interface" in _interface_reference_rules(model_root)
        (record,) = [
            record
            for record in validator.check_port_interface_references(str(model_root)).records
            if record.rule == "composition.port-undeclared-interface"
        ]
        assert record.target == "NoSuchIface"
        assert record.subjects == (
            Subject(
                "compositions",
                "Sub",
                (
                    field_step("ports"),
                    member_step("rTrigger"),
                    field_step("interface"),
                ),
            ),
        )
        assert "NoSuchIface" in _generation_refusal(model_root, tmp_path)

    def test_the_unperturbed_fixture_reports_nothing(self, model_root: Path) -> None:
        assert _interface_reference_rules(model_root) == []


class TestTheExternalStubsReferencePair:
    """A stub's ``external_ref``/``external_dest`` pair, checked by the gate.

    A malformed ``DEST`` is emitted verbatim and only a licensed XSD rejects it, so
    on a host with no schema ``swcraft validate`` would ship it.
    """

    def test_a_blank_reference(self, model_root: Path, tmp_path: Path) -> None:
        _edit(model_root, "swc/external/ExternalStub.yaml", external_ref="")

        (record,) = _records(model_root, "swc.external-incomplete-reference")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("external_ref"),)),)
        assert _generation_refusal(model_root, tmp_path) != ""

    def test_a_blank_dest(self, model_root: Path, tmp_path: Path) -> None:
        _edit(model_root, "swc/external/ExternalStub.yaml", external_dest="")

        (record,) = _records(model_root, "swc.external-incomplete-reference")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("external_dest"),)),)
        assert _generation_refusal(model_root, tmp_path) != ""

    def test_a_dest_that_is_not_one(self, model_root: Path) -> None:
        """The shape a licensed XSD catches and the structural fallback ships."""
        _edit(model_root, "swc/external/ExternalStub.yaml", external_dest="NOT A DEST <&>")

        (record,) = _records(model_root, "swc.external-incomplete-reference")
        assert record.target == "NOT A DEST <&>"

    def test_a_dest_this_exporter_never_generates_is_still_accepted(self, model_root: Path) -> None:
        """The check is the token GRAMMAR and deliberately not the enumeration.

        A tool export may carry ``DEST`` values SWCraft never emits; pinning the
        ones it does emit would refuse a correct import.
        """
        _edit(model_root, "swc/external/ExternalStub.yaml", external_dest="SENSOR-ACTUATOR-SW-COMPONENT-TYPE")

        assert "swc.external-incomplete-reference" not in _rules(model_root)

    @pytest.mark.parametrize(
        "ref",
        ["/ComponentTypes/Gamma", "/ComponentTypes/NothingDeclares", "/DataTypes/X", "componenttypes/Gamma"],
    )
    def test_a_reference_into_a_generated_package(self, model_root: Path, tmp_path: Path, ref: str) -> None:
        """A path inside a package the writer declares this project's own elements in.

        A rule of its own rather than a wider ``swc.external-incomplete-reference``,
        which shipped: refusing a model it accepted would be a major. Every
        prototype of the stub would bind to the declared element; a name nothing
        declares yet binds the day one is declared. Refused by both gates, the
        writer from an output directory that does not exist.
        """
        _edit(model_root, "swc/external/ExternalStub.yaml", external_ref=ref)

        assert _rules(model_root) == ["swc.external-reference-inside-generated-package"]
        (record,) = _records(model_root, "swc.external-reference-inside-generated-package")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("external_ref"),)),)
        assert record.target == ref
        assert "a package the generated ARXML declares this project's own elements in" in record.message
        assert record.message.endswith("rather than to the source's element.")
        output_dir = tmp_path / "fresh-output"
        with pytest.raises(ValueError, match="ExternalStub") as caught:
            generate_arxml(str(model_root), str(output_dir / "model.arxml"))
        assert f"'{ref}'" in str(caught.value)
        assert "rather than to the source's element" in str(caught.value)
        assert not output_dir.exists()

    def test_a_lookalike_package_is_accepted(self, model_root: Path) -> None:
        _edit(model_root, "swc/external/ExternalStub.yaml", external_ref="/Other/ComponentTypes/Gamma")

        assert _rules(model_root) == []

    def test_the_pair_on_a_component_that_is_not_a_stub(self, model_root: Path) -> None:
        _edit(model_root, "swc/core/Gamma.yaml", external_ref="/Foreign/Pkg/Gamma")

        (record,) = _records(model_root, "swc.external-reference-without-stub")
        assert record.subjects == (Subject("swc", "Gamma", (field_step("external_ref"),)),)

    def test_a_stub_charging_a_memory_budget_is_reported(self, model_root: Path) -> None:
        """Budgets ignore stubs, so a budget on one is a declaration nothing reads."""
        _edit(model_root, "swc/external/ExternalStub.yaml", memory={"RAM0": 128})

        (record,) = _records(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("memory"),)),)

    def test_a_stub_naming_a_declared_mapping_set_is_reported(self, model_root: Path) -> None:
        """A stub emits no internal behaviour, so a mapping set it names is never written."""
        write_model_element(model_root, {"name": "Maps", "maps": []}, layer="mapping_sets")
        _edit(model_root, "swc/external/ExternalStub.yaml", data_type_mappings=["Maps"])

        (record,) = _records(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("data_type_mappings"),)),)
        assert "data_type_mappings" in record.message
        whole = [record.rule for record in validator.check_cross_layer_consistency(str(model_root)).records]
        assert "swc.unknown-mapping-set" not in whole

    def test_a_stub_naming_an_undeclared_mapping_set_is_reported_by_both_rules(self, model_root: Path) -> None:
        """``swc.unknown-mapping-set`` does not abstain on a stub; one repair (removing the entry) clears both."""
        _edit(model_root, "swc/external/ExternalStub.yaml", data_type_mappings=["Absent"])

        whole = [record.rule for record in validator.check_cross_layer_consistency(str(model_root)).records]
        assert whole.count("swc.external-with-ports") == 1
        assert whole.count("swc.unknown-mapping-set") == 1


# ---------------------------------------------------------------------------
# The identifier scope covers the layer's four name sites
# ---------------------------------------------------------------------------


class TestTheIdentifierScopeCoversTheCompositionLayer:
    """The four ``SHORT-NAME``s one composition file emits, with each one's message and subject.

    ``test_identifier_coverage.py`` derives the coverage from ``schema.py``; the
    messages and subjects are what it does not reach.
    """

    def test_a_composition_name(self, model_root: Path) -> None:
        _edit(model_root, "compositions/Sink.yaml", name="My Sink")

        issues = check_identifier_conformance(str(model_root)).issues
        assert len(issues) == 1, issues
        assert "Composition 'My Sink'" in issues[0]
        assert " in the compositions layer " in issues[0] and "Sink.yaml" not in issues[0]

    def test_a_boundary_port_name(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["ports"][0]["name"] = "r Signal In"
        _write(model_root / "compositions" / "Sink.yaml", document)

        issues = check_identifier_conformance(str(model_root)).issues
        assert len(issues) == 1, issues
        assert "Composition 'Sink' port 'r Signal In'" in issues[0]

    def test_a_member_name(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["members"][0]["name"] = "9Consumer"
        _write(model_root / "compositions" / "Sink.yaml", document)

        issues = check_identifier_conformance(str(model_root)).issues
        assert len(issues) == 1, issues
        assert "Composition 'Sink' member '9Consumer'" in issues[0]

    def test_an_over_length_delegation_name(self, model_root: Path) -> None:
        """The length limb, on the delegation: three names concatenated overrun it easiest."""
        document = _load(model_root / "compositions" / "Sink.yaml")
        document["delegations"][0]["name"] = "D" + "x" * 128
        _write(model_root / "compositions" / "Sink.yaml", document)

        issues = check_identifier_conformance(str(model_root)).issues
        assert len(issues) == 1, issues
        assert "delegation" in issues[0]
        assert "must be at most 128 characters" in issues[0]

    def test_a_conforming_layer_reports_nothing(self, model_root: Path) -> None:
        assert check_identifier_conformance(str(model_root)).issues == []

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    def test_an_over_length_name_is_reported_at_every_site(self, site: CompositionNameSite, model_root: Path) -> None:
        """The **length** limb, at all four sites; the accepted-at-128 twin follows."""
        path = model_root / "compositions" / "Sink.yaml"
        _write(path, site.plant(_load(path), "N" * 129))

        issues = check_identifier_conformance(str(model_root)).issues

        assert len(issues) == 1, issues
        assert site.report_word in issues[0], issues[0]
        assert "must be at most 128 characters" in issues[0]

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    def test_a_name_at_the_limit_is_reported_at_no_site(self, site: CompositionNameSite, model_root: Path) -> None:
        """128 is legal, so the limb above is a limit and not a blanket refusal.

        What this does **not** reach: the *cross-file* references the rename dangles
        (the root's member naming ``Sink``, the connector naming its boundary port);
        ``check_identifier_conformance`` resolves nothing, so they cannot mask this one.
        """
        path = model_root / "compositions" / "Sink.yaml"
        _write(path, site.plant(_load(path), "N" * 128))

        assert check_identifier_conformance(str(model_root)).issues == []


class TestTheWritePathHalfOfTheIdentifierScope:
    """``emitted_identifier_violations`` — the protocol's pre-materialization half.

    The engine asks it *before* the shadow tree exists: a composition's ``name``
    and its three member-name sites.
    """

    def _staged(self, composition: dict[str, Any]) -> StagedModel:
        return StagedModel(registry=reg.current(), elements={"compositions": [composition]})

    def test_a_composition_name_is_refused(self) -> None:
        violations = emitted_identifier_violations(self._staged({"name": "My Root", "title": "T"}))
        assert [violation.error for violation in violations] == ["invalid-identifier"]
        assert violations[0].addresses[0].layer == "compositions"

    @pytest.mark.parametrize(
        ("field", "member"),
        [
            ("ports", {"name": "my port", "direction": "provide", "interface": "ISignal"}),
            ("members", {"name": "my member", "type": "A"}),
            ("delegations", {"name": "my delegation", "port": "p", "member": "m", "member_port": "q"}),
        ],
    )
    def test_each_member_name_is_refused_at_its_own_address(self, field: str, member: dict[str, Any]) -> None:
        """The **whole** address, step for step, as the JSON a client receives — the message and the path.

        A weaker check passed with every site addressed under ``ports``, sending a
        client repairing a delegation to a path that resolves to nothing.
        """
        staged = self._staged({"name": "Root", "title": "T", field: [member]})

        violations = emitted_identifier_violations(staged)

        assert [violation.error for violation in violations] == ["invalid-identifier"]
        member_name = member["name"]
        assert [violation.message for violation in violations] == [
            f"the composition 'Root' {field[:-1]} name '{member_name}' is emitted as an "
            f"AUTOSAR SHORT-NAME and {REASON_PATTERN}"
        ]
        assert [address.to_json() for address in violations[0].addresses] == [
            {
                "layer": "compositions",
                "element": "Root",
                "path": [{"field": field}, {"member": member_name}, {"field": "name"}],
            }
        ]

    def test_a_conforming_composition_is_refused_nothing(self) -> None:
        staged = self._staged(
            {
                "name": "Root",
                "title": "T",
                "ports": [{"name": "pOut", "direction": "provide", "interface": "ISignal"}],
                "members": [{"name": "Alpha", "type": "Beta"}],
                "delegations": [{"name": "D", "port": "pOut", "member": "Alpha", "member_port": "p"}],
            }
        )
        assert emitted_identifier_violations(staged) == ()


class TestTheDuplicateLinkKeyIsTheRESOLVEDComposition:
    """`rule:connector.duplicate-link` is keyed on the RESOLVED composition the link is drawn in.

    `connector.composition` is `None` for a connector drawn inside the root, so a raw
    key would split the implicit and the named root. The key must still be **per
    composition**, or two compositions each wiring their own copy of one type pair
    would be refused as one duplicated link.
    """

    def _second_composition_wiring_the_same_pair(self, model_root: Path) -> None:
        _write(
            model_root / "compositions" / "Second.yaml",
            {
                "name": "Second",
                "title": "A second composition placing the same two types",
                "members": [{"name": "Alpha", "type": "Beta"}, {"name": "Gamma", "type": "Gamma"}],
            },
        )
        _write(
            model_root / "connectors" / "second_link.yaml",
            {
                "name": "second_link",
                "composition": "Second",
                "from": {"swc": "Beta", "port": "pInner"},
                "to": {"swc": "Gamma", "port": "rInner"},
            },
        )

    def test_the_implicit_root_and_the_named_root_are_one_key(self, model_root: Path, tmp_path: Path) -> None:
        """`root_link` names no composition; its twin names `Root` in full."""
        document = _load(model_root / "connectors" / "root_link.yaml")
        _write(
            model_root / "connectors" / "root_link_named.yaml",
            {**document, "name": "root_link_named", "composition": "Root"},
        )

        assert "connector.duplicate-link" in _connector_rules(model_root)
        assert "Duplicate connector link" in _generation_refusal(model_root, tmp_path)

    def test_one_pair_wired_inside_two_compositions_is_not_a_duplicate(self, model_root: Path) -> None:
        """Two compositions each wiring their own copy of one pair are two links; a tool export can be this shape."""
        self._second_composition_wiring_the_same_pair(model_root)

        assert "connector.duplicate-link" not in _connector_rules(model_root)

    def test_the_key_still_separates_two_named_compositions(self, model_root: Path) -> None:
        """And the same pair inside one composition twice IS still a duplicate."""
        self._second_composition_wiring_the_same_pair(model_root)
        _edit(model_root, "connectors/second_link.yaml", composition="Sub")

        assert _connector_rules(model_root).count("connector.duplicate-link") == 1


# ---------------------------------------------------------------------------
# The write-path agreement class
# ---------------------------------------------------------------------------


def _generation_refusal(model_root: Path, tmp_path: Path) -> str:
    """``generate_arxml``'s message, or ``""`` when it does not refuse."""
    try:
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
    except (ValueError, TypeError) as exc:
        return str(exc)
    return ""


class TestTheWriterRefusesWhatCheckModelReports:
    """The agreement class: per shape, the gate reports the rule and the writer refuses the same model."""

    def test_a_duplicate_member_name(self, model_root: Path, tmp_path: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "Alpha", "type": "Gamma"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        assert "composition.duplicate-member" in _rules(model_root)
        assert "Alpha" in _generation_refusal(model_root, tmp_path)

    def test_a_member_type_that_resolves_nowhere(self, model_root: Path, tmp_path: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"][1]["type"] = "Absent"
        _write(model_root / "compositions" / "Sub.yaml", document)

        assert "composition.member-unknown-type" in _rules(model_root)
        assert "Absent" in _generation_refusal(model_root, tmp_path)

    def test_one_type_placed_twice_in_one_composition(self, model_root: Path, tmp_path: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "SecondGamma", "type": "Gamma"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        assert "composition.member-type-repeated" in _rules(model_root)
        refusal = _generation_refusal(model_root, tmp_path)
        assert "Gamma" in refusal and "Sub" in refusal

    def test_a_delegation_whose_two_ports_name_different_interfaces(self, model_root: Path, tmp_path: Path) -> None:
        """The XSD accepts this document at 4.2.2 and at 4.3.0, so the gate and the writer are the whole check.

        AUTOSAR's compatible-interface requirement on a ``DELEGATION-SW-CONNECTOR``
        is not expressible in the schema.
        """
        _set_member(model_root, "compositions/Sub.yaml", "delegations", 0, member_port="pInner")

        assert "composition.delegation-interface-mismatch" in _rules(model_root)
        refusal = _generation_refusal(model_root, tmp_path)
        assert "ISignal" in refusal and "ITrigger" in refusal

    def test_a_delegation_the_standard_joins_is_accepted_by_both(self, model_root: Path, tmp_path: Path) -> None:
        """The pair's **other** direction: a delegation the standard joins is accepted by both.

        ``Gamma.rTrig`` takes an interface of the same one element under another
        name; the inner port is the required side of ``[constr_1070]`` 1(a), so
        the subset holds.
        """
        _interface(model_root, "ITriggerBasic", "trigger")
        _set_member(model_root, "swc/core/Gamma.yaml", "ports", 1, interface="ITriggerBasic")

        assert _rules(model_root) == []
        assert _generation_refusal(model_root, tmp_path) == ""

    def test_an_external_stub_declaring_a_memory_budget(self, model_root: Path, tmp_path: Path) -> None:
        """A stub's memory budget: ``swc.external-with-ports`` reported, generation refused (a stub emits no type)."""
        _edit(model_root, "swc/external/ExternalStub.yaml", memory={"RAM0": 1024})

        assert "swc.external-with-ports" in _rules(model_root)
        assert "ExternalStub" in _generation_refusal(model_root, tmp_path)

    def test_an_external_stub_naming_a_mapping_set(self, model_root: Path, tmp_path: Path) -> None:
        """A stub's mapping set: ``swc.external-with-ports`` reported, generation refused naming the field."""
        write_model_element(model_root, {"name": "Maps", "maps": []}, layer="mapping_sets")
        _edit(model_root, "swc/external/ExternalStub.yaml", data_type_mappings=["Maps"])

        assert "swc.external-with-ports" in _rules(model_root)
        refusal = _generation_refusal(model_root, tmp_path)
        assert "ExternalStub" in refusal and "data_type_mappings" in refusal

    def test_an_external_stub_whose_reference_is_not_a_reference_path(self, model_root: Path, tmp_path: Path) -> None:
        """A malformed ``external_ref``: ``swc.external-incomplete-reference`` reported, generation refused."""
        _edit(model_root, "swc/external/ExternalStub.yaml", external_ref="not a path at all")

        assert "swc.external-incomplete-reference" in _rules(model_root)
        assert "not a path at all" in _generation_refusal(model_root, tmp_path)

    def test_a_component_that_is_not_a_stub_carrying_the_reference_pair(self, model_root: Path, tmp_path: Path) -> None:
        """The reference pair on a non-stub: ``swc.external-reference-without-stub`` reported, generation refused."""
        _edit(model_root, "swc/core/Gamma.yaml", external_ref="/Foreign/Pkg/Gamma")

        assert "swc.external-reference-without-stub" in _rules(model_root)
        assert "Gamma" in _generation_refusal(model_root, tmp_path)

    def test_a_recursive_composition_hierarchy(self, model_root: Path, tmp_path: Path) -> None:
        """A recursive hierarchy: XSD-valid, since each composition is emitted once, yet self-containing.

        ``Sink`` gains a member typed ``Root``, which already places ``Sink``.
        """
        _append(model_root, "compositions/Sink.yaml", "members", {"name": "Up", "composition": "Root"})

        assert "composition.cycle" in _rules(model_root)
        assert "cycle" in _generation_refusal(model_root, tmp_path)

    def test_no_root_with_a_composition_less_connector(self, model_root: Path, tmp_path: Path) -> None:
        """The shape where both halves act; the residue is in the module docstring."""
        _edit(model_root, "compositions/Root.yaml", root=False)

        assert "composition.no-root" in _rules(model_root)
        assert "root: true" in _generation_refusal(model_root, tmp_path)

    def test_an_over_length_composition_name(self, model_root: Path, tmp_path: Path) -> None:
        long_name = "C" + "x" * 128
        _edit(model_root, "compositions/Sink.yaml", name=long_name)

        reported = check_identifier_conformance(str(model_root)).records
        assert [record.rule for record in reported] == ["identifier.declared"]
        assert long_name in _generation_refusal(model_root, tmp_path)

    def test_the_two_type_form_shapes_are_refused_by_the_loader(self, model_root: Path) -> None:
        """The type-form shapes: one refusal, one level lower — ``schema.py``'s ``@model_validator``s at load."""
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"][0]["composition"] = "Sink"
        _write(model_root / "compositions" / "Sub.yaml", document)

        with pytest.raises(ValueError, match="exactly one"):
            check_composition_consistency(str(model_root))

    def test_the_writer_accepts_the_unperturbed_fixture(self, model_root: Path, tmp_path: Path) -> None:
        """The negative control: every refusal above is the perturbation's."""
        assert _generation_refusal(model_root, tmp_path) == ""

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    @pytest.mark.parametrize("bad", ["my bad name", "N" * 129], ids=["pattern", "length"])
    def test_every_composition_name_site_is_reported_and_refused(
        self, site: CompositionNameSite, bad: str, model_root: Path, tmp_path: Path
    ) -> None:
        """The exporter's guard, at every site and on both limbs.

        The ``guard_label`` assertion pins the refusal to
        ``arxml_generation._check_composition_names`` rather than to a downstream
        resolution failure. With ``test_writer_elements.py::TestTheCompositionsFourNameSites``
        these are the four sites times the two guards times the two limbs.
        """
        path = model_root / "compositions" / "Sink.yaml"
        _write(path, site.plant(_load(path), bad))

        reported = check_identifier_conformance(str(model_root)).records
        assert [record.rule for record in reported] == ["identifier.declared"], reported
        assert bad in reported[0].message

        refusal = _generation_refusal(model_root, tmp_path)
        assert site.guard_label in refusal, refusal
        assert bad in refusal, refusal

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    def test_the_exporters_guard_accepts_a_name_at_the_limit_at_every_site(self, site: CompositionNameSite) -> None:
        """The other limb of the pair above, with the guard asked directly.

        A planted name leaves other files naming the old one, so a whole
        ``generate_arxml`` run would refuse for a dangling reference instead.
        """
        composition = Composition.model_validate(site.plant(one_of_each_site(), "N" * 128))

        _check_composition_names([composition])

    def test_write_element_refuses_a_composition_name_the_gate_reports(self, tmp_path: Path) -> None:
        """The other write path: ``write_element``, which the protocol's materialization calls, has its own guard."""
        (tmp_path / "compositions").mkdir()
        with pytest.raises(ValueError, match="Composition name"):
            write_element(
                str(tmp_path),
                "compositions/My Root.yaml",
                Composition(name="My Root", title="T"),
                create=True,
            )


class TestTheReleaseNotesCollisionPromiseIsPinnedEndToEnd:
    """``docs/release-notes/0.9.0.md`` § *Migrating*, step 6 — every claim it makes, end to end.

    The collision is caught at both ends (`rule:name.duplicate-component-type`),
    with exit 1 and no file written.

    What it does **not** reach: the note's *wording*. The message is asserted on
    its subject (the two names), not letter for letter, which would make a
    message contract.
    """

    def _project(self, model_root: Path) -> Path:
        """A manifest beside the perturbed fixture, so the CLI can open it."""
        return _write_manifest(model_root.parent)

    def test_check_model_reports_the_collision_and_exits_one(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = self._project(model_root)
        _edit(model_root, "compositions/Sink.yaml", name="Beta")
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "Beta" in captured.out + captured.err

    def test_generate_arxml_exits_one_and_writes_no_file(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The refused generation exits 1, names ``Beta`` and writes no ``dist/model.arxml``."""
        manifest = self._project(model_root)
        _edit(model_root, "compositions/Sink.yaml", name="Beta")
        output = model_root.parent / "dist" / "model.arxml"
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        assert "Beta" in capsys.readouterr().err
        assert not output.exists(), "a refused generation wrote an ARXML"

    def test_the_unperturbed_project_passes_both_commands(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative control: the refusals above are the collision's, not a project the CLI cannot open."""
        manifest = self._project(model_root)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])
        main()

        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest)])
        main()

        assert (model_root.parent / "dist" / "model.arxml").is_file()


# ---------------------------------------------------------------------------
# The connector rule judges COMPATIBILITY, not name equality
# ---------------------------------------------------------------------------
#
# `rule:connector.interface-mismatch` asks `validator.interface_compatibility`
# in `subset` context: the `to` port's interface is the REQUIRED one and the `from` port's
# the PROVIDED one, and the pair is joined when every member of the required
# interface has a compatible counterpart of the same name in the provided one
# ([constr_1069] 1(a) for data, [constr_1079] 1(a) for operations).
#
# They read a fixture of their own: on every other root the two ports of a
# connector name ONE interface, and the same-name short-circuit answers before
# the predicate is asked.


@pytest.fixture
def compatible_model_root(tmp_path: Path) -> Path:
    """A writable copy of ``fixtures/interface_compatibility/model``; every case below perturbs it."""
    destination = tmp_path / "model"
    shutil.copytree(INTERFACE_COMPATIBILITY_MODEL_ROOT, destination)
    return destination


def _mismatch(model_root: Path) -> validator.ConsistencyIssue:
    """The one ``connector.interface-mismatch`` record, and the assertion that it is one.

    Each case perturbs one connector, so a second record is an unintended second root cause.
    """
    records = [
        record
        for record in check_connector_consistency(str(model_root)).records
        if record.rule == "connector.interface-mismatch"
    ]
    assert len(records) == 1, [record.message for record in records]
    return records[0]


def _endpoint_subjects(connector: str) -> tuple[Subject, ...]:
    """The rule's two subjects: each endpoint's ``port``."""
    return tuple(Subject("connectors", connector, (field_step(side), field_step("port"))) for side in ("from", "to"))


class TestTwoDifferentInterfacesTheStandardJoins:
    """Two different interfaces the standard joins, on the one root that carries them."""

    def test_the_fixture_is_clean_under_the_composed_gate(self, compatible_model_root: Path) -> None:
        """The negative control: nothing at all, from any check, before a perturbation."""
        result = check_cross_layer_consistency(str(compatible_model_root))

        assert result.consistent, result.issues

    def test_neither_connector_is_reported(self, compatible_model_root: Path) -> None:
        assert _connector_rules(compatible_model_root) == []

    @pytest.mark.parametrize(
        ("connector", "provided", "required"),
        [("status_link", "IStatusFull", "IStatusBasic"), ("command_link", "ICommandFull", "ICommandBasic")],
    )
    def test_each_pair_really_names_two_interfaces(
        self, compatible_model_root: Path, connector: str, provided: str, required: str
    ) -> None:
        """Keeps the cases above from being vacuous: one interface on both sides would also report nothing."""
        document = _load(compatible_model_root / "connectors" / f"{connector}.yaml")
        ports = {
            (component["name"], port["name"]): port["interface"]
            for path in sorted((compatible_model_root / "swc").rglob("*.yaml"), key=lambda path: path.as_posix())
            for component in [_load(path)]
            for port in component["ports"]
        }

        assert ports[(document["from"]["swc"], document["from"]["port"])] == provided
        assert ports[(document["to"]["swc"], document["to"]["port"])] == required


class TestAConnectorBetweenCompositionTypedEndpoints:
    """The message for ``composition:`` endpoints, which ``_endpoint_label`` renders like ``swc:`` ones.

    The root of ``fixtures/compositions/model`` is the one committed connector
    between two composition boundary ports.
    """

    def test_the_whole_sentence_names_both_compositions(self, model_root: Path) -> None:
        # `Sink`'s boundary port takes an interface that shares no member name
        # with `ISignal`; the connector pass is read alone, because the same edit
        # makes `Sink`'s own delegation a finding of the composition pass.
        _set_member(model_root, "compositions/Sink.yaml", "ports", 0, interface="ITrigger")

        records = check_connector_consistency(str(model_root)).records

        assert [record.rule for record in records] == ["connector.interface-mismatch"]
        assert records[0].message == (
            "Connector 'root_link' interface mismatch: required 'ITrigger' (Sink.rSignalIn) is not "
            "satisfied by provided 'ISignal' (Sub.pSignalOut): "
            "data element 'trigger' has no counterpart in 'ISignal'."
        )
        assert records[0].subjects == _endpoint_subjects("root_link")


class TestAnIncompatiblePairIsStillReported:
    """One case per shape the subject can take, over the same clean root."""

    def test_a_required_data_element_without_a_counterpart(self, compatible_model_root: Path) -> None:
        """``[constr_1069]`` 1(a): the required side asks for an element the provided side has not got."""
        document = _load(compatible_model_root / "interfaces" / "IStatusBasic.yaml")
        document["data_elements"].append({"name": "extra", "implementation_type": "Tally"})
        _write(compatible_model_root / "interfaces" / "IStatusBasic.yaml", document)

        record = _mismatch(compatible_model_root)

        assert record.message == (
            "Connector 'status_link' interface mismatch: required 'IStatusBasic' (Display.rStatus) "
            "is not satisfied by provided 'IStatusFull' (Sensor.pStatus): "
            "data element 'extra' has no counterpart in 'IStatusFull'."
        )
        assert record.subjects == _endpoint_subjects("status_link")

    def test_a_data_element_whose_resolved_base_type_differs(self, compatible_model_root: Path) -> None:
        """``[constr_1220]``: the same element name, two structures."""
        write_model_element(
            compatible_model_root,
            {"name": "u16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
        )
        write_model_element(compatible_model_root, {"name": "Wide", "kind": "value_type", "base": "u16_base"})
        _set_member(
            compatible_model_root, "interfaces/IStatusBasic.yaml", "data_elements", 0, implementation_type="Wide"
        )

        record = _mismatch(compatible_model_root)

        assert record.message.endswith("data element 'speed': type 'Wide' vs 'Count' — base type size 16 vs 8."), (
            record.message
        )

    def test_only_the_first_incompatibility_is_reported(self, compatible_model_root: Path) -> None:
        """One finding per connector, the first in the required side's declaration order.

        The type disagreement is declared before the missing element, so the record names the type.
        """
        write_model_element(
            compatible_model_root,
            {"name": "u16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
        )
        write_model_element(compatible_model_root, {"name": "Wide", "kind": "value_type", "base": "u16_base"})
        document = _load(compatible_model_root / "interfaces" / "IStatusBasic.yaml")
        document["data_elements"][0]["implementation_type"] = "Wide"
        document["data_elements"].append({"name": "extra", "implementation_type": "Tally"})
        _write(compatible_model_root / "interfaces" / "IStatusBasic.yaml", document)

        record = _mismatch(compatible_model_root)

        assert "data element 'speed'" in record.message
        assert "extra" not in record.message

    def test_a_required_operation_without_a_counterpart(self, compatible_model_root: Path) -> None:
        """``[constr_1079]`` 1(a), the client-server half."""
        document = _load(compatible_model_root / "interfaces" / "ICommandBasic.yaml")
        document["operations"].append({"name": "Halt"})
        _write(compatible_model_root / "interfaces" / "ICommandBasic.yaml", document)

        record = _mismatch(compatible_model_root)

        assert record.message == (
            "Connector 'command_link' interface mismatch: required 'ICommandBasic' (Controller.rCommand) "
            "is not satisfied by provided 'ICommandFull' (Actuator.pCommand): "
            "operation 'Halt' has no counterpart in 'ICommandFull'."
        )
        assert record.subjects == _endpoint_subjects("command_link")

    def test_an_argument_whose_direction_differs(self, compatible_model_root: Path) -> None:
        """``[constr_1076]`` 2: the n-th argument pair is judged by direction and type, never by name."""
        document = _load(compatible_model_root / "interfaces" / "ICommandBasic.yaml")
        document["operations"][1]["arguments"][0]["direction"] = "out"
        _write(compatible_model_root / "interfaces" / "ICommandBasic.yaml", document)

        record = _mismatch(compatible_model_root)

        assert record.message.endswith("operation 'Reset': argument 1 'id' direction out vs in."), record.message

    def test_two_interfaces_of_different_kinds(self, compatible_model_root: Path) -> None:
        """The coarsest disagreement, and the one the XSD accepts at both versions."""
        _set_member(compatible_model_root, "swc/core/Display.yaml", "ports", 0, interface="ICommandBasic")

        record = _mismatch(compatible_model_root)

        assert record.message.endswith("interface kinds differ: client_server vs sender_receiver."), record.message

    def test_an_undeclared_interface_is_another_rules_finding(self, compatible_model_root: Path) -> None:
        """The abstention, at the caller: ``port.undeclared-interface`` owns it, and reports it."""
        _set_member(compatible_model_root, "swc/core/Display.yaml", "ports", 0, interface="IGhost")

        assert "connector.interface-mismatch" not in _connector_rules(compatible_model_root)
        assert "port.undeclared-interface" in [
            record.rule for record in check_cross_layer_consistency(str(compatible_model_root)).records
        ]


class TestTwoApplicationTypesWhosePhysicsDiffers:
    """The predicate's application-type arm, reached through the connector rule on a real model.

    ``test_interface_compatibility.py`` holds the arm row by row in memory. Only
    the **connector** pass is read: the composed gate would add
    ``data-type.application-type-unmapped`` for a type no mapping set pairs.
    """

    #: One reference physical type and one differing in exactly the first of the
    #: three fields ``[constr_1051]`` compares.
    PHYSICS = {
        "PhysKph": {"unit": "km/h", "conversion": {"factor": 0.1, "offset": 0.0}, "range": {"min": 0.0, "max": 400.0}},
        "PhysMps": {"unit": "m/s", "conversion": {"factor": 0.1, "offset": 0.0}, "range": {"min": 0.0, "max": 400.0}},
    }

    def _declare(self, model_root: Path) -> None:
        for name, physics in self.PHYSICS.items():
            write_model_element(model_root, {"name": name, "kind": "application_type", **physics})

    @staticmethod
    def _application_typed(model_root: Path, relative: str, name: str) -> None:
        """Type the interface's first data element by the application type *name*."""
        path = model_root / relative
        document = _load(path)
        element = document["data_elements"][0]
        document["data_elements"][0] = {
            ("application_type" if key == "implementation_type" else key): (
                name if key == "implementation_type" else value
            )
            for key, value in element.items()
        }
        _write(path, document)

    def test_a_unit_that_differs_is_reported_with_both_units(self, compatible_model_root: Path) -> None:
        """``[constr_1051]`` 1, read fail-closed: one unit, or neither."""
        self._declare(compatible_model_root)
        self._application_typed(compatible_model_root, "interfaces/IStatusBasic.yaml", "PhysMps")
        self._application_typed(compatible_model_root, "interfaces/IStatusFull.yaml", "PhysKph")

        record = _mismatch(compatible_model_root)

        assert record.message == (
            "Connector 'status_link' interface mismatch: required 'IStatusBasic' (Display.rStatus) "
            "is not satisfied by provided 'IStatusFull' (Sensor.pStatus): "
            "data element 'speed': type 'PhysMps' vs 'PhysKph' — application type unit 'm/s' vs 'km/h'."
        )
        assert record.subjects == _endpoint_subjects("status_link")

    def test_an_application_type_against_an_implementation_type(self, compatible_model_root: Path) -> None:
        """An application type and an implementation type never join, and the KIND comparison says so.

        The standard admits the pair only through a mapping context, which a
        connector does not have, so the physics is not compared at all.
        """
        self._declare(compatible_model_root)
        self._application_typed(compatible_model_root, "interfaces/IStatusBasic.yaml", "PhysKph")

        record = _mismatch(compatible_model_root)

        assert record.message.endswith(
            "data element 'speed': type 'PhysKph' vs 'Count' — kind application_type vs value_type."
        ), record.message


# ---------------------------------------------------------------------------
# The family's emission gate
# ---------------------------------------------------------------------------


def _append(model_root: Path, relative: str, field: str, member: dict[str, Any]) -> None:
    """Append *member* to one element file's *field* list."""
    path = model_root / relative
    document = _load(path)
    document.setdefault(field, []).append(member)
    _write(path, document)


def _set_member(model_root: Path, relative: str, field: str, index: int, **changes: Any) -> None:
    """Change one member of one element file's *field* list in place."""
    path = model_root / relative
    document = _load(path)
    document[field][index].update(changes)
    _write(path, document)


#: One perturbation of ``fixtures/compositions/model`` per rule the shipped
#: contract binds to this layer — the family's **emission** witness:
#: ``test_validator_rule_coverage.py`` proves declaration and mapping, and a
#: declared, mapped, unreachable rule satisfies both.
#:
#: The key set is **derived from the contract** by
#: :func:`test_the_emission_table_covers_every_rule_the_contract_binds_to_this_layer`,
#: so a rule added to the family fails here until it has a witness. Membership
#: rather than equality is asserted per rule: one perturbation may legitimately
#: trip a second rule (a renamed composition dangles the member that named it),
#: and the *isolation* of each shape is what the per-rule cases above assert.
#:
#: What this table does **not** reach: it witnesses that each rule fires and is
#: answered with a protocol code, never that it fires for the right *reason* or
#: names the right subjects. Those are the cases above, one per rule.
COMPOSITION_FAMILY_BREAKS: dict[str, Callable[[Path], None]] = {
    "connector.endpoint-unknown-port": lambda root: _edit(
        root, "connectors/sub_link.yaml", **{"from": {"swc": "Beta", "port": "absent"}}
    ),
    "connector.from-not-provide": lambda root: _edit(
        root, "connectors/sub_link.yaml", **{"from": {"swc": "Gamma", "port": "rInner"}}
    ),
    "connector.to-not-require": lambda root: _edit(
        root, "connectors/sub_link.yaml", **{"to": {"swc": "Beta", "port": "pInner"}}
    ),
    "connector.interface-mismatch": lambda root: _edit(
        root, "connectors/sub_link.yaml", **{"from": {"swc": "Beta", "port": "pOut"}}
    ),
    # `root_link` is the only connector whose endpoints are composition-typed.
    "connector.endpoint-unknown-composition": lambda root: _edit(
        root, "connectors/root_link.yaml", **{"from": {"composition": "Absent", "port": "pSignalOut"}}
    ),
    "connector.unknown-composition": lambda root: _edit(root, "connectors/sub_link.yaml", composition="Absent"),
    "composition.no-root": lambda root: _edit(root, "compositions/Root.yaml", root=False),
    "composition.multiple-roots": lambda root: _edit(root, "compositions/Sub.yaml", root=True),
    "composition.duplicate-name": lambda root: _write(
        root / "compositions" / "Duplicate.yaml", {**_load(root / "compositions" / "Sink.yaml"), "name": "Sub"}
    ),
    "composition.duplicate-port": lambda root: _append(
        root, "compositions/Sub.yaml", "ports", {"name": "rTrigger", "direction": "require", "interface": "ITrigger"}
    ),
    "composition.duplicate-member": lambda root: _append(
        root, "compositions/Sub.yaml", "members", {"name": "Alpha", "type": "Gamma"}
    ),
    "composition.duplicate-delegation": lambda root: _append(
        root,
        "compositions/Sub.yaml",
        "delegations",
        {"name": "Sub_pSignalOut_Alpha", "port": "rTrigger", "member": "Gamma", "member_port": "rTrig"},
    ),
    # A second delegation differing only in `name`: the name key of the row
    # above cannot see it, which is the whole point of the second key.
    "composition.duplicate-delegation-link": lambda root: _append(
        root,
        "compositions/Sub.yaml",
        "delegations",
        {"name": "Sub_pSignalOut_Alpha_Again", "port": "pSignalOut", "member": "Alpha", "member_port": "pOut"},
    ),
    "composition.cycle": lambda root: _append(
        root, "compositions/Sink.yaml", "members", {"name": "Up", "composition": "Root"}
    ),
    "composition.member-unknown-type": lambda root: _set_member(
        root, "compositions/Sub.yaml", "members", 1, type="Absent"
    ),
    "composition.member-unknown-composition": lambda root: _set_member(
        root, "compositions/Root.yaml", "members", 0, composition="Absent"
    ),
    "composition.member-type-repeated": lambda root: _append(
        root, "compositions/Sub.yaml", "members", {"name": "SecondGamma", "type": "Gamma"}
    ),
    "composition.delegation-unknown-member": lambda root: _set_member(
        root, "compositions/Sink.yaml", "delegations", 0, member="Absent"
    ),
    "composition.delegation-unknown-port": lambda root: _set_member(
        root, "compositions/Sink.yaml", "delegations", 0, port="absent"
    ),
    "composition.delegation-port-direction-mismatch": lambda root: _set_member(
        root, "compositions/Sub.yaml", "delegations", 0, member="Gamma", member_port="rTrig"
    ),
    # The two ports face the same way and name different interfaces: `Sub`'s
    # `pSignalOut` is `ISignal` and `Beta.pInner` is `ITrigger`, both provide. The
    # direction rule abstains, which is what makes this row exercise the
    # interface rule alone.
    "composition.delegation-interface-mismatch": lambda root: _set_member(
        root, "compositions/Sub.yaml", "delegations", 0, member_port="pInner"
    ),
    "composition.connector-endpoint-not-a-member": lambda root: _edit(
        root, "connectors/sub_link.yaml", **{"to": {"swc": "Consumer", "port": "rIn"}}
    ),
    "composition.delegation-connector-name-collision": lambda root: _append(
        root,
        "compositions/Sub.yaml",
        "delegations",
        {"name": "sub_link", "port": "rTrigger", "member": "Gamma", "member_port": "rTrig"},
    ),
    "composition.port-undeclared-interface": lambda root: _set_member(
        root, "compositions/Sink.yaml", "ports", 0, interface="Absent"
    ),
    "name.duplicate-component-type": lambda root: _edit(root, "compositions/Sink.yaml", name="Beta"),
    "name.composition-implementation-collision": lambda root: _edit(
        root, "compositions/Sink.yaml", name="Beta_Implementation"
    ),
    # A budgeted runnable makes the writer generate `Beta_Timing`.
    "name.composition-timing-collision": lambda root: (
        _edit(root, "swc/core/Beta.yaml", runnables=[{"name": "Run", "wcet": "1us", "wcet_type": "gross"}]),
        _edit(root, "compositions/Sink.yaml", name="Beta_Timing"),
    ),
    "identifier.declared": lambda root: _edit(root, "compositions/Sink.yaml", name="my sink"),
    # A composition's ports, members, delegations and
    # connectors are one name space ([TPS_GST_00018]), so a boundary port named
    # like a member is an exact cross-container repeat no per-list rule sees.
    "name.name-space-collision": lambda root: _append(
        root, "compositions/Sub.yaml", "ports", {"name": "Alpha", "direction": "require", "interface": "ITrigger"}
    ),
    # `[constr_1295]`, judged across the root's one connector by
    # following both composition-typed ends down their delegations to `Beta` and
    # `Consumer`, two application components, once `ISignal.value` is a pointer.
    "connector.pointer-between-application-components": lambda root: (
        _write(
            model_element_path(root, "uint8Ptr", kind="pointer"),
            {"name": "uint8Ptr", "kind": "pointer", "target": "uint8"},
        ),
        _write(
            root / "interfaces" / "ISignal.yaml",
            {
                **_load(root / "interfaces" / "ISignal.yaml"),
                "data_elements": [{"name": "value", "implementation_type": "uint8Ptr"}],
            },
        ),
    ),
    # `[constr_1174]`, a composition port typed by a service
    # interface. `Sink` and `Sub` both own a port on `ISignal`.
    "composition.port-service-interface": lambda root: _edit(root, "interfaces/ISignal.yaml", is_service=True),
    # A composition's title holding a character XML 1.0 cannot carry.
    "text.xml-unrepresentable-character": lambda root: _edit(root, "compositions/Sub.yaml", title="Sub\x07title"),
    # A composition is a linkable element, and its `satisfies`
    # naming a requirement no file declares is the trace rule's broken reference.
    "trace.link-unknown-requirement": lambda root: _edit(root, "compositions/Sub.yaml", satisfies=["REQ_GHOST@1"]),
    # One composition's list naming one requirement at two versions -- the repeat is this rule's
    # whether or not the id is declared (that half is the rule above's).
    "trace.duplicate-link": lambda root: _edit(root, "compositions/Sub.yaml", satisfies=["REQ_GHOST@1", "REQ_GHOST@2"]),
    # A requirement deriving from a composition no file declares -- the element
    # form names the layer by token.
    "trace.derived-from-unknown-target": lambda root: _write(
        root / "requirements" / "REQ_PROBE.yaml",
        {
            "id": "REQ_PROBE",
            "title": "Probe",
            "description": "Derived from a composition no file declares.",
            "version": 1,
            "source": "architecture",
            "derived_from": ["composition:Ghost"],
        },
    ),
    # A key no field of a composition declares, which the loader drops.
    "model.unknown-key": lambda root: _edit(root, "compositions/Sub.yaml", titel="Sub"),
}


#: Rules the contract binds to this layer that this table deliberately does NOT
#: witness, each with the reason and the suite that does; each key is asserted
#: to still be one the contract binds here.
_WITNESSED_ELSEWHERE: dict[str, str] = {
    # It reads the FILE NAMES of every layer directory, and two names differing
    # only in case cannot be built on a case-insensitive filesystem (macOS,
    # Windows). Witnessed probe-gated, and over a synthesized listing on every host.
    "storage.filename-case-collision": "test_validator_storage.py",
    # The same shape: a file named for a Windows device cannot be built on Windows.
    "storage.reserved-device-name": "test_reserved_device_names.py",
    # A `task.*` rule that names the composition fields it walks; its break needs
    # two partitions and two tasks this layer's model does not carry.
    "task.instance-in-multiple-partitions": "test_task_model.py",
    # On the same terms: a `task.*` rule over a task this layer's model does not carry.
    "task.runnable-cost-exceeds-task": "test_runnable_costs.py",
    # The instance identity's eight rules: `task.*` or `sequence.*` rules over a
    # task or a sequence this layer's model does not carry.
    "task.resolved-duplicate-runnable": "test_task_model.py",
    "task.instance-ambiguous": "test_task_model.py",
    "task.instance-unknown": "test_task_model.py",
    "task.instance-not-a-component": "test_task_model.py",
    "sequence.resolved-duplicate-step": "test_sequence_model.py",
    "sequence.step-instance-ambiguous": "test_sequence_model.py",
    "sequence.step-instance-unknown": "test_sequence_model.py",
    "sequence.step-instance-not-a-component": "test_sequence_model.py",
    # Traceability WARNINGS: a warning is never in `records`, which the emission
    # case reads, and has no protocol code; witnessed landing in `warnings`.
    "trace.requirement-unallocated": "test_validator_trace.py",
    "trace.link-version-not-stated": "test_validator_trace.py",
    "trace.link-needs-confirmation": "test_validator_trace.py",
    # A warning too.
    "trace.link-unpublishable-id": "test_validator_trace.py",
}


def _family_rules_the_contract_declares() -> set[str]:
    """Every ``CONSISTENCY_RULES`` rule the shipped contract binds to this layer.

    Read from the contract's ``appliesTo`` (``layer:compositions`` or any
    ``field:compositions.*``) intersected with the validator's inventory, which
    drops the loader-owned element-local rules; :data:`_WITNESSED_ELSEWHERE` is removed.
    """
    declared = set()
    for rule in load_contract()["rules"]:
        applies = rule.get("appliesTo", ())
        if any(entry == "layer:compositions" or entry.startswith("field:compositions.") for entry in applies):
            identifier = rule["id"].removeprefix("rule:")
            if identifier in validator.CONSISTENCY_RULES:
                declared.add(identifier)
    stale = sorted(set(_WITNESSED_ELSEWHERE) - declared)
    assert stale == [], f"these exemptions name no rule the contract binds to this layer: {stale}"
    return declared - set(_WITNESSED_ELSEWHERE)


def test_the_emission_table_covers_every_rule_the_contract_binds_to_this_layer() -> None:
    """The table and the contract's family rules agree, both directions: a rule with no witness is a red.

    A row for a rule the contract no longer binds would quietly witness nothing.
    """
    assert set(COMPOSITION_FAMILY_BREAKS) == _family_rules_the_contract_declares()


@pytest.mark.parametrize("rule", sorted(COMPOSITION_FAMILY_BREAKS))
def test_each_rule_of_the_family_is_emitted_and_answered_with_a_protocol_code(rule: str, model_root: Path) -> None:
    """Declared, mapped **and reachable** — through the composed gate ``check-model`` runs.

    The protocol half is asserted on the rule's **own** records, so the code
    assertion cannot pass on another rule's violation.
    """
    COMPOSITION_FAMILY_BREAKS[rule](model_root)

    result = check_cross_layer_consistency(str(model_root))
    reported = [record for record in result.records if record.rule == rule]
    assert reported, f"{rule} was not emitted; reported {sorted({r.rule for r in result.records})}"

    own = validator.ConsistencyResult(consistent=False, records=tuple(reported))
    violations = diagnose(own)
    assert violations, f"{rule} is answered by no protocol violation"
    assert {violation.error for violation in violations} == {RULE_CODES[rule].code}


# ---------------------------------------------------------------------------
# `check-model` over the committed roots, and through the protocol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", MIGRATED_MODEL_ROOTS)
def test_check_model_is_clean_on_every_migrated_root(relative: str) -> None:
    """The composition and connector checks are clean on every migrated root, the template's included.

    The template's root is what ``swcraft init`` copies, and a scaffolded project
    must pass the ``check-model`` it is told to run.
    """
    model_root = str(REPO_ROOT / relative)

    assert check_composition_consistency(model_root).consistent, check_composition_consistency(model_root).issues
    assert check_connector_consistency(model_root).consistent, check_connector_consistency(model_root).issues


def test_check_model_is_clean_on_a_freshly_inited_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A freshly inited project — a root with no members — passes the gate it is told to run."""
    project = tmp_path / "fresh"
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project), "--name", "Fresh"])
    main()

    monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(project / "swcraft.yaml")])
    main()

    assert (project / "model" / "compositions" / "Root.yaml").is_file()


class TestTheProtocolAnswersTheCompositionRules:
    """Pass D over a staged composition: a code from the closed error enum, never an escape.

    ``broken-reference`` where an identity does not resolve,
    ``consistency-violation`` where every reference resolves and a rule over the
    assembled result fails. ``diagnosis.RULE_CODES`` is the table.
    """

    def test_a_member_naming_no_declared_component_is_a_broken_reference(self, model_root: Path) -> None:
        """A member ADDED with an unresolvable type, so nothing else moves.

        Re-pointing an existing member's `type` would also unplace the type it
        named, which a connector endpoint then reports — two rules for one edit.
        """
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "Zeta", "type": "Absent"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        violations = diagnose(check_cross_layer_consistency(str(model_root)))

        assert [violation.error for violation in violations] == ["broken-reference"]
        assert "Absent" in violations[0].message
        assert violations[0].addresses

    def test_the_engine_refuses_a_staged_composition_with_an_unknown_member(self, tmp_path: Path) -> None:
        """``handle_request`` end to end — stage, materialize, pass D, translate: a code, never an escape.

        Every element the change set writes validates, so only the rule can refuse it.
        """
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": baseline_token(project / "model"),
                "intent": "place a member of a type nothing declares",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "compositions", "element": "Extra"},
                        "value": {
                            "name": "Extra",
                            "title": "A second composition",
                            "members": [{"name": "Ghost", "type": "Absent"}],
                        },
                    }
                ],
            },
            project=project,
        )

        assert response["response"] == "error", response
        assert response["error"] == "broken-reference", response
        assert response["failure_class"] == "invalid", response
        assert "Absent" in response["message"], response

    def test_the_engine_answers_a_staged_hierarchy_rule_with_consistency_violation(self, tmp_path: Path) -> None:
        """The other code, through the same path: a second ``root: true`` resolves every reference yet breaks a rule."""
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": baseline_token(project / "model"),
                "intent": "declare a second root",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "compositions", "element": "Extra"},
                        "value": {"name": "Extra", "title": "A second root", "root": True},
                    }
                ],
            },
            project=project,
        )

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        assert response["failure_class"] == "invalid", response
        assert "root: true" in response["message"], response

    def test_a_duplicated_member_name_is_a_consistency_violation(self, model_root: Path) -> None:
        document = _load(model_root / "compositions" / "Sub.yaml")
        document["members"].append({"name": "Alpha", "type": "Gamma"})
        _write(model_root / "compositions" / "Sub.yaml", document)

        violations = diagnose(check_cross_layer_consistency(str(model_root)))

        assert "consistency-violation" in {violation.error for violation in violations}
        assert any("Alpha" in violation.message for violation in violations)


def test_the_composed_gate_runs_the_composition_check(model_root: Path) -> None:
    """The composer reaches it: a rule reported by the check alone is not a gate."""
    _edit(model_root, "compositions/Root.yaml", root=False)

    result = check_cross_layer_consistency(str(model_root))

    assert not result.consistent
    assert "composition.no-root" in {record.rule for record in result.records}
