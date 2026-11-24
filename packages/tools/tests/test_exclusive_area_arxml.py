"""The ARXML writer's half of the exclusive areas: the areas and the runnables' references.

A component's ``exclusive_areas`` are written in its ``SWC-INTERNAL-BEHAVIOR``:

* as one ``EXCLUSIVE-AREAS`` between ``DATA-TYPE-MAPPING-REFS`` and ``EVENTS``
  (``group INTERNAL-BEHAVIOR`` precedes ``group SWC-INTERNAL-BEHAVIOR``), one position
  at 4.2.2 and 4.3.0; no wrapper for a component with no area;
* each ``EXCLUSIVE-AREA`` holding ``SHORT-NAME`` and, when the description is
  non-empty, ``DESC``, and nothing else: no ``VARIATION-POINT``.

A runnable's ``can_enter`` is written as ``CAN-ENTER-EXCLUSIVE-AREA-REFS`` before
``MINIMUM-START-INTERVAL`` and its ``runs_inside`` as
``RUNS-INSIDE-EXCLUSIVE-AREA-REFS`` after it (``group EXECUTABLE-ENTITY``), each
reference ``DEST="EXCLUSIVE-AREA"`` naming
``/ComponentTypes[/{package}]/{swc}/{behaviour}/{area}``, in declared order
(``[TPS_SWCT_01049]``--``[TPS_SWCT_01051]``). No ``SWC-EXCLUSIVE-AREA-POLICY``, no
nesting order and no ``REENTRANCY-LEVEL`` is written: the model has no field for them,
and their absence is the standard's default.

**The writer refuses, with nothing written, every class ``check-model`` reports** about
an area or a reference: each negative class is generated into a fresh directory, which
stays empty, and ``check-model`` is asked the same model. The cases ``check-model`` is
silent on generate.

What this module does **not** reach: XSD conformance (``test-xsd``); a model without an
area byte for byte as before; the importer and component exchange; whether an RTE
generator accepts the output; and any model shape outside the ones written below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from exclusive_area_support import (
    CALIBRATE,
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    MONITOR,
    OFFSET_TABLE,
    SHAPE_RUNNABLE_DESCRIPTION,
    SPARE_AREA,
    SPEED_STATE,
    area_model,
    every_area_shape_model,
)
from external_stubs_support import change, put
from inter_runnable_variable_support import BEHAVIOUR, PACKAGE
from inter_runnable_variable_support import MONITOR as SHAPE_MONITOR
from inter_runnable_variable_support import WRITER as SHAPE_WRITER
from lxml import etree
from swcraft_tools.validator import WARNING_RULES, check_cross_layer_consistency, check_xml_representable_text
from swcraft_tools.writer.arxml_generation import INTERNAL_BEHAVIOR_SUFFIX, generate_arxml

NS = {"ar": "http://autosar.org/schema/r4.0"}

BEHAVIOUR_PATH = f"/ComponentTypes/{COMPONENT}/{COMPONENT}{INTERNAL_BEHAVIOR_SUFFIX}"
SHAPE_BEHAVIOUR_PATH = f"/ComponentTypes/{PACKAGE}/{COMPONENT}/{BEHAVIOUR}"
VERSIONS = ("4.2.2", "4.3.0")
CAN_ENTER = "CAN-ENTER-EXCLUSIVE-AREA-REFS"
RUNS_INSIDE = "RUNS-INSIDE-EXCLUSIVE-AREA-REFS"


# Helpers


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A copy of the shared fixture, with its root composition: clean."""
    return area_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _area_entry(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [area for area in document["exclusive_areas"] if area["name"] == name]
    return found


def _runnable_entry(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return found


def _generate(model_root: Path, output: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(output), version=version)
    return etree.parse(str(output)).getroot()


def _tag(node: etree._Element) -> str:
    return etree.QName(node).localname


def _children(node: etree._Element) -> list[str]:
    return [_tag(child) for child in node]


def _short_name(node: etree._Element) -> str:
    return node.findtext("ar:SHORT-NAME", namespaces=NS)


def _behaviour(document: etree._Element) -> etree._Element:
    (node,) = document.xpath(
        "//ar:APPLICATION-SW-COMPONENT-TYPE[ar:SHORT-NAME=$name]//ar:SWC-INTERNAL-BEHAVIOR",
        namespaces=NS,
        name=COMPONENT,
    )
    return node


def _areas(document: etree._Element) -> list[etree._Element]:
    return _behaviour(document).xpath("ar:EXCLUSIVE-AREAS/ar:EXCLUSIVE-AREA", namespaces=NS)


def _area(document: etree._Element, name: str) -> etree._Element:
    (node,) = [area for area in _areas(document) if _short_name(area) == name]
    return node


def _runnable(document: etree._Element, name: str) -> etree._Element:
    (node,) = _behaviour(document).xpath(
        "ar:RUNNABLES/ar:RUNNABLE-ENTITY[ar:SHORT-NAME=$name]", namespaces=NS, name=name
    )
    return node


def _references(runnable: etree._Element, wrapper: str) -> list[tuple[str, str, str]]:
    """``(tag, DEST, path)`` of every reference in *runnable*'s *wrapper*, in document order."""
    return [(_tag(ref), ref.get("DEST"), ref.text) for ref in runnable.xpath(f"ar:{wrapper}/*", namespaces=NS)]


def _path_of(node: etree._Element) -> str:
    """The AUTOSAR path of *node*: its ancestors' ``SHORT-NAME``s and its own."""
    names = [_short_name(ancestor) for ancestor in node.iterancestors() if _short_name(ancestor) is not None]
    return "/" + "/".join([*reversed(names), _short_name(node)])


def _without_any_area(document: dict[str, Any]) -> None:
    document.pop("exclusive_areas")
    for runnable in document["runnables"]:
        runnable.pop("can_enter", None)
        runnable.pop("runs_inside", None)


# The areas


class TestTheAreas:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_behaviour_holds_the_wrapper_before_events(self, model: Path, tmp_path: Path, version: str) -> None:
        """The wrapper holds one position at both versions over what is emitted: before ``EVENTS``."""
        assert _children(_behaviour(_generate(model, tmp_path / "out.arxml", version))) == [
            "SHORT-NAME",
            "EXCLUSIVE-AREAS",
            "EVENTS",
            "HANDLE-TERMINATION-AND-RESTART",
            "RUNNABLES",
            "SUPPORTS-MULTIPLE-INSTANTIATION",
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_wrapper_follows_the_mapping_refs_among_every_neighbour(self, tmp_path: Path, version: str) -> None:
        """``DATA-TYPE-MAPPING-REFS`` before it, ``EVENTS`` and the variables' wrappers after it."""
        document = _generate(every_area_shape_model(tmp_path / "model"), tmp_path / "out.arxml", version)

        assert _children(_behaviour(document)) == [
            "SHORT-NAME",
            "ADMIN-DATA",
            "DATA-TYPE-MAPPING-REFS",
            "EXCLUSIVE-AREAS",
            "EVENTS",
            "EXPLICIT-INTER-RUNNABLE-VARIABLES",
            "HANDLE-TERMINATION-AND-RESTART",
            "IMPLICIT-INTER-RUNNABLE-VARIABLES",
            "RUNNABLES",
            "SUPPORTS-MULTIPLE-INSTANTIATION",
        ]

    def test_the_areas_are_written_in_declared_order(self, model: Path, tmp_path: Path) -> None:
        assert [_short_name(area) for area in _areas(_generate(model, tmp_path / "out.arxml"))] == [
            SPEED_STATE,
            OFFSET_TABLE,
            SPARE_AREA,
        ]

    def test_a_reordered_declaration_is_written_in_its_new_order(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: d["exclusive_areas"].reverse())

        assert [_short_name(area) for area in _areas(_generate(model, tmp_path / "out.arxml"))] == [
            SPARE_AREA,
            OFFSET_TABLE,
            SPEED_STATE,
        ]

    def test_a_stated_description_is_written_exactly_as_desc(self, model: Path, tmp_path: Path) -> None:
        area = _area(_generate(model, tmp_path / "out.arxml"), SPEED_STATE)

        assert _children(area) == ["SHORT-NAME", "DESC"]
        assert area.findtext("ar:DESC/ar:L-2", namespaces=NS) == (
            "Guards the filtered speed shared by the 10 ms filter and the 100 ms monitor."
        )

    def test_an_area_without_a_description_holds_its_short_name_alone(self, model: Path, tmp_path: Path) -> None:
        """No ``DESC``, and no ``VARIATION-POINT`` or any other default added."""
        document = _generate(model, tmp_path / "out.arxml")

        assert _children(_area(document, OFFSET_TABLE)) == ["SHORT-NAME"]
        assert _children(_area(document, SPARE_AREA)) == ["SHORT-NAME"]

    def test_an_area_nobody_uses_is_written(self, model: Path, tmp_path: Path) -> None:
        """The writer changes no value the model holds: an unused area is a declared one."""
        assert _short_name(_area(_generate(model, tmp_path / "out.arxml"), SPARE_AREA)) == SPARE_AREA

    def test_a_component_with_no_area_writes_no_wrapper(self, model: Path, tmp_path: Path) -> None:
        _component(model, _without_any_area)

        document = _generate(model, tmp_path / "out.arxml")

        assert "EXCLUSIVE-AREAS" not in _children(_behaviour(document))
        assert b"EXCLUSIVE" not in etree.tostring(document)

    def test_an_area_is_a_child_of_its_behaviour(self, model: Path, tmp_path: Path) -> None:
        """The wrapper carries no path segment: the area's path is the behaviour's plus its name."""
        assert _path_of(_area(_generate(model, tmp_path / "out.arxml"), SPEED_STATE)) == (
            f"{BEHAVIOUR_PATH}/{SPEED_STATE}"
        )


# The references


class TestTheReferences:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_can_enter_precedes_and_runs_inside_follows_the_minimum_start_interval(
        self, model: Path, tmp_path: Path, version: str
    ) -> None:
        """``group EXECUTABLE-ENTITY``'s order at both versions: can-enter before, runs-inside after."""
        _component(model, lambda d: _runnable_entry(d, FILTER).update(can_enter=[{"area": OFFSET_TABLE}]))
        document = _generate(model, tmp_path / "out.arxml", version)

        assert _children(_runnable(document, FILTER)) == [
            "SHORT-NAME",
            CAN_ENTER,
            "MINIMUM-START-INTERVAL",
            RUNS_INSIDE,
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
        ]
        assert _children(_runnable(document, MONITOR)) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            RUNS_INSIDE,
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
        ]
        assert _children(_runnable(document, CALIBRATE)) == [
            "SHORT-NAME",
            CAN_ENTER,
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_references_sit_among_every_runnable_neighbour(self, tmp_path: Path, version: str) -> None:
        """``DESC`` before ``CAN-ENTER``; the access containers, ``SYMBOL`` and the written variables after."""
        document = _generate(every_area_shape_model(tmp_path / "model"), tmp_path / "out.arxml", version)

        assert _children(_runnable(document, SHAPE_WRITER)) == [
            "SHORT-NAME",
            "DESC",
            "MINIMUM-START-INTERVAL",
            RUNS_INSIDE,
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
            "WRITTEN-LOCAL-VARIABLES",
        ]
        assert _runnable(document, SHAPE_WRITER).findtext("ar:DESC/ar:L-2", namespaces=NS) == (
            SHAPE_RUNNABLE_DESCRIPTION
        )
        assert _children(_runnable(document, SHAPE_MONITOR)) == [
            "SHORT-NAME",
            CAN_ENTER,
            "MINIMUM-START-INTERVAL",
            RUNS_INSIDE,
            "CAN-BE-INVOKED-CONCURRENTLY",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "READ-LOCAL-VARIABLES",
            "SERVER-CALL-POINTS",
            "SYMBOL",
            "WRITTEN-LOCAL-VARIABLES",
        ]

    def test_each_reference_is_written_in_its_role(self, model: Path, tmp_path: Path) -> None:
        """Can-enter and runs-inside are each written in their own role, never swapped."""
        document = _generate(model, tmp_path / "out.arxml")

        for runnable in (FILTER, MONITOR):
            assert _references(_runnable(document, runnable), RUNS_INSIDE) == [
                ("RUNS-INSIDE-EXCLUSIVE-AREA-REF", "EXCLUSIVE-AREA", f"{BEHAVIOUR_PATH}/{SPEED_STATE}")
            ]
            assert _references(_runnable(document, runnable), CAN_ENTER) == []
        assert _references(_runnable(document, CALIBRATE), CAN_ENTER) == [
            ("CAN-ENTER-EXCLUSIVE-AREA-REF", "EXCLUSIVE-AREA", f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}")
        ]
        assert _references(_runnable(document, CALIBRATE), RUNS_INSIDE) == []

    def test_swapping_an_entrys_role_moves_it_to_the_other_wrapper(self, model: Path, tmp_path: Path) -> None:
        def swap(document: dict[str, Any]) -> None:
            calibrate = _runnable_entry(document, CALIBRATE)
            calibrate["runs_inside"] = calibrate.pop("can_enter")

        _component(model, swap)
        calibrate = _runnable(_generate(model, tmp_path / "out.arxml"), CALIBRATE)

        assert _references(calibrate, CAN_ENTER) == []
        assert [path for _tag, _dest, path in _references(calibrate, RUNS_INSIDE)] == [
            f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}"
        ]

    @pytest.mark.parametrize(
        "order", [[OFFSET_TABLE, SPEED_STATE, SPARE_AREA], [SPARE_AREA, SPEED_STATE, OFFSET_TABLE]]
    )
    @pytest.mark.parametrize("list_name,wrapper", [("can_enter", CAN_ENTER), ("runs_inside", RUNS_INSIDE)])
    def test_the_entries_are_written_in_declared_order(
        self, model: Path, tmp_path: Path, order: list[str], list_name: str, wrapper: str
    ) -> None:
        def mutate(document: dict[str, Any]) -> None:
            calibrate = _runnable_entry(document, CALIBRATE)
            calibrate.pop("can_enter")
            calibrate[list_name] = [{"area": area} for area in order]

        _component(model, mutate)
        calibrate = _runnable(_generate(model, tmp_path / "out.arxml"), CALIBRATE)

        assert [path for _tag, _dest, path in _references(calibrate, wrapper)] == [
            f"{BEHAVIOUR_PATH}/{area}" for area in order
        ]

    def test_one_area_in_both_roles_writes_both_references(self, model: Path, tmp_path: Path) -> None:
        """``check-model`` is silent on one area in both roles, and both values are written, each in its place."""
        _component(model, lambda d: _runnable_entry(d, FILTER).update(can_enter=[{"area": SPEED_STATE}]))
        filter_speed = _runnable(_generate(model, tmp_path / "out.arxml"), FILTER)

        assert [path for _tag, _dest, path in _references(filter_speed, CAN_ENTER)] == [
            f"{BEHAVIOUR_PATH}/{SPEED_STATE}"
        ]
        assert [path for _tag, _dest, path in _references(filter_speed, RUNS_INSIDE)] == [
            f"{BEHAVIOUR_PATH}/{SPEED_STATE}"
        ]

    def test_every_reference_resolves_to_an_area_of_its_own_behaviour(self, tmp_path: Path) -> None:
        """Each path names an ``EXCLUSIVE-AREA`` of the same behaviour."""
        document = _generate(every_area_shape_model(tmp_path / "model"), tmp_path / "out.arxml")
        written = {_path_of(area) for area in _areas(document)}
        references = [
            ref
            for wrapper in (CAN_ENTER, RUNS_INSIDE)
            for ref in _behaviour(document).xpath(f"ar:RUNNABLES/ar:RUNNABLE-ENTITY/ar:{wrapper}/*", namespaces=NS)
        ]

        assert len(references) == 4
        for ref in references:
            assert ref.get("DEST") == "EXCLUSIVE-AREA"
            assert ref.text in written
            assert ref.text.startswith(_path_of(_behaviour(document)) + "/")

    def test_the_path_follows_a_stated_package_and_behaviour_name(self, tmp_path: Path) -> None:
        document = _generate(every_area_shape_model(tmp_path / "model"), tmp_path / "out.arxml")

        assert _path_of(_behaviour(document)) == SHAPE_BEHAVIOUR_PATH
        assert [path for _tag, _dest, path in _references(_runnable(document, SHAPE_MONITOR), CAN_ENTER)] == [
            f"{SHAPE_BEHAVIOUR_PATH}/{OFFSET_TABLE}",
            f"{SHAPE_BEHAVIOUR_PATH}/{SPEED_STATE}",
        ]
        assert [path for _tag, _dest, path in _references(_runnable(document, SHAPE_WRITER), RUNS_INSIDE)] == [
            f"{SHAPE_BEHAVIOUR_PATH}/{SPEED_STATE}"
        ]
        assert _path_of(_area(document, SPEED_STATE)) == f"{SHAPE_BEHAVIOUR_PATH}/{SPEED_STATE}"

    def test_a_runnable_without_an_entry_writes_no_wrapper(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, MONITOR).pop("runs_inside"))

        assert _children(_runnable(_generate(model, tmp_path / "out.arxml"), MONITOR)) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
        ]


class TestNoDefaultIsAdded:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_no_policy_no_nesting_order_no_variation_point_no_reentrancy_level(
        self, tmp_path: Path, version: str
    ) -> None:
        """The model has no field for them; their absence is the standard's default."""
        document = _generate(every_area_shape_model(tmp_path / "model"), tmp_path / "out.arxml", version)
        tags = {_tag(node) for node in document.iter() if isinstance(node.tag, str)}

        assert {"EXCLUSIVE-AREAS", "EXCLUSIVE-AREA", CAN_ENTER, RUNS_INSIDE} <= tags
        assert not tags & {
            "EXCLUSIVE-AREA-POLICYS",
            "SWC-EXCLUSIVE-AREA-POLICY",
            "API-PRINCIPLE",
            "EXCLUSIVE-AREA-NESTING-ORDERS",
            "EXCLUSIVE-AREA-NESTING-ORDER",
            "EXCLUSIVE-AREA-NESTING-ORDER-REFS",
            "VARIATION-POINT",
            "REENTRANCY-LEVEL",
            "CALLED-FROM-WITHIN-EXCLUSIVE-AREA-REF",
        }


# The refusal matrix: every class check-model reports


def _unknown_area_runs_inside(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, MONITOR)["runs_inside"].append({"area": "Missing"}))


def _unknown_area_can_enter(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, CALIBRATE)["can_enter"].append({"area": "Missing"}))


def _unknown_area_by_case(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, CALIBRATE)["can_enter"].append({"area": SPEED_STATE.lower()}))


def _an_area_of_another_component(root: Path) -> None:
    put(
        root,
        "swc/chassis/Other.yaml",
        {
            "name": "Other",
            "type": "application",
            "exclusive_areas": [{"name": "OtherArea"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    change(
        root,
        "compositions/Root.yaml",
        lambda d: d["members"].append({"name": "Other", "type": "Other"}),
    )
    _component(root, lambda d: _runnable_entry(d, CALIBRATE)["can_enter"].append({"area": "OtherArea"}))


def _renamed_area(name: str) -> Any:
    def mutate(root: Path) -> None:
        _component(root, lambda d: _area_entry(d, SPARE_AREA).update(name=name))

    return mutate


def _named_like_a_variable(name: str) -> Any:
    def mutate(root: Path) -> None:
        put(
            root,
            "data_types/base/uint8_base.yaml",
            {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        put(root, "data_types/implementation/uint8.yaml", {"name": "uint8", "kind": "value_type", "base": "uint8_base"})
        _component(
            root, lambda d: d.update(variables=[{"name": name, "access": "explicit", "implementation_type": "uint8"}])
        )

    return mutate


def _areas_on_an_external_stub(root: Path) -> None:
    put(
        root,
        "swc/chassis/Foreign.yaml",
        {
            "name": "Foreign",
            "external": True,
            "external_ref": "/Foreign/Foreign",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "exclusive_areas": [{"name": "Hidden"}],
        },
    )


#: Each negative class, the change that produces it, and the rule ``check-model``
#: reports it under. The writer is asked the same model and must refuse it.
REFUSED = {
    "unknown-area-runs-inside": (_unknown_area_runs_inside, "swc.exclusive-area.unknown-area"),
    "unknown-area-can-enter": (_unknown_area_can_enter, "swc.exclusive-area.unknown-area"),
    "unknown-area-by-case": (_unknown_area_by_case, "swc.exclusive-area.unknown-area"),
    "an-area-of-another-component": (_an_area_of_another_component, "swc.exclusive-area.unknown-area"),
    "named-like-a-runnable": (_renamed_area(FILTER), "name.name-space-collision"),
    "named-like-a-runnable-but-for-case": (_renamed_area(FILTER.lower()), "name.name-space-collision"),
    "named-like-an-event": (_renamed_area(f"TMT_{FILTER}"), "name.name-space-collision"),
    "named-like-an-event-but-for-case": (_renamed_area(f"tmt_{FILTER}"), "name.name-space-collision"),
    "named-like-a-variable": (_named_like_a_variable(SPARE_AREA), "name.name-space-collision"),
    "named-like-a-variable-but-for-case": (_named_like_a_variable(SPARE_AREA.upper()), "name.name-space-collision"),
    "two-areas-differing-in-case": (_renamed_area(SPEED_STATE.lower()), "name.name-space-collision"),
    "illegal-area-name": (_renamed_area("spare area"), "identifier.declared"),
    "overlong-area-name": (_renamed_area("A" * 129), "identifier.declared"),
    "areas-on-an-external-stub": (_areas_on_an_external_stub, "swc.external-with-ports"),
}

#: The classes the writer refuses in the validator's words: its message carries every finding verbatim.
SHARED_PREDICATE = frozenset(
    {
        "unknown-area-runs-inside",
        "unknown-area-can-enter",
        "unknown-area-by-case",
        "an-area-of-another-component",
        "named-like-a-runnable",
        "named-like-a-runnable-but-for-case",
        "named-like-an-event",
        "named-like-an-event-but-for-case",
        "named-like-a-variable",
        "named-like-a-variable-but-for-case",
        "two-areas-differing-in-case",
    }
)


def _refusal(root: Path, tmp_path: Path) -> str:
    """Generate into a fresh directory, which must stay empty; return the refusal's text."""
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError) as refusal:
        generate_arxml(str(root), str(out / "model.arxml"))
    assert sorted(entry.name for entry in out.iterdir()) == [], "a refused model must leave no output"
    return str(refusal.value)


class TestTheRefusalMatrix:
    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_check_model_reports_the_class_as_its_one_finding(self, case: str, model: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        result = check_cross_layer_consistency(str(model))

        assert [record.rule for record in (*result.records, *result.warnings)] == [rule]
        assert rule not in WARNING_RULES

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_generate_arxml_refuses_it_and_writes_nothing(self, case: str, model: Path, tmp_path: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        message = _refusal(model, tmp_path)

        if case in SHARED_PREDICATE:
            findings = [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]
            assert findings, "the case must produce a finding, or the loop below proves nothing"
            assert f"rule:{rule}: " in message
            for finding in findings:
                assert finding.message in message

    def test_the_shared_predicate_classes_are_the_matrix_less_the_kept_guards(self) -> None:
        assert set(REFUSED) - SHARED_PREDICATE == {
            "illegal-area-name",
            "overlong-area-name",
            "areas-on-an-external-stub",
        }

    def test_each_kept_guard_refusal_names_its_offender(self, tmp_path: Path) -> None:
        """The classes refused by a kept guard name the offending name, as the gate does."""
        expectations = {
            "illegal-area-name": f"SWC '{COMPONENT}' exclusive area name: 'spare area'",
            "overlong-area-name": f"'{'A' * 129}'",
            "areas-on-an-external-stub": "external stub 'Foreign' declares exclusive_areas",
        }
        for case, offender in expectations.items():
            root = area_model(tmp_path / case / "model")
            REFUSED[case][0](root)

            assert offender in _refusal(root, tmp_path / case), case

    def test_an_area_name_on_the_identifier_limit_generates(self, model: Path, tmp_path: Path) -> None:
        on_limit = "A" * 128
        _component(model, lambda d: _area_entry(d, SPARE_AREA).update(name=on_limit))

        assert _short_name(_area(_generate(model, tmp_path / "out.arxml"), on_limit)) == on_limit

    def test_an_xml_unrepresentable_description_is_refused_and_writes_nothing(
        self, model: Path, tmp_path: Path
    ) -> None:
        _component(model, lambda d: _area_entry(d, SPEED_STATE).update(description="bell \x07 here"))
        (record,) = check_xml_representable_text(str(model)).records
        assert record.rule == "text.xml-unrepresentable-character"

        message = _refusal(model, tmp_path)

        assert message == f"rule:text.xml-unrepresentable-character: {record.message}"


class TestSilenceGenerates:
    """The cases ``check-model`` is silent on: no finding, so the writer writes them."""

    def test_an_unused_area(self, model: Path, tmp_path: Path) -> None:
        assert check_cross_layer_consistency(str(model)).records == ()
        assert SPARE_AREA in [_short_name(area) for area in _areas(_generate(model, tmp_path / "out.arxml"))]

    def test_an_area_one_concurrent_runnable_uses(self, model: Path, tmp_path: Path) -> None:
        _component(
            model, lambda d: _runnable_entry(d, MONITOR).update(concurrent=True, runs_inside=[{"area": SPARE_AREA}])
        )
        assert check_cross_layer_consistency(str(model)).records == ()

        monitor = _runnable(_generate(model, tmp_path / "out.arxml"), MONITOR)

        assert [path for _tag, _dest, path in _references(monitor, RUNS_INSIDE)] == [f"{BEHAVIOUR_PATH}/{SPARE_AREA}"]
        assert monitor.findtext("ar:CAN-BE-INVOKED-CONCURRENTLY", namespaces=NS) == "true"

    def test_one_area_in_both_roles_on_one_runnable(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, FILTER).update(can_enter=[{"area": SPEED_STATE}]))
        assert check_cross_layer_consistency(str(model)).records == ()

        filter_runnable = _runnable(_generate(model, tmp_path / "out.arxml"), FILTER)

        speed_state = f"{BEHAVIOUR_PATH}/{SPEED_STATE}"
        assert [path for _tag, _dest, path in _references(filter_runnable, CAN_ENTER)] == [speed_state]
        assert [path for _tag, _dest, path in _references(filter_runnable, RUNS_INSIDE)] == [speed_state]


class TestTheFixturesAreClean:
    def test_check_model_reports_nothing_about_either_model(self, tmp_path: Path) -> None:
        for name, build in (("plain", area_model), ("every-shape", every_area_shape_model)):
            result = check_cross_layer_consistency(str(build(tmp_path / name)))
            assert (result.consistent, result.issues, result.warnings) == (True, [], ()), name
