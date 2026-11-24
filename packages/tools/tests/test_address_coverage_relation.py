"""The editing scope's coverage relation, as properties over an address corpus.

Reflexive on identical entries, transitive along a path prefix, never satisfied across differing
operations.
"""

from __future__ import annotations

import pytest
from swcraft_tools.editing.address import (
    Address,
    FieldStep,
    KeyStep,
    MemberStep,
    PathStep,
    ScopeEntry,
    covers,
    is_address_prefix,
)

#: The three change operations, stated here rather than imported from the address module, so a
#: fourth verb does not silently join these properties.
OPERATIONS = ("create", "update", "remove")


# The coverage relation's algebra

#: Path shapes reaching every step form and every nesting depth an address has.
COVERAGE_PATHS: tuple[tuple[PathStep, ...], ...] = (
    (),
    (FieldStep("ports"),),
    (FieldStep("ports"), MemberStep("pOut")),
    (FieldStep("ports"), MemberStep("pOut"), FieldStep("interface")),
    (FieldStep("memory"),),
    (FieldStep("memory"), KeyStep("RAM")),
    (FieldStep("runnables"),),
    (FieldStep("runnables"), MemberStep("ExampleRun")),
    (FieldStep("runnables"), MemberStep("ExampleRun"), FieldStep("events")),
    (
        FieldStep("runnables"),
        MemberStep("ExampleRun"),
        FieldStep("events"),
        MemberStep({"type": "timing", "period": 0.01}),
    ),
    (FieldStep("steps"), MemberStep({"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"})),
)


def _coverage_addresses() -> tuple[Address, ...]:
    """Layer, element and field addresses across two layers and three elements."""
    addresses = [Address(layer=name) for name in ("swc", "tasks", "interfaces")]
    for layer_name, element in (("swc", "ExampleSWC"), ("swc", "ExampleConsumerSWC"), ("tasks", "Task_10ms")):
        for path in COVERAGE_PATHS:
            addresses.append(Address(layer=layer_name, element=element, path=path))
    return tuple(addresses)


COVERAGE_ADDRESSES: tuple[Address, ...] = _coverage_addresses()


def _prefix_chains() -> tuple[tuple[Address, ...], ...]:
    """Every address with all of its own path prefixes, longest last."""
    chains = []
    for layer_name, element in (("swc", "ExampleSWC"), ("tasks", "Task_10ms")):
        for path in COVERAGE_PATHS:
            chains.append(
                tuple(Address(layer=layer_name, element=element, path=path[:cut]) for cut in range(len(path) + 1))
            )
    return tuple(chains)


class TestTheCoverageRelation:
    """The coverage rule is transitive and exact on the operation.

    A non-transitive relation would let a scope entry approve a change its own prefix does not; one inexact on the
    operation would let a scope naming an element authorize deleting it.
    """

    def test_the_corpus_reaches_every_step_form_and_depth(self) -> None:
        forms = {type(step) for path in COVERAGE_PATHS for step in path}
        assert forms == {FieldStep, MemberStep, KeyStep}
        assert max(len(path) for path in COVERAGE_PATHS) >= 4

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_it_is_reflexive_on_identical_entries(self, operation: str) -> None:
        for address in COVERAGE_ADDRESSES:
            assert covers(ScopeEntry(operation=operation, address=address), operation, address), address

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_it_is_transitive_along_a_path_prefix(self, operation: str) -> None:
        for chain in _prefix_chains():
            for outer in chain:
                for middle in chain:
                    for inner in chain:
                        outer_entry = ScopeEntry(operation=operation, address=outer)
                        middle_entry = ScopeEntry(operation=operation, address=middle)
                        if covers(outer_entry, operation, middle) and covers(middle_entry, operation, inner):
                            assert covers(outer_entry, operation, inner), (outer, middle, inner)

    def test_it_is_never_satisfied_across_differing_operations(self) -> None:
        for approved in OPERATIONS:
            for requested in OPERATIONS:
                if approved == requested:
                    continue
                for scope_address in COVERAGE_ADDRESSES:
                    for delta_address in COVERAGE_ADDRESSES:
                        entry = ScopeEntry(operation=approved, address=scope_address)
                        assert not covers(entry, requested, delta_address), (approved, requested, scope_address)

    def test_the_prefix_relation_is_a_partial_order(self) -> None:
        # Reflexive, transitive and antisymmetric: "omission is the wildcard" is a containment.
        for left in COVERAGE_ADDRESSES:
            assert is_address_prefix(left, left)
            for right in COVERAGE_ADDRESSES:
                if is_address_prefix(left, right) and is_address_prefix(right, left):
                    assert left == right, (left, right)
                for third in COVERAGE_ADDRESSES:
                    if is_address_prefix(left, right) and is_address_prefix(right, third):
                        assert is_address_prefix(left, third), (left, right, third)

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_a_layer_entry_covers_its_whole_layer_and_nothing_else(self, operation: str) -> None:
        for layer_name in ("swc", "tasks", "interfaces"):
            entry = ScopeEntry(operation=operation, address=Address(layer=layer_name))
            for address in COVERAGE_ADDRESSES:
                assert covers(entry, operation, address) == (address.layer == layer_name), address

    def test_a_longer_scope_never_covers_a_shorter_change(self) -> None:
        # The direction that is not symmetric: approving a field does not approve
        # the element that contains it.
        for chain in _prefix_chains():
            for depth, longer in enumerate(chain):
                for shorter in chain[:depth]:
                    assert not covers(ScopeEntry(operation="update", address=longer), "update", shorter)
