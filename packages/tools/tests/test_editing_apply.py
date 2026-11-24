"""The in-memory apply and passes B and C: what the shipped vectors cannot state.

``test_editing_vectors.py`` replays the vectors and is the acceptance floor; this
module asserts the rules a single exchange cannot state about itself:

* **the multiset rules** — a duplicate is legal, ``create`` adds one occurrence,
  ``remove`` removes one, and the same-identity ban is lifted for such a member
  while a map key keeps it; the ban itself holds for any member, layer and order;
* **``touched`` is the value that moved**, a subset of what was named, which
  keeps ``UnexplainedChangeError`` unreachable; and nothing is written;
* **the pass boundary** — an address that resolves against nothing is left to
  pass C, which tells a later supplier (``partial-change-set``) from none
  (``broken-reference``); removing a still-referenced element is pass D's;
* **binding is about position, never the verb** — every value a client sends is
  bound at every depth, and a whole-collection ``update`` only reorders;
* **an ambiguous baseline is refused before binding**: an address cannot pick
  between two files, or two members, that declare one identity.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from swcraft_tools.editing.address import Address, FieldStep, KeyStep, MemberStep
from swcraft_tools.editing.apply import (
    IDENTITY_CONFLICT,
    IMMUTABLE_FIELD,
    SCHEMA_VIOLATION,
    UNKNOWN_FIELD,
    StagedModel,
    apply_operations,
)
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.passes import BROKEN_REFERENCE, PARTIAL_CHANGE_SET, pass_b, pass_c
from swcraft_tools.editing.protocol import CreateOperation, RemoveOperation, UpdateOperation
from swcraft_tools.editing.read import read_value
from swcraft_tools.snapshot import load_snapshot

TIMING_10MS = {"type": "timing", "period": 0.01}
TIMING_20MS = {"type": "timing", "period": 0.02}
#: The two as the staged model holds them: an identity omits a field at its
#: default, a value carries every field, the event's `None` `name` included.
TIMING_10MS_VALUE = {**TIMING_10MS, "disabled_modes": [], "name": None}
TIMING_20MS_VALUE = {**TIMING_20MS, "disabled_modes": [], "name": None}


# Fixtures


def _model_root(tmp_path: Path, vector: str) -> Path:
    return fixtures.build_fixture(tmp_path / vector, vector) / "model"


@pytest.fixture
def base(tmp_path: Path) -> Any:
    """The plain ``examples/basic`` baseline every vector starts from."""
    return load_snapshot(str(_model_root(tmp_path, "read-swc-element")))


@pytest.fixture
def multiset(tmp_path: Path) -> Any:
    """The baseline holding two 10 ms events and one 20 ms one."""
    return load_snapshot(str(_model_root(tmp_path, "create-and-remove-duplicate-runnable-events")))


def _events_address(*, member: Any = None) -> Address:
    path: list[Any] = [
        FieldStep("runnables"),
        MemberStep("ExampleRun"),
        FieldStep("events"),
    ]
    if member is not None:
        path.append(MemberStep(member))
    return Address(layer="swc", element="ExampleSWC", path=tuple(path))


def _events(outcome: Any) -> list[dict[str, Any]]:
    swc = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
    return swc["runnables"][0]["events"]


def _codes(outcome: Any) -> list[str]:
    return [violation.error for violation in outcome.violations]


class TestStagedModel:
    """The staged tree is the baseline, published, and independent of it."""

    def test_every_layer_is_staged_in_the_published_vocabulary(self, base: Any) -> None:
        staged = StagedModel.from_snapshot(base)
        assert set(staged.elements) == set(staged.registry.layer_names)
        swc = next(item for item in staged.elements["swc"] if item["name"] == "ExampleSWC")
        # `group` is placement metadata published as a declared field, and
        # `from`/`from_` is the alias the registry resolves once.
        assert swc["group"] == "example"
        connector = staged.elements["connectors"][0]
        assert "from" in connector and "from_" not in connector

    def test_the_baseline_copy_is_not_the_staged_one(self, base: Any) -> None:
        staged = StagedModel.from_snapshot(base)
        staged.elements["tasks"][0]["priority"] = 99
        assert staged.baseline["tasks"][staged.elements["tasks"][0]["name"]]["priority"] != 99

    def test_an_untouched_model_touches_nothing(self, base: Any) -> None:
        assert StagedModel.from_snapshot(base).touched() == ()


class TestTouchedIsTheValueThatMoved:
    """The property that keeps ``UnexplainedChangeError`` unreachable.

    ``write_element`` has no "value unchanged" short-circuit, so a touched no-op
    would rewrite a file with no delta entry and the commit would fail with an
    internal assertion carrying no protocol code.
    """

    def test_a_no_op_update_touches_nothing(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("priority"),))
        current = next(task for task in base.tasks if task.name == "Task_10ms").priority
        outcome = apply_operations(base, [UpdateOperation(address=address, value=current)])
        assert outcome.violations == ()
        assert outcome.touched == ()

    def test_a_real_update_touches_exactly_its_element(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("priority"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value=3)])
        assert [(item.layer, item.identity) for item in outcome.touched] == [("tasks", "Task_10ms")]
        assert outcome.touched[0].before["priority"] != 3
        assert outcome.touched[0].after["priority"] == 3

    def test_the_touched_set_is_a_subset_of_what_the_operations_name(self, base: Any) -> None:
        # The engine authors nothing the client did not send.
        named = [
            UpdateOperation(
                address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("description"),)),
                value=next(task for task in base.tasks if task.name == "Task_10ms").description,
            ),
            UpdateOperation(
                address=Address(layer="swc", element="ExampleSWC", path=(FieldStep("description"),)),
                value="A different description",
            ),
        ]
        outcome = apply_operations(base, named)
        touched = {(item.layer, item.identity) for item in outcome.touched}
        assert touched == {("swc", "ExampleSWC")}
        assert touched <= {(operation.address.layer, operation.address.element) for operation in named}

    def test_a_create_and_a_remove_are_touched_with_one_side_absent(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="compositions", element="NewComposition"),
                    value={"name": "NewComposition", "title": "A second composition", "description": ""},
                ),
                RemoveOperation(address=Address(layer="interfaces", element="IDiagnosticService")),
            ],
        )
        assert outcome.violations == ()
        by_identity = {item.identity: item for item in outcome.touched}
        assert by_identity["NewComposition"].before is None and by_identity["NewComposition"].after is not None
        assert by_identity["IDiagnosticService"].after is None


class TestNothingIsWritten:
    """The apply is in memory: no path is opened and no byte moves."""

    def test_the_model_is_byte_identical_after_an_accepted_apply(self, tmp_path: Path) -> None:
        model_root = _model_root(tmp_path, "read-swc-element")
        before = fixtures.model_state_key(model_root)
        snapshot = load_snapshot(str(model_root))
        outcome = apply_operations(
            snapshot,
            [
                UpdateOperation(
                    address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("priority"),)),
                    value=3,
                )
            ],
        )
        assert outcome.touched  # the apply really did something in memory
        assert fixtures.model_state_key(model_root) == before

    def test_the_model_is_byte_identical_after_a_refused_apply(self, tmp_path: Path) -> None:
        model_root = _model_root(tmp_path, "read-swc-element")
        before = fixtures.model_state_key(model_root)
        snapshot = load_snapshot(str(model_root))
        outcome = apply_operations(
            snapshot,
            [
                UpdateOperation(
                    address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),)),
                    value="-1us",
                )
            ],
        )
        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert fixtures.model_state_key(model_root) == before


class TestTheMultisetRules:
    """A ``whole_value`` LIST is a multiset; members are counted, never keyed."""

    def test_a_duplicate_is_legal_and_never_an_identity_conflict(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [CreateOperation(address=_events_address(member=TIMING_10MS), value=dict(TIMING_10MS))],
        )
        assert outcome.violations == ()
        assert _events(outcome) == [TIMING_10MS_VALUE, TIMING_10MS_VALUE]

    def test_create_adds_exactly_one_occurrence(self, multiset: Any) -> None:
        outcome = apply_operations(
            multiset,
            [CreateOperation(address=_events_address(member=TIMING_20MS), value=dict(TIMING_20MS))],
        )
        assert outcome.violations == ()
        assert _events(outcome).count(TIMING_20MS_VALUE) == 2

    def test_remove_removes_exactly_one_occurrence(self, multiset: Any) -> None:
        outcome = apply_operations(multiset, [RemoveOperation(address=_events_address(member=TIMING_10MS))])
        assert outcome.violations == ()
        assert _events(outcome).count(TIMING_10MS_VALUE) == 1

    def test_clearing_a_twice_recorded_value_takes_two_removes(self, multiset: Any) -> None:
        outcome = apply_operations(
            multiset,
            [
                RemoveOperation(address=_events_address(member=TIMING_10MS)),
                RemoveOperation(address=_events_address(member=TIMING_10MS)),
            ],
        )
        assert outcome.violations == ()
        assert _events(outcome) == [TIMING_20MS_VALUE]

    def test_a_third_remove_is_inapplicable_rather_than_a_pass_b_code(self, multiset: Any) -> None:
        outcome = apply_operations(
            multiset,
            [RemoveOperation(address=_events_address(member=TIMING_10MS))] * 3,
        )
        assert outcome.violations == ()
        assert [item.reason for item in outcome.inapplicable] == ["no_such_member"]
        assert outcome.inapplicable[0].position == 2

    def test_a_remove_and_a_create_of_one_multiset_member_is_not_a_conflict(self, base: Any) -> None:
        # The one stated exemption from the global ban: one occurrence out and
        # one in is not a replacement.
        outcome = apply_operations(
            base,
            [
                RemoveOperation(address=_events_address(member=TIMING_10MS)),
                CreateOperation(address=_events_address(member=TIMING_10MS), value=dict(TIMING_10MS)),
            ],
        )
        assert outcome.violations == ()
        assert _events(outcome) == [TIMING_10MS_VALUE]

    def test_the_exemption_holds_in_the_other_operation_order_too(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(address=_events_address(member=TIMING_20MS), value=dict(TIMING_20MS)),
                RemoveOperation(address=_events_address(member=TIMING_20MS)),
            ],
        )
        assert outcome.violations == ()
        assert _events(outcome) == [TIMING_10MS_VALUE]

    def test_a_whole_value_member_has_no_addressable_interior(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=_events_address(member=TIMING_10MS),
                    value=dict(TIMING_20MS),
                )
            ],
        )
        assert _codes(outcome) == [IMMUTABLE_FIELD]


class TestAMapIsNotAMultiset:
    """``SWComponent.memory`` is keyed, so both verbs and the ban stay in force."""

    def _memory(self, key: str) -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"), KeyStep(key)))

    def test_create_at_a_held_key_is_an_identity_conflict(self, base: Any) -> None:
        outcome = apply_operations(base, [CreateOperation(address=self._memory("RAM0"), value=1024)])
        assert _codes(outcome) == [IDENTITY_CONFLICT]

    def test_update_at_an_absent_key_is_left_to_pass_c(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._memory("NVRAM"), value=1024)])
        assert outcome.violations == ()
        assert [item.reason for item in outcome.inapplicable] == ["no_such_key"]

    def test_update_at_a_held_key_applies(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._memory("RAM0"), value=4096)])
        assert outcome.violations == ()
        swc = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
        assert swc["memory"]["RAM0"] == 4096

    def test_a_remove_and_a_create_at_one_key_is_banned(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                RemoveOperation(address=self._memory("RAM0")),
                CreateOperation(address=self._memory("RAM0"), value=4096),
            ],
        )
        assert _codes(outcome) == [IDENTITY_CONFLICT]


class TestTheSameIdentityBanIsGlobal:
    """The same-identity replacement ban is not a rule about ``group``."""

    def test_it_fires_for_a_nested_collection_member(self, base: Any) -> None:
        port = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pOut")))
        outcome = apply_operations(
            base,
            [
                RemoveOperation(address=port),
                CreateOperation(
                    address=port,
                    value={"name": "pOut", "direction": "require", "interface": "IExample", "description": ""},
                ),
            ],
        )
        assert _codes(outcome) == [IDENTITY_CONFLICT]
        assert outcome.violations[0].addresses == (port,)

    def test_it_fires_in_the_create_then_remove_order_too(self, base: Any) -> None:
        address = Address(layer="compositions", element="NewComposition")
        outcome = apply_operations(
            base,
            [
                CreateOperation(address=address, value={"name": "NewComposition", "title": "New", "description": "x"}),
                RemoveOperation(address=address),
            ],
        )
        assert _codes(outcome) == [IDENTITY_CONFLICT]

    def test_a_remove_and_a_create_at_different_identities_is_the_rename_case(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                RemoveOperation(address=Address(layer="compositions", element="Root")),
                CreateOperation(
                    address=Address(layer="compositions", element="RenamedRoot"),
                    value={"name": "RenamedRoot", "title": "Renamed", "description": "x"},
                ),
            ],
        )
        assert outcome.violations == ()

    def test_an_update_never_participates_in_the_ban(self, base: Any) -> None:
        address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("description"),))
        outcome = apply_operations(
            base,
            [UpdateOperation(address=address, value="a"), UpdateOperation(address=address, value="b")],
        )
        assert outcome.violations == ()


class TestPassBCodes:
    """Each of the four codes, at the place the registry raises it."""

    def test_an_undeclared_layer_is_unknown_field(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [UpdateOperation(address=Address(layer="galaxies", element="x", path=(FieldStep("y"),)), value=1)],
        )
        assert _codes(outcome) == [UNKNOWN_FIELD]

    def test_an_undeclared_nested_field_is_unknown_field(self, base: Any) -> None:
        address = Address(
            layer="swc",
            element="ExampleSWC",
            path=(FieldStep("runnables"), MemberStep("ExampleRun"), FieldStep("deadline_us")),
        )
        outcome = apply_operations(base, [UpdateOperation(address=address, value=5)])
        assert _codes(outcome) == [UNKNOWN_FIELD]
        assert outcome.violations[0].addresses == (address,)

    def test_a_create_payload_carrying_an_undeclared_field_is_unknown_field(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="compositions", element="NewComposition"),
                    value={"name": "NewComposition", "title": "New", "description": "x", "asil": "B"},
                )
            ],
        )
        # Reported at the offending field's own address, which is what a client
        # has to fix — Pydantic would have dropped it silently.
        assert _codes(outcome) == [UNKNOWN_FIELD]
        assert outcome.violations[0].addresses[0].path == (FieldStep("asil"),)

    def test_the_identity_field_is_immutable(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("name"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value="Task_20ms")])
        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_the_placement_field_is_immutable(self, base: Any) -> None:
        address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("group"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value="sensors")])
        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_a_kind_discriminator_is_immutable(self, base: Any) -> None:
        address = Address(layer="implementation_types", element="float32", path=(FieldStep("kind"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value="value_type")])
        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_a_member_identity_is_immutable(self, base: Any) -> None:
        address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pOut")))
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=address,
                    value={"name": "pRenamed", "direction": "provide", "interface": "IExample", "description": ""},
                )
            ],
        )
        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_a_bound_is_a_schema_violation(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("period"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value=0.0)])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_nan_is_a_schema_violation(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("period"),))
        outcome = apply_operations(base, [UpdateOperation(address=address, value=float("nan"))])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_create_whose_payload_disagrees_with_its_address_is_a_schema_violation(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="compositions", element="NewComposition"),
                    value={"name": "OtherName", "title": "Other", "description": "x"},
                )
            ],
        )
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_create_at_a_field_address_has_no_meaning(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("priority"),))
        outcome = apply_operations(base, [CreateOperation(address=address, value=1)])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_remove_at_a_field_address_has_no_meaning(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),))
        outcome = apply_operations(base, [RemoveOperation(address=address)])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_an_unknown_kind_discriminator_is_a_schema_violation(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="implementation_types", element="weird"),
                    value={"name": "weird", "kind": "matrix", "size": 8},
                )
            ],
        )
        assert _codes(outcome) == [SCHEMA_VIOLATION]


# The pass boundary.


class TestPassBDoesNotStopAtTheFirstProblem:
    """One round trip reports everything pass B can see."""

    def test_every_violation_is_reported_in_operation_order(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=Address(layer="swc", element="ExampleSWC", path=(FieldStep("safety_level"),)),
                    value="ASIL_B",
                ),
                UpdateOperation(
                    address=Address(layer="hardware", element="RAM0", path=(FieldStep("kind"),)),
                    value="core",
                ),
                UpdateOperation(
                    address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),)),
                    value="-1us",
                ),
            ],
        )
        assert _codes(outcome) == [UNKNOWN_FIELD, IMMUTABLE_FIELD, SCHEMA_VIOLATION]

    def test_a_refused_operation_leaves_the_staged_tree_untouched(self, base: Any) -> None:
        staged_before = deepcopy(StagedModel.from_snapshot(base).elements)
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),)),
                    value="-1us",
                )
            ],
        )
        assert outcome.staged.elements == staged_before
        assert outcome.touched == ()

    def test_an_unresolvable_address_is_pass_cs_and_the_walk_continues(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=Address(layer="swc", element="NoSuchSWC", path=(FieldStep("description"),)),
                    value="x",
                ),
                UpdateOperation(
                    address=Address(layer="swc", element="ExampleSWC", path=(FieldStep("safety_level"),)),
                    value="x",
                ),
            ],
        )
        assert [item.reason for item in outcome.inapplicable] == ["no_such_element"]
        assert outcome.inapplicable[0].position == 0
        assert _codes(outcome) == [UNKNOWN_FIELD]


class TestPassBIsTheDeclaredSeam:
    """``passes.pass_b`` is the surface; the mechanics live in ``apply``."""

    def test_pass_b_is_the_apply(self, base: Any) -> None:
        operations = [
            UpdateOperation(
                address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("priority"),)),
                value=4,
            )
        ]
        assert pass_b(operations, base).touched == apply_operations(base, operations).touched

    def test_pass_b_raises_nothing_of_its_own(self, base: Any) -> None:
        # The engine composes the errorResult, because pass C's violations join
        # the same one and the primary is whichever pass order reached first.
        outcome = pass_b(
            [
                UpdateOperation(
                    address=Address(layer="swc", element="ExampleSWC", path=(FieldStep("safety_level"),)),
                    value="x",
                )
            ],
            base,
        )
        assert _codes(outcome) == [UNKNOWN_FIELD]


# Pass C: a later supplier or none.


def _diagnose(snapshot: Any, operations: list[Any]) -> list[Any]:
    """Pass B then pass C, which is the order and the only order they run in."""
    return list(pass_c(operations, pass_b(operations, snapshot).inapplicable))


def _swc(element: str, *path: Any) -> Address:
    return Address(layer="swc", element=element, path=tuple(path))


class TestTheMissingTargetIsTheShortestAbsentPrefix:
    """What pass C asks its question *about*, which the address alone cannot say."""

    def test_a_miss_on_the_element_names_the_element(self, base: Any) -> None:
        outcome = pass_b([UpdateOperation(address=_swc("NoSuchSWC", FieldStep("description")), value="x")], base)
        assert outcome.inapplicable[0].missing == Address(layer="swc", element="NoSuchSWC")

    def test_a_miss_deep_inside_an_absent_element_still_names_the_element(self, base: Any) -> None:
        address = _swc("NoSuchSWC", FieldStep("memory"), KeyStep("RAM0"))
        outcome = pass_b([UpdateOperation(address=address, value=1024)], base)
        assert outcome.inapplicable[0].missing == Address(layer="swc", element="NoSuchSWC")

    def test_a_miss_on_a_member_names_the_member(self, base: Any) -> None:
        address = _swc("ExampleSWC", FieldStep("ports"), MemberStep("pNew"), FieldStep("interface"))
        outcome = pass_b([UpdateOperation(address=address, value="IExample")], base)
        assert outcome.inapplicable[0].missing == _swc("ExampleSWC", FieldStep("ports"), MemberStep("pNew"))

    def test_a_miss_on_a_map_key_names_the_entry(self, base: Any) -> None:
        address = _swc("ExampleSWC", FieldStep("memory"), KeyStep("NVRAM"))
        outcome = pass_b([UpdateOperation(address=address, value=1024)], base)
        assert outcome.inapplicable[0].missing == address


class TestPassCDiscriminates:
    """`partial-change-set` where a later operation supplies it, `broken-reference` where none does."""

    def test_an_update_before_the_create_that_supplies_it_is_partial(self, base: Any) -> None:
        # The shape `partial-change-set-update-before-create` states, as a unit:
        # the outcome is consistent and only the ordered application is not.
        address = _swc("SpeedSensorSWC", FieldStep("description"))
        violations = _diagnose(
            base,
            [
                UpdateOperation(address=address, value="Reads the wheel speed sensors"),
                CreateOperation(
                    address=Address(layer="swc", element="SpeedSensorSWC"),
                    value={"name": "SpeedSensorSWC", "group": "sensors", "type": "application"},
                ),
            ],
        )
        assert [violation.error for violation in violations] == [PARTIAL_CHANGE_SET]
        assert violations[0].failure_class == "partial"
        # The address is the one the CLIENT wrote, not the missing prefix.
        assert violations[0].addresses == (address,)

    def test_the_same_two_operations_in_the_other_order_simply_apply(self, base: Any) -> None:
        # The other direction of the boundary: nothing is wrong with the set
        # except its order, so putting the create first leaves pass C silent.
        operations = [
            CreateOperation(
                address=Address(layer="swc", element="SpeedSensorSWC"),
                value={"name": "SpeedSensorSWC", "group": "sensors", "type": "application"},
            ),
            UpdateOperation(address=_swc("SpeedSensorSWC", FieldStep("description")), value="Reads the sensors"),
        ]
        outcome = pass_b(operations, base)
        assert outcome.violations == () and outcome.inapplicable == ()
        assert pass_c(operations, outcome.inapplicable) == ()

    def test_an_address_nothing_supplies_is_a_broken_reference(self, base: Any) -> None:
        address = _swc("NoSuchSWC", FieldStep("description"))
        violations = _diagnose(base, [UpdateOperation(address=address, value="x")])
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE]
        assert violations[0].failure_class == "invalid"
        assert violations[0].addresses == (address,)

    def test_a_later_remove_never_supplies(self, base: Any) -> None:
        # A remove takes something away; it can never be the operation that
        # would have made an earlier one applicable.
        violations = _diagnose(
            base,
            [
                UpdateOperation(address=_swc("NoSuchSWC", FieldStep("description")), value="x"),
                RemoveOperation(address=Address(layer="swc", element="NoSuchSWC")),
            ],
        )
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE, BROKEN_REFERENCE]

    def test_a_later_update_at_the_same_address_never_supplies(self, base: Any) -> None:
        # An update needs the thing to be there already, so two updates at one
        # absent map key are two broken references and not a reordering.
        address = _swc("ExampleSWC", FieldStep("memory"), KeyStep("NVRAM"))
        violations = _diagnose(
            base,
            [UpdateOperation(address=address, value=1024), UpdateOperation(address=address, value=2048)],
        )
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE, BROKEN_REFERENCE]

    def test_a_later_update_at_a_strict_prefix_does_supply(self, base: Any) -> None:
        # Replacing the whole map supplies the entry the earlier operation named.
        violations = _diagnose(
            base,
            [
                UpdateOperation(address=_swc("ExampleSWC", FieldStep("memory"), KeyStep("NVRAM")), value=1024),
                UpdateOperation(address=_swc("ExampleSWC", FieldStep("memory")), value={"RAM0": 2048, "NVRAM": 512}),
            ],
        )
        assert [violation.error for violation in violations] == [PARTIAL_CHANGE_SET]

    def test_a_later_create_at_a_prefix_of_the_target_supplies_it(self, base: Any) -> None:
        # The miss is on the element, so the supplying create sits above the address.
        violations = _diagnose(
            base,
            [
                UpdateOperation(address=_swc("SpeedSensorSWC", FieldStep("memory"), KeyStep("RAM0")), value=1024),
                CreateOperation(
                    address=Address(layer="swc", element="SpeedSensorSWC"),
                    value={"name": "SpeedSensorSWC", "group": "sensors", "type": "application"},
                ),
            ],
        )
        assert [violation.error for violation in violations] == [PARTIAL_CHANGE_SET]

    def test_a_later_create_of_a_collection_member_supplies_it(self, base: Any) -> None:
        violations = _diagnose(
            base,
            [
                UpdateOperation(
                    address=_swc("ExampleSWC", FieldStep("ports"), MemberStep("pNew"), FieldStep("description")),
                    value="x",
                ),
                CreateOperation(
                    address=_swc("ExampleSWC", FieldStep("ports"), MemberStep("pNew")),
                    value={"name": "pNew", "direction": "provide", "interface": "IExample"},
                ),
            ],
        )
        assert [violation.error for violation in violations] == [PARTIAL_CHANGE_SET]

    def test_a_refused_earlier_create_is_no_supplier(self, base: Any) -> None:
        # Pass B refused the create, so the element is still absent when the
        # update runs and nothing after it supplies the element.
        violations = _diagnose(
            base,
            [
                CreateOperation(
                    address=Address(layer="swc", element="SpeedSensorSWC"),
                    value={"name": "SpeedSensorSWC", "group": "sensors", "type": "not_a_declared_type"},
                ),
                UpdateOperation(address=_swc("SpeedSensorSWC", FieldStep("description")), value="x"),
            ],
        )
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE]


class TestPassCReportsEveryInapplicableOperation:
    """One round trip, never the first miss only."""

    def test_both_classes_are_reported_in_operation_order(self, base: Any) -> None:
        violations = _diagnose(
            base,
            [
                UpdateOperation(address=_swc("NoSuchSWC", FieldStep("description")), value="x"),
                UpdateOperation(address=_swc("SpeedSensorSWC", FieldStep("description")), value="y"),
                CreateOperation(
                    address=Address(layer="swc", element="SpeedSensorSWC"),
                    value={"name": "SpeedSensorSWC", "group": "sensors", "type": "application"},
                ),
            ],
        )
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE, PARTIAL_CHANGE_SET]

    def test_pass_c_raises_nothing_of_its_own(self, base: Any) -> None:
        # Like pass B: the engine composes the errorResult, because both passes'
        # violations join one round trip and the primary is the first in pass order.
        assert isinstance(_diagnose(base, [RemoveOperation(address=Address(layer="tasks", element="No"))]), list)

    def test_no_inapplicable_operation_is_no_violation(self, base: Any) -> None:
        assert pass_c([], ()) == ()


class TestPassCOwnsOnlyTheAddressHalfOfBrokenReference:
    """The scope boundary against pass D, stated as a test rather than a comment."""

    def test_removing_a_still_referenced_element_is_not_pass_cs(self, tmp_path: Path) -> None:
        # Two ports still name `IExample`, but the remove is applicable on its
        # own: the violation exists only in the assembled outcome, pass D's.
        snapshot = load_snapshot(str(_model_root(tmp_path, "broken-reference-remove-referenced-interface")))
        operations = [RemoveOperation(address=Address(layer="interfaces", element="IExample"))]
        outcome = pass_b(operations, snapshot)
        assert outcome.violations == () and outcome.inapplicable == ()
        assert pass_c(operations, outcome.inapplicable) == ()


class TestBothPassesReachOneRoundTrip:
    """Pass B's violations and pass C's arrive together, in pass order."""

    def test_the_engine_concatenates_them_primary_first(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "combined", "read-swc-element")
        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "One pass-B refusal and one pass-C miss in one request.",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "safety_level"}]},
                        "value": "ASIL_B",
                    },
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "NoSuchSWC", "path": [{"field": "description"}]},
                        "value": "x",
                    },
                ],
            },
            project=project,
        )
        assert response["response"] == "error"
        assert response["error"] == UNKNOWN_FIELD
        assert [item["error"] for item in response["additional"]] == [BROKEN_REFERENCE]


class TestTheExampleVectorPayloadsBind:
    """Every ``create`` the shipped examples carry is written in registry vocabulary.

    The unknown-key check's other failure: a contract payload refused because the
    registry publishes a different name for the field.
    """

    @pytest.mark.parametrize(
        "vector_name",
        sorted(
            name
            for name, vector in fixtures.VECTORS.items()
            if vector["kind"] == "example"
            and any(op["operation"] == "create" for op in vector["input"].get("operations", []))
        ),
    )
    def test_no_create_payload_carries_an_undeclared_field(self, vector_name: str, tmp_path: Path) -> None:
        snapshot = load_snapshot(str(_model_root(tmp_path, vector_name)))
        operations = [
            CreateOperation(address=Address.from_json(op["address"]), value=op["value"])
            for op in fixtures.VECTORS[vector_name]["input"]["operations"]
            if op["operation"] == "create"
        ]
        outcome = apply_operations(snapshot, operations)
        assert UNKNOWN_FIELD not in _codes(outcome), [violation.message for violation in outcome.violations]


class TestEveryPayloadPositionIsBound:
    """``unknown-field`` is about the registry's vocabulary, not about an address step.

    Every value a client sends is bound, at every depth: Pydantic's
    ``extra="ignore"`` would drop an undeclared key and call it a success.
    """

    _PORT = {
        "name": "pOut",
        "direction": "provide",
        "interface": "IExample",
        "description": "Example output port providing data",
        "safety_level": "ASIL_B",
    }

    @staticmethod
    def _swc(*path: Any) -> Address:
        return Address(layer="swc", element="ExampleSWC", path=tuple(path))

    def test_a_member_replacement_is_bound(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._swc(FieldStep("ports"), MemberStep("pOut")),
                    value=dict(self._PORT),
                )
            ],
        )

        assert _codes(outcome) == [UNKNOWN_FIELD]
        assert outcome.violations[0].addresses[0].path[-1] == FieldStep("safety_level")

    def test_a_whole_collection_replacement_is_bound_member_by_member(self, base: Any) -> None:
        outcome = apply_operations(
            base, [UpdateOperation(address=self._swc(FieldStep("ports")), value=[dict(self._PORT)])]
        )

        assert _codes(outcome) == [UNKNOWN_FIELD]
        # Reported at the member's own address below the field, which is where a
        # client would go to fix it.
        assert outcome.violations[0].addresses[0].path == (
            FieldStep("ports"),
            MemberStep("pOut"),
            FieldStep("safety_level"),
        )

    def test_an_object_typed_field_replacement_is_bound(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=Address(layer="connectors", element="Example_to_Consumer", path=(FieldStep("to"),)),
                    value={"swc": "ExampleConsumerSWC", "port": "rIn", "qualifier": "strong"},
                )
            ],
        )

        assert _codes(outcome) == [UNKNOWN_FIELD]
        assert outcome.violations[0].addresses[0].path == (FieldStep("to"), FieldStep("qualifier"))

    def test_a_create_payload_is_bound_into_its_nested_members(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="swc", element="NewSWC"),
                    value={
                        "name": "NewSWC",
                        "group": "example",
                        "type": "application",
                        "ports": [dict(self._PORT) | {"name": "pNew"}],
                    },
                )
            ],
        )

        assert _codes(outcome) == [UNKNOWN_FIELD]
        assert outcome.violations[0].addresses[0].path == (
            FieldStep("ports"),
            MemberStep("pNew"),
            FieldStep("safety_level"),
        )

    def test_the_staged_tree_is_untouched_by_a_refused_payload(self, base: Any) -> None:
        before = deepcopy(StagedModel.from_snapshot(base).elements)

        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._swc(FieldStep("ports"), MemberStep("pOut")),
                    value=dict(self._PORT),
                )
            ],
        )

        assert outcome.staged.elements == before
        assert outcome.touched == ()


class TestAWholeCollectionUpdateIsTheReorderingPrimitive:
    """A whole-collection ``update`` only reorders.

    A renamed member is ``immutable-field``; an added or dropped one is
    ``schema-violation`` naming the verb that owns membership.
    """

    _RUN = {"name": "ExampleRun", "min_start_interval": 0.01, "events": [dict(TIMING_10MS)]}
    _SECOND_PORT = {"name": "pSecond", "direction": "provide", "interface": "IExample", "description": ""}
    _FIRST_PORT = {
        "name": "pOut",
        "direction": "provide",
        "interface": "IExample",
        "description": "Example output port providing data",
    }

    @staticmethod
    def _runnables() -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("runnables"),))

    @staticmethod
    def _ports(*path: Any) -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), *path))

    def test_a_pure_reorder_is_accepted(self, base: Any) -> None:
        # A second port first, so there is an order to reverse at all. The
        # `create` is how membership changes; the `update` then only reorders.
        outcome = apply_operations(
            base,
            [
                CreateOperation(address=self._ports(MemberStep("pSecond")), value=dict(self._SECOND_PORT)),
                UpdateOperation(address=self._ports(), value=[dict(self._SECOND_PORT), dict(self._FIRST_PORT)]),
            ],
        )

        assert _codes(outcome) == []
        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
        assert [port["name"] for port in staged["ports"]] == ["pSecond", "pOut"]

    def test_a_renamed_member_is_immutable_field(self, base: Any) -> None:
        outcome = apply_operations(
            base, [UpdateOperation(address=self._runnables(), value=[dict(self._RUN) | {"name": "RenamedRun"}])]
        )

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        assert outcome.touched == ()

    def test_an_added_member_names_the_create_that_owns_it(self, base: Any) -> None:
        added = {"name": "SecondRun", "min_start_interval": 0.0, "events": []}
        outcome = apply_operations(base, [UpdateOperation(address=self._runnables(), value=[dict(self._RUN), added])])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "create" in outcome.violations[0].message

    def test_a_dropped_member_names_the_remove_that_owns_it(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._runnables(), value=[])])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "remove" in outcome.violations[0].message

    @staticmethod
    def _memory() -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"),))

    def test_a_map_keeps_its_key_set(self, base: Any) -> None:
        outcome = apply_operations(
            base, [UpdateOperation(address=self._memory(), value={"RAM0": 2048, "NoSuchPool": 16})]
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "create" in outcome.violations[0].message

    def test_a_dropped_map_key_names_the_remove_that_owns_it(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._memory(), value={})])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "remove" in outcome.violations[0].message

    def test_a_whole_map_value_change_is_the_content_change_the_verb_is_for(self, base: Any) -> None:
        # The key set is unchanged, so this is a content change; comparing the
        # mapping's values instead of its keys would refuse it as membership.
        outcome = apply_operations(base, [UpdateOperation(address=self._memory(), value={"RAM0": 4096})])

        assert _codes(outcome) == []
        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
        assert staged["memory"] == {"RAM0": 4096}

    def test_a_renamed_map_key_is_immutable_field(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._memory(), value={"ROM": 2048})])

        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_a_renamed_map_key_is_immutable_field_at_a_zero_budget_too(self, base: Any) -> None:
        # A budget of 0 is legal and `Counter.__sub__` drops non-positive counts,
        # so counting values let a rename between zero-valued keys pass: the
        # guard failed open.
        outcome = apply_operations(
            base,
            [
                UpdateOperation(address=self._memory(), value={"RAM0": 0}),
                UpdateOperation(address=self._memory(), value={"ROM": 0}),
            ],
        )

        assert _codes(outcome) == [IMMUTABLE_FIELD]

    def test_a_zero_valued_key_cannot_be_added_or_dropped_either(self, base: Any) -> None:
        added = apply_operations(
            base,
            [
                UpdateOperation(address=self._memory(), value={"RAM0": 0}),
                UpdateOperation(address=self._memory(), value={"RAM0": 0, "ROM": 0}),
            ],
        )
        dropped = apply_operations(
            base,
            [
                UpdateOperation(address=self._memory(), value={"RAM0": 0}),
                UpdateOperation(address=self._memory(), value={}),
            ],
        )

        assert _codes(added) == [SCHEMA_VIOLATION]
        assert _codes(dropped) == [SCHEMA_VIOLATION]

    def test_a_per_member_content_change_is_still_accepted(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [UpdateOperation(address=self._runnables(), value=[dict(self._RUN) | {"min_start_interval": 0.02}])],
        )

        assert _codes(outcome) == []
        assert len(outcome.touched) == 1


class TestTheMembershipRuleHoldsAtEveryDepth:
    """The membership rule holds for a replacement's nested collections, not only its top level.

    Restricting it to the addressed collection would admit the same edit one or
    two levels down.
    """

    _RUN = {"name": "ExampleRun", "min_start_interval": 0.01, "events": [dict(TIMING_10MS)]}

    @staticmethod
    def _runnables(*path: Any) -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("runnables"), *path))

    @staticmethod
    def _operations(*path: Any) -> Address:
        return Address(layer="interfaces", element="IDiagnosticService", path=(FieldStep("operations"), *path))

    def test_an_event_added_inside_a_replaced_collection_is_refused(self, base: Any) -> None:
        nested = dict(self._RUN) | {"events": [dict(TIMING_10MS), dict(TIMING_20MS)]}
        outcome = apply_operations(base, [UpdateOperation(address=self._runnables(), value=[nested])])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        # Reported where the client would write the `create` that owns membership.
        assert outcome.violations[0].addresses == (self._runnables(MemberStep("ExampleRun"), FieldStep("events")),)

    def test_an_event_added_through_the_member_address_is_refused_too(self, base: Any) -> None:
        nested = dict(self._RUN) | {"events": [dict(TIMING_10MS), dict(TIMING_20MS)]}
        outcome = apply_operations(
            base, [UpdateOperation(address=self._runnables(MemberStep("ExampleRun")), value=nested)]
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert outcome.violations[0].addresses == (self._runnables(MemberStep("ExampleRun"), FieldStep("events")),)

    def test_an_argument_dropped_two_levels_down_is_refused(self, base: Any) -> None:
        replacement = [
            {"name": "ReadDtc", "arguments": [{"name": "dtc_id", "direction": "in", "implementation_type": "uint16"}]},
            {
                "name": "ClearDtc",
                "arguments": [
                    {"name": "dtc_id", "direction": "in", "implementation_type": "uint16"},
                    {"name": "result", "direction": "out", "implementation_type": "uint8"},
                ],
            },
        ]
        outcome = apply_operations(base, [UpdateOperation(address=self._operations(), value=replacement)])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "remove" in outcome.violations[0].message
        assert outcome.violations[0].addresses == (self._operations(MemberStep("ReadDtc"), FieldStep("arguments")),)

    def test_a_nested_reorder_is_still_accepted(self, base: Any) -> None:
        # The member identities come back unchanged: a reorder, one level down.
        replacement = [
            {
                "name": "ReadDtc",
                "arguments": [
                    {"name": "status", "direction": "out", "implementation_type": "uint8"},
                    {"name": "dtc_id", "direction": "in", "implementation_type": "uint16"},
                ],
            },
            {
                "name": "ClearDtc",
                "arguments": [
                    {"name": "dtc_id", "direction": "in", "implementation_type": "uint16"},
                    {"name": "result", "direction": "out", "implementation_type": "uint8"},
                ],
            },
        ]
        outcome = apply_operations(base, [UpdateOperation(address=self._operations(), value=replacement)])

        assert _codes(outcome) == []
        assert len(outcome.touched) == 1

    def test_a_created_element_carries_no_membership_baseline(self, base: Any) -> None:
        # A `create` replaces nothing, so its nested collections change no membership.
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="interfaces", element="INewService"),
                    value={
                        "name": "INewService",
                        "kind": "client_server",
                        "operations": [
                            {
                                "name": "Ping",
                                "arguments": [
                                    {"name": "a", "direction": "in", "implementation_type": "uint8"},
                                    {"name": "b", "direction": "out", "implementation_type": "uint8"},
                                ],
                            }
                        ],
                    },
                )
            ],
        )

        assert _codes(outcome) == []


class TestAnOmittedCollectionIsAMembershipChangeToo:
    """A collection the replacement omits is a membership change, as ``events: []`` is.

    A replacement is re-validated through its Pydantic class, so an omitted
    collection comes back at its default; walking only the spelled keys would let
    an update silently drop every event.
    """

    @staticmethod
    def _runnables(*path: Any) -> Address:
        return Address(layer="swc", element="ExampleSWC", path=(FieldStep("runnables"), *path))

    @staticmethod
    def _operations(*path: Any) -> Address:
        return Address(layer="interfaces", element="IDiagnosticService", path=(FieldStep("operations"), *path))

    def test_omitting_events_at_the_member_address_is_refused(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._runnables(MemberStep("ExampleRun")),
                    value={"name": "ExampleRun", "min_start_interval": 0.01},
                )
            ],
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert outcome.violations[0].addresses == (self._runnables(MemberStep("ExampleRun"), FieldStep("events")),)

    def test_omitting_events_inside_a_replaced_collection_is_refused(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [UpdateOperation(address=self._runnables(), value=[{"name": "ExampleRun", "min_start_interval": 0.01}])],
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert outcome.violations[0].addresses == (self._runnables(MemberStep("ExampleRun"), FieldStep("events")),)

    def test_omitting_arguments_two_levels_down_is_refused(self, base: Any) -> None:
        outcome = apply_operations(
            base, [UpdateOperation(address=self._operations(MemberStep("ReadDtc")), value={"name": "ReadDtc"})]
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert outcome.violations[0].addresses == (self._operations(MemberStep("ReadDtc"), FieldStep("arguments")),)

    def test_the_two_spellings_of_the_same_edit_answer_alike(self, base: Any) -> None:
        # An omitted and an explicitly emptied collection get one answer.
        omitted = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._runnables(MemberStep("ExampleRun")),
                    value={"name": "ExampleRun", "min_start_interval": 0.01},
                )
            ],
        )
        emptied = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._runnables(MemberStep("ExampleRun")),
                    value={"name": "ExampleRun", "min_start_interval": 0.01, "events": []},
                )
            ],
        )

        assert _codes(omitted) == _codes(emptied)
        assert omitted.violations[0].addresses == emptied.violations[0].addresses
        assert omitted.violations[0].message == emptied.violations[0].message

    def test_a_create_may_omit_a_defaulted_collection(self, base: Any) -> None:
        # A `create` stands in for nothing, so an omitted collection is its default.
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(
                        layer="swc", element="ExampleSWC", path=(FieldStep("runnables"), MemberStep("New"))
                    ),
                    value={"name": "New", "min_start_interval": 0.01},
                )
            ],
        )

        assert _codes(outcome) == []

    def test_an_omitted_scalar_is_not_touched_by_the_rule(self, base: Any) -> None:
        # The rule is about membership; an omitted defaulted scalar is a value
        # change, which is what an `update` is for.
        outcome = apply_operations(
            base,
            [
                UpdateOperation(
                    address=self._runnables(MemberStep("ExampleRun")),
                    value={"name": "ExampleRun", "events": [dict(TIMING_10MS)]},
                )
            ],
        )

        assert _codes(outcome) == []


class TestTheStagedTreeHoldsTheValidatedValue:
    """``touched`` compares like with like, in one vocabulary.

    A payload that validates to the baseline value (``"1536"`` for ``1536``) leaves
    the element untouched; a touched element with an empty delta would fail the
    commit with ``UnexplainedChangeError``, a fault with no protocol code.
    """

    _RAM = Address(layer="swc", element="ExampleConsumerSWC", path=(FieldStep("memory"), KeyStep("RAM0")))

    def test_a_payload_that_validates_to_the_baseline_value_is_not_touched(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._RAM, value="1536")])

        assert _codes(outcome) == []
        assert outcome.touched == ()

    def test_the_staged_value_is_the_validated_one_not_the_client_spelling(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=self._RAM, value="2048")])

        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleConsumerSWC")
        assert staged["memory"]["RAM0"] == 2048

    def test_a_created_element_is_staged_in_the_published_vocabulary(self, base: Any) -> None:
        outcome = apply_operations(
            base,
            [
                CreateOperation(
                    address=Address(layer="swc", element="NewSWC"),
                    value={"name": "NewSWC", "group": "example", "type": "application"},
                )
            ],
        )

        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "NewSWC")
        # Exactly what `read` would answer: placement as its declared field,
        # every declared default filled in.
        assert staged["group"] == "example"
        assert staged["ports"] == []
        assert staged["memory"] == {}


class TestTheIdentityIsHeldToTheSameGuardAsThePlacementValue:
    """The identity and the placement value, both composed into a path, are refused by one guard.

    ``$defs/identity`` does not refuse an embedded ``..``; the registry's
    composition guard does, so ``a..b`` is refused at bind time, not at
    materialization as a placement refusal at a field the layer does not declare.
    """

    _CREATE = CreateOperation(
        address=Address(layer="compositions", element="a..b"),
        value={"name": "a..b", "title": "A composition"},
    )

    def test_an_embedded_traversal_identity_is_refused_at_bind_time(self, base: Any) -> None:
        outcome = apply_operations(base, [self._CREATE])

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert "identity" in outcome.violations[0].message

    def test_it_is_reported_at_the_element_with_no_path(self, base: Any) -> None:
        # No path: the identity is not a field of the element, and `compositions`
        # declares no placement field to blame.
        outcome = apply_operations(base, [self._CREATE])

        assert outcome.violations[0].addresses[0] == Address(layer="compositions", element="a..b")

    @pytest.mark.parametrize(
        "identity",
        ["A\nB", "A\x1b[2JB", "A" * 300],
        ids=["newline", "ansi-escape", "too-long"],
    )
    def test_an_unusable_file_name_is_refused_at_bind_time_rather_than_at_apply_time(
        self, base: Any, identity: str
    ) -> None:
        # Past every pass, a control character or a length no file name can hold
        # would raise a bare `OSError` in materialization, the audit signature of
        # a partially applied model, for a commit that applied nothing.
        outcome = apply_operations(
            base,
            [CreateOperation(address=Address(layer="compositions", element=identity), value={"name": identity})],
        )

        assert _codes(outcome) == [SCHEMA_VIOLATION]
        assert outcome.violations[0].addresses[0] == Address(layer="compositions", element=identity)


class TestAnAmbiguousBaselineIsRefusedBeforeBinding:
    """Two files, one identity: an address that names two elements is a guess.

    The baseline index is last-wins, so without the refusal a ``remove`` would
    answer success with an empty delta for an operation that did nothing.
    """

    @staticmethod
    def _duplicate(tmp_path: Path) -> Path:
        project = fixtures.build_fixture(tmp_path / "duplicate", "read-swc-element")
        source = project / "model" / "swc" / "example" / "ExampleSWC.yaml"
        other = project / "model" / "swc" / "other"
        other.mkdir(parents=True)
        (other / "ExampleSWC.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return project

    def _remove(self, project: Path) -> dict[str, Any]:
        return handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Remove ExampleSWC.",
                "operations": [{"operation": "remove", "address": {"layer": "swc", "element": "ExampleSWC"}}],
            },
            project=project,
        )

    def test_it_is_answered_with_the_condition_that_is_really_wrong(self, tmp_path: Path) -> None:
        project = self._duplicate(tmp_path)

        response = self._remove(project)

        assert response["error"] == "consistency-violation"
        assert response["addresses"] == [{"layer": "swc", "element": "ExampleSWC"}]
        assert "2 elements" in response["message"]

    def test_neither_file_is_removed(self, tmp_path: Path) -> None:
        project = self._duplicate(tmp_path)

        self._remove(project)

        assert (project / "model" / "swc" / "example" / "ExampleSWC.yaml").exists()
        assert (project / "model" / "swc" / "other" / "ExampleSWC.yaml").exists()

    def test_an_unambiguous_baseline_is_not_refused_by_it(self, tmp_path: Path) -> None:
        # The check quantifies over the whole baseline, so the plain fixture is
        # the evidence that it does not fire on every change set.
        project = fixtures.build_fixture(tmp_path / "plain", "read-swc-element")

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Retime the task.",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                        "value": 6,
                    }
                ],
            },
            project=project,
        )

        assert response["status"] == "staged"


class TestAnAmbiguousMemberIsRefusedBeforeBinding:
    """The same question one level down: a member identity must pick exactly one member.

    Where the loader refuses no duplicate, the collection is exercised end to
    end; the ``composite`` strategy, which the loader guards, against a
    constructed value. Refuses an apply taking the first match silently, under
    which a ``remove`` reaches ``staged`` with a wrong delta.

    Does not reach: the loader-guarded collections at apply (only the loader's
    refusal is asserted), and ``composite`` beyond ``tasks.runnables``.
    """

    #: ``(case, relative file, document, the collection the duplicate is in)``,
    #: one per ``field``-identified collection the loader does not guard; asserted
    #: complete against the registry below.
    _CASES: tuple[tuple[str, str, str, str], ...] = (
        (
            "swc-ports",
            "swc/example/DupSWC.yaml",
            "name: DupSWC\ntype: application\nrunnables: []\nports:\n"
            "  - {name: pOut, direction: provide, interface: IExample}\n"
            "  - {name: pOut, direction: provide, interface: IExample}\n",
            "ports",
        ),
        (
            "swc-runnables",
            "swc/example/DupSWC.yaml",
            "name: DupSWC\ntype: application\nports: []\nrunnables:\n  - {name: Run}\n  - {name: Run}\n",
            "runnables",
        ),
        (
            "interfaces-data_elements",
            "interfaces/IDup.yaml",
            "name: IDup\nkind: sender_receiver\ndata_elements:\n"
            "  - {name: value, implementation_type: float32}\n  - {name: value, implementation_type: float32}\n",
            "data_elements",
        ),
        (
            "interfaces-operations",
            "interfaces/IDupOps.yaml",
            "name: IDupOps\nkind: client_server\noperations:\n"
            "  - {name: Read, arguments: []}\n  - {name: Read, arguments: []}\n",
            "operations",
        ),
        (
            # Two levels below the element: the depth a hand-written table forgets.
            "interfaces-arguments",
            "interfaces/IDupArgs.yaml",
            "name: IDupArgs\nkind: client_server\noperations:\n  - name: Read\n    arguments:\n"
            "      - {name: dtc_id, direction: in, implementation_type: uint16}\n"
            "      - {name: dtc_id, direction: in, implementation_type: uint16}\n",
            "operations",
        ),
        (
            # `Idle` and not `Off`: YAML 1.1 reads a bare `Off` as `false`.
            "application_types-literals",
            "data_types/application/DupEnum.yaml",
            "name: DupEnum\nkind: enum\nliterals:\n  - {name: Idle, value: 0}\n  - {name: Idle, value: 1}\n",
            "literals",
        ),
        (
            "application_types-fields",
            "data_types/application/DupRecord.yaml",
            "name: DupRecord\nkind: record\nfields:\n"
            "  - {name: speed, type: float32}\n  - {name: speed, type: float32}\n",
            "fields",
        ),
        # `schema.py` refuses no duplicate element name (that is `check-model`'s).
        (
            "implementation_types-elements",
            "data_types/implementation/DupImplRecord.yaml",
            "name: DupImplRecord\nkind: implementation_record\nelements:\n"
            "  - {name: speed, type: float32}\n  - {name: speed, type: float32}\n",
            "elements",
        ),
        # `schema.py` refuses no duplicate entry name (that is `check-model`'s).
        (
            "compu_methods-entries",
            "data_types/compu_methods/DupTable.yaml",
            "name: DupTable\nkind: texttable\nentries:\n  - {name: Idle, value: 0}\n  - {name: Idle, value: 1}\n",
            "entries",
        ),
        # A composition's three collections, `field`-keyed by `name`; `schema.py`
        # refuses a duplicate in none of them.
        (
            "compositions-ports",
            "compositions/DupComp.yaml",
            "name: DupComp\ntitle: Duplicate\nports:\n"
            "  - {name: pOut, direction: provide, interface: IExample}\n"
            "  - {name: pOut, direction: provide, interface: IExample}\n",
            "ports",
        ),
        (
            "compositions-members",
            "compositions/DupCompMembers.yaml",
            "name: DupCompMembers\ntitle: Duplicate\nmembers:\n"
            "  - {name: Inner, type: ExampleSWC}\n  - {name: Inner, type: ExampleSWC}\n",
            "members",
        ),
        (
            "compositions-delegations",
            "compositions/DupCompDelegations.yaml",
            "name: DupCompDelegations\ntitle: Duplicate\ndelegations:\n"
            "  - {name: dOut, port: pOut, member: Inner, member_port: pOut}\n"
            "  - {name: dOut, port: pOut, member: Inner, member_port: pOut}\n",
            "delegations",
        ),
    )

    #: ``(layer, relative file, document)``: collections whose duplicate the loader
    #: refuses, each asserted refused at load; a relaxed rule needs an end-to-end
    #: case. The completeness check compares layers, not collections, so ``swc``'s
    #: ``writes`` and ``calls`` have no document.
    _COMPOSITE_DOCUMENTS: tuple[tuple[str, str, str], ...] = (
        (
            "tasks",
            "tasks/Task_10ms.yaml",
            "name: Task_10ms\nperiod: 0.01\npriority: 5\ncore: 0\nwcet: 1200us\nrunnables:\n"
            "  - {swc: ExampleConsumerSWC, runnable: ExampleConsumerRun}\n"
            "  - {swc: ExampleConsumerSWC, runnable: ExampleConsumerRun}\n",
        ),
        (
            "sequences",
            "sequences/task10ms_consumer_cycle.yaml",
            "name: task10ms_consumer_cycle\ntask: Task_10ms\ntitle: Consumer task execution cycle\nsteps:\n"
            "  - {swc: ExampleConsumerSWC, runnable: ExampleConsumerRun}\n"
            "  - {swc: ExampleConsumerSWC, runnable: ExampleConsumerRun}\n",
        ),
        # A runnable's access list, refused at load by
        # `rule:swc.runnable.reads.no-duplicate-entry`.
        (
            "swc",
            "swc/example/ExampleConsumerSWC.yaml",
            "name: ExampleConsumerSWC\ntype: application\nports:\n"
            "  - {name: rIn, direction: require, interface: IExample}\n"
            "runnables:\n"
            "  - name: ExampleConsumerRun\n"
            "    events:\n"
            "      - {type: timing, period: 0.01}\n"
            "    reads:\n"
            "      - {port: rIn, element: value}\n"
            "      - {port: rIn, element: value, by: value}\n",
        ),
        # A `field`-strategy collection the loader guards, so it is here and not
        # in `_CASES`: `rule:mapping_sets.mapping_set.maps.unique-application`
        # refuses two pairs for one application type.
        (
            "mapping_sets",
            "data_types/mapping_sets/DupSet.yaml",
            "name: DupSet\nmaps:\n"
            "  - {application: Kph, implementation: Speed}\n"
            "  - {application: Kph, implementation: Angle}\n",
        ),
        # A port's com-spec entries, `field`-keyed by `data_element`, refused at
        # load by `rule:swc.port.com_specs.no-duplicate-entry`.
        (
            "swc",
            "swc/example/DupComSpecSWC.yaml",
            "name: DupComSpecSWC\ntype: application\nrunnables: []\nports:\n"
            "  - name: rIn\n    direction: require\n    interface: IExample\n    com_specs:\n"
            "      - {data_element: value, enable_update: true}\n"
            "      - {data_element: value, alive_timeout: 0.1}\n",
        ),
        # A component's variables (keyed by `name`) and a runnable's two variable
        # access lists (by `variable`), each refused at load by its own
        # `no-duplicate-entry` rule.
        (
            "swc",
            "swc/example/DupVariableSWC.yaml",
            "name: DupVariableSWC\ntype: application\nvariables:\n"
            "  - {name: V, access: implicit, implementation_type: uint8}\n"
            "  - {name: V, access: explicit, implementation_type: uint8}\n",
        ),
        (
            "swc",
            "swc/example/DupVariableReadSWC.yaml",
            "name: DupVariableReadSWC\ntype: application\nrunnables:\n"
            "  - name: R\n    reads_variables:\n      - {variable: V}\n      - {variable: V, name: RV_Other}\n",
        ),
        (
            "swc",
            "swc/example/DupVariableWriteSWC.yaml",
            "name: DupVariableWriteSWC\ntype: application\nrunnables:\n"
            "  - name: R\n    writes_variables:\n      - {variable: V}\n      - {variable: V, name: WV_Other}\n",
        ),
        # A component's exclusive areas (keyed by `name`) and a runnable's two
        # area lists (by `area`), each refused at load by its own
        # `no-duplicate-entry` rule.
        (
            "swc",
            "swc/example/DupAreaSWC.yaml",
            "name: DupAreaSWC\ntype: application\nexclusive_areas:\n  - {name: A}\n  - {name: A, description: again}\n",
        ),
        (
            "swc",
            "swc/example/DupCanEnterSWC.yaml",
            "name: DupCanEnterSWC\ntype: application\nrunnables:\n"
            "  - name: R\n    can_enter:\n      - {area: A}\n      - {area: A}\n",
        ),
        (
            "swc",
            "swc/example/DupRunsInsideSWC.yaml",
            "name: DupRunsInsideSWC\ntype: application\nrunnables:\n"
            "  - name: R\n    runs_inside:\n      - {area: A}\n      - {area: A}\n",
        ),
    )

    #: The ``field``-strategy collections whose duplicate the loader refuses, so
    #: no end-to-end case can exist for them. Named rather than widening the
    #: assertion below, so each new exception has to be typed in.
    _FIELD_COLLECTIONS_THE_LOADER_REFUSES: frozenset[tuple[str, str]] = frozenset(
        {
            ("mapping_sets", "maps"),
            ("swc", "com_specs"),
            ("swc", "variables"),
            ("swc", "reads_variables"),
            ("swc", "writes_variables"),
            ("swc", "exclusive_areas"),
            ("swc", "can_enter"),
            ("swc", "runs_inside"),
        }
    )

    @staticmethod
    def _plant(tmp_path: Path, case: str, relative: str, document: str) -> tuple[Path, str, str]:
        project = fixtures.build_fixture(tmp_path / case, "read-swc-element")
        target = project / "model" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8")
        directory, _, filename = relative.rpartition("/")
        # The data description layers keep their elements under `data_types/`,
        # so their directories are not their names.
        layer = {
            "data_types/application": "application_types",
            "data_types/implementation": "implementation_types",
            "data_types/mapping_sets": "mapping_sets",
            "data_types/compu_methods": "compu_methods",
        }.get(directory, directory.split("/")[0])
        return project, layer, filename.removesuffix(".yaml")

    @pytest.mark.parametrize(("case", "relative", "document", "field"), _CASES, ids=[c[0] for c in _CASES])
    def test_an_operation_reaching_into_it_is_refused(
        self, tmp_path: Path, case: str, relative: str, document: str, field: str
    ) -> None:
        project, layer, element = self._plant(tmp_path, case, relative, document)

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Edit inside an element whose collection holds a duplicate identity.",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": layer, "element": element, "path": [{"field": field}]},
                        "value": [],
                    }
                ],
            },
            project=project,
        )

        assert response["error"] == "consistency-violation", case
        assert "2 members" in response["message"], case
        # Reported at the member step that cannot pick one, not the collection.
        reported = response["addresses"][0]
        assert (reported["layer"], reported["element"]) == (layer, element), case
        assert "member" in reported["path"][-1], case

    @staticmethod
    def _uniquely_identified() -> set[tuple[str, str, str]]:
        """``(layer, collection, strategy)`` for every collection an identity must pick one of."""
        from swcraft_tools.editing import registry as reg

        return {
            (layer, descriptor.name, descriptor.collection.member_identity)
            for layer in reg.REGISTRY.layer_names
            for kind in reg.REGISTRY.layer(layer).element_kinds
            for descriptor in kind.fields
            if descriptor.collection is not None and descriptor.collection.member_identity != "whole_value"
        }

    def test_every_uniquely_identified_collection_is_covered(self) -> None:
        # The tables are hand-written; the registry is the authority on which
        # collections need an identity to pick one member, and by which strategy.
        declared = self._uniquely_identified()
        field_arm = {(layer, name) for layer, name, strategy in declared if strategy == "field"}
        composite_arm = {(layer, name) for layer, name, strategy in declared if strategy == "composite"}
        # The case id is `layer-collection`; the tuple's own `field` is the field
        # the *address* names, which for the nested `arguments` case is the
        # `operations` above it — so coverage is read off the id, not off it.
        assert field_arm == {tuple(case.split("-", 1)) for case, _, _, _ in self._CASES} | (
            self._FIELD_COLLECTIONS_THE_LOADER_REFUSES
        )
        assert composite_arm == {
            ("tasks", "runnables"),
            ("sequences", "steps"),
            ("swc", "reads"),
            ("swc", "writes"),
            ("swc", "calls"),
        }
        assert {layer for layer, _, _ in self._COMPOSITE_DOCUMENTS} == {layer for layer, _ in composite_arm} | {
            layer for layer, _ in self._FIELD_COLLECTIONS_THE_LOADER_REFUSES
        }

    @pytest.mark.parametrize(("layer", "relative", "document"), _COMPOSITE_DOCUMENTS, ids=lambda v: str(v)[:24])
    def test_a_composite_duplicate_is_refused_by_the_loader(
        self, tmp_path: Path, layer: str, relative: str, document: str
    ) -> None:
        # The premise of the arms' split, asserted: the loader refuses the
        # document, so the reader never produces the value.
        project = fixtures.build_fixture(tmp_path / layer, "read-swc-element")
        (project / "model" / relative).write_text(document, encoding="utf-8")

        response = handle_request(
            {
                "operation": "read",
                "address": {"layer": layer},
            },
            project=project,
        )

        assert response["error"] == "consistency-violation"
        assert "malformed YAML, or carries a value its schema rejects" in response["message"]

    def test_the_composite_strategy_is_refused_when_a_value_carries_one(self, tmp_path: Path) -> None:
        # Past the schema rule via `model_construct`: the counting keys on the
        # whole `{swc, runnable}` pair, which no `field` case can show, and still
        # refuses the address if the schema rule is relaxed.
        from swcraft_tools.editing.passes import ambiguous_member_violations
        from swcraft_tools.schema import Task, TaskRunnableRef

        project = fixtures.build_fixture(tmp_path / "composite", "read-swc-element")
        ref = TaskRunnableRef(swc="ExampleConsumerSWC", runnable="ExampleConsumerRun")
        task = Task.model_construct(
            name="Task_10ms", period=0.01, priority=5, partition="PARTITION0", runnables=[ref, ref]
        )
        snapshot = load_snapshot(str(project / "model"))
        snapshot.tasks.clear()
        snapshot.tasks.append(task)
        operation = UpdateOperation(
            address=Address(layer="tasks", element="Task_10ms", path=(FieldStep("runnables"),)), value=[]
        )

        violations = ambiguous_member_violations(snapshot, [operation])

        assert len(violations) == 1
        assert "2 members" in violations[0].message
        assert violations[0].addresses[0].path[-1] == MemberStep(ref)

    def test_removing_the_element_that_holds_it_is_still_accepted(self, tmp_path: Path) -> None:
        # Why the refusal is per addressed element: pass D refuses every outcome
        # still holding the duplicate, so removing its element is the one change
        # set such a model can accept.
        project, layer, element = self._plant(tmp_path, "carve-out", *self._CASES[0][1:3])

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Remove the component whose ports collide.",
                "operations": [{"operation": "remove", "address": {"layer": layer, "element": element}}],
            },
            project=project,
        )

        assert response["status"] == "staged"

    def test_a_duplicate_elsewhere_is_not_answered_as_an_addressing_problem(self, tmp_path: Path) -> None:
        # Refused by pass D's `name.duplicate-port`, not by this condition: pass D
        # owns "the model is wrong", this check "the address cannot say which
        # member you mean".
        project, _, _ = self._plant(tmp_path, "elsewhere", *self._CASES[0][1:3])

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Retime the task.",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                        "value": 6,
                    }
                ],
            },
            project=project,
        )

        assert "duplicate port name" in str(response)
        for detail in [response, *response.get("additional", [])]:
            assert "cannot say which one an operation means" not in detail["message"]

    def test_a_whole_value_collection_is_exempt(self, tmp_path: Path) -> None:
        # Its members are counted: a duplicate is legal there.
        project = fixtures.build_fixture(tmp_path / "counted", "read-swc-element")
        (project / "model" / "swc" / "example" / "CountedSWC.yaml").write_text(
            "name: CountedSWC\ntype: application\nports: []\nrunnables:\n"
            "  - name: Run\n    events:\n      - {type: timing, period: 0.01}\n"
            "      - {type: timing, period: 0.01}\n",
            encoding="utf-8",
        )

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Retype the component.",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "CountedSWC", "path": [{"field": "type"}]},
                        "value": "service",
                    }
                ],
            },
            project=project,
        )

        assert "cannot say which one an operation means" not in str(response)


# The budget's load rules through a payload.


def _runnable_address(field: str | None = None) -> Address:
    path: tuple[Any, ...] = (FieldStep("runnables"), MemberStep("ExampleRun"))
    return Address(layer="swc", element="ExampleSWC", path=(*path, FieldStep(field)) if field else path)


class TestTheBudgetRulesThroughAPayload:
    """The format, positivity and pairing of a `wcet` are ``schema-violation``; the retired key is ``unknown-field``.

    The retired spelling never reaches the schema's own refusal here:
    ``_bind_payload`` refuses a field the registry does not declare before any
    validation, so the protocol's answer is that code. The file-side twins of
    these cases are `test_runnable_costs.py` and `test_schema.py`.
    """

    @pytest.mark.parametrize("value", ["1.5ms", "0us", 1200, "1200"])
    def test_a_task_budget_s_format_and_positivity_are_schema_violations(self, base: Any, value: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),))
        assert _codes(apply_operations(base, [UpdateOperation(address=address, value=value)])) == [SCHEMA_VIOLATION]

    def test_a_task_budget_is_staged(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),))
        assert _codes(apply_operations(base, [UpdateOperation(address=address, value="2ms")])) == []

    def test_the_task_s_retired_key_is_unknown_field(self, base: Any) -> None:
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet_us"),))
        assert _codes(apply_operations(base, [UpdateOperation(address=address, value=1200)])) == [UNKNOWN_FIELD]

    @pytest.mark.parametrize("value", ["1.5ms", "0us", 400])
    def test_a_runnable_budget_s_format_and_positivity_are_schema_violations(self, base: Any, value: Any) -> None:
        runnable = {**read_value(base, _runnable_address()), "wcet": value, "wcet_type": "gross"}
        outcome = apply_operations(base, [UpdateOperation(address=_runnable_address(), value=runnable)])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_runnable_budget_without_its_type_is_a_schema_violation(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=_runnable_address("wcet"), value="400us")])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_runnable_budget_with_its_type_is_staged_by_one_update_of_the_runnable(self, base: Any) -> None:
        """Each operation is validated on the element it leaves, so the pair is set in one operation.

        Two field updates leave the runnable half-paired after the first, which
        the pairing rule refuses.
        """
        runnable = {**read_value(base, _runnable_address()), "wcet": "400us", "wcet_type": "gross"}
        outcome = apply_operations(base, [UpdateOperation(address=_runnable_address(), value=runnable)])
        assert _codes(outcome) == []
        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
        assert (staged["runnables"][0]["wcet"], staged["runnables"][0]["wcet_type"]) == ("400us", "gross")

    def test_a_runnable_budget_type_without_its_budget_is_a_schema_violation(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=_runnable_address("wcet_type"), value="gross")])
        assert _codes(outcome) == [SCHEMA_VIOLATION]

    def test_a_runnable_budget_is_cleared_by_one_update_of_the_runnable(self, base: Any) -> None:
        """Removing a budget is the same shape as setting one: both keys go in one update of the runnable."""
        unbudgeted = dict(read_value(base, _runnable_address()))
        budgeted = {**unbudgeted, "wcet": "400us", "wcet_type": "gross"}
        outcome = apply_operations(
            base,
            [
                UpdateOperation(address=_runnable_address(), value=budgeted),
                UpdateOperation(address=_runnable_address(), value=unbudgeted),
            ],
        )
        assert _codes(outcome) == []
        staged = next(item for item in outcome.staged.elements["swc"] if item["name"] == "ExampleSWC")
        assert (staged["runnables"][0].get("wcet"), staged["runnables"][0].get("wcet_type")) == (None, None)

    def test_the_runnable_s_retired_key_is_unknown_field(self, base: Any) -> None:
        outcome = apply_operations(base, [UpdateOperation(address=_runnable_address("wcet_us"), value=400)])
        assert _codes(outcome) == [UNKNOWN_FIELD]


# The immutable-field refusal says what the update did.

_READ = {"port": "rIn", "element": "value"}
_WRITE = {"port": "pOut", "element": "value"}


def _with_data_accesses(tmp_path: Path) -> Path:
    """``examples/basic`` with one ``reads`` entry, one ``writes`` entry and a second data element.

    The plain example has neither entry; the second element of ``IExample``
    (``status``) is the different identity a ``writes`` entry can move to.
    """
    project = fixtures.build_fixture(tmp_path / "accesses", "read-swc-element")
    swc_dir = project / "model" / "swc" / "example"
    for name, entry in (
        ("ExampleConsumerSWC", "    reads:\n      - port: rIn\n"),
        ("ExampleSWC", "    writes:\n      - port: pOut\n"),
    ):
        path = swc_dir / f"{name}.yaml"
        text = path.read_text(encoding="utf-8")
        anchor = "        period: 0.01\n"
        assert text.count(anchor) == 1
        path.write_text(
            text.replace(anchor, anchor + entry + "        element: value\n"), encoding="utf-8", newline="\n"
        )
    interface = project / "model" / "interfaces" / "IExample.yaml"
    interface.write_text(
        interface.read_text(encoding="utf-8") + "  - name: status\n    implementation_type: float32\n",
        encoding="utf-8",
        newline="\n",
    )
    return project


def _read_address(*tail: Any) -> Address:
    return Address(
        layer="swc",
        element="ExampleConsumerSWC",
        path=(FieldStep("runnables"), MemberStep("ExampleConsumerRun"), FieldStep("reads"), MemberStep(_READ), *tail),
    )


def _write_address(identity: Any = _WRITE, *tail: Any) -> Address:
    return Address(
        layer="swc",
        element="ExampleSWC",
        path=(FieldStep("runnables"), MemberStep("ExampleRun"), FieldStep("writes"), MemberStep(identity), *tail),
    )


def _wire(address: Address) -> dict[str, Any]:
    return {"layer": address.layer, "element": address.element, "path": [step.to_json() for step in address.path]}


def _advised_path(message: str) -> list[Any]:
    """The field address the refusal names, parsed back exactly as written."""
    start = message.index("its own address, ") + len("its own address, ")
    return json.loads(message[start : message.index("; to replace the member")])


class TestTheImmutableFieldRefusalSaysWhatTheUpdateDid:
    """An update at a member's address replaces the member; the refusal says so and names the field's own address.

    It does not advise a remove and a create at the same identity, which the
    server refuses as ``identity-conflict``.
    """

    @pytest.fixture
    def accesses(self, tmp_path: Path) -> Any:
        return load_snapshot(str(_with_data_accesses(tmp_path) / "model"))

    def test_a_member_update_without_its_identity_names_the_field_address(self, accesses: Any) -> None:
        outcome = apply_operations(accesses, [UpdateOperation(address=_read_address(), value={"by": "implicit"})])

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        violation = outcome.violations[0]
        assert violation.addresses == (_read_address(),)
        assert "replaces the whole member" in violation.message
        assert "does not carry the member's identity (port, element)" in violation.message
        assert "a different one" not in violation.message
        assert _advised_path(violation.message) == [step.to_json() for step in _read_address(FieldStep("by")).path]
        assert "to replace the member, send it whole, its identity included" in violation.message

    def test_a_field_identified_member_update_without_its_name_says_the_same(self, accesses: Any) -> None:
        address = Address(layer="swc", element="ExampleConsumerSWC", path=(FieldStep("ports"), MemberStep("rIn")))
        outcome = apply_operations(accesses, [UpdateOperation(address=address, value={"description": "changed"})])

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        assert "does not carry the member's identity (name)" in outcome.violations[0].message
        assert _advised_path(outcome.violations[0].message) == [
            {"field": "ports"},
            {"member": "rIn"},
            {"field": "description"},
        ]

    def test_a_payload_of_several_fields_names_the_field_as_a_placeholder(self, accesses: Any) -> None:
        outcome = apply_operations(
            accesses, [UpdateOperation(address=_read_address(), value={"by": "implicit", "port": "rIn"})]
        )

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        assert "does not carry the member's identity (element)" in outcome.violations[0].message
        assert '{"member": {"port": "rIn", "element": "value"}}, {"field": <name>}]' in outcome.violations[0].message

    def test_an_optional_identity_field_left_out_is_not_missing(self, accesses: Any) -> None:
        # `instance` is an optional part of a task entry's identity, so leaving it
        # out is the same member.
        identity = {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}
        address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("runnables"), MemberStep(identity)))
        outcome = apply_operations(accesses, [UpdateOperation(address=address, value=dict(identity))])

        assert _codes(outcome) == []

    def test_a_complete_different_identity_keeps_the_remove_and_create_sentence(self, accesses: Any) -> None:
        outcome = apply_operations(
            accesses, [UpdateOperation(address=_read_address(), value={"port": "rIn", "element": "status"})]
        )

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        message = outcome.violations[0].message
        assert outcome.violations[0].addresses == (_read_address(),)
        assert "the replacement carries a different one" in message
        assert "a remove and a create at the new identity, in one change set, at two different identities" in message

    def test_a_field_update_of_a_member_identity_field_says_it_is_part_of_the_identity(self, accesses: Any) -> None:
        address = _write_address(_WRITE, FieldStep("element"))
        outcome = apply_operations(accesses, [UpdateOperation(address=address, value="status")])

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        assert outcome.violations[0].addresses == (address,)
        assert outcome.violations[0].message == (
            "'element' is part of the member's identity: a changed value is that member removed and one created "
            "at the new identity, in one change set"
        )

    def test_a_field_update_of_another_immutable_field_keeps_today_s_sentence(self, accesses: Any) -> None:
        address = Address(layer="hardware", element="RAM0", path=(FieldStep("kind"),))
        outcome = apply_operations(accesses, [UpdateOperation(address=address, value="processor")])

        assert _codes(outcome) == [IMMUTABLE_FIELD]
        assert outcome.violations[0].message == (
            "'kind' is declared immutable, and an approved scope can never make an immutable field writable"
        )


class TestTheImmutableFieldAdviceIsExecuted:
    """Each sentence's advice, sent back through the engine as an agent would, is staged."""

    @staticmethod
    def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return handle_request(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Follow the refusal's advice.",
                "operations": operations,
            },
            project=project,
        )

    def test_the_named_field_address_is_staged(self, tmp_path: Path) -> None:
        # The field address the refusal names is sent next, as written.
        project = _with_data_accesses(tmp_path)
        refused = self._change_set(
            project, [{"operation": "update", "address": _wire(_read_address()), "value": {"by": "implicit"}}]
        )
        assert (refused["response"], refused["error"]) == ("error", IMMUTABLE_FIELD)

        advised = {"layer": "swc", "element": "ExampleConsumerSWC", "path": _advised_path(refused["message"])}
        staged = self._change_set(project, [{"operation": "update", "address": advised, "value": "implicit"}])

        assert staged["status"] == "staged"

    def test_a_remove_and_a_create_at_a_different_identity_is_staged(self, tmp_path: Path) -> None:
        # At the same identity the pair is `identity-conflict`
        # (`identity-conflict-same-identity-group-move.json`).
        project = _with_data_accesses(tmp_path)
        refused = self._change_set(
            project,
            [
                {
                    "operation": "update",
                    "address": _wire(_write_address(_WRITE, FieldStep("element"))),
                    "value": "status",
                }
            ],
        )
        assert (refused["error"], "part of the member's identity" in refused["message"]) == (IMMUTABLE_FIELD, True)

        moved = {"port": "pOut", "element": "status"}
        staged = self._change_set(
            project,
            [
                {"operation": "remove", "address": _wire(_write_address())},
                {"operation": "create", "address": _wire(_write_address(moved)), "value": moved},
            ],
        )

        assert staged["status"] == "staged"
