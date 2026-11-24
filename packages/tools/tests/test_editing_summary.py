"""The two summary rules and the index projection.

The expected summary field set of every shipped kind is derived from ``load_contract()``
by the summary predicate (layer identity, placement, discriminator, required scalars,
scalar references, collection counts) and compared with what
:func:`~swcraft_tools.editing.read.summarize` serves, so a kind a later contract
declares needs no edit here.

The contract is read directly, not through the registry: the registry is a projection
of the same document, so an expectation derived from it would compare ``read.py``
against the tables ``read.py`` consults.

What this file does **not** reach: the wire (the ``form`` request key and the budget
are their own suites); the sizes of the forms (``scripts/measure_mcp_scale.py``); and
any project but ``examples/basic`` for the served half, which reaches only the kinds
that model declares (no ``enum``, ``record``, ``array``, ``system``, ``container`` or
``init_event`` element); the index-ordering test uses a synthetic two-file
``implementation_types`` layer, and the two task-entry summary tests an
``examples/basic`` copy whose ``Task_10ms`` entries are rewritten.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, StateSnapshot, model_element_path
from swcraft_tools.contract import load_contract
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.read import (
    ReadMissError,
    SummaryNotDerivable,
    collection_index,
    layer_index,
    publish_element,
    read_value,
    summarize,
    summarize_collection,
    summary_field_names,
)
from swcraft_tools.listing import list_model
from swcraft_tools.model_registry import production_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.schema import (
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    Port,
    SequenceStep,
    TaskRunnableRef,
)

BASIC_MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"

#: ``examples/basic`` stores both its components under ``swc/example/``; a summary
#: publishes ``group`` as a full read does, so every SWC summary here passes it.
BASIC_PLACEMENT = {"group": "example"}


@pytest.fixture(scope="module")
def registry() -> reg.Registry:
    return reg.current()


@pytest.fixture(scope="module")
def snapshot() -> Any:
    return StateSnapshot(state=load_model_state(BASIC_MODEL_ROOT))


# The expectation, derived from the contract document itself


def _contract_kinds() -> dict[str, dict[str, Any]]:
    """Every declared kind by name, with the layer that declares it attached."""
    kinds: dict[str, dict[str, Any]] = {}
    for layer in load_contract()["layers"]:
        for kind in layer["kinds"]:
            kinds[kind["kind"]] = dict(kind, _layer=layer)
    return kinds


def _identity_names(kind: dict[str, Any], kinds: dict[str, dict[str, Any]]) -> set[str]:
    """The identity clause for one kind, top-level or member.

    Top-level: the layer's ``identityField``, every ``placement`` field and the
    kind's ``discriminatorField``. Member: the owning collection's identity, and
    nothing where the owner is a required object (``connector.from`` / ``.to``).
    """
    if not kind.get("memberOnly", False):
        names = {kind["_layer"]["identityField"]}
        names.update(field["name"] for field in kind["fields"] if field.get("placement", False))
        if kind.get("discriminatorField"):
            names.add(kind["discriminatorField"])
        return names

    names: set[str] = set()
    for owner in kind.get("memberOf", []):
        declaration = next(f for f in kinds[owner["kind"]]["fields"] if f["name"] == owner["field"])
        collection = declaration.get("collection")
        if collection is None:
            continue
        if collection.get("identityField"):
            names.add(collection["identityField"])
        names.update(collection.get("identityFields", []))
    return names


def _is_scalar_reference(field: dict[str, Any]) -> bool:
    """A declared ``reference`` that is neither a collection nor an object, required or not."""
    return field.get("reference") is not None and field.get("collection") is None and field.get("elementKind") is None


def _expected_summary(kind: dict[str, Any], kinds: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """The summary predicate over one kind's declared fields, in declared field order.

    Four clauses: identity, collections, required scalars and every scalar reference,
    optional ones included, so a port's ``interface`` is in an agent's default read.
    ``observed_interface`` declares no ``reference`` and stays out.
    """
    identity = _identity_names(kind, kinds)
    expected = []
    for field in kind["fields"]:
        required_scalar = (
            field.get("required", False)
            and field.get("collection") is None
            and field.get("elementKind") is None
            and not field.get("placement", False)
        )
        if (
            field["name"] in identity
            or field.get("collection") is not None
            or required_scalar
            or _is_scalar_reference(field)
        ):
            expected.append(field["name"])
    return tuple(expected)


CONTRACT_KINDS = _contract_kinds()
EXPECTED_SUMMARIES = {name: _expected_summary(kind, CONTRACT_KINDS) for name, kind in CONTRACT_KINDS.items()}
TOP_LEVEL_KINDS = sorted(name for name, kind in CONTRACT_KINDS.items() if not kind.get("memberOnly", False))
MEMBER_KINDS = sorted(name for name, kind in CONTRACT_KINDS.items() if kind.get("memberOnly", False))

#: The optional scalar references, by kind: the fields the reference clause alone
#: selects, and the only ones a summary omits (when unset).
OPTIONAL_REFERENCES = {
    (kind_name, field["name"])
    for kind_name, kind in CONTRACT_KINDS.items()
    for field in kind["fields"]
    if _is_scalar_reference(field) and not field.get("required", False)
}


@pytest.mark.parametrize("kind_name", TOP_LEVEL_KINDS)
def test_top_level_summary_fields_are_the_contract_predicate(kind_name: str, registry: reg.Registry) -> None:
    """Every shipped top-level kind's summary is exactly the predicate's derivation from the contract."""
    assert summary_field_names(registry.element_kind(kind_name), registry) == EXPECTED_SUMMARIES[kind_name]


@pytest.mark.parametrize("kind_name", MEMBER_KINDS)
def test_member_summary_fields_are_the_contract_predicate(kind_name: str, registry: reg.Registry) -> None:
    """Every shipped member kind: the owning collection's identity plus the same two clauses."""
    assert summary_field_names(registry.element_kind(kind_name), registry) == EXPECTED_SUMMARIES[kind_name]


def test_every_shipped_kind_is_covered_by_the_derivation(registry: reg.Registry) -> None:
    """The two parametrizations above name every kind the registry declares — none is unasserted."""
    assert set(EXPECTED_SUMMARIES) == set(registry.kind_names())
    assert len(TOP_LEVEL_KINDS) + len(MEMBER_KINDS) == len(EXPECTED_SUMMARIES)


# What `summarize()` serves, on `examples/basic`


def _element(snapshot: Any, layer: str, identity: str, identity_field: str = "name") -> Any:
    return next(element for element in snapshot.state[layer] if getattr(element, identity_field) == identity)


def _summary(snapshot: Any, registry: reg.Registry, layer: str, identity: str, identity_field: str = "name") -> Any:
    element = _element(snapshot, layer, identity, identity_field)
    kind = registry.kind_of_model(type(element))
    return summarize(element, kind, registry, placement=BASIC_PLACEMENT if layer == "swc" else None)


def test_swc_summary_is_identity_placement_and_counts(snapshot: Any, registry: reg.Registry) -> None:
    """An SWC's summary: its name, its group, and a count for each of its collections."""
    assert _summary(snapshot, registry, "swc", "ExampleSWC") == {
        "name": "ExampleSWC",
        "group": "example",
        "ports": 1,
        "runnables": 1,
        "memory": 1,
        # A count of zero is still a count: it tells a client the field is a
        # collection rather than absent.
        "data_type_mappings": 0,
        "satisfies": 1,
        "variables": 0,
        "exclusive_areas": 0,
    }


def test_connector_summary_carries_no_endpoints(snapshot: Any, registry: reg.Registry) -> None:
    """A required object is not in a summary: the connectors' endpoints, the bulk of their size.

    The reference clause does not reach inside ``from`` / ``to``; the connector's
    own optional ``composition`` is unset here, so it is omitted.
    """
    summary = _summary(snapshot, registry, "connectors", "Example_to_Consumer")
    assert summary == {"name": "Example_to_Consumer"}
    assert "from" not in summary
    assert "to" not in summary


def test_requirement_summary_carries_its_required_description(snapshot: Any, registry: reg.Registry) -> None:
    """``description`` is required, so the summary carries it whole."""
    assert _summary(snapshot, registry, "requirements", "REQ_EXAMPLE_001", "id") == {
        "id": "REQ_EXAMPLE_001",
        "title": "Example requirement for ExampleSWC",
        "description": "The system shall provide example functionality via ExampleSWC.",
        # `version` and `source` are required scalars, and `derived_from` a
        # collection counted like any other.
        "version": 1,
        "source": "architecture",
        "derived_from": 0,
    }


def test_discriminator_is_carried_by_a_summary(snapshot: Any, registry: reg.Registry) -> None:
    """A data type's ``kind`` tells its kinds apart; the summary keeps it.

    The base type's summary is stated beside it under ``base_types``, where ``kind``
    is a required field.
    """
    assert _summary(snapshot, registry, "implementation_types", "uint8") == {
        "name": "uint8",
        "kind": "value_type",
        "base": "uint8_base",
    }
    assert _summary(snapshot, registry, "base_types", "uint8_base") == {
        "name": "uint8_base",
        "kind": "base_type",
        "size": 8,
        "encoding": "NONE",
    }


def test_served_summaries_match_the_derivation_for_every_element(snapshot: Any, registry: reg.Registry) -> None:
    """Every element of ``examples/basic`` is served in exactly its kind's derived field set."""
    seen = set()
    for layer in registry.layers:
        for element in snapshot.state[layer.name]:
            kind = registry.kind_of_model(type(element))
            summary = summarize(element, kind, registry, placement=BASIC_PLACEMENT if layer.name == "swc" else None)
            # An unset optional reference is omitted, never published as null.
            unset = {
                name
                for name in EXPECTED_SUMMARIES[kind.kind]
                if (kind.kind, name) in OPTIONAL_REFERENCES and getattr(element, name) is None
            }
            assert tuple(summary) == tuple(n for n in EXPECTED_SUMMARIES[kind.kind] if n not in unset), kind.kind
            assert None not in [summary[name] for name in summary if (kind.kind, name) in OPTIONAL_REFERENCES]
            seen.add(kind.kind)
    assert {"swc", "connector", "requirement", "base_type", "task", "sequence", "memory"} <= seen


def test_the_optional_references_are_the_six_the_correction_names() -> None:
    """Named rather than counted: a new optional reference joins every default read, and should read as one."""
    assert OPTIONAL_REFERENCES == {
        ("port", "interface"),
        ("connector", "composition"),
        ("connector_endpoint", "swc"),
        ("connector_endpoint", "composition"),
        ("composition_member", "type"),
        ("composition_member", "composition"),
        ("data_element", "implementation_type"),
        ("data_element", "application_type"),
        ("argument", "implementation_type"),
        ("argument", "application_type"),
        ("application_type", "unit_ref"),
        ("application_type", "compu_method_ref"),
        ("application_type", "data_constr_ref"),
        ("enum", "compu_method_ref"),
        ("value_type", "compu_method_ref"),
        ("reference_type", "compu_method_ref"),
        ("task_runnable_ref", "swc"),
        ("sequence_step", "swc"),
        # A variable's two type fields, exactly one of the pair set.
        ("variable", "application_type"),
        ("variable", "implementation_type"),
    }
    port = CONTRACT_KINDS["port"]
    assert "observed_interface" in [field["name"] for field in port["fields"]]
    assert "observed_interface" not in EXPECTED_SUMMARIES["port"]


def test_a_kind_that_owns_no_collection_is_summarized_out_of_the_element_alone(
    snapshot: Any, registry: reg.Registry
) -> None:
    """A kind that owns no collection: every summary entry is the element's own value, byte for byte.

    The contrast is ``ExampleSWC``, whose ``ports`` entry is a count where the full
    form carries the list. The set of such kinds is derived from the contract.
    """
    kinds_without_collections = {
        name for name, kind in CONTRACT_KINDS.items() if all(f.get("collection") is None for f in kind["fields"])
    }
    assert {"connector", "base_type", "value_type"} <= kinds_without_collections
    seen = set()
    for layer in registry.layers:
        for element in snapshot.state[layer.name]:
            kind = registry.kind_of_model(type(element))
            if kind.kind not in kinds_without_collections:
                continue
            placement = BASIC_PLACEMENT if layer.name == "swc" else None
            summary = summarize(element, kind, registry, placement=placement)
            published = publish_element(element, registry, placement=placement)
            assert summary.items() <= published.items(), kind.kind
            seen.add(kind.kind)
    assert {"connector", "base_type"} <= seen

    owner = _summary(snapshot, registry, "swc", "ExampleSWC")
    whole = publish_element(
        next(e for e in snapshot.state["swc"] if e.name == "ExampleSWC"), registry, placement=BASIC_PLACEMENT
    )
    assert owner["ports"] == 1 and isinstance(whole["ports"], list)


# The member rule


def test_port_summary_is_identity_plus_required_scalars(snapshot: Any, registry: reg.Registry) -> None:
    """``port`` -> ``name``, ``direction``, ``interface`` and its collections as counts.

    Not ``description`` and not ``observed_interface``. ``interface`` is optional (a
    stub's port declares none) and is selected by the reference clause.
    """
    port = _element(snapshot, "swc", "ExampleSWC").ports[0]
    assert summarize(port, registry.element_kind("port"), registry, placement=None) == {
        "name": "pOut",
        "direction": "provide",
        "interface": "IExample",
        "measures": 0,
        "com_specs": 0,
        "satisfies": 0,
    }


def test_a_port_summary_publishes_a_set_interface_and_omits_an_unset_one(registry: reg.Registry) -> None:
    """A set reference is published, an unset one is absent, never ``null``.

    The full form is unchanged: it publishes both, ``null`` included.
    """
    kind = registry.element_kind("port")
    declared = Port(name="pIn", direction="require", interface="SomeSRI")
    stub = Port(name="pStub", direction="require", observed_interface="SeenSRI")

    assert summarize(declared, kind, registry, placement=None) == {
        "name": "pIn",
        "direction": "require",
        "interface": "SomeSRI",
        "measures": 0,
        "com_specs": 0,
        "satisfies": 0,
    }
    stub_summary = summarize(stub, kind, registry, placement=None)
    assert stub_summary == {"name": "pStub", "direction": "require", "measures": 0, "com_specs": 0, "satisfies": 0}

    full = publish_element(stub, registry, placement=None)
    assert full["interface"] is None
    assert full["observed_interface"] == "SeenSRI"


def test_composite_member_summary_carries_every_identity_field(snapshot: Any, registry: reg.Registry) -> None:
    """``task.runnables`` is a composite collection: both ``identity_fields``, in field order."""
    member = _element(snapshot, "tasks", "Task_10ms").runnables[0]
    assert summarize(member, registry.element_kind("task_runnable_ref"), registry, placement=None) == {
        "swc": "ExampleConsumerSWC",
        "runnable": "ExampleConsumerRun",
    }


@pytest.mark.parametrize(
    ("kind_name", "model", "extra"),
    [("task_runnable_ref", TaskRunnableRef, {}), ("sequence_step", SequenceStep, {"note": ""})],
)
def test_a_composite_member_set_by_instance_publishes_instance_and_no_swc(
    kind_name: str, model: type[Any], extra: dict[str, Any], registry: reg.Registry
) -> None:
    """An unset selector is omitted from a ``summary`` row; ``full`` publishes it ``null``.

    ``instance`` is no reference, so the reference clause alone would publish ``"swc": null``.
    """
    kind = registry.element_kind(kind_name)
    by_instance = model(instance="Top/worker", runnable="Run")
    by_swc = model(swc="Worker", runnable="Run")

    assert summarize(by_instance, kind, registry, placement=None) == {"instance": "Top/worker", "runnable": "Run"}
    assert summarize(by_swc, kind, registry, placement=None) == {"swc": "Worker", "runnable": "Run"}
    assert publish_element(by_instance, registry, placement=None) == {
        "swc": None,
        "instance": "Top/worker",
        "runnable": "Run",
        **extra,
    }
    assert publish_element(by_swc, registry, placement=None) == {
        "swc": "Worker",
        "instance": None,
        "runnable": "Run",
        **extra,
    }


def test_a_summary_rows_two_keys_address_the_member(tmp_path: Path, registry: reg.Registry) -> None:
    """A ``summary`` row is replayable as a ``member`` step although it carries two keys.

    The absent selector reads as ``null`` in an address, for an entry written with
    ``instance`` and one written with ``swc`` alike.
    """
    model_root = tmp_path / "model"
    shutil.copytree(BASIC_MODEL_ROOT, model_root)
    task_path = model_root / "tasks" / "Task_10ms.yaml"
    base = task_path.read_text(encoding="utf-8")
    entry = "- swc: ExampleConsumerSWC\n  runnable: ExampleConsumerRun\n"
    assert base.endswith(entry), base
    rewritten = base.removesuffix(entry) + (
        "- instance: ExampleConsumerSWC\n  runnable: ExampleConsumerRun\n- swc: ExampleSWC\n  runnable: ExampleRun\n"
    )
    task_path.write_text(rewritten, encoding="utf-8", newline="\n")
    state = StateSnapshot(state=load_model_state(model_root))

    collection = Address(layer="tasks", element="Task_10ms", path=(FieldStep("runnables"),))
    rows = read_value(state, collection, form="summary", registry=registry)
    assert rows == [
        {"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"},
        {"swc": "ExampleSWC", "runnable": "ExampleRun"},
    ]
    answers = [
        read_value(
            state,
            Address(layer="tasks", element="Task_10ms", path=(FieldStep("runnables"), MemberStep(row))),
            registry=registry,
        )
        for row in rows
    ]
    assert answers == [
        {"swc": None, "instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"},
        {"swc": "ExampleSWC", "instance": None, "runnable": "ExampleRun"},
    ]


def test_a_summary_rows_two_keys_continue_a_page_as_the_three_key_identity_does(
    tmp_path: Path, registry: reg.Registry
) -> None:
    """A two-key ``after`` cursor continues a page where the emitted three-key cursor does.

    Asserted at a collection address and over a layer's ``members`` slice.
    """
    model_root = tmp_path / "model"
    shutil.copytree(BASIC_MODEL_ROOT, model_root)
    task_path = model_root / "tasks" / "Task_10ms.yaml"
    base = task_path.read_text(encoding="utf-8")
    entry = "- swc: ExampleConsumerSWC\n  runnable: ExampleConsumerRun\n"
    rewritten = base.removesuffix(entry) + (
        "- instance: ExampleConsumerSWC\n  runnable: ExampleConsumerRun\n- swc: ExampleSWC\n  runnable: ExampleRun\n"
    )
    task_path.write_text(rewritten, encoding="utf-8", newline="\n")
    state = StateSnapshot(state=load_model_state(model_root))
    collection = Address(layer="tasks", element="Task_10ms", path=(FieldStep("runnables"),))
    two_key = {"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}
    three_key = {"swc": None, **two_key}

    pages = [read_value(state, collection, limit=1, after=after, registry=registry) for after in (two_key, three_key)]
    assert pages[0] == pages[1] == [{"swc": "ExampleSWC", "runnable": "ExampleRun"}]

    layer = Address(layer="tasks")
    sliced = [
        read_value(
            state,
            layer,
            members="runnables",
            limit=1,
            after={"element": "Task_10ms", "member": after},
            registry=registry,
        )
        for after in (two_key, three_key)
    ]
    assert sliced[0] == sliced[1]
    assert [row["member"]["runnable"] for row in sliced[0]] == ["ExampleRun"]


def test_member_of_a_required_object_has_no_identity_clause(registry: reg.Registry) -> None:
    """``connector_endpoint`` has no owning collection: no identity clause.

    It selects every declared field (``port``, and ``swc``/``composition`` by the
    reference clause), so ``summary`` is no narrower at ``connector.from`` / ``.to``
    and an explicit one is served ``full`` (``test_editing_budget.py``).
    """
    assert summary_field_names(registry.element_kind("connector_endpoint"), registry) == ("swc", "composition", "port")


def test_a_composition_member_summary_publishes_whichever_alternative_is_set(registry: reg.Registry) -> None:
    """``composition_member`` names a component type **or** a sub-composition; the summary shows the set one."""
    kind = registry.element_kind("composition_member")
    assert summary_field_names(kind, registry) == ("name", "type", "composition")
    typed = CompositionMember(name="m1", type="SomeSWC")
    nested = CompositionMember(name="m2", composition="Inner")
    assert summarize(typed, kind, registry, placement=None) == {"name": "m1", "type": "SomeSWC"}
    assert summarize(nested, kind, registry, placement=None) == {"name": "m2", "composition": "Inner"}


def test_a_connector_endpoint_summary_is_its_full_answer_minus_the_unset_alternative(registry: reg.Registry) -> None:
    """Both endpoint alternatives are exposed, the set one published and the unset one omitted."""
    kind = registry.element_kind("connector_endpoint")
    assert summarize(ConnectorEndpoint(swc="A", port="p"), kind, registry, placement=None) == {"swc": "A", "port": "p"}
    assert summarize(ConnectorEndpoint(composition="C", port="p"), kind, registry, placement=None) == {
        "composition": "C",
        "port": "p",
    }


def test_a_connector_summary_carries_its_composition_when_set(registry: reg.Registry) -> None:
    """``connector.composition`` is published when set and omitted otherwise; ``from`` / ``to`` never."""
    kind = registry.element_kind("connector")
    ends = {"from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}}
    scoped = Connector.model_validate({"name": "c1", **ends, "composition": "Top"})
    unscoped = Connector.model_validate({"name": "c2", **ends})
    assert summarize(scoped, kind, registry, placement=None) == {"name": "c1", "composition": "Top"}
    assert summarize(unscoped, kind, registry, placement=None) == {"name": "c2"}


def test_collection_address_in_summary_form_summarizes_each_member(snapshot: Any, registry: reg.Registry) -> None:
    """``{...,"ports"}`` answers the members' summaries, in reader order."""
    element = _element(snapshot, "swc", "ExampleSWC")
    field = registry.element_kind("swc").get_field("ports")
    assert summarize_collection(element.ports, field, registry) == [
        # `measures` is a collection, so the summary renders its count, as a
        # component's `ports` is one level up.
        {"name": "pOut", "direction": "provide", "interface": "IExample", "measures": 0, "com_specs": 0, "satisfies": 0}
    ]


@pytest.mark.parametrize(
    ("layer", "identity", "identity_field", "owner_kind", "field_name", "count"),
    [
        ("swc", "ExampleSWC", "name", "swc", "memory", 1),
        # The trace link is the component's own `satisfies`.
        ("swc", "ExampleSWC", "name", "swc", "satisfies", 1),
        ("hardware", "RAM0", "name", "memory", "accessible", 1),
    ],
)
def test_whole_value_collections_answer_verbatim_and_count_in_the_owner(
    snapshot: Any,
    registry: reg.Registry,
    layer: str,
    identity: str,
    identity_field: str,
    owner_kind: str,
    field_name: str,
    count: int,
) -> None:
    """A ``whole_value`` collection has no member summary: the owner counts it, its address answers it."""
    assert _summary(snapshot, registry, layer, identity, identity_field)[field_name] == count

    element = _element(snapshot, layer, identity, identity_field)
    field = registry.element_kind(owner_kind).get_field(field_name)
    served = summarize_collection(getattr(element, field_name), field, registry)
    assert served == read_value(
        snapshot, Address(layer=layer, element=identity, path=(FieldStep(field=field_name),)), registry=registry
    )


def test_whole_value_collection_of_element_kinds_answers_verbatim(snapshot: Any, registry: reg.Registry) -> None:
    """``runnable.events`` is ``whole_value`` with element-kind members: answered verbatim.

    Verbatim includes a timing event's ``disabled_modes`` and ``name``.
    """
    runnable = _element(snapshot, "swc", "ExampleSWC").runnables[0]
    field = registry.element_kind("runnable").get_field("events")
    assert summarize_collection(runnable.events, field, registry) == [
        {"type": "timing", "period": 0.01, "disabled_modes": [], "name": None}
    ]


# `layer_index` and `collection_index`


def test_layer_index_is_its_identities_and_their_count(snapshot: Any, registry: reg.Registry) -> None:
    """A layer index: the identity values in the order the layer read answers, plus the count."""
    layer = registry.layer("implementation_types")
    index = layer_index(snapshot.state["implementation_types"], layer, registry)
    # The `examples/basic` implementation types; its base types live in `base_types`.
    assert index["count"] == len(snapshot.state["implementation_types"]) == 12
    assert index["identities"] == [
        element["name"] for element in read_value(snapshot, Address(layer="implementation_types"), registry=registry)
    ]


def test_the_layer_index_is_not_swcraft_lists_ordering(tmp_path: Path, registry: reg.Registry) -> None:
    """``layer_index`` and ``swcraft list`` answer the same set in different orders.

    ``layer_index`` ranks by declared element kind, then reader order; ``swcraft list``
    is reader order alone. ``examples/basic`` holds value types only, so it cannot show
    the difference; hence the two-file layer. The index must not reshuffle as a client
    steps down the forms, which are kind-ranked.
    """
    root = tmp_path / "model"
    (root / "data_types" / "implementation").mkdir(parents=True)
    (model_element_path(root, "RefFirst", kind="reference_type")).write_text(
        "name: RefFirst\nkind: reference_type\nbase: uint8\n", encoding="utf-8", newline="\n"
    )
    (model_element_path(root, "uint8", kind="value_type")).write_text(
        "name: uint8\nkind: value_type\nbase: uint8_base\n", encoding="utf-8", newline="\n"
    )

    state = load_model_state(str(root), registry=production_registry())
    served = layer_index(state["implementation_types"], registry.layer("implementation_types"), registry)["identities"]
    reader_order = list_model(str(root), "implementation_types", registry=production_registry())[
        "implementation_types"
    ].identities

    assert sorted(served) == sorted(reader_order), "the two answer the same SET"
    assert reader_order == ("RefFirst", "uint8"), "`swcraft list` is the reader's file-name order"
    assert served == ["uint8", "RefFirst"], "the index ranks by declared element kind first"


def test_the_index_of_an_empty_layer_is_an_empty_list(snapshot: Any, registry: reg.Registry) -> None:
    """An empty layer indexes to zero identities: an answer, not a refusal."""
    assert layer_index([], registry.layer("sequences"), registry) == {
        "count": 0,
        "identities": [],
    }


def test_the_index_of_a_field_identified_collection_is_the_member_identities(
    snapshot: Any, registry: reg.Registry
) -> None:
    element = _element(snapshot, "swc", "ExampleSWC")
    field = registry.element_kind("swc").get_field("ports")
    assert collection_index(element.ports, field) == {"count": 1, "identities": ["pOut"]}


def test_the_index_of_a_composite_collection_is_the_identity_objects(snapshot: Any, registry: reg.Registry) -> None:
    """A composite identity is an object, because that is what a ``member`` step accepts.

    Guards a defect: it was an array, which ``member_matches`` refuses. The engine round trip is
    ``test_editing_read.py::test_an_identity_out_of_an_index_addresses_the_member_it_names``.
    """
    element = _element(snapshot, "tasks", "Task_10ms")
    field = registry.element_kind("task").get_field("runnables")
    assert collection_index(element.runnables, field) == {
        "count": 1,
        # The emitted identity: three keys, the absent selector `None`.
        "identities": [{"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}],
    }


def test_the_index_of_a_whole_value_list_is_the_values(snapshot: Any, registry: reg.Registry) -> None:
    """Over ``swc.satisfies``: a whole-value list indexes to its values."""
    element = _element(snapshot, "swc", "ExampleSWC")
    field = registry.element_kind("swc").get_field("satisfies")
    assert collection_index(element.satisfies, field) == {
        "count": 1,
        "identities": ["REQ_EXAMPLE_001@1"],
    }


def test_the_index_of_a_map_is_its_keys(snapshot: Any, registry: reg.Registry) -> None:
    """``swc.memory`` is a ``whole_value`` **map**: the key is the identity, the value is content."""
    element = _element(snapshot, "swc", "ExampleSWC")
    field = registry.element_kind("swc").get_field("memory")
    assert collection_index(element.memory, field) == {"count": 1, "identities": ["RAM0"]}


def test_an_absent_collection_reads_as_no_members_in_every_projection(snapshot: Any, registry: reg.Registry) -> None:
    """The three helpers agree about ``None``, which keeps a default read total.

    No shipped collection is optional; this guards the first one a contract declares
    from becoming a ``TypeError``, since a collection address defaults to ``summary``.
    """
    field = registry.element_kind("swc").get_field("ports")
    assert collection_index(None, field) == {"count": 0, "identities": []}
    assert summarize_collection(None, field, registry) is None


# The refusal, and what is routed away from it


def test_a_top_level_kind_without_its_layer_identity_is_refused(registry: reg.Registry) -> None:
    """Impossible in a conforming contract; the refusal is the guard that says so."""
    kind = registry.element_kind("swc")
    identity_field = registry.layer(kind.layer).identity_field
    broken = replace(kind, fields=tuple(f for f in kind.fields if f.name != identity_field))
    with pytest.raises(SummaryNotDerivable, match=identity_field):
        summary_field_names(broken, registry)


def test_a_member_kind_without_the_layer_identity_is_not_refused(registry: reg.Registry) -> None:
    """A member kind is routed to the member rule, never to that refusal — ``connector_endpoint`` is the proof."""
    for kind_name in MEMBER_KINDS:
        kind = registry.element_kind(kind_name)
        assert summary_field_names(kind, registry) == EXPECTED_SUMMARIES[kind_name]
    endpoint = registry.element_kind("connector_endpoint")
    assert registry.layer(endpoint.layer).identity_field not in summary_field_names(endpoint, registry)


def test_summarize_refuses_an_swc_whose_placement_it_was_not_given(snapshot: Any, registry: reg.Registry) -> None:
    """``group`` lives beside the element; a summary without it misses, exactly as a full read does."""
    element = _element(snapshot, "swc", "ExampleSWC")
    with pytest.raises(ReadMissError):
        summarize(element, registry.element_kind("swc"), registry, placement=None)


def test_a_summary_discloses_no_storage(snapshot: Any, registry: reg.Registry) -> None:
    """A summary discloses no storage: no separator, no suffix, no manifest root key."""
    markers = ("/", "\\", ".yaml", "model_root", "dist_root")
    for layer in registry.layers:
        for element in snapshot.state[layer.name]:
            kind = registry.kind_of_model(type(element))
            summary = summarize(element, kind, registry, placement=BASIC_PLACEMENT if layer.name == "swc" else None)
            for key, value in summary.items():
                assert not any(marker in key for marker in markers)
                if isinstance(value, str):
                    assert not any(marker in value for marker in markers), (layer.name, key)
