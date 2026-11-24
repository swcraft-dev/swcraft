"""The computed values a read may ask for: ``validator.DERIVED`` and the functions that own them.

Each name is computed by its one owner -- e.g. ``connection_ends`` by
:func:`validator.component_ends`, ``base_type`` by
:func:`validator.data_element_base_type` over ``resolve_type`` and
``application_type_pairings`` -- and :func:`validator.derive_rows` applies them to
identity rows built from a :class:`ModelState` in memory.

The pointer-crossing rule, ``component_ends``' other caller, keeps its own suites
(``test_impl_composites_validator.py``, ``test_validator_compositions.py``); nothing
here restates them.
"""

from __future__ import annotations

import builtins
import json
import os
import pathlib
import shutil
from collections.abc import Iterable, Mapping
from typing import Any

import pytest
import validator_package
import yaml
from benchmark_question_support import trace_agreement
from conftest import AGENT_TASK_BENCH, REPO_ROOT
from pydantic import TypeAdapter
from swcraft_tools import validator
from swcraft_tools.data_description import DataDescription
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.query import QueryError
from swcraft_tools.query.slicing import sliced_path
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import (
    Composition,
    Connector,
    DataType,
    Hardware,
    Interface,
    MappingSet,
    Requirement,
    SWComponent,
    Task,
)
from swcraft_tools.trace import StaleLink, stale_link

_INTERFACE = TypeAdapter(Interface)
#: The requirements layer's three computed values, in the vocabulary's order.
REQUIREMENT_VALUES = ("realised_by", "allocation", "stale_links")
SCHEMA_PATH = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol" / "protocol.schema.json"
)
_HARDWARE = TypeAdapter(Hardware)
_DATA_TYPE = TypeAdapter(DataType)


# ---------------------------------------------------------------------------
# Builders: every element in memory, no directory anywhere
# ---------------------------------------------------------------------------


def _swc(name: str, ports: Iterable[tuple[str, str, str | None]] = (), **extra: Any) -> SWComponent:
    """A component with *ports* given as ``(name, direction, interface)``."""
    return SWComponent.model_validate(
        {
            "name": name,
            "ports": [
                {"name": port, "direction": direction, **({} if interface is None else {"interface": interface})}
                for port, direction, interface in ports
            ],
            **extra,
        }
    )


def _composition(
    name: str, members: Iterable[tuple[str, str, str]], delegations: Iterable[tuple[str, str, str]]
) -> Composition:
    """A composition: *members* ``(name, "type"/"composition", target)``, *delegations* ``(port, member, port)``."""
    return Composition.model_validate(
        {
            "name": name,
            "title": name,
            "members": [{"name": member, role: target} for member, role, target in members],
            "delegations": [
                {"name": f"{port}_{member}_{member_port}", "port": port, "member": member, "member_port": member_port}
                for port, member, member_port in delegations
            ],
        }
    )


def _connector(name: str, source: Mapping[str, str], target: Mapping[str, str]) -> Connector:
    return Connector.model_validate({"name": name, "from": dict(source), "to": dict(target)})


def _sr(name: str, elements: Mapping[str, Mapping[str, str]]) -> Any:
    """A sender-receiver interface; each element maps its name to its one type key."""
    return _INTERFACE.validate_python(
        {
            "name": name,
            "kind": "sender_receiver",
            "data_elements": [{"name": element, **types} for element, types in elements.items()],
        }
    )


def _set(name: str, maps: Mapping[str, str]) -> MappingSet:
    return MappingSet.model_validate(
        {"name": name, "maps": [{"application": app, "implementation": impl} for app, impl in maps.items()]}
    )


def _type(**fields: Any) -> Any:
    return _DATA_TYPE.validate_python(fields)


#: The data types every ``base_type`` case resolves against.
_TYPES = (
    _type(name="uint16", kind="base_type", size=16, encoding="NONE"),
    _type(name="Speed_T", kind="value_type", base="uint16"),
    _type(name="SpeedRef_T", kind="reference_type", base="Speed_T"),
    _type(name="Record_T", kind="implementation_record", elements=[{"name": "a", "type": "Speed_T"}]),
    _type(name="Array_T", kind="implementation_array", element_type="Speed_T", length=4, element_name="item"),
    _type(name="Pointer_T", kind="pointer", target="Speed_T"),
    _type(name="Foreign_T", kind="external_type"),
    _type(name="LoopA_T", kind="reference_type", base="LoopB_T"),
    _type(name="LoopB_T", kind="reference_type", base="LoopA_T"),
)


def _state(**layers: list[Any]) -> ModelState:
    return ModelState(layers=layers)


# ---------------------------------------------------------------------------
# component_ends: the owner of connection_ends
# ---------------------------------------------------------------------------


class TestComponentEnds:
    """The connector ends through delegations, read by the pointer rule and by ``connection_ends``."""

    def _ends(self, endpoint: Mapping[str, str], swcs: list[SWComponent], compositions: list[Composition]) -> list:
        connector = _connector("C", endpoint, endpoint)
        ends = validator.component_ends(
            connector.from_, {swc.name: swc for swc in swcs}, {c.name: c for c in compositions}
        )
        return [(swc.name, port.name) for swc, port in ends]

    def test_the_private_name_is_gone(self) -> None:
        assert not validator_package.defines("_component_ends")
        assert not validator_package.binding_modules("_component_ends")

    def test_an_atomic_endpoint_is_itself(self) -> None:
        swcs = [_swc("A", [("Out", "provide", "I")])]
        assert self._ends({"swc": "A", "port": "Out"}, swcs, []) == [("A", "Out")]

    def test_one_and_two_delegation_levels(self) -> None:
        swcs = [_swc("A", [("Out", "provide", "I")]), _swc("B", [("Out", "provide", "I")])]
        inner = _composition("Inner", [("b", "type", "B")], [("P", "b", "Out")])
        outer = _composition("Outer", [("a", "type", "A"), ("i", "composition", "Inner")], [("P", "i", "P")])
        assert self._ends({"composition": "Inner", "port": "P"}, swcs, [inner, outer]) == [("B", "Out")]
        assert self._ends({"composition": "Outer", "port": "P"}, swcs, [inner, outer]) == [("B", "Out")]

    def test_a_fan_out_reaches_every_end_in_declaration_order(self) -> None:
        swcs = [_swc("A", [("Out", "provide", "I")]), _swc("B", [("Out", "provide", "I")])]
        group = _composition("G", [("a", "type", "A"), ("b", "type", "B")], [("P", "b", "Out"), ("P", "a", "Out")])
        assert self._ends({"composition": "G", "port": "P"}, swcs, [group]) == [("B", "Out"), ("A", "Out")]

    def test_a_stub_end_is_an_end(self) -> None:
        stub = _swc("Stub", [("Out", "provide", "I")], external=True, external_ref="/Elsewhere/Stub")
        group = _composition("G", [("s", "type", "Stub")], [("P", "s", "Out")])
        assert self._ends({"composition": "G", "port": "P"}, [stub], [group]) == [("Stub", "Out")]

    def test_what_does_not_resolve_reaches_nothing(self) -> None:
        swcs = [_swc("A", [("Out", "provide", "I")])]
        group = _composition(
            "G", [("a", "type", "A"), ("x", "type", "Nowhere")], [("P", "x", "Out"), ("P", "y", "Out")]
        )
        assert self._ends({"swc": "A", "port": "Undeclared"}, swcs, [group]) == []
        assert self._ends({"swc": "Nowhere", "port": "Out"}, swcs, [group]) == []
        assert self._ends({"composition": "Nowhere", "port": "P"}, swcs, [group]) == []
        assert self._ends({"composition": "G", "port": "P"}, swcs, [group]) == []

    def test_a_cycle_stops_only_its_own_branch(self) -> None:
        """A composition that delegates back into itself keeps the exit its sibling delegation reaches."""
        swcs = [_swc("A", [("Out", "provide", "I")])]
        loop = _composition(
            "Loop", [("self", "composition", "Loop"), ("a", "type", "A")], [("P", "self", "P"), ("P", "a", "Out")]
        )
        assert self._ends({"composition": "Loop", "port": "P"}, swcs, [loop]) == [("A", "Out")]

    def test_a_missing_composition_boundary_port_is_not_checked(self) -> None:
        """A stated limit: the delegation is followed whether or not the composition declares ``P``.

        The composition rules own that finding.
        """
        swcs = [_swc("A", [("Out", "provide", "I")])]
        group = _composition("G", [("a", "type", "A")], [("P", "a", "Out")])
        assert not group.ports
        assert self._ends({"composition": "G", "port": "P"}, swcs, [group]) == [("A", "Out")]


# ---------------------------------------------------------------------------
# data_element_base_type: the owner of base_type
# ---------------------------------------------------------------------------


_BOTH_ABSENT = {"implementation": None, "base": None}


class TestDataElementBaseType:
    """What a port's data element resolves to, abstaining wherever another rule owns the cause."""

    interfaces: Mapping[str, Any] = {
        interface.name: interface
        for interface in (
            _sr(
                "ISpeed",
                {
                    "ByImpl": {"implementation_type": "Speed_T"},
                    "ByRef": {"implementation_type": "SpeedRef_T"},
                    "ByApp": {"application_type": "Speed_A"},
                    "ByRecord": {"implementation_type": "Record_T"},
                    "ByArray": {"implementation_type": "Array_T"},
                    "ByPointer": {"implementation_type": "Pointer_T"},
                    "ByForeign": {"implementation_type": "Foreign_T"},
                    "ByUndeclared": {"implementation_type": "Nowhere_T"},
                    "ByCycle": {"implementation_type": "LoopA_T"},
                },
            ),
            _INTERFACE.validate_python(
                {
                    "name": "ICall",
                    "kind": "client_server",
                    "operations": [{"name": "ByImpl", "arguments": []}],
                }
            ),
            _INTERFACE.validate_python(
                {
                    "name": "IStub",
                    "kind": "external_interface",
                    "external_ref": "/X/IStub",
                    "external_dest": "SENDER-RECEIVER-INTERFACE",
                    "data_elements": ["ByImpl"],
                }
            ),
        )
    }

    def _answer(
        self,
        element: str,
        *,
        port: str = "Out",
        sets: tuple[MappingSet, ...] = (),
        mappings: tuple[str, ...] = (),
        interface: str | None = "ISpeed",
    ) -> dict[str, str | None]:
        swc = _swc("A", [("Out", "provide", interface)], data_type_mappings=list(mappings))
        return validator.data_element_base_type(
            swc,
            port,
            element,
            interfaces=self.interfaces,
            mapping_sets=sets,
            data_types=DataDescription(_TYPES),
        )

    def test_through_an_implementation_type(self) -> None:
        assert self._answer("ByImpl") == {"implementation": "Speed_T", "base": "uint16"}

    def test_through_a_reference_type_to_a_value_type(self) -> None:
        assert self._answer("ByRef") == {"implementation": "SpeedRef_T", "base": "uint16"}

    def test_through_an_application_type_and_its_one_pairing(self) -> None:
        sets = (_set("M1", {"Speed_A": "Speed_T"}), _set("M2", {"Speed_A": "Speed_T", "Other_A": "Record_T"}))
        assert self._answer("ByApp", sets=sets, mappings=("M1", "M2")) == {
            "implementation": "Speed_T",
            "base": "uint16",
        }

    @pytest.mark.parametrize(
        ("element", "port", "interface"),
        [
            pytest.param("ByImpl", "Undeclared", "ISpeed", id="undeclared-port"),
            pytest.param("ByImpl", "Out", None, id="port-names-no-interface"),
            pytest.param("ByImpl", "Out", "INowhere", id="undeclared-interface"),
            pytest.param("Undeclared", "Out", "ISpeed", id="undeclared-element"),
            pytest.param("ByImpl", "Out", "ICall", id="client-server-interface"),
            pytest.param("ByImpl", "Out", "IStub", id="external-interface-stub"),
        ],
    )
    def test_no_element_or_type_content_abstains_on_both(self, element: str, port: str, interface: str | None) -> None:
        assert self._answer(element, port=port, interface=interface) == _BOTH_ABSENT

    def test_an_application_type_no_named_set_pairs_abstains(self) -> None:
        assert self._answer("ByApp", sets=(_set("M1", {"Other_A": "Speed_T"}),), mappings=("M1",)) == _BOTH_ABSENT
        assert self._answer("ByApp", sets=(_set("M1", {"Speed_A": "Speed_T"}),), mappings=()) == _BOTH_ABSENT
        assert self._answer("ByApp", mappings=("Undeclared",)) == _BOTH_ABSENT

    def test_two_pairings_abstain(self) -> None:
        sets = (_set("M1", {"Speed_A": "Speed_T"}), _set("M2", {"Speed_A": "SpeedRef_T"}))
        assert self._answer("ByApp", sets=sets, mappings=("M1", "M2")) == _BOTH_ABSENT

    def test_a_duplicated_set_abstains_for_every_element_of_the_component(self) -> None:
        """One set name in two files with conflicting maps: the double-mapping rule's skip, element-blind."""
        sets = (_set("M1", {"Speed_A": "Speed_T"}), _set("M1", {"Speed_A": "SpeedRef_T"}))
        assert self._answer("ByApp", sets=sets, mappings=("M1",)) == _BOTH_ABSENT
        assert self._answer("ByImpl", sets=sets, mappings=("M1",)) == _BOTH_ABSENT
        # A component naming no duplicated set is untouched by the duplicate.
        assert self._answer("ByImpl", sets=sets, mappings=()) == {"implementation": "Speed_T", "base": "uint16"}

    @pytest.mark.parametrize(
        ("element", "implementation"),
        [
            ("ByRecord", "Record_T"),
            ("ByArray", "Array_T"),
            ("ByPointer", "Pointer_T"),
            ("ByForeign", "Foreign_T"),
            ("ByUndeclared", "Nowhere_T"),
            ("ByCycle", "LoopA_T"),
        ],
    )
    def test_a_known_implementation_without_a_base_keeps_its_name(self, element: str, implementation: str) -> None:
        assert self._answer(element) == {"implementation": implementation, "base": None}


# ---------------------------------------------------------------------------
# derive_rows: the rows, multiplied, from a state in memory
# ---------------------------------------------------------------------------


def _pilot_state() -> ModelState:
    """A nested composition with a delegation fan-out, a stub, a mapped application type and a value type."""
    swcs = [
        _swc("Writer", [("Out", "provide", "ISpeed")], data_type_mappings=["M"]),
        _swc("ReaderA", [("In", "require", "ISpeed")]),
        _swc("ReaderB", [("In", "require", "ISpeed")]),
        _swc("Stub", [("In", "require", "ISpeed")], external=True, external_ref="/X/Stub"),
    ]
    inner = _composition("Inner", [("b", "type", "ReaderB")], [("P", "b", "In")])
    group = _composition(
        "Group",
        [("a", "type", "ReaderA"), ("i", "composition", "Inner"), ("s", "type", "Stub")],
        [("P", "a", "In"), ("P", "i", "P"), ("P", "s", "In")],
    )
    connectors = [
        _connector("ToGroup", {"swc": "Writer", "port": "Out"}, {"composition": "Group", "port": "P"}),
        _connector("Dangling", {"swc": "Writer", "port": "Out"}, {"swc": "Nowhere", "port": "In"}),
    ]
    interfaces = [
        _sr("ISpeed", {"Raw": {"implementation_type": "Speed_T"}, "Physical": {"application_type": "Speed_A"}})
    ]
    return _state(
        swc=swcs,
        compositions=[inner, group],
        connectors=connectors,
        interfaces=interfaces,
        mapping_sets=[_set("M", {"Speed_A": "SpeedRef_T"})],
        implementation_types=list(_TYPES[1:]),
        base_types=[_TYPES[0]],
    )


def _ends(provider: tuple[str, str], requester: tuple[str, str]) -> dict[str, dict[str, str]]:
    return {
        "provider": {"swc": provider[0], "port": provider[1]},
        "requester": {"swc": requester[0], "port": requester[1]},
    }


class TestDeriveRows:
    """``derive_rows``' multiplicity, its row shape, and that it reads no file."""

    def test_connection_ends_yields_one_row_per_value(self) -> None:
        """In ``component_ends``' order: breadth first, so the nested reader comes after the stub."""
        rows = validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], _pilot_state())
        assert rows == [
            {"element": "ToGroup", "derived": {"connection_ends": _ends(("Writer", "Out"), (requester, "In"))}}
            for requester in ("ReaderA", "Stub", "ReaderB")
        ]

    def test_a_connector_with_no_end_yields_no_row(self) -> None:
        state = _pilot_state()
        assert validator.derive_rows([{"element": "Dangling"}], ["connection_ends"], state) == []
        assert validator.derive_rows([{"element": "Undeclared"}], ["connection_ends"], state) == []

    def test_base_type_yields_exactly_one_row_per_row(self) -> None:
        rows = [
            {"element": "Writer", "through": ["Run"], "member": {"port": "Out", "element": "Raw"}},
            {"element": "Writer", "through": ["Run"], "member": {"port": "Out", "element": "Physical"}},
            {"element": "ReaderA", "through": ["Run"], "member": {"port": "In", "element": "Physical"}},
            {"element": "Undeclared", "through": ["Run"], "member": {"port": "In", "element": "Raw"}},
        ]
        derived = validator.derive_rows(rows, ["base_type"], _pilot_state())
        assert [row["derived"] for row in derived] == [
            {"base_type": {"implementation": "Speed_T", "base": "uint16"}},
            {"base_type": {"implementation": "SpeedRef_T", "base": "uint16"}},
            # ReaderA names no mapping set: the application type pairs with nothing.
            {"base_type": _BOTH_ABSENT},
            {"base_type": _BOTH_ABSENT},
        ]

    def test_every_input_key_is_kept_in_order_and_derived_comes_last(self) -> None:
        row = {"element": "Writer", "through": ["Run"], "member": {"port": "Out", "element": "Raw"}}
        [derived] = validator.derive_rows([row], ["base_type"], _pilot_state())
        assert list(derived) == ["element", "through", "member", "derived"]
        assert row == {"element": "Writer", "through": ["Run"], "member": {"port": "Out", "element": "Raw"}}

    def test_a_name_applied_twice_by_the_caller_is_one_key(self) -> None:
        [derived] = validator.derive_rows(
            [{"element": "Writer", "member": {"port": "Out", "element": "Raw"}}],
            ["base_type", "base_type"],
            _pilot_state(),
        )
        assert list(derived["derived"]) == ["base_type"]

    def test_a_set_valued_name_applied_twice_is_applied_once(self) -> None:
        """Not its values squared: a repeated name is one name, set-valued or not."""
        state = _pilot_state()
        once = validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], state)
        assert validator.derive_rows([{"element": "ToGroup"}], ["connection_ends", "connection_ends"], state) == once
        assert len(once) == 3

    @pytest.mark.parametrize(
        ("row", "error"),
        [
            pytest.param({"element": "Writer"}, KeyError, id="no-member"),
            pytest.param({"member": {"port": "Out", "element": "Raw"}}, KeyError, id="no-element"),
            pytest.param({"element": "Writer", "member": ["Out", "Raw"]}, TypeError, id="member-not-a-mapping"),
        ],
    )
    def test_a_row_of_another_shape_is_a_caller_defect(self, row: dict[str, object], error: type[Exception]) -> None:
        with pytest.raises(error):
            validator.derive_rows([row], ["base_type"], _pilot_state())

    def test_a_component_declared_twice_resolves_to_its_last_declaration(self) -> None:
        state = _pilot_state()
        swcs = [*state.layers["swc"], _swc("Writer", [("Out", "provide", "ISpeed")])]
        twice = ModelState(layers={**state.layers, "swc": swcs})
        row = {"element": "Writer", "member": {"port": "Out", "element": "Physical"}}
        # The first Writer names the set `M`; the last names none, so the application type pairs with nothing.
        assert validator.derive_rows([row], ["base_type"], state)[0]["derived"] == {
            "base_type": {"implementation": "SpeedRef_T", "base": "uint16"}
        }
        assert validator.derive_rows([row], ["base_type"], twice)[0]["derived"] == {"base_type": _BOTH_ABSENT}

    def test_a_connector_declared_twice_resolves_to_its_first_declaration(self) -> None:
        state = _pilot_state()
        later = _connector("ToGroup", {"swc": "Writer", "port": "Out"}, {"swc": "ReaderA", "port": "In"})
        twice = ModelState(layers={**state.layers, "connectors": [*state.layers["connectors"], later]})
        assert validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], twice) == (
            validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], state)
        )

    def test_a_name_outside_the_vocabulary_is_a_caller_defect(self) -> None:
        with pytest.raises(ValueError, match="'no_such_value'"):
            validator.derive_rows([{"element": "ToGroup"}], ["no_such_value"], _pilot_state())

    def test_an_empty_state_answers_without_a_layer(self) -> None:
        assert validator.derive_rows([{"element": "C"}], ["connection_ends"], _state()) == []

    def test_it_reads_no_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _pilot_state()

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("derive_rows touched the file system")

        for owner, name in (
            (builtins, "open"),
            (os, "scandir"),
            (os, "listdir"),
            (pathlib.Path, "open"),
            (pathlib.Path, "iterdir"),
            (pathlib.Path, "read_text"),
        ):
            monkeypatch.setattr(owner, name, refuse)
        rows = validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], state)
        rows += validator.derive_rows(
            [{"element": "Writer", "member": {"port": "Out", "element": "Raw"}}], ["base_type"], state
        )
        assert len(rows) == 4


# ---------------------------------------------------------------------------
# effective_level: the published name-keyed effective level, composed
# ---------------------------------------------------------------------------


def _requirement(req_id: str, asil: str | None = None) -> Requirement:
    """A requirement; the components name it in ``satisfies``."""
    return Requirement.model_validate(
        {
            "id": req_id,
            "title": req_id,
            "description": f"The system shall satisfy {req_id}.",
            "version": 1,
            "source": "architecture",
            **({} if asil is None else {"asil": asil}),
        }
    )


def _level_state() -> ModelState:
    """One component per way a level is answered, and two allocations to compare."""
    return _state(
        swc=[
            _swc("Unrated"),
            _swc("Declared", asil="B", satisfies=["REQ_1@1"]),
            _swc("Allocated", satisfies=["REQ_1@1", "REQ_2@1"]),
            _swc("Decomposed", satisfies=["REQ_3@1"]),
            _swc("Unlevelled", satisfies=["REQ_4@1"]),
        ],
        requirements=[
            _requirement("REQ_1", asil="D"),
            _requirement("REQ_2", asil="A"),
            _requirement("REQ_3", asil="B(D)"),
            _requirement("REQ_4"),
        ],
    )


def _levels(state: ModelState, *names: str) -> list[str]:
    rows = validator.derive_rows([{"element": name} for name in names], ["effective_level"], state)
    return [row["derived"]["effective_level"]["level"] for row in rows]


class TestEffectiveLevel:
    """``effective_level`` is :func:`validator.effective_asil_levels`' value at the row, and no second formula."""

    def test_qm_where_nothing_allocates_a_level(self) -> None:
        assert _levels(_level_state(), "Unrated", "Unlevelled") == ["QM", "QM"]

    def test_a_declared_level_is_the_answer_over_a_higher_allocation(self) -> None:
        assert _levels(_level_state(), "Declared") == ["B"]

    def test_an_undeclared_level_is_the_highest_allocated(self) -> None:
        assert _levels(_level_state(), "Allocated", "Decomposed") == ["D", "B(D)"]

    def test_the_value_is_effective_asil_levels_for_every_component(self) -> None:
        state = _level_state()
        swcs = state.layers["swc"]
        expected = validator.effective_asil_levels(swcs, state.layers["requirements"])
        names = [swc.name for swc in swcs]
        assert dict(zip(names, _levels(state, *names), strict=True)) == expected
        assert set(expected.values()) == {"QM", "B", "D", "B(D)"}

    def test_the_rating_is_the_plain_level_beside_the_decomposed_one(self) -> None:
        """``rating`` is ``B`` for ``B(D)`` and for ``B``, ``QM`` for ``QM``.

        So one ``equals`` clause counts a level together with its decompositions.
        """
        rows = validator.derive_rows(
            [{"element": name} for name in ("Decomposed", "Declared", "Unrated")], ["effective_level"], _level_state()
        )
        assert [row["derived"]["effective_level"] for row in rows] == [
            {"level": "B(D)", "rating": "B"},
            {"level": "B", "rating": "B"},
            {"level": "QM", "rating": "QM"},
        ]

    def test_the_value_is_composed_never_recomputed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def answered(swcs: Any, requirements: Any) -> dict[str, str]:
            calls.append(len(swcs))
            return {swc.name: "C" for swc in swcs}

        validator_package.patch_everywhere(monkeypatch, "effective_asil_levels", answered)
        assert _levels(_level_state(), "Unrated", "Declared", "Allocated") == ["C", "C", "C"]
        assert calls == [5], "built once per call, over the whole swc layer"

    def test_a_component_declared_twice_resolves_to_its_first_declaration(self) -> None:
        state = _level_state()
        twice = ModelState(layers={**state.layers, "swc": [*state.layers["swc"], _swc("Unrated", asil="D")]})
        assert _levels(twice, "Unrated") == ["QM"]
        first = ModelState(layers={**state.layers, "swc": [_swc("Unrated", asil="D"), *state.layers["swc"]]})
        assert _levels(first, "Unrated") == ["D"]

    def test_no_requirements_layer_is_qm_for_every_component(self) -> None:
        assert _levels(_state(swc=[_swc("Alone", asil="A"), _swc("Bare")]), "Alone", "Bare") == ["A", "QM"]

    def test_a_row_naming_no_declared_component_is_a_caller_defect(self) -> None:
        with pytest.raises(KeyError):
            _levels(_level_state(), "Undeclared")

    @pytest.mark.parametrize(
        ("layer", "members"),
        [
            pytest.param("connectors", None, id="connectors"),
            pytest.param("requirements", None, id="requirements"),
            pytest.param("swc", "runnables.reads", id="swc-member-path"),
            pytest.param("swc", "ports", id="swc-ports"),
        ],
    )
    def test_it_is_refused_anywhere_but_the_components(self, layer: str, members: str | None) -> None:
        from swcraft_tools import query

        refusal = query.derived_refusal(validator.DERIVED, ["effective_level"], layer, members)
        assert refusal is not None
        assert refusal.kind == "not-applicable"
        assert "the elements of layer 'swc'" in refusal.message
        assert query.derived_refusal(validator.DERIVED, ["effective_level"], "swc", None) is None


# ---------------------------------------------------------------------------
# partitions and crossing_ends: what a partition runs, read once
# ---------------------------------------------------------------------------


def _core(name: str) -> Any:
    return _HARDWARE.validate_python({"name": name, "kind": "core", "threshold": 0.5})


def _partition(name: str, core: str) -> Any:
    return _HARDWARE.validate_python({"name": name, "kind": "partition", "core": core})


def _task(name: str, partition: str, entries: Iterable[Mapping[str, str]]) -> Task:
    return Task.model_validate(
        {"name": name, "period": 0.01, "priority": 1, "partition": partition, "runnables": list(entries)}
    )


def _runs(*names: str) -> list[dict[str, str]]:
    return [{"swc": name, "runnable": "Run"} for name in names]


def _placed(name: str, direction: str) -> SWComponent:
    """A component with one port ``Out``/``In`` on ``ISpeed`` and one runnable ``Run``."""
    port = "Out" if direction == "provide" else "In"
    return _swc(name, [(port, direction, "ISpeed")], runnables=[{"name": "Run"}])


def _partition_state() -> ModelState:
    """Two cores, three partitions (one on an undeclared core, one name declared twice) and their tasks.

    ``Sensor`` and ``Mate`` run in ``P_A``, ``Actuator`` in ``P_B``, ``Twin`` in
    both, ``Lonely`` in ``P_Lost`` (whose core names nothing), and ``Idle`` in
    none -- a task names its undeclared runnable, which runs nothing. The second
    ``P_A`` file, on ``Core1``, is never the resolution of its name.
    """
    return _state(
        swc=[
            _placed("Sensor", "provide"),
            _placed("Mate", "require"),
            _placed("Actuator", "require"),
            _placed("Twin", "require"),
            _placed("Lonely", "require"),
            _placed("Idle", "require"),
        ],
        hardware=[
            _core("Core0"),
            _core("Core1"),
            _partition("P_B", "Core1"),
            _partition("P_A", "Core0"),
            _partition("P_Lost", "NoCore"),
            _partition("P_A", "Core1"),
        ],
        tasks=[
            _task("T_A", "P_A", _runs("Sensor", "Mate", "Twin")),
            _task("T_B", "P_B", [*_runs("Actuator", "Twin"), {"swc": "Idle", "runnable": "Missing"}]),
            _task("T_Lost", "P_Lost", _runs("Lonely")),
        ],
        compositions=[_composition("Group", [("a", "type", "Actuator")], [("P", "a", "In")])],
        connectors=[
            _connector("Crossing", {"swc": "Sensor", "port": "Out"}, {"swc": "Actuator", "port": "In"}),
            _connector("Same", {"swc": "Sensor", "port": "Out"}, {"swc": "Mate", "port": "In"}),
            _connector("Delegated", {"swc": "Sensor", "port": "Out"}, {"composition": "Group", "port": "P"}),
            _connector("ToIdle", {"swc": "Sensor", "port": "Out"}, {"swc": "Idle", "port": "In"}),
            _connector("ToTwin", {"swc": "Sensor", "port": "Out"}, {"swc": "Twin", "port": "In"}),
        ],
    )


def _partitions_of(state: ModelState, name: str) -> list[dict[str, str | None]]:
    rows = validator.derive_rows([{"element": name}], ["partitions"], state)
    assert all(row["element"] == name for row in rows)
    return [row["derived"]["partitions"] for row in rows]


def _crossing(connector: str, state: ModelState | None = None) -> list[dict[str, dict[str, str]]]:
    rows = validator.derive_rows([{"element": connector}], ["crossing_ends"], state or _partition_state())
    return [row["derived"]["crossing_ends"] for row in rows]


def _crossing_end(provider: str, provided_in: str, requester: str, required_in: str) -> dict[str, dict[str, str]]:
    return {
        "provider": {"swc": provider, "port": "Out", "partition": provided_in},
        "requester": {"swc": requester, "port": "In", "partition": required_in},
    }


class TestPartitions:
    """``partitions`` is :func:`validator.partition_runs`' reading, one row per partition."""

    def test_one_partition_is_one_row(self) -> None:
        assert _partitions_of(_partition_state(), "Sensor") == [{"partition": "P_A", "core": "Core0"}]

    def test_two_partitions_are_two_rows_in_partition_name_order(self) -> None:
        assert _partitions_of(_partition_state(), "Twin") == [
            {"partition": "P_A", "core": "Core0"},
            {"partition": "P_B", "core": "Core1"},
        ]

    def test_a_component_no_partition_runs_has_no_row(self) -> None:
        """``Idle``'s one task entry names a runnable it does not declare, which runs nothing."""
        state = _partition_state()
        assert _partitions_of(state, "Idle") == []
        assert _partitions_of(state, "Undeclared") == []

    def test_a_task_in_an_undeclared_partition_places_nothing(self) -> None:
        state = _partition_state()
        nowhere = ModelState(
            layers={**state.layers, "tasks": [*state.layers["tasks"], _task("T_Nowhere", "P_Nowhere", _runs("Idle"))]}
        )
        assert _partitions_of(nowhere, "Idle") == []

    def test_a_declared_partition_no_task_names_is_no_value(self) -> None:
        state = _partition_state()
        empty = ModelState(
            layers={**state.layers, "hardware": [*state.layers["hardware"], _partition("P_Empty", "Core0")]}
        )
        for swc in ("Sensor", "Mate", "Actuator", "Twin", "Lonely", "Idle"):
            assert all(value["partition"] != "P_Empty" for value in _partitions_of(empty, swc))

    def test_a_core_that_does_not_resolve_is_null(self) -> None:
        assert _partitions_of(_partition_state(), "Lonely") == [{"partition": "P_Lost", "core": None}]

    def test_a_partition_name_declared_twice_resolves_to_its_first_declaration(self) -> None:
        """The second ``P_A`` (on ``Core1``) is no value: one row, on the first file's core."""
        assert _partitions_of(_partition_state(), "Mate") == [{"partition": "P_A", "core": "Core0"}]

    def test_a_partition_name_resolving_to_another_kind_runs_nothing(self) -> None:
        state = _partition_state()
        shadowed = ModelState(layers={**state.layers, "hardware": [_core("P_B"), *state.layers["hardware"]]})
        assert _partitions_of(shadowed, "Actuator") == []
        assert _partitions_of(shadowed, "Twin") == [{"partition": "P_A", "core": "Core0"}]

    def test_an_instance_entry_counts_as_its_type(self) -> None:
        state = _partition_state()
        by_instance = _task("T_C", "P_C", [{"instance": "a", "runnable": "Run"}])
        [group] = state.layers["compositions"]
        placed = ModelState(
            layers={
                **state.layers,
                "compositions": [group.model_copy(update={"root": True})],
                "hardware": [*state.layers["hardware"], _partition("P_C", "Core0")],
                "tasks": [*state.layers["tasks"], by_instance],
            }
        )
        assert _partitions_of(placed, "Actuator") == [
            {"partition": "P_B", "core": "Core1"},
            {"partition": "P_C", "core": "Core0"},
        ]

    def test_the_components_are_partition_runs_keys_for_every_partition(self) -> None:
        """No second formula: each value's partition runs the row's component by ``partition_runs``."""
        state = _partition_state()
        swcs = state.layers["swc"]
        by_swc_name = {swc.name: swc for swc in swcs}
        identities = identities_from(state.layers["compositions"])
        for partition in ("P_A", "P_B", "P_Lost"):
            runs = validator.asil.partition_runs(
                partition, state.layers["tasks"], by_swc_name, lambda _s: "", identities
            )
            answered = {
                swc.name for swc in swcs if partition in [v["partition"] for v in _partitions_of(state, swc.name)]
            }
            assert answered == set(runs)

    def test_the_value_dicts_are_fresh_per_row(self) -> None:
        state = _partition_state()
        [first, second] = validator.derive_rows([{"element": "Sensor"}, {"element": "Mate"}], ["partitions"], state)
        first["derived"]["partitions"]["core"] = "Changed"
        assert second["derived"]["partitions"] == {"partition": "P_A", "core": "Core0"}

    def test_it_is_refused_anywhere_but_the_components(self) -> None:
        from swcraft_tools import query

        for layer, members in (("connectors", None), ("hardware", None), ("swc", "runnables.reads")):
            refusal = query.derived_refusal(validator.DERIVED, ["partitions"], layer, members)
            assert refusal is not None
            assert refusal.kind == "not-applicable"
        assert query.derived_refusal(validator.DERIVED, ["partitions"], "swc", None) is None


class TestCrossingEnds:
    """``crossing_ends`` composes ``connection_ends``' pairs with the ``partitions`` reading."""

    def test_a_crossing_connection_is_one_row(self) -> None:
        assert _crossing("Crossing") == [_crossing_end("Sensor", "P_A", "Actuator", "P_B")]

    def test_a_same_partition_connection_has_no_row(self) -> None:
        assert _crossing("Same") == []

    def test_a_delegated_connector_resolves_through_its_composition(self) -> None:
        assert _crossing("Delegated") == [_crossing_end("Sensor", "P_A", "Actuator", "P_B")]

    def test_an_end_no_partition_runs_crosses_nothing(self) -> None:
        assert _crossing("ToIdle") == []

    def test_a_component_in_two_partitions_yields_only_the_differing_combinations(self) -> None:
        assert _crossing("ToTwin") == [_crossing_end("Sensor", "P_A", "Twin", "P_B")]

    def test_a_provider_in_two_partitions_yields_only_the_differing_combinations(self) -> None:
        state = _partition_state()
        twin_out = _swc("Twin", [("Out", "provide", "ISpeed")], runnables=[{"name": "Run"}])
        swcs = [swc for swc in state.layers["swc"] if swc.name != "Twin"] + [twin_out]
        both = ModelState(
            layers={
                **state.layers,
                "swc": swcs,
                "connectors": [
                    _connector("TwinToAll", {"swc": "Twin", "port": "Out"}, {"swc": "Actuator", "port": "In"})
                ],
            }
        )
        assert _crossing("TwinToAll", both) == [_crossing_end("Twin", "P_A", "Actuator", "P_B")]

    def test_both_ends_in_two_partitions_yield_each_differing_combination_once(self) -> None:
        """Provider and requester each run in ``P_A`` and ``P_B``: A->B and B->A, never A->A or B->B."""
        state = _partition_state()
        twin_out = _swc("Twin", [("Out", "provide", "ISpeed")], runnables=[{"name": "Run"}])
        swcs = [swc for swc in state.layers["swc"] if swc.name != "Twin"] + [twin_out]
        both = ModelState(
            layers={
                **state.layers,
                "swc": swcs,
                "tasks": [*state.layers["tasks"], _task("T_A2", "P_A", _runs("Actuator"))],
                "connectors": [
                    _connector("TwinToAll", {"swc": "Twin", "port": "Out"}, {"swc": "Actuator", "port": "In"})
                ],
            }
        )
        assert _partitions_of(both, "Actuator") == [
            {"partition": "P_A", "core": "Core0"},
            {"partition": "P_B", "core": "Core1"},
        ]
        assert _crossing("TwinToAll", both) == [
            _crossing_end("Twin", "P_A", "Actuator", "P_B"),
            _crossing_end("Twin", "P_B", "Actuator", "P_A"),
        ]

    def test_an_undeclared_connector_has_no_row(self) -> None:
        assert _crossing("Undeclared") == []

    def test_the_ends_are_connection_ends_values(self) -> None:
        """Drop ``partition`` from every value and it is one of ``connection_ends``' values for the row."""
        state = _partition_state()
        for connector in ("Crossing", "Delegated", "ToTwin"):
            ends = [
                row["derived"]["connection_ends"]
                for row in validator.derive_rows([{"element": connector}], ["connection_ends"], state)
            ]
            for value in _crossing(connector, state):
                stripped = {side: {k: v for k, v in end.items() if k != "partition"} for side, end in value.items()}
                assert stripped in ends

    def test_with_connection_ends_on_a_fan_out_it_is_the_uncorrelated_product(self) -> None:
        """Two set-valued names on one row are their product, never zipped by end.

        ``Fan`` reaches ``Actuator`` (``P_B``) and ``Mate`` (``P_A``) from
        ``Sensor`` (``P_A``): two connection ends, one crossing end, so two rows,
        one of which pairs the ``Mate`` connection end with the ``Actuator``
        crossing end -- the stated limit of ``derive_rows``.
        """
        state = _partition_state()
        fan = _composition(
            "Fan", [("a", "type", "Actuator"), ("m", "type", "Mate")], [("P", "a", "In"), ("P", "m", "In")]
        )
        fanned = ModelState(
            layers={
                **state.layers,
                "compositions": [*state.layers["compositions"], fan],
                "connectors": [
                    _connector("FanOut", {"swc": "Sensor", "port": "Out"}, {"composition": "Fan", "port": "P"})
                ],
            }
        )
        from swcraft_tools import query

        assert (
            query.derived_refusal(validator.DERIVED, ["connection_ends", "crossing_ends"], "connectors", None) is None
        )
        rows = validator.derive_rows([{"element": "FanOut"}], ["connection_ends", "crossing_ends"], fanned)
        crossing = _crossing_end("Sensor", "P_A", "Actuator", "P_B")
        assert [row["derived"] for row in rows] == [
            {"connection_ends": _ends(("Sensor", "Out"), ("Actuator", "In")), "crossing_ends": crossing},
            {"connection_ends": _ends(("Sensor", "Out"), ("Mate", "In")), "crossing_ends": crossing},
        ]

    def test_it_is_refused_anywhere_but_the_connectors(self) -> None:
        from swcraft_tools import query

        refusal = query.derived_refusal(validator.DERIVED, ["crossing_ends"], "swc", None)
        assert refusal is not None
        assert refusal.kind == "not-applicable"
        assert query.derived_refusal(validator.DERIVED, ["crossing_ends"], "connectors", None) is None

    def test_both_partition_names_build_one_index_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        original = validator.asil.partition_runs

        def counted(partition: str, *args: Any) -> dict[str, str]:
            calls.append(partition)
            return original(partition, *args)

        validator_package.patch_everywhere(monkeypatch, "partition_runs", counted)
        state = _partition_state()
        validator.derive_rows([{"element": "Crossing"}, {"element": "ToTwin"}], ["crossing_ends"], state)
        assert calls == ["P_A", "P_B", "P_Lost"], "once per resolved partition, in name order, for the whole call"


class TestTwoNamesOnOneRow:
    """``effective_level`` and ``partitions`` share ``{"layer": "swc"}``: the product of their values."""

    def test_the_one_value_repeats_on_each_partition_row(self) -> None:
        state = _partition_state()
        rated = ModelState(
            layers={
                **state.layers,
                "swc": [_swc("Twin", [("In", "require", "ISpeed")], runnables=[{"name": "Run"}], asil="C")],
            }
        )
        rows = validator.derive_rows([{"element": "Twin"}], ["effective_level", "partitions"], rated)
        assert rows == [
            {
                "element": "Twin",
                "derived": {
                    "effective_level": {"level": "C", "rating": "C"},
                    "partitions": {"partition": "P_A", "core": "Core0"},
                },
            },
            {
                "element": "Twin",
                "derived": {
                    "effective_level": {"level": "C", "rating": "C"},
                    "partitions": {"partition": "P_B", "core": "Core1"},
                },
            },
        ]

    def test_names_order_the_keys_and_no_partition_drops_the_row(self) -> None:
        state = _partition_state()
        [row] = validator.derive_rows([{"element": "Sensor"}], ["partitions", "effective_level"], state)
        assert list(row["derived"]) == ["partitions", "effective_level"]
        assert validator.derive_rows([{"element": "Idle"}], ["effective_level", "partitions"], state) == []


# ---------------------------------------------------------------------------
# DERIVED: the vocabulary, held against the live registry
# ---------------------------------------------------------------------------


class TestTheVocabulary:
    """``DERIVED`` is closed, four fields per entry, and every ``at`` is declared."""

    def test_the_published_entries_exactly(self) -> None:
        assert [entry.as_json() for entry in validator.DERIVED] == [
            {
                "name": "connection_ends",
                "at": {"layer": "connectors"},
                "rows": "one per value",
                "shape": {
                    "provider": {"swc": "string", "port": "string"},
                    "requester": {"swc": "string", "port": "string"},
                },
            },
            {
                "name": "base_type",
                "at": {"layer": "swc", "members": ["runnables.reads", "runnables.writes"]},
                "rows": "one",
                "shape": {"implementation": "string|null", "base": "string|null"},
            },
            {
                "name": "effective_level",
                "at": {"layer": "swc"},
                "rows": "one",
                "shape": {"level": "string", "rating": "string"},
            },
            {
                "name": "partitions",
                "at": {"layer": "swc"},
                "rows": "one per value",
                "shape": {"partition": "string", "core": "string|null"},
            },
            {
                "name": "crossing_ends",
                "at": {"layer": "connectors"},
                "rows": "one per value",
                "shape": {
                    "provider": {"swc": "string", "port": "string", "partition": "string"},
                    "requester": {"swc": "string", "port": "string", "partition": "string"},
                },
            },
            {
                "name": "unconnected",
                "at": {"layer": "swc", "members": ["ports"]},
                "rows": "one per value",
                "shape": {"direction": "string", "interface": "string|null"},
            },
            {
                "name": "core",
                "at": {"layer": "tasks"},
                "rows": "one",
                "shape": {"partition": "string", "core": "string|null"},
            },
            {
                "name": "realised_by",
                "at": {"layer": "requirements"},
                "rows": "one per value",
                "shape": {
                    "layer": "string",
                    "element": "string",
                    "member": "string|null",
                    "kind": "string",
                    "stated": "integer|null",
                },
            },
            {
                "name": "allocation",
                "at": {"layer": "requirements"},
                "rows": "one",
                "shape": {"sites": "integer", "elements": "integer", "allocated": "boolean|null"},
            },
            {
                "name": "stale_links",
                "at": {"layer": "requirements"},
                "rows": "one per value",
                "shape": {
                    "layer": "string",
                    "element": "string",
                    "member": "string|null",
                    "kind": "string",
                    "stated": "integer|null",
                    "current": "integer",
                },
            },
        ]

    def test_as_json_is_a_fresh_copy(self) -> None:
        published = validator.DERIVED[1].as_json()
        published["at"]["members"].append("ports")  # type: ignore[index, union-attr]
        published["shape"]["base"] = "string"  # type: ignore[index]
        assert validator.DERIVED[1].as_json()["at"] == {
            "layer": "swc",
            "members": ["runnables.reads", "runnables.writes"],
        }
        assert validator.DERIVED[1].as_json()["shape"] == {"implementation": "string|null", "base": "string|null"}

    def test_names_are_unique_and_rows_and_leaves_are_from_the_closed_sets(self) -> None:
        names = [entry.name for entry in validator.DERIVED]
        assert len(names) == len(set(names))

        def leaves(shape: Mapping[str, object]) -> list[object]:
            found: list[object] = []
            for value in shape.values():
                found.extend(leaves(value) if isinstance(value, Mapping) else [value])
            return found

        for entry in validator.DERIVED:
            assert entry.rows in {"one", "one per value"}
            assert set(leaves(entry.shape)) <= {"string", "string|null", "integer", "integer|null", "boolean|null"}
            assert set(entry.at) <= {"layer", "members"}

    @pytest.mark.parametrize("entry", validator.DERIVED, ids=lambda entry: entry.name)
    def test_every_at_names_a_declared_layer_and_member_path(self, entry: validator.DerivedValue) -> None:
        registry = current_registry()
        layer = entry.at["layer"]
        assert layer in registry.layer_names
        facts = registry.layer(str(layer))
        for members in entry.at.get("members", ()):  # type: ignore[union-attr]
            try:
                path = sliced_path(facts, str(members), registry)
            except QueryError as refusal:  # pragma: no cover - the failure's message
                pytest.fail(f"{entry.name} at {layer}.{members}: {refusal}")
            assert path[-1].collection is not None

    def test_every_name_has_an_arm(self) -> None:
        """A name in the vocabulary that ``derive_rows`` does not compute would answer a wrong shape."""
        state = _pilot_state()
        [first, *_rest] = validator.derive_rows([{"element": "ToGroup"}], ["connection_ends"], state)
        ends = first["derived"]["connection_ends"]  # type: ignore[index]
        shape = validator.DERIVED[0].shape
        assert set(ends) == set(shape)
        for side in shape:
            assert set(ends[side]) == set(shape[side])  # type: ignore[arg-type]
        [row] = validator.derive_rows(
            [{"element": "Writer", "member": {"port": "Out", "element": "Raw"}}], ["base_type"], state
        )
        assert set(row["derived"]["base_type"]) == set(validator.DERIVED[1].shape)  # type: ignore[arg-type]
        [level] = validator.derive_rows([{"element": "Writer"}], ["effective_level"], state)
        assert set(level["derived"]["effective_level"]) == set(validator.DERIVED[2].shape)  # type: ignore[arg-type]
        placed = _partition_state()
        [partition] = validator.derive_rows([{"element": "Sensor"}], ["partitions"], placed)
        assert set(partition["derived"]["partitions"]) == set(validator.DERIVED[3].shape)  # type: ignore[arg-type]
        [crossing] = validator.derive_rows([{"element": "Crossing"}], ["crossing_ends"], placed)
        shape = validator.DERIVED[4].shape
        assert set(crossing["derived"]["crossing_ends"]) == set(shape)
        for side in shape:
            assert set(crossing["derived"]["crossing_ends"][side]) == set(shape[side])  # type: ignore[arg-type]
        assert {entry.name for entry in validator.DERIVED} == {
            "connection_ends",
            "base_type",
            "effective_level",
            "partitions",
            "crossing_ends",
            "unconnected",
            "core",
            "realised_by",
            "allocation",
            "stale_links",
        }
        linked = _linked_state()
        requirement_values = [entry for entry in validator.DERIVED if entry.name in REQUIREMENT_VALUES]
        assert [entry.name for entry in requirement_values] == list(REQUIREMENT_VALUES)
        for entry in requirement_values:
            [row, *_more] = validator.derive_rows([{"element": "REQ-A"}], [entry.name], linked)
            assert set(row["derived"][entry.name]) == set(entry.shape)

    def test_the_leaves_are_the_protocols_closed_set(self) -> None:
        """The schema's ``$defs/derivedShape`` enum, the protocol parser's tuple and this test agree."""
        from swcraft_tools.editing.protocol import DERIVED_SHAPE_LEAVES

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        [leaf, _nested] = schema["$defs"]["derivedShape"]["additionalProperties"]["anyOf"]
        assert tuple(leaf["enum"]) == DERIVED_SHAPE_LEAVES
        assert set(DERIVED_SHAPE_LEAVES) == {"string", "string|null", "integer", "integer|null", "boolean|null"}


# ---------------------------------------------------------------------------
# unconnected and core (`validator._port_and_task_arms`)
# ---------------------------------------------------------------------------


def _unconnected(state: ModelState, *rows: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
    answered = validator.derive_rows([dict(row) for row in rows], ["unconnected"], state)
    return [(row["element"], row["member"], row["derived"]["unconnected"]) for row in answered]


class TestUnconnected:
    """``unconnected``: a port no connector end and no delegation reaches, with its direction and interface."""

    def test_a_port_a_connector_reaches_has_no_row_and_one_nothing_reaches_has_one(self) -> None:
        state = _partition_state()
        assert _unconnected(state, {"element": "Sensor", "member": "Out"}) == []
        loose = ModelState(layers={**state.layers, "swc": [*state.layers["swc"], _placed("Loose", "provide")]})
        assert _unconnected(loose, {"element": "Loose", "member": "Out"}) == [
            ("Loose", "Out", {"direction": "provide", "interface": "ISpeed"})
        ]

    def test_a_port_a_delegation_reaches_has_no_row(self) -> None:
        """``Group``'s delegation names member ``a`` of type ``Actuator`` and its port ``In``."""
        state = _partition_state()
        without_connectors = ModelState(layers={**state.layers, "connectors": []})
        assert _unconnected(without_connectors, {"element": "Actuator", "member": "In"}) == []
        assert _unconnected(without_connectors, {"element": "Mate", "member": "In"}) == [
            ("Mate", "In", {"direction": "require", "interface": "ISpeed"})
        ]

    def test_a_stub_port_answers_no_interface(self) -> None:
        stub = _swc("Stub", [("Probe", "require", None)])
        state = ModelState(layers={**_partition_state().layers, "swc": [*_partition_state().layers["swc"], stub]})
        assert _unconnected(state, {"element": "Stub", "member": "Probe"}) == [
            ("Stub", "Probe", {"direction": "require", "interface": None})
        ]

    def test_a_member_row_given_as_a_mapping_is_read_by_its_name(self) -> None:
        state = ModelState(layers={**_partition_state().layers, "connectors": []})
        assert _unconnected(state, {"element": "Mate", "member": {"name": "In", "direction": "require"}}) == [
            ("Mate", {"name": "In", "direction": "require"}, {"direction": "require", "interface": "ISpeed"})
        ]

    def test_a_component_or_port_not_declared_has_no_row(self) -> None:
        state = ModelState(layers={**_partition_state().layers, "connectors": []})
        assert _unconnected(state, {"element": "Undeclared", "member": "In"}) == []
        assert _unconnected(state, {"element": "Mate", "member": "Undeclared"}) == []

    def test_two_instances_of_one_type_are_not_told_apart(self) -> None:
        """The stated limit: connectors name component types, so one connected instance connects the type."""
        state = _partition_state()
        twins = _composition("Pair", [("left", "type", "Mate"), ("right", "type", "Mate")], [])
        paired = ModelState(layers={**state.layers, "compositions": [*state.layers["compositions"], twins]})
        assert _unconnected(paired, {"element": "Mate", "member": "In"}) == []


def _cores(state: ModelState, *tasks: str) -> list[dict[str, str | None]]:
    rows = validator.derive_rows([{"element": task} for task in tasks], ["core"], state)
    return [row["derived"]["core"] for row in rows]


class TestCore:
    """``core``: the task's partition and that partition's core, ``None`` where it does not resolve."""

    def test_a_task_on_a_partition_answers_its_core(self) -> None:
        assert _cores(_partition_state(), "T_A", "T_B") == [
            {"partition": "P_A", "core": "Core0"},
            {"partition": "P_B", "core": "Core1"},
        ]

    def test_a_partition_on_an_undeclared_core_answers_none(self) -> None:
        assert _cores(_partition_state(), "T_Lost") == [{"partition": "P_Lost", "core": None}]

    def test_a_partition_that_resolves_to_nothing_answers_none(self) -> None:
        state = _partition_state()
        nowhere = ModelState(
            layers={**state.layers, "tasks": [*state.layers["tasks"], _task("T_Nowhere", "P_Nowhere", _runs("Idle"))]}
        )
        assert _cores(nowhere, "T_Nowhere") == [{"partition": "P_Nowhere", "core": None}]

    def test_a_partition_name_resolving_to_another_kind_answers_none(self) -> None:
        state = _partition_state()
        shadowed = ModelState(layers={**state.layers, "hardware": [_core("P_B"), *state.layers["hardware"]]})
        assert _cores(shadowed, "T_B") == [{"partition": "P_B", "core": None}]

    def test_a_partition_declared_twice_resolves_to_its_first_declaration(self) -> None:
        assert _cores(_partition_state(), "T_A") == [{"partition": "P_A", "core": "Core0"}]


# ---------------------------------------------------------------------------
# realised_by, allocation and stale_links: the requirements layer's values
# (`validator._requirement_arms`), on the trace rules' classification
# ---------------------------------------------------------------------------


def _req(req_id: str, version: int = 1, **extra: Any) -> Requirement:
    return Requirement.model_validate(
        {
            "id": req_id,
            "title": req_id,
            "description": f"The system shall satisfy {req_id}.",
            "version": version,
            "source": "architecture",
            **extra,
        }
    )


def _linked_state() -> ModelState:
    """``REQ-A`` (version 2) named at all seven site kinds, the hardware one by its four kinds.

    The component's own list states ``@2`` (current), its port a bare id, its
    runnable ``@1`` (stale), the interface ``@x`` (malformed), the composition
    ``@1``, the task ``@2``, a core ``@1``, a processor ``@2``, a memory and a
    partition ``@2``; a second component names it in the element form only.
    ``REQ-B`` is named nowhere, ``REQ-STUB`` is a stub, ``REQ-TWICE`` is declared
    by two files at two versions and linked at ``@1``, and ``REQ-C`` derives from
    ``REQ-A@1`` -- a ``derived_from`` member, no link.
    """
    component = _swc(
        "Brake",
        [("pOut", "provide", "IBrake")],
        runnables=[{"name": "Run", "satisfies": ["REQ-A@1"]}],
        satisfies=["REQ-A@2", "REQ-TWICE@1"],
    )
    component.ports[0].satisfies.append("REQ-A")
    elsewhere = _swc("Other", satisfies=["swc:Brake"])
    interface = _INTERFACE.validate_python(
        {
            "name": "IBrake",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint16"}],
            "satisfies": ["REQ-A@x"],
        }
    )
    composition = Composition.model_validate(
        {"name": "Top", "title": "Top", "members": [{"name": "brake", "type": "Brake"}], "satisfies": ["REQ-A@1"]}
    )
    task = Task.model_validate(
        {
            "name": "T_10ms",
            "period": 0.01,
            "priority": 1,
            "partition": "P_A",
            "runnables": [{"swc": "Brake", "runnable": "Run"}],
            "satisfies": ["REQ-A@2"],
        }
    )
    hardware = [
        _HARDWARE.validate_python({"name": "Core0", "kind": "core", "threshold": 0.5, "satisfies": ["REQ-A@1"]}),
        _HARDWARE.validate_python({"name": "Ecu", "kind": "processor", "satisfies": ["REQ-A@2"]}),
        _HARDWARE.validate_python(
            {
                "name": "Ram",
                "kind": "memory",
                "type": "RAM",
                "size": 1024,
                "accessible": ["Core0"],
                "threshold": 0.8,
                "satisfies": ["REQ-A@2"],
            }
        ),
        _HARDWARE.validate_python({"name": "P_A", "kind": "partition", "core": "Core0", "satisfies": ["REQ-A@2"]}),
    ]
    return _state(
        swc=[component, elsewhere],
        interfaces=[interface],
        compositions=[composition],
        tasks=[task],
        hardware=hardware,
        requirements=[
            _req("REQ-A", 2),
            _req("REQ-B"),
            _req("REQ-STUB", stub=True),
            _req("REQ-TWICE", 1),
            _req("REQ-TWICE", 2),
            _req("REQ-C", derived_from=["REQ-A@1"]),
        ],
    )


def _value(name: str, element: str, state: ModelState | None = None) -> list[Any]:
    rows = validator.derive_rows([{"element": element}], [name], state or _linked_state())
    return [row["derived"][name] for row in rows]


def _site(layer: str, element: str, member: str | None, kind: str, stated: int | None) -> dict[str, Any]:
    return {"layer": layer, "element": element, "member": member, "kind": kind, "stated": stated}


class TestRealisedBy:
    """``realised_by``: one row per ``satisfies`` member naming the requirement, at every site kind."""

    def test_every_site_kind_in_layer_then_element_then_file_order(self) -> None:
        assert _value("realised_by", "REQ-A") == [
            _site("swc", "Brake", None, "swc", 2),
            _site("swc", "Brake", "pOut", "port", None),
            _site("swc", "Brake", "Run", "runnable", 1),
            _site("interfaces", "IBrake", None, "interface", None),
            _site("compositions", "Top", None, "composition", 1),
            _site("tasks", "T_10ms", None, "task", 2),
            _site("hardware", "Core0", None, "core", 1),
            _site("hardware", "Ecu", None, "processor", 2),
            _site("hardware", "Ram", None, "memory", 2),
            _site("hardware", "P_A", None, "partition", 2),
        ]

    def test_a_requirement_nothing_names_has_no_row_and_an_element_form_member_is_no_site(self) -> None:
        assert _value("realised_by", "REQ-B") == []
        assert all(site["element"] != "Other" for site in _value("realised_by", "REQ-A"))

    def test_each_row_is_a_fresh_value(self) -> None:
        state = _linked_state()
        first, second = validator.derive_rows([{"element": "REQ-A"}, {"element": "REQ-A"}], ["allocation"], state)
        first["derived"]["allocation"]["sites"] = 0
        assert second["derived"]["allocation"]["sites"] == 10


class TestAllocation:
    """``allocation``: the sites, the distinct elements, and ``trace.requirement-unallocated``'s reading."""

    def test_an_allocated_requirement_folds_a_port_and_a_runnable_into_its_component(self) -> None:
        assert _value("allocation", "REQ-A") == [{"sites": 10, "elements": 8, "allocated": True}]

    def test_a_requirement_named_nowhere_is_unallocated(self) -> None:
        assert _value("allocation", "REQ-B") == [{"sites": 0, "elements": 0, "allocated": False}]

    def test_a_stub_abstains(self) -> None:
        assert _value("allocation", "REQ-STUB") == [{"sites": 0, "elements": 0, "allocated": None}]

    def test_a_member_one_list_holds_twice_is_one_site_and_one_row(self) -> None:
        """A ``satisfies`` multiset: ``sites`` counts ``realised_by``'s rows, and both count the doubled member once."""
        component = _swc("Brake", satisfies=["REQ-A@1", "REQ-A@1", "REQ-B@1"])
        state = _state(swc=[component], requirements=[_req("REQ-A", 2), _req("REQ-B")])
        assert _value("realised_by", "REQ-A", state) == [_site("swc", "Brake", None, "swc", 1)]
        assert _value("allocation", "REQ-A", state) == [{"sites": 1, "elements": 1, "allocated": True}]
        assert _value("stale_links", "REQ-A", state) == [{**_site("swc", "Brake", None, "swc", 1), "current": 2}]
        # Another requirement at the same site, at the same stated version, is a site of its own.
        assert _value("allocation", "REQ-B", state) == [{"sites": 1, "elements": 1, "allocated": True}]

    @pytest.mark.parametrize(
        "members", [["REQ-A@x", "REQ-A"], ["REQ-A", "REQ-A@x"]], ids=["malformed-first", "bare-first"]
    )
    def test_a_malformed_suffix_beside_the_bare_id_is_one_site_and_the_bare_one_stays_stale(
        self, members: list[str]
    ) -> None:
        """``R@x`` and ``R`` share one row (``stated`` None); only ``R`` is ``trace.link-version-not-stated``'s site."""
        state = _state(swc=[_swc("Brake", satisfies=members)], requirements=[_req("REQ-A", 2)])
        assert _value("realised_by", "REQ-A", state) == [_site("swc", "Brake", None, "swc", None)]
        assert _value("allocation", "REQ-A", state) == [{"sites": 1, "elements": 1, "allocated": True}]
        assert _value("stale_links", "REQ-A", state) == [{**_site("swc", "Brake", None, "swc", None), "current": 2}]

    def test_a_derived_from_member_does_not_allocate(self) -> None:
        state = _state(requirements=[_req("REQ-A"), _req("REQ-C", derived_from=["REQ-A@1"])])
        assert _value("allocation", "REQ-A", state) == [{"sites": 0, "elements": 0, "allocated": False}]


class TestStaleLinks:
    """``stale_links``: the two version rules' sites, their abstentions kept."""

    def test_a_differing_and_an_unstated_link_are_rows_and_a_current_one_is_not(self) -> None:
        assert _value("stale_links", "REQ-A") == [
            {**_site("swc", "Brake", "pOut", "port", None), "current": 2},
            {**_site("swc", "Brake", "Run", "runnable", 1), "current": 2},
            {**_site("compositions", "Top", None, "composition", 1), "current": 2},
            {**_site("hardware", "Core0", None, "core", 1), "current": 2},
        ]

    def test_the_abstentions_a_malformed_suffix_a_duplicate_id_and_derived_from(self) -> None:
        """``IBrake``'s ``REQ-A@x`` is a site of ``realised_by`` and no stale row; ``REQ-TWICE`` has none."""
        assert all(row["element"] != "IBrake" for row in _value("stale_links", "REQ-A"))
        assert _value("stale_links", "REQ-TWICE") == []
        assert _value("realised_by", "REQ-TWICE") == [_site("swc", "Brake", None, "swc", 1)]
        assert all(row["layer"] != "requirements" for row in _value("stale_links", "REQ-A"))


class TestTheClassificationHelpers:
    """``stale_link`` and ``requirement_allocated``: data only, the reading the rule and the arms share."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("REQ-A@2", None),
            ("REQ-A@1", StaleLink("REQ-A", 1, 2)),
            ("REQ-A", StaleLink("REQ-A", None, 2)),
            ("REQ-A@x", None),
            ("REQ-A@02", None),
            ("REQ-TWICE@1", None),
            ("REQ-NONE@1", None),
            ("swc:Brake", None),
        ],
    )
    def test_stale_link(self, value: str, expected: StaleLink | None) -> None:
        versions = {"REQ-A": [2], "REQ-TWICE": [1, 2]}
        assert stale_link(value, versions) == expected

    def test_requirement_allocated(self) -> None:
        assert validator.requirement_allocated("R", {"R"}, set()) is True
        assert validator.requirement_allocated("R", set(), set()) is False
        assert validator.requirement_allocated("R", {"R"}, {"R"}) is None


# ---------------------------------------------------------------------------
# The agreement: the values and the four trace rules, on the small bench
# ---------------------------------------------------------------------------


def test_the_values_agree_with_the_trace_rules_on_the_small_bench() -> None:
    """``allocated == false`` is the unallocated warning's subjects; ``stale_links`` the version rules' sites."""
    values, findings = trace_agreement(AGENT_TASK_BENCH / "model")
    assert values == findings
    unallocated, stale = values
    assert len(unallocated) == 31
    assert sorted(stale) == [
        ("interfaces", name, None, "interface", "REQ_BTP_TEMPERATURE_RANGE", 1, 2)
        for name in ("IBrakeDiagnostics", "IBrakeTemperature", "IThermalProtectionStatus")
    ]


def test_the_values_agree_with_the_trace_rules_on_the_abstentions(tmp_path: pathlib.Path) -> None:
    """A duplicate id, a malformed suffix and a ``derived_from`` member: neither side reports them."""

    model = tmp_path / "model"
    shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", model)
    requirements = model / "requirements"
    (requirements / "REQ_TWICE_A.yaml").write_text(
        yaml.safe_dump(_req("REQ_TWICE").model_dump(mode="json", exclude_defaults=True)),
        encoding="utf-8",
        newline="\n",
    )
    (requirements / "REQ_TWICE_B.yaml").write_text(
        yaml.safe_dump(_req("REQ_TWICE", 2).model_dump(mode="json", exclude_defaults=True)),
        encoding="utf-8",
        newline="\n",
    )
    (requirements / "REQ_CHILD.yaml").write_text(
        yaml.safe_dump(
            _req("REQ_CHILD", derived_from=["REQ_EXAMPLE_001"]).model_dump(mode="json", exclude_defaults=True)
        ),
        encoding="utf-8",
        newline="\n",
    )
    component = model / "swc" / "example" / "ExampleSWC.yaml"
    text = component.read_text(encoding="utf-8")
    assert "- REQ_EXAMPLE_001@1" in text, "the plant needs the component's link"
    component.write_text(
        text.replace("- REQ_EXAMPLE_001@1", "- REQ_EXAMPLE_001@x\n- REQ_TWICE@1\n- REQ_CHILD@1"),
        encoding="utf-8",
        newline="\n",
    )
    state = load_model_state(model)
    for identifier in ("REQ_EXAMPLE_001", "REQ_TWICE", "REQ_CHILD"):
        rows = validator.derive_rows([{"element": identifier}], ["realised_by"], state)
        assert any(row["derived"]["realised_by"]["element"] == "ExampleSWC" for row in rows), "planted: " + identifier
    values, findings = trace_agreement(model)
    assert values == findings
    result = validator.check_requirement_traceability(str(model))
    assert any(issue.rule == "trace.link-version-not-stated" for issue in result.warnings), "derived_from's bare id"
    _unallocated, stale = values
    assert not stale, "the malformed suffix, the duplicate id and the current link raise nothing"
