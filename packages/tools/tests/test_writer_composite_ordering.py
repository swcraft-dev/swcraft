"""The writer's composite-type dependency graph and topological sort.

`composite_dependencies()` and `topological_sort_data_types()` determine the emission
order of `_build_document()`'s composite pass, so that a record's/array's field/element
types are always emitted before the composite that references them.
"""

from __future__ import annotations

import pytest
from swcraft_tools.schema import (
    ArrayDataType,
    BaseDataType,
    EnumDataType,
    EnumLiteral,
    RecordDataType,
    RecordField,
    ValueDataType,
)
from swcraft_tools.writer.arxml_generation import composite_dependencies, topological_sort_data_types


def _enum(name: str) -> EnumDataType:
    return EnumDataType(name=name, kind="enum", literals=[EnumLiteral(name="A", value=0)])


def _record(name: str, *field_types: str) -> RecordDataType:
    return RecordDataType(
        name=name,
        kind="record",
        fields=[RecordField(name=f"f{i}", type=t) for i, t in enumerate(field_types)],
    )


def _array(name: str, element_type: str) -> ArrayDataType:
    return ArrayDataType(name=name, kind="array", element_type=element_type, length=4)


def _primitive(name: str) -> BaseDataType:
    return BaseDataType(name=name, kind="base_type", size=32, encoding="IEEE754")


def _sort_composites(types: list) -> list:
    """The composite pass's call: `_build_document()` sorts composites exactly so."""
    return topological_sort_data_types(types, composite_dependencies, "Composite")


class TestCompositeDependencies:
    def test_enum_has_no_dependencies(self) -> None:
        assert composite_dependencies(_enum("Mode")) == []

    def test_record_returns_field_types_in_order(self) -> None:
        rec = _record("Status", "float32", "Mode", "Counter")
        assert composite_dependencies(rec) == ["float32", "Mode", "Counter"]

    def test_array_returns_single_element_type(self) -> None:
        arr = _array("StatusArray", "Status")
        assert composite_dependencies(arr) == ["Status"]

    def test_primitive_has_no_dependencies(self) -> None:
        assert composite_dependencies(_primitive("float32")) == []

    def test_alias_has_no_dependencies(self) -> None:
        alias = ValueDataType(name="VehicleSpeed", kind="value_type", base="float32")
        assert composite_dependencies(alias) == []


class TestTopologicalSortOrder:
    def test_linear_chain(self) -> None:
        # A depends on B depends on C.
        c = _enum("C")
        b = _array("B", "C")
        a = _array("A", "B")
        sorted_ = _sort_composites([a, b, c])
        names = [dt.name for dt in sorted_]
        assert names.index("C") < names.index("B") < names.index("A")

    def test_diamond(self) -> None:
        # A depends on B and C; both B and C depend on D.
        d = _enum("D")
        b = _array("B", "D")
        c = _array("C", "D")
        a = _record("A", "B", "C")
        sorted_ = _sort_composites([a, b, c, d])
        names = [dt.name for dt in sorted_]
        assert names.index("D") < names.index("B")
        assert names.index("D") < names.index("C")
        assert names.index("B") < names.index("A")
        assert names.index("C") < names.index("A")


class TestTopologicalSortMembership:
    def test_empty_input(self) -> None:
        assert _sort_composites([]) == []

    def test_flat_list_no_inter_composite_deps(self) -> None:
        composites = [_enum("A"), _enum("B"), _enum("C")]
        sorted_ = _sort_composites(composites)
        assert {dt.name for dt in sorted_} == {"A", "B", "C"}
        assert len(sorted_) == 3

    def test_dependency_on_primitive_is_not_a_graph_edge(self) -> None:
        # A name outside the composites batch is no ordering constraint and no cycle.
        rec = _record("Status", "float32")
        sorted_ = _sort_composites([rec])
        assert [dt.name for dt in sorted_] == ["Status"]


class TestTopologicalSortCycles:
    def test_self_cycle_raises(self) -> None:
        rec = _record("Self", "Self")
        with pytest.raises(ValueError, match="Composite data type dependency cycle") as exc_info:
            _sort_composites([rec])
        assert "Self" in str(exc_info.value)

    def test_two_node_indirect_cycle_raises(self) -> None:
        a = _array("A", "B")
        b = _array("B", "A")
        with pytest.raises(ValueError, match="Composite data type dependency cycle") as exc_info:
            _sort_composites([a, b])
        message = str(exc_info.value)
        assert "A" in message
        assert "B" in message

    def test_three_node_indirect_cycle_raises(self) -> None:
        a = _array("A", "B")
        b = _array("B", "C")
        c = _array("C", "A")
        with pytest.raises(ValueError, match="Composite data type dependency cycle") as exc_info:
            _sort_composites([a, b, c])
        message = str(exc_info.value)
        assert "A" in message
        assert "B" in message
        assert "C" in message
