"""Tests for component_exchange.closure.

Forward dependency closure for one SWC: the SWC itself, the interfaces its
ports reference, and the transitive data-type closure over those interfaces.
Composition/connector/task/sequence/requirement content is architect-owned
and never read or included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path, write_model_element
from swcraft_tools.component_exchange import (
    AmbiguousComponentError,
    AmbiguousReferenceError,
    ComponentClosure,
    ComponentNotFoundError,
    MissingReferenceError,
    compute_closure,
)
from swcraft_tools.model_registry import production_registry

#: The layers the closure reads through ``read_data_description``: the three type layers its
#: ``data_types`` field carries and the three element layers its ``elements`` field carries.
_DATA_DESCRIPTION_LAYERS = frozenset(
    {"base_types", "application_types", "implementation_types", "units", "compu_methods", "data_constraints"}
)


def _component_owned_layers() -> set[str]:
    """The layers a component export carries, read off `ComponentClosure` itself.

    The dataclass is the one statement of what a component owns (no contract fact holds the split).
    Its ``data_types`` and ``elements`` fields name no layer: they stand for the six data description
    layers, a pairing stated here.
    """
    fields = set(ComponentClosure.__dataclass_fields__)
    assert {"data_types", "elements"} <= fields
    return (fields - {"data_types", "elements"}) | _DATA_DESCRIPTION_LAYERS


def _integration_owned_layers() -> tuple[str, ...]:
    """Every declared layer the closure does not own, in contract order."""
    owned = _component_owned_layers()
    return tuple(facts.storage.directory for facts in production_registry().layers if facts.name not in owned)


# Fixture model
#
# TargetSwc exercises both interface kinds and six data type kinds: base type, value type,
# application type, enum, record and array (the chain cases below cover the reference type). A record field and an
# array element resolve in `application_types` alone, so both name `Distance_T`, which the
# closure reaches as `float32` through TargetSwc's mapping set.
# OtherSwc/IOther/Other_T exist only to prove unrelated content is excluded.


def _dump(path: Path, data: dict[str, Any]) -> None:
    """Write *data* at *path*; a type spelled directly under ``data_types/`` goes to its kind's layer."""
    if path.parent.name == "data_types" and data.get("kind") is not None:
        path = model_element_path(path.parent.parent, path.stem, kind=data["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _build_model(tmp_path: Path) -> str:
    model_root = tmp_path / "model"
    dt = model_root / "data_types"
    _dump(
        model_element_path(model_root, "uint8_base", kind="base_type"),
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
    )
    _dump(dt / "uint8.yaml", {"name": "uint8", "kind": "value_type", "base": "uint8_base"})
    _dump(
        model_element_path(model_root, "float32_base", kind="base_type"),
        {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
    )
    _dump(dt / "float32.yaml", {"name": "float32", "kind": "value_type", "base": "float32_base"})
    _dump(dt / "Speed_T.yaml", {"name": "Speed_T", "kind": "value_type", "base": "float32_base"})
    _dump(
        dt / "Gear_T.yaml",
        {
            "name": "Gear_T",
            "kind": "enum",
            "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
        },
    )
    _dump(
        dt / "Pose_T.yaml",
        {
            "name": "Pose_T",
            "kind": "record",
            "fields": [{"name": "x", "type": "Distance_T"}, {"name": "gear", "type": "Gear_T"}],
        },
    )
    _dump(dt / "Samples_T.yaml", {"name": "Samples_T", "kind": "array", "element_type": "Distance_T", "length": 4})
    _dump(dt / "Distance_T.yaml", {"name": "Distance_T", "kind": "application_type"})
    write_model_element(
        model_root,
        {"name": "PhysicalTypes", "maps": [{"application": "Distance_T", "implementation": "float32"}]},
        layer="mapping_sets",
    )
    _dump(
        model_element_path(model_root, "Other_T_base", kind="base_type"),
        {"name": "Other_T_base", "kind": "base_type", "size": 16, "encoding": "NONE"},
    )
    _dump(dt / "Other_T.yaml", {"name": "Other_T", "kind": "value_type", "base": "Other_T_base"})

    ifaces = model_root / "interfaces"
    _dump(
        ifaces / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "speed", "implementation_type": "Speed_T"},
                {"name": "pose", "application_type": "Pose_T"},
            ],
        },
    )
    _dump(
        ifaces / "ICalib.yaml",
        {
            "name": "ICalib",
            "kind": "client_server",
            "operations": [
                {
                    "name": "SetOffset",
                    "arguments": [
                        {"name": "samples", "direction": "in", "application_type": "Samples_T"},
                        {"name": "raw", "direction": "out", "implementation_type": "uint8"},
                    ],
                }
            ],
        },
    )
    _dump(
        ifaces / "IOther.yaml",
        {
            "name": "IOther",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "Other_T"}],
        },
    )

    swc = model_root / "swc"
    _dump(
        swc / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [
                {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                {"name": "rCalib", "direction": "require", "interface": "ICalib"},
            ],
            "runnables": [{"name": "TargetRunnable", "events": [{"type": "timing", "period": 0.01}]}],
            "data_type_mappings": ["PhysicalTypes"],
        },
    )
    _dump(
        swc / "body" / "OtherSwc.yaml",
        {
            "name": "OtherSwc",
            "ports": [{"name": "pOther", "direction": "provide", "interface": "IOther"}],
        },
    )
    return str(model_root)


EXPECTED_CLOSURE_DATA_TYPES = {
    "uint8",
    "uint8_base",
    "float32",
    "float32_base",
    "Speed_T",
    "Gear_T",
    "Pose_T",
    "Samples_T",
    "Distance_T",
}


# Happy path: closure content


class TestComputeClosureContent:
    def test_returns_component_closure_with_target_swc(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        assert isinstance(closure, ComponentClosure)
        assert closure.swc.name == "TargetSwc"
        assert {p.name for p in closure.swc.ports} == {"pSpeed", "rCalib"}
        assert {r.name for r in closure.swc.runnables} == {"TargetRunnable"}

    @pytest.mark.parametrize(
        ("iface_name", "kind"),
        [("ISpeed", "sender_receiver"), ("ICalib", "client_server")],
    )
    def test_includes_each_referenced_interface_kind(self, tmp_path: Path, iface_name: str, kind: str) -> None:
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        by_name = {i.name: i for i in closure.interfaces}
        assert iface_name in by_name
        assert by_name[iface_name].kind == kind

    @pytest.mark.parametrize(
        ("dt_name", "kind"),
        [
            ("uint8_base", "base_type"),
            ("float32_base", "base_type"),
            ("uint8", "value_type"),
            ("float32", "value_type"),
            ("Speed_T", "value_type"),
            ("Gear_T", "enum"),
            ("Pose_T", "record"),
            ("Samples_T", "array"),
            ("Distance_T", "application_type"),
        ],
    )
    def test_includes_each_data_type_kind_transitively(self, tmp_path: Path, dt_name: str, kind: str) -> None:
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        by_name = {d.name: d for d in closure.data_types}
        assert dt_name in by_name
        assert by_name[dt_name].kind == kind

    def test_excludes_unrelated_swc_interface_and_data_type(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        assert closure.swc.name != "OtherSwc"
        assert {i.name for i in closure.interfaces} == {"ISpeed", "ICalib"}
        assert {d.name for d in closure.data_types} == EXPECTED_CLOSURE_DATA_TYPES

    def test_output_ordering_is_deterministic_by_name(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        assert [i.name for i in closure.interfaces] == sorted(i.name for i in closure.interfaces)
        assert [d.name for d in closure.data_types] == sorted(d.name for d in closure.data_types)

    def test_swc_with_no_ports_yields_empty_closure_beyond_itself(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _dump(Path(model_root) / "swc" / "body" / "LonerSwc.yaml", {"name": "LonerSwc"})

        closure = compute_closure(model_root, "LonerSwc")

        assert closure.swc.name == "LonerSwc"
        assert closure.interfaces == []
        assert closure.data_types == []


class TestTheClosureFollowsEveryLinkOfATypeChain:
    """A value type's ``base`` and a reference type's ``base`` are both edges.

    Guards a closure that followed only the value type's edge, so an export naming only the top
    of a chain was refused as an undeclared base. Each case names the TOP link alone, so only the
    walk can collect a lower one.
    """

    @staticmethod
    def _chain_model(tmp_path: Path, top: str) -> str:
        model_root = tmp_path / "model"
        dt = model_root / "data_types"
        _dump(
            model_element_path(model_root, "u8_base", kind="base_type"),
            {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )
        _dump(dt / "U8.yaml", {"name": "U8", "kind": "value_type", "base": "u8_base"})
        _dump(dt / "U8Ref.yaml", {"name": "U8Ref", "kind": "reference_type", "base": "U8"})
        _dump(dt / "U8RefRef.yaml", {"name": "U8RefRef", "kind": "reference_type", "base": "U8Ref"})
        _dump(
            model_element_path(model_root, "Unused_base", kind="base_type"),
            {"name": "Unused_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )
        _dump(
            model_root / "interfaces" / "IChain.yaml",
            {"name": "IChain", "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": top}]},
        )
        _dump(
            model_root / "swc" / "body" / "ChainSwc.yaml",
            {"name": "ChainSwc", "ports": [{"name": "pChain", "direction": "provide", "interface": "IChain"}]},
        )
        return str(model_root)

    @pytest.mark.parametrize(
        ("top", "expected"),
        [
            pytest.param("U8", ["U8", "u8_base"], id="value_type"),
            pytest.param("U8Ref", ["U8", "U8Ref", "u8_base"], id="reference_type"),
            pytest.param("U8RefRef", ["U8", "U8Ref", "U8RefRef", "u8_base"], id="reference_type_over_reference_type"),
        ],
    )
    def test_every_link_below_the_named_type_is_collected(self, tmp_path: Path, top: str, expected: list[str]) -> None:
        closure = compute_closure(self._chain_model(tmp_path, top), "ChainSwc")

        assert [d.name for d in closure.data_types] == expected

    def test_a_reference_type_resting_on_an_undeclared_type_is_a_missing_reference(self, tmp_path: Path) -> None:
        model_root = self._chain_model(tmp_path, "U8RefRef")
        model_element_path(model_root, "U8Ref", kind="reference_type").unlink()

        with pytest.raises(MissingReferenceError, match="U8Ref") as exc_info:
            compute_closure(model_root, "ChainSwc")

        assert exc_info.value.missing == frozenset({"U8Ref"})


# Target SWC resolution errors


class TestTargetSwcResolution:
    def test_unknown_swc_raises_component_not_found(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)

        with pytest.raises(ComponentNotFoundError, match="NoSuchSwc"):
            compute_closure(model_root, "NoSuchSwc")

    def test_duplicate_swc_name_raises_ambiguous_component(self, tmp_path: Path) -> None:
        """Two SWC files sharing one `name` are refused by compute_closure: the SWC reader checks no uniqueness."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "powertrain" / "TargetSwcCopy.yaml",
            {"name": "TargetSwc", "description": "duplicate of body/TargetSwc.yaml"},
        )

        with pytest.raises(AmbiguousComponentError, match="TargetSwc"):
            compute_closure(model_root, "TargetSwc")


# Referenced interface / data type ambiguity


class TestAmbiguousReferences:
    def test_two_interface_files_with_referenced_name_raise(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "interfaces" / "ISpeed_duplicate.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "conflicting", "implementation_type": "uint8"}],
            },
        )

        with pytest.raises(AmbiguousReferenceError, match="ISpeed"):
            compute_closure(model_root, "TargetSwc")

    def test_two_data_type_files_pulled_in_transitively_raise(self, tmp_path: Path) -> None:
        """Gear_T, reached only via Pose_T's record field, declared twice still raises."""
        model_root = _build_model(tmp_path)
        # The repeat is inside the enum's own layer: a base type named `Gear_T` is another
        # layer's element (the test below).
        _dump(
            model_element_path(Path(model_root), "Gear_T_duplicate", kind="enum"),
            {"name": "Gear_T", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
        )

        with pytest.raises(AmbiguousReferenceError, match="Gear_T"):
            compute_closure(model_root, "TargetSwc")

    def test_a_base_type_named_like_a_transitively_reached_type_is_no_ambiguity(self, tmp_path: Path) -> None:
        """Identity is (layer, name): a base type sharing the enum's name is neither reached nor a repeat."""
        model_root = _build_model(tmp_path)
        _dump(
            model_element_path(Path(model_root), "Gear_T_duplicate", kind="base_type"),
            {"name": "Gear_T", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )

        closure = compute_closure(model_root, "TargetSwc")

        (gear,) = [d for d in closure.data_types if d.name == "Gear_T"]
        assert gear.kind == "enum"

    def test_duplicates_outside_the_reference_set_are_not_checked(self, tmp_path: Path) -> None:
        """Names the closure never references are the project's own problem and block nothing."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "interfaces" / "IOther_duplicate.yaml",
            {
                "name": "IOther",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v2", "implementation_type": "Other_T"}],
            },
        )
        _dump(
            model_element_path(Path(model_root), "Other_T_duplicate", kind="value_type"),
            {"name": "Other_T", "kind": "value_type", "base": "Other_T_base"},
        )

        closure = compute_closure(model_root, "TargetSwc")

        assert {d.name for d in closure.data_types} == EXPECTED_CLOSURE_DATA_TYPES


# Dangling (undeclared) references


class TestMissingReferences:
    def test_port_referencing_undeclared_interface_raises(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "body" / "DanglingSwc.yaml",
            {
                "name": "DanglingSwc",
                "ports": [{"name": "pGone", "direction": "provide", "interface": "IUndeclared"}],
            },
        )

        with pytest.raises(MissingReferenceError, match="IUndeclared") as exc_info:
            compute_closure(model_root, "DanglingSwc")
        assert "IUndeclared" in exc_info.value.missing

    def test_interface_referencing_undeclared_data_type_raises(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "interfaces" / "IBroken.yaml",
            {
                "name": "IBroken",
                "kind": "sender_receiver",
                "data_elements": [{"name": "x", "implementation_type": "Undeclared_T"}],
            },
        )
        _dump(
            Path(model_root) / "swc" / "body" / "BrokenSwc.yaml",
            {
                "name": "BrokenSwc",
                "ports": [{"name": "pBroken", "direction": "provide", "interface": "IBroken"}],
            },
        )

        with pytest.raises(MissingReferenceError, match="Undeclared_T") as exc_info:
            compute_closure(model_root, "BrokenSwc")
        assert "Undeclared_T" in exc_info.value.missing

    def test_record_field_referencing_undeclared_data_type_raises(self, tmp_path: Path) -> None:
        """A record inside the closure naming an undeclared type is a missing reference."""
        model_root = _build_model(tmp_path)
        model_element_path(model_root, "Gear_T", kind="enum").unlink()

        with pytest.raises(MissingReferenceError, match="Gear_T") as exc_info:
            compute_closure(model_root, "TargetSwc")
        assert "Gear_T" in exc_info.value.missing


class TestAReferenceResolvesOnlyInTheLayerItsFieldNames:
    """A name declared only in a layer the referring field does not name is missing.

    A name-only lookup would silently carry the wrong kind into the export.
    """

    @staticmethod
    def _probe_swc(model_root: str, element: dict[str, Any]) -> None:
        _dump(
            Path(model_root) / "interfaces" / "IProbe.yaml",
            {"name": "IProbe", "kind": "sender_receiver", "data_elements": [element]},
        )
        _dump(
            Path(model_root) / "swc" / "body" / "ProbeSwc.yaml",
            {"name": "ProbeSwc", "ports": [{"name": "pProbe", "direction": "provide", "interface": "IProbe"}]},
        )

    def test_an_implementation_type_field_does_not_resolve_to_an_application_type(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        write_model_element(model_root, {"name": "ProbeShared", "kind": "application_type"})
        self._probe_swc(model_root, {"name": "x", "implementation_type": "ProbeShared"})

        with pytest.raises(MissingReferenceError, match="ProbeShared") as exc_info:
            compute_closure(model_root, "ProbeSwc")
        assert exc_info.value.missing == frozenset({"ProbeShared"})

    def test_an_application_type_field_does_not_resolve_to_an_implementation_type(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        write_model_element(model_root, {"name": "ProbeShared", "kind": "value_type", "base": "uint8_base"})
        self._probe_swc(model_root, {"name": "x", "application_type": "ProbeShared"})

        with pytest.raises(MissingReferenceError, match="ProbeShared") as exc_info:
            compute_closure(model_root, "ProbeSwc")
        assert exc_info.value.missing == frozenset({"ProbeShared"})

    def test_a_value_types_base_does_not_resolve_to_an_implementation_type(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        write_model_element(model_root, {"name": "ProbeShared", "kind": "value_type", "base": "uint8_base"})
        write_model_element(model_root, {"name": "ProbeValue", "kind": "value_type", "base": "ProbeShared"})
        self._probe_swc(model_root, {"name": "x", "implementation_type": "ProbeValue"})

        with pytest.raises(MissingReferenceError, match="ProbeShared") as exc_info:
            compute_closure(model_root, "ProbeSwc")
        assert exc_info.value.missing == frozenset({"ProbeShared"})

    def test_the_same_names_in_their_own_layers_resolve(self, tmp_path: Path) -> None:
        """The control: the three refusals above are about the layer, not the shape."""
        model_root = _build_model(tmp_path)
        write_model_element(model_root, {"name": "ProbeShared", "kind": "value_type", "base": "uint8_base"})
        write_model_element(model_root, {"name": "ProbeApp", "kind": "application_type"})
        _dump(
            Path(model_root) / "interfaces" / "IProbe.yaml",
            {
                "name": "IProbe",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "x", "implementation_type": "ProbeShared"},
                    {"name": "y", "application_type": "ProbeApp"},
                ],
            },
        )
        _dump(
            Path(model_root) / "swc" / "body" / "ProbeSwc.yaml",
            {"name": "ProbeSwc", "ports": [{"name": "pProbe", "direction": "provide", "interface": "IProbe"}]},
        )

        closure = compute_closure(model_root, "ProbeSwc")

        assert {d.name for d in closure.data_types} == {"ProbeShared", "ProbeApp", "uint8_base"}


# Ownership boundary: integration-owned layers are never read


class TestIntegrationLayersNeverRead:
    def test_malformed_integration_layer_files_do_not_break_closure(self, tmp_path: Path) -> None:
        """`compute_closure` reads no integration-owned layer: a malformed file there would raise.

        The layers are derived (every declared layer the closure does not own), never a hand-written
        list, which can miss one such as `resources/`.
        """
        model_root = _build_model(tmp_path)
        integration_owned = _integration_owned_layers()
        assert integration_owned, "the assertion is vacuous unless some layer is integration-owned"
        for layer in integration_owned:
            bad = Path(model_root) / layer / "malformed.yaml"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("{ this is: [ not valid yaml", encoding="utf-8")

        closure = compute_closure(model_root, "TargetSwc")

        assert closure.swc.name == "TargetSwc"
        assert {i.name for i in closure.interfaces} == {"ISpeed", "ICalib"}
        assert {d.name for d in closure.data_types} == EXPECTED_CLOSURE_DATA_TYPES

    def test_closure_result_carries_no_integration_content(self, tmp_path: Path) -> None:
        """The `ComponentClosure` dataclass has exactly the component-owned fields.

        It is the anchor the test above derives its complement from.
        """
        model_root = _build_model(tmp_path)

        closure = compute_closure(model_root, "TargetSwc")

        # Pinned figures: a field added to or dropped from `ComponentClosure` fails here before the
        # derived complement silently moves. Counts, because `test_layer_vocabulary_gate.py` refuses a
        # literal set of layer names in a test module: five fields; nine owned layers, the six data
        # description layers standing for `data_types` and `elements`.
        fields = set(closure.__dataclass_fields__)
        assert len(fields) == 5
        assert {"data_types", "elements"} <= fields
        assert fields - {"data_types", "elements"} <= set(production_registry().layer_names)
        assert len(_component_owned_layers()) == 9
        assert _component_owned_layers() >= fields - {"data_types", "elements"}
        assert {d.name for d in closure.data_types} >= {"uint8_base", "float32_base"}, (
            "the base types travel in `data_types`"
        )
        assert _component_owned_layers() <= set(production_registry().layer_names)


def test_a_prototype_stating_no_type_is_refused() -> None:
    """Only a schema bypass builds a data element with neither type; the closure refuses it by name."""
    from swcraft_tools.component_exchange.closure import _prototype_type_key
    from swcraft_tools.schema import DataElement

    with pytest.raises(RuntimeError, match="'Speed' states no type"):
        _prototype_type_key(DataElement.model_construct(name="Speed"))
