"""The implementation composites and pointer, at ``check-model``.

``implementation_record`` (an ``IMPLEMENTATION-DATA-TYPE`` of category
``STRUCTURE``), ``implementation_array`` (``ARRAY``, fixed size) and ``pointer``
(``DATA_REFERENCE``), asked of the validator over one real, valid project (the
``read-swc-element`` vector) broken one way per test, and over the compositions fixture
and the mixed-event fixture where the question needs their shape.

The mapping predicate judges a composite map's category, member count, members position
by position, array length and the scalar leaf's category; it does **not** judge member
names, scalar representability, same-shape swapped members, or an unresolved or stub
child. ``[constr_1066]`` refuses a map onto a pointer directly, through an alias and inside
a sub-element. ``[constr_1295]`` is judged only where both ends are declared application
components, not across an external component stub or a service component.

Every implementation role resolves in ``implementation_types`` alone, and a record field and
an array element in ``application_types`` alone; a model naming the other layer reports its
role's undeclared-name rule.

What it does **not** reach: the writer's refusals and emission, the protocol's writes of
these kinds, the importer, and any model shape outside the ones written below — this is
example-based evidence, not a proof over every model declaring an implementation
composite.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.registry as reg
from conftest import model_element_storage_name, write_model_element
from external_stubs_support import COMPOSITIONS_MODEL_ROOT, change, put
from operation_event_support import SERVER_FILE, TIMING_EVENT, mixed_event_model
from swcraft_tools import validator
from swcraft_tools.data_description import read_data_description
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    INVALID_IDENTIFIER,
    RULE_CODES,
    diagnose,
    emitted_identifier_violations,
)
from swcraft_tools.validator import ConsistencyIssue, check_cross_layer_consistency
from swcraft_tools.validator.compatibility import ResolvedType, resolve_type

_PROVIDER = "swc/example/ExampleSWC.yaml"
_CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
_SR_INTERFACE = "interfaces/IExample.yaml"
_CS_INTERFACE = "interfaces/IDiagnosticService.yaml"

#: The cross-element rules contract ``7.8.0`` added that contract ``8.0.0``
#: keeps, and the code each is answered with. Written out rather than read from
#: ``RULE_CODES``: a rule moving between codes changes what a client is told to
#: repair.
NEW_RULE_CODES: dict[str, str] = {
    "data-type.implementation-element-undeclared-type": BROKEN_REFERENCE,
    "data-type.implementation-record-duplicate-element-name": CONSISTENCY_VIOLATION,
    "data-type.implementation-array-undeclared-element-type": BROKEN_REFERENCE,
    "data-type.pointer-undeclared-target": BROKEN_REFERENCE,
    "mapping-set.incompatible-composite-map": CONSISTENCY_VIOLATION,
    "mapping-set.pointer-mapped": CONSISTENCY_VIOLATION,
    "connector.pointer-between-application-components": CONSISTENCY_VIOLATION,
}

#: The three of contract ``7.8.0``'s ten that contract ``8.0.0`` retired: each
#: judged an application-side name in an implementation role, which is now an
#: undeclared name of ``implementation_types``.
RETIRED_ROLE_RULES: tuple[str, ...] = (
    "data-type.implementation-element-not-implementation-type",
    "data-type.implementation-array-element-not-implementation-type",
    "data-type.pointer-target-not-implementation-type",
)

#: The two wrong-kind rules of the older roles contract ``8.0.0`` retired too.
RETIRED_OLDER_ROLE_RULES: tuple[str, ...] = (
    "data-type.reference-type-base-not-implementation-type",
    "data-type.composite-member-implementation-type",
)


# The model and its builders


@pytest.fixture()
def model_root(tmp_path: Path) -> Path:
    root = fixtures.build_fixture(tmp_path / "project", "read-swc-element") / "model"
    assert check_cross_layer_consistency(str(root)).consistent
    return root


def _type(model_root: Path, document: dict[str, Any]) -> None:
    put(model_root, model_element_storage_name(document["name"], kind=document["kind"]), document)


def _impl_record(model_root: Path, name: str, *elements: tuple[str, str]) -> None:
    _type(
        model_root,
        {"name": name, "kind": "implementation_record", "elements": [{"name": n, "type": t} for n, t in elements]},
    )


def _impl_array(model_root: Path, name: str, element_type: str, length: int = 4, element_name: str = "Element") -> None:
    _type(
        model_root,
        {
            "name": name,
            "kind": "implementation_array",
            "element_type": element_type,
            "length": length,
            "element_name": element_name,
        },
    )


def _pointer(model_root: Path, name: str, target: str) -> None:
    _type(model_root, {"name": name, "kind": "pointer", "target": target})


def _reference(model_root: Path, name: str, base: str) -> None:
    _type(model_root, {"name": name, "kind": "reference_type", "base": base})


def _application(model_root: Path, name: str, **physics: Any) -> None:
    _type(model_root, {"name": name, "kind": "application_type", **physics})


def _record(model_root: Path, name: str, *fields: tuple[str, str]) -> None:
    _type(model_root, {"name": name, "kind": "record", "fields": [{"name": n, "type": t} for n, t in fields]})


def _array(model_root: Path, name: str, element_type: str, length: int = 4) -> None:
    _type(model_root, {"name": name, "kind": "array", "element_type": element_type, "length": length})


def _stub(model_root: Path, name: str) -> None:
    _type(
        model_root,
        {
            "name": name,
            "kind": "external_type",
            "external_ref": f"/Foreign/ImplementationDataTypes/{name}",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        },
    )


def _mapping_set(model_root: Path, name: str, *pairs: tuple[str, str]) -> None:
    write_model_element(
        model_root,
        {"name": name, "maps": [{"application": a, "implementation": i} for a, i in pairs]},
        layer="mapping_sets",
    )


def _type_field(model_root: Path, type_name: str) -> str:
    """``application_type`` when *type_name* is a declared application type, else ``implementation_type``."""
    return (
        "application_type"
        if (model_root / model_element_storage_name(type_name, kind="enum")).is_file()
        else "implementation_type"
    )


def _set_type(model_root: Path, prototype: dict[str, Any], type_name: str) -> None:
    """Type *prototype* by *type_name* in the field its side asks for, where its type field stood."""
    field = _type_field(model_root, type_name)
    items = [
        (field if key in ("implementation_type", "application_type") else key, value)
        for key, value in prototype.items()
    ]
    prototype.clear()
    prototype.update({key: (type_name if key == field else value) for key, value in items})


def _typed_element(model_root: Path, type_name: str, init: Any = None) -> None:
    """Give ``IExample.value`` another type, and an ``init`` when one is passed."""

    def mutate(document: dict[str, Any]) -> None:
        _set_type(model_root, document["data_elements"][0], type_name)
        if init is not None:
            document["data_elements"][0]["init"] = init

    change(model_root, _SR_INTERFACE, mutate)


def _records(model_root: Path) -> list[ConsistencyIssue]:
    return list(check_cross_layer_consistency(str(model_root)).records)


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in _records(model_root)]


def _only(model_root: Path, rule: str) -> ConsistencyIssue:
    records = _records(model_root)
    assert [record.rule for record in records] == [rule], [(r.rule, r.message) for r in records]
    return records[0]


def _data_type(name: str, *steps: Any) -> Subject:
    return Subject("implementation_types", name, tuple(steps))


def _application_type(name: str, *steps: Any) -> Subject:
    return Subject("application_types", name, tuple(steps))


def _element_type(record: str, element: str) -> Subject:
    return _data_type(record, field_step("elements"), member_step(element), field_step("type"))


# The three reference roles

#: Every kind a role of the new kinds admits, as ``(label, builder)``: the
#: builder declares a type named ``Target`` of that kind.
_ADMITTED: dict[str, Any] = {
    "value_type": lambda root: _type(root, {"name": "Target", "kind": "value_type", "base": "uint8_base"}),
    "reference_type": lambda root: _reference(root, "Target", "uint8"),
    "implementation_record": lambda root: _impl_record(root, "Target", ("a", "uint8")),
    "implementation_array": lambda root: _impl_array(root, "Target", "uint8"),
    "pointer": lambda root: _pointer(root, "Target", "uint8"),
    "external_type": lambda root: _stub(root, "Target"),
}


def _base_target(root: Path) -> None:
    """A base type named ``Target``.

    Since contract ``8.0.0`` it lies in the ``base_types`` layer, which no role
    of the new kinds resolves in, so the reference is undeclared rather than
    refused for its kind.
    """
    _type(root, {"name": "Target", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "t"})


#: Every application-side kind: each lies in the ``application_types`` layer,
#: which no implementation role resolves in, so naming one is an undeclared name.
_REFUSED: dict[str, Any] = {
    "application_type": lambda root: _application(root, "Target"),
    "enum": lambda root: _type(root, {"name": "Target", "kind": "enum", "literals": [{"name": "A", "value": 0}]}),
    "record": lambda root: _record(root, "Target", ("a", "Speed")),
    "array": lambda root: _array(root, "Target", "Speed"),
}


def _with_speed(model_root: Path) -> None:
    """``Speed``, the application type the application composites above are built over."""
    _application(model_root, "Speed")


class TestTheImplementationRecordElementRole:
    def test_an_undeclared_element_type_is_a_broken_reference(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "Nope"))

        record = _only(model_root, "data-type.implementation-element-undeclared-type")

        assert record.subjects == (_element_type("Frame", "a"),)
        assert record.target == "Nope"
        assert "Implementation record 'Frame' element 'a'" in record.message

    @pytest.mark.parametrize("kind", sorted(_REFUSED))
    def test_an_application_side_kind_is_not_declared_in_the_roles_layer(self, model_root: Path, kind: str) -> None:
        """An undeclared name, never the retired ``implementation-element-not-implementation-type``."""
        _with_speed(model_root)
        _REFUSED[kind](model_root)
        _impl_record(model_root, "Frame", ("a", "Target"))

        record = _only(model_root, "data-type.implementation-element-undeclared-type")

        assert record.subjects == (_element_type("Frame", "a"),)
        assert record.target == "Target"
        assert "data-type.implementation-element-not-implementation-type" not in RULE_CODES

    def test_a_base_type_is_not_declared_in_the_roles_layer(self, model_root: Path) -> None:
        _with_speed(model_root)
        _base_target(model_root)
        _impl_record(model_root, "Frame", ("a", "Target"))

        record = _only(model_root, "data-type.implementation-element-undeclared-type")

        assert record.subjects == (_element_type("Frame", "a"),)
        assert record.target == "Target"

    @pytest.mark.parametrize("kind", sorted(_ADMITTED))
    def test_every_admitted_kind_is_accepted(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        _impl_record(model_root, "Frame", ("a", "Target"))

        assert _rules(model_root) == []

    def test_a_duplicate_element_name_is_reported_once_at_the_member(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint8"), ("a", "uint16"), ("b", "uint8"))

        record = _only(model_root, "data-type.implementation-record-duplicate-element-name")

        assert record.subjects == (_data_type("Frame", field_step("elements"), member_step("a")),)
        assert "['a']" in record.message


class TestTheImplementationArrayElementRole:
    def test_an_undeclared_element_type_is_a_broken_reference(self, model_root: Path) -> None:
        _impl_array(model_root, "Samples", "Nope")

        record = _only(model_root, "data-type.implementation-array-undeclared-element-type")

        assert record.subjects == (_data_type("Samples", field_step("element_type")),)
        assert record.target == "Nope"

    @pytest.mark.parametrize("kind", sorted(_REFUSED))
    def test_an_application_side_kind_is_not_declared_in_the_roles_layer(self, model_root: Path, kind: str) -> None:
        """An undeclared name, never the retired ``implementation-array-element-not-implementation-type``."""
        _with_speed(model_root)
        _REFUSED[kind](model_root)
        _impl_array(model_root, "Samples", "Target")

        record = _only(model_root, "data-type.implementation-array-undeclared-element-type")

        assert record.subjects == (_data_type("Samples", field_step("element_type")),)
        assert record.target == "Target"
        assert "data-type.implementation-array-element-not-implementation-type" not in RULE_CODES

    def test_a_base_type_is_not_declared_in_the_roles_layer(self, model_root: Path) -> None:
        _with_speed(model_root)
        _base_target(model_root)
        _impl_array(model_root, "Samples", "Target")

        record = _only(model_root, "data-type.implementation-array-undeclared-element-type")

        assert record.subjects == (_data_type("Samples", field_step("element_type")),)
        assert record.target == "Target"

    @pytest.mark.parametrize("kind", sorted(_ADMITTED))
    def test_every_admitted_kind_is_accepted(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        _impl_array(model_root, "Samples", "Target")

        assert _rules(model_root) == []


class TestThePointerTargetRole:
    def test_an_undeclared_target_is_a_broken_reference(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "Nope")

        record = _only(model_root, "data-type.pointer-undeclared-target")

        assert record.subjects == (_data_type("Ptr", field_step("target")),)
        assert record.target == "Nope"

    @pytest.mark.parametrize("kind", sorted(_REFUSED))
    def test_an_application_side_kind_is_not_declared_in_the_roles_layer(self, model_root: Path, kind: str) -> None:
        """An undeclared name, never the retired ``pointer-target-not-implementation-type``."""
        _with_speed(model_root)
        _REFUSED[kind](model_root)
        _pointer(model_root, "Ptr", "Target")

        record = _only(model_root, "data-type.pointer-undeclared-target")

        assert record.subjects == (_data_type("Ptr", field_step("target")),)
        assert record.target == "Target"
        assert "data-type.pointer-target-not-implementation-type" not in RULE_CODES

    def test_a_base_type_is_not_declared_in_the_roles_layer(self, model_root: Path) -> None:
        _with_speed(model_root)
        _base_target(model_root)
        _pointer(model_root, "Ptr", "Target")

        record = _only(model_root, "data-type.pointer-undeclared-target")

        assert record.subjects == (_data_type("Ptr", field_step("target")),)
        assert record.target == "Target"

    @pytest.mark.parametrize("kind", sorted(_ADMITTED))
    def test_every_admitted_kind_is_accepted(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        _pointer(model_root, "Ptr", "Target")

        assert _rules(model_root) == []

    def test_a_pointer_to_a_named_pointer_is_accepted(self, model_root: Path) -> None:
        """``[constr_1254]``'s option 1: the pointer to a pointer goes through a named type."""
        _pointer(model_root, "Ptr", "uint8")
        _pointer(model_root, "PtrPtr", "Ptr")

        assert _rules(model_root) == []


class TestTheWidenedAndTheGuardedRoles:
    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer"])
    def test_a_reference_type_may_alias_each_new_kind(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        _reference(model_root, "Alias", "Target")

        assert _rules(model_root) == []

    def test_a_reference_type_over_an_application_record_is_an_undeclared_base(self, model_root: Path) -> None:
        """Still refused: a reference type's ``base`` resolves in ``implementation_types`` alone."""
        _with_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _reference(model_root, "Alias", "Pose")

        record = _only(model_root, "data-type.reference-type-undeclared-base")
        assert "implementation_record" in record.message
        assert "not declared in the implementation_types layer" in record.message
        assert record.target == "Pose"

    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer"])
    def test_a_record_field_naming_a_new_kind_is_an_undeclared_field_type(self, model_root: Path, kind: str) -> None:
        """An undeclared name, never the retired ``composite-member-implementation-type``."""
        _ADMITTED[kind](model_root)
        _record(model_root, "Pose", ("x", "Target"))

        record = _only(model_root, "data-type.record-field-undeclared-type")
        assert record.subjects == (
            _application_type("Pose", field_step("fields"), member_step("x"), field_step("type")),
        )

    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer"])
    def test_an_array_element_naming_a_new_kind_is_an_undeclared_element_type(
        self, model_root: Path, kind: str
    ) -> None:
        """An undeclared name, never the retired ``composite-member-implementation-type``."""
        _ADMITTED[kind](model_root)
        _array(model_root, "Poses", "Target")

        record = _only(model_root, "data-type.array-undeclared-element-type")
        assert record.subjects == (_application_type("Poses", field_step("element_type")),)
        assert record.message.endswith("in the application_types layer."), record.message

    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer"])
    def test_an_argument_may_be_typed_by_each_new_kind(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        change(
            model_root, _CS_INTERFACE, lambda d: d["operations"][0]["arguments"][0].update(implementation_type="Target")
        )
        change(
            model_root, _CS_INTERFACE, lambda d: d["operations"][1]["arguments"][0].update(implementation_type="Target")
        )

        assert _rules(model_root) == []


# data-type.cycle over the new edges


class TestCyclesThroughTheNewEdges:
    def test_a_record_containing_itself_is_a_cycle(self, model_root: Path) -> None:
        _impl_record(model_root, "Node", ("a", "uint8"), ("self", "Node"))

        assert "data-type.cycle" in _rules(model_root)

    def test_an_array_of_itself_through_a_record_is_a_cycle(self, model_root: Path) -> None:
        _impl_array(model_root, "Many", "Holder")
        _impl_record(model_root, "Holder", ("many", "Many"))

        record = next(r for r in _records(model_root) if r.rule == "data-type.cycle")
        assert {subject.element for subject in record.subjects} == {"Many", "Holder"}

    def test_a_recursive_pointer_graph_is_one_cycle_finding(self, model_root: Path) -> None:
        """A linked list through a pointer: refused as ``data-type.cycle``, one cause one finding."""
        _impl_record(model_root, "ListNode", ("value", "uint8"), ("next", "ListNodePtr"))
        _pointer(model_root, "ListNodePtr", "ListNode")

        assert _rules(model_root) == ["data-type.cycle"]

    def test_two_pointers_naming_each_other_are_a_cycle(self, model_root: Path) -> None:
        _pointer(model_root, "PtrA", "PtrB")
        _pointer(model_root, "PtrB", "PtrA")

        assert _rules(model_root) == ["data-type.cycle"]

    def test_a_map_between_two_cyclic_composites_is_the_cycle_alone(self, model_root: Path) -> None:
        """The map predicate's ``seen`` guard: a map over two self-containing sides ends, adding nothing."""
        _application(model_root, "Speed")
        _record(model_root, "Node", ("a", "Speed"), ("self", "Node"))
        _impl_record(model_root, "NodeImpl", ("a", "uint8"), ("self", "NodeImpl"))
        _mapping_set(model_root, "Maps", ("Node", "NodeImpl"))

        assert set(_rules(model_root)) == {"data-type.cycle"}

    def test_an_acyclic_chain_through_every_new_edge_is_accepted(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"), ("b", "uint8"))
        _impl_array(model_root, "Frames", "Frame")
        _pointer(model_root, "FramesPtr", "Frames")
        _reference(model_root, "FramesPtrAlias", "FramesPtr")

        assert _rules(model_root) == []


# Resolution and compatibility over the new kinds


def _resolved(model_root: Path, name: str) -> ResolvedType | None:
    # Both data description layers: a value type's `base` resolves in
    # `base_types`, which `implementation_types` alone does not hold (contract `8.0.0`).
    # Every name asked here is an implementation type: `application=False`.
    return resolve_type(name, read_data_description(str(model_root)), {}, application=False)


def _second_interface(model_root: Path, element_type: str) -> None:
    """``IExample2`` with one element ``value`` of *element_type*, and the consumer's port on it."""
    put(
        model_root,
        "interfaces/IExample2.yaml",
        {
            "name": "IExample2",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", _type_field(model_root, element_type): element_type}],
        },
    )
    change(model_root, _CONSUMER, lambda d: d["ports"][0].update(interface="IExample2"))


class TestResolutionReachesEveryKind:
    """``resolve_type``'s docstring says every kind the contract declares has an arm."""

    def test_an_implementation_record_resolves_to_its_elements(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"), ("b", "uint8"))

        resolved = _resolved(model_root, "Frame")

        assert resolved is not None
        assert resolved.kind == "implementation_record"
        assert [(name, type_name) for name, type_name, _ in resolved.fields] == [("a", "uint16"), ("b", "uint8")]

    def test_an_implementation_array_resolves_to_its_element_and_length(self, model_root: Path) -> None:
        _impl_array(model_root, "Samples", "uint8", 6)

        resolved = _resolved(model_root, "Samples")

        assert resolved is not None
        assert resolved.kind == "implementation_array"
        assert resolved.length == 6
        assert resolved.element is not None and resolved.element[0] == "uint8"

    def test_a_pointer_resolves_to_its_target(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")

        resolved = _resolved(model_root, "Ptr")

        assert resolved is not None
        assert resolved.kind == "pointer"
        assert resolved.target is not None and resolved.target[0] == "uint8"

    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer"])
    def test_an_alias_resolves_through_to_the_composite(self, model_root: Path, kind: str) -> None:
        _ADMITTED[kind](model_root)
        _reference(model_root, "Alias", "Target")

        resolved = _resolved(model_root, "Alias")

        assert resolved is not None and resolved.kind == kind

    def test_a_composite_over_a_stub_member_abstains(self, model_root: Path) -> None:
        _stub(model_root, "Foreign")
        _impl_record(model_root, "Frame", ("a", "Foreign"))

        assert _resolved(model_root, "Frame") is None

    def test_a_recursive_pointer_graph_abstains(self, model_root: Path) -> None:
        _impl_record(model_root, "ListNode", ("next", "ListNodePtr"))
        _pointer(model_root, "ListNodePtr", "ListNode")

        assert _resolved(model_root, "ListNodePtr") is None


class TestConnectorsAcrossTheNewKinds:
    def test_two_equal_structures_named_differently_are_compatible(self, model_root: Path) -> None:
        _impl_record(model_root, "FrameA", ("a", "uint16"), ("b", "uint8"))
        _impl_record(model_root, "FrameB", ("a", "uint16"), ("b", "uint8"))
        _reference(model_root, "FrameAlias", "FrameB")
        _typed_element(model_root, "FrameA")
        _second_interface(model_root, "FrameAlias")

        assert _rules(model_root) == []

    def test_a_structure_whose_element_differs_is_an_interface_mismatch(self, model_root: Path) -> None:
        _impl_record(model_root, "FrameA", ("a", "uint16"), ("b", "uint8"))
        _impl_record(model_root, "FrameB", ("a", "uint16"), ("b", "uint16"))
        _typed_element(model_root, "FrameA")
        _second_interface(model_root, "FrameB")

        record = _only(model_root, "connector.interface-mismatch")
        assert "implementation record element 'b'" in record.message

    def test_an_element_name_counts_for_a_connection(self, model_root: Path) -> None:
        """``[constr_1187]`` 2: every attribute but the type's own short name matches."""
        _impl_record(model_root, "FrameA", ("a", "uint8"))
        _impl_record(model_root, "FrameB", ("z", "uint8"))
        _typed_element(model_root, "FrameA")
        _second_interface(model_root, "FrameB")

        record = _only(model_root, "connector.interface-mismatch")
        assert "implementation record element 1 'z' vs 'a'" in record.message

    def test_an_array_element_name_counts_for_a_connection(self, model_root: Path) -> None:
        """``[constr_1187]`` 2 read as for the record: the sub-element's short name is an attribute."""
        _impl_array(model_root, "SamplesA", "uint8", 4, element_name="Sample")
        _impl_array(model_root, "SamplesB", "uint8", 4, element_name="Value")
        _typed_element(model_root, "SamplesA")
        _second_interface(model_root, "SamplesB")

        record = _only(model_root, "connector.interface-mismatch")
        assert "implementation array element name 'Value' vs 'Sample'" in record.message

    def test_arrays_of_different_length_are_an_interface_mismatch(self, model_root: Path) -> None:
        _impl_array(model_root, "SamplesA", "uint8", 4)
        _impl_array(model_root, "SamplesB", "uint8", 8)
        _typed_element(model_root, "SamplesA")
        _second_interface(model_root, "SamplesB")

        record = _only(model_root, "connector.interface-mismatch")
        assert "implementation array length 8 vs 4" in record.message

    def test_a_structure_against_an_application_record_is_a_kind_mismatch(self, model_root: Path) -> None:
        _with_speed(model_root)
        _record(model_root, "Pose", ("a", "Speed"))
        _impl_record(model_root, "Frame", ("a", "uint8"))
        _typed_element(model_root, "Frame")
        _second_interface(model_root, "Pose")

        messages = [r.message for r in _records(model_root) if r.rule == "connector.interface-mismatch"]
        assert messages and "kind record vs implementation_record" in messages[0]


# init on the new kinds


class TestInitOnTheNewKinds:
    def test_a_map_of_exactly_the_elements_is_accepted(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"), ("b", "uint8"))
        _typed_element(model_root, "Frame", {"a": 1, "b": 2})

        assert _rules(model_root) == []

    def test_a_map_missing_an_element_is_a_record_field_mismatch(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"), ("b", "uint8"))
        _typed_element(model_root, "Frame", {"a": 1})

        record = _only(model_root, "interface.init-record-field-mismatch")
        assert record.subjects[1] == _data_type("Frame", field_step("elements"))
        assert "missing ['b']" in record.message

    def test_a_scalar_where_a_structure_is_declared_is_a_shape_mismatch(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"))
        _typed_element(model_root, "Frame", 3)

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    def test_a_member_value_is_judged_against_its_own_type(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"))
        _typed_element(model_root, "Frame", {"a": "text"})

        record = _only(model_root, "interface.init-string-scalar")
        assert "'init.a'" in record.message

    def test_a_list_of_the_declared_length_is_accepted(self, model_root: Path) -> None:
        _impl_array(model_root, "Samples", "uint8", 3)
        _typed_element(model_root, "Samples", [1, 2, 3])

        assert _rules(model_root) == []

    def test_a_list_of_another_length_is_an_array_length_mismatch(self, model_root: Path) -> None:
        _impl_array(model_root, "Samples", "uint8", 3)
        _typed_element(model_root, "Samples", [1, 2])

        record = _only(model_root, "interface.init-array-length-mismatch")
        assert record.subjects[1] == _data_type("Samples", field_step("length"))

    def test_an_alias_to_a_composite_takes_the_composite_shape(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"))
        _reference(model_root, "FrameAlias", "Frame")
        _typed_element(model_root, "FrameAlias", {"a": 1})

        assert _rules(model_root) == []

    def test_an_alias_to_a_composite_refuses_a_scalar(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("a", "uint16"))
        _reference(model_root, "FrameAlias", "Frame")
        _typed_element(model_root, "FrameAlias", 1)

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    def test_a_pointer_takes_no_init(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _typed_element(model_root, "Ptr", 0)
        # A service provider, so [constr_1295] has no finding to add on this link.
        change(model_root, _PROVIDER, lambda d: d.update(type="service"))

        record = _only(model_root, "interface.init-shape-mismatch")
        assert "pointer" in record.message
        assert record.subjects[1] == _data_type("Ptr")

    def test_a_pointer_inside_a_structure_takes_no_init(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _impl_record(model_root, "Frame", ("a", "uint8"), ("p", "Ptr"))
        _typed_element(model_root, "Frame", {"a": 1, "p": 0})

        record = _only(model_root, "interface.init-shape-mismatch")
        assert "'init.p'" in record.message

    def test_an_init_reaching_a_stub_inside_a_composite_is_refused(self, model_root: Path) -> None:
        """The rule's walk extends into a composite's members."""
        _stub(model_root, "Foreign")
        _impl_record(model_root, "Frame", ("a", "uint8"), ("f", "Foreign"))
        _typed_element(model_root, "Frame", {"a": 1, "f": 2})

        record = _only(model_root, "interface.init-on-external-type")
        assert record.subjects[1] == _data_type("Foreign")
        assert "'init.f'" in record.message

    def test_an_init_reaching_a_stub_through_an_alias_inside_an_array_is_refused(self, model_root: Path) -> None:
        _stub(model_root, "Foreign")
        _reference(model_root, "ForeignAlias", "Foreign")
        _impl_array(model_root, "Samples", "ForeignAlias", 2)
        _typed_element(model_root, "Samples", [1, 2])

        records = _records(model_root)
        assert {record.rule for record in records} == {"interface.init-on-external-type"}
        assert all(record.subjects[1] == _data_type("Foreign") for record in records)


# The mapping predicate and [constr_1066]


def _mapped_speed(model_root: Path) -> None:
    """``Speed`` and ``Heading``, two application types, and ``Gear``, an enumeration."""
    _application(model_root, "Speed", unit="km/h")
    _application(model_root, "Heading", unit="deg")
    _type(model_root, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})


class TestTheCompositeMapPredicate:
    def test_a_record_onto_a_structure_of_the_same_shape_is_accepted(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"), ("gear", "Gear"))
        _impl_record(model_root, "PoseImpl", ("x", "uint16"), ("gear", "uint8"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_renamed_members_are_accepted(self, model_root: Path) -> None:
        """Member names are not judged."""
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _impl_record(model_root, "PoseImpl", ("somethingElse", "uint16"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_a_different_member_count_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"), ("y", "Speed"))
        _impl_record(model_root, "PoseImpl", ("x", "uint16"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert record.subjects == (
            Subject("mapping_sets", "Maps", (field_step("maps"), member_step("Pose"), field_step("implementation"))),
        )
        assert "2 fields" in record.message and "1 element" in record.message

    def test_a_wrong_category_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _impl_array(model_root, "PoseImpl", "uint16", 1)
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "record 'Pose' vs implementation_array 'PoseImpl'" in record.message

    def test_a_record_onto_a_value_type_is_a_moved_refusal(self, model_root: Path) -> None:
        """The category check refuses it, not ``unknown-application``."""
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _mapping_set(model_root, "Maps", ("Pose", "uint16"))

        _only(model_root, "mapping-set.incompatible-composite-map")

    def test_an_application_type_onto_a_structure_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _impl_record(model_root, "PoseImpl", ("x", "uint16"))
        _mapping_set(model_root, "Maps", ("Speed", "PoseImpl"))

        _only(model_root, "mapping-set.incompatible-composite-map")

    def test_reordered_members_of_distinguishable_shape_are_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _array(model_root, "Speeds", "Speed", 2)
        _record(model_root, "Pose", ("speeds", "Speeds"), ("x", "Speed"))
        _impl_array(model_root, "SpeedsImpl", "uint16", 2)
        _impl_record(model_root, "PoseImpl", ("x", "uint16"), ("speeds", "SpeedsImpl"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "member 1" in record.message

    def test_reordered_members_of_the_same_shape_are_not_detected(self, model_root: Path) -> None:
        """The stated limit: ``{int, int}`` against ``{int, int}`` cannot tell which is which."""
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"), ("heading", "Heading"))
        _impl_record(model_root, "PoseImpl", ("heading", "uint16"), ("x", "uint16"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_an_incompatible_scalar_range_is_not_judged(self, model_root: Path) -> None:
        """Scalar representability is not judged."""
        _application(model_root, "Big", range={"min": 0.0, "max": 1.0e9})
        _record(model_root, "Pose", ("x", "Big"))
        _impl_record(model_root, "PoseImpl", ("x", "uint8"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_a_scalar_leaf_against_a_composite_element_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _impl_record(model_root, "Inner", ("a", "uint8"))
        _impl_record(model_root, "PoseImpl", ("x", "Inner"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "member 1" in record.message

    def test_different_but_compatible_child_type_names_are_accepted(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _reference(model_root, "Speed_T", "uint16")
        _impl_record(model_root, "PoseImpl", ("x", "Speed_T"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_an_array_of_another_length_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _array(model_root, "Speeds", "Speed", 4)
        _impl_array(model_root, "SpeedsImpl", "uint16", 8)
        _mapping_set(model_root, "Maps", ("Speeds", "SpeedsImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "length 4 vs 8" in record.message

    def test_an_array_of_the_same_length_is_accepted(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _array(model_root, "Speeds", "Speed", 4)
        _impl_array(model_root, "SpeedsImpl", "uint16", 4)
        _mapping_set(model_root, "Maps", ("Speeds", "SpeedsImpl"))

        assert _rules(model_root) == []

    def test_an_alias_to_a_composite_is_judged_through(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"), ("y", "Speed"))
        _impl_record(model_root, "PoseImpl", ("x", "uint16"))
        _reference(model_root, "PoseAlias", "PoseImpl")
        _mapping_set(model_root, "Maps", ("Pose", "PoseAlias"))

        _only(model_root, "mapping-set.incompatible-composite-map")

    def test_an_unresolved_external_child_is_not_judged(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _stub(model_root, "Foreign")
        _record(model_root, "Pose", ("x", "Speed"))
        _impl_record(model_root, "PoseImpl", ("x", "Foreign"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        assert _rules(model_root) == []

    def test_a_member_after_a_stub_is_still_judged(self, model_root: Path) -> None:
        """A stub member is passed over, never counted compatible: the next member is judged."""
        _mapped_speed(model_root)
        _stub(model_root, "Foreign")
        _impl_record(model_root, "Inner", ("a", "uint8"))
        _record(model_root, "Pose", ("x", "Speed"), ("y", "Speed"))
        _impl_record(model_root, "PoseImpl", ("x", "Foreign"), ("y", "Inner"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "member 2 of record 'Pose': category application_type 'Speed' vs implementation_record 'Inner'" in (
            record.message
        )

    def test_an_array_element_is_judged_recursively(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _array(model_root, "Poses", "Pose", 2)
        _impl_record(model_root, "PoseImpl", ("x", "uint16"), ("y", "uint16"))
        _impl_array(model_root, "PosesImpl", "PoseImpl", 2)
        _mapping_set(model_root, "Maps", ("Poses", "PosesImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert (
            "element of array 'Poses': record 'Pose' has 1 field and implementation record 'PoseImpl' 2 elements"
            in record.message
        )

    def test_a_member_two_levels_down_is_named_by_both_positions(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"), ("y", "Speed"))
        _record(model_root, "Outer", ("a", "Speed"), ("pose", "Pose"))
        _impl_record(model_root, "Inner", ("a", "uint8"))
        _impl_record(model_root, "PoseImpl", ("x", "uint16"), ("y", "Inner"))
        _impl_record(model_root, "OuterImpl", ("a", "uint16"), ("pose", "PoseImpl"))
        _mapping_set(model_root, "Maps", ("Outer", "OuterImpl"))

        record = _only(model_root, "mapping-set.incompatible-composite-map")
        assert "member 2 of record 'Outer': member 2 of record 'Pose': category" in record.message

    def test_a_record_onto_an_alias_ending_at_a_stub_is_not_judged(self, model_root: Path) -> None:
        """Unresolved, so not judged: the one such pair accepted."""
        _mapped_speed(model_root)
        _stub(model_root, "Foreign")
        _reference(model_root, "ForeignAlias", "Foreign")
        _record(model_root, "Pose", ("x", "Speed"))
        _mapping_set(model_root, "Maps", ("Pose", "ForeignAlias"))

        assert _rules(model_root) == []

    def test_a_scalar_map_is_still_not_judged(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _mapping_set(model_root, "Maps", ("Speed", "uint8"), ("Gear", "float32"))

        assert _rules(model_root) == []


class TestAPointerIsNeverMapped:
    def test_a_map_onto_a_pointer_is_refused_alone(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _pointer(model_root, "Ptr", "uint16")
        _mapping_set(model_root, "Maps", ("Speed", "Ptr"))

        record = _only(model_root, "mapping-set.pointer-mapped")
        assert "[constr_1066]" in record.message

    def test_a_map_onto_an_alias_of_a_pointer_is_refused(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _pointer(model_root, "Ptr", "uint16")
        _reference(model_root, "PtrAlias", "Ptr")
        _mapping_set(model_root, "Maps", ("Speed", "PtrAlias"))

        record = _only(model_root, "mapping-set.pointer-mapped")
        assert "'PtrAlias' -> 'Ptr'" in record.message

    def test_a_map_onto_a_structure_containing_a_pointer_is_refused(self, model_root: Path) -> None:
        """R22-11's extension of ``[constr_1066]`` into sub-elements."""
        _mapped_speed(model_root)
        _pointer(model_root, "Ptr", "uint16")
        _record(model_root, "Pose", ("x", "Speed"), ("p", "Speed"))
        _impl_record(model_root, "PoseImpl", ("x", "uint16"), ("p", "Ptr"))
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"))

        record = _only(model_root, "mapping-set.pointer-mapped")
        assert "'Ptr'" in record.message

    def test_a_pointer_not_mapped_is_legal(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint16")

        assert _rules(model_root) == []


class TestTheMayAndMustBeMappedSplit:
    """Records and arrays MAY be mapped and are never required to be."""

    def test_an_unmapped_record_on_a_port_is_not_required_to_be_mapped(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _typed_element(model_root, "Pose")
        _mapping_set(model_root, "Maps", ("Speed", "uint16"))
        for relative in (_PROVIDER, _CONSUMER):
            change(model_root, relative, lambda d: d.update(data_type_mappings=["Maps"]))

        assert _rules(model_root) == []

    def test_an_unmapped_application_type_is_still_required_to_be_mapped(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _typed_element(model_root, "Speed")

        assert set(_rules(model_root)) == {"data-type.application-type-unmapped"}

    def test_the_application_side_admits_a_record_and_an_array(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _record(model_root, "Pose", ("x", "Speed"))
        _array(model_root, "Speeds", "Speed", 2)
        _impl_record(model_root, "PoseImpl", ("x", "uint16"))
        _impl_array(model_root, "SpeedsImpl", "uint16", 2)
        _mapping_set(model_root, "Maps", ("Pose", "PoseImpl"), ("Speeds", "SpeedsImpl"))

        assert _rules(model_root) == []

    def test_the_application_side_still_refuses_an_implementation_kind(self, model_root: Path) -> None:
        _impl_record(model_root, "PoseImpl", ("x", "uint16"))
        _mapping_set(model_root, "Maps", ("PoseImpl", "uint16"))

        assert _rules(model_root) == ["mapping-set.unknown-application"]

    def test_the_implementation_side_still_refuses_an_application_kind(self, model_root: Path) -> None:
        _mapped_speed(model_root)
        _mapping_set(model_root, "Maps", ("Speed", "Heading"))

        assert _rules(model_root) == ["mapping-set.unknown-implementation"]


# [constr_1295]


class TestAPointerBetweenApplicationComponents:
    """Judged only where both ends are declared application components."""

    def test_a_pointer_typed_element_on_an_application_link_is_refused(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _typed_element(model_root, "Ptr")

        record = _only(model_root, "connector.pointer-between-application-components")
        assert "[constr_1295]" in record.message
        assert "'ExampleSWC'" in record.message and "'ExampleConsumerSWC'" in record.message
        assert record.subjects == (
            Subject("connectors", "Example_to_Consumer", (field_step("from"), field_step("port"))),
            Subject("connectors", "Example_to_Consumer", (field_step("to"), field_step("port"))),
        )

    def test_a_pointer_through_a_reference_chain_is_refused(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _reference(model_root, "PtrAlias", "Ptr")
        _typed_element(model_root, "PtrAlias")

        _only(model_root, "connector.pointer-between-application-components")

    def test_a_pointer_typed_argument_is_refused(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        change(
            model_root, _CS_INTERFACE, lambda d: d["operations"][0]["arguments"][0].update(implementation_type="Ptr")
        )
        for relative, direction in ((_PROVIDER, "provide"), (_CONSUMER, "require")):
            change(
                model_root,
                relative,
                lambda d, direction=direction: d["ports"].append(
                    {"name": "diag", "direction": direction, "interface": "IDiagnosticService"}
                ),
            )
        put(
            model_root,
            "connectors/Diag.yaml",
            {
                "name": "Diag",
                "from": {"swc": "ExampleSWC", "port": "diag"},
                "to": {"swc": "ExampleConsumerSWC", "port": "diag"},
            },
        )

        record = _only(model_root, "connector.pointer-between-application-components")
        assert "argument 'dtc_id'" in record.message

    @pytest.mark.parametrize("kind", ["service", "ecu_abstraction"])
    def test_a_service_or_ecu_abstraction_end_is_legal(self, model_root: Path, kind: str) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _typed_element(model_root, "Ptr")
        change(model_root, _PROVIDER, lambda d: d.update(type=kind))

        assert _rules(model_root) == []

    def test_a_pointer_inside_a_structure_is_not_judged(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _impl_record(model_root, "Frame", ("p", "Ptr"))
        _typed_element(model_root, "Frame")

        assert _rules(model_root) == []

    def test_an_external_component_stub_end_is_not_judged(self, model_root: Path) -> None:
        """A stub's port carries no interface, so no pointer can be read on its side of the link."""
        _pointer(model_root, "Ptr", "uint8")
        _typed_element(model_root, "Ptr")
        change(
            model_root,
            _PROVIDER,
            lambda d: d.update(
                external=True,
                external_ref="/Foreign/ComponentTypes/ExampleSWC",
                external_dest="APPLICATION-SW-COMPONENT-TYPE",
                ports=[{"name": "pOut", "direction": "provide"}],
                runnables=[],
                memory={},
            ),
        )

        assert "connector.pointer-between-application-components" not in _rules(model_root)

    def test_an_unconnected_port_is_not_judged(self, model_root: Path) -> None:
        _pointer(model_root, "Ptr", "uint8")
        _typed_element(model_root, "Ptr")
        (model_root / "connectors" / "Example_to_Consumer.yaml").unlink()

        assert _rules(model_root) == []


@pytest.fixture()
def compositions_root(tmp_path: Path) -> Path:
    destination = tmp_path / "model"
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, destination)
    assert check_cross_layer_consistency(str(destination)).consistent
    return destination


class TestAPointerFollowedThroughDelegations:
    def _pointer_signal(self, root: Path) -> None:
        _pointer(root, "Ptr", "uint8")
        change(root, "interfaces/ISignal.yaml", lambda d: d["data_elements"][0].update(implementation_type="Ptr"))

    def test_the_two_ends_are_found_through_delegations(self, compositions_root: Path) -> None:
        self._pointer_signal(compositions_root)

        record = _only(compositions_root, "connector.pointer-between-application-components")
        assert "'Beta'" in record.message and "'Consumer'" in record.message
        assert "root_link" in record.message

    def test_a_service_end_found_through_a_delegation_is_legal(self, compositions_root: Path) -> None:
        self._pointer_signal(compositions_root)
        change(compositions_root, "swc/core/Consumer.yaml", lambda d: d.update(type="service"))

        assert _rules(compositions_root) == []


# The signature check

_INTERFACE_FILE = "interfaces/IRecords.yaml"


@pytest.fixture()
def server_model(tmp_path: Path) -> Path:
    return mixed_event_model(tmp_path / "model")


def _two_operations(root: Path, first: str, second: str) -> None:
    for name, type_name in (("OpA", first), ("OpB", second)):
        change(
            root,
            _INTERFACE_FILE,
            lambda d, name=name, type_name=type_name: d["operations"].append(
                {
                    "name": name,
                    "arguments": [{"name": "a", "direction": "in", _type_field(root, type_name): type_name}],
                }
            ),
        )

    def mutate(document: dict[str, Any]) -> None:
        document["runnables"][0]["events"] = [
            TIMING_EVENT,
            {"type": "operation", "port": "pRecords", "operation": "OpA"},
            {"type": "operation", "port": "pRecords", "operation": "OpB"},
        ]

    change(root, SERVER_FILE, mutate)


def _signature_findings(root: Path) -> list[ConsistencyIssue]:
    return [r for r in _records(root) if r.rule == "swc.operation-event.incompatible-signatures"]


class TestSignaturesOverTheNewKinds:
    def test_an_implementation_composite_argument_is_compared_by_its_own_shape(self, server_model: Path) -> None:
        _impl_record(server_model, "FrameA", ("a", "uint8"))
        _impl_record(server_model, "FrameB", ("a", "uint8"), ("b", "uint8"))
        _two_operations(server_model, "FrameA", "FrameB")

        (finding,) = _signature_findings(server_model)
        assert "implementation record elements 1 vs 2" in finding.message

    def test_equal_implementation_composites_are_compatible(self, server_model: Path) -> None:
        _impl_record(server_model, "FrameA", ("a", "uint8"))
        _impl_record(server_model, "FrameB", ("a", "uint8"))
        _two_operations(server_model, "FrameA", "FrameB")

        assert _signature_findings(server_model) == []

    def test_a_pointer_argument_is_resolved(self, server_model: Path) -> None:
        _pointer(server_model, "PtrA", "uint8")
        _impl_record(server_model, "Frame", ("a", "uint8"))
        _pointer(server_model, "PtrB", "Frame")
        _two_operations(server_model, "PtrA", "PtrB")

        (finding,) = _signature_findings(server_model)
        assert "pointer target" in finding.message

    def test_a_mapped_application_record_resolves_through_its_map(self, server_model: Path) -> None:
        _application(server_model, "Speed")
        _record(server_model, "Pose", ("x", "Speed"))
        _impl_record(server_model, "PoseImpl", ("x", "uint8"))
        _impl_record(server_model, "Other", ("x", "uint8"), ("y", "uint8"))
        _mapping_set(server_model, "Maps", ("Pose", "PoseImpl"), ("Speed", "uint8"))
        change(server_model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(server_model, "Pose", "Other")

        (finding,) = _signature_findings(server_model)
        assert "'PoseImpl' vs 'Other'" in finding.message

    def test_an_unmapped_application_record_is_not_judged(self, server_model: Path) -> None:
        """An unmapped application composite argument stays not judged."""
        _application(server_model, "Speed")
        _record(server_model, "Pose", ("x", "Speed"))
        _impl_record(server_model, "Other", ("x", "uint8"), ("y", "uint8"))
        _mapping_set(server_model, "Maps", ("Speed", "uint8"))
        change(server_model, SERVER_FILE, lambda d: d.update(data_type_mappings=["Maps"]))
        _two_operations(server_model, "Pose", "Other")

        assert _signature_findings(server_model) == []


# Names


class TestTheNewNamesAreIdentifiers:
    def test_an_element_name_is_identifier_checked(self, model_root: Path) -> None:
        _impl_record(model_root, "Frame", ("my element", "uint8"))

        record = _only(model_root, "identifier.declared")
        assert "Implementation record 'Frame' element 'my element'" in record.message
        assert record.subjects == (
            _data_type("Frame", field_step("elements"), member_step("my element"), field_step("name")),
        )

    def test_an_array_element_name_is_identifier_checked(self, model_root: Path) -> None:
        _type(
            model_root,
            {
                "name": "Samples",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 2,
                "element_name": "my element",
            },
        )

        record = _only(model_root, "identifier.declared")
        assert "Implementation array 'Samples' element name 'my element'" in record.message
        assert record.subjects == (_data_type("Samples", field_step("element_name")),)

    def test_the_pre_materialization_guard_reads_both_names(self) -> None:
        staged = StagedModel(
            registry=reg.current(),
            elements={
                "implementation_types": [
                    {"name": "Frame", "kind": "implementation_record", "elements": [{"name": "my el", "type": "u"}]},
                    {
                        "name": "Samples",
                        "kind": "implementation_array",
                        "element_type": "u",
                        "length": 2,
                        "element_name": "my element",
                    },
                ]
            },
        )

        violations = emitted_identifier_violations(staged)

        assert {violation.error for violation in violations} == {INVALID_IDENTIFIER}
        messages = " ".join(violation.message for violation in violations)
        assert "my el" in messages and "my element" in messages


# Pass D

#: One break per new rule, over the ``read-swc-element`` baseline.
_BREAKS: dict[str, Any] = {
    "data-type.implementation-element-undeclared-type": lambda root: _impl_record(root, "Frame", ("a", "Nope")),
    "data-type.implementation-record-duplicate-element-name": lambda root: _impl_record(
        root, "Frame", ("a", "uint8"), ("a", "uint8")
    ),
    "data-type.implementation-array-undeclared-element-type": lambda root: _impl_array(root, "Samples", "Nope"),
    "data-type.pointer-undeclared-target": lambda root: _pointer(root, "Ptr", "Nope"),
    "mapping-set.incompatible-composite-map": lambda root: (
        _application(root, "Speed"),
        _record(root, "Pose", ("x", "Speed"), ("y", "Speed")),
        _impl_record(root, "PoseImpl", ("x", "uint16")),
        _mapping_set(root, "Maps", ("Pose", "PoseImpl")),
    ),
    "mapping-set.pointer-mapped": lambda root: (
        _application(root, "Speed"),
        _pointer(root, "Ptr", "uint16"),
        _mapping_set(root, "Maps", ("Speed", "Ptr")),
    ),
    "connector.pointer-between-application-components": lambda root: (
        _pointer(root, "Ptr", "uint8"),
        _typed_element(root, "Ptr"),
    ),
}


class TestPassD:
    def test_the_breaks_are_the_seven_new_rules_contract_8_keeps(self) -> None:
        assert set(_BREAKS) == set(NEW_RULE_CODES)
        assert set(NEW_RULE_CODES).isdisjoint(RETIRED_ROLE_RULES)

    @pytest.mark.parametrize("rule", [*RETIRED_ROLE_RULES, *RETIRED_OLDER_ROLE_RULES])
    def test_a_retired_role_rule_is_declared_nowhere(self, rule: str) -> None:
        assert rule not in RULE_CODES
        assert rule not in validator.CONSISTENCY_RULES

    def test_the_codes_are_the_protocols(self) -> None:
        assert {rule: RULE_CODES[rule].code for rule in NEW_RULE_CODES} == NEW_RULE_CODES

    @pytest.mark.parametrize("rule", sorted(NEW_RULE_CODES))
    def test_each_new_finding_reaches_the_protocol_under_its_code(self, model_root: Path, rule: str) -> None:
        _BREAKS[rule](model_root)

        reported = [record for record in _records(model_root) if record.rule == rule]
        assert reported, _rules(model_root)
        violations = diagnose(validator.ConsistencyResult(consistent=False, records=tuple(reported)))
        assert {violation.error for violation in violations} == {NEW_RULE_CODES[rule]}
