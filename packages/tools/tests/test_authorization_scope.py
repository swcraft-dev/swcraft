"""Tests for authorization.py: the layer field vocabulary, and the removal of the tool-keyed gate.

* :func:`~swcraft_tools.authorization._layer_fields` (with ``_layer_names`` and ``_layer_identity_fields``)
  uses each layer model's top-level fields under their serialization aliases, exactly what
  ``differ.diff_layer()`` reports; the pinning suite asserts that equivalence both ways.
* The two guards that keep the vocabulary though no scope consumes it: the verbatim freeze and the pin
  against the editing registry.
* The removal as a gate: no removed name is still exported or imported in ``packages/tools/``,
  ``packages/mcp/`` or ``packages/agent/``.

The address-keyed replacement is tested in ``packages/tools/tests/test_editing_authorization.py``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from pydantic import BaseModel
from swcraft_tools import authorization
from swcraft_tools.authorization import _layer_fields, _layer_identity_fields, _layer_names
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Composition,
    Connector,
    Core,
    CSInterface,
    DataConstr,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Linear,
    MappingSet,
    Memory,
    Partition,
    PointerDataType,
    Processor,
    RecordDataType,
    ReferenceDataType,
    Requirement,
    Sequence,
    SRInterface,
    SWComponent,
    Task,
    TextTable,
    Unit,
    ValueDataType,
)
from swcraft_tools.snapshot import ModelSnapshot

#: The three projections over the **production** contract.
LAYER_FIELDS = _layer_fields(production_registry())
LAYER_IDENTITY_FIELDS = _layer_identity_fields(production_registry())
LAYER_NAMES = _layer_names(production_registry())

#: The recorded pre-kernel vocabulary, read rather than transcribed; `test_authorization_generic.py` reads the
#: same file, so there is one recorded oracle.
FROZEN_VOCABULARY = Path(__file__).parent / "fixtures" / "recorded_oracles" / "authorization_vocabulary.json"

# Layer fixtures: one base payload per model class plus a single-field
# alternative per non-identity field, keyed by the field's SERIALIZATION ALIAS
# (that is the name `diff_layer()` reports, e.g. `from`, never `from_`).

#: Every element kind's base payload plus a single-field alternative per non-identity field, as
#: ``(class, base, {alias: value})``. The layer is resolved from the class through
#: ``ModelRegistry.kind_of_model()``, so no layer inventory is restated; the positional lookups (``[0]``)
#: depend on this order.
_ELEMENT_SPECS: tuple[tuple[type[BaseModel], dict, dict], ...] = (
    (
        Composition,
        {
            "name": "Elem",
            "title": "t",
            "description": "d",
            "ports": [{"name": "p", "direction": "provide", "interface": "I"}],
            "members": [{"name": "m", "type": "A"}],
            "delegations": [{"name": "d", "port": "p", "member": "m", "member_port": "q"}],
        },
        {
            "title": "t2",
            "description": "d2",
            "root": True,
            "ports": [],
            "members": [{"name": "m2", "type": "A"}],
            "delegations": [{"name": "d2", "port": "p", "member": "m", "member_port": "q"}],
            # The sub-package the element is emitted in.
            "package": "Sub",
        },
    ),
    (
        SWComponent,
        {
            "name": "Elem",
            "type": "application",
            "description": "d",
            "ports": [{"name": "p", "direction": "provide", "interface": "I"}],
            "runnables": [{"name": "r"}],
            "memory": {"pool": 1},
        },
        {
            "type": "service",
            "description": "d2",
            "ports": [],
            "runnables": [{"name": "r2"}],
            "memory": {"pool": 2},
            "external": True,
            "external_ref": "/Pkg/Comp",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "asil": "D",
            "handle_termination_and_restart": "no_support",
            "data_type_mappings": ["Physical"],
            "behavior_name": "Comp_Behaviour",
            "package": "Sub",
            "variables": [{"name": "v", "access": "implicit", "implementation_type": "t"}],
            "exclusive_areas": [{"name": "a"}],
        },
    ),
    (
        SRInterface,
        {
            "name": "Elem",
            "kind": "sender_receiver",
            "data_elements": [{"name": "de", "implementation_type": "t"}],
            "description": "d",
        },
        {
            "data_elements": [{"name": "de2", "implementation_type": "t"}],
            "description": "d2",
            "package": "Sub",
            "is_service": True,
        },
    ),
    (
        CSInterface,
        {"name": "Elem", "kind": "client_server", "operations": [{"name": "op"}], "description": "d"},
        {"operations": [{"name": "op2"}], "description": "d2", "package": "Sub", "is_service": True},
    ),
    (
        BaseDataType,
        {
            "name": "Elem",
            "kind": "base_type",
            "size": 32,
            "encoding": "NONE",
            "native_declaration": "uint32_t",
            "description": "d",
        },
        {"size": 16, "encoding": "IEEE754", "native_declaration": "uint16_t", "description": "d2", "package": "Sub"},
    ),
    (
        ValueDataType,
        {
            "name": "Elem",
            "kind": "value_type",
            "base": "Base",
            "unit": "m",
            "value_min": 0.0,
            "value_max": 10.0,
            "description": "d",
        },
        {
            "base": "Other",
            "unit": "km",
            "value_min": 1.0,
            "value_max": 11.0,
            "compu_method_ref": "T",
            "description": "d2",
        },
    ),
    (
        ReferenceDataType,
        {"name": "Elem", "kind": "reference_type", "base": "Value", "description": "d"},
        {"base": "Other", "compu_method_ref": "T", "description": "d2"},
    ),
    (
        EnumDataType,
        {"name": "Elem", "kind": "enum", "literals": [{"name": "A", "value": 0}], "description": "d"},
        {"literals": [{"name": "B", "value": 1}], "description": "d2"},
    ),
    (
        RecordDataType,
        {"name": "Elem", "kind": "record", "fields": [{"name": "f", "type": "t"}], "description": "d"},
        {"fields": [{"name": "f2", "type": "t"}], "description": "d2"},
    ),
    (
        ArrayDataType,
        {"name": "Elem", "kind": "array", "element_type": "t", "length": 4, "description": "d"},
        {"element_type": "t2", "length": 8, "description": "d2"},
    ),
    (
        Connector,
        {
            "name": "Elem",
            "from": {"swc": "A", "port": "p"},
            "to": {"swc": "B", "port": "q"},
            "description": "d",
        },
        {
            "from": {"swc": "C", "port": "p"},
            "to": {"swc": "D", "port": "q"},
            "description": "d2",
            "composition": "Top",
        },
    ),
    (
        Requirement,
        {
            "id": "Elem",
            "title": "t",
            "description": "d",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
        {
            "title": "t2",
            "description": "d2",
            "req_type": "spec",
            "version": 2,
            "source": "external",
            "derived_from": ["Parent@1"],
            "asil": "D",
            # The requirement's provenance.
            "document": "SRS-Brakes",
            "baseline": "BL-7",
            "revision": "1.3",
            "stub": True,
        },
    ),
    (
        Task,
        {
            "name": "Elem",
            "period": 0.01,
            "priority": 1,
            "partition": "PARTITION0",
            "wcet": "100us",
            "description": "d",
            "runnables": [{"swc": "A", "runnable": "r"}],
            "coexistence": "argued",
        },
        {
            "period": 0.02,
            "priority": 2,
            "partition": "PARTITION1",
            "wcet": "200us",
            "description": "d2",
            "runnables": [],
            "coexistence": "argued differently",
        },
    ),
    (
        Sequence,
        {"name": "Elem", "task": "T", "title": "t", "steps": [{"swc": "A", "runnable": "r"}]},
        {"task": "T2", "title": "t2", "steps": [{"swc": "B", "runnable": "r"}]},
    ),
    (
        ApplicationDataType,
        {
            "name": "Elem",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 0.1, "offset": 0.0},
            "range": {"min": 0.0, "max": 400.0},
            "description": "d",
        },
        {
            "unit": "deg",
            "conversion": {"factor": 1.0, "offset": 0.0},
            "range": {"min": -180.0, "max": 180.0},
            "description": "d2",
            # The application kinds are a layer of their own, so `package` is reached there through its own kinds.
            "package": "Sub",
        },
    ),
    (
        MappingSet,
        {
            "name": "Elem",
            "maps": [{"application": "Kph", "implementation": "Speed"}],
            "description": "d",
        },
        {"maps": [{"application": "Deg", "implementation": "Angle"}], "description": "d2", "package": "Sub"},
    ),
    # The three layers of the forms a type names by reference.
    (
        Unit,
        {"name": "Elem", "display_name": "km/h", "description": "d"},
        {"display_name": "kph", "description": "d2", "package": "Sub"},
    ),
    (
        TextTable,
        {"name": "Elem", "kind": "texttable", "entries": [{"name": "A", "value": 0}], "description": "d"},
        {"entries": [{"name": "B", "value": 1}], "description": "d2", "package": "Sub"},
    ),
    (
        Linear,
        {"name": "Elem", "kind": "linear", "unit": "U", "factor": 2.0, "offset": 1.0, "description": "d"},
        {"unit": "U2", "factor": 3.0, "offset": 0.5, "description": "d2", "package": "Sub"},
    ),
    (
        DataConstr,
        {"name": "Elem", "min": 0.0, "max": 10.0, "level": 0, "description": "d"},
        {"min": 1.0, "max": 11.0, "level": 1, "description": "d2", "package": "Sub"},
    ),
    (
        ExternalInterface,
        {
            "name": "Elem",
            "kind": "external_interface",
            "external_ref": "/Pkg/Elem",
            "external_dest": "CLIENT-SERVER-INTERFACE",
            "description": "d",
        },
        {"external_ref": "/Other/Elem", "external_dest": "SENDER-RECEIVER-INTERFACE", "description": "d2"},
    ),
    (
        ExternalDataType,
        {
            "name": "Elem",
            "kind": "external_type",
            "external_ref": "/Pkg/Elem",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
            "description": "d",
        },
        {"external_ref": "/Other/Elem", "external_dest": "APPLICATION-PRIMITIVE-DATA-TYPE", "description": "d2"},
    ),
    # The implementation record, array and pointer.
    (
        ImplementationRecordDataType,
        {
            "name": "Elem",
            "kind": "implementation_record",
            "elements": [{"name": "left", "type": "u16"}],
            "description": "d",
        },
        {"elements": [{"name": "right", "type": "u16"}], "description": "d2"},
    ),
    (
        ImplementationArrayDataType,
        {
            "name": "Elem",
            "kind": "implementation_array",
            "element_type": "u16",
            "length": 4,
            "element_name": "Element",
            "description": "d",
        },
        {"element_type": "u32", "length": 8, "element_name": "Item", "description": "d2"},
    ),
    (
        PointerDataType,
        {"name": "Elem", "kind": "pointer", "target": "Frame", "description": "d"},
        {"target": "Other", "description": "d2", "package": "Sub"},
    ),
    (
        Processor,
        {"name": "Elem", "kind": "processor", "description": "d"},
        {"description": "d2"},
    ),
    (
        Core,
        {
            "name": "Elem",
            "kind": "core",
            "threshold": 0.7,
            "enforcement": "strict",
            "hw_id": 0,
            "description": "d",
        },
        {"threshold": 0.8, "enforcement": "advisory", "asil_capability": "B", "hw_id": 1, "description": "d2"},
    ),
    (
        Memory,
        {
            "name": "Elem",
            "kind": "memory",
            "type": "RAM",
            "size": 1024,
            "accessible": ["CORE0"],
            "threshold": 0.7,
            "enforcement": "strict",
            "description": "d",
        },
        {"type": "Flash", "size": 2048, "accessible": ["CORE1"], "asil_capability": "D"},
    ),
    # The hardware layer's fourth kind.
    (
        Partition,
        {"name": "Elem", "kind": "partition", "core": "CORE0", "description": "d"},
        {"core": "CORE1", "asil": "B", "trust": "trusted", "coexistence": "argued", "description": "d2"},
    ),
)

#: Every linkable kind's ``satisfies`` alternative, added once rather than in each row.
_ELEMENT_SPECS = tuple(
    (cls, base, {**alts, "satisfies": ["Req@1"]} if "satisfies" in cls.model_fields else alts)
    for cls, base, alts in _ELEMENT_SPECS
)

#: The base payload of each element kind, addressed by its class, so the cross-kind rows need no layer or index.
_BASE_PAYLOADS: dict[type[BaseModel], dict] = {cls: base for cls, base, _alts in _ELEMENT_SPECS}

#: Two-field alternatives for a field a single-field one cannot reach: restating a property in the other form
#: needs the inline form cleared in the same edit, or the form rules refuse two forms at once.
_TWO_FIELD_ALTERNATIVES: dict[type[BaseModel], tuple[dict, ...]] = {
    ApplicationDataType: (
        {"unit": "", "unit_ref": "U"},
        {"conversion": None, "compu_method_ref": "C"},
        {"range": None, "data_constr_ref": "D"},
    ),
    EnumDataType: ({"literals": None, "compu_method_ref": "T"},),
}


def _adapter_layer_of(cls: type[BaseModel]) -> str:
    """The ``LAYER_NAMES`` key *cls* belongs to: its layer's ``snapshotAttributes`` spelling, else the layer name."""
    registry = production_registry()
    facts = registry.kind_of_model(cls)
    assert facts is not None, f"{cls.__name__} is bound to no element kind the contract declares"
    layer = registry.layer(facts.layer)
    return layer.snapshot_attribute or layer.name


def _specs_by_layer() -> dict[str, list[tuple[type[BaseModel], dict, dict]]]:
    """:data:`_ELEMENT_SPECS`, grouped onto the vocabulary's layer keys."""
    grouped: dict[str, list[tuple[type[BaseModel], dict, dict]]] = {layer: [] for layer in LAYER_NAMES}
    for cls, base, alts in _ELEMENT_SPECS:
        grouped[_adapter_layer_of(cls)].append((cls, base, alts))
    return grouped


#: layer -> [(model class, base payload, {alias: alternative value}), ...]
_LAYER_SPECS: dict[str, list[tuple[type[BaseModel], dict, dict]]] = _specs_by_layer()

# `kind` is a discriminator: it changes only by swapping union members, which `diff_layer()` reports as a
# multi-field change over the union of both dumps.
#: (old class, new class, expected aliases); the layer and both payloads are derived.
_CROSS_KIND_CASES: tuple[tuple[type[BaseModel], type[BaseModel], set[str]], ...] = (
    (SRInterface, CSInterface, {"kind", "data_elements", "operations"}),
    # The base type and the application kinds are layers of their own, so the `data_types` swap is stated
    # over two implementation kinds.
    (ValueDataType, ReferenceDataType, {"kind", "base", "unit", "value_min", "value_max"}),
    # An application type also dumps `unit_ref` and `data_constr_ref`; both dump a `None` `compu_method_ref`.
    (
        ApplicationDataType,
        EnumDataType,
        {"kind", "unit", "conversion", "range", "literals", "unit_ref", "data_constr_ref"},
    ),
    # The compu methods' two categories; a linear dumps its `limits`, `None` here.
    (TextTable, Linear, {"kind", "entries", "unit", "factor", "offset", "limits"}),
    # `hardware` has four kinds; the swap uses the two with a budget, whose dumps differ most. Shared
    # `threshold`, `enforcement` and `description` are not changed, so they are not reported.
    (Core, Memory, {"kind", "hw_id", "type", "size", "accessible"}),
)


def _cross_kind_rows() -> list[tuple[str, type[BaseModel], dict, type[BaseModel], dict, set[str]]]:
    """The parametrization rows, in the shape the two readers below expect."""
    rows = []
    for old_cls, new_cls, expected in _CROSS_KIND_CASES:
        layer = _adapter_layer_of(old_cls)
        assert _adapter_layer_of(new_cls) == layer, f"{old_cls.__name__} and {new_cls.__name__} are not one layer"
        rows.append((layer, old_cls, _BASE_PAYLOADS[old_cls], new_cls, _BASE_PAYLOADS[new_cls], expected))
    return rows


CROSS_KIND_ROWS = _cross_kind_rows()


def _snapshot(layer: str, items: list[BaseModel]) -> ModelSnapshot:
    layers = {name: [] for name in LAYER_NAMES}
    layers[layer] = items
    return ModelSnapshot(**layers)


def _layer_diff(layer: str, old: list[BaseModel], new: list[BaseModel]):
    return getattr(diff_snapshots(_snapshot(layer, old), _snapshot(layer, new)), layer)


def _changed_aliases(layer: str, old: BaseModel, new: BaseModel) -> set[str]:
    layer_diff = _layer_diff(layer, [old], [new])
    assert len(layer_diff.changed) == 1, f"expected exactly one changed element in '{layer}'"
    return {field_name for field_name, _, _ in layer_diff.changed[0].changed_fields}


def _field_cases() -> list[tuple[str, type[BaseModel], dict, str, object]]:
    return [
        (layer, cls, base, alias, value)
        for layer, specs in _LAYER_SPECS.items()
        for cls, base, alts in specs
        for alias, value in alts.items()
    ]


# The generated vocabulary


def test_vocabulary_covers_exactly_the_nine_snapshot_layers():
    assert set(LAYER_FIELDS) == set(LAYER_NAMES)
    # `ModelSnapshot` also carries `swc_placement`, placement metadata that is not a layer.
    assert set(LAYER_NAMES) == {
        f.name for f in ModelSnapshot.__dataclass_fields__.values() if f.name != "swc_placement"
    }
    # ...and the set is the frozen one: a swap of one layer for another, invisible to a count, fails here.
    assert set(LAYER_NAMES) - REPLACED_ATTRIBUTES == set(_frozen()["LAYER_NAMES"]) - REPLACED_ATTRIBUTES
    # ...and the rows the scope drops are exactly these eleven: two replacements, each in the retired layer's
    # place (`resources` -> `hardware`, `containers` -> `compositions`); `systems`, removed, so later frozen
    # rows sit one index later; and six layers added with none retired, each appended last.
    #
    # Written as memberships and a length rather than one set literal: a literal naming two live layer
    # directories is a layer table to `test_layer_vocabulary_gate.py`'s gate B, which scans this file.
    assert len(REPLACED_ATTRIBUTES) == 11
    assert "resources" in REPLACED_ATTRIBUTES and "hardware" in REPLACED_ATTRIBUTES
    assert "containers" in REPLACED_ATTRIBUTES and "compositions" in REPLACED_ATTRIBUTES
    assert "systems" in REPLACED_ATTRIBUTES and "systems" not in LAYER_NAMES
    assert "mapping_sets" in REPLACED_ATTRIBUTES and "mapping_sets" not in _frozen()["LAYER_NAMES"]
    assert "base_types" in REPLACED_ATTRIBUTES and "base_types" not in _frozen()["LAYER_NAMES"]
    assert "application_types" in REPLACED_ATTRIBUTES and "application_types" not in _frozen()["LAYER_NAMES"]
    for added in ("units", "compu_methods", "data_constraints"):
        assert added in REPLACED_ATTRIBUTES and added not in _frozen()["LAYER_NAMES"]
    # The renamed `implementation_types` sits among the appended rows (the binding's key order), so `_frozen`
    # reads the recorded row last.
    assert LAYER_NAMES[-7:] == (
        "mapping_sets",
        "application_types",
        "implementation_types",
        "base_types",
        "units",
        "compu_methods",
        "data_constraints",
    )
    frozen_order = [name for name in _frozen()["LAYER_NAMES"] if name != "systems"]
    live_order = [
        name
        for name in LAYER_NAMES
        if name not in {"mapping_sets", "base_types", "application_types", "units", "compu_methods", "data_constraints"}
    ]
    assert live_order[-1] == frozen_order[-1] == "implementation_types"
    assert live_order.index("hardware") == frozen_order.index("resources") == 7
    assert live_order.index("compositions") == frozen_order.index("containers") == 0


def test_the_element_specs_cover_every_declared_element_kind():
    """Every top-level kind the contract declares has exactly one spec, and every vocabulary layer at least one.

    The positional ``_LAYER_SPECS[layer][0]`` lookups depend on it.
    """
    registry = production_registry()
    declared = {kind.model for facts in registry.layers for kind in facts.top_level_kinds}
    covered = [cls for cls, _base, _alts in _ELEMENT_SPECS]

    assert set(covered) == declared
    assert len(covered) == len(set(covered)), "one element kind carries two specs"
    assert set(_LAYER_SPECS) == set(LAYER_NAMES)
    assert all(_LAYER_SPECS[layer] for layer in LAYER_NAMES), "a vocabulary layer has no spec to mutate"


def test_the_cross_kind_cases_are_exactly_the_multi_kind_layers():
    """The cross-kind cases are exactly the layers that declare two or more kinds."""
    registry = production_registry()
    multi_kind = {facts.snapshot_attribute or facts.name for facts in registry.layers if len(facts.top_level_kinds) > 1}

    assert {layer for layer, *_rest in CROSS_KIND_ROWS} == multi_kind
    assert len(CROSS_KIND_ROWS) == len(multi_kind), "two cross-kind rows for one layer"


def test_swcs_vocabulary_lists_the_swcomponent_top_level_fields():
    """A component's top-level fields, ``external`` and ``asil`` included, are ordinary scope names."""
    assert LAYER_FIELDS["swcs"] == frozenset(
        {
            "type",
            "description",
            "ports",
            "runnables",
            "memory",
            "external",
            "external_ref",
            "external_dest",
            "asil",
            "handle_termination_and_restart",
            "data_type_mappings",
            "behavior_name",
            "package",
            "satisfies",
            "variables",
            "exclusive_areas",
        }
    )


def test_swc_events_is_not_a_vocabulary_field():
    # Runnable events surface through `runnables`; an `events` entry would promise a granularity the differ
    # never reports.
    assert "events" not in LAYER_FIELDS["swcs"]


def test_connectors_vocabulary_uses_the_serialization_alias():
    assert "from" in LAYER_FIELDS["connectors"]
    assert "from_" not in LAYER_FIELDS["connectors"]
    assert "to" in LAYER_FIELDS["connectors"]


def test_union_layers_cover_every_member_class():
    assert {"kind", "data_elements", "operations"} <= LAYER_FIELDS["interfaces"]
    assert {"kind", "base", "elements", "element_type", "length", "target"} <= LAYER_FIELDS["implementation_types"]
    # The base type's layer declares one kind, so it is no union.
    assert {"size", "encoding", "native_declaration"} <= LAYER_FIELDS["base_types"]
    assert not {"size", "encoding", "native_declaration"} & LAYER_FIELDS["implementation_types"]
    # The application kinds are a union of their own.
    assert {"kind", "literals", "fields", "element_type", "length", "conversion", "range"} <= LAYER_FIELDS[
        "application_types"
    ]
    assert not {"literals", "fields", "conversion", "range"} & LAYER_FIELDS["implementation_types"]
    assert {"kind", "type", "size", "accessible", "hw_id", "threshold", "enforcement"} <= LAYER_FIELDS["hardware"]


def test_identity_field_is_absent_from_every_layer_vocabulary():
    assert LAYER_IDENTITY_FIELDS["requirements"] == "id"
    for layer in LAYER_NAMES:
        identity = LAYER_IDENTITY_FIELDS[layer]
        assert identity not in LAYER_FIELDS[layer], f"'{identity}' must not be in the '{layer}' vocabulary"


# The vocabulary pinned against diff_layer()'s actual output


@pytest.mark.parametrize(
    ("layer", "cls", "base", "alias", "value"),
    _field_cases(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_each_mutated_field_is_reported_under_a_vocabulary_name(layer, cls, base, alias, value):
    reported = _changed_aliases(layer, cls.model_validate(base), cls.model_validate({**base, alias: value}))
    assert reported == {alias}
    assert reported <= LAYER_FIELDS[layer]


@pytest.mark.parametrize(("layer", "old_cls", "old", "new_cls", "new", "expected"), CROSS_KIND_ROWS)
def test_changing_the_discriminator_is_reported_under_vocabulary_names(layer, old_cls, old, new_cls, new, expected):
    reported = _changed_aliases(layer, old_cls.model_validate(old), new_cls.model_validate(new))
    assert reported == expected
    assert reported <= LAYER_FIELDS[layer]


#: The vocabulary entries no mutation can reach: ``base_types`` declares one kind, so its ``kind`` field has
#: one value and no change of kind to report it under.
_UNREACHABLE_LITERALS: dict[str, set[str]] = {"base_types": {"kind"}}


@pytest.mark.parametrize("layer", LAYER_NAMES)
def test_vocabulary_is_exactly_what_the_differ_can_report(layer):
    """...and vice versa: no vocabulary entry is unreachable by a real mutation, but a one-value literal."""
    reachable: set[str] = set()
    for cls, base, alts in _LAYER_SPECS[layer]:
        for alias, value in alts.items():
            reachable |= _changed_aliases(layer, cls.model_validate(base), cls.model_validate({**base, alias: value}))
        for pair in _TWO_FIELD_ALTERNATIVES.get(cls, ()):
            reachable |= _changed_aliases(layer, cls.model_validate(base), cls.model_validate({**base, **pair}))
    for case_layer, old_cls, old, new_cls, new, _expected in CROSS_KIND_ROWS:
        if case_layer == layer:
            reachable |= _changed_aliases(layer, old_cls.model_validate(old), new_cls.model_validate(new))
    assert reachable == set(LAYER_FIELDS[layer]) - _UNREACHABLE_LITERALS.get(layer, set())
    assert _UNREACHABLE_LITERALS.get(layer, set()) <= set(LAYER_FIELDS[layer])


@pytest.mark.parametrize("layer", LAYER_NAMES)
def test_mutating_the_identity_field_yields_added_plus_removed_and_no_changed_fields(layer):
    cls, base, _alts = _LAYER_SPECS[layer][0]
    identity = LAYER_IDENTITY_FIELDS[layer]
    layer_diff = _layer_diff(
        layer,
        [cls.model_validate(base)],
        [cls.model_validate({**base, identity: "Renamed"})],
    )
    assert len(layer_diff.added) == 1
    assert len(layer_diff.removed) == 1
    assert layer_diff.changed == []


# The table is frozen, and it is still pinned


#: The layer vocabulary, frozen. It is a recorded literal, ``fixtures/recorded_oracles/authorization_vocabulary.json``
#: (never regenerated), not a re-derivation, which would pass any edit to the derivation it guards.
#:
#: The row recorded as ``data_types`` is the renamed ``implementation_types`` layer's, graded field for field
#: and read last, since the binding appends the attribute after every recorded row.
RENAMED_8_0: dict[str, str] = {"data_types": "implementation_types"}


def _frozen() -> dict[str, Any]:
    recorded = json.loads(FROZEN_VOCABULARY.read_text(encoding="utf-8"))
    renamed = set(RENAMED_8_0.values())

    def last(names: list[str]) -> list[str]:
        live = [RENAMED_8_0.get(name, name) for name in names]
        return [name for name in live if name not in renamed] + [name for name in live if name in renamed]

    result: dict[str, Any] = {"LAYER_NAMES": last(recorded["LAYER_NAMES"])}
    for table in ("_LAYER_MODELS", "LAYER_IDENTITY_FIELDS", "LAYER_FIELDS"):
        rows = {RENAMED_8_0.get(key, key): value for key, value in recorded[table].items()}
        result[table] = {key: rows[key] for key in last(list(rows))}
    result["DELETE_ELEMENT_LAYERS"] = recorded["DELETE_ELEMENT_LAYERS"]
    return result


def _frozen_layer_fields() -> dict[str, frozenset[str]]:
    return {layer: frozenset(fields) for layer, fields in _frozen()["LAYER_FIELDS"].items()}


#: The snapshot attributes the frozen oracle cannot grade (replaced, removed and added layers), derived from
#: the two vocabularies' symmetric difference.
REPLACED_ATTRIBUTES: frozenset[str] = frozenset(_frozen()["LAYER_NAMES"]) ^ frozenset(LAYER_NAMES)

#: Fields added to layers the frozen oracle carries, which a layer-level exclusion cannot reach; subtracted
#: from the derived side only (:func:`test_the_added_fields_are_exactly_the_rows_the_frozen_table_lacks`
#: derives the whole table).
ADDED_FIELDS_4_0: dict[str, frozenset[str]] = {
    "connectors": frozenset({"composition"}),
    "swcs": frozenset({"external", "external_ref", "external_dest"}),
}

#: The safety integrity level, on a component and on a requirement.
ADDED_FIELDS_4_4: dict[str, frozenset[str]] = {
    "swcs": frozenset({"asil"}),
    "requirements": frozenset({"asil"}),
}

#: ``coexistence``, the one field of its set top-level on a layer element.
ADDED_FIELDS_4_5: dict[str, frozenset[str]] = {
    "tasks": frozenset({"coexistence"}),
}

#: A component's mapping-set list; the application type's nested records belong to `application_types`,
#: which the frozen oracle does not carry.
ADDED_FIELDS_7_0: dict[str, frozenset[str]] = {
    "swcs": frozenset({"data_type_mappings"}),
}

#: The external stub's reference pair on both stub kinds' layers, keyed through each kind's layer rather
#: than a two-layer literal, which `test_layer_vocabulary_gate.py`'s gate B would read as a layer table.
ADDED_FIELDS_7_5: dict[str, frozenset[str]] = {
    production_registry().kind(kind).layer: frozenset({"external_ref", "external_dest"})
    for kind in ("external_interface", "external_type")
}

#: The implementation record's `elements`, array's `element_name` and pointer's `target`, keyed through the
#: kinds' layer like `ADDED_FIELDS_7_5`.
ADDED_FIELDS_7_8: dict[str, frozenset[str]] = {
    production_registry().kind("implementation_record").layer: frozenset({"elements", "element_name", "target"})
}

#: A component's `handle_termination_and_restart`.
ADDED_FIELDS_7_9: dict[str, frozenset[str]] = {
    "swcs": frozenset({"handle_termination_and_restart"}),
}

#: `package`, on the kinds emitted directly into a root package, keyed through a kind of each layer like
#: `ADDED_FIELDS_7_5`; the composition's and mapping set's rows are stated whole below.
ADDED_FIELDS_7_10: dict[str, frozenset[str]] = {
    # The base type, which declares `package` too, is in `base_types`, a layer the oracle does not carry.
    _adapter_layer_of(cls): frozenset({"package"})
    for cls in (SWComponent, SRInterface, ValueDataType)
}

#: An interface's `is_service`, keyed through a kind of the layer like `ADDED_FIELDS_7_5`.
ADDED_FIELDS_7_11: dict[str, frozenset[str]] = {_adapter_layer_of(SRInterface): frozenset({"is_service"})}

#: A value type's and a reference type's `compu_method_ref`; the application kinds' reference fields belong to
#: `application_types`.
ADDED_FIELDS_8_0_ELEMENTS: dict[str, frozenset[str]] = {
    _adapter_layer_of(ValueDataType): frozenset({"compu_method_ref"})
}

#: A component's `behavior_name`; the events' and access entries' `name` are member fields.
ADDED_FIELDS_8_0_NAMES: dict[str, frozenset[str]] = {"swcs": frozenset({"behavior_name"})}

#: The trace links: `satisfies` on every linkable layer the frozen vocabulary carries, and the requirement's
#: `version`, `source` and `derived_from`, keyed through each kind's layer.
ADDED_FIELDS_11_0: dict[str, frozenset[str]] = {"swcs": frozenset({"satisfies"})} | {
    production_registry().kind(kind).layer: fields
    for kind, fields in (
        ("sender_receiver", frozenset({"satisfies"})),
        ("task", frozenset({"satisfies"})),
        ("requirement", frozenset({"version", "source", "derived_from"})),
    )
}

#: The requirement's provenance (`document`, `baseline`, `revision`) and its `stub` flag.
ADDED_FIELDS_11_1: dict[str, frozenset[str]] = {
    production_registry().kind("requirement").layer: frozenset({"document", "baseline", "revision", "stub"})
}

#: A component's inter-runnable `variables`; the runnable's variable lists are member fields.
ADDED_FIELDS_12_1: dict[str, frozenset[str]] = {"swcs": frozenset({"variables"})}

#: A component's `exclusive_areas`; the runnable's area lists are member fields.
ADDED_FIELDS_12_2: dict[str, frozenset[str]] = {"swcs": frozenset({"exclusive_areas"})}

#: Every added field the frozen vocabulary cannot carry, per layer, derived so a new table cannot be forgotten.
ADDED_FIELDS: dict[str, frozenset[str]] = {
    layer: ADDED_FIELDS_4_0.get(layer, frozenset())
    | ADDED_FIELDS_4_4.get(layer, frozenset())
    | ADDED_FIELDS_4_5.get(layer, frozenset())
    | ADDED_FIELDS_7_0.get(layer, frozenset())
    | ADDED_FIELDS_7_5.get(layer, frozenset())
    | ADDED_FIELDS_7_8.get(layer, frozenset())
    | ADDED_FIELDS_7_9.get(layer, frozenset())
    | ADDED_FIELDS_7_10.get(layer, frozenset())
    | ADDED_FIELDS_7_11.get(layer, frozenset())
    | ADDED_FIELDS_8_0_ELEMENTS.get(layer, frozenset())
    | ADDED_FIELDS_8_0_NAMES.get(layer, frozenset())
    | ADDED_FIELDS_11_0.get(layer, frozenset())
    | ADDED_FIELDS_11_1.get(layer, frozenset())
    | ADDED_FIELDS_12_1.get(layer, frozenset())
    | ADDED_FIELDS_12_2.get(layer, frozenset())
    for layer in (
        ADDED_FIELDS_4_0.keys()
        | ADDED_FIELDS_4_4.keys()
        | ADDED_FIELDS_4_5.keys()
        | ADDED_FIELDS_7_0.keys()
        | ADDED_FIELDS_7_5.keys()
        | ADDED_FIELDS_7_8.keys()
        | ADDED_FIELDS_7_9.keys()
        | ADDED_FIELDS_7_10.keys()
        | ADDED_FIELDS_7_11.keys()
        | ADDED_FIELDS_8_0_ELEMENTS.keys()
        | ADDED_FIELDS_8_0_NAMES.keys()
        | ADDED_FIELDS_11_0.keys()
        | ADDED_FIELDS_11_1.keys()
        | ADDED_FIELDS_12_1.keys()
        | ADDED_FIELDS_12_2.keys()
    )
}


#: The fields that left the frozen `data_types` row with the kinds that moved to `base_types` and
#: `application_types`. Each is a scope name of its new layer
#: (:func:`test_the_moved_fields_are_exactly_the_ones_the_base_types_layer_took`), so they are subtracted
#: from the frozen side only.
MOVED_TO_8_0: dict[str, frozenset[str]] = {
    "base_types": frozenset({"size", "encoding", "native_declaration"}),
    "application_types": frozenset({"literals", "fields"}),
}
MOVED_FIELDS_8_0: dict[str, frozenset[str]] = {
    "implementation_types": MOVED_TO_8_0["base_types"] | MOVED_TO_8_0["application_types"],
}

#: A requirement's removed scope names, gone rather than moved; subtracted on the frozen side only.
REMOVED_FIELDS_11_0: dict[str, frozenset[str]] = {"requirements": frozenset({"links", "status"})}


def _graded(rows: dict[str, Any], *, frozen: bool = False) -> dict[str, Any]:
    graded = {key: value for key, value in rows.items() if key not in REPLACED_ATTRIBUTES}
    graded = {
        key: (value - ADDED_FIELDS[key] if key in ADDED_FIELDS and isinstance(value, frozenset) else value)
        for key, value in graded.items()
    }
    if frozen:
        graded = {
            key: (value - MOVED_FIELDS_8_0[key] if key in MOVED_FIELDS_8_0 and isinstance(value, frozenset) else value)
            for key, value in graded.items()
        }
        graded = {
            key: (
                value - REMOVED_FIELDS_11_0[key]
                if key in REMOVED_FIELDS_11_0 and isinstance(value, frozenset)
                else value
            )
            for key, value in graded.items()
        }
    return graded


def test_the_moved_fields_are_exactly_the_ones_the_base_types_layer_took():
    """The fields lost from frozen rows are exactly :data:`MOVED_FIELDS_8_0`, each a scope name of its new layer.

    A field really gone, rather than moved, still fails.
    """
    derived = dict(LAYER_FIELDS)
    frozen = _frozen_layer_fields()
    shared = set(derived) & set(frozen) - REPLACED_ATTRIBUTES
    lost = {layer: frozen[layer] - derived[layer] for layer in shared if frozen[layer] - derived[layer]}
    # The two removed fields are lost too, and are no scope name of any layer.
    assert lost == MOVED_FIELDS_8_0 | REMOVED_FIELDS_11_0
    assert not any(REMOVED_FIELDS_11_0["requirements"] & fields for fields in LAYER_FIELDS.values())
    for layer, fields in MOVED_TO_8_0.items():
        assert fields <= LAYER_FIELDS[layer]


def test_the_added_fields_are_exactly_the_rows_the_frozen_table_lacks():
    """The exclusion is exactly the fields the derived vocabulary carries and the frozen one lacks.

    It hides nothing the other way: a frozen field the derivation lost is a dropped scope name and fails.
    """
    derived = dict(LAYER_FIELDS)
    frozen = _frozen_layer_fields()
    shared = set(derived) & set(frozen) - REPLACED_ATTRIBUTES

    gained = {layer: derived[layer] - frozen[layer] for layer in shared if derived[layer] - frozen[layer]}
    assert gained == ADDED_FIELDS
    lost = {
        layer: frozen[layer]
        - derived[layer]
        - MOVED_FIELDS_8_0.get(layer, frozenset())
        - REMOVED_FIELDS_11_0.get(layer, frozenset())
        for layer in shared
        if frozen[layer]
        - derived[layer]
        - MOVED_FIELDS_8_0.get(layer, frozenset())
        - REMOVED_FIELDS_11_0.get(layer, frozenset())
    }
    assert lost == {}, f"a frozen scope name is gone from the derivation: {lost}"


def test_layer_fields_is_unchanged_by_the_removal_of_the_tool_keyed_gate():
    """The frozen baseline, entry by entry, compared as a plain dict so a missing and an extra layer differ."""
    assert _graded(dict(LAYER_FIELDS)) == _graded(_frozen_layer_fields(), frozen=True)
    assert len(_graded(dict(LAYER_FIELDS))) == 7
    # The two rows the scope drops, stated rather than skipped, each minus the identity field.
    assert LAYER_FIELDS["hardware"] == frozenset(
        {
            "kind",
            "type",
            "size",
            "accessible",
            "hw_id",
            "threshold",
            "enforcement",
            "asil_capability",
            "description",
            "core",
            "asil",
            "coexistence",
            "trust",
            "satisfies",
        }
    )
    assert LAYER_FIELDS["compositions"] == frozenset(
        {"title", "description", "root", "ports", "members", "delegations", "package", "satisfies"}
    )


def test_the_frozen_table_is_not_a_restatement_of_the_derivation():
    """The freeze fails on a content edit: one field removed from one layer breaks the comparison."""
    perturbed = _graded(dict(LAYER_FIELDS))
    perturbed["tasks"] = frozenset(perturbed["tasks"] - {"priority"})
    assert perturbed != _graded(_frozen_layer_fields(), frozen=True)

    gained = _graded(dict(LAYER_FIELDS))
    gained["swcs"] = frozenset(gained["swcs"] | {"group"})
    assert gained != _graded(_frozen_layer_fields(), frozen=True)


def _fields_from_the_editing_registry(registry: reg.Registry) -> dict[str, frozenset[str]]:
    """The same vocabulary composed from the contract's ``fieldDecl`` rows through the editing registry.

    Two differences are by construction and neither may be "fixed": the vocabulary drops the identity field,
    and it has no ``group``, which the registry publishes as a placement field (so ``schema_fields`` is used).
    """
    composed: dict[str, frozenset[str]] = {}
    for layer in registry.layers:
        derived: set[str] = set()
        for kind in layer.top_level_kinds:
            derived |= set(registry.schema_fields(kind.kind))
        composed[layer.snapshot_attribute] = frozenset(derived - {layer.identity_field})
    return composed


def test_layer_fields_is_still_pinned_against_the_registry():
    """The vocabulary equals the editing registry's projection, and the freeze's mutation breaks it too."""
    composed = _fields_from_the_editing_registry(reg.REGISTRY)
    assert dict(LAYER_FIELDS) == composed

    dropped = dict(LAYER_FIELDS)
    dropped["tasks"] = frozenset(dropped["tasks"] - {"priority"})
    assert dropped != composed

    gained = dict(LAYER_FIELDS)
    gained["swcs"] = frozenset(gained["swcs"] | {"group"})
    assert gained != composed
    assert "group" not in composed["swcs"], "the placement field is the registry's one declared addition"
    assert set(reg.REGISTRY.all_fields("swc")) - set(reg.REGISTRY.schema_fields("swc")) == {"group"}


def test_the_layer_vocabulary_mapping_is_immutable():
    vocabulary = _layer_fields(production_registry())
    with pytest.raises(TypeError):
        vocabulary["swcs"] = frozenset()  # type: ignore[index]
    with pytest.raises(AttributeError):
        vocabulary["swcs"].add("events")  # type: ignore[attr-defined]


# The removal itself

#: Every name removed from ``authorization.py`` with the tool-keyed gate, and
#: **not re-anchored**: each was keyed on an MCP tool name. The address-keyed
#: replacement is ``swcraft_tools.editing.authorization`` (gate 1 over the
#: operations a change-set request states, gate 2 over the resulting delta), a
#: different question asked of a different thing — re-pointing a table keyed by a
#: tool name at a ``change_set`` request would have been a claim nothing could
#: honour.
REMOVED_NAMES: frozenset[str] = frozenset(
    {
        "MODEL_WRITING_TOOLS",
        "SAFETY_ACT_TOOLS",
        "ScopeReference",
        "ScopeValidationError",
        "TOOL_TARGETS",
        "TaskScope",
        "TouchedTarget",
        "UNDERIVABLE",
        "UnderivableTargetsError",
        "check_call",
        "check_delta",
        "parse_task_scope",
        "touched_targets",
    }
)

#: What survives, and its reader; asserted as an equality over the public surface.
#:
#: * ``REFUSAL_MARKER`` / ``Refusal`` — ``swcraft_agent.core`` builds its refusals in them.
#: * ``classify_safety_act`` and its four types — the always-on guard, imported by ``editing/safety_guard.py``.
#: * ``classify_argument_act`` and ``classify_allocation_act`` — the directions outside ``hardware``; pure
#:   classifiers kept beside their siblings so no second copy of a safety decision exists.
#: * ``classify_trust_act`` — the partition's trust class, imported by ``editing/safety_guard.py``.
#: * ``DELETE_ELEMENT_LAYERS`` — read by the classifier alone.
SURVIVING_NAMES: frozenset[str] = frozenset(
    {
        "DELETE_ELEMENT_LAYERS",
        "REFUSAL_MARKER",
        "Refusal",
        "SafetyChange",
        "SafetyChangeKind",
        "SafetyClassificationError",
        "SafetyVerdict",
        "classify_allocation_act",
        "classify_argument_act",
        "classify_safety_act",
        "classify_trust_act",
    }
)

#: The three project source trees a removed name could still be imported from.
PROJECT_ROOTS: tuple[str, ...] = ("packages/tools", "packages/mcp", "packages/agent")

#: How a wholesale import of the module is reported by :func:`_authorization_imports`.
WHOLESALE = "swcraft_tools.authorization"


def test_every_removed_name_is_really_gone():
    for name in sorted(REMOVED_NAMES):
        assert not hasattr(authorization, name), f"'{name}' is still exported by authorization.py"


def _declared_public_names(path: Path) -> set[str]:
    """Every public name ``path`` declares at module level, read off the source, not ``vars(module)``."""
    declared: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            declared.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            declared.add(node.target.id)
        elif isinstance(node, ast.Assign):
            declared.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return {name for name in declared if not name.startswith("_")}


def test_the_surviving_surface_is_exactly_the_named_one():
    """The public surface is exactly the named survivors."""
    published = _declared_public_names(Path(authorization.__file__))
    assert published == SURVIVING_NAMES, (
        f"unexpected: {sorted(published - SURVIVING_NAMES)}, missing: {sorted(SURVIVING_NAMES - published)}"
    )


def _python_sources() -> list[Path]:
    return sorted(
        (
            path
            for root in PROJECT_ROOTS
            for path in (REPO_ROOT / root).rglob("*.py")
            if ".venv" not in path.parts and "build" not in path.parts
        ),
        key=lambda path: path.as_posix(),
    )


def _authorization_imports(source: str) -> set[str]:
    """Every name a module takes from ``swcraft_tools.authorization``; a wholesale import reports the module path."""
    names: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.authorization":
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools":
            names.update(WHOLESALE for alias in node.names if alias.name == "authorization")
        elif isinstance(node, ast.Import):
            names.update(WHOLESALE for alias in node.names if alias.name == WHOLESALE)
    return names


def test_nothing_in_the_three_projects_imports_a_removed_name():
    """No module of the three projects, tests included, imports a removed name."""
    offenders: dict[str, list[str]] = {}
    for path in _python_sources():
        imported = _authorization_imports(path.read_text(encoding="utf-8"))
        found = sorted(imported & REMOVED_NAMES)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, f"removed authorization names are still imported: {offenders}"


def test_no_module_reaches_the_whole_authorization_module_wholesale():
    """Only a closed list of tests imports the authorization module wholesale, to read a private name.

    No production module is on the list, and that is the load-bearing half.
    """
    wholesale = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in _python_sources()
        if WHOLESALE in _authorization_imports(path.read_text(encoding="utf-8"))
    }
    assert wholesale == {
        "packages/tools/tests/test_authorization_generic.py",
        "packages/tools/tests/test_authorization_scope.py",
        "packages/tools/tests/test_editing_authorization.py",
    }, sorted(wholesale)


def test_the_import_gate_is_not_vacuous(tmp_path):
    """A positive control: the scan sees each import form in a synthetic offender."""
    offender = tmp_path / "unmigrated.py"
    offender.write_text(
        "from swcraft_tools.authorization import TaskScope, check_call\n"
        "import swcraft_tools.authorization\n"
        "from swcraft_tools import authorization\n",
        encoding="utf-8",
    )

    found = _authorization_imports(offender.read_text(encoding="utf-8"))

    assert found & REMOVED_NAMES == {"TaskScope", "check_call"}
    assert WHOLESALE in found

    plain = tmp_path / "clean.py"
    plain.write_text("from swcraft_tools.authorization import classify_safety_act\n", encoding="utf-8")
    assert _authorization_imports(plain.read_text(encoding="utf-8")) == {"classify_safety_act"}


def test_the_scan_reaches_every_project():
    """The gate is worth nothing if a root globs to nothing."""
    by_root = {root: [p for p in _python_sources() if p.is_relative_to(REPO_ROOT / root)] for root in PROJECT_ROOTS}
    for root, paths in by_root.items():
        assert len(paths) > 10, f"only {len(paths)} python files found under {root}/"
