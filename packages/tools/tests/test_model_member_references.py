"""The member-reference declaration: a field's value names a member of a declared collection.

``fieldDecl.memberReference`` says, for example, that a runnable access's ``port`` names one of its own
component's ``ports``, and its ``element`` or ``operation`` a data element or an operation of that
port's interface. The kernel registry publishes each as a
:class:`~swcraft_tools.model_registry.MemberReferenceEdge` and refuses at load a declaration a reader
could not reach as declared; this module holds:

- the shipped edges and their facts, and the field each is published on;
- one planted refusal per load-time check, each through a synthetic contract;
- **the agreement test, the independent oracle**: the declaration restates what
  ``validator.check_runnable_access`` enforces, and each rule's hand-authored ``appliesTo`` must name
  the source field and, per listed collection, the collection's field or its member identity field. A
  non-inertness twin plants a narrowed ``appliesTo`` and asserts the check reports it.

**What this does not reach.** The meta-schema's acceptance of the key and its three-way exclusion are
``test_model_contract.py``'s perturbation rows; whether a reader *navigates* by the declaration is the
extension's tests' and the generator's (``test_editor_schema.py``). The agreement test compares
identifiers, not behaviour: it proves the rule names what the declaration names, never that the
validator's code reads exactly those collections.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.model_registry import (
    MemberReferenceEdge,
    ModelRegistry,
    ModelRegistryError,
    build_model_registry,
    production_registry,
)

_DATA_ELEMENTS = (
    "collection:interfaces.sender_receiver.data_elements",
    "collection:interfaces.external_interface.data_elements",
)
_OPERATIONS = (
    "collection:interfaces.client_server.operations",
    "collection:interfaces.external_interface.operations",
)

#: The rule each declared source field restates, by the field's name. Written out rather than derived,
#: because the pairing IS the claim under test: a member reference declared on a field no row names
#: fails here until someone says which rule enforces it.
_RESTATED_RULE = {
    "port": "rule:swc.access.unknown-port",
    "element": "rule:swc.access.unknown-element",
    "operation": "rule:swc.access.unknown-operation",
    # A variable access's `variable`, `[constr_2026]`'s rule.
    "variable": "rule:swc.variable-access.unknown-variable",
    # An exclusive area entry's `area`, `unknown-area`'s rule.
    "area": "rule:swc.exclusive-area.unknown-area",
}

#: The source fields whose name another kind shares but whose rule is their own, by field id, read
#: before :data:`_RESTATED_RULE`: a data-received event names `port` and `element` as a read does, and
#: its own resolution family (`rule:swc.data-event.*`) enforces them.
_RESTATED_RULE_BY_FIELD = {
    "field:swc.data_event.port": "rule:swc.data-event.unknown-port",
    "field:swc.data_event.element": "rule:swc.data-event.unknown-element",
    # A call-returned event names `port` and `operation` as a call does, and resolves them through the
    # call it names.
    "field:swc.call_returned_event.port": "rule:swc.call-returned-event.unknown-call",
    "field:swc.call_returned_event.operation": "rule:swc.call-returned-event.unknown-call",
}


def _field_decl(contract: dict[str, Any], field_id: str) -> dict[str, Any]:
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field
    raise AssertionError(field_id)


def _build(contract: dict[str, Any]) -> ModelRegistry:
    return build_model_registry(contract=contract, binding=load_binding())


def _planted(field_id: str, **declaration: Any) -> dict[str, Any]:
    """The shipped contract with *field_id*'s member reference replaced by *declaration*."""
    contract = copy.deepcopy(load_contract())
    _field_decl(contract, field_id)["memberReference"] = {"summary": "Planted.", **declaration}
    return contract


# ---------------------------------------------------------------------------
# The shipped declaration
# ---------------------------------------------------------------------------


def test_the_shipped_member_references_are_the_six_runnable_access_fields() -> None:
    """The shipped member references, in contract order, each with its collections and its `via`."""
    edges = production_registry().member_references()
    assert [(edge.field_id, edge.source_layer, edge.source_kind, edge.source_field, edge.via) for edge in edges] == [
        # The data-received event's pair, its kind declared before the reads'.
        ("field:swc.data_event.port", "swc", "data_event", "port", None),
        ("field:swc.data_event.element", "swc", "data_event", "element", "port"),
        # The call-returned event's pair, its kind declared after the data event's.
        ("field:swc.call_returned_event.port", "swc", "call_returned_event", "port", None),
        ("field:swc.call_returned_event.operation", "swc", "call_returned_event", "operation", "port"),
        ("field:swc.data_read.port", "swc", "data_read", "port", None),
        ("field:swc.data_read.element", "swc", "data_read", "element", "port"),
        ("field:swc.data_write.port", "swc", "data_write", "port", None),
        ("field:swc.data_write.element", "swc", "data_write", "element", "port"),
        ("field:swc.operation_call.port", "swc", "operation_call", "port", None),
        ("field:swc.operation_call.operation", "swc", "operation_call", "operation", "port"),
        # The two variable access fields, into the component's own variables.
        ("field:swc.variable_read.variable", "swc", "variable_read", "variable", None),
        ("field:swc.variable_write.variable", "swc", "variable_write", "variable", None),
        # The two exclusive area entry fields, into the component's own areas.
        ("field:swc.can_enter_area.area", "swc", "can_enter_area", "area", None),
        ("field:swc.runs_inside_area.area", "swc", "runs_inside_area", "area", None),
    ]
    expected_collections = {
        "port": (("collection:swc.swc.ports", "swc", "swc", "ports", "name"),),
        "variable": (("collection:swc.swc.variables", "swc", "swc", "variables", "name"),),
        "area": (("collection:swc.swc.exclusive_areas", "swc", "swc", "exclusive_areas", "name"),),
        "element": (
            (_DATA_ELEMENTS[0], "interfaces", "sender_receiver", "data_elements", "name"),
            (_DATA_ELEMENTS[1], "interfaces", "external_interface", "data_elements", None),
        ),
        "operation": (
            (_OPERATIONS[0], "interfaces", "client_server", "operations", "name"),
            (_OPERATIONS[1], "interfaces", "external_interface", "operations", None),
        ),
    }
    for edge in edges:
        assert (
            tuple(
                (target.collection_id, target.layer, target.kind, target.field, target.identity_field)
                for target in edge.collections
            )
            == expected_collections[edge.source_field]
        ), edge.field_id
        assert edge.summary, edge.field_id


def test_a_member_reference_is_published_on_the_field_that_declares_it() -> None:
    """The fact rides the field, as a relation does — and no other field carries one."""
    registry = production_registry()
    declared = {edge.field_id: edge for edge in registry.member_references()}
    carrying = {
        facts.id: facts.member_reference
        for kind in registry.kinds.values()
        for facts in kind.fields
        if facts.member_reference is not None
    }
    assert carrying == declared


def test_no_field_declares_two_ways_of_naming() -> None:
    """At most one of `reference`, `relation` and `member_reference` per field (the meta-schema's exclusion)."""
    for kind in production_registry().kinds.values():
        for facts in kind.fields:
            declared = [fact for fact in (facts.reference, facts.relation, facts.member_reference) if fact is not None]
            assert len(declared) <= 1, facts.id


def test_a_member_reference_adds_no_edge_to_the_reference_topology() -> None:
    """Neither `references()` nor `relations()` carries a member-reference field: `impact` walks neither."""
    registry = production_registry()
    member_fields = {edge.field_id for edge in registry.member_references()}
    assert member_fields.isdisjoint({edge.field_id for edge in registry.references()})
    assert member_fields.isdisjoint({edge.field_id for edge in registry.relations()})


def test_the_member_reference_edge_is_a_frozen_value() -> None:
    edge = production_registry().member_references()[0]
    assert isinstance(edge, MemberReferenceEdge)
    with pytest.raises(AttributeError):
        edge.via = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The load-time checks, each against a planted declaration
# ---------------------------------------------------------------------------


def test_a_listed_identifier_naming_no_declared_collection_is_refused() -> None:
    contract = _planted("field:swc.data_read.port", collections=["collection:swc.swc.portz"])
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.port.*names no declared collection"):
        _build(contract)


def test_a_via_naming_no_sibling_field_is_refused() -> None:
    contract = _planted("field:swc.data_read.element", collections=list(_DATA_ELEMENTS), via="prt")
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.element.*'prt'.*does not declare"):
        _build(contract)


def test_a_via_whose_sibling_declares_no_member_reference_is_refused() -> None:
    contract = copy.deepcopy(load_contract())
    del _field_decl(contract, "field:swc.data_read.port")["memberReference"]
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.element.*declares no member reference"):
        _build(contract)


def test_a_via_sibling_whose_member_kind_has_no_reference_into_the_layer_is_refused() -> None:
    """The port carries one reference into `interfaces`; with it gone, nothing names the interface."""
    contract = copy.deepcopy(load_contract())
    del _field_decl(contract, "field:swc.port.interface")["reference"]
    with pytest.raises(
        ModelRegistryError, match=r"field:swc\.data_(?:event|read)\.element.*'port' carries 0 references"
    ):
        _build(contract)


def test_a_via_sibling_whose_member_kind_has_two_references_into_the_layer_is_refused() -> None:
    """A second reference into `interfaces` on the port: which one names the interface is undecided."""
    contract = copy.deepcopy(load_contract())
    _field_decl(contract, "field:swc.port.observed_interface")["reference"] = {
        "layer": "interfaces",
        "identityField": "name",
    }
    with pytest.raises(
        ModelRegistryError, match=r"field:swc\.data_(?:event|read)\.element.*'port' carries 2 references"
    ):
        _build(contract)


def test_a_via_sibling_over_a_whole_value_list_is_refused() -> None:
    """A whole-value list's member is a bare value: no member kind, so no reference names the element."""
    contract = _planted("field:swc.data_read.port", collections=["collection:swc.swc.data_type_mappings"])
    with pytest.raises(
        ModelRegistryError,
        match=r"field:swc\.data_read\.element.*'collection:swc\.swc\.data_type_mappings', a whole-value list",
    ):
        _build(contract)


def test_a_via_through_a_sibling_reached_through_via_is_refused() -> None:
    """A reader follows one `via` step; a chain would load and navigate nowhere."""
    contract = _planted("field:swc.data_read.port", collections=list(_DATA_ELEMENTS), via="element")
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.(port|element).*never a chain"):
        _build(contract)


def test_a_via_over_collections_of_two_layers_is_refused() -> None:
    contract = _planted(
        "field:swc.data_read.element",
        collections=[_DATA_ELEMENTS[0], "collection:swc.swc.ports"],
        via="port",
    )
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.element.*collections of 2 layers"):
        _build(contract)


@pytest.mark.parametrize(
    "collection",
    [_DATA_ELEMENTS[0], "collection:swc.port.com_specs"],
    ids=["another-layer", "member-only-owner"],
)
def test_an_owner_scoped_collection_off_the_holding_element_is_refused(collection: str) -> None:
    """Without `via` the collection is on the element holding the field: same layer, top-level kind."""
    contract = _planted("field:swc.data_read.port", collections=[collection])
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.port.*without `via`"):
        _build(contract)


def test_a_field_collection_whose_identity_field_is_absent_is_refused() -> None:
    contract = copy.deepcopy(load_contract())
    del _field_decl(contract, "field:swc.swc.ports")["collection"]["identityField"]
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_(?:event|read)\.port.*declares no identity field"):
        _build(contract)


def test_a_collection_no_single_value_identifies_is_refused() -> None:
    """A composite collection (a runnable's reads) has no one value a name could equal."""
    contract = _planted("field:swc.data_read.port", collections=["collection:swc.runnable.reads"])
    with pytest.raises(ModelRegistryError, match=r"field:swc\.data_read\.port.*no single value identifies"):
        _build(contract)


def test_the_shipped_contract_passes_every_check() -> None:
    """The positive control of the refusals above: the shipped declarations build."""
    registry = _build(load_contract())
    # One per shipped member-reference field; a new declaration moves it.
    assert len(registry.member_references()) == 14


# ---------------------------------------------------------------------------
# The agreement test: the declaration and the rules the validator reports
# ---------------------------------------------------------------------------


def _agreement_failures(contract: dict[str, Any], registry: ModelRegistry) -> list[str]:
    """Every way a member reference and the rule its field restates disagree.

    Read from the contract's own `rules` and the registry's edges: the rule's `appliesTo` must name the
    source field and, per listed collection, the collection's field id or, for a `field` collection, a
    member kind's identity field (`field:swc.port.name` for the ports).
    """
    applies_to = {rule["id"]: set(rule["appliesTo"]) for rule in contract["rules"]}
    failures: list[str] = []
    for edge in registry.member_references():
        rule = _RESTATED_RULE_BY_FIELD.get(edge.field_id) or _RESTATED_RULE.get(edge.source_field)
        if rule is None:
            failures.append(f"{edge.field_id}: no rule is paired with a member reference on '{edge.source_field}'")
            continue
        named = applies_to.get(rule, set())
        if edge.field_id not in named:
            failures.append(f"{edge.field_id}: {rule}'s appliesTo does not name the source field")
        for target in edge.collections:
            accepted = {f"field:{target.layer}.{target.kind}.{target.field}"}
            if target.identity_field is not None:
                members = registry.field_facts(target.kind, target.field)
                assert members is not None and members.collection is not None
                accepted |= {
                    f"field:{target.layer}.{member}.{target.identity_field}"
                    for member in members.collection.member_kinds
                }
            if not accepted & named:
                failures.append(f"{edge.field_id}: {rule}'s appliesTo names none of {sorted(accepted)}")
    return failures


def test_each_member_reference_agrees_with_the_rule_its_field_restates() -> None:
    assert _agreement_failures(load_contract(), production_registry()) == []


def test_every_restated_rule_is_declared() -> None:
    declared = {rule["id"] for rule in load_contract()["rules"]}
    assert set(_RESTATED_RULE.values()) <= declared


@pytest.mark.parametrize(
    ("rule", "removed", "expected"),
    [
        ("rule:swc.access.unknown-port", "field:swc.port.name", "field:swc.swc.ports"),
        ("rule:swc.access.unknown-element", "field:swc.data_write.element", "does not name the source field"),
        (
            "rule:swc.access.unknown-operation",
            "field:interfaces.external_interface.operations",
            "external_interface.operations",
        ),
    ],
    ids=["port-identity", "element-source", "stub-operations"],
)
def test_the_agreement_check_is_not_inert(rule: str, removed: str, expected: str) -> None:
    """A rule narrowed by one entry is reported, naming what it lost."""
    contract = copy.deepcopy(load_contract())
    for declared in contract["rules"]:
        if declared["id"] == rule:
            declared["appliesTo"].remove(removed)
    failures = _agreement_failures(contract, production_registry())
    assert failures and any(expected in failure for failure in failures), failures
