"""The differ's per-item detail against the authorization gate.

``ChangedItem.changed_fields`` is the projection the authorization gate decides
scope coverage on, so the per-item ``collection_changes`` are added **beside**
it, never instead of it: for a change to a collection field the gate keeps
seeing the top-level field name (``ports``, ``operations``). The gate is
``editing.authorization.gate_delta`` over ``editing.delta.project_delta``'s
real projection of the diff.

Every diff is a real one (a model written, snapshotted, mutated, snapshotted
again): a hand-made ``ModelDiff`` would let this file agree with itself while
disagreeing with the differ. Each accepting scope is paired with one that
refuses the same delta, or a gate that stopped reading the delta would pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from swcraft_tools import writer
from swcraft_tools.differ import ChangedItem, LayerDiff, ModelDiff, ModelStateDiff, diff_snapshots
from swcraft_tools.editing.address import Address, FieldStep, ScopeEntry
from swcraft_tools.editing.authorization import UNAUTHORIZED_DELTA, gate_delta
from swcraft_tools.editing.delta import project_delta
from swcraft_tools.editing.protocol import DeltaEntry, Violation
from swcraft_tools.session import _touched_identities
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot

# Fixture model and the real-diff harness.


def _build_model(model_root: Path) -> None:
    """A small model carrying both collection shapes pinned here."""
    root = str(model_root)
    writer.create_data_type(root, "uint32", "base_type", size=32, encoding="NONE", native_declaration="uint32_t")
    writer.create_interface(root, "ISpeed", [{"name": "speed", "implementation_type": "uint32"}])
    writer.create_cs_interface(
        root,
        "IDiag",
        [{"name": "ReadDtc", "arguments": [{"name": "aDtc", "direction": "out", "implementation_type": "uint32"}]}],
    )

    writer.create_swc(root, "EngineController", "powertrain")
    writer.add_port(root, "EngineController", "pSpeed", "provide", "ISpeed")
    writer.add_runnable(root, "EngineController", "RunEngine")

    writer.create_swc(root, "Dashboard", "body")
    writer.add_port(root, "Dashboard", "rSpeed", "require", "ISpeed")

    writer.create_connector(root, "CSpeed", "EngineController", "pSpeed", "Dashboard", "rSpeed")
    writer.create_requirement(root, "REQ-1", "Speed", "The speed shall be published")


@pytest.fixture
def model_root(tmp_path) -> Path:
    root = tmp_path / "model"
    root.mkdir()
    _build_model(root)
    return root


class Delta:
    """Snapshot a model; after a mutation, give the real diff and its real delta projection."""

    def __init__(self, model_root: Path) -> None:
        self._model_root = str(model_root)
        self._before = load_snapshot(self._model_root)

    def _staged(self) -> ModelSnapshot:
        return load_snapshot(self._model_root)

    def diff(self) -> ModelDiff:
        return diff_snapshots(self._before, self._staged())

    def entries(self) -> list[DeltaEntry]:
        staged = self._staged()
        return project_delta(diff_snapshots(self._before, staged), baseline=self._before, staged=staged)


@pytest.fixture
def delta(model_root) -> Delta:
    return Delta(model_root)


def _scope(layer: str, element: str, field: str) -> tuple[ScopeEntry, ...]:
    """One scope entry: ``create`` (how a gained member is projected) at one element's one field."""
    return (ScopeEntry(operation="create", address=Address(layer=layer, element=element, path=(FieldStep(field),))),)


def _refusal(violation: Violation | None) -> Violation:
    assert violation is not None, "the gate admitted a delta it was asked to refuse"
    assert violation.error == UNAUTHORIZED_DELTA
    return violation


def _changed_field_names(changed) -> list[str]:
    """The projection the authorization gate is built on, off a ``ChangedItem``."""
    return [field_name for field_name, _, _ in changed.changed_fields]


def _the_one_changed(layer_diff):
    assert layer_diff.added == [] and layer_diff.removed == []
    assert len(layer_diff.changed) == 1
    return layer_diff.changed[0]


# A gained port: the per-item detail is additive.


def test_a_gained_port_reports_ports_as_the_one_changed_field_and_the_port_beside_it(model_root, delta):
    """The changed-field tuple still names ``ports``, and the added port sits beside it."""
    writer.add_port(str(model_root), "EngineController", "pTorqueLimit", "provide", "ISpeed")

    changed = _the_one_changed(delta.diff().swcs)
    assert changed.key == "EngineController"
    assert _changed_field_names(changed) == ["ports"]

    assert len(changed.collection_changes) == 1
    collection_change = changed.collection_changes[0]
    assert collection_change.field == "ports"
    assert collection_change.added == [
        {
            "name": "pTorqueLimit",
            "direction": "provide",
            "interface": "ISpeed",
            "observed_interface": None,
            "description": "",
            "measures": {},
            "com_specs": [],
            # The port's trace link, the last field.
            "satisfies": [],
        }
    ]


def test_the_gained_ports_delta_address_is_the_field_name_plus_the_member(model_root, delta):
    """The delta address the gate sees starts with the differ's field name, then the member."""
    writer.add_port(str(model_root), "EngineController", "pTorqueLimit", "provide", "ISpeed")

    entries = delta.entries()
    assert [entry.operation for entry in entries] == ["create"]
    assert entries[0].address.to_json() == {
        "layer": "swc",
        "element": "EngineController",
        "path": [{"field": "ports"}, {"member": "pTorqueLimit"}],
    }


def test_gate_delta_accepts_the_gained_port_for_a_scope_that_allows_ports(model_root, delta):
    writer.add_port(str(model_root), "EngineController", "pTorqueLimit", "provide", "ISpeed")

    assert gate_delta(delta.entries(), _scope("swc", "EngineController", "ports")) is None


def test_gate_delta_refuses_the_same_gained_port_when_the_scope_does_not_allow_ports(model_root, delta):
    """The non-vacuity half: a scope naming the same element but not ``ports`` refuses it."""
    writer.add_port(str(model_root), "EngineController", "pTorqueLimit", "provide", "ISpeed")

    violation = _refusal(gate_delta(delta.entries(), _scope("swc", "EngineController", "description")))

    assert [address.to_json()["path"][0] for address in violation.addresses] == [{"field": "ports"}]
    assert "ports" in violation.message


# An argument added to an operation: only the top-level field name is pinned.


def _replace_cs_interface(model_root: Path, name: str, operations: list[dict]) -> None:
    """Rewrite one client-server interface (no writer adds an argument to an existing operation)."""
    (model_root / "interfaces" / f"{name}.yaml").unlink()
    writer.create_cs_interface(str(model_root), name, operations)


_TWO_ARGUMENT_OPERATIONS = [
    {
        "name": "ReadDtc",
        "arguments": [
            {"name": "aDtc", "direction": "out", "implementation_type": "uint32"},
            {"name": "aStatus", "direction": "out", "implementation_type": "uint32"},
        ],
    }
]


def test_an_added_argument_still_reports_the_interfaces_operations_field(model_root, delta):
    """A nested change reports the interface's top-level ``operations`` field."""
    _replace_cs_interface(model_root, "IDiag", _TWO_ARGUMENT_OPERATIONS)

    changed = _the_one_changed(delta.diff().interfaces)
    assert changed.key == "IDiag"
    assert _changed_field_names(changed) == ["operations"]


def test_the_added_arguments_delta_address_still_begins_with_operations(model_root, delta):
    """The nested delta address still begins with ``operations``, the prefix coverage reads."""
    _replace_cs_interface(model_root, "IDiag", _TWO_ARGUMENT_OPERATIONS)

    entries = delta.entries()
    assert [entry.operation for entry in entries] == ["create"]
    assert entries[0].address.to_json() == {
        "layer": "interfaces",
        "element": "IDiag",
        "path": [
            {"field": "operations"},
            {"member": "ReadDtc"},
            {"field": "arguments"},
            {"member": "aStatus"},
        ],
    }


def test_gate_delta_accepts_the_added_argument_for_a_scope_that_allows_operations(model_root, delta):
    _replace_cs_interface(model_root, "IDiag", _TWO_ARGUMENT_OPERATIONS)

    assert gate_delta(delta.entries(), _scope("interfaces", "IDiag", "operations")) is None


def test_gate_delta_refuses_the_added_argument_when_the_scope_does_not_allow_operations(model_root, delta):
    _replace_cs_interface(model_root, "IDiag", _TWO_ARGUMENT_OPERATIONS)

    violation = _refusal(gate_delta(delta.entries(), _scope("interfaces", "IDiag", "description")))

    assert [address.to_json()["path"][0] for address in violation.addresses] == [{"field": "operations"}]
    assert "operations" in violation.message


# The session's other reader of a ModelDiff.


def test_touched_identities_reports_the_same_elements_as_before_this_phase(model_root, delta):
    """The session's view of a diff is identities only, untouched by the per-item detail."""
    root = str(model_root)
    writer.add_port(root, "EngineController", "pTorqueLimit", "provide", "ISpeed")
    writer.create_swc(root, "BodyControl", "body")
    (model_root / "connectors" / "CSpeed.yaml").unlink()
    _replace_cs_interface(model_root, "IDiag", _TWO_ARGUMENT_OPERATIONS)

    assert _touched_identities(delta.diff()) == {
        "compositions": set(),
        "swcs": {"EngineController", "BodyControl"},
        "interfaces": {"IDiag"},
        "implementation_types": set(),
        "connectors": {"CSpeed"},
        "requirements": set(),
        "tasks": set(),
        "sequences": set(),
        "hardware": set(),
        "mapping_sets": set(),
        "base_types": set(),
        "application_types": set(),
        "units": set(),
        "compu_methods": set(),
        "data_constraints": set(),
    }


def test_touched_identities_refuses_a_changed_element_keyed_by_no_identity():
    """A layer's changed element is keyed by its identity string; a tuple key is refused, not filed."""
    diff = ModelDiff.from_state_diff(ModelStateDiff(layers={"swc": LayerDiff(changed=[ChangedItem(key=("A", "B"))])}))
    with pytest.raises(TypeError, match="layer 'swc': a changed element is keyed"):
        _touched_identities(diff)
