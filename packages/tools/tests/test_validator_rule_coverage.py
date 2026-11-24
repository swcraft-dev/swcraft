"""The reverse coverage table: from the validator's rule inventory to the protocol's codes.

A test that drives a refused change set exercises only the rules somebody wrote a case for, and a rule with no code
is answered by ``UndiagnosedInconsistency``, an internal fault. So this module reads the other way:
:data:`~swcraft_tools.validator.CONSISTENCY_RULES` is the closed set of rules, and
:data:`~swcraft_tools.editing.diagnosis.RULE_CODES` must name every ``error`` rule and nothing else (pass D reads
the error half of a result; :func:`~swcraft_tools.validator.severity_of`). A new check fails until its code is
decided. Further:

* every code is a real member of the enum, of the ``invalid`` class;
* ``consistency-violation`` is used, for the rule shape it exists for;
* each of :data:`FORMERLY_UNDIAGNOSED`'s rules fires end to end through ``check_cross_layer_consistency``, and
  ``trace.requirement-unallocated`` witnesses that a warning lands in ``warnings`` and pass D answers nothing;
* a record's ``subjects`` become the rejection's ``addresses``, so each rule names the right model places.

**What this module does not reach.** Emission is proved only for :data:`FORMERLY_UNDIAGNOSED`'s rows, the warning
witness and the per-family classes, so a rule with a declaration, a mapping and no case anywhere passes here. A rule
added to a layer family owes an emission witness in that family's module; no derived gate covers the whole
inventory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import validator_package
import yaml
from conftest import write_model_element
from swcraft_tools import validator
from swcraft_tools.diagnostics import Subject, SubjectStep
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    INVALID_IDENTIFIER,
    RULE_CODES,
    diagnose,
)
from swcraft_tools.editing.passes import pass_d
from swcraft_tools.editing.protocol import ERROR_CLASS_MAP
from swcraft_tools.reader import find_swc_file
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    SEVERITY_ERROR,
    ConsistencyResult,
    access,
    check_cross_layer_consistency,
    hardware,
    names,
    severity_of,
)
from swcraft_tools.validator.hardware import _RESOURCE_FINDING_RULES
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_binding,
    load_contract,
)

# ---------------------------------------------------------------------------
# 1. The coverage relation itself
# ---------------------------------------------------------------------------


def _error_rules() -> set[str]:
    """The rules pass D can answer: every declared rule whose severity is ``error``."""
    return {rule for rule in CONSISTENCY_RULES if severity_of(rule) == SEVERITY_ERROR}


def _warning_rules() -> set[str]:
    """The complement: the rules whose findings are listed, never refused."""
    return set(CONSISTENCY_RULES) - _error_rules()


#: A shipped, mapped error rule, made a warning by `monkeypatch` for one test at
#: a time, so the partition names it. Chosen by identifier, not position, so the
#: perturbation does not go inert when the inventory's first rule changes.
_SYNTHETIC_WARNING_RULE = "connector.duplicate-name"


class TestEveryValidatorRuleHasACode:
    """The rule-to-code table, in both directions, over the error rules.

    ``RULE_CODES`` and ``validator.WARNING_RULES`` partition the inventory. The
    shipped warning set is the conformance gate's to hold against the contract;
    the partition is held here with a synthetic warning rule, so it does not go
    inert while the set is empty.
    """

    def test_every_validator_rule_is_mapped_to_a_protocol_code(self) -> None:
        unmapped = sorted(_error_rules() - set(RULE_CODES))
        assert not unmapped, (
            f"`validator.CONSISTENCY_RULES` declares {unmapped} as error rules, which `editing/diagnosis.py`'s "
            f"RULE_CODES does not name. A validator error rule with no protocol code is answered with "
            f"`UndiagnosedInconsistency` — an internal fault, not a protocol answer — so decide which of the "
            f"protocol's codes each one is and add it. If none fits, the enum needs another member, which means "
            f"the decision record's error-code block and its prose bullet, `protocol.schema.json`'s enum and "
            f"`error_class_map`, "
            f"`protocol.ERROR_CLASS_MAP`, and a vector, all in one change."
        )

    def test_no_warning_rule_has_a_protocol_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two tables partition the inventory: a warning rule maps to no code (pass D never sees one)."""
        coded_warnings = sorted(set(RULE_CODES) & _warning_rules())
        assert not coded_warnings, (
            f"RULE_CODES maps {coded_warnings}, whose severity is warning: pass D reads the error half of a "
            f"result and never sees one of their findings, so the row is dead contract. Remove the row, or "
            f"remove the rule's `severity` from the contract and `validator.WARNING_RULES` together."
        )
        assert set(RULE_CODES) | _warning_rules() == set(CONSISTENCY_RULES)

        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))

        assert sorted(set(RULE_CODES) & _warning_rules()) == [_SYNTHETIC_WARNING_RULE]
        assert _SYNTHETIC_WARNING_RULE not in _error_rules()

    def test_the_table_names_no_rule_the_validator_does_not_declare(self) -> None:
        # Catches a rule renamed on one side only: a mapping for a rule that no
        # longer exists never fires, and its absence looks like the rule passing.
        stale = sorted(set(RULE_CODES) - set(CONSISTENCY_RULES))
        assert not stale, f"RULE_CODES maps {stale}, which `validator.CONSISTENCY_RULES` does not declare"

    def test_every_declared_rule_states_what_it_means(self) -> None:
        # The inventory is documentation as well as a key set; an empty
        # description makes the table above unreviewable.
        assert all(description.strip() for description in CONSISTENCY_RULES.values())

    def test_the_inventory_is_not_trivially_small(self) -> None:
        # A guard against the table being emptied to make the relation hold.
        assert len(CONSISTENCY_RULES) >= 50


class TestTheCodesTheTableUses:
    """Whatever the table maps onto has to be a code pass D can legitimately produce."""

    def test_every_mapped_code_is_a_member_of_the_closed_enum(self) -> None:
        unknown = sorted({mapping.code for mapping in RULE_CODES.values()} - set(ERROR_CLASS_MAP))
        assert not unknown, f"RULE_CODES produces {unknown}, which is not in the protocol's error-code enum"

    def test_every_mapped_code_is_of_the_invalid_failure_class(self) -> None:
        # `stale`, `unauthorized` and `partial` are decided by other passes over
        # other inputs, so a rule mapping onto one of them is a category error.
        classes = {ERROR_CLASS_MAP[mapping.code] for mapping in RULE_CODES.values()}
        assert classes == {"invalid"}, classes

    def test_pass_d_uses_exactly_three_codes(self) -> None:
        # Pass D has four codes; `schema-violation` is unreachable there because a
        # payload is Pydantic-validated twice before pass D runs.
        assert {mapping.code for mapping in RULE_CODES.values()} == {
            BROKEN_REFERENCE,
            INVALID_IDENTIFIER,
            CONSISTENCY_VIOLATION,
        }

    def test_the_tenth_code_is_the_majority_answer(self) -> None:
        # If `consistency-violation` ever maps to a handful of rules, either the
        # rules moved or somebody widened `broken-reference`.
        consistency = [rule for rule, mapping in RULE_CODES.items() if mapping.code == CONSISTENCY_VIOLATION]
        assert len(consistency) > len(RULE_CODES) / 2, sorted(consistency)


class TestTheHardwareRulesArrivedWithTheirCodes:
    """Which code each `hardware.*` rule gets, and that the three budget rules are still mapped.

    The `hardware.` prefix does not partition by emission site: it holds the
    rules `check_hardware_topology` mints and three minted in
    `resource_analysis.py`. So the two sets are written out separately and the
    prefix is asserted to be their union, never used to derive either one.
    """

    #: The thirteen `check_hardware_topology` mints, and the code each is answered
    #: with. Written out rather than derived from a prefix: a rule moving between
    #: `broken-reference` and `consistency-violation` changes what a client is
    #: told to repair, and a derivation would follow the move instead of failing.
    TOPOLOGY_CODES = {
        "hardware.layer-absent": CONSISTENCY_VIOLATION,
        "hardware.no-processor": CONSISTENCY_VIOLATION,
        "hardware.multiple-processors": CONSISTENCY_VIOLATION,
        "hardware.duplicate-name": CONSISTENCY_VIOLATION,
        # A task names a partition, and a partition names a core.
        "hardware.undeclared-partition": BROKEN_REFERENCE,
        "hardware.partition-not-a-partition": CONSISTENCY_VIOLATION,
        "hardware.partition-undeclared-core": BROKEN_REFERENCE,
        "hardware.partition-core-not-a-core": CONSISTENCY_VIOLATION,
        "hardware.undeclared-memory": BROKEN_REFERENCE,
        "hardware.memory-not-a-memory": CONSISTENCY_VIOLATION,
        "hardware.accessible-undeclared-core": BROKEN_REFERENCE,
        "hardware.accessible-not-a-core": CONSISTENCY_VIOLATION,
        # Every reference the reachability rule reads resolves, so what fails is
        # a design rule over three of them and not an identity.
        "hardware.unreachable-memory": CONSISTENCY_VIOLATION,
    }

    #: The three the budget pass drains out of `resource_analysis`'s finding
    #: codes. They share the prefix and nothing else: `_RESOURCE_FINDING_RULES`
    #: is their binding and `check_resource_budgets` their emission site.
    BUDGET_CODES = {
        "hardware.missing-budget": CONSISTENCY_VIOLATION,
        "hardware.threshold-breach": CONSISTENCY_VIOLATION,
        "hardware.rate-monotonic-violation": CONSISTENCY_VIOLATION,
    }

    #: Both, for the parametrizations that are about the code a client is
    #: answered with and not about where the rule is minted.
    HARDWARE_CODES = {**TOPOLOGY_CODES, **BUDGET_CODES}

    def test_the_validator_declares_exactly_these_sixteen(self) -> None:
        """Thirteen topology rules plus the three budget ones; the prefix is their union.

        A test that read the prefix as an emission site would accept a budget
        rule appearing in `check_hardware_topology`, or the reverse.
        """
        assert set(self.TOPOLOGY_CODES) & set(self.BUDGET_CODES) == set()
        assert {rule for rule in CONSISTENCY_RULES if rule.startswith("hardware.")} == set(self.HARDWARE_CODES)
        assert len(self.HARDWARE_CODES) == 16

    @pytest.mark.parametrize("rule", sorted(HARDWARE_CODES))
    def test_each_one_is_answered_with_the_code_it_was_given(self, rule: str) -> None:
        assert RULE_CODES[rule].code == self.HARDWARE_CODES[rule]

    def test_the_four_reference_rules_group_by_what_the_repair_fixes(self) -> None:
        """One repair, one violation: declaring the core fixes every referrer.

        A partition's `core` and a memory's `accessible` entry both name a core,
        so they share a group; a memory budget and a task's `partition` each have
        a group of their own.
        """
        core_group = RULE_CODES["hardware.accessible-undeclared-core"].group
        assert RULE_CODES["hardware.partition-undeclared-core"].group == core_group
        assert RULE_CODES["hardware.undeclared-memory"].group != core_group
        assert RULE_CODES["hardware.undeclared-partition"].group not in {
            core_group,
            RULE_CODES["hardware.undeclared-memory"].group,
        }

    def test_the_partition_retired_rules_are_gone_from_both_tables(self) -> None:
        """The task's two core rules and the core placement rule are retired."""
        retired = {"hardware.undeclared-core", "hardware.core-not-a-core", "safety.task-on-unqualified-core"}

        assert retired & set(CONSISTENCY_RULES) == set()
        assert retired & set(RULE_CODES) == set()

    def test_the_four_retired_rules_are_gone_from_both_tables(self) -> None:
        """`check_hardware_topology` subsumed them; leaving either half is a leak.

        A retired rule left in `RULE_CODES` is dead contract, and one left in
        `CONSISTENCY_RULES` is a rule nothing can emit — the two failures the
        coverage relation cannot tell apart on its own.
        """
        retired = {
            "resource.duplicate-resource-name",
            "resource.duplicate-core-id",
            "resource.undeclared-core",
            "resource.undeclared-memory-pool",
        }

        assert retired & set(CONSISTENCY_RULES) == set()
        assert retired & set(RULE_CODES) == set()

    def test_the_three_surviving_budget_codes_still_map(self) -> None:
        """The budget pass's three rules are the three the table maps.

        Read off `_RESOURCE_FINDING_RULES` itself, so the table changing is this
        test's failure and not a silent change of what `check_resource_budgets`
        can report.
        """
        surviving = set(_RESOURCE_FINDING_RULES.values())

        assert surviving == set(self.BUDGET_CODES)
        assert surviving <= set(CONSISTENCY_RULES)
        assert all(RULE_CODES[rule].code == CONSISTENCY_VIOLATION for rule in surviving)

    def test_no_rule_identifier_survives_under_the_retired_layers_prefix(self) -> None:
        """No `resource.*` identifier is left in `CONSISTENCY_RULES`, `_RESOURCE_FINDING_RULES` or `RULE_CODES`.

        The coverage relation compares two of the three tables and would agree on
        a stale key held by both. What this does **not** reach: the contract and
        the binding (`test_model_contract.py::test_no_rule_identifier_names_the_retired_resource_family`),
        and prose anywhere.
        """
        stale = sorted(
            rule
            for rule in set(CONSISTENCY_RULES) | set(RULE_CODES) | set(_RESOURCE_FINDING_RULES.values())
            if rule.startswith("resource.")
        )

        assert stale == []


class TestTheAccessRulesArrivedWithTheirCodes:
    """Which code each of the five runnable-access rules (`reads`, `writes`, `calls`) gets.

    A name that does not resolve is `broken-reference`; an entry whose every
    reference resolves but faces the wrong way (a read on a provide port, a call
    through a sender/receiver port) is `consistency-violation`. The behaviour is
    `packages/tools/tests/test_validator_access.py`'s; this class is about the
    table. The `swc.access.` prefix holds more identifiers than the walk mints
    (`swc.access.name-collision` is `check_name_uniqueness`'s), so the tests state
    the binding in both directions.
    """

    #: Written out rather than derived from the `swc.access.` prefix, for the
    #: reason `TOPOLOGY_CODES` above gives: a rule moving between the two codes
    #: changes what a client is told to repair.
    ACCESS_CODES = {
        "swc.access.unknown-port": BROKEN_REFERENCE,
        "swc.access.unknown-element": BROKEN_REFERENCE,
        "swc.access.unknown-operation": BROKEN_REFERENCE,
        "swc.access.direction-mismatch": CONSISTENCY_VIOLATION,
        "swc.access.interface-kind-mismatch": CONSISTENCY_VIOLATION,
    }

    @pytest.mark.parametrize("rule", sorted(ACCESS_CODES))
    def test_each_one_is_answered_with_the_code_it_was_given(self, rule: str) -> None:
        assert RULE_CODES[rule].code == self.ACCESS_CODES[rule]

    def test_the_three_reference_rules_group_by_what_the_repair_fixes(self) -> None:
        """A missing port, data element and operation are repaired at three places: three groups."""
        groups = [
            RULE_CODES[rule].group for rule in sorted(self.ACCESS_CODES) if rule.startswith("swc.access.unknown-")
        ]
        assert len(groups) == 3
        assert len(set(groups)) == 3

    def test_every_one_is_bound_to_the_access_walk_and_the_sixth_is_not(self) -> None:
        """The walk owns five of the prefix's bindings; the others are bound elsewhere.

        Stated in both directions: a gate that only listed the five would go green
        if another binding were quietly moved into the walk.
        """
        bindings = {
            rule: bound for rule, bound in load_binding()["rules"].items() if rule.startswith("rule:swc.access.")
        }
        others = (
            "rule:swc.access.name-collision",
            "rule:swc.access.queued-read-by-value",
            # The implicit read's two, bound outside the walk.
            "rule:swc.access.queued-implicit-read",
            "rule:swc.access.implicit-read-enable-update",
        )
        assert sorted(bindings) == sorted([*(f"rule:{rule}" for rule in self.ACCESS_CODES), *others])
        # The walk and its per-entry phase, which the walk calls.
        walk = validator_package.reached_functions(access, "runnable_access_records")
        assert {(bound["module"], bound["callable"]) for rule, bound in bindings.items() if rule not in others} == {
            ("swcraft_tools.validator.access", "runnable_access_records"),
            ("swcraft_tools.validator.access", "_access_entry_records"),
        }
        assert "_access_entry_records" in walk
        assert bindings["rule:swc.access.name-collision"]["callable"] == "_component_member_name_records"
        assert "_component_member_name_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert bindings["rule:swc.access.queued-read-by-value"]["callable"] == "queued_read_records"
        for rule in others[2:]:
            assert bindings[rule]["module"].rpartition(".")[0] == "swcraft_tools.validator"
            assert bindings[rule]["callable"] not in walk
        assert callable(validator.runnable_access_records)


class TestTheInitConstantCollisionRuleArrivedWithItsCode:
    """The report of a colliding ``{Interface}_{Element}_Init`` constant name.

    The writer refuses the collision, so ``check-model`` must report it too. This
    class is about the table, the binding and the contract entry; the behaviour
    is ``packages/tools/tests/test_validator_name_uniqueness.py``'s.
    ``consistency-violation`` and not ``broken-reference``: every name involved
    resolves, and what fails is a rule over the resolved result. The same shape
    as ``rule:swc.access.name-collision`` at the runnable scope.
    """

    RULE = "interface.init-constant-name-collision"

    def test_the_validator_declares_it_and_states_what_it_means(self) -> None:
        assert self.RULE in CONSISTENCY_RULES
        assert CONSISTENCY_RULES[self.RULE].strip()

    def test_it_is_answered_with_consistency_violation(self) -> None:
        assert RULE_CODES[self.RULE].code == CONSISTENCY_VIOLATION

    def test_it_is_bound_to_the_name_uniqueness_check(self) -> None:
        """Bound to the phase of `check_name_uniqueness` that mints it, which the check calls."""
        bound = load_binding()["rules"][f"rule:{self.RULE}"]
        assert (bound["module"], bound["callable"]) == (
            "swcraft_tools.validator.names",
            "_init_constant_collision_records",
        )
        assert "_init_constant_collision_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert callable(validator.check_name_uniqueness)

    def test_the_contract_declares_it_over_every_declared_name_that_can_claim_one(self) -> None:
        """`appliesTo` names the declared names a finding can be about, and the collection walked.

        The element's `init` is not read: the reservation is unconditional. Three
        names, not the two the derivation joins: the other half of a collision is
        a declared interface of either kind whose own name is some pair's
        constant, so a client-server interface named `IX_value_Init` is a
        claimant too."""
        contract = load_contract()
        (rule,) = [entry for entry in contract["rules"] if entry["id"] == f"rule:{self.RULE}"]
        assert rule["family"] == "cross-element"
        assert rule["appliesTo"] == [
            "field:interfaces.sender_receiver.name",
            "field:interfaces.client_server.name",
            "field:interfaces.data_element.name",
            # The collection the derivation walks for its pairs
            # (`_init_constant_claimants`): a collection walked is a read.
            "field:interfaces.sender_receiver.data_elements",
        ]
        assert rule["summary"] == CONSISTENCY_RULES[self.RULE]

    def test_it_rides_an_already_opened_minor(self) -> None:
        """The contract carries the version the tree pins; adding the rule was a minor (additive) change."""
        # The version the tree carries; it moves with every contract bump.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_scan_reads_its_emission_at_the_interfaces_layer(self) -> None:
        """The emission-layer scan resolves the rule's `subjects=` expression statically, at `interfaces`."""
        contract = load_contract()
        sites = emission_layers(contract)[f"rule:{self.RULE}"]
        assert sites, "the scan found no emission site for the rule"
        assert all(site == frozenset({"interfaces"}) for site in sites), sites
        assert [failure for failure in cross_element_layer_failures(contract) if self.RULE in failure] == []


#: What the unwritable-integer rule reads that the string rule does not: an
#: enumeration's reference to a table decides whether its vocabulary resolves
#: (`validator.stated_literals`), and the integer is refused before the names
#: are asked (`_named_value_violations`); a text under an enumeration is the
#: literal rule's, never the string rule's.
_UNWRITABLE_ONLY: dict[str, list[str]] = {
    "interface.init-unwritable-integer": ["field:application_types.enum.compu_method_ref"],
}


class TestTheTwoValueRulesArrivedWithTheirCodes:
    """The two rules about a value the writer refuses: a text under a type with no table, a too-wide integer.

    The writer refuses both values, so ``check-model`` must report them too. This
    class is about the table, the binding and the contract entry; the behaviour
    is `packages/tools/tests/test_validator_init.py`'s and the agreement with the
    writer `packages/tools/tests/test_arxml_init_writer.py`'s.
    `consistency-violation` and not `broken-reference`: the type resolves and the
    value is present, but is not one the type takes; the repair is an edit to the
    element.

    **What this class does not reach.** It does not say the rules fire (the
    validator suite's), nor that the contract's `appliesTo` is complete: the
    emission-layer gate is a one-way containment check.
    """

    RULES = ("interface.init-string-scalar", "interface.init-unwritable-integer")

    @pytest.mark.parametrize("rule", RULES)
    def test_the_validator_declares_it_and_states_what_it_means(self, rule: str) -> None:
        assert rule in CONSISTENCY_RULES
        assert CONSISTENCY_RULES[rule].strip()

    @pytest.mark.parametrize("rule", RULES)
    def test_it_is_answered_with_consistency_violation(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION

    @pytest.mark.parametrize("rule", RULES)
    def test_it_is_bound_to_the_init_conformance_check(self, rule: str) -> None:
        bound = load_binding()["rules"][f"rule:{rule}"]
        assert (bound["module"], bound["callable"]) == ("swcraft_tools.validator.init_values", "check_init_conformance")
        assert callable(validator.check_init_conformance)

    @pytest.mark.parametrize("rule", RULES)
    def test_the_contract_declares_it_over_the_value_the_type_and_every_kind(self, rule: str) -> None:
        """`appliesTo` names the fields the walk reads and every kind a value can be typed by.

        The walk dispatches on the declared kind and either refusal can be reached
        under any of them; a `base_type` is not among them, since no data
        prototype may name one."""
        contract = load_contract()
        (entry,) = [candidate for candidate in contract["rules"] if candidate["id"] == f"rule:{rule}"]
        assert entry["family"] == "cross-element"
        assert entry["appliesTo"] == [
            "field:interfaces.data_element.init",
            # The side a prototype states.
            "field:interfaces.data_element.application_type",
            "field:interfaces.data_element.implementation_type",
            "kind:implementation_types.value_type",
            "kind:implementation_types.reference_type",
            "kind:application_types.enum",
            "kind:application_types.record",
            "kind:application_types.array",
            # A scalar under an application type reaches both refusals exactly as
            # one under a value type does, and neither reads the type's physics.
            "kind:application_types.application_type",
            "field:implementation_types.reference_type.base",
            # The reference to a table, and whether it names a `texttable`, select
            # which rule judges the value: under a table a text is an entry's name
            # (the literal rule's) and a too-wide integer is refused before the
            # entries are asked, so both selectors are read.
            "field:implementation_types.value_type.compu_method_ref",
            "field:implementation_types.reference_type.compu_method_ref",
            "kind:compu_methods.texttable",
            # A leaf reached through an implementation record or array meets both
            # refusals as one under a record or an array does; a pointer takes no
            # value at all.
            "kind:implementation_types.implementation_record",
            "kind:implementation_types.implementation_array",
            "kind:implementation_types.pointer",
            # A port's own `init` in `com_specs`, judged against its data
            # element's type, and the walk that reaches it -- the component (a
            # stub is skipped), its ports, a port's interface, its entries and the
            # element each names, and the element's policy (a queued element's
            # entry is the inapplicable rule's); the names the message quotes and
            # the entry is resolved by.
            "field:swc.swc.external",
            "field:swc.swc.name",
            "field:swc.swc.ports",
            "field:swc.port.name",
            "field:swc.port.interface",
            "field:swc.port.com_specs",
            "field:swc.port_com_spec.data_element",
            "field:swc.port_com_spec.init",
            "field:interfaces.sender_receiver.data_elements",
            "field:interfaces.data_element.name",
            "field:interfaces.data_element.sw_impl_policy",
            # The members the walk descends through on either side, the stub that
            # ends the walk with the external-type rule instead
            # (`external_type_chain`, asked first), and the value type's `base`,
            # whose resolution decides whether the scalar is judged at all.
            "field:application_types.record.fields",
            "field:application_types.record_field.type",
            "field:application_types.array.element_type",
            "field:implementation_types.implementation_record.elements",
            "field:implementation_types.implementation_element.type",
            "field:implementation_types.implementation_array.element_type",
            "kind:implementation_types.external_type",
            "field:implementation_types.value_type.base",
        ] + _UNWRITABLE_ONLY.get(rule, []) + [
            # A component variable's own `init`, walked the same way.
            "field:swc.swc.variables",
            "field:swc.variable.name",
            "field:swc.variable.init",
            "field:swc.variable.application_type",
            "field:swc.variable.implementation_type",
        ]
        assert entry["summary"] == CONSISTENCY_RULES[rule]

    def test_they_ride_an_already_opened_minor(self) -> None:
        """The contract carries the version the tree pins; adding the rules was a minor (additive) change."""
        # The version the tree carries; it moves with every contract bump.
        assert load_contract()["contractVersion"] == "12.7.1"

    @pytest.mark.parametrize("rule", RULES)
    def test_the_scan_reads_its_emission_at_the_interfaces_layer(self, rule: str) -> None:
        """The emission-layer scan resolves each site's `subjects=` expression statically.

        A record names the `init` (an interface's element, a port's or a
        variable's) and the declaration that refused the value, which may be an
        implementation or an application type."""
        contract = load_contract()
        sites = emission_layers(contract)[f"rule:{rule}"]
        assert sites, "the scan found no emission site for the rule"
        assert sorted(sites, key=sorted) == [
            frozenset({"interfaces", "implementation_types", "application_types"}),
            frozenset({"swc", "implementation_types", "application_types"}),
            # A variable's own `init` (`variable_init_records`), the port site's layers.
            frozenset({"swc", "implementation_types", "application_types"}),
        ], sites
        assert [failure for failure in cross_element_layer_failures(contract) if rule in failure] == []

    def test_they_are_not_in_the_check_name_uniqueness_group(self) -> None:
        """The prefix is not the check: the name-collision rule is `check_name_uniqueness`'s, these two are not."""
        bindings = load_binding()["rules"]

        assert bindings["rule:interface.init-constant-name-collision"]["callable"] == "_init_constant_collision_records"
        assert "_init_constant_collision_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert {bindings[f"rule:{rule}"]["callable"] for rule in self.RULES} == {"check_init_conformance"}


class TestTheAccessPointCollisionRuleArrivedWithItsCode:
    """The report of two entries of one runnable deriving one access-point ``SHORT-NAME``.

    The writer refuses the collision, so ``check-model`` must report it too. This
    class is about the table, the binding and the contract entry; the behaviour
    is ``packages/tools/tests/test_validator_name_uniqueness.py``'s.
    ``consistency-violation`` and not ``broken-reference``: every name involved
    resolves, and what fails is a rule over the resolved result. The same shape
    as ``rule:interface.init-constant-name-collision`` at the interfaces scope.
    """

    RULE = "swc.access.name-collision"

    def test_the_validator_declares_it_and_states_what_it_means(self) -> None:
        assert self.RULE in CONSISTENCY_RULES
        assert CONSISTENCY_RULES[self.RULE].strip()

    def test_it_is_answered_with_consistency_violation(self) -> None:
        assert RULE_CODES[self.RULE].code == CONSISTENCY_VIOLATION

    def test_it_is_bound_to_the_name_uniqueness_check_and_not_to_the_access_check(self) -> None:
        """Bound to `check_name_uniqueness`: a derived-name collision is a name-uniqueness question."""
        bindings = load_binding()["rules"]
        bound = bindings[f"rule:{self.RULE}"]

        assert (bound["module"], bound["callable"]) == (
            "swcraft_tools.validator.names",
            "_component_member_name_records",
        )
        assert "_component_member_name_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert callable(validator.check_name_uniqueness)
        assert {
            bindings[f"rule:swc.access.{name}"]["callable"]
            for name in (
                "unknown-port",
                "unknown-element",
                "unknown-operation",
                "direction-mismatch",
                "interface-kind-mismatch",
            )
        } == {"runnable_access_records", "_access_entry_records"}

    def test_the_contract_declares_it_over_the_walk_and_the_names_the_derivation_joins(self) -> None:
        """`appliesTo` names the lists the walk reads and the member fields the derived name is built from.

        Not `field:swc.port.name`: the verdict never reads a declared port, and a
        lookup key is excluded from a hand-authored `appliesTo`.
        `field:swc.data_read.by` is absent because `by` chooses the container,
        never the name. A stated name replaces the derived one, so the entries'
        `name` fields are read too.
        """
        contract = load_contract()
        (rule,) = [entry for entry in contract["rules"] if entry["id"] == f"rule:{self.RULE}"]
        assert rule["family"] == "cross-element"
        assert rule["appliesTo"] == [
            "field:swc.swc.runnables",
            "field:swc.runnable.reads",
            "field:swc.runnable.writes",
            "field:swc.runnable.calls",
            "field:swc.data_read.port",
            "field:swc.data_read.element",
            "field:swc.data_write.port",
            "field:swc.data_write.element",
            "field:swc.operation_call.port",
            "field:swc.operation_call.operation",
            # A polled call and a call-returned event each give the runnable a
            # result point in the same name space; `asynchronous` alone names nothing.
            "field:swc.operation_call.poll",
            "field:swc.runnable.events",
            "field:swc.call_returned_event.type",
            "field:swc.call_returned_event.port",
            "field:swc.call_returned_event.operation",
            "field:swc.data_read.name",
            "field:swc.data_write.name",
            "field:swc.operation_call.name",
            "field:swc.operation_call.result",
            "field:swc.call_returned_event.result",
            # A runnable's two variable access lists, whose entries share the
            # access point name space, and their variable and stated-name fields.
            "field:swc.runnable.reads_variables",
            "field:swc.runnable.writes_variables",
            "field:swc.variable_read.variable",
            "field:swc.variable_write.variable",
            "field:swc.variable_read.name",
            "field:swc.variable_write.name",
        ]
        assert rule["summary"] == CONSISTENCY_RULES[self.RULE]

    def test_it_opens_the_next_minor(self) -> None:
        """The contract carries the version the tree pins; one identifier added is a minor change."""
        # The version the tree carries; it moves with every contract bump.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_scan_reads_its_emission_at_the_swc_layer(self) -> None:
        """The emission-layer scan resolves the one subject, the runnable holding both entries, at `swc`."""
        contract = load_contract()
        sites = emission_layers(contract)[f"rule:{self.RULE}"]
        assert sites, "the scan found no emission site for the rule"
        assert all(site == frozenset({"swc"}) for site in sites), sites
        assert [failure for failure in cross_element_layer_failures(contract) if self.RULE in failure] == []


class TestTheHardwareRulesAreEmittedWhereTheContractSaysTheyAre:
    """The contract gate's emission-layer scan, narrowed to the `hardware.*` rules.

    `test_model_contract_conformance.py` runs the scan over every cross-element
    rule; reading it here names a hardware failure as such rather than as one
    line of a long diff. What the scan does **not** prove: it reads layers and not
    fields, a rule naming one layer where it should name two passes, and no
    `appliesTo` entry naming the far side of a reference is reachable from a
    subject at all.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def observed(cls) -> dict[str, list[frozenset[str] | None]]:
        return emission_layers()

    @pytest.mark.parametrize("rule", sorted(TestTheHardwareRulesArrivedWithTheirCodes.HARDWARE_CODES))
    def test_the_scan_reads_every_subject_of_the_rule(
        self, rule: str, observed: dict[str, list[frozenset[str] | None]]
    ) -> None:
        sites = observed.get(f"rule:{rule}")

        assert sites, f"{rule} is emitted nowhere the scan can see"
        assert all(site is not None for site in sites), f"{rule} has a subject expression the scan cannot read"

    def test_no_hardware_rule_reports_at_a_layer_it_does_not_declare(
        self, observed: dict[str, list[frozenset[str] | None]]
    ) -> None:
        failures = [
            failure
            for failure in cross_element_layer_failures(load_contract(), observed)
            if "rule:hardware." in failure
        ]

        assert failures == []

    #: The callables the thirteen are bound to: `check_hardware_topology`, which
    #: mints `hardware.layer-absent` itself, and the phases it calls, each of
    #: which mints the rules it holds.
    TOPOLOGY_CALLABLES = {
        "check_hardware_topology",
        "_hardware_identity_records",
        "_partition_reference_records",
        "_memory_key_records",
        "_accessible_records",
        "_unreachable_memory_records",
    }

    def test_the_emission_site_of_every_topology_rule_is_the_validator(self) -> None:
        """`binding.json` names the topology check or one of its phases for all thirteen.

        Every bound callable is read off `check_hardware_topology`'s syntax tree,
        so a rule bound to a function the check never reaches fails. Selected on
        the binding shape (a `callable`, which the budget rules do not carry), so
        the two halves of the prefix are told apart by the binding, not the id.
        """
        bindings = {
            rule: binding
            for rule, binding in load_binding()["rules"].items()
            if rule.startswith("rule:hardware.") and "callable" in binding
        }

        assert sorted(bindings) == sorted(
            f"rule:{rule}" for rule in TestTheHardwareRulesArrivedWithTheirCodes.TOPOLOGY_CODES
        )
        assert {binding["module"] for binding in bindings.values()} == {"swcraft_tools.validator.hardware"}
        assert {binding["callable"] for binding in bindings.values()} == self.TOPOLOGY_CALLABLES
        assert self.TOPOLOGY_CALLABLES <= validator_package.reached_functions(hardware, "check_hardware_topology")
        assert all(callable(getattr(hardware, name)) for name in self.TOPOLOGY_CALLABLES)
        assert callable(validator.check_hardware_topology)

    def test_the_three_budget_rules_are_bound_to_the_table_and_not_to_that_callable(self) -> None:
        """The budget rules are bound to `_RESOURCE_FINDING_RULES`, never to the topology check.

        They are minted in `resource_analysis.py`; a binding onto
        `check_hardware_topology` would read plausibly under the shared prefix.
        """
        rules = load_binding()["rules"]

        for rule in TestTheHardwareRulesArrivedWithTheirCodes.BUDGET_CODES:
            assert rules[f"rule:{rule}"] == {
                "module": "swcraft_tools.validator.hardware",
                "table": "_RESOURCE_FINDING_RULES",
                "key": next(key for key, value in _RESOURCE_FINDING_RULES.items() if value == rule),
            }


# ---------------------------------------------------------------------------
# 2. The rules really fire, and produce the code the table promises
# ---------------------------------------------------------------------------


def _write(model_root: Path, relative: str, document: Any) -> None:
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _leave_the_requirement_unallocated(model_root: Path) -> None:
    """Remove every component's trace link and keep the requirement: ``trace.requirement-unallocated``, a warning.

    Deleting the requirement file instead would leave the ``satisfies`` members
    dangling, which is ``trace.link-unknown-requirement``'s error.
    """
    for swc in ("ExampleSWC", "ExampleConsumerSWC"):
        path = Path(find_swc_file(str(model_root), swc))
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document.pop("satisfies", None)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _connector_direction(model_root: Path) -> None:
    _write(
        model_root,
        "connectors/Example_to_Consumer.yaml",
        # `from` still resolves to a provide port, so the `to` rule is the one reached.
        "name: Example_to_Consumer\nfrom:\n  swc: ExampleSWC\n  port: pOut\nto:\n  swc: ExampleSWC\n  port: pOut\n",
    )


def _value_type_over_value_type(model_root: Path) -> None:
    """A value type over a value type: its `base` resolves in `base_types` alone, so it is undeclared."""
    write_model_element(model_root, {"name": "ProbeValueB", "kind": "value_type", "base": "float32"})


def _reference_type_over_an_enum(model_root: Path) -> None:
    """A reference type over an enumeration: the enumeration is an application type, so the `base` is undeclared."""
    write_model_element(model_root, {"name": "ProbeEnum", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
    write_model_element(model_root, {"name": "ProbeRefB", "kind": "reference_type", "base": "ProbeEnum"})


def _map_onto_a_pointer(model_root: Path) -> None:
    """A map whose `implementation` names a pointer: the reference resolves, but to the wrong kind.

    Each data-type role resolves in its own layer, so this is the one wrong-kind
    case left."""
    write_model_element(model_root, {"name": "ProbePtr", "kind": "pointer", "target": "uint8"})
    write_model_element(model_root, {"name": "ProbePhys", "kind": "application_type"})
    write_model_element(
        model_root,
        {"name": "ProbeMapping", "maps": [{"application": "ProbePhys", "implementation": "ProbePtr"}]},
        layer="mapping_sets",
    )


def _data_type_cycle(model_root: Path) -> None:
    write_model_element(
        model_root, {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "self_", "type": "ProbeRecord"}]}
    )


def _task_period(model_root: Path) -> None:
    document = yaml.safe_load((model_root / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8"))
    document["period"] = 0.003
    _write(model_root, "tasks/Task_10ms.yaml", document)


def _two_tasks_one_runnable(model_root: Path) -> None:
    _write(
        model_root,
        "tasks/Task_Probe.yaml",
        "name: Task_Probe\nperiod: 0.01\npriority: 4\npartition: PARTITION0\nwcet: 100us\n"
        "runnables:\n  - swc: ExampleConsumerSWC\n    runnable: ExampleConsumerRun\n",
    )


def _sequence_step_order(model_root: Path) -> None:
    _write(
        model_root,
        "swc/example/ExampleSWC.yaml",
        "name: ExampleSWC\ntype: application\nports:\n  - name: pOut\n    direction: provide\n"
        "    interface: IExample\nrunnables:\n  - name: ExampleRun\n    min_start_interval: 0.01\n"
        "    events:\n      - type: timing\n        period: 0.01\n"
        "  - name: SecondRun\n    events:\n      - type: timing\n        period: 0.01\n"
        "memory:\n  RAM0: 2048\nhandle_termination_and_restart: no_support\n",
    )
    _write(
        model_root,
        "tasks/Task_10ms.yaml",
        "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION0\nwcet: 1200us\n"
        "runnables:\n  - swc: ExampleSWC\n    runnable: ExampleRun\n"
        "  - swc: ExampleSWC\n    runnable: SecondRun\n",
    )
    _write(
        model_root,
        "sequences/task10ms_consumer_cycle.yaml",
        "name: task10ms_consumer_cycle\ntask: Task_10ms\ntitle: t\n"
        "steps:\n  - swc: ExampleSWC\n    runnable: SecondRun\n"
        "  - swc: ExampleSWC\n    runnable: ExampleRun\n",
    )


def _threshold_breach(model_root: Path) -> None:
    _write(
        model_root,
        "hardware/RAM0.yaml",
        {
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 16,
            "accessible": ["CORE0"],
            "threshold": 0.5,
            "enforcement": "strict",
        },
    )


def _duplicate_hardware_name(model_root: Path) -> None:
    """A second file claiming an identity the layer already declares.

    A ``core`` and not a ``processor``: a second processor would trip
    ``hardware.multiple-processors`` as well. Its ``threshold`` is the base
    core's, so the CPU budget is unchanged.
    """
    _write(
        model_root,
        "hardware/CORE0_again.yaml",
        {"name": "CORE0", "kind": "core", "threshold": 0.7, "enforcement": "strict"},
    )


def _duplicate_port_name(model_root: Path) -> None:
    """Two ports of one component claiming one name.

    Both are ``provide``: a ``require`` twin would also fire
    ``connector.from-not-provide`` through the connector's ``from``.
    """
    _write(
        model_root,
        "swc/example/ExampleSWC.yaml",
        "name: ExampleSWC\ntype: application\nports:\n  - name: pOut\n    direction: provide\n"
        "    interface: IExample\n  - name: pOut\n    direction: provide\n    interface: IExample\n"
        "runnables:\n  - name: ExampleRun\n    min_start_interval: 0.01\n"
        "    events:\n      - type: timing\n        period: 0.01\n"
        "memory:\n  RAM0: 2048\nhandle_termination_and_restart: no_support\n",
    )


def _subject(layer: str, element: str | None, *steps: tuple[str, Any]) -> Subject:
    """A :class:`Subject` from a terse ``(kind, value)`` spelling."""
    return Subject(layer, element, tuple(SubjectStep(kind, value) for kind, value in steps))


#: The consumer's one task entry as a finding names it: the composite triple,
#: the absent selector ``None``.
_CONSUMER_PAIR = {"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}

#: Nine checks answered with ``consistency-violation``, each with a model built
#: to break exactly it, the rule id that must answer for it, and the exact
#: subjects each record carries — one tuple per record the rule produces. The
#: subjects become the rejection's ``$defs/address``; a rule pointing at the wrong
#: element sends the repair to the wrong place, invisibly to a code-only
#: assertion.
FORMERLY_UNDIAGNOSED: tuple[tuple[str, Callable[[Path], None], str, tuple[tuple[Subject, ...], ...]], ...] = (
    (
        "connector: a `to` endpoint that is not a require port",
        _connector_direction,
        "connector.to-not-require",
        ((_subject("connectors", "Example_to_Consumer", ("field", "to"), ("field", "port")),),),
    ),
    (
        "mapping sets: a map onto a pointer",
        _map_onto_a_pointer,
        "mapping-set.pointer-mapped",
        # The `implementation` field of the offending map, not the type it
        # names: the reference resolves, and what is wrong is which kind it
        # points at.
        (
            (
                _subject(
                    "mapping_sets",
                    "ProbeMapping",
                    ("field", "maps"),
                    ("member", "ProbePhys"),
                    ("field", "implementation"),
                ),
            ),
        ),
    ),
    (
        "data types: a dependency cycle",
        _data_type_cycle,
        "data-type.cycle",
        ((_subject("application_types", "ProbeRecord"),),),
    ),
    (
        "tasks: a runnable period that is no multiple",
        _task_period,
        "task.runnable-period-not-multiple",
        # Both ends of the arithmetic: the task period and the event period it no
        # longer divides. Either is a legal repair, so both are named.
        (
            (
                _subject("tasks", "Task_10ms", ("field", "period")),
                _subject(
                    "swc",
                    "ExampleConsumerSWC",
                    ("field", "runnables"),
                    ("member", "ExampleConsumerRun"),
                    ("field", "events"),
                    ("member", {"type": "timing", "period": 0.01}),
                ),
            ),
        ),
    ),
    (
        "tasks: one pair mapped by two tasks",
        _two_tasks_one_runnable,
        "task.runnable-mapped-to-multiple-tasks",
        # Both mappings that claim the pair: the protocol cannot say which one
        # the repair drops without naming both.
        (
            (
                _subject("tasks", "Task_10ms", ("field", "runnables"), ("member", _CONSUMER_PAIR)),
                _subject("tasks", "Task_Probe", ("field", "runnables"), ("member", _CONSUMER_PAIR)),
            ),
        ),
    ),
    (
        "sequences: the subsequence rule",
        _sequence_step_order,
        "sequence.step-order",
        # The whole collection, never one step: an address has no ordinal, so
        # "these are in the wrong order" is a statement about `steps` itself.
        ((_subject("sequences", "task10ms_consumer_cycle", ("field", "steps")),),),
    ),
    (
        "hardware: a threshold breach",
        _threshold_breach,
        "hardware.threshold-breach",
        # The memory that is too small and every budget drawn on it: enlarging the
        # memory and shrinking a consumer are both legal repairs.
        (
            (
                _subject("hardware", "RAM0", ("field", "size")),
                _subject("swc", "ExampleConsumerSWC", ("field", "memory"), ("key", "RAM0")),
                _subject("swc", "ExampleSWC", ("field", "memory"), ("key", "RAM0")),
            ),
        ),
    ),
    (
        "hardware: one identity declared in two files",
        _duplicate_hardware_name,
        "hardware.duplicate-name",
        ((_subject("hardware", "CORE0"),),),
    ),
    (
        "names: a duplicate nested name",
        _duplicate_port_name,
        "name.duplicate-port",
        ((_subject("swc", "ExampleSWC", ("field", "ports"), ("member", "pOut")),),),
    ),
)


class TestTheFormerlyUndiagnosedChecksAreAnswered:
    """Each of the nine, end to end, through the validator the engine really calls.

    Each case breaks exactly one rule, asserts the validator reports that rule
    and no other (equality, not membership, so a row that stops isolating fails),
    and that the translation answers ``consistency-violation`` at an address.
    What the isolation does **not** reach: a rule firing more than once inside
    one row, which the subject assertion covers instead.
    """

    @pytest.fixture
    def model_root(self, tmp_path: Path) -> Path:
        return fixtures.build_fixture(tmp_path / "project", "read-swc-element") / "model"

    @pytest.mark.parametrize(
        ("label", "break_it", "rule", "subjects"),
        FORMERLY_UNDIAGNOSED,
        ids=[entry[2] for entry in FORMERLY_UNDIAGNOSED],
    )
    def test_the_check_reports_its_rule_and_the_protocol_answers_with_a_code(
        self,
        model_root: Path,
        label: str,
        break_it: Callable[[Path], None],
        rule: str,
        subjects: tuple[tuple[Subject, ...], ...],
    ) -> None:
        break_it(model_root)
        result = check_cross_layer_consistency(str(model_root))

        assert not result.consistent, f"{label}: the fixture no longer breaks the check it exists to break"
        assert {record.rule for record in result.records} == {rule}, (
            f"{label}: expected {rule!r} and nothing else, got "
            f"{sorted({record.rule for record in result.records})} — the fixture stopped isolating "
            "the rule it exists to isolate"
        )

        # Diagnose a result narrowed to this rule's records, so the answered set
        # is this rule's code and not every violation's.
        own = ConsistencyResult(
            consistent=False,
            records=tuple(record for record in result.records if record.rule == rule),
        )
        answered = {violation.error for violation in diagnose(own)}
        assert answered == {CONSISTENCY_VIOLATION}, f"{label}: {sorted(answered)}"

    @pytest.mark.parametrize(
        ("label", "break_it", "rule", "subjects"),
        FORMERLY_UNDIAGNOSED,
        ids=[entry[2] for entry in FORMERLY_UNDIAGNOSED],
    )
    def test_the_rule_names_the_model_places_it_is_about(
        self,
        model_root: Path,
        label: str,
        break_it: Callable[[Path], None],
        rule: str,
        subjects: tuple[tuple[Subject, ...], ...],
    ) -> None:
        """The exact subjects, per record — the addresses a rejection is built from.

        Unordered across records, ordered within one (the protocol renders it as
        the ``addresses`` list). ``repr``, because a member identity may be an
        unhashable dict.
        """
        break_it(model_root)
        result = check_cross_layer_consistency(str(model_root))
        observed = sorted(repr(record.subjects) for record in result.records if record.rule == rule)
        assert observed == sorted(repr(expected) for expected in subjects), label

    @pytest.mark.parametrize(
        ("label", "break_it", "rule", "subjects"),
        FORMERLY_UNDIAGNOSED,
        ids=[entry[2] for entry in FORMERLY_UNDIAGNOSED],
    )
    def test_the_violation_carries_an_address(
        self,
        model_root: Path,
        label: str,
        break_it: Callable[[Path], None],
        rule: str,
        subjects: tuple[tuple[Subject, ...], ...],
    ) -> None:
        # A code with no address is half an answer: every one of these failures
        # has a responsible address.
        break_it(model_root)
        violations = diagnose(check_cross_layer_consistency(str(model_root)))
        addressed = [violation for violation in violations if violation.addresses]
        assert addressed, f"{label}: {violations}"

    def test_every_row_states_at_least_one_subject(self) -> None:
        # A row stating `()` would make the subject case pass over nothing.
        for label, _break_it, rule, subjects in FORMERLY_UNDIAGNOSED:
            assert subjects, f"{label}: {rule} states no subjects"
            for record in subjects:
                assert record, f"{label}: {rule} states an empty subject tuple"

    def test_an_unallocated_requirement_is_a_warning_the_protocol_never_sees(self, model_root: Path) -> None:
        """A warning lands in ``warnings``, the model stays consistent, and neither diagnosis nor pass D answers."""
        _leave_the_requirement_unallocated(model_root)
        result = check_cross_layer_consistency(str(model_root))

        assert result.consistent is True
        assert result.records == ()
        assert [(record.rule, record.subjects) for record in result.warnings] == [
            ("trace.requirement-unallocated", (_subject("requirements", "REQ_EXAMPLE_001"),))
        ]
        assert "trace.requirement-unallocated" not in RULE_CODES
        assert "cross-layer.swc-without-requirement-link" not in RULE_CODES
        assert diagnose(result) == ()
        assert pass_d(str(model_root)) == ()

    def test_a_value_type_over_a_value_type_is_an_undeclared_base(self, model_root: Path) -> None:
        """A value type over a value type is a broken reference at its ``base``; the retired rule id is gone."""
        _value_type_over_value_type(model_root)
        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {"data-type.value-type-undeclared-base"}
        (record,) = result.records
        assert record.subjects == (_subject("implementation_types", "ProbeValueB", ("field", "base")),)
        assert {violation.error for violation in diagnose(result)} == {BROKEN_REFERENCE}
        assert "data-type.value-type-base-not-base-type" not in CONSISTENCY_RULES
        assert "data-type.value-type-base-not-base-type" not in RULE_CODES

    def test_a_reference_type_over_an_enumeration_is_an_undeclared_base(self, model_root: Path) -> None:
        """A reference type over an enumeration is a broken reference at its ``base``; the retired rule id is gone."""
        _reference_type_over_an_enum(model_root)
        result = check_cross_layer_consistency(str(model_root))

        assert {record.rule for record in result.records} == {"data-type.reference-type-undeclared-base"}
        (record,) = result.records
        assert record.subjects == (_subject("implementation_types", "ProbeRefB", ("field", "base")),)
        assert {violation.error for violation in diagnose(result)} == {BROKEN_REFERENCE}
        retired = "data-type.reference-type-base-not-implementation-type"
        assert retired not in CONSISTENCY_RULES
        assert retired not in RULE_CODES
        assert f"rule:{retired}" not in {entry["id"] for entry in load_contract()["rules"]}

    def test_a_valid_model_answers_nothing(self, model_root: Path) -> None:
        # The negative control: without a break, nothing fires.
        result = check_cross_layer_consistency(str(model_root))
        assert result.consistent and diagnose(result) == ()


#: The nine ``rule:interface.init-*`` rules, audited once against what each
#: reads to reach its verdict: the prototype's type and the reference chain, the
#: members it descends through on either side, the stub that ends it with the
#: external-type rule, the selectors that decide which rule judges a value (a
#: type's table and whether it is a ``texttable``, an enumeration's table), and
#: the values the rule compares.
_INIT_RULE_READS: dict[str, tuple[str, ...]] = {
    "rule:interface.init-shape-mismatch": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.reference_type.compu_method_ref",
        "kind:compu_methods.texttable",
        "field:implementation_types.value_type.base",
        "field:implementation_types.value_type.value_min",
        "field:implementation_types.value_type.value_max",
        "field:application_types.application_type.range",
        "field:application_types.range.min",
        "field:application_types.range.max",
        "field:application_types.application_type.data_constr_ref",
        "field:data_constraints.data_constr.min",
        "field:data_constraints.data_constr.max",
        "kind:implementation_types.value_type",
        "kind:application_types.application_type",
        "kind:application_types.enum",
        "field:application_types.enum.compu_method_ref",
        "kind:implementation_types.pointer",
    ),
    "rule:interface.init-unknown-enum-literal": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.reference_type.compu_method_ref",
        "kind:compu_methods.texttable",
        "kind:application_types.enum",
        "field:application_types.enum.literals",
        "field:application_types.enum_literal.name",
        "field:application_types.enum.compu_method_ref",
        "kind:implementation_types.value_type",
        "field:compu_methods.texttable.entries",
        "field:compu_methods.table_entry.name",
    ),
    "rule:interface.init-record-field-mismatch": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:application_types.record_field.name",
        "field:implementation_types.implementation_element.name",
    ),
    "rule:interface.init-array-length-mismatch": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:application_types.array.length",
        "field:implementation_types.implementation_array.length",
    ),
    "rule:interface.init-out-of-range": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.reference_type.compu_method_ref",
        "kind:compu_methods.texttable",
        "field:implementation_types.value_type.base",
        "field:implementation_types.value_type.value_min",
        "field:implementation_types.value_type.value_max",
        "field:application_types.application_type.range",
        "field:application_types.range.min",
        "field:application_types.range.max",
        "field:application_types.application_type.data_constr_ref",
        "field:data_constraints.data_constr.min",
        "field:data_constraints.data_constr.max",
        "kind:implementation_types.value_type",
        "kind:application_types.application_type",
    ),
    "rule:interface.init-string-scalar": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.reference_type.compu_method_ref",
        "kind:compu_methods.texttable",
        "field:implementation_types.value_type.base",
        "kind:implementation_types.value_type",
        "kind:application_types.application_type",
    ),
    "rule:interface.init-unwritable-integer": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:application_types.record",
        "field:application_types.record.fields",
        "field:application_types.record_field.type",
        "kind:application_types.array",
        "field:application_types.array.element_type",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.reference_type.compu_method_ref",
        "kind:compu_methods.texttable",
        "field:implementation_types.value_type.base",
        "kind:implementation_types.value_type",
        "kind:application_types.application_type",
        "kind:application_types.enum",
        "field:application_types.enum.compu_method_ref",
    ),
    "rule:interface.init-on-external-type": (
        "kind:implementation_types.reference_type",
        "field:implementation_types.reference_type.base",
        "kind:implementation_types.implementation_record",
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.type",
        "kind:implementation_types.implementation_array",
        "field:implementation_types.implementation_array.element_type",
        "kind:implementation_types.external_type",
    ),
    "rule:interface.init-constant-name-collision": ("field:interfaces.sender_receiver.data_elements",),
}


class TestTheInitRulesNameWhatTheyRead:
    """Every ``rule:interface.init-*`` rule declares what its verdict is reached through.

    **What it does not reach**: it is a containment check over the audit's own
    table, so a read the audit missed passes, and an entry beyond the table is
    not refused; lookup-key identities and fields a message only quotes stay out
    by convention, not by this test. ``kind:compu_methods.linear`` is out by
    convention too: the walk asks only whether a referenced compu method is a
    texttable and reads no field of a linear one, so the selector is declared
    once as ``kind:compu_methods.texttable``.
    """

    @pytest.mark.parametrize("rule", sorted(_INIT_RULE_READS))
    def test_its_applies_to_names_every_entry_the_walk_reads(self, rule: str) -> None:
        (entry,) = [candidate for candidate in load_contract()["rules"] if candidate["id"] == rule]
        assert [read for read in _INIT_RULE_READS[rule] if read not in entry["appliesTo"]] == []
