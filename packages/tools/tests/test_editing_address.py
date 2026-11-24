"""The address model, resolution and prefix coverage.

Four groups:

1. **the form** — ``{layer, element?, path?}``, the three step forms, and the
   two things the form cannot say: a storage path and an ordinal step;
2. **coverage** — same operation, same layer, element-wise path prefix, with
   omission as the wildcard;
3. **member matching** — the three strategies the registry publishes, and the
   multiset rule that separates a ``whole_value`` LIST from a ``whole_value`` MAP;
4. **resolution** — the addressed value, or a typed miss carrying the failed step.

The multiset half is tested hardest: a naive implementation returns the first
match everywhere, which passes on every unique collection and silently halves
``Runnable.events``.
"""

from __future__ import annotations

import pytest
from swcraft_tools.editing import address as addr
from swcraft_tools.editing import registry as reg
from swcraft_tools.schema import (
    Connector,
    ConnectorEndpoint,
    Core,
    Memory,
    Port,
    Requirement,
    Runnable,
    SWComponent,
    Task,
    TaskRunnableRef,
    TimingEvent,
)
from swcraft_tools.snapshot import ModelSnapshot

# One small snapshot, holding every shape an address can reach


def _snapshot() -> ModelSnapshot:
    """A snapshot with a duplicated multiset member, a map, and an aliased field.

    ``ExampleRun`` records the 10 ms trigger twice, which ``schema.py`` and
    ``check-model`` permit.
    """
    swc = SWComponent(
        name="ExampleSWC",
        ports=[
            Port(name="pOut", direction="provide", interface="IExample"),
            Port(name="pIn", direction="require", interface="IOther"),
        ],
        runnables=[
            Runnable(
                name="ExampleRun",
                min_start_interval=0.01,
                events=[
                    TimingEvent(type="timing", period=0.01),
                    TimingEvent(type="timing", period=0.01),
                    TimingEvent(type="timing", period=0.02),
                ],
            )
        ],
        memory={"RAM": 2048},
        # One link written twice: a scalar whole-value multiset, which
        # `schema.py` permits as it permits the doubled event.
        satisfies=["REQ-SPEED-001@1", "REQ-SPEED-001@1"],
    )
    return ModelSnapshot(
        compositions=[],
        swcs=[swc],
        interfaces=[],
        implementation_types=[],
        connectors=[
            Connector(
                name="ExampleConn",
                **{"from": ConnectorEndpoint(swc="ExampleSWC", port="pOut")},
                to=ConnectorEndpoint(swc="OtherSWC", port="pIn"),
            )
        ],
        requirements=[Requirement(id="REQ-SPEED-001", title="t", description="d", version=1, source="architecture")],
        tasks=[
            Task(
                name="Task_10ms",
                period=0.01,
                priority=10,
                partition="PARTITION0",
                runnables=[TaskRunnableRef(swc="ExampleSWC", runnable="ExampleRun")],
            )
        ],
        sequences=[],
        hardware=[
            Core(name="CORE0", kind="core", threshold=0.8),
            Memory(name="RAM", kind="memory", type="RAM", size=65536, accessible=["CORE0"], threshold=0.9),
        ],
        mapping_sets=[],
        base_types=[],
        application_types=[],
        units=[],
        compu_methods=[],
        data_constraints=[],
    )


@pytest.fixture
def snapshot() -> ModelSnapshot:
    return _snapshot()


def _collection(kind: str, field: str) -> reg.CollectionDescriptor:
    descriptor = reg.REGISTRY.element_kind(kind).get_field(field)
    assert descriptor is not None and descriptor.collection is not None
    return descriptor.collection


EVENTS = _collection("runnable", "events")
PORTS = _collection("swc", "ports")
TASK_RUNNABLES = _collection("task", "runnables")
LINKS = _collection("swc", "satisfies")
MEMORY = _collection("swc", "memory")


# 1. The form


def test_the_three_step_forms_are_the_only_ones() -> None:
    assert set(addr._STEP_KEYS) == {"field", "member", "key"}


def test_no_step_form_carries_an_ordinal() -> None:
    """Nothing in an address is positional, in either direction."""
    for step in (addr.FieldStep("ports"), addr.MemberStep("pOut"), addr.KeyStep("RAM")):
        assert not hasattr(step, "index")
        assert "index" not in step.to_json()


def test_an_ordinal_step_is_refused_rather_than_reinterpreted() -> None:
    with pytest.raises(addr.AddressError, match="no ordinal step"):
        addr.Address.from_json({"layer": "swc", "element": "X", "path": [{"index": 0}]})


def test_a_step_carries_exactly_one_form() -> None:
    for bad in ({}, {"field": "ports", "member": "pOut"}):
        with pytest.raises(addr.AddressError, match="exactly one"):
            addr.Address.from_json({"layer": "swc", "element": "X", "path": [bad]})


def test_a_layer_only_address_is_legal() -> None:
    address = addr.Address.from_json({"layer": "implementation_types"})
    assert address.element is None and address.path == ()
    assert not address.is_element_address and not address.is_field_address


def test_a_path_without_an_element_is_not_an_address() -> None:
    """``dependentRequired: {"path": ["element"]}`` — a per-layer field wildcard is inexpressible."""
    with pytest.raises(addr.AddressError, match="dependentRequired"):
        addr.Address.from_json({"layer": "swc", "path": [{"field": "ports"}]})
    with pytest.raises(addr.AddressError, match="dependentRequired"):
        addr.Address(layer="swc", path=(addr.FieldStep("ports"),))


def test_an_empty_path_is_refused() -> None:
    with pytest.raises(addr.AddressError, match="at least one step"):
        addr.Address.from_json({"layer": "swc", "element": "X", "path": []})


def test_a_storage_path_is_not_an_under_specified_address() -> None:
    """``negative/envelope-storage-path-as-address.json``: a string is a different kind of thing."""
    with pytest.raises(addr.AddressError, match="storage path"):
        addr.Address.from_json("model/swc/sensors/ExampleSWC.yaml")


def test_an_address_carries_no_other_key() -> None:
    with pytest.raises(addr.AddressError, match="additionalProperties"):
        addr.Address.from_json({"layer": "swc", "element": "X", "file": "X.yaml"})


def test_an_explicit_null_element_is_not_the_layer_wildcard() -> None:
    """Only omission is the wildcard, tested by key presence, never by value.

    An explicit ``element: null`` is an ``AddressError``, as the published ``$defs/address`` admits
    no ``null``. A serializer spelling an unset ``element`` as ``null`` would widen an
    element-scoped approval to the whole layer, unseen in the echo, since
    :meth:`Address.to_json` drops the key.
    """
    with pytest.raises(addr.AddressError, match="omit the key"):
        addr.Address.from_json({"layer": "swc", "element": None})


def test_an_explicit_null_path_is_not_the_whole_element_wildcard() -> None:
    with pytest.raises(addr.AddressError, match="omit the key"):
        addr.Address.from_json({"layer": "swc", "element": "X", "path": None})


def test_a_null_element_does_not_reach_a_scope_entry_either() -> None:
    """``parse_approved_scope``'s promise: a malformed entry is an ``AddressError``."""
    with pytest.raises(addr.AddressError):
        addr.ScopeEntry.from_json({"operation": "remove", "address": {"layer": "swc", "element": None}})


@pytest.mark.parametrize("identity", ["../../etc/passwd", "..", ".", "a/b", "a\\b", "a\x00b"])
def test_a_traversal_identity_is_a_malformed_address(identity: str) -> None:
    """A path-traversal identity is refused by the form, not sanitized later."""
    with pytest.raises(addr.AddressError, match="single path segment"):
        addr.Address.from_json({"layer": "swc", "element": identity})


def test_a_requirement_id_is_a_legal_identity() -> None:
    """`$defs/identity` is deliberately not the AUTOSAR `Identifier` pattern."""
    assert addr.Address.from_json({"layer": "requirements", "element": "REQ-SPEED-001"}).element == "REQ-SPEED-001"


def test_a_layer_name_is_checked_against_its_pattern_not_against_the_registry() -> None:
    """An undeclared layer parses and misses at resolution — it is a semantic failure, not a malformed envelope."""
    assert addr.Address.from_json({"layer": "no_such_layer"}).layer == "no_such_layer"
    with pytest.raises(addr.AddressError, match="not a layer name"):
        addr.Address.from_json({"layer": "SWC"})


@pytest.mark.parametrize(
    "member",
    ["pOut", {"swc": "X", "runnable": "R"}, {"type": "timing", "period": 0.01}, 3, 1.5, True, None, ["a", "b"]],
)
def test_a_member_step_admits_any_json_value(member: object) -> None:
    """`$defs/pathStep`: a `whole_value` identity is the member's own value, of any type."""
    address = addr.Address.from_json({"layer": "swc", "element": "X", "path": [{"member": member}]})
    assert address.path[0] == addr.MemberStep(member)


def test_round_trip_through_json_is_exact() -> None:
    payload = {
        "layer": "swc",
        "element": "ExampleSWC",
        "path": [
            {"field": "runnables"},
            {"member": "ExampleRun"},
            {"field": "events"},
            {"member": {"type": "timing", "period": 0.01}},
        ],
    }
    assert addr.Address.from_json(payload).to_json() == payload


def test_a_field_address_needs_both_an_element_and_a_path() -> None:
    assert addr.Address("swc", "ExampleSWC", (addr.FieldStep("ports"),)).is_field_address
    assert not addr.Address("swc", "ExampleSWC").is_field_address


# 2. Coverage

_SWC = "swc"
_MEMORY_RAM = addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("memory"), addr.KeyStep("RAM")))
_PORT_OUT = addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("ports"), addr.MemberStep("pOut")))


def test_coverage_exact_match() -> None:
    entry = addr.ScopeEntry("update", _MEMORY_RAM)
    assert entry.covers("update", _MEMORY_RAM)


def test_coverage_element_wildcard_is_omission() -> None:
    """Omitting `element` covers every element of the layer; omitting `path` covers the whole element."""
    assert addr.ScopeEntry("update", addr.Address(_SWC)).covers("update", _MEMORY_RAM)
    assert addr.ScopeEntry("update", addr.Address(_SWC, "ExampleSWC")).covers("update", _MEMORY_RAM)


def test_coverage_path_prefix() -> None:
    """The coverage rule: approving `{swc, ExampleSWC, [memory]}` covers `[memory, RAM]`."""
    entry = addr.ScopeEntry("update", addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("memory"),)))
    assert entry.covers("update", _MEMORY_RAM)


def test_coverage_does_not_reach_a_sibling_path() -> None:
    entry = addr.ScopeEntry("update", addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("memory"),)))
    assert not entry.covers("update", _PORT_OUT)


def test_coverage_does_not_reach_a_longer_scope_than_the_delta() -> None:
    """A prefix is never longer than what it prefixes: approving a key does not cover the whole map."""
    entry = addr.ScopeEntry("update", _MEMORY_RAM)
    assert not entry.covers("update", addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("memory"),)))


def test_coverage_does_not_reach_another_element_or_another_layer() -> None:
    entry = addr.ScopeEntry("update", addr.Address(_SWC, "ExampleSWC"))
    assert not entry.covers("update", addr.Address(_SWC, "OtherSWC"))
    assert not entry.covers("update", addr.Address("tasks", "ExampleSWC"))


def test_an_update_approval_never_covers_a_remove() -> None:
    """Operation is part of the key and matched exactly, so no approval widens to another operation."""
    entry = addr.ScopeEntry("update", addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("description"),)))
    assert not entry.covers("remove", addr.Address(_SWC, "ExampleSWC"))
    assert not entry.covers("create", addr.Address(_SWC, "ExampleSWC", (addr.FieldStep("description"),)))


def test_one_scope_entry_covers_every_occurrence_of_a_multiset_member() -> None:
    """Coverage is stated over an address, and in a multiset two occurrences share one."""
    address = addr.Address(
        _SWC,
        "ExampleSWC",
        (
            addr.FieldStep("runnables"),
            addr.MemberStep("ExampleRun"),
            addr.FieldStep("events"),
            addr.MemberStep({"type": "timing", "period": 0.01}),
        ),
    )
    entry = addr.ScopeEntry("remove", address)
    assert entry.covers("remove", address)
    assert entry.covers("remove", addr.Address.from_json(address.to_json()))


def test_a_composite_member_step_compares_by_identity_not_by_key_order() -> None:
    left = addr.MemberStep({"swc": "ExampleSWC", "runnable": "ExampleRun"})
    right = addr.MemberStep({"runnable": "ExampleRun", "swc": "ExampleSWC"})
    assert left == right and hash(left) == hash(right)
    entry = addr.ScopeEntry("update", addr.Address("tasks", "Task_10ms", (addr.FieldStep("runnables"), left)))
    assert entry.covers("update", addr.Address("tasks", "Task_10ms", (addr.FieldStep("runnables"), right)))


def test_covers_any_needs_at_least_one_entry() -> None:
    entries = [addr.ScopeEntry("remove", addr.Address(_SWC, "Other")), addr.ScopeEntry("update", addr.Address(_SWC))]
    assert addr.covers_any(entries, "update", _MEMORY_RAM)
    assert not addr.covers_any(entries, "create", _MEMORY_RAM)
    assert not addr.covers_any([], "update", _MEMORY_RAM)


def test_a_scope_entry_names_a_change_operation() -> None:
    with pytest.raises(addr.AddressError, match="not one of"):
        addr.ScopeEntry("read", addr.Address(_SWC))
    entry = addr.ScopeEntry.from_json({"operation": "remove", "address": {"layer": "swc", "element": "X"}})
    assert entry.to_json() == {"operation": "remove", "address": {"layer": "swc", "element": "X"}}


# 3. Member matching and the multiset


def test_field_strategy_matches_on_the_declared_identity_field(snapshot: ModelSnapshot) -> None:
    ports = snapshot.swcs[0].ports
    assert addr.member_indices(ports, PORTS, "pOut") == (0,)
    assert addr.member_indices(ports, PORTS, "pIn") == (1,)
    assert addr.member_indices(ports, PORTS, "absent") == ()


def test_field_strategy_is_blind_to_every_other_field(snapshot: ModelSnapshot) -> None:
    ports = snapshot.swcs[0].ports
    assert not addr.member_matches(ports[0], PORTS, "provide")
    assert not addr.member_matches(ports[0], PORTS, {"name": "pOut"})


def test_composite_strategy_needs_exactly_the_declared_tuple(snapshot: ModelSnapshot) -> None:
    members = snapshot.tasks[0].runnables
    assert addr.member_indices(members, TASK_RUNNABLES, {"swc": "ExampleSWC", "runnable": "ExampleRun"}) == (0,)
    assert addr.member_indices(members, TASK_RUNNABLES, {"swc": "ExampleSWC"}) == ()
    assert addr.member_indices(members, TASK_RUNNABLES, {"swc": "ExampleSWC", "runnable": "R", "extra": 1}) == ()
    assert addr.member_indices(members, TASK_RUNNABLES, "ExampleRun") == ()


def test_whole_value_strategy_compares_the_whole_member(snapshot: ModelSnapshot) -> None:
    events = snapshot.swcs[0].runnables[0].events
    assert addr.member_indices(events, EVENTS, {"type": "timing", "period": 0.02}) == (2,)
    assert addr.member_indices(events, EVENTS, {"type": "timing"}) == ()
    assert addr.member_indices(events, EVENTS, {"type": "timing", "period": 0.03}) == ()


def test_a_whole_value_list_is_a_multiset_and_members_are_counted(snapshot: ModelSnapshot) -> None:
    """A duplicate is legal, and clearing it takes one `remove` per occurrence."""
    events = snapshot.swcs[0].runnables[0].events
    ten_ms = {"type": "timing", "period": 0.01}
    assert addr.member_indices(events, EVENTS, ten_ms) == (0, 1)
    assert len(addr.member_indices(events, EVENTS, {"type": "timing", "period": 0.02})) == 1
    assert len(addr.member_indices(events, EVENTS, {"type": "init"})) == 0


def test_a_scalar_whole_value_list_counts_the_same_way(snapshot: ModelSnapshot) -> None:
    """`SWComponent.satisfies` is a `list[str]`: the string IS the member and its own identity."""
    links = snapshot.swcs[0].satisfies
    assert len(addr.member_indices(links, LINKS, "REQ-SPEED-001@1")) == 2
    assert len(addr.member_indices(links, LINKS, "REQ-SPEED-001@2")) == 0


def test_a_map_has_no_members_to_match(snapshot: ModelSnapshot) -> None:
    """A `whole_value` MAP is not a multiset: its key is the identity and it is unique by construction."""
    assert MEMORY.kind == "map" and MEMORY.member_identity == "whole_value"
    with pytest.raises(addr.AddressError, match="unique by construction"):
        addr.member_indices(snapshot.swcs[0].memory, MEMORY, "RAM")


def test_member_matching_tolerates_an_integer_for_a_float_field() -> None:
    """JSON spells `1` where a `float` field dumps `1.0`; both must reach the same member."""
    events = [TimingEvent(type="timing", period=1.0)]
    assert addr.member_indices(events, EVENTS, {"type": "timing", "period": 1}) == (0,)


def test_a_boolean_is_not_an_integer() -> None:
    assert not addr.values_equal(True, 1)
    assert not addr.values_equal(False, 0)
    assert addr.values_equal(True, True)


def test_canonical_value_dumps_a_model_under_its_aliases(snapshot: ModelSnapshot) -> None:
    dumped = addr.canonical_value(snapshot.connectors[0])
    assert "from" in dumped and "from_" not in dumped


def test_identity_key_ignores_map_key_order_and_matches_nested_shapes() -> None:
    assert addr.identity_key({"a": 1, "b": [1, {"c": 2}]}) == addr.identity_key({"b": [1, {"c": 2}], "a": 1})
    assert not addr.values_equal({"a": 1}, {"a": 1, "b": 2})
    assert not addr.values_equal([1, 2], [2, 1])


def test_an_integer_too_wide_for_a_float_is_keyed_rather_than_raising() -> None:
    """An `int` that `float()` refuses with `OverflowError` equals itself and nothing else.

    `init` and `enum_literal.value` carry no width bound; keying through
    `float(value)` made every comparison against such a member a crash.
    """
    wide = 10**400
    assert addr.identity_key(wide) == addr.identity_key(10**400)
    assert addr.values_equal(wide, 10**400)
    assert not addr.values_equal(wide, -wide)
    assert not addr.values_equal(wide, 1)
    assert not addr.values_equal(wide, 1.0)
    # Hashable: the key is a `dict`/`Counter` key in `apply.py`'s member pairing.
    assert len({addr.identity_key(wide), addr.identity_key(10**400)}) == 1


def test_two_integers_a_float_rounds_together_are_two_numbers() -> None:
    """An ``int`` shares the float key only where a float holds it exactly.

    ``float()`` rounds 2**64 - 1 and 2**64 - 2 together without raising, so an
    edit between them was judged untouched.
    """
    wide = 2**64 - 1
    assert not addr.values_equal(wide, wide - 1)
    assert addr.values_equal(wide, 2**64 - 1)
    assert not addr.values_equal(wide, float(wide))
    assert not addr.values_equal(2**53 + 1, 2**53)
    assert not addr.values_equal(2**63 - 1, 2**63)
    # ... and what a float does hold is still one number with it.
    assert addr.values_equal(2**64, float(2**64))
    assert addr.values_equal(-(2**63), float(-(2**63)))
    assert addr.values_equal(2**53, float(2**53))
    assert addr.values_equal(1, 1.0)
    assert not addr.values_equal({"value_max": wide}, {"value_max": wide - 1})
    assert len({addr.identity_key(wide), addr.identity_key(wide - 1)}) == 2


def test_an_integer_too_wide_for_a_float_is_keyed_inside_a_container_too() -> None:
    """A wide integer is keyed as a leaf of a nested map or list, the shape `init` arrives in."""
    wide = 10**400
    assert addr.values_equal({"x": wide, "y": 1}, {"y": 1, "x": 10**400})
    assert not addr.values_equal({"x": wide}, {"x": wide + 1})
    assert addr.values_equal([1, [wide]], [1, [10**400]])


# 4. Resolution


def test_a_layer_address_resolves_to_the_layer(snapshot: ModelSnapshot) -> None:
    result = addr.resolve(addr.Address("hardware"), snapshot)
    assert result.found and result.value == snapshot.hardware


def test_an_element_address_resolves_to_its_element(snapshot: ModelSnapshot) -> None:
    result = addr.resolve(addr.Address("swc", "ExampleSWC"), snapshot)
    assert result.found and result.value is snapshot.swcs[0]
    assert result.kind is not None and result.kind.kind == "swc"


def test_an_element_of_a_kind_discriminated_layer_reports_its_own_kind(snapshot: ModelSnapshot) -> None:
    result = addr.resolve(addr.Address("hardware", "RAM"), snapshot)
    assert result.found and result.kind is not None and result.kind.kind == "memory"


def test_the_requirements_layer_resolves_on_id(snapshot: ModelSnapshot) -> None:
    result = addr.resolve(addr.Address("requirements", "REQ-SPEED-001"), snapshot)
    assert result.found and result.value is snapshot.requirements[0]


def test_a_field_step_resolves_through_the_published_alias(snapshot: ModelSnapshot) -> None:
    """`Connector.from_` is published as `from`; the registry resolves the alias once, for everyone."""
    address = addr.Address("connectors", "ExampleConn", (addr.FieldStep("from"), addr.FieldStep("swc")))
    result = addr.resolve(address, snapshot)
    assert result.found and result.value == "ExampleSWC"


def test_a_full_path_of_all_three_step_forms_resolves(snapshot: ModelSnapshot) -> None:
    address = addr.Address.from_json(
        {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [
                {"field": "runnables"},
                {"member": "ExampleRun"},
                {"field": "min_start_interval"},
            ],
        }
    )
    assert addr.resolve(address, snapshot).value == 0.01
    key_address = addr.Address("swc", "ExampleSWC", (addr.FieldStep("memory"), addr.KeyStep("RAM")))
    result = addr.resolve(key_address, snapshot)
    assert result.found and result.value == 2048 and result.container == {"RAM": 2048}


def test_a_multiset_member_resolves_to_its_first_occurrence(snapshot: ModelSnapshot) -> None:
    address = addr.Address(
        "swc",
        "ExampleSWC",
        (
            addr.FieldStep("runnables"),
            addr.MemberStep("ExampleRun"),
            addr.FieldStep("events"),
            addr.MemberStep({"type": "timing", "period": 0.01}),
        ),
    )
    result = addr.resolve(address, snapshot)
    assert result.found and result.value is snapshot.swcs[0].runnables[0].events[0]
    assert len(addr.member_indices(result.container, EVENTS, {"type": "timing", "period": 0.01})) == 2


def test_the_addressed_collection_is_reported_for_a_field_step(snapshot: ModelSnapshot) -> None:
    address = addr.Address("swc", "ExampleSWC", (addr.FieldStep("ports"),))
    result = addr.resolve(address, snapshot)
    assert result.found and result.collection is PORTS and result.container is snapshot.swcs[0]


@pytest.mark.parametrize(
    ("address", "reason", "step_index"),
    [
        (addr.Address("no_such_layer"), "no_such_layer", None),
        (addr.Address("swc", "Absent"), "no_such_element", None),
        (addr.Address("swc", "ExampleSWC", (addr.FieldStep("safety_level"),)), "no_such_field", 0),
        (addr.Address("swc", "ExampleSWC", (addr.FieldStep("ports"), addr.MemberStep("absent"))), "no_such_member", 1),
        (addr.Address("swc", "ExampleSWC", (addr.FieldStep("memory"), addr.KeyStep("FLASH"))), "no_such_key", 1),
        (addr.Address("swc", "ExampleSWC", (addr.FieldStep("ports"), addr.KeyStep("pOut"))), "not_traversable", 1),
        (addr.Address("swc", "ExampleSWC", (addr.FieldStep("memory"), addr.MemberStep("RAM"))), "not_traversable", 1),
        (
            addr.Address("swc", "ExampleSWC", (addr.FieldStep("description"), addr.FieldStep("anything"))),
            "not_traversable",
            1,
        ),
    ],
)
def test_a_miss_is_typed_and_carries_the_step_that_failed(
    snapshot: ModelSnapshot, address: addr.Address, reason: str, step_index: int | None
) -> None:
    result = addr.resolve(address, snapshot)
    assert not result.found
    assert result.reason == reason
    assert result.step_index == step_index
    assert result.step == (address.path[step_index] if step_index is not None else None)
    assert result.detail


def test_a_miss_carries_the_address_it_failed_on(snapshot: ModelSnapshot) -> None:
    address = addr.Address("swc", "Absent")
    assert addr.resolve(address, snapshot).address is address


def test_a_placement_field_resolves_only_from_placement_metadata(snapshot: ModelSnapshot) -> None:
    """`swc.group` is declared but is not in `SWComponent.model_fields`; only placement supplies it."""
    address = addr.Address("swc", "ExampleSWC", (addr.FieldStep("group"),))
    miss = addr.resolve(address, snapshot)
    assert not miss.found and miss.reason == "placement_unavailable"
    resolved = addr.resolve(address, snapshot, placement={"group": "sensors"})
    assert resolved.found and resolved.value == "sensors"
    assert resolved.field is not None and resolved.field.placement


def test_resolution_reads_the_layer_through_the_registry_not_through_its_name(snapshot: ModelSnapshot) -> None:
    """The `swc` layer lives in `ModelSnapshot.swcs`; the mapping is a registry fact, held in one place."""
    assert reg.REGISTRY.layer("swc").snapshot_attribute == "swcs"
    assert addr.resolve(addr.Address("swc"), snapshot).value is snapshot.swcs


# An optional identity field reads as `null` when it is absent, and an address has one spelling


class TestTheOptionalIdentityFieldReadsAsNull:
    RUNNABLES = reg.REGISTRY.element_kind("task").get_field("runnables").collection
    STEPS = reg.REGISTRY.element_kind("sequence").get_field("steps").collection
    TWO_KEYS = {"swc": "A", "runnable": "Run"}
    THREE_KEYS = {"swc": "A", "instance": None, "runnable": "Run"}

    def _address(self, member: dict) -> addr.Address:
        return addr.Address.from_json(
            {"layer": "tasks", "element": "T", "path": [{"field": "runnables"}, {"member": member}]}
        )

    def test_the_projection_names_the_two_selectors_and_not_the_runnable(self) -> None:
        for collection in (self.RUNNABLES, self.STEPS):
            assert collection.identity_fields == ("swc", "instance", "runnable")
            assert collection.optional_identity_fields == ("swc", "instance")

    def test_a_two_key_identity_matches_the_member_whose_instance_is_absent(self) -> None:
        member = TaskRunnableRef(swc="A", runnable="Run")
        assert addr.member_matches(member, self.RUNNABLES, self.TWO_KEYS)
        assert addr.member_matches(member, self.RUNNABLES, self.THREE_KEYS)
        assert addr.member_matches(dict(self.TWO_KEYS), self.RUNNABLES, self.THREE_KEYS)

    def test_a_two_key_identity_does_not_match_an_instance_member(self) -> None:
        member = TaskRunnableRef(instance="A", runnable="Run")
        assert not addr.member_matches(member, self.RUNNABLES, self.TWO_KEYS)
        assert addr.member_matches(member, self.RUNNABLES, {"instance": "A", "runnable": "Run"})

    def test_a_two_key_payload_has_the_three_key_identity(self) -> None:
        assert addr.member_identity_of(dict(self.TWO_KEYS), self.RUNNABLES) == self.THREE_KEYS

    def test_a_payload_without_the_required_runnable_has_no_identity(self) -> None:
        assert addr.member_identity_of({"swc": "A"}, self.RUNNABLES) is None
        assert not addr.member_matches(TaskRunnableRef(swc="A", runnable="Run"), self.RUNNABLES, {"swc": "A"})

    def test_a_two_key_and_a_three_key_spelling_reduce_to_one_address_and_hash_equal(self) -> None:
        two = addr.reduce_address(self._address(self.TWO_KEYS))
        three = addr.reduce_address(self._address(self.THREE_KEYS))
        assert two == three
        assert hash(two) == hash(three)
        assert two.to_json()["path"][1] == {"member": self.THREE_KEYS}

    def test_an_unreduced_two_key_step_is_another_step_which_is_why_the_reduction_exists(self) -> None:
        assert self._address(self.TWO_KEYS) != self._address(self.THREE_KEYS)

    def test_a_two_key_scope_covers_a_three_key_delta_address_and_the_other_way_round(self) -> None:
        two = addr.reduce_address(self._address(self.TWO_KEYS))
        three = addr.reduce_address(self._address(self.THREE_KEYS))
        deeper = addr.Address(layer="tasks", element="T", path=(*three.path, addr.FieldStep("runnable")))
        assert addr.covers(addr.ScopeEntry(operation="remove", address=two), "remove", three)
        assert addr.covers(addr.ScopeEntry(operation="remove", address=three), "remove", two)
        assert addr.is_address_prefix(two, deeper)

    def test_a_scope_on_one_selector_never_covers_the_other_selectors_address(self) -> None:
        """Completion spells an absent selector ``null`` and resolves nothing.

        ``swc: A`` and ``instance: A`` are two identities to coverage, in both directions.
        """
        by_swc = addr.reduce_address(self._address(self.TWO_KEYS))
        by_instance = addr.reduce_address(self._address({"instance": "A", "runnable": "Run"}))
        assert not addr.covers(addr.ScopeEntry(operation="update", address=by_swc), "update", by_instance)
        assert not addr.covers(addr.ScopeEntry(operation="update", address=by_instance), "update", by_swc)
        assert not addr.is_address_prefix(by_swc, by_instance)

    @pytest.mark.parametrize(
        "member",
        [
            pytest.param({"swc": "A", "runnable": "Run", "core": "C0"}, id="a-fourth-key"),
            pytest.param({"swc": "A"}, id="no-runnable"),
            pytest.param("A", id="not-an-object"),
        ],
    )
    def test_a_step_that_names_nothing_is_kept_as_written(self, member) -> None:
        address = self._address(member)
        assert addr.reduce_address(address) == address
        assert addr.reduce_address(address).to_json()["path"][1] == {"member": member}
