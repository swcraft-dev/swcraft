"""Deriving a change set from a planned model state (`editing/derive.py`).

The derivation turns the model as it is into the model a planner (the MCP `import_component` tool)
wants, as a draft `change_set` request. Two properties are asserted:

* A protocol `update` addresses a field, member or map entry, never a whole element: an absent
  element is one element-level `create`, an existing one is compared leaf by leaf through the
  differ and delta projection a staged change set is reviewed with.
* The derivation is replayed in memory before it is offered and must reproduce every planned
  element's canonical payload; anything else is raised as an internal planning fault.

The module is generic (planned elements, not an import plan) because nothing outside `editing/`
may import it (`test_editing_package_boundaries.py`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from swcraft_tools.editing.address import canonical_value
from swcraft_tools.editing.derive import DerivationFault, derive_change_set
from swcraft_tools.editing.protocol import ChangeSetRequest
from swcraft_tools.schema import (
    BaseDataType,
    DataElement,
    Port,
    ReferenceDataType,
    Runnable,
    SRInterface,
    SWComponent,
    TimingEvent,
    ValueDataType,
)
from swcraft_tools.snapshot import ModelSnapshot

BASELINE_TOKEN = "rev1:AAAAAAAAAAAAAAAAAAAAAA"


def _snapshot(**layers: Any) -> ModelSnapshot:
    """A snapshot with only the layers a test cares about populated."""
    empty: dict[str, Any] = {
        "compositions": [],
        "swcs": [],
        "interfaces": [],
        "implementation_types": [],
        "connectors": [],
        "requirements": [],
        "tasks": [],
        "sequences": [],
        "hardware": [],
        "mapping_sets": [],
        "base_types": [],
        "application_types": [],
        "units": [],
        "compu_methods": [],
        "data_constraints": [],
    }
    return ModelSnapshot(**(empty | layers))


def _float32() -> BaseDataType:
    return BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754")


def _ispeed() -> SRInterface:
    return SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="float32")],
    )


def _sensor(runnables: list[Runnable] | None = None) -> SWComponent:
    return SWComponent(
        name="SpeedSensor",
        type="application",
        ports=[Port(name="pSpeed", direction="provide", interface="ISpeed")],
        runnables=runnables if runnables is not None else [],
    )


def _populated() -> ModelSnapshot:
    return _snapshot(
        base_types=[_float32()],
        interfaces=[_ispeed()],
        swcs=[_sensor()],
        swc_placement={"SpeedSensor": {"group": "sensors"}},
    )


def _operations(request: dict[str, Any]) -> list[dict[str, Any]]:
    return request["operations"]


class TestAnAbsentElementBecomesOneElementLevelCreate:
    def test_a_new_element_of_a_flat_layer_carries_its_whole_published_value(self) -> None:
        request = derive_change_set(
            _snapshot(),
            [("base_types", _float32(), {})],
            baseline_token=BASELINE_TOKEN,
        )

        assert _operations(request) == [
            {
                "operation": "create",
                "address": {"layer": "base_types", "element": "float32"},
                "value": {
                    "name": "float32",
                    "kind": "base_type",
                    "size": 32,
                    "encoding": "IEEE754",
                    "native_declaration": "",
                    "description": "",
                    # Contract 7.10.0: published at its `null` default.
                    "package": None,
                },
            }
        ]

    def test_a_new_swc_carries_the_registry_declared_placement_value(self) -> None:
        request = derive_change_set(
            _snapshot(interfaces=[_ispeed()], base_types=[_float32()]),
            [("swc", _sensor(), {"group": "components"})],
            baseline_token=BASELINE_TOKEN,
        )

        (operation,) = _operations(request)
        assert operation["operation"] == "create"
        assert operation["address"] == {"layer": "swc", "element": "SpeedSensor"}
        assert operation["value"]["group"] == "components"

    def test_several_absent_elements_become_one_create_each(self) -> None:
        request = derive_change_set(
            _snapshot(),
            [
                ("swc", _sensor(), {"group": "components"}),
                ("base_types", _float32(), {}),
                ("interfaces", _ispeed(), {}),
            ],
            baseline_token=BASELINE_TOKEN,
        )

        assert [(op["operation"], op["address"]["layer"]) for op in _operations(request)] == [
            ("create", "swc"),
            ("create", "interfaces"),
            ("create", "base_types"),
        ]


class TestAnExistingElementBecomesLeafOperations:
    def test_a_changed_element_never_produces_an_element_level_update(self) -> None:
        planned = _sensor(runnables=[Runnable(name="Run", events=[TimingEvent(type="timing", period=0.01)])])

        request = derive_change_set(
            _populated(),
            [("swc", planned, {"group": "sensors"})],
            baseline_token=BASELINE_TOKEN,
        )

        operations = _operations(request)
        assert operations, "a changed element must produce at least one operation"
        for operation in operations:
            assert operation["address"]["element"] == "SpeedSensor"
            if operation["operation"] == "update":
                assert operation["address"].get("path"), operation

    def test_an_added_collection_member_is_a_create_at_the_members_address(self) -> None:
        planned = _sensor(runnables=[Runnable(name="Run", events=[])])

        request = derive_change_set(
            _populated(),
            [("swc", planned, {"group": "sensors"})],
            baseline_token=BASELINE_TOKEN,
        )

        assert _operations(request) == [
            {
                "operation": "create",
                "address": {
                    "layer": "swc",
                    "element": "SpeedSensor",
                    "path": [{"field": "runnables"}, {"member": "Run"}],
                },
                "value": {
                    "name": "Run",
                    "min_start_interval": 0.0,
                    "concurrent": False,
                    "events": [],
                    "reads": [],
                    "writes": [],
                    "calls": [],
                    # Contract `12.1.0`: the two variable access lists, after `calls`.
                    "reads_variables": [],
                    "writes_variables": [],
                    # Contract `12.2.0`: the two exclusive area lists, after them.
                    "can_enter": [],
                    "runs_inside": [],
                    "description": "",
                    "symbol": None,
                    "wcet": None,
                    "wcet_type": None,
                    # Contract `11.0.0`: the trace link, declared last on the kind.
                    "satisfies": [],
                },
            }
        ]

    def test_a_removed_collection_member_is_a_remove_and_carries_no_value(self) -> None:
        baseline = _snapshot(
            base_types=[_float32()],
            interfaces=[_ispeed()],
            swcs=[_sensor(runnables=[Runnable(name="Run", events=[])])],
            swc_placement={"SpeedSensor": {"group": "sensors"}},
        )

        request = derive_change_set(
            baseline,
            [("swc", _sensor(), {"group": "sensors"})],
            baseline_token=BASELINE_TOKEN,
        )

        assert _operations(request) == [
            {
                "operation": "remove",
                "address": {
                    "layer": "swc",
                    "element": "SpeedSensor",
                    "path": [{"field": "runnables"}, {"member": "Run"}],
                },
            }
        ]

    def test_a_changed_scalar_field_is_one_update_at_that_fields_address(self) -> None:
        planned = SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            description="the vehicle speed",
            data_elements=[DataElement(name="speed", implementation_type="float32")],
        )

        request = derive_change_set(
            _populated(),
            [("interfaces", planned, {})],
            baseline_token=BASELINE_TOKEN,
        )

        assert _operations(request) == [
            {
                "operation": "update",
                "address": {
                    "layer": "interfaces",
                    "element": "ISpeed",
                    "path": [{"field": "description"}],
                },
                "value": "the vehicle speed",
            }
        ]


class TestTheReplayIsCheckedBeforeTheChangeSetIsOffered:
    def test_replaying_the_derived_operations_reproduces_every_planned_payload(self) -> None:
        from swcraft_tools.editing.apply import apply_operations
        from swcraft_tools.editing.protocol import parse_change_operation
        from swcraft_tools.editing.read import publish_element
        from swcraft_tools.editing.registry import REGISTRY

        baseline = _populated()
        planned_swc = _sensor(runnables=[Runnable(name="Run", min_start_interval=0.02, events=[])])
        planned = [
            ("swc", planned_swc, {"group": "sensors"}),
            ("base_types", BaseDataType(name="uint8", kind="base_type", size=8, encoding="NONE"), {}),
        ]

        request = derive_change_set(baseline, planned, baseline_token=BASELINE_TOKEN)

        outcome = apply_operations(
            baseline,
            [parse_change_operation(operation, position) for position, operation in enumerate(_operations(request))],
        )
        assert outcome.violations == ()
        assert outcome.inapplicable == ()
        for layer_name, element, placement in planned:
            layer = REGISTRY.layer(layer_name)
            produced = outcome.staged.get(layer, getattr(element, layer.identity_field))
            expected = publish_element(element, REGISTRY, placement=placement or None)
            assert json.dumps(canonical_value(produced)) == json.dumps(canonical_value(expected))

    def test_a_derived_operation_the_protocol_would_refuse_is_a_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Branch 1 of `_require_replay`: pass A over the derived operations. No planned
        # state can make the projection emit a malformed operation, so the composer is replaced.
        from swcraft_tools.editing import derive as derive_module

        real_operation = derive_module._operation

        def _drop_the_value(entry: Any) -> dict[str, Any]:
            operation = real_operation(entry)
            operation.pop("value", None)
            return operation

        monkeypatch.setattr(derive_module, "_operation", _drop_the_value)

        with pytest.raises(DerivationFault) as fault:
            derive_change_set(_snapshot(), [("base_types", _float32(), {})], baseline_token=BASELINE_TOKEN)

        assert "not a protocol change operation" in str(fault.value)

    def test_a_replay_the_engine_refuses_is_a_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Branch 2: pass B answered with a violation; the stub adds one real `Violation`
        # to the real outcome.
        from swcraft_tools.editing import derive as derive_module
        from swcraft_tools.editing.address import Address
        from swcraft_tools.editing.protocol import Violation

        real_apply = derive_module.apply_operations

        def _refused(*args: Any, **kwargs: Any) -> Any:
            outcome = real_apply(*args, **kwargs)
            violation = Violation(
                error="immutable-field",
                message="kind is immutable",
                addresses=(Address(layer="base_types", element="float32"),),
            )
            return replace(outcome, violations=(violation,))

        monkeypatch.setattr(derive_module, "apply_operations", _refused)

        with pytest.raises(DerivationFault) as fault:
            derive_change_set(_snapshot(), [("base_types", _float32(), {})], baseline_token=BASELINE_TOKEN)

        assert "was refused (immutable-field)" in str(fault.value)

    def test_a_replay_that_does_not_apply_is_a_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Branch 3: pass B could not bind an operation's address.
        from swcraft_tools.editing import derive as derive_module
        from swcraft_tools.editing.address import Address
        from swcraft_tools.editing.apply import InapplicableOperation
        from swcraft_tools.editing.protocol import parse_change_operation

        real_apply = derive_module.apply_operations
        missing = Address(layer="base_types", element="float32")

        def _unapplied(snapshot: Any, operations: Any, **kwargs: Any) -> Any:
            outcome = real_apply(snapshot, operations, **kwargs)
            miss = InapplicableOperation(
                position=0,
                operation=parse_change_operation(
                    {"operation": "remove", "address": missing.to_json()},
                    0,
                ),
                reason="missing-target",
                message="base_types/float32 is not there",
                missing=missing,
            )
            return replace(outcome, inapplicable=(miss,))

        monkeypatch.setattr(derive_module, "apply_operations", _unapplied)

        with pytest.raises(DerivationFault) as fault:
            derive_change_set(_snapshot(), [("base_types", _float32(), {})], baseline_token=BASELINE_TOKEN)

        assert "could not be applied (missing-target)" in str(fault.value)

    def test_a_replay_that_lands_something_else_than_the_plan_reported_is_a_fault(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Branch 4, the one the guard is for: the replay applied cleanly but lands a payload
        # other than the planned one, i.e. a draft whose report and operations disagree.
        from swcraft_tools.editing import derive as derive_module

        real_publish = derive_module.publish_element

        def _publish_something_else(element: Any, registry: Any, **kwargs: Any) -> dict[str, Any]:
            return real_publish(element, registry, **kwargs) | {"size": 64}

        monkeypatch.setattr(derive_module, "publish_element", _publish_something_else)

        with pytest.raises(DerivationFault) as fault:
            derive_change_set(_snapshot(), [("base_types", _float32(), {})], baseline_token=BASELINE_TOKEN)

        message = str(fault.value)
        assert "did not reproduce the planned 'float32'" in message
        assert "would land something other than what the plan reported" in message

    def test_an_immutable_field_change_is_a_loud_fault_not_a_change_set(self) -> None:
        # `kind` is the data-type layer's discriminator, so it is immutable and pass B refuses
        # the update. A value type named like the base type `float32` is another layer's
        # element, so the kind change is stated on a reference type planned as a value type.
        baseline = _snapshot(
            base_types=[_float32()],
            implementation_types=[
                ValueDataType(name="Speed", kind="value_type", base="float32"),
                ReferenceDataType(name="SpeedRef", kind="reference_type", base="Speed"),
            ],
        )
        planned = ValueDataType(name="SpeedRef", kind="value_type", base="float32")

        with pytest.raises(DerivationFault) as fault:
            derive_change_set(
                baseline,
                [("implementation_types", planned, {})],
                baseline_token=BASELINE_TOKEN,
            )

        # The replay's refusal names the field and the code, not the element.
        assert "was refused (immutable-field)" in str(fault.value)
        assert "'kind'" in str(fault.value)


class TestTheEnvelope:
    def test_an_unchanged_plan_derives_no_change_set_at_all(self) -> None:
        # Never an empty `operations` array: `$defs/changeSetRequest` declares `minItems: 1`.
        assert (
            derive_change_set(
                _populated(),
                [("swc", _sensor(), {"group": "sensors"})],
                baseline_token=BASELINE_TOKEN,
            )
            is None
        )

    def test_nothing_planned_derives_no_change_set_at_all(self) -> None:
        assert derive_change_set(_populated(), [], baseline_token=BASELINE_TOKEN) is None

    def test_the_baseline_token_is_copied_unchanged(self) -> None:
        request = derive_change_set(
            _snapshot(),
            [("base_types", _float32(), {})],
            baseline_token="rev1:not-this-projects-token",
        )

        assert request["baseline"] == "rev1:not-this-projects-token"

    def test_the_intent_is_carried_when_given_and_omitted_when_not(self) -> None:
        with_intent = derive_change_set(
            _snapshot(),
            [("base_types", _float32(), {})],
            baseline_token=BASELINE_TOKEN,
            intent="Import SpeedSensor",
        )
        without = derive_change_set(
            _snapshot(),
            [("base_types", _float32(), {})],
            baseline_token=BASELINE_TOKEN,
        )

        assert with_intent["intent"] == "Import SpeedSensor"
        assert "intent" not in without

    def test_the_derived_request_is_a_complete_protocol_change_set_request(self) -> None:
        request = derive_change_set(
            _snapshot(),
            [
                ("swc", _sensor(), {"group": "components"}),
                ("interfaces", _ispeed(), {}),
                ("base_types", _float32(), {}),
            ],
            baseline_token=BASELINE_TOKEN,
            intent="Import SpeedSensor",
        )

        parsed = ChangeSetRequest.from_json(request)
        assert parsed.baseline == BASELINE_TOKEN
        assert parsed.intent == "Import SpeedSensor"
        assert len(parsed.operations) == 3

    def test_an_undeclared_layer_is_a_fault_rather_than_a_silent_omission(self) -> None:
        with pytest.raises(DerivationFault, match="swcs"):
            derive_change_set(
                _snapshot(),
                [("swcs", _sensor(), {})],
                baseline_token=BASELINE_TOKEN,
            )
