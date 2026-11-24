"""The composition layer's schema classes, and the reference fields beside them.

What is asserted here is the **loading** half: the shapes
`schema.py` accepts, the shapes it refuses, and the defaults a file that omits a
key comes back with. What a value *means* against the rest of the model — that a
member's `type` names a declared component, that exactly one composition is the
root, that a delegation's two ports agree in direction — is `check-model`'s;
nothing here stands in for it.

Most of these cases are about an element that names a type living in one of two
layers:

* **The member.** `name` and the type field are always written, even when
  they are equal, and the type field is `type` for a component or `composition`
  for another composition. Exactly one of the two is set; neither and both are
  refused at load, which is what `exactly_one_type_field` is.
* **The endpoint.** A connector endpoint names the TYPE in one of the same two
  fields, under the same exactly-one rule. `Connector.composition` is the
  reference field whose `None` means
  something: the ROOT composition, resolved where the model is in hand and never
  written into the file.

The write side — that the serializer really leaves the unset key out — is
asserted here through `connector_to_dict` and `swc_to_dict` rather than through
`write_element`, because those two are the functions the ARXML importer and the
mutation API share, and a `null` written by either is a document the loader then
refuses on the way back in.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError
from swcraft_tools.contract import load_contract
from swcraft_tools.schema import (
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    Delegation,
    SWComponent,
)
from swcraft_tools.writer.yaml_documents import connector_to_dict, swc_to_dict

# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_a_composition_needs_only_a_name_and_a_title() -> None:
    composition = Composition(name="Root", title="Root composition")

    assert composition.root is False
    assert composition.description == ""
    assert (composition.ports, composition.members, composition.delegations) == ([], [], [])


def test_a_composition_without_a_title_is_refused() -> None:
    with pytest.raises(ValidationError):
        Composition(name="Root")


@pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", ".."])
def test_a_composition_name_that_is_blank_or_a_path_is_refused(name: str) -> None:
    """The name is the filename — `compositions/{name}.yaml` — so it is a segment.

    The same guard every other file-backed element carries, asserted here rather
    than assumed: a composition is a top-level element and the traversal rule is
    not inherited from anywhere.
    """
    with pytest.raises(ValidationError):
        Composition(name=name, title="T")


def test_a_composition_carries_its_three_collections() -> None:
    composition = Composition(
        name="Root",
        title="Root composition",
        root=True,
        ports=[CompositionPort(name="pOut", direction="provide", interface="ISpeed")],
        members=[CompositionMember(name="Sensor", type="SpeedSensor")],
        delegations=[Delegation(name="dOut", port="pOut", member="Sensor", member_port="pSpeed")],
    )

    assert composition.root is True
    assert composition.ports[0].description == ""
    assert composition.members[0].composition is None
    assert composition.delegations[0].member_port == "pSpeed"


# ---------------------------------------------------------------------------
# CompositionPort
# ---------------------------------------------------------------------------


def test_a_composition_port_declares_the_same_four_fields_as_a_component_port() -> None:
    port = CompositionPort(name="pOut", direction="provide", interface="ISpeed")

    assert tuple(CompositionPort.model_fields) == ("name", "direction", "interface", "description")
    assert (port.name, port.direction, port.interface, port.description) == ("pOut", "provide", "ISpeed", "")


def test_a_composition_port_direction_is_the_two_component_port_takes() -> None:
    with pytest.raises(ValidationError):
        CompositionPort(name="pOut", direction="delegate", interface="ISpeed")


# ---------------------------------------------------------------------------
# CompositionMember
# ---------------------------------------------------------------------------


def test_a_member_may_name_a_type_that_differs_from_its_own_name() -> None:
    """The pair form's whole reserve, and the one AUTOSAR carries for every prototype."""
    member = CompositionMember(name="Alpha", type="Beta")

    assert (member.name, member.type, member.composition) == ("Alpha", "Beta", None)


def test_a_member_may_name_another_composition_instead() -> None:
    member = CompositionMember(name="Inner", composition="LeafComposition")

    assert (member.type, member.composition) == (None, "LeafComposition")


def test_a_member_declaring_both_type_fields_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        CompositionMember(name="Inner", type="SpeedSensor", composition="LeafComposition")


def test_a_member_declaring_neither_type_field_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        CompositionMember(name="Inner")


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["name", "port", "member", "member_port"])
def test_a_delegation_refuses_a_blank_in_any_of_its_four_names(field: str) -> None:
    document = {"name": "dOut", "port": "pOut", "member": "Sensor", "member_port": "pSpeed"} | {field: "  "}
    with pytest.raises(ValidationError, match="must not be empty"):
        Delegation(**document)


# ---------------------------------------------------------------------------
# ConnectorEndpoint and Connector
# ---------------------------------------------------------------------------


def test_an_endpoint_names_a_component_or_a_composition_and_never_both() -> None:
    assert ConnectorEndpoint(swc="SpeedSensor", port="pOut").composition is None
    assert ConnectorEndpoint(composition="ControlSw", port="pOut").swc is None

    with pytest.raises(ValidationError, match="exactly one"):
        ConnectorEndpoint(swc="SpeedSensor", composition="ControlSw", port="pOut")
    with pytest.raises(ValidationError, match="exactly one"):
        ConnectorEndpoint(port="pOut")


def test_an_endpoints_declared_fields_still_may_not_be_blank() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        ConnectorEndpoint(swc="  ", port="pOut")
    with pytest.raises(ValidationError, match="must not be empty"):
        ConnectorEndpoint(swc="SpeedSensor", port="  ")


def _connector(**overrides: object) -> Connector:
    document: dict[str, object] = {
        "name": "Sensor_to_Control",
        "from": {"swc": "SpeedSensor", "port": "pOut"},
        "to": {"swc": "BrakeCtl", "port": "rIn"},
    }
    return Connector.model_validate(document | overrides)


def test_a_connector_declares_no_composition_by_default() -> None:
    """`None` is the ROOT, and it is deliberately not a schema default.

    The root's name is a property of the model rather than a constant, so it is
    resolved where the model is in hand — the writer, the rules, the importer —
    and `Connector.composition` therefore carries no `omitAtDefault` either: the
    derived omission rule already drops a `None`.
    """
    assert _connector().composition is None


def test_a_connector_composition_is_absent_from_the_written_file_when_it_is_none() -> None:
    written = connector_to_dict(_connector())

    assert "composition" not in written
    assert written["from"] == {"swc": "SpeedSensor", "port": "pOut"}


def test_a_connector_composition_is_written_when_it_is_set() -> None:
    written = connector_to_dict(_connector(composition="ControlSw"))

    assert written["composition"] == "ControlSw"


def test_a_composition_typed_endpoint_is_written_under_its_own_key_alone() -> None:
    """Never `swc: null` beside it: the loader's exactly-one rule would refuse it."""
    written = connector_to_dict(_connector(**{"from": {"composition": "BswComposition", "port": "pOut"}}))

    assert written["from"] == {"composition": "BswComposition", "port": "pOut"}
    assert "swc" not in written["from"]


# ---------------------------------------------------------------------------
# SWComponent — the external stub
# ---------------------------------------------------------------------------


def test_a_component_is_not_a_stub_by_default() -> None:
    component = SWComponent(name="SpeedSensor")

    assert (component.external, component.external_ref, component.external_dest) == (False, "", "")


def test_a_stub_carries_the_sources_reference_and_dest_verbatim_and_no_ports() -> None:
    stub = SWComponent(
        name="LogBox",
        external=True,
        external_ref="/Project/Architecture/Product/LogBox/LogBox",
        external_dest="COMPOSITION-SW-COMPONENT-TYPE",
    )

    assert stub.external is True
    assert stub.external_ref == "/Project/Architecture/Product/LogBox/LogBox"
    assert stub.external_dest == "COMPOSITION-SW-COMPONENT-TYPE"
    assert (stub.ports, stub.runnables, stub.memory) == ([], [], {})


def test_a_component_that_is_no_stub_writes_none_of_the_three_keys() -> None:
    """The additive-delta rule at the serializer: a file without the stub keys round-trips."""
    written = swc_to_dict(SWComponent(name="SpeedSensor"))

    assert "external" not in written
    assert "external_ref" not in written
    assert "external_dest" not in written


def test_a_stub_writes_all_three_and_keeps_the_dest_the_source_carried() -> None:
    written = swc_to_dict(
        SWComponent(
            name="Rte",
            external=True,
            external_ref="/Project/SupplierPlatforms/Bsw/Bsw",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
        )
    )

    assert written["external"] is True
    assert written["external_ref"] == "/Project/SupplierPlatforms/Bsw/Bsw"
    assert written["external_dest"] == "APPLICATION-SW-COMPONENT-TYPE"
    assert list(written) == [
        "name",
        "type",
        "external",
        "external_ref",
        "external_dest",
        "ports",
        "runnables",
    ], "the stub triple is written between `description` and the collections, in declaration order"


# ---------------------------------------------------------------------------
# The loader half of the layer's rule family — the emission witnesses
# ---------------------------------------------------------------------------


#: Every ``element-local`` rule the contract binds to the ``compositions`` layer,
#: mapped to one document per declared ``appliesTo`` entry that the loader must
#: refuse. The **mechanism**, not only the instances: the gates that *look* like
#: coverage cannot see a loader rule -- the conformance gate compares
#: declarations to **decorator presence**, and
#: ``test_validator_compositions.COMPOSITION_FAMILY_BREAKS`` selects only
#: ``validator.CONSISTENCY_RULES``, which excludes the family by construction.
#:
#: The key is the contract identifier and the inner key is the ``appliesTo``
#: entry, so the two directions below are both derived from the shipped contract:
#: a new element-local rule over this layer is red until it is witnessed, and a
#: new *field* on an existing one is red until that field is witnessed too.
#:
#: **What this table does not reach**: it witnesses that the loader refuses the
#: shape, never that it refuses it for the right reason or with the right
#: message — the ``match`` on each case is what carries that, and it is the
#: guard's own message string rather than a contract of it.
COMPOSITION_LOADER_BREAKS: dict[str, dict[str, tuple[Callable[[], object], str]]] = {
    "rule:compositions.composition.name.not-blank": {
        "field:compositions.composition.name": (
            lambda: Composition(name="  ", title="Root"),
            "name must not be empty",
        ),
    },
    "rule:compositions.composition_port.reference-not-blank": {
        "field:compositions.composition_port.name": (
            lambda: CompositionPort(name="  ", direction="provide", interface="ISignal"),
            "must not be empty",
        ),
        "field:compositions.composition_port.interface": (
            lambda: CompositionPort(name="pOut", direction="provide", interface="  "),
            "must not be empty",
        ),
    },
    "rule:compositions.composition_member.reference-not-blank": {
        "field:compositions.composition_member.name": (
            lambda: CompositionMember(name="  ", type="Beta"),
            "must not be empty",
        ),
        "field:compositions.composition_member.type": (
            lambda: CompositionMember(name="Alpha", type="  "),
            "must not be empty",
        ),
        "field:compositions.composition_member.composition": (
            lambda: CompositionMember(name="Inner", composition="  "),
            "must not be empty",
        ),
    },
    "rule:compositions.composition_member.type-form": {
        "kind:compositions.composition_member": (
            lambda: CompositionMember(name="Inner"),
            "exactly one",
        ),
    },
    "rule:compositions.delegation.reference-not-blank": {
        "field:compositions.delegation.name": (
            lambda: Delegation(name="  ", port="pOut", member="Sensor", member_port="pSpeed"),
            "must not be empty",
        ),
        "field:compositions.delegation.port": (
            lambda: Delegation(name="dOut", port="  ", member="Sensor", member_port="pSpeed"),
            "must not be empty",
        ),
        "field:compositions.delegation.member": (
            lambda: Delegation(name="dOut", port="pOut", member="  ", member_port="pSpeed"),
            "must not be empty",
        ),
        "field:compositions.delegation.member_port": (
            lambda: Delegation(name="dOut", port="pOut", member="Sensor", member_port="  "),
            "must not be empty",
        ),
    },
}


def _layer_local_rules() -> dict[str, tuple[str, ...]]:
    """Every ``element-local`` rule the shipped contract binds to this layer, with its ``appliesTo``.

    Read at the **call** from the contract itself, so the table above is
    compared against the declaration rather than against a remembered list.
    """
    return {
        rule["id"]: tuple(rule["appliesTo"])
        for rule in load_contract()["rules"]
        if rule["family"] == "element-local"
        and any(
            entry == "layer:compositions" or entry.startswith(("field:compositions.", "kind:compositions."))
            for entry in rule["appliesTo"]
        )
    }


def test_the_loader_table_covers_every_element_local_rule_of_the_layer() -> None:
    """Both directions, so neither a new rule nor a retired one goes unnoticed."""
    assert set(COMPOSITION_LOADER_BREAKS) == set(_layer_local_rules())


def test_every_declared_applies_to_entry_of_those_rules_has_its_own_case() -> None:
    """A rule guarding four names needs four documents, not one: a table keyed by rule alone would not."""
    declared = _layer_local_rules()
    assert {rule: tuple(sorted(cases)) for rule, cases in COMPOSITION_LOADER_BREAKS.items()} == {
        rule: tuple(sorted(entries)) for rule, entries in declared.items()
    }


@pytest.mark.parametrize(
    ("rule", "entry"),
    sorted((rule, entry) for rule, cases in COMPOSITION_LOADER_BREAKS.items() for entry in cases),
)
def test_each_element_local_rule_of_the_layer_is_refused_at_load(rule: str, entry: str) -> None:
    """The witness itself: the loader refuses the document the entry names."""
    build, message = COMPOSITION_LOADER_BREAKS[rule][entry]

    with pytest.raises(ValidationError, match=message):
        build()
