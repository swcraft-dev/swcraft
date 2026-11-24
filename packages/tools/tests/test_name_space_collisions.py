"""The name-space predicate: one ``SHORT-NAME`` per AUTOSAR name space, ignoring case.

``[constr_2508]``: *"The content of shortName needs to be unique (case insensitive)
within a given Identifiable"* (GST R4.2.2 ``:2635``, R4.3.0 ``:2711``), the name space
being the one *"established by the nearest ancestor (not only parent) element which
is an Identifiable"* (``[TPS_GST_00018]``, R4.3.0 ``:2682``). The name spaces are
nine below the package level, and each root package with its sub-packages; one
predicate, ``validator.name_space_collisions``, judges them, with its
``NameSpaceCollision`` and its ``name_space_label``.

The first half holds the predicate alone, on hand-built model elements: which groups
it reports and which it leaves to the rule that already has them, the ``level`` of
every hit, the labels and the one sentence. It also holds that
``runnable_event_collisions`` compares through ``identifier.short_name_key`` and
answers as it did.

The second half holds ``rule:name.name-space-collision`` and its five readers:
``check-model`` (after ``check_operation_events``), the writer's guard after
emission -- after ``_check_connectors`` and the containment check -- with
``ArxmlPackage``'s case-folding claim as its package-level twin during emission (the
one exception to "the connector first"), the component export's
``ClosureValidationError``, pass D's ``consistency-violation`` and ``import
component``'s ``ReservedNameError`` over the whole incoming closure; and an
ARXML-side scan over every generating fixture tree's output.

What this module does **not** reach: whether the ``appliesTo`` ids are exactly the
fields the predicate reads (a review is the only control over that list), and real
tool exports.
"""

from __future__ import annotations

import ast
import copy
import inspect
import shutil
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from call_order_support import record_calls
from conftest import REPO_ROOT, model_element_path, write_model_element
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools import validator
from swcraft_tools.arxml_version import NAMESPACE, ArxmlVersion
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    ReservedNameError,
    export_component,
    import_component,
)
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.data_description import read_data_description
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_data_types,
    read_interfaces,
    read_mapping_sets,
    read_swc_model,
)
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    DataReadRef,
    DataType,
    Delegation,
    EnumDataType,
    EnumLiteral,
    ExternalDataType,
    ExternalInterface,
    ImplementationElement,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    MappingSet,
    OperationEvent,
    Port,
    RecordDataType,
    RecordField,
    Runnable,
    SRInterface,
    SWComponent,
    TimingEvent,
    ValueDataType,
)
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    NAME_SPACE_LEVEL_ELEMENT,
    NAME_SPACE_LEVEL_PACKAGE,
    NameSpaceCollision,
    check_cross_layer_consistency,
    name_space_collisions,
    name_space_label,
    runnable_event_collisions,
)
from swcraft_tools.validator.names import _grouped, _name_space_subject
from swcraft_tools.writer import arxml_generation
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.arxml_xml import ArxmlDocument, PackagePlacement, ShortNameScope
from test_package_placement_writer import FIXTURE_TREES as GENERATING_TREES

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _collisions(
    *,
    swcs: list[SWComponent] | None = None,
    interfaces: list[Interface] | None = None,
    data_types: list[DataType] | None = None,
    compositions: list[Composition] | None = None,
    connectors: list[Connector] | None = None,
    mapping_sets: list[MappingSet] | None = None,
) -> list[NameSpaceCollision]:
    return name_space_collisions(
        swcs or [], interfaces or [], data_types or [], compositions or [], connectors or [], mapping_sets or [], []
    )


def _swc(
    name: str = "Brake", *, ports: tuple[str, ...] = (), runnables: tuple[Runnable, ...] = (), **extra: Any
) -> SWComponent:
    return SWComponent(
        name=name,
        ports=[Port(name=port, direction="provide", interface="IBrake") for port in ports],
        runnables=list(runnables),
        **extra,
    )


def _value(name: str, *, package: str | None = None) -> ValueDataType:
    return ValueDataType(name=name, kind="value_type", base="uint8", package=package)


def _base(name: str, *, package: str | None = None) -> BaseDataType:
    return BaseDataType(name=name, kind="base_type", size=8, encoding="NONE", package=package)


def _sr(name: str, *elements: DataElement, package: str | None = None) -> SRInterface:
    # One element when none is named: an interface declares at least one.
    declared = list(elements) or [DataElement(name="value", implementation_type="T")]
    return SRInterface(name=name, kind="sender_receiver", data_elements=declared, package=package)


def _composition(
    name: str = "Chassis",
    *,
    root: bool = True,
    ports: tuple[str, ...] = (),
    members: tuple[str, ...] = (),
    delegations: tuple[str, ...] = (),
    package: str | None = None,
) -> Composition:
    return Composition(
        name=name,
        title=name,
        root=root,
        ports=[CompositionPort(name=port, direction="provide", interface="IBrake") for port in ports],
        members=[CompositionMember(name=member, type="Brake") for member in members],
        delegations=[
            Delegation(name=delegation, port="outer", member="m", member_port="p") for delegation in delegations
        ],
        package=package,
    )


def _connector(name: str, composition: str | None = None) -> Connector:
    return Connector.model_validate(
        {
            "name": name,
            "from": {"swc": "Brake", "port": "p"},
            "to": {"swc": "Wheel", "port": "r"},
            "composition": composition,
        }
    )


def _only(collisions: list[NameSpaceCollision]) -> NameSpaceCollision:
    assert len(collisions) == 1, [collision.sentence for collision in collisions]
    return collisions[0]


# ---------------------------------------------------------------------------
# The package name spaces
# ---------------------------------------------------------------------------


class TestPackageNameSpaces:
    """Each home is a name space: a data description layer's package, ``/PortInterfaces``, ``/ComponentTypes``."""

    def test_a_case_only_group_in_a_layer_package(self) -> None:
        hit = _only(_collisions(data_types=[_value("Boolean"), _value("boolean")]))

        assert hit.name_space == "package '/DataTypes/Implementation'"
        assert hit.level == NAME_SPACE_LEVEL_PACKAGE
        assert hit.names == (("Boolean", "implementation_types"), ("boolean", "implementation_types"))
        assert hit.subjects == (Subject("implementation_types", "Boolean"), Subject("implementation_types", "boolean"))

    def test_a_case_only_pair_of_two_layers_is_two_name_spaces(self) -> None:
        """A value type and a base type, an enum's form and a mapping set: each in its own layer's package."""
        enum = EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="P", value=0)])
        assert _collisions(data_types=[_value("Boolean"), _base("boolean")]) == []
        assert _collisions(data_types=[enum], mapping_sets=[MappingSet(name="gear_t_compumethod")]) == []

    def test_a_case_only_group_in_a_sub_package(self) -> None:
        hit = _only(
            _collisions(data_types=[_base("SInt8", package="PlatformTypes"), _base("sint8", package="PlatformTypes")])
        )

        assert hit.name_space == "package '/DataTypes/Base/PlatformTypes'"
        assert hit.level == NAME_SPACE_LEVEL_PACKAGE
        assert hit.names == (("SInt8", "base_types"), ("sint8", "base_types"))

    @pytest.mark.parametrize("spelling", ["PlatformTypes", "platformtypes"])
    def test_a_sub_package_name_against_a_layer_packages_element(self, spelling: str) -> None:
        """Exact or case-only: the sub-package's SHORT-NAME is a child of its layer package, beside its elements."""
        hit = _only(_collisions(data_types=[_value(spelling), _value("boolean", package="PlatformTypes")]))

        assert hit.name_space == "package '/DataTypes/Implementation'"
        assert hit.level == NAME_SPACE_LEVEL_PACKAGE
        assert (spelling, "implementation_types") in hit.names
        assert ("PlatformTypes", "sub-packages") in hit.names
        assert Subject("implementation_types", "boolean", (field_step("package"),)) in hit.subjects

    @pytest.mark.parametrize("spelling", ["X_Unit", "x_unit"])
    def test_a_sub_package_name_against_a_generated_form(self, spelling: str) -> None:
        application = ApplicationDataType(name="X", kind="application_type", unit="m")
        placed = ApplicationDataType(name="B", kind="application_type", package=spelling)
        hit = _only(_collisions(data_types=[application, placed]))

        assert hit.name_space == "package '/DataTypes/Application'"
        assert hit.level == NAME_SPACE_LEVEL_PACKAGE
        assert hit.names == tuple(sorted({("X_Unit", "generated for data type 'X'"), (spelling, "sub-packages")}))

    @pytest.mark.parametrize("spelling", ["X_Unit", "x_unit"])
    def test_a_sub_package_of_another_layer_is_not_judged_against_a_form(self, spelling: str) -> None:
        application = ApplicationDataType(name="X", kind="application_type", unit="m")
        assert _collisions(data_types=[application, _base("B", package=spelling)]) == []

    def test_a_case_only_group_in_port_interfaces(self) -> None:
        interface = CSInterface(kind="client_server", name="IBrake", operations=[CSOperation(name="get")])
        hit = _only(_collisions(interfaces=[interface, _sr("ibrake")]))

        assert hit.name_space == "package '/PortInterfaces'"
        assert hit.names == (("IBrake", "interfaces"), ("ibrake", "interfaces"))

    def test_a_component_against_a_composition_in_component_types(self) -> None:
        hit = _only(_collisions(swcs=[_swc("Brake")], compositions=[_composition("brake")]))

        assert hit.name_space == "package '/ComponentTypes'"
        assert hit.names == (("Brake", "swc"), ("brake", "compositions"))

    def test_a_generated_implementation_against_a_declared_name(self) -> None:
        hit = _only(_collisions(swcs=[_swc("Brake"), _swc("brake_implementation")]))

        assert hit.names == (("Brake_Implementation", "generated for SWC 'Brake'"), ("brake_implementation", "swc"))

    def test_an_application_type_against_a_generated_compu_method(self) -> None:
        enum = EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="P", value=0)])
        other = ApplicationDataType(name="gear_t_compumethod", kind="application_type")
        hit = _only(_collisions(data_types=[enum, other]))

        assert hit.name_space == "package '/DataTypes/Application'"
        assert hit.names == (
            ("Gear_T_CompuMethod", "generated for data type 'Gear_T'"),
            ("gear_t_compumethod", "application_types"),
        )

    def test_two_sub_packages_differing_in_case(self) -> None:
        hit = _only(_collisions(data_types=[_base("A", package="Legacy"), _base("B", package="legacy")]))

        assert hit.name_space == "package '/DataTypes/Base'"
        assert hit.names == (("Legacy", "sub-packages"), ("legacy", "sub-packages"))

    def test_one_sub_package_named_by_several_elements_is_one_name(self) -> None:
        hit = _only(
            _collisions(data_types=[_base("A", package="Legacy"), _base("B", package="Legacy"), _base("legacy")])
        )

        assert hit.names == (("Legacy", "sub-packages"), ("legacy", "base_types"))
        assert hit.subjects == (
            Subject("base_types", "A", (field_step("package"),)),
            Subject("base_types", "B", (field_step("package"),)),
            Subject("base_types", "legacy"),
        )

    def test_one_exact_name_in_two_packages_is_left_to_model_identity(self) -> None:
        assert _collisions(data_types=[_value("Boolean"), _base("Boolean", package="PlatformTypes")]) == []

    def test_one_exact_name_twice_in_one_package_is_left_to_model_identity(self) -> None:
        assert _collisions(data_types=[_value("Boolean"), _base("Boolean")]) == []
        assert _collisions(swcs=[_swc("Brake")], compositions=[_composition("Brake")]) == []

    def test_a_placed_pair_passes(self) -> None:
        assert _collisions(data_types=[_value("Boolean"), _base("boolean", package="PlatformTypes")]) == []

    def test_a_generated_form_takes_its_owners_package(self) -> None:
        """``X_Unit`` of an ``X`` placed in ``P`` is in ``/DataTypes/P``, beside what else is placed there."""
        application = ApplicationDataType(name="X", kind="application_type", unit="m", package="P")

        assert _collisions(data_types=[application, _value("x_unit")]) == []
        assert _collisions(data_types=[application, _value("x_unit", package="P")]) == []
        other = ApplicationDataType(name="x_unit", kind="application_type", package="P")
        hit = _only(_collisions(data_types=[application, other]))
        assert hit.name_space == "package '/DataTypes/Application/P'"


# ---------------------------------------------------------------------------
# The `_Init` constants
# ---------------------------------------------------------------------------


class TestInitConstants:
    def test_counted_when_init_is_set_and_the_type_resolves(self) -> None:
        interface = _sr("IBrake", DataElement(name="Speed", implementation_type="Speed_T", init=0))
        hit = _only(_collisions(interfaces=[interface, _sr("ibrake_speed_init")], data_types=[_value("Speed_T")]))

        assert hit.name_space == "package '/PortInterfaces'"
        assert hit.names == (
            ("IBrake_Speed_Init", "generated for interface 'IBrake'"),
            ("ibrake_speed_init", "interfaces"),
        )

    def test_not_counted_without_init(self) -> None:
        interface = _sr("IBrake", DataElement(name="Speed", implementation_type="Speed_T"))

        assert _collisions(interfaces=[interface, _sr("ibrake_speed_init")], data_types=[_value("Speed_T")]) == []

    def test_not_counted_when_the_type_is_undeclared(self) -> None:
        interface = _sr("IBrake", DataElement(name="Speed", implementation_type="Speed_T", init=0))

        assert _collisions(interfaces=[interface, _sr("ibrake_speed_init")]) == []

    def test_not_counted_when_the_type_is_a_stub(self) -> None:
        interface = _sr("IBrake", DataElement(name="Speed", implementation_type="Speed_T", init=0))
        stub = ExternalDataType(name="Speed_T", kind="external_type", external_ref="/X/Speed_T")

        assert _collisions(interfaces=[interface, _sr("ibrake_speed_init")], data_types=[stub]) == []


# ---------------------------------------------------------------------------
# The name spaces below the package level
# ---------------------------------------------------------------------------


class TestBelowThePackageLevel:
    def test_a_case_only_port_pair(self) -> None:
        hit = _only(_collisions(swcs=[_swc(ports=("pOut", "POUT"))]))

        assert hit.name_space == "SWC 'Brake'"
        assert hit.level == NAME_SPACE_LEVEL_ELEMENT
        assert hit.names == (("POUT", "ports"), ("pOut", "ports"))
        owner = Subject("swc", "Brake")
        assert hit.subjects == (
            owner.at(field_step("ports"), member_step("POUT")),
            owner.at(field_step("ports"), member_step("pOut")),
        )

    def test_a_port_against_the_generated_behaviour_name(self) -> None:
        hit = _only(_collisions(swcs=[_swc(ports=("Brake_InternalBehavior",))]))

        assert hit.name_space == "SWC 'Brake'"
        assert hit.level == NAME_SPACE_LEVEL_ELEMENT
        assert hit.names == (
            ("Brake_InternalBehavior", "internal behaviour (generated)"),
            ("Brake_InternalBehavior", "ports"),
        )

    def test_a_case_only_runnable_pair(self) -> None:
        hit = _only(_collisions(swcs=[_swc(runnables=(Runnable(name="Run"), Runnable(name="run")))]))

        assert hit.name_space == "behaviour 'Brake_InternalBehavior'"
        assert hit.names == (("Run", "runnables"), ("run", "runnables"))

    def test_an_event_pair_from_two_operation_events(self) -> None:
        runnable = Runnable(
            name="Serve",
            events=[
                OperationEvent(type="operation", port="a", operation="b_c"),
                OperationEvent(type="operation", port="a", operation="B_c"),
            ],
        )
        hit = _only(_collisions(swcs=[_swc(runnables=(runnable,))]))

        assert hit.name_space == "behaviour 'Brake_InternalBehavior'"
        assert hit.names == (("OIT_a_B_c", "events (generated)"), ("OIT_a_b_c", "events (generated)"))
        # Both events are addressed by their runnable's `events` list: one subject.
        assert hit.subjects == (
            Subject("swc", "Brake").at(field_step("runnables"), member_step("Serve"), field_step("events")),
        )

    def test_an_access_point_pair_across_two_access_containers(self) -> None:
        """By value and by argument are two containers; ``R_pA_x`` and ``R_pa_x`` share the runnable's name space."""
        runnable = Runnable(
            name="Run",
            reads=[DataReadRef(port="pA", element="x", by="value"), DataReadRef(port="pa", element="x")],
        )
        hit = _only(_collisions(swcs=[_swc(runnables=(runnable,))]))

        assert hit.name_space == "runnable 'Brake/Run'"
        assert hit.level == NAME_SPACE_LEVEL_ELEMENT
        assert hit.names == (("R_pA_x", "reads"), ("R_pa_x", "reads"))

    def test_an_exact_access_point_repeat_is_left_to_its_rule(self) -> None:
        runnable = Runnable(
            name="Run", reads=[DataReadRef(port="a_b", element="c"), DataReadRef(port="a", element="b_c")]
        )

        assert _collisions(swcs=[_swc(runnables=(runnable,))]) == []

    def test_a_case_only_data_element_pair(self) -> None:
        interface = _sr(
            "IBrake",
            DataElement(name="Speed", implementation_type="T"),
            DataElement(name="speed", implementation_type="T"),
        )
        hit = _only(_collisions(interfaces=[interface]))

        assert hit.name_space == "interface 'IBrake'"
        assert hit.names == (("Speed", "data_elements"), ("speed", "data_elements"))

    def test_a_case_only_operation_pair_and_argument_pair(self) -> None:
        arguments = [
            CSArgument(name="value", direction="in", implementation_type="T"),
            CSArgument(name="Value", direction="out", implementation_type="T"),
        ]
        interface = CSInterface(
            name="IBrake",
            kind="client_server",
            operations=[CSOperation(name="get", arguments=arguments), CSOperation(name="Get")],
        )
        interface_hit, operation_hit = _collisions(interfaces=[interface])

        assert interface_hit.name_space == "interface 'IBrake'"
        assert interface_hit.names == (("Get", "operations"), ("get", "operations"))
        assert operation_hit.name_space == "operation 'IBrake/get'"
        assert operation_hit.names == (("Value", "arguments"), ("value", "arguments"))
        assert operation_hit.subjects[0] == Subject("interfaces", "IBrake").at(
            field_step("operations"), member_step("get"), field_step("arguments"), member_step("Value")
        )

    def test_a_case_only_record_field_pair(self) -> None:
        record = RecordDataType(
            name="Pose_T", kind="record", fields=[RecordField(name="x", type="T"), RecordField(name="X", type="T")]
        )
        hit = _only(_collisions(data_types=[record]))

        assert hit.name_space == "record 'Pose_T'"
        assert hit.level == NAME_SPACE_LEVEL_ELEMENT
        assert hit.names == (("X", "fields"), ("x", "fields"))

    def test_a_case_only_implementation_element_pair(self) -> None:
        record = ImplementationRecordDataType(
            name="Frame_T",
            kind="implementation_record",
            elements=[ImplementationElement(name="id", type="T"), ImplementationElement(name="ID", type="T")],
        )
        hit = _only(_collisions(data_types=[record]))

        assert hit.name_space == "implementation record 'Frame_T'"
        assert hit.names == (("ID", "elements"), ("id", "elements"))


class TestTheCompositionNameSpace:
    def test_a_port_against_a_member(self) -> None:
        hit = _only(_collisions(compositions=[_composition(ports=("Brake",), members=("Brake",))]))

        assert hit.name_space == "composition 'Chassis'"
        assert hit.level == NAME_SPACE_LEVEL_ELEMENT
        assert hit.names == (("Brake", "members"), ("Brake", "ports"))

    def test_a_port_against_a_delegation(self) -> None:
        hit = _only(_collisions(compositions=[_composition(ports=("Brake",), delegations=("Brake",))]))

        assert hit.names == (("Brake", "delegations"), ("Brake", "ports"))

    def test_a_port_against_a_connector_naming_the_composition(self) -> None:
        compositions = [_composition(ports=("Link",)), _composition("Other", root=False)]
        hit = _only(_collisions(compositions=compositions, connectors=[_connector("Link", "Chassis")]))

        assert hit.name_space == "composition 'Chassis'"
        assert hit.names == (("Link", "connectors"), ("Link", "ports"))
        assert Subject("connectors", "Link") in hit.subjects

    def test_a_connector_in_another_composition_is_another_name_space(self) -> None:
        compositions = [_composition(ports=("Link",)), _composition("Other", root=False)]

        assert _collisions(compositions=compositions, connectors=[_connector("Link", "Other")]) == []

    def test_a_connector_naming_no_composition_is_the_roots(self) -> None:
        compositions = [_composition("Other", root=False, members=("Link",)), _composition(members=("link",))]
        hit = _only(_collisions(compositions=compositions, connectors=[_connector("LINK")]))

        assert hit.name_space == "composition 'Chassis'"
        assert hit.names == (("LINK", "connectors"), ("link", "members"))

    def test_a_connector_naming_no_composition_without_one_root_is_judged_nowhere(self) -> None:
        compositions = [_composition(root=False, members=("Link",))]

        assert _collisions(compositions=compositions, connectors=[_connector("Link")]) == []

    def test_a_case_only_member_pair(self) -> None:
        hit = _only(_collisions(compositions=[_composition(members=("Front", "front"))]))

        assert hit.names == (("Front", "members"), ("front", "members"))


class TestLeftToTheRuleThatHasIt:
    def test_an_exact_same_container_group(self) -> None:
        assert _collisions(swcs=[_swc(ports=("pOut", "pOut"))]) == []
        assert _collisions(compositions=[_composition(members=("m", "m"))]) == []
        record = RecordDataType(
            name="Pose_T", kind="record", fields=[RecordField(name="x", type="T"), RecordField(name="x", type="T")]
        )
        assert _collisions(data_types=[record]) == []

    def test_a_group_holding_both_kinds_is_reported_once_with_each_name_once(self) -> None:
        hit = _only(_collisions(swcs=[_swc(ports=("a", "a", "A"))]))

        assert hit.names == (("A", "ports"), ("a", "ports"))
        assert len(hit.subjects) == 2

    @pytest.mark.parametrize("runnable", ["TMT_Run", "tmt_run"])
    def test_a_runnable_event_pair(self, runnable: str) -> None:
        swc = _swc(
            runnables=(Runnable(name="Run", events=[TimingEvent(type="timing", period=0.01)]), Runnable(name=runnable))
        )

        assert _collisions(swcs=[swc]) == []
        assert [collision.runnable for collision in runnable_event_collisions(swc)] == [runnable]

    def test_two_events_differing_in_case_beside_a_runnable_are_reported(self) -> None:
        swc = _swc(
            runnables=(
                Runnable(name="Foo", events=[TimingEvent(type="timing", period=0.01)]),
                Runnable(name="foo", events=[TimingEvent(type="timing", period=0.01)]),
            )
        )
        runnable_pair, event_pair = _collisions(swcs=[swc])

        assert runnable_pair.names == (("Foo", "runnables"), ("foo", "runnables"))
        assert event_pair.names == (("TMT_Foo", "events (generated)"), ("TMT_foo", "events (generated)"))


class TestExternalsAreSkipped:
    def test_an_external_component(self) -> None:
        stub = _swc(
            "Stub",
            ports=("p", "P"),
            runnables=(Runnable(name="Run"), Runnable(name="run")),
            external=True,
            external_ref="/X/Stub",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
        )

        assert _collisions(swcs=[stub, _swc("stub")]) == []

    def test_an_external_interface(self) -> None:
        stub = ExternalInterface(name="IBrake", kind="external_interface", external_ref="/X/IBrake")

        assert _collisions(interfaces=[stub, _sr("ibrake")]) == []

    def test_an_external_data_type(self) -> None:
        stub = ExternalDataType(name="Boolean", kind="external_type", external_ref="/X/Boolean")

        assert _collisions(data_types=[stub, _value("boolean")]) == []


class TestOrder:
    def test_element_level_first_then_packages_in_home_order(self) -> None:
        collisions = _collisions(
            swcs=[_swc(ports=("p", "P")), _swc("brake")],
            interfaces=[_sr("I"), _sr("i")],
            data_types=[_value("V"), _value("v")],
        )

        assert [(collision.level, collision.name_space) for collision in collisions] == [
            (NAME_SPACE_LEVEL_ELEMENT, "SWC 'Brake'"),
            (NAME_SPACE_LEVEL_PACKAGE, "package '/DataTypes/Implementation'"),
            (NAME_SPACE_LEVEL_PACKAGE, "package '/PortInterfaces'"),
            (NAME_SPACE_LEVEL_PACKAGE, "package '/ComponentTypes'"),
            (NAME_SPACE_LEVEL_PACKAGE, "package '/ComponentTypes'"),
        ]
        # The last two: 'Brake' / 'brake', then their `_Implementation` forms.
        assert collisions[-1].names == (
            ("Brake_Implementation", "generated for SWC 'Brake'"),
            ("brake_Implementation", "generated for SWC 'brake'"),
        )

    def test_groups_of_one_name_space_in_key_order(self) -> None:
        collisions = _collisions(swcs=[_swc(ports=("zz", "ZZ", "aa", "AA"))])

        assert [collision.names[0][0] for collision in collisions] == ["AA", "ZZ"]


# ---------------------------------------------------------------------------
# The label and the sentence
# ---------------------------------------------------------------------------


class TestTheLabel:
    @pytest.mark.parametrize(
        ("kind", "owner", "label"),
        [
            ("package", "/DataTypes", "package '/DataTypes'"),
            ("package", "/DataTypes/PlatformTypes", "package '/DataTypes/PlatformTypes'"),
            ("swc", "Brake", "SWC 'Brake'"),
            ("behaviour", "Brake_InternalBehavior", "behaviour 'Brake_InternalBehavior'"),
            ("runnable", "Brake/Run", "runnable 'Brake/Run'"),
            ("interface", "IBrake", "interface 'IBrake'"),
            ("operation", "IBrake/get", "operation 'IBrake/get'"),
            ("record", "Pose_T", "record 'Pose_T'"),
            ("implementation_record", "Frame_T", "implementation record 'Frame_T'"),
            ("composition", "Chassis", "composition 'Chassis'"),
        ],
    )
    def test_every_owner_kind(self, kind: str, owner: str, label: str) -> None:
        assert name_space_label(kind, owner) == label

    def test_a_container_owns_no_name_space(self) -> None:
        with pytest.raises(ValueError, match="no name space is owned by a 'PORTS'"):
            name_space_label("PORTS", "Brake")


class TestTheSentence:
    _BASE = (
        "SWC 'Brake' holds names equal ignoring case: 'POUT' (ports), 'pOut' (ports) -- AUTOSAR requires a "
        "SHORT-NAME to be unique ignoring case within its name space ([constr_2508]); rename all but one."
    )

    def test_the_sentence(self) -> None:
        assert _only(_collisions(swcs=[_swc(ports=("pOut", "POUT"))])).sentence == self._BASE

    def test_with_generated_event_names(self) -> None:
        runnable = Runnable(
            name="Serve",
            events=[
                OperationEvent(type="operation", port="a", operation="b_c"),
                OperationEvent(type="operation", port="a", operation="B_c"),
            ],
        )

        assert _only(_collisions(swcs=[_swc(runnables=(runnable,))])).sentence == (
            "behaviour 'Brake_InternalBehavior' holds names equal ignoring case: 'OIT_a_B_c' (events (generated)), "
            "'OIT_a_b_c' (events (generated)) -- AUTOSAR requires a SHORT-NAME to be unique ignoring case within "
            "its name space ([constr_2508]); rename all but one "
            "(generated event names; rename a port, an operation or a runnable, or state an event's name)."
        )

    def test_with_the_generated_behaviour_name(self) -> None:
        assert _only(_collisions(swcs=[_swc(ports=("brake_internalbehavior",))])).sentence == (
            "SWC 'Brake' holds names equal ignoring case: 'Brake_InternalBehavior' (internal behaviour (generated)), "
            "'brake_internalbehavior' (ports) -- AUTOSAR requires a SHORT-NAME to be unique ignoring case within "
            "its name space ([constr_2508]); rename all but one "
            "(the behaviour's generated name; rename the port, or state behavior_name)."
        )

    def test_for_a_package(self) -> None:
        assert _only(_collisions(data_types=[_value("Boolean"), _value("boolean")])).sentence == (
            "package '/DataTypes/Implementation' holds names equal ignoring case: 'Boolean' (implementation_types), "
            "'boolean' (implementation_types) "
            "-- AUTOSAR requires a SHORT-NAME to be unique ignoring case within its name space ([constr_2508]); "
            "rename all but one (set package on one of them to place it in a sub-package, or rename it)."
        )


# ---------------------------------------------------------------------------
# Purity, and the runnable-event predicate on the same key
# ---------------------------------------------------------------------------


class TestPure:
    def test_the_same_answer_twice_and_no_input_moved(self) -> None:
        swc = _swc(ports=("p", "P"))
        before = swc.model_dump()

        assert _collisions(swcs=[swc]) == _collisions(swcs=[swc])
        assert swc.model_dump() == before

    def test_two_init_events_differing_in_case(self) -> None:
        swc = _swc(
            runnables=(
                Runnable(name="Boot", events=[InitEvent(type="init")]),
                Runnable(name="boot", events=[InitEvent(type="init")]),
            )
        )
        runnable_pair, event_pair = _collisions(swcs=[swc])

        assert runnable_pair.names == (("Boot", "runnables"), ("boot", "runnables"))
        assert event_pair.names == (("IT_Boot", "events (generated)"), ("IT_boot", "events (generated)"))
        owner = Subject("swc", "Brake").at(field_step("runnables"))
        assert event_pair.subjects == (
            owner.at(member_step("Boot"), field_step("events")),
            owner.at(member_step("boot"), field_step("events")),
        )


def _function_source(function: Any) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(function)))


class TestRunnableEventCollisionsOnTheKey:
    def test_it_compares_through_short_name_key_and_folds_nothing_itself(self) -> None:
        tree = _function_source(validator.runnable_event_collisions)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        calls = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "casefold" not in attributes
        assert "lower" not in attributes
        assert "short_name_key" in calls

    def test_the_predicate_folds_nothing_itself(self) -> None:
        for function in (validator.name_space_collisions, _grouped):
            attributes = {node.attr for node in ast.walk(_function_source(function)) if isinstance(node, ast.Attribute)}
            assert not attributes & {"casefold", "lower", "upper"}

    @pytest.mark.parametrize(
        ("runnables", "expected"),
        [
            (
                (Runnable(name="Run", events=[TimingEvent(type="timing", period=0.01)]), Runnable(name="TMT_Run")),
                ["TMT_Run"],
            ),
            (
                (Runnable(name="Run", events=[TimingEvent(type="timing", period=0.01)]), Runnable(name="tmt_run")),
                ["tmt_run"],
            ),
            ((Runnable(name="Boot", events=[InitEvent(type="init")]), Runnable(name="IT_Boot")), ["IT_Boot"]),
            (
                (
                    Runnable(
                        name="Run",
                        events=[TimingEvent(type="timing", period=0.01), TimingEvent(type="timing", period=0.02)],
                    ),
                    Runnable(name="TMT_Run"),
                ),
                [],
            ),
            (
                (
                    Runnable(
                        name="Run",
                        events=[TimingEvent(type="timing", period=0.01), TimingEvent(type="timing", period=0.02)],
                    ),
                    Runnable(name="TMT_Run_0"),
                ),
                ["TMT_Run_0"],
            ),
            ((Runnable(name="Other"), Runnable(name="TMT_Other")), []),
        ],
    )
    def test_its_answers_are_unchanged(self, runnables: tuple[Runnable, ...], expected: list[str]) -> None:
        """The six shapes of ``test_runnable_event_name_space.py``, on the new key; that module runs unchanged too."""
        assert [collision.runnable for collision in runnable_event_collisions(_swc(runnables=runnables))] == expected


# ---------------------------------------------------------------------------
# The committed model trees
# ---------------------------------------------------------------------------

#: Every committed model tree the reader loads. ``editor_schema/accepted_but_invalid``
#: does not load, by design; its three exact same-container groups are the
#: per-list rules'.
_TREES = [
    REPO_ROOT / "examples" / "basic" / "model",
    *sorted(
        {
            path.parent
            for path in (REPO_ROOT / "packages" / "tools" / "tests" / "fixtures").rglob("swc")
            if path.is_dir() and "accepted_but_invalid" not in path.parts
        }
    ),
]


@pytest.mark.parametrize("tree", _TREES, ids=lambda tree: tree.relative_to(REPO_ROOT).as_posix())
def test_no_committed_tree_holds_a_group(tree: Path) -> None:
    root = str(tree)
    found = name_space_collisions(
        read_swc_model(root),
        read_interfaces(root),
        read_data_types(root),
        read_compositions(root),
        read_connectors(root),
        read_mapping_sets(root),
        read_data_description(root).elements,
    )

    assert found == [], [collision.sentence for collision in found]


# ===========================================================================
# `rule:name.name-space-collision` and its readers
# ===========================================================================
#
# The rule is the predicate above, read by five places -- `check-model`, the
# writer's post-emission guard (with `ArxmlPackage`'s case-folding claim its
# package-level twin during emission), the component export's closure check,
# pass D and `import component` -- each held below on `examples/basic` (and, for
# `import component`'s incoming shared elements, on the component-exchange
# fixture). The last section scans the ARXML: name spaces computed from the XML,
# never from the model.

RULE = "name.name-space-collision"
RULE_ID = f"rule:{RULE}"
BASIC = REPO_ROOT / "examples" / "basic"
EXCHANGE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "component_exchange"
PROVIDER = "swc/example/ExampleSWC.yaml"
VERSIONS = [str(version) for version in ArxmlVersion]
_NS = f"{{{NAMESPACE}}}"


def _basic(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _edit(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _put(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _case_only_port(model: Path) -> None:
    _edit(
        model / PROVIDER,
        lambda swc: swc["ports"].append({"name": "POUT", "direction": "provide", "interface": "IExample"}),
    )


def _case_only_data_element(model: Path) -> None:
    _edit(
        model / "interfaces" / "IExample.yaml",
        lambda iface: iface["data_elements"].append({"name": "VALUE", "implementation_type": "float32"}),
    )


def _case_only_argument(model: Path) -> None:
    _edit(
        model / "interfaces" / "IDiagnosticService.yaml",
        lambda iface: iface["operations"][0]["arguments"].append(
            {"name": "DTC_ID", "direction": "in", "implementation_type": "uint16"}
        ),
    )


def _connector_named_like_a_member(model: Path) -> None:
    _edit(model / "connectors" / "Example_to_Consumer.yaml", lambda connector: connector.update(name="ExampleSWC"))


#: shape -> (mutation, the name space the one finding names). Each is a group
#: BELOW the package level, which the writer's post-emission guard refuses.
ELEMENT_SHAPES: dict[str, tuple[Callable[[Path], None], str]] = {
    "case-only ports": (_case_only_port, "SWC 'ExampleSWC'"),
    "case-only data elements": (_case_only_data_element, "interface 'IExample'"),
    "case-only arguments": (_case_only_argument, "operation 'IDiagnosticService/ReadDtc'"),
    "connector named like a member": (_connector_named_like_a_member, "composition 'Root'"),
}


def _rule_records(model: Path) -> list[Any]:
    return [record for record in check_cross_layer_consistency(str(model)).records if record.rule == RULE]


class TestTheRule:
    def test_declared_bound_and_answered_with_consistency_violation(self) -> None:
        contract = load_contract()
        (rule,) = [rule for rule in contract["rules"] if rule["id"] == RULE_ID]
        assert rule["family"] == "cross-element"
        assert rule["summary"] == CONSISTENCY_RULES[RULE]
        assert load_binding()["rules"][RULE_ID] == {
            "module": "swcraft_tools.validator.names",
            "callable": "check_name_space_collisions",
        }
        assert RULE_CODES[RULE].code == "consistency-violation"

    def test_the_applies_to_is_decision_6s_81_and_the_composition_root(self) -> None:
        """The ``appliesTo`` declares 119 distinct ids, the fields the predicate reads.

        Beside the element names and the nineteen ``package`` fields: ``root`` (a
        connector naming no composition is placed in the root composition's name
        space, ``_composition_name_spaces``), a data element's
        ``application_type`` beside its ``implementation_type``, the stated names
        (``behavior_name``, each event's, read's, write's and call's ``name``), a
        behaviour's variables and variable accesses, its exclusive areas, a
        data-received and a call-returned event's emitted-name fields, a call's
        ``poll`` and both stated ``result`` fields. 119 is the contract's declared
        count: nothing here ties it to the fields the predicate reads, which
        review alone holds.
        """
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == RULE_ID]
        applies = rule["appliesTo"]
        assert len(applies) == len(set(applies)) == 119
        assert "field:compositions.composition.root" in applies
        assert {
            "field:interfaces.data_element.implementation_type",
            "field:interfaces.data_element.application_type",
        } <= set(applies)
        assert "field:swc.data_read.by" not in applies
        assert sum(entry.endswith(".package") for entry in applies) == 19

    def test_the_unmodified_example_has_no_finding(self, tmp_path: Path) -> None:
        assert _rule_records(_basic(tmp_path) / "model") == []

    @pytest.mark.parametrize("shape", sorted(ELEMENT_SHAPES))
    def test_check_model_reports_the_predicates_sentence(self, shape: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        mutate, name_space = ELEMENT_SHAPES[shape]
        mutate(model)

        (record,) = _rule_records(model)
        (collision,) = _collisions(
            swcs=read_swc_model(str(model)),
            interfaces=read_interfaces(str(model)),
            data_types=read_data_types(str(model)),
            compositions=read_compositions(str(model)),
            connectors=read_connectors(str(model)),
            mapping_sets=read_mapping_sets(str(model)),
        )
        assert record.message == collision.sentence
        assert record.message.startswith(f"{name_space} holds names equal ignoring case")
        assert record.subjects == collision.subjects

    def test_the_subjects_are_the_two_ports(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _case_only_port(model)

        (record,) = _rule_records(model)
        owner = Subject("swc", "ExampleSWC")
        assert record.subjects == (
            owner.at(field_step("ports"), member_step("POUT")),
            owner.at(field_step("ports"), member_step("pOut")),
        )

    def test_a_connector_subject_is_reported_at_the_connectors_layer(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _connector_named_like_a_member(model)

        (record,) = _rule_records(model)
        assert "connectors" in {subject.layer for subject in record.subjects}

    def test_a_mapping_set_subject_is_reported_at_the_mapping_sets_layer(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        write_model_element(model, {"name": "SetA"}, layer="mapping_sets")
        _put(model_element_path(model, "seta~1", layer="mapping_sets"), {"name": "seta"})

        (record,) = _rule_records(model)
        assert {subject.layer for subject in record.subjects} == {"mapping_sets"}
        assert record.message.startswith("package '/DataTypes/MappingSets' holds")

    def test_a_subject_in_an_undeclared_layer_raises(self) -> None:
        with pytest.raises(ValueError, match="unexpected layer 'requirements'"):
            _name_space_subject(Subject("requirements", "REQ_1"))

    def test_reported_after_check_operation_events(self, tmp_path: Path) -> None:
        """The composed tuple's order: the operation-event finding first, this rule's last."""
        model = _basic(tmp_path) / "model"
        _case_only_port(model)
        _edit(
            model / PROVIDER,
            lambda swc: swc["runnables"].append(
                {"name": "Serve", "events": [{"type": "operation", "port": "absent", "operation": "ReadDtc"}]}
            ),
        )

        rules = [record.rule for record in check_cross_layer_consistency(str(model)).records]
        assert "swc.operation-event.unknown-port" in rules
        assert rules[-1] == RULE
        assert rules.index("swc.operation-event.unknown-port") < rules.index(RULE)


class TestTheWritersGuard:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("shape", sorted(ELEMENT_SHAPES))
    def test_refused_with_nothing_written(self, shape: str, version: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        ELEMENT_SHAPES[shape][0](model)
        (record,) = _rule_records(model)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output), version=version)

        assert str(caught.value).startswith(f"{RULE_ID}: ")
        assert record.message in str(caught.value)
        assert not output.exists()
        assert not output.parent.exists()

    def test_the_guard_runs_after_emission_and_the_containment_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check order: compositions emitted, containment proven, then this guard."""
        model = _basic(tmp_path) / "model"
        _case_only_port(model)
        calls = record_calls(
            monkeypatch,
            arxml_generation,
            (
                "_emit_compositions",
                "_check_connectors",
                "_check_package_containment",
                "_check_name_space_collisions",
            ),
        )

        with pytest.raises(ValueError, match=RULE_ID):
            generate_arxml(str(model), str(tmp_path / "model.arxml"))

        assert calls == [
            "_emit_compositions",
            "_check_connectors",
            "_check_package_containment",
            "_check_name_space_collisions",
        ]

    def test_a_broken_connector_is_reported_first(self, tmp_path: Path) -> None:
        """The connector's error comes from `generate arxml`; `check-model` reports both findings, in that order."""
        model = _basic(tmp_path) / "model"
        _case_only_port(model)
        _edit(
            model / "connectors" / "Example_to_Consumer.yaml", lambda connector: connector["from"].update(port="absent")
        )
        output = tmp_path / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output))

        assert RULE_ID not in str(caught.value)
        assert "absent" in str(caught.value)
        assert not output.exists()
        rules = [record.rule for record in check_cross_layer_consistency(str(model)).records]
        assert "connector.endpoint-unknown-port" in rules
        assert rules.index("connector.endpoint-unknown-port") < rules.index(RULE)

    def test_the_example_still_generates(self, tmp_path: Path) -> None:
        output = tmp_path / "model.arxml"
        generate_arxml(str(_basic(tmp_path) / "model"), str(output))
        assert output.is_file()


def _case_only_data_type(model: Path) -> None:
    _put(
        model_element_path(model, "BOOL~1", kind="value_type"),
        {"name": "BOOL", "kind": "value_type", "base": "bool_base"},
    )


def _sub_package_named_like_an_element(model: Path) -> None:
    write_model_element(model, {"name": "Legacy", "kind": "value_type", "base": "int8_base"})
    _edit(model_element_path(model, "int8", kind="value_type"), lambda dt: dt.update(package="Legacy"))


def _sub_package_named_like_an_element_ignoring_case(model: Path) -> None:
    write_model_element(model, {"name": "legacy", "kind": "value_type", "base": "int8_base"})
    _edit(model_element_path(model, "int8", kind="value_type"), lambda dt: dt.update(package="Legacy"))


def _sub_package_named_like_a_generated_form(model: Path) -> None:
    write_model_element(
        model,
        {"name": "Gear", "kind": "enum", "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}]},
    )
    _edit(model_element_path(model, "int8", kind="value_type"), lambda dt: dt.update(package="gear_compumethod"))


#: shape -> (mutation, the two spellings the claim names). Each holds inside one
#: layer's package, ``/DataTypes/Implementation``: two value types, or a value type
#: and the sub-package of another value type.
PACKAGE_SHAPES: dict[str, tuple[Callable[[Path], None], tuple[str, str]]] = {
    "case-only data types": (_case_only_data_type, ("bool", "BOOL")),
    "sub-package named like an element": (_sub_package_named_like_an_element, ("Legacy", "Legacy")),
    "sub-package named like an element, ignoring case": (
        _sub_package_named_like_an_element_ignoring_case,
        ("legacy", "Legacy"),
    ),
}

#: The layer package the three shapes hold in.
SHAPES_PACKAGE = "/DataTypes/Implementation"


def _base_type_named_like_a_value_type_ignoring_case(model: Path) -> None:
    _put(
        model_element_path(model, "BOOL~1", kind="base_type"),
        {"name": "BOOL", "kind": "base_type", "size": 8, "encoding": "BOOLEAN", "native_declaration": "boolean"},
    )


#: shape -> mutation: a pair in two layers' packages, which the package claims do
#: not judge together. One is a generated form and a sub-package of two different
#: packages.
TWO_PACKAGE_SHAPES: dict[str, Callable[[Path], None]] = {
    "sub-package named like a generated form of another layer": _sub_package_named_like_a_generated_form,
    "base type named like a value type, ignoring case": _base_type_named_like_a_value_type_ignoring_case,
}


class TestThePackageClaim:
    """At package level the writer's refusal is `ArxmlPackage`'s case-folding claim, during emission."""

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("shape", sorted(PACKAGE_SHAPES))
    def test_refused_during_emission_naming_the_rule(self, shape: str, version: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        mutate, spellings = PACKAGE_SHAPES[shape]
        mutate(model)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output), version=version)

        message = str(caught.value)
        assert message.startswith(f"{RULE_ID}: package '{SHAPES_PACKAGE}' holds ")
        assert all(f"'{spelling}'" in message for spelling in spellings)
        assert "[constr_2508]" in message
        assert not output.parent.exists()

    @pytest.mark.parametrize("shape", sorted(PACKAGE_SHAPES))
    def test_check_model_reports_it_under_the_same_rule(self, shape: str, tmp_path: Path) -> None:
        """The same rule id on both sides: the claim's message names it, and it is the finding's rule."""
        model = _basic(tmp_path) / "model"
        PACKAGE_SHAPES[shape][0](model)

        (record,) = _rule_records(model)
        assert f"rule:{record.rule}" == RULE_ID
        assert record.message.startswith(f"package '{SHAPES_PACKAGE}' holds names equal ignoring case")
        assert "(set package on one of them to place it in a sub-package, or rename it)" in record.message

    def test_the_claim_is_the_one_exception_to_the_connector_first(self, tmp_path: Path) -> None:
        """The claim runs during emission, in the first data-type pass, before `_check_connectors`."""
        model = _basic(tmp_path) / "model"
        _case_only_data_type(model)
        _edit(
            model / "connectors" / "Example_to_Consumer.yaml", lambda connector: connector["from"].update(port="absent")
        )

        with pytest.raises(ValueError, match=RULE_ID):
            generate_arxml(str(model), str(tmp_path / "model.arxml"))

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("shape", sorted(TWO_PACKAGE_SHAPES))
    def test_a_pair_in_two_layer_packages_is_accepted(self, shape: str, version: str, tmp_path: Path) -> None:
        """Each layer's package is a name space of its own: neither the claim nor the rule judges the pair together."""
        model = _basic(tmp_path) / "model"
        TWO_PACKAGE_SHAPES[shape](model)
        output = tmp_path / "out" / "model.arxml"

        assert _rule_records(model) == []
        generate_arxml(str(model), str(output), version=version)

        assert output.is_file()
        assert arxml_name_space_groups(output) == []

    def test_a_placed_pair_generates(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _case_only_data_type(model)
        _edit(model_element_path(model, "BOOL~1", kind="value_type"), lambda dt: dt.update(package="Legacy"))
        output = tmp_path / "model.arxml"

        generate_arxml(str(model), str(output))

        assert _rule_records(model) == []
        assert output.is_file()
        assert arxml_name_space_groups(output) == []


class TestArxmlPackageFoldsCase:
    @staticmethod
    def _document(entries: dict[tuple[str, str], str | None]) -> ArxmlDocument:
        return ArxmlDocument(placement=PackagePlacement(entries=entries))

    def test_a_case_only_pair_in_a_root(self) -> None:
        document = self._document({})
        document.data_types.create_element("SW-BASE-TYPE", "boolean")
        with pytest.raises(ValueError) as caught:
            document.data_types.create_element("SW-BASE-TYPE", "Boolean")
        assert str(caught.value) == (
            f"{RULE_ID}: package '/DataTypes' holds 'boolean' and 'Boolean', equal ignoring case -- AUTOSAR "
            "requires a SHORT-NAME to be unique ignoring case within its package ([constr_2508]); set package on "
            "one of them to place it in a sub-package, or rename it."
        )

    def test_a_case_only_pair_in_a_sub_package(self) -> None:
        document = self._document({("DataTypes", "a"): "Lib", ("DataTypes", "A"): "Lib"})
        document.data_types.create_element("SW-BASE-TYPE", "a")
        with pytest.raises(ValueError, match=r"package '/DataTypes/Lib' holds 'a' and 'A'"):
            document.data_types.create_element("SW-BASE-TYPE", "A")

    @pytest.mark.parametrize("element", ["Lib", "lib", "LIB"])
    @pytest.mark.parametrize("sub_package_first", [True, False])
    def test_a_sub_package_against_a_root_element(self, element: str, sub_package_first: bool) -> None:
        document = self._document({("DataTypes", element): None, ("DataTypes", "x"): "Lib"})
        first, second = ("x", element) if sub_package_first else (element, "x")
        document.data_types.create_element("SW-BASE-TYPE", first)
        with pytest.raises(ValueError, match=rf"{RULE_ID}: package '/DataTypes' holds .*\(a sub-package\)"):
            document.data_types.create_element("SW-BASE-TYPE", second)

    def test_two_sub_packages_differing_only_in_case(self) -> None:
        document = self._document({("DataTypes", "x"): "Lib", ("DataTypes", "y"): "LIB"})
        document.data_types.create_element("SW-BASE-TYPE", "x")
        with pytest.raises(ValueError, match=r"'Lib' \(a sub-package\) and 'LIB' \(a sub-package\)"):
            document.data_types.create_element("SW-BASE-TYPE", "y")

    def test_an_exact_repeat_keeps_the_scope_message(self) -> None:
        document = self._document({})
        document.data_types.create_element("SW-BASE-TYPE", "a")
        with pytest.raises(ValueError, match="Duplicate SHORT-NAME 'a' in AR-PACKAGE 'DataTypes'"):
            document.data_types.create_element("SW-BASE-TYPE", "a")

    def test_the_nested_scopes_stay_exact(self) -> None:
        scope = ShortNameScope(etree.Element("PORTS"), "PORTS of SWC 'X'")
        scope.claim("pOut")
        scope.claim("POUT")
        with pytest.raises(ValueError, match="Duplicate SHORT-NAME 'pOut'"):
            scope.claim("pOut")


class TestTheComponentExport:
    @pytest.mark.parametrize("shape", ["case-only ports", "case-only data elements"])
    def test_refused_as_a_closure_validation_error(self, shape: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        ELEMENT_SHAPES[shape][0](model)
        (record,) = _rule_records(model)
        output = tmp_path / "ExampleSWC.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model), "ExampleSWC", str(output))

        assert record.message in caught.value.issues
        assert not output.exists()

    def test_a_package_level_group_of_the_closure_is_refused_too(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _put(
            model_element_path(model, "FLOAT32~1", kind="value_type"),
            {"name": "FLOAT32", "kind": "value_type", "base": "float32_base"},
        )
        _edit(
            model / "interfaces" / "IExample.yaml",
            lambda iface: iface["data_elements"].append({"name": "big", "implementation_type": "FLOAT32"}),
        )
        output = tmp_path / "ExampleSWC.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model), "ExampleSWC", str(output))

        assert any(issue.startswith("package '/DataTypes/Implementation' holds") for issue in caught.value.issues)
        assert not output.exists()


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


class TestPassD:
    def test_a_change_set_leaving_a_group_is_refused(self, tmp_path: Path) -> None:
        project = _basic(tmp_path)
        before = files_of(project / "model")
        token = handle_request({"operation": "discover"}, project=str(project))["baseline"]

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "a port whose name differs from another only in case",
                "operations": [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "ExampleSWC",
                            "path": [{"field": "ports"}, {"member": "POUT"}],
                        },
                        "value": {"name": "POUT", "direction": "provide", "interface": "IExample"},
                    }
                ],
            },
            project=str(project),
            review=_approve,
        )

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        assert "SWC 'ExampleSWC' holds names equal ignoring case" in response["message"], response
        assert files_of(project / "model") == before


# ---------------------------------------------------------------------------
# `import component`: the whole incoming closure
# ---------------------------------------------------------------------------


def _exchange_source(tmp_path: Path) -> Path:
    """The component-exchange fixture, plus an implementation record ``Frame_T`` its SensorSwc reaches."""
    model = tmp_path / "source" / "model"
    shutil.copytree(EXCHANGE / "model", model)
    write_model_element(
        model,
        {
            "name": "Frame_T",
            "kind": "implementation_record",
            "elements": [{"name": "raw", "type": "uint16"}, {"name": "count", "type": "uint8"}],
        },
    )
    _edit(
        model / "interfaces" / "IVehicleSpeed.yaml",
        lambda iface: iface["data_elements"].append({"name": "frame", "implementation_type": "Frame_T"}),
    )
    return model


def _clean_export(tmp_path: Path) -> Path:
    exported = tmp_path / "SensorSwc.arxml"
    export_component(str(_exchange_source(tmp_path)), "SensorSwc", str(exported))
    return exported


def _short_name(element: Any) -> str | None:
    node = element.find(f"{_NS}SHORT-NAME")
    return None if node is None else node.text


def _nearest_identifiable(element: Any) -> Any:
    parent = element.getparent()
    while parent is not None and _short_name(parent) is None:
        parent = parent.getparent()
    return parent


def _clone_renamed(source: Path, target: Path, owner: str, member: str, renamed: str) -> Path:
    """*source* with the element *member* of the Identifiable *owner* copied beside itself as *renamed*.

    A copy, never a rename: nothing references the copy, so the reader drops
    nothing and the file differs from the clean export by that one name alone.
    """
    tree = etree.parse(str(source))
    (element,) = [
        element
        for element in tree.getroot().iter()
        if _short_name(element) == member and _short_name(_nearest_identifiable(element)) == owner
    ]
    added = copy.deepcopy(element)
    added.find(f"{_NS}SHORT-NAME").text = renamed
    element.addnext(added)
    target.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return target


#: shape -> (the owner and member copied, the copy's name, the name space the refusal names).
IMPORT_SHAPES: dict[str, tuple[tuple[str, str], str, str]] = {
    "the component's ports": (("SensorSwc", "pSpeed"), "PSPEED", "SWC 'SensorSwc'"),
    "two data elements of one interface": (("IVehicleSpeed", "speed"), "SPEED", "interface 'IVehicleSpeed'"),
    "two arguments of one operation": (("SetOffset", "raw"), "RAW", "operation 'ICalibration/SetOffset'"),
    "two record fields": (("Pose_T", "x"), "X", "record 'Pose_T'"),
    "two implementation-record elements": (("Frame_T", "raw"), "RAW", "implementation record 'Frame_T'"),
}


class TestImportComponent:
    @pytest.mark.parametrize("shape", sorted(IMPORT_SHAPES))
    @pytest.mark.parametrize("update", [False, True], ids=["create", "update"])
    def test_refused_with_nothing_written(self, shape: str, update: bool, tmp_path: Path) -> None:
        (owner, member), renamed, name_space = IMPORT_SHAPES[shape]
        clean = _clean_export(tmp_path)
        target = init_project(tmp_path / "target")
        if update:
            import_component(str(clean), str(target / "model"))
        foreign = _clone_renamed(clean, tmp_path / "foreign.arxml", owner, member, renamed)
        before = files_of(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(str(foreign), str(target / "model"), update=update)

        message = str(caught.value)
        assert f"{name_space} holds names equal ignoring case" in message
        assert f"'{member}'" in message and f"'{renamed}'" in message
        assert "[constr_2508]" in message
        assert files_of(target) == before

    def test_the_clean_export_imports(self, tmp_path: Path) -> None:
        target = init_project(tmp_path / "target")
        report = import_component(str(_clean_export(tmp_path)), str(target / "model"))
        assert report.swc_action == "created"

    def test_update_refuses_a_group_only_the_carried_access_lists_create(self, tmp_path: Path) -> None:
        """An unmarked export: the target's lists are carried, and two of them derive case-only access points."""
        clean = _clean_export(tmp_path)
        assert b'GID="ACCESS-POINTS"' not in clean.read_bytes()
        target = init_project(tmp_path / "target")
        import_component(str(clean), str(target / "model"))
        (swc_file,) = (target / "model" / "swc").rglob("SensorSwc.yaml")

        def legacy(swc: dict[str, Any]) -> None:
            # A port the export does not declare, so the update replaces the
            # component (an `unchanged` one would carry nothing).
            swc["ports"].append({"name": "pLegacy", "direction": "provide", "interface": "IVehicleSpeed"})
            swc["runnables"][0]["writes"] = [
                {"port": "pSpeed", "element": "speed"},
                {"port": "PSPEED", "element": "speed"},
            ]

        _edit(swc_file, legacy)
        before = files_of(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(str(clean), str(target / "model"), update=True)

        message = str(caught.value)
        assert "runnable 'SensorSwc/SensorMain' holds names equal ignoring case" in message
        assert "'W_PSPEED_speed'" in message and "'W_pSpeed_speed'" in message
        assert files_of(target) == before

    def test_update_refuses_a_group_only_the_carried_operation_events_create(self, tmp_path: Path) -> None:
        """An export with no operation-events marker: the target's events are carried onto its runnable."""
        clean = _clean_export(tmp_path)
        tree = etree.parse(str(clean))
        (marker,) = [sd for sd in tree.getroot().iter(f"{_NS}SD") if sd.get("GID") == "OPERATION-EVENTS"]
        marker.getparent().remove(marker)
        unmarked = tmp_path / "unmarked.arxml"
        unmarked.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        target = init_project(tmp_path / "target")
        import_component(str(clean), str(target / "model"))
        (swc_file,) = (target / "model" / "swc").rglob("SensorSwc.yaml")

        def legacy(swc: dict[str, Any]) -> None:
            swc["ports"].append({"name": "pLegacy", "direction": "provide", "interface": "IVehicleSpeed"})
            swc["runnables"][0]["events"].extend(
                [
                    {"type": "operation", "port": "pCal", "operation": "SetOffset"},
                    {"type": "operation", "port": "PCAL", "operation": "SetOffset"},
                ]
            )

        _edit(swc_file, legacy)
        before = files_of(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(str(unmarked), str(target / "model"), update=True)

        message = str(caught.value)
        assert "behaviour 'SensorSwc_InternalBehavior' holds names equal ignoring case" in message
        assert "'OIT_PCAL_SetOffset'" in message and "'OIT_pCal_SetOffset'" in message
        assert files_of(target) == before


# ---------------------------------------------------------------------------
# The ARXML-side scan
# ---------------------------------------------------------------------------


def arxml_name_space_groups(path: Path) -> list[tuple[str, list[str]]]:
    """Every group of ``SHORT-NAME``s equal ignoring case under one nearest named ancestor, read from the XML.

    An ``lxml`` walk sharing no code with the writer or the predicate: each element
    carrying a ``SHORT-NAME`` belongs to the name space of its nearest ancestor that
    carries one (``[TPS_GST_00018]``, *"nearest ancestor (not only parent)"*) -- the
    document root for a top-level package -- and two names of one name space whose
    ``str.casefold()`` is equal are a group, exact or not. Returned as
    ``(the owner's SHORT-NAME, the names in document order)``.
    """
    tree = etree.parse(str(path))
    groups: dict[tuple[str, str], list[str]] = {}
    labels: dict[str, str] = {}
    for element in tree.getroot().iter():
        name = _short_name(element) if isinstance(element.tag, str) else None
        if name is None:
            continue
        owner = _nearest_identifiable(element)
        key = "/" if owner is None else tree.getpath(owner)
        labels[key] = "/" if owner is None else str(_short_name(owner))
        groups.setdefault((key, name.casefold()), []).append(name)
    return sorted((labels[key], names) for (key, _), names in groups.items() if len(names) > 1)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("tree", GENERATING_TREES, ids=lambda tree: tree.relative_to(REPO_ROOT).parent.as_posix())
def test_no_generated_document_holds_a_group(tree: Path, version: str, tmp_path: Path) -> None:
    output = tmp_path / "model.arxml"
    generate_arxml(str(tree), str(output), version=version)

    assert arxml_name_space_groups(output) == []


def test_the_scan_finds_a_group_the_writer_would_not_emit(tmp_path: Path) -> None:
    """Non-vacuity: a hand-edited document holding a case-only pair of ports is found."""
    output = tmp_path / "model.arxml"
    generate_arxml(str(_basic(tmp_path) / "model"), str(output))
    _clone_renamed(output, output, "ExampleSWC", "pOut", "POUT")

    assert arxml_name_space_groups(output) == [("ExampleSWC", ["pOut", "POUT"])]
