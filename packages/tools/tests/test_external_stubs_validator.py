"""The two stub kinds at ``check-model``.

A stub (an ``external_interface`` or an ``external_type``) carries the source's
reference path and its ``DEST`` verbatim. Over one real, valid project (the
``read-swc-element`` vector), broken one way per test:

* **the role table**: each role that admits a stub resolves through one, and each that
  does not keeps its finding;
* **the stub's own rules**: ``interface.external-incomplete-reference`` and
  ``data-type.external-incomplete-reference`` against the admitted ``DEST`` sets of both
  licensed schemas (the eight port interface values, two abstract;
  ``IMPLEMENTATION-DATA-TYPE`` alone), compared as sets and never by suffix;
* **findings where no other rule owns the defect**: an ``init`` on a stub-typed element,
  directly or through reference types (``interface.init-on-external-type``), an access
  through a stub port (``swc.access.unknown-element`` / ``unknown-operation``), a
  ``measures`` key on a stub port (``safety.measure-unknown-element``), each one finding
  per cause;
* **the kind across a stub**: the compatibility predicate compares a concrete ``DEST``
  with the other side's kind and abstains on an abstract one and beyond kind;
* **names**: a stub's name is identifier-checked, stubs claim no emitted ``SHORT-NAME``,
  and model-identity uniqueness holds;
* **pass D**: every new finding reaches the protocol under its code.

What it does **not** reach: the writer, the protocol's writes of a stub, the importer,
and any model shape outside the ones written below; this is example-based evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
from conftest import model_element_storage_name, write_model_element
from swcraft_tools import validator
from swcraft_tools.data_description import read_data_description
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, CONSISTENCY_VIOLATION, RULE_CODES, diagnose
from swcraft_tools.reader import read_interfaces
from swcraft_tools.validator import (
    ABSTRACT_INTERFACE_DESTS,
    EXTERNAL_DATA_TYPE_DESTS,
    EXTERNAL_INTERFACE_DESTS,
    check_cross_layer_consistency,
    check_port_compatibility,
    interface_compatibility,
)
from swcraft_tools.validator.compatibility import interface_index

_PROVIDER = "swc/example/ExampleSWC.yaml"
_CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
_SR_INTERFACE = "interfaces/IExample.yaml"
_CS_INTERFACE = "interfaces/IDiagnosticService.yaml"
_ROOT = "compositions/Root.yaml"

#: The eight values of ``PORT-INTERFACE--SUBTYPES-ENUM``, read from both licensed schemas
#: and written out rather than imported, so a change to the validator's set fails here.
_PORT_INTERFACE_DESTS = (
    "CLIENT-SERVER-INTERFACE",
    "DATA-INTERFACE",
    "MODE-SWITCH-INTERFACE",
    "NV-DATA-INTERFACE",
    "PARAMETER-INTERFACE",
    "PORT-INTERFACE",
    "SENDER-RECEIVER-INTERFACE",
    "TRIGGER-INTERFACE",
)

_SR_REF = "/Foreign/PortInterfaces/IForeignSR"
_TYPE_REF = "/Foreign/ImplementationDataTypes/ForeignT"


# The model


@pytest.fixture()
def model_root(tmp_path: Path) -> Path:
    root = fixtures.build_fixture(tmp_path / "project", "read-swc-element") / "model"
    assert check_cross_layer_consistency(str(root)).consistent
    return root


def _interface_stub(model_root: Path, name: str, dest: str = "SENDER-RECEIVER-INTERFACE", ref: str = _SR_REF) -> None:
    stubs.interface_stub(model_root, name, ref=ref, dest=dest)


def _type_stub(model_root: Path, name: str, dest: str = "IMPLEMENTATION-DATA-TYPE", ref: str = _TYPE_REF) -> None:
    stubs.type_stub(model_root, name, ref=ref, dest=dest)


def _data_type(model_root: Path, document: dict[str, Any]) -> None:
    stubs.put(model_root, model_element_storage_name(document["name"], kind=document["kind"]), document)


def _records(model_root: Path) -> list[validator.ConsistencyIssue]:
    return check_cross_layer_consistency(str(model_root)).records


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in _records(model_root)]


def _typed_argument(model_root: Path, type_name: str) -> None:
    """Give ``IDiagnosticService.ReadDtc``'s first argument another ``implementation_type``."""
    stubs.change(
        model_root,
        _CS_INTERFACE,
        lambda d: d["operations"][0]["arguments"][0].update(implementation_type=type_name),
    )


def _typed_element(model_root: Path, type_name: str, init: Any = None) -> None:
    """Give ``IExample.value`` another type, and an ``init`` when one is passed."""

    # An element naming a declared application type says so in `application_type`.
    field = (
        "application_type"
        if (model_root / model_element_storage_name(type_name, kind="enum")).is_file()
        else "implementation_type"
    )

    def mutate(document: dict[str, Any]) -> None:
        element = document["data_elements"][0]
        document["data_elements"][0] = {
            (field if key in ("implementation_type", "application_type") else key): (
                type_name if key in ("implementation_type", "application_type") else value
            )
            for key, value in element.items()
        }
        if init is not None:
            document["data_elements"][0]["init"] = init

    stubs.change(model_root, _SR_INTERFACE, mutate)


def _repoint_ports(model_root: Path, interface: str) -> None:
    """Both ends of the one connector onto *interface*, so no connector rule answers."""
    stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface=interface))
    stubs.change(model_root, _CONSUMER, lambda d: d["ports"][0].update(interface=interface))


# The admitted sets


class TestTheAdmittedSets:
    def test_the_interface_set_is_the_eight_schema_values(self) -> None:
        assert frozenset(_PORT_INTERFACE_DESTS) == EXTERNAL_INTERFACE_DESTS

    def test_two_of_them_are_abstract(self) -> None:
        assert frozenset({"DATA-INTERFACE", "PORT-INTERFACE"}) == ABSTRACT_INTERFACE_DESTS
        assert ABSTRACT_INTERFACE_DESTS < EXTERNAL_INTERFACE_DESTS

    def test_the_type_set_is_the_implementation_type_alone(self) -> None:
        assert frozenset({"IMPLEMENTATION-DATA-TYPE"}) == EXTERNAL_DATA_TYPE_DESTS


# The role table


class TestTheRoleTable:
    """One test per row of the role table, admitted and refused."""

    def test_an_swc_port_resolves_through_an_interface_stub(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeignSR")
        _repoint_ports(model_root, "IForeignSR")

        assert _rules(model_root) == []

    def test_a_composition_boundary_port_resolves_through_an_interface_stub(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeignSR")
        stubs.change(
            model_root,
            _ROOT,
            lambda d: d.update(ports=[{"name": "pExt", "direction": "provide", "interface": "IForeignSR"}]),
        )

        assert _rules(model_root) == []

    def test_a_port_naming_nothing_declared_is_still_undeclared(self, model_root: Path) -> None:
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeignSR"))

        assert "port.undeclared-interface" in _rules(model_root)

    def test_a_boundary_port_naming_nothing_declared_is_still_undeclared(self, model_root: Path) -> None:
        stubs.change(
            model_root,
            _ROOT,
            lambda d: d.update(ports=[{"name": "pExt", "direction": "provide", "interface": "IForeignSR"}]),
        )

        assert _rules(model_root) == ["composition.port-undeclared-interface"]

    def test_a_data_element_resolves_through_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _typed_element(model_root, "ForeignT")

        assert _rules(model_root) == []

    def test_an_argument_resolves_through_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _typed_argument(model_root, "ForeignT")

        assert _rules(model_root) == []

    def test_an_argument_naming_nothing_declared_is_still_undeclared(self, model_root: Path) -> None:
        # The twin that keeps the test above from passing vacuously: had `_typed_argument` planted a
        # key an argument does not declare, the reader would drop it and both tests would answer [].
        _typed_argument(model_root, "ForeignT")

        assert _rules(model_root) == ["interface.argument-undeclared-type"]

    def test_a_data_element_naming_nothing_declared_is_still_undeclared(self, model_root: Path) -> None:
        _typed_element(model_root, "ForeignT")

        assert _rules(model_root) == ["interface.data-element-undeclared-type"]

    def test_a_reference_base_resolves_through_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RefT", "kind": "reference_type", "base": "ForeignT"})
        _typed_element(model_root, "RefT")

        assert _rules(model_root) == []

    def test_a_reference_base_naming_nothing_declared_is_still_undeclared(self, model_root: Path) -> None:
        _data_type(model_root, {"name": "RefT", "kind": "reference_type", "base": "ForeignT"})

        assert _rules(model_root) == ["data-type.reference-type-undeclared-base"]

    def test_a_value_type_base_never_admits_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "ValT", "kind": "value_type", "base": "ForeignT"})

        # A value type's `base` resolves in `base_types` alone, where no type stub
        # is, so the finding is the undeclared base.
        rules = _rules(model_root)
        assert rules == ["data-type.value-type-undeclared-base"]
        assert "data-type.value-type-base-not-base-type" not in rules

    def test_a_record_field_never_admits_a_type_stub(self, model_root: Path) -> None:
        """A record field resolves in `application_types` alone, so a type stub is an undeclared name there."""
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RecT", "kind": "record", "fields": [{"name": "a", "type": "ForeignT"}]})

        records = _records(model_root)

        assert [record.rule for record in records] == ["data-type.record-field-undeclared-type"]
        assert "'ForeignT'" in records[0].message
        assert "data-type.composite-member-implementation-type" not in validator.CONSISTENCY_RULES

    def test_an_array_element_never_admits_a_type_stub(self, model_root: Path) -> None:
        """As the record field: an undeclared element type of `application_types`."""
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "ArrT", "kind": "array", "element_type": "ForeignT", "length": 2})

        records = _records(model_root)

        assert [record.rule for record in records] == ["data-type.array-undeclared-element-type"]
        assert "'ForeignT'" in records[0].message and "application_types layer" in records[0].message

    def test_a_mapping_set_application_never_admits_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        write_model_element(
            model_root,
            {"name": "Maps", "maps": [{"application": "ForeignT", "implementation": "float32"}]},
            layer="mapping_sets",
        )

        assert _rules(model_root) == ["mapping-set.unknown-application"]

    def test_a_mapping_set_implementation_never_admits_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "Speed", "kind": "application_type", "unit": "mps"})
        write_model_element(
            model_root,
            {"name": "Maps", "maps": [{"application": "Speed", "implementation": "ForeignT"}]},
            layer="mapping_sets",
        )

        assert _rules(model_root) == ["mapping-set.unknown-implementation"]


# The stub's own reference pair


class TestTheInterfaceStubsReferencePair:
    @pytest.mark.parametrize("dest", _PORT_INTERFACE_DESTS)
    def test_every_admitted_dest_is_clean(self, model_root: Path, dest: str) -> None:
        _interface_stub(model_root, "IForeign", dest=dest)

        assert _rules(model_root) == []

    @pytest.mark.parametrize(
        "dest",
        [
            "APPLICATION-SW-COMPONENT-TYPE",
            # A suffix match would admit this one; the set does not.
            "FOREIGN-SENDER-RECEIVER-INTERFACE",
            "IMPLEMENTATION-DATA-TYPE",
            "sender-receiver-interface",
        ],
    )
    def test_a_dest_outside_the_set_is_the_finding(self, model_root: Path, dest: str) -> None:
        _interface_stub(model_root, "IForeign", dest=dest)

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.external-incomplete-reference"]
        assert records[0].subjects == (Subject("interfaces", "IForeign", (field_step("external_dest"),)),)
        assert dest in records[0].message

    def test_a_malformed_ref_is_the_finding(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", ref="not a path at all")

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.external-incomplete-reference"]
        assert records[0].subjects == (Subject("interfaces", "IForeign", (field_step("external_ref"),)),)

    def test_a_malformed_ref_and_dest_are_two_findings(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", ref="not a path", dest="NOT-A-PORT-INTERFACE")

        assert _rules(model_root) == ["interface.external-incomplete-reference"] * 2

    @pytest.mark.parametrize(
        ("document", "blank"),
        [
            ({"external_dest": "SENDER-RECEIVER-INTERFACE"}, ("external_ref",)),
            ({"external_ref": _SR_REF}, ("external_dest",)),
            ({}, ("external_ref", "external_dest")),
            ({"external_ref": "  ", "external_dest": ""}, ("external_ref", "external_dest")),
        ],
    )
    def test_a_blank_half_is_one_finding_naming_each_blank_field(
        self, model_root: Path, document: dict[str, str], blank: tuple[str, ...]
    ) -> None:
        stubs.put(
            model_root, "interfaces/IForeign.yaml", {"name": "IForeign", "kind": "external_interface", **document}
        )

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.external-incomplete-reference"]
        assert records[0].subjects == tuple(Subject("interfaces", "IForeign", (field_step(name),)) for name in blank)


class TestTheTypeStubsReferencePair:
    def test_the_admitted_dest_is_clean(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")

        assert _rules(model_root) == []

    @pytest.mark.parametrize(
        "dest",
        ["APPLICATION-PRIMITIVE-DATA-TYPE", "AUTOSAR-DATA-TYPE", "SW-BASE-TYPE", "FOREIGN-IMPLEMENTATION-DATA-TYPE"],
    )
    def test_a_dest_outside_the_set_is_the_finding(self, model_root: Path, dest: str) -> None:
        _type_stub(model_root, "ForeignT", dest=dest)

        records = _records(model_root)

        assert [record.rule for record in records] == ["data-type.external-incomplete-reference"]
        assert records[0].subjects == (Subject("implementation_types", "ForeignT", (field_step("external_dest"),)),)

    def test_a_malformed_ref_is_the_finding(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT", ref="/Foreign/9bad")

        assert _rules(model_root) == ["data-type.external-incomplete-reference"]

    def test_a_blank_pair_is_one_finding(self, model_root: Path) -> None:
        write_model_element(model_root, {"name": "ForeignT", "kind": "external_type"})

        records = _records(model_root)

        assert [record.rule for record in records] == ["data-type.external-incomplete-reference"]
        assert len(records[0].subjects) == 2

    def test_a_malformed_stub_used_in_a_role_is_reported_once_on_the_stub(self, model_root: Path) -> None:
        """The referrer is not reported a second time: the stub's own rule owns the cause."""
        _type_stub(model_root, "ForeignT", dest="APPLICATION-PRIMITIVE-DATA-TYPE")
        _typed_element(model_root, "ForeignT")

        assert _rules(model_root) == ["data-type.external-incomplete-reference"]


#: Paths inside the three root packages the ARXML writer declares this project's own
#: elements in: a declared element's, an undeclared one, a generated init constant's,
#: the other two packages, the relative spelling ``REF_PATH`` admits, another case.
_OWN_PACKAGE_REFS = (
    "/PortInterfaces/IExample",
    "/PortInterfaces/INothingDeclares",
    "/PortInterfaces/IExample_value_Init",
    "/DataTypes/uint8",
    "/ComponentTypes/ExampleSWC",
    "PortInterfaces/IExample",
    "/portinterfaces/IExample",
    "/DataTypes",
)

#: A package of that name anywhere but first, or a first segment that merely begins
#: with one, is somebody else's package.
_FOREIGN_LOOKALIKE_REFS = ("/Foreign/PortInterfaces/IExample", "/DataTypesExtra/uint8", "/Pkg/DataTypes/uint8")


class TestAReferenceIntoTheGeneratedPackages:
    """A stub's ``external_ref`` inside ``/DataTypes``, ``/PortInterfaces`` or ``/ComponentTypes`` is refused.

    The reference would bind to a declared element in the generated document while
    every model rule treats it as foreign.
    """

    @pytest.mark.parametrize("ref", _OWN_PACKAGE_REFS)
    def test_an_interface_stub(self, model_root: Path, ref: str) -> None:
        _interface_stub(model_root, "IForeign", ref=ref)

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.external-incomplete-reference"]
        assert records[0].subjects == (Subject("interfaces", "IForeign", (field_step("external_ref"),)),)
        assert f"'{ref}'" in records[0].message
        assert "a package the generated ARXML declares this project's own elements in" in records[0].message
        assert records[0].message.endswith("rather than to the source's element.")

    @pytest.mark.parametrize("ref", _OWN_PACKAGE_REFS)
    def test_a_type_stub(self, model_root: Path, ref: str) -> None:
        _type_stub(model_root, "ForeignT", ref=ref)

        records = _records(model_root)

        assert [record.rule for record in records] == ["data-type.external-incomplete-reference"]
        assert records[0].subjects == (Subject("implementation_types", "ForeignT", (field_step("external_ref"),)),)
        assert f"'{ref}'" in records[0].message

    def test_a_dest_outside_the_set_is_a_second_finding(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", ref="/PortInterfaces/IExample", dest="NOT-A-PORT-INTERFACE")

        assert _rules(model_root) == ["interface.external-incomplete-reference"] * 2

    @pytest.mark.parametrize("ref", _FOREIGN_LOOKALIKE_REFS)
    def test_a_lookalike_package_is_clean(self, model_root: Path, ref: str) -> None:
        _interface_stub(model_root, "IForeign", ref=ref)
        _type_stub(model_root, "ForeignT", ref=ref)

        assert _rules(model_root) == []

    def test_the_probe_shape(self, model_root: Path) -> None:
        """Both stubs aimed at the declared element of their own name, and each used."""
        _interface_stub(model_root, "IExample_ext", ref="/PortInterfaces/IExample")
        _type_stub(model_root, "uint8_ext", ref="/DataTypes/uint8")
        stubs.change(
            model_root,
            _CONSUMER,
            lambda d: d["ports"].append({"name": "rX", "direction": "require", "interface": "IExample_ext"}),
        )
        stubs.change(
            model_root,
            _SR_INTERFACE,
            lambda d: d["data_elements"].append({"name": "x", "implementation_type": "uint8_ext"}),
        )

        assert _rules(model_root) == [
            "data-type.external-incomplete-reference",
            "interface.external-incomplete-reference",
        ]


# init on a stub-typed element


class TestInitOnAnExternalType:
    @pytest.mark.parametrize("init", [0, 1.5, [1, 2], {"a": 1}], ids=["int", "float", "list", "map"])
    def test_a_direct_stub_type_refuses_any_value(self, model_root: Path, init: Any) -> None:
        _type_stub(model_root, "ForeignT")
        _typed_element(model_root, "ForeignT", init)

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.init-on-external-type"]
        assert records[0].subjects == (
            Subject(
                "interfaces",
                "IExample",
                (field_step("data_elements"), member_step("value"), field_step("init")),
            ),
            Subject("implementation_types", "ForeignT"),
        )
        assert "'ForeignT'" in records[0].message

    def test_a_one_hop_chain_ending_at_a_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RefT", "kind": "reference_type", "base": "ForeignT"})
        _typed_element(model_root, "RefT", 3)

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.init-on-external-type"]
        assert "'RefT'" in records[0].message and "'ForeignT'" in records[0].message
        assert records[0].subjects[1] == Subject("implementation_types", "ForeignT")

    @pytest.mark.parametrize("init", [7, [1, 2, 3]], ids=["scalar", "structured"])
    def test_a_multi_hop_chain_ending_at_a_stub(self, model_root: Path, init: Any) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RefA", "kind": "reference_type", "base": "RefB"})
        _data_type(model_root, {"name": "RefB", "kind": "reference_type", "base": "RefC"})
        _data_type(model_root, {"name": "RefC", "kind": "reference_type", "base": "ForeignT"})
        _typed_element(model_root, "RefA", init)

        records = _records(model_root)

        assert [record.rule for record in records] == ["interface.init-on-external-type"]
        assert "'RefA' -> 'RefB' -> 'RefC' -> 'ForeignT'" in records[0].message

    def test_a_chain_ending_at_a_value_type_is_judged_as_before(self, model_root: Path) -> None:
        _data_type(model_root, {"name": "RefT", "kind": "reference_type", "base": "float32"})
        _typed_element(model_root, "RefT", "fast")

        assert _rules(model_root) == ["interface.init-string-scalar"]

    def test_a_stub_typed_element_without_init_is_clean(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RefT", "kind": "reference_type", "base": "ForeignT"})
        _typed_element(model_root, "RefT")

        assert _rules(model_root) == []

    def test_a_cyclic_chain_is_the_cycle_rules_alone(self, model_root: Path) -> None:
        _data_type(model_root, {"name": "RefA", "kind": "reference_type", "base": "RefB"})
        _data_type(model_root, {"name": "RefB", "kind": "reference_type", "base": "RefA"})
        _typed_element(model_root, "RefA", 1)

        assert "interface.init-on-external-type" not in _rules(model_root)

    def test_a_record_member_typed_by_a_stub_is_the_undeclared_field_rule_alone(self, model_root: Path) -> None:
        """A record member typed by a stub is its field's undeclared name, the one finding."""
        _type_stub(model_root, "ForeignT")
        _data_type(model_root, {"name": "RecT", "kind": "record", "fields": [{"name": "a", "type": "ForeignT"}]})
        _typed_element(model_root, "RecT", {"a": 1})

        assert _rules(model_root) == ["data-type.record-field-undeclared-type"]


# Access through a stub port

_ACCESS_CASES = [
    # (list, entry, the port's direction, the rule, whether the direction is wrong)
    ("reads", {"port": "p", "element": "value"}, "require", "swc.access.unknown-element", False),
    ("reads", {"port": "p", "element": "value"}, "provide", "swc.access.unknown-element", True),
    ("writes", {"port": "p", "element": "value"}, "provide", "swc.access.unknown-element", False),
    ("writes", {"port": "p", "element": "value"}, "require", "swc.access.unknown-element", True),
    ("calls", {"port": "p", "operation": "Op"}, "require", "swc.access.unknown-operation", False),
    ("calls", {"port": "p", "operation": "Op"}, "provide", "swc.access.unknown-operation", True),
]


class TestAccessThroughAStubPort:
    @pytest.mark.parametrize("dest", ["SENDER-RECEIVER-INTERFACE", "CLIENT-SERVER-INTERFACE", "PORT-INTERFACE"])
    @pytest.mark.parametrize(("list_name", "entry", "direction", "rule", "wrong_way"), _ACCESS_CASES)
    def test_the_access_finding_never_a_key_error(
        self,
        model_root: Path,
        dest: str,
        list_name: str,
        entry: dict[str, str],
        direction: str,
        rule: str,
        wrong_way: bool,
    ) -> None:
        _interface_stub(model_root, "IForeign", dest=dest)

        def mutate(document: dict[str, Any]) -> None:
            document["ports"].append({"name": "p", "direction": direction, "interface": "IForeign"})
            document["runnables"][0][list_name] = [entry]

        stubs.change(model_root, _CONSUMER, mutate)

        records = _records(model_root)
        expected = [rule, "swc.access.direction-mismatch"] if wrong_way else [rule]

        assert sorted(record.rule for record in records) == sorted(expected)
        finding = next(record for record in records if record.rule == rule)
        assert "external interface stub" in finding.message
        assert finding.target == f"IForeign.{next(iter(v for k, v in entry.items() if k != 'port'))}"


# The kind across a stub


def _maps(model_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    interfaces = interface_index(read_interfaces(str(model_root)))
    data_types = read_data_description(str(model_root))
    return interfaces, data_types


class TestTheKindAcrossAStub:
    @pytest.mark.parametrize("context", ["subset", "shared-member"])
    @pytest.mark.parametrize(
        ("dest", "declared", "differs"),
        [
            ("SENDER-RECEIVER-INTERFACE", "IExample", False),
            ("CLIENT-SERVER-INTERFACE", "IExample", True),
            ("CLIENT-SERVER-INTERFACE", "IDiagnosticService", False),
            ("SENDER-RECEIVER-INTERFACE", "IDiagnosticService", True),
            ("MODE-SWITCH-INTERFACE", "IExample", True),
            ("NV-DATA-INTERFACE", "IDiagnosticService", True),
            ("PORT-INTERFACE", "IExample", False),
            ("DATA-INTERFACE", "IDiagnosticService", False),
            ("NOT-A-DEST", "IExample", False),
        ],
    )
    def test_a_concrete_dest_is_compared_and_an_abstract_one_abstains(
        self, model_root: Path, context: str, dest: str, declared: str, differs: bool
    ) -> None:
        _interface_stub(model_root, "IForeign", dest=dest)
        interfaces, data_types = _maps(model_root)

        for required, provided in (("IForeign", declared), (declared, "IForeign")):
            answer = interface_compatibility(
                required, provided, context=context, interfaces=interfaces, data_types=data_types
            )
            if differs:
                assert answer is not None
                assert answer.subject.startswith("interface kinds differ: ")
                assert f"external_interface ({dest})" in answer.subject
            else:
                assert answer is None

    @pytest.mark.parametrize(
        ("first", "second", "differs"),
        [
            ("SENDER-RECEIVER-INTERFACE", "SENDER-RECEIVER-INTERFACE", False),
            ("SENDER-RECEIVER-INTERFACE", "CLIENT-SERVER-INTERFACE", True),
            ("SENDER-RECEIVER-INTERFACE", "PORT-INTERFACE", False),
            ("TRIGGER-INTERFACE", "MODE-SWITCH-INTERFACE", True),
        ],
    )
    def test_two_stubs_compare_their_dests(self, model_root: Path, first: str, second: str, differs: bool) -> None:
        _interface_stub(model_root, "IForeignA", dest=first)
        _interface_stub(model_root, "IForeignB", dest=second)
        interfaces, data_types = _maps(model_root)

        answer = interface_compatibility(
            "IForeignA", "IForeignB", context="subset", interfaces=interfaces, data_types=data_types
        )

        assert (answer is not None) is differs

    def test_a_connector_across_a_kind_mismatch_is_the_finding(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="CLIENT-SERVER-INTERFACE")
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))

        records = _records(model_root)

        assert [record.rule for record in records] == ["connector.interface-mismatch"]
        assert "external_interface (CLIENT-SERVER-INTERFACE)" in records[0].message

    def test_a_connector_across_the_same_kind_is_clean(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="SENDER-RECEIVER-INTERFACE")
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))

        assert _rules(model_root) == []

    def test_a_delegation_across_a_kind_mismatch_is_the_finding(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="CLIENT-SERVER-INTERFACE")
        stubs.change(
            model_root,
            _ROOT,
            lambda d: d.update(
                ports=[{"name": "pExt", "direction": "provide", "interface": "IForeign"}],
                delegations=[{"name": "dExt", "port": "pExt", "member": "ExampleSWC", "member_port": "pOut"}],
            ),
        )

        assert _rules(model_root) == ["composition.delegation-interface-mismatch"]

    def test_a_delegation_across_an_abstract_dest_is_clean(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="PORT-INTERFACE")
        stubs.change(
            model_root,
            _ROOT,
            lambda d: d.update(
                ports=[{"name": "pExt", "direction": "provide", "interface": "IForeign"}],
                delegations=[{"name": "dExt", "port": "pExt", "member": "ExampleSWC", "member_port": "pOut"}],
            ),
        )

        assert _rules(model_root) == []


class TestThePreFlight:
    def _ask(self, model_root: Path) -> validator.CompatibilityResult:
        return check_port_compatibility(str(model_root), "ExampleSWC", "pOut", "ExampleConsumerSWC", "rIn")

    @pytest.mark.parametrize("dest", ["SENDER-RECEIVER-INTERFACE", "PORT-INTERFACE", "DATA-INTERFACE"])
    def test_a_stub_it_cannot_judge_is_not_judged(self, model_root: Path, dest: str) -> None:
        _interface_stub(model_root, "IForeign", dest=dest)
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))

        answer = self._ask(model_root)

        assert answer.compatible is True
        assert answer.reason.startswith("not judged: external interface 'IForeign'"), answer.reason

    def test_a_kind_mismatch_across_a_stub_is_incompatible(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="CLIENT-SERVER-INTERFACE")
        stubs.change(model_root, _CONSUMER, lambda d: d["ports"][0].update(interface="IForeign"))

        answer = self._ask(model_root)

        assert answer.compatible is False
        assert "interface kinds differ" in answer.reason

    @pytest.mark.parametrize("dest", ["SENDER-RECEIVER-INTERFACE", "PORT-INTERFACE", "DATA-INTERFACE"])
    def test_one_stub_on_both_sides_is_the_same_interface(self, model_root: Path, dest: str) -> None:
        # Not *not judged*: one interface on both ends is compatible by identity,
        # even where its abstract DEST hides the kind.
        _interface_stub(model_root, "IForeign", dest=dest)
        _repoint_ports(model_root, "IForeign")

        assert self._ask(model_root) == validator.CompatibilityResult(compatible=True, reason="")

    def test_two_declared_interfaces_answer_as_before(self, model_root: Path) -> None:
        assert self._ask(model_root) == validator.CompatibilityResult(compatible=True, reason="")

    @pytest.mark.parametrize("dest", ["SENDER-RECEIVER-INTERFACE", "PORT-INTERFACE"])
    def test_the_order_of_the_two_ports_does_not_change_the_answer(self, model_root: Path, dest: str) -> None:
        # A connector has a direction, so the reversed pair is a question for the function.
        _interface_stub(model_root, "IForeign", dest=dest)
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))

        forward = self._ask(model_root)
        backward = check_port_compatibility(str(model_root), "ExampleConsumerSWC", "rIn", "ExampleSWC", "pOut")

        assert forward == backward
        assert backward.compatible is True
        assert backward.reason.startswith("not judged: external interface 'IForeign'"), backward.reason


# measures on a stub port, and the ASIL port rules


class TestMeasuresOnAStubPort:
    """The two ASIL port rules judge through a data element's own level; a stub has none.

    Each test first shows the rule firing over the declared interface, so the
    abstention is measured against a finding that exists.
    """

    @staticmethod
    def _rate(model_root: Path) -> None:
        stubs.change(model_root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(asil="A"))
        stubs.change(model_root, _CONSUMER, lambda d: d.update(asil="D"))
        stubs.change(model_root, _PROVIDER, lambda d: d.update(asil="A"))
        stubs.change(model_root, "requirements/REQ_EXAMPLE_001.yaml", lambda d: d.update(asil="A"))

    def test_the_declared_interface_is_judged(self, model_root: Path) -> None:
        self._rate(model_root)

        assert _rules(model_root) == ["safety.consumer-above-element"]

    def test_a_measure_key_is_one_finding_and_the_asil_rules_abstain(self, model_root: Path) -> None:
        self._rate(model_root)
        _interface_stub(model_root, "IForeign")
        _repoint_ports(model_root, "IForeign")
        stubs.change(model_root, _CONSUMER, lambda d: d["ports"][0].update(measures={"value": "end-to-end protection"}))

        records = _records(model_root)

        assert [record.rule for record in records] == ["safety.measure-unknown-element"]
        assert "external interface stub" in records[0].message

    def test_the_asil_rules_abstain_without_a_measure(self, model_root: Path) -> None:
        self._rate(model_root)
        _interface_stub(model_root, "IForeign")
        _repoint_ports(model_root, "IForeign")

        assert _rules(model_root) == []


# Names


class TestTheStubsOwnName:
    def test_an_interface_stubs_name_is_identifier_checked(self, model_root: Path) -> None:
        stubs.put(
            model_root,
            "interfaces/I Foreign.yaml",
            {
                "name": "I Foreign",
                "kind": "external_interface",
                "external_ref": _SR_REF,
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        )

        assert _rules(model_root) == ["identifier.declared"]

    def test_a_type_stubs_name_is_identifier_checked(self, model_root: Path) -> None:
        write_model_element(
            model_root,
            {
                "name": "Foreign T",
                "kind": "external_type",
                "external_ref": _TYPE_REF,
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )

        assert _rules(model_root) == ["identifier.declared"]


class TestStubsClaimNoEmittedName:
    """A stub emits no ``SHORT-NAME``, so it occupies none of the generated forms' namespace."""

    def test_a_stub_beside_an_enums_compu_method(self, model_root: Path) -> None:
        _data_type(model_root, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        _type_stub(model_root, "Gear_CompuMethod")

        assert _rules(model_root) == []

    def test_a_declared_type_there_still_collides(self, model_root: Path) -> None:
        _data_type(model_root, {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        # An occupant of the enum's package: a form collides inside its owner's package only.
        _data_type(model_root, {"name": "Gear_CompuMethod", "kind": "application_type"})

        assert "data-type.compu-method-name-collision" in _rules(model_root)

    @pytest.mark.parametrize("suffix", ["_Unit", "_Compu", "_Range"])
    def test_a_stub_beside_an_application_types_generated_forms(self, model_root: Path, suffix: str) -> None:
        _data_type(
            model_root,
            {
                "name": "Speed",
                "kind": "application_type",
                "unit": "mps",
                "conversion": {"factor": 1.0, "offset": 0.0},
                "range": {"min": 0.0, "max": 100.0},
            },
        )
        _type_stub(model_root, f"Speed{suffix}")

        assert _rules(model_root) == []

    def test_an_interface_stub_named_like_an_init_constant(self, model_root: Path) -> None:
        _interface_stub(model_root, "IExample_value_Init")

        assert _rules(model_root) == []

    def test_a_mapping_set_named_like_a_type_stub(self, model_root: Path) -> None:
        _type_stub(model_root, "Maps")
        write_model_element(model_root, {"name": "Maps", "maps": []}, layer="mapping_sets")

        assert _rules(model_root) == []

    def test_model_identity_still_holds_across_interfaces(self, model_root: Path) -> None:
        stubs.put(
            model_root,
            "interfaces/IExample_stub.yaml",
            {
                "name": "IExample",
                "kind": "external_interface",
                "external_ref": _SR_REF,
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        )

        assert "name.duplicate-interface" in _rules(model_root)

    def test_model_identity_still_holds_across_data_types(self, model_root: Path) -> None:
        stubs.put(
            model_root,
            model_element_storage_name("float32_stub", kind="external_type"),
            {
                "name": "float32",
                "kind": "external_type",
                "external_ref": _TYPE_REF,
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )

        assert "data-type.duplicate-name" in _rules(model_root)


# Pass D


def _one_violation(model_root: Path) -> Any:
    violations = diagnose(check_cross_layer_consistency(str(model_root)))
    assert len(violations) == 1, violations
    return violations[0]


class TestPassDAnswersEveryNewFinding:
    def test_the_interface_stubs_reference_pair(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="NOT-A-PORT-INTERFACE")

        violation = _one_violation(model_root)

        assert violation.error == CONSISTENCY_VIOLATION
        assert [(a.layer, a.element) for a in violation.addresses] == [("interfaces", "IForeign")]

    def test_the_type_stubs_reference_pair(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT", ref="not a path")

        violation = _one_violation(model_root)

        assert violation.error == CONSISTENCY_VIOLATION
        assert [(a.layer, a.element) for a in violation.addresses] == [("implementation_types", "ForeignT")]

    def test_init_on_an_external_type(self, model_root: Path) -> None:
        _type_stub(model_root, "ForeignT")
        _typed_element(model_root, "ForeignT", 1)

        violation = _one_violation(model_root)

        assert violation.error == CONSISTENCY_VIOLATION
        assert [(a.layer, a.element) for a in violation.addresses] == [
            ("interfaces", "IExample"),
            ("implementation_types", "ForeignT"),
        ]

    @pytest.mark.parametrize(
        ("list_name", "entry"),
        [("reads", {"port": "p", "element": "value"}), ("calls", {"port": "p", "operation": "Op"})],
    )
    def test_an_access_through_a_stub_port(self, model_root: Path, list_name: str, entry: dict[str, str]) -> None:
        _interface_stub(model_root, "IForeign")

        def mutate(document: dict[str, Any]) -> None:
            document["ports"].append({"name": "p", "direction": "require", "interface": "IForeign"})
            document["runnables"][0][list_name] = [entry]

        stubs.change(model_root, _CONSUMER, mutate)

        assert _one_violation(model_root).error == BROKEN_REFERENCE

    def test_a_kind_mismatch_across_a_stub(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign", dest="CLIENT-SERVER-INTERFACE")
        stubs.change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))

        assert _one_violation(model_root).error == CONSISTENCY_VIOLATION

    def test_a_measure_on_a_stub_port(self, model_root: Path) -> None:
        _interface_stub(model_root, "IForeign")
        _repoint_ports(model_root, "IForeign")
        stubs.change(model_root, _CONSUMER, lambda d: d["ports"][0].update(measures={"value": "end-to-end protection"}))

        assert _one_violation(model_root).error == CONSISTENCY_VIOLATION

    def test_a_measure_on_a_stub_port_the_stub_lists(self, model_root: Path) -> None:
        """A key the stub lists in its ``data_elements`` is accepted, and no level rule reads it."""
        _interface_stub(model_root, "IForeign")
        stubs.change(model_root, "interfaces/IForeign.yaml", lambda d: d.update(data_elements=["value"]))
        _repoint_ports(model_root, "IForeign")
        stubs.change(model_root, _CONSUMER, lambda d: d["ports"][0].update(measures={"value": "end-to-end protection"}))

        assert list(diagnose(check_cross_layer_consistency(str(model_root)))) == []

    @pytest.mark.parametrize(
        "rule",
        [
            "interface.external-incomplete-reference",
            "data-type.external-incomplete-reference",
            "interface.init-on-external-type",
        ],
    )
    def test_each_new_rule_is_a_consistency_violation(self, rule: str) -> None:
        """The stub's reference points outside the model by construction, so nothing here is a broken reference."""
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION


class TestPassDRefusesAChangeSetThatTypesAnInitByAStub:
    """Through the protocol, end to end: the one new finding an update of a declared element can create."""

    def test_the_change_set_is_refused_and_writes_nothing(self, tmp_path: Path) -> None:
        from swcraft_tools.editing.engine import handle_request
        from swcraft_tools.editing.staging import baseline_token

        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        model_root = project / "model"
        _type_stub(model_root, "ForeignT")
        _typed_element(model_root, "ForeignT")
        assert check_cross_layer_consistency(str(model_root)).consistent
        before = {path: path.read_bytes() for path in model_root.rglob("*.yaml")}

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": baseline_token(model_root),
                "intent": "give the stub-typed element an initial value",
                "operations": [
                    {
                        "operation": "update",
                        "address": {
                            "layer": "interfaces",
                            "element": "IExample",
                            "path": [{"field": "data_elements"}, {"member": "value"}, {"field": "init"}],
                        },
                        "value": 1,
                    }
                ],
            },
            project=project,
        )

        assert response["error"] == "consistency-violation", response
        assert "external data type stub" in response["message"], response
        assert {path: path.read_bytes() for path in model_root.rglob("*.yaml")} == before
