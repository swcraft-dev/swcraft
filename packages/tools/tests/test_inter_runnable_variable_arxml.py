"""The ARXML writer's half of inter-runnable variables and the runnables' accesses to them.

A component's ``variables`` are written in its ``SWC-INTERNAL-BEHAVIOR``:

* each ``explicit`` one as a ``VARIABLE-DATA-PROTOTYPE`` of ``EXPLICIT-INTER-RUNNABLE-VARIABLES``
  (between ``EVENTS`` and ``HANDLE-TERMINATION-AND-RESTART``) and each ``implicit`` one in
  ``IMPLICIT-INTER-RUNNABLE-VARIABLES`` (between that and ``RUNNABLES``) -- ``[TPS_SWCT_01052]``'s two
  roles, one position at 4.2.2 and 4.3.0; a wrapper with no variable is not written;
* holding ``SHORT-NAME``, ``DESC``, ``SW-DATA-DEF-PROPS`` with the calibration access alone,
  ``TYPE-TREF`` with the resolved type's ``DEST``, and ``INIT-VALUE`` with the value **in place**
  (no new ``CONSTANT-SPECIFICATION``).

A runnable's ``reads_variables`` are written in ``READ-LOCAL-VARIABLES``, after the port containers
and before ``SERVER-CALL-POINTS``, and its ``writes_variables`` in ``WRITTEN-LOCAL-VARIABLES``, after
``SYMBOL``: one ``VARIABLE-ACCESS`` each, named by the stated ``name`` or ``RV_``/``WV_`` + the
variable, with one ``LOCAL-VARIABLE-REF`` to the variable of the same behaviour
(``[TPS_SWCT_01521]``, ``[constr_2026]``). No ``SCOPE`` and no ``SW-IMPL-POLICY`` is written.

**The writer refuses, with nothing written, every class ``check-model`` reports** about a variable or
an access, through the validator's own record functions or the kept guards; each negative class is
generated into a fresh directory, which stays empty, and ``check-model`` is asked the same model.
**The warning is not a refusal:** a read of a variable with no ``init`` generates.

**Not reached:** XSD conformance (``test_arxml_xsd_conformance.py``'s
``TestTheInterRunnableVariablesConform``, ``test-xsd``); the importer and component exchange; whether
an RTE generator accepts the output; any model shape outside the ones below -- example-based
evidence, not a proof over every model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from external_stubs_support import change, put
from inter_runnable_variable_support import (
    BEHAVIOUR,
    COMPONENT,
    COMPONENT_FILE,
    FILTERED,
    FRAME,
    GEAR,
    HISTORY,
    LOGGER,
    MONITOR,
    PACKAGE,
    PEAK,
    SPARE,
    WRITER,
    every_variable_shape_model,
    variable_model,
)
from lxml import etree
from swcraft_tools.validator import (
    WARNING_RULES,
    check_cross_layer_consistency,
    check_xml_representable_text,
)
from swcraft_tools.writer.arxml_generation import (
    INTERNAL_BEHAVIOR_SUFFIX,
    READ_VARIABLE_PREFIX,
    WRITE_VARIABLE_PREFIX,
    generate_arxml,
)

NS = {"ar": "http://autosar.org/schema/r4.0"}

BEHAVIOUR_PATH = f"/ComponentTypes/{COMPONENT}/{COMPONENT}{INTERNAL_BEHAVIOR_SUFFIX}"
VERSIONS = ("4.2.2", "4.3.0")


# Helpers


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A copy of the shared fixture, with its root composition: clean."""
    return variable_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _variable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [variable for variable in document["variables"] if variable["name"] == name]
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


def _wrapper(document: etree._Element, tag: str) -> list[etree._Element]:
    return _behaviour(document).xpath(f"ar:{tag}/ar:VARIABLE-DATA-PROTOTYPE", namespaces=NS)


def _prototype(document: etree._Element, name: str) -> etree._Element:
    (node,) = _behaviour(document).xpath(
        "ar:*/ar:VARIABLE-DATA-PROTOTYPE[ar:SHORT-NAME=$name]", namespaces=NS, name=name
    )
    return node


def _runnable(document: etree._Element, name: str) -> etree._Element:
    (node,) = _behaviour(document).xpath(
        "ar:RUNNABLES/ar:RUNNABLE-ENTITY[ar:SHORT-NAME=$name]", namespaces=NS, name=name
    )
    return node


def _accesses(runnable: etree._Element, container: str) -> list[tuple[str, str, str]]:
    """``(SHORT-NAME, LOCAL-VARIABLE-REF text, DEST)`` of every access in *container*."""
    return [
        (
            _short_name(access),
            access.findtext("ar:ACCESSED-VARIABLE/ar:LOCAL-VARIABLE-REF", namespaces=NS),
            access.find("ar:ACCESSED-VARIABLE/ar:LOCAL-VARIABLE-REF", namespaces=NS).get("DEST"),
        )
        for access in runnable.xpath(f"ar:{container}/ar:VARIABLE-ACCESS", namespaces=NS)
    ]


def _path_of(node: etree._Element) -> str:
    """The AUTOSAR path of *node*: its ancestors' ``SHORT-NAME``s and its own."""
    names = [_short_name(ancestor) for ancestor in node.iterancestors() if _short_name(ancestor) is not None]
    return "/" + "/".join([*reversed(names), _short_name(node)])


# The variables


class TestTheRoles:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_behaviour_holds_the_two_wrappers_in_the_schemas_position(
        self, model: Path, tmp_path: Path, version: str
    ) -> None:
        """One order at both versions over what is emitted."""
        assert _children(_behaviour(_generate(model, tmp_path / "out.arxml", version))) == [
            "SHORT-NAME",
            "DATA-TYPE-MAPPING-REFS",
            "EVENTS",
            "EXPLICIT-INTER-RUNNABLE-VARIABLES",
            "HANDLE-TERMINATION-AND-RESTART",
            "IMPLICIT-INTER-RUNNABLE-VARIABLES",
            "RUNNABLES",
            "SUPPORTS-MULTIPLE-INSTANTIATION",
        ]

    def test_each_variable_is_in_its_roles_wrapper_in_declared_order(self, model: Path, tmp_path: Path) -> None:
        """Explicit and implicit are never swapped."""
        document = _generate(model, tmp_path / "out.arxml")

        assert [_short_name(node) for node in _wrapper(document, "EXPLICIT-INTER-RUNNABLE-VARIABLES")] == [PEAK, SPARE]
        assert [_short_name(node) for node in _wrapper(document, "IMPLICIT-INTER-RUNNABLE-VARIABLES")] == [FILTERED]

    def test_swapping_a_variables_access_moves_it_to_the_other_wrapper(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _variable(d, SPARE).update(access="implicit"))
        document = _generate(model, tmp_path / "out.arxml")

        assert [_short_name(node) for node in _wrapper(document, "EXPLICIT-INTER-RUNNABLE-VARIABLES")] == [PEAK]
        assert [_short_name(node) for node in _wrapper(document, "IMPLICIT-INTER-RUNNABLE-VARIABLES")] == [
            FILTERED,
            SPARE,
        ]

    def test_a_role_with_no_variable_writes_no_wrapper(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _variable(d, FILTERED).update(access="explicit"))

        assert "IMPLICIT-INTER-RUNNABLE-VARIABLES" not in _children(
            _behaviour(_generate(model, tmp_path / "out.arxml"))
        )


class TestThePrototype:
    def test_every_stated_field_is_written_in_the_schemas_order(self, model: Path, tmp_path: Path) -> None:
        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), FILTERED)

        assert _children(prototype) == ["SHORT-NAME", "DESC", "SW-DATA-DEF-PROPS", "TYPE-TREF", "INIT-VALUE"]

    def test_the_description_is_the_desc(self, model: Path, tmp_path: Path) -> None:
        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), FILTERED)

        assert prototype.findtext("ar:DESC/ar:L-2", namespaces=NS) == (
            "Speed after the 10 ms filter, read by the 50 ms and the 100 ms runnables."
        )

    def test_the_calibration_access_alone_is_the_sw_data_def_props(self, model: Path, tmp_path: Path) -> None:
        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), FILTERED)
        (conditional,) = prototype.xpath(
            "ar:SW-DATA-DEF-PROPS/ar:SW-DATA-DEF-PROPS-VARIANTS/ar:SW-DATA-DEF-PROPS-CONDITIONAL", namespaces=NS
        )

        assert _children(conditional) == ["SW-CALIBRATION-ACCESS"]
        assert conditional.findtext("ar:SW-CALIBRATION-ACCESS", namespaces=NS) == "READ-ONLY"

    @pytest.mark.parametrize(("literal", "text"), [("not_accessible", "NOT-ACCESSIBLE"), ("read_write", "READ-WRITE")])
    def test_a_stated_calibration_access_is_written_exactly(
        self, model: Path, tmp_path: Path, literal: str, text: str
    ) -> None:
        """The explicit default included: the writer writes what the model states."""
        _component(model, lambda d: _variable(d, SPARE).update(sw_calibration_access=literal))
        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), SPARE)

        assert prototype.findtext(".//ar:SW-CALIBRATION-ACCESS", namespaces=NS) == text

    def test_a_variable_stating_neither_writes_no_desc_and_no_props(self, model: Path, tmp_path: Path) -> None:
        """No default is added: an absent calibration access is the standard's ``notAccessible``."""
        assert _children(_prototype(_generate(model, tmp_path / "out.arxml"), SPARE)) == ["SHORT-NAME", "TYPE-TREF"]

    def test_an_application_type_is_referenced_on_the_application_side(self, model: Path, tmp_path: Path) -> None:
        reference = _prototype(_generate(model, tmp_path / "out.arxml"), FILTERED).find("ar:TYPE-TREF", namespaces=NS)

        assert (reference.text, reference.get("DEST")) == (
            "/DataTypes/Application/VehicleSpeed",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        )

    def test_an_implementation_type_is_referenced_on_the_implementation_side(self, model: Path, tmp_path: Path) -> None:
        reference = _prototype(_generate(model, tmp_path / "out.arxml"), PEAK).find("ar:TYPE-TREF", namespaces=NS)

        assert (reference.text, reference.get("DEST")) == (
            "/DataTypes/Implementation/uint16",
            "IMPLEMENTATION-DATA-TYPE",
        )

    def test_an_external_type_stub_is_referenced_at_its_external_path(self, model: Path, tmp_path: Path) -> None:
        """The shape the importer mints for a type the file declares nowhere: a stub, and no ``init``."""
        put(
            model,
            "data_types/implementation/Foreign_T.yaml",
            {
                "name": "Foreign_T",
                "kind": "external_type",
                "external_ref": "/Foreign/Types/Foreign_T",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        _component(model, lambda d: _variable(d, SPARE).update(implementation_type="Foreign_T"))

        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), SPARE)
        reference = prototype.find("ar:TYPE-TREF", namespaces=NS)

        assert (reference.text, reference.get("DEST")) == ("/Foreign/Types/Foreign_T", "IMPLEMENTATION-DATA-TYPE")
        assert prototype.find("ar:INIT-VALUE", namespaces=NS) is None

    def test_the_type_reference_resolves_inside_the_document(self, model: Path, tmp_path: Path) -> None:
        document = _generate(model, tmp_path / "out.arxml")
        paths = {_path_of(node) for node in document.iter() if _short_name(node) is not None}

        for name in (FILTERED, PEAK, SPARE):
            assert _prototype(document, name).findtext("ar:TYPE-TREF", namespaces=NS) in paths

    def test_no_sw_impl_policy_is_written(self, tmp_path: Path) -> None:
        document = _generate(every_variable_shape_model(tmp_path / "model"), tmp_path / "out.arxml")

        assert document.xpath("//ar:SW-IMPL-POLICY", namespaces=NS) == []


class TestTheInitValue:
    """The init value: in place, in the application value form, by the type and the position."""

    @pytest.fixture
    def document(self, tmp_path: Path) -> etree._Element:
        return _generate(every_variable_shape_model(tmp_path / "model"), tmp_path / "out.arxml")

    def _value(self, document: etree._Element, name: str) -> etree._Element:
        (value,) = _prototype(document, name).find("ar:INIT-VALUE", namespaces=NS)
        return value

    def test_a_scalar_of_an_application_type_is_an_application_value(self, document: etree._Element) -> None:
        value = self._value(document, FILTERED)

        assert _tag(value) == "APPLICATION-VALUE-SPECIFICATION"
        assert value.findtext("ar:CATEGORY", namespaces=NS) == "VALUE"
        assert value.findtext("ar:SW-VALUE-CONT/ar:SW-VALUES-PHYS/ar:V", namespaces=NS) == "0"

    def test_a_scalar_of_an_implementation_type_is_a_numerical_value(self, document: etree._Element) -> None:
        value = self._value(document, PEAK)

        assert _tag(value) == "NUMERICAL-VALUE-SPECIFICATION"
        assert value.findtext("ar:VALUE", namespaces=NS) == "0"

    def test_an_enumeration_literal_is_its_name(self, document: etree._Element) -> None:
        value = self._value(document, GEAR)

        assert _tag(value) == "APPLICATION-VALUE-SPECIFICATION"
        assert value.findtext("ar:SW-VALUE-CONT/ar:SW-VALUES-PHYS/ar:VT", namespaces=NS) == "D"

    def test_a_record_is_one_labelled_member_per_element_in_order(self, document: etree._Element) -> None:
        value = self._value(document, FRAME)

        assert _tag(value) == "RECORD-VALUE-SPECIFICATION"
        assert [
            (member.findtext("ar:SHORT-LABEL", namespaces=NS), member.findtext("ar:VALUE", namespaces=NS))
            for member in value.find("ar:FIELDS", namespaces=NS)
        ] == [("count", "1"), ("peak", "2")]

    def test_an_array_is_one_member_per_index(self, document: etree._Element) -> None:
        value = self._value(document, HISTORY)

        assert _tag(value) == "ARRAY-VALUE-SPECIFICATION"
        assert [member.findtext("ar:VALUE", namespaces=NS) for member in value.find("ar:ELEMENTS", namespaces=NS)] == [
            "3",
            "4",
        ]

    def test_a_variable_without_an_init_writes_no_init_value(self, document: etree._Element) -> None:
        assert _prototype(document, SPARE).find("ar:INIT-VALUE", namespaces=NS) is None

    def test_no_constant_specification_is_added(self, model: Path, tmp_path: Path) -> None:
        """The value is in place: the fixture declares no data element, so the document has no constant at all."""
        document = _generate(model, tmp_path / "out.arxml")

        assert document.xpath("//ar:CONSTANT-SPECIFICATION | //ar:CONSTANT-REFERENCE", namespaces=NS) == []


# The accesses


class TestTheAccesses:
    def test_a_read_is_a_variable_access_in_read_local_variables(self, model: Path, tmp_path: Path) -> None:
        monitor = _runnable(_generate(model, tmp_path / "out.arxml"), MONITOR)

        assert _accesses(monitor, "READ-LOCAL-VARIABLES") == [
            (f"{READ_VARIABLE_PREFIX}{FILTERED}", f"{BEHAVIOUR_PATH}/{FILTERED}", "VARIABLE-DATA-PROTOTYPE"),
            (f"{READ_VARIABLE_PREFIX}{PEAK}", f"{BEHAVIOUR_PATH}/{PEAK}", "VARIABLE-DATA-PROTOTYPE"),
        ]

    def test_a_write_is_a_variable_access_in_written_local_variables(self, model: Path, tmp_path: Path) -> None:
        document = _generate(model, tmp_path / "out.arxml")

        assert _accesses(_runnable(document, WRITER), "WRITTEN-LOCAL-VARIABLES") == [
            (f"{WRITE_VARIABLE_PREFIX}{FILTERED}", f"{BEHAVIOUR_PATH}/{FILTERED}", "VARIABLE-DATA-PROTOTYPE"),
        ]
        assert _accesses(_runnable(document, MONITOR), "WRITTEN-LOCAL-VARIABLES") == [
            (f"{WRITE_VARIABLE_PREFIX}{PEAK}", f"{BEHAVIOUR_PATH}/{PEAK}", "VARIABLE-DATA-PROTOTYPE"),
        ]

    def test_a_stated_access_name_is_written_as_stated(self, model: Path, tmp_path: Path) -> None:
        logger = _runnable(_generate(model, tmp_path / "out.arxml"), LOGGER)

        assert [name for name, _ref, _dest in _accesses(logger, "READ-LOCAL-VARIABLES")] == ["RV_Logged"]

    def test_a_stated_write_name_is_written_as_stated(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, WRITER)["writes_variables"][0].update(name="WV_Filtered"))
        writer = _runnable(_generate(model, tmp_path / "out.arxml"), WRITER)

        assert [name for name, _ref, _dest in _accesses(writer, "WRITTEN-LOCAL-VARIABLES")] == ["WV_Filtered"]

    def test_an_access_holds_only_its_name_and_its_local_variable_ref(self, model: Path, tmp_path: Path) -> None:
        """No ``SCOPE``, no IREF form, no in-implementation-type form."""
        document = _generate(model, tmp_path / "out.arxml")
        accesses = document.xpath("//ar:READ-LOCAL-VARIABLES/* | //ar:WRITTEN-LOCAL-VARIABLES/*", namespaces=NS)

        assert len(accesses) == 5
        for access in accesses:
            assert _children(access) == ["SHORT-NAME", "ACCESSED-VARIABLE"]
            assert _children(access.find("ar:ACCESSED-VARIABLE", namespaces=NS)) == ["LOCAL-VARIABLE-REF"]
        assert document.xpath("//ar:SCOPE", namespaces=NS) == []

    def test_every_local_variable_ref_resolves_to_a_variable_of_the_same_behaviour(
        self, model: Path, tmp_path: Path
    ) -> None:
        """The reference names a prototype of the runnable's own behaviour."""
        document = _generate(model, tmp_path / "out.arxml")
        behaviour = _behaviour(document)
        prototypes = {
            _path_of(node): node for node in behaviour.xpath("ar:*/ar:VARIABLE-DATA-PROTOTYPE", namespaces=NS)
        }

        references = document.xpath("//ar:LOCAL-VARIABLE-REF", namespaces=NS)
        assert references
        for reference in references:
            assert reference.text in prototypes

    def test_the_untouched_variable_is_accessed_by_no_runnable(self, model: Path, tmp_path: Path) -> None:
        document = _generate(model, tmp_path / "out.arxml")

        assert not any(
            reference.text.endswith(f"/{SPARE}")
            for reference in document.xpath("//ar:LOCAL-VARIABLE-REF", namespaces=NS)
        )

    def test_a_runnable_without_accesses_writes_neither_container(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, LOGGER).pop("reads_variables"))
        logger = _runnable(_generate(model, tmp_path / "out.arxml"), LOGGER)

        assert _children(logger) == ["SHORT-NAME", "MINIMUM-START-INTERVAL", "CAN-BE-INVOKED-CONCURRENTLY", "SYMBOL"]


class TestTheRunnableOrder:
    """``READ-LOCAL-VARIABLES`` before ``SERVER-CALL-POINTS``, ``WRITTEN-LOCAL-VARIABLES`` after ``SYMBOL``."""

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_containers_sit_where_the_schema_puts_them(self, tmp_path: Path, version: str) -> None:
        document = _generate(every_variable_shape_model(tmp_path / "model"), tmp_path / "out.arxml", version)

        assert _children(_runnable(document, MONITOR)) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "READ-LOCAL-VARIABLES",
            "SERVER-CALL-POINTS",
            "SYMBOL",
            "WRITTEN-LOCAL-VARIABLES",
        ]

    def test_with_no_call_point_the_reads_follow_the_port_containers(self, tmp_path: Path) -> None:
        root = every_variable_shape_model(tmp_path / "model")
        _component(root, lambda d: _runnable_entry(d, MONITOR).pop("calls"))

        assert _children(_runnable(_generate(root, tmp_path / "out.arxml"), MONITOR))[3:] == [
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "READ-LOCAL-VARIABLES",
            "SYMBOL",
            "WRITTEN-LOCAL-VARIABLES",
        ]

    def test_with_only_a_call_point_the_reads_precede_it(self, tmp_path: Path) -> None:
        root = every_variable_shape_model(tmp_path / "model")
        _component(root, lambda d: _runnable_entry(d, MONITOR).pop("reads"))

        assert _children(_runnable(_generate(root, tmp_path / "out.arxml"), MONITOR))[3:] == [
            "READ-LOCAL-VARIABLES",
            "SERVER-CALL-POINTS",
            "SYMBOL",
            "WRITTEN-LOCAL-VARIABLES",
        ]


class TestTheStatedPlacement:
    def test_the_local_variable_ref_follows_a_stated_package_and_behaviour_name(self, tmp_path: Path) -> None:
        document = _generate(every_variable_shape_model(tmp_path / "model"), tmp_path / "out.arxml")
        expected = f"/ComponentTypes/{PACKAGE}/{COMPONENT}/{BEHAVIOUR}"

        assert _path_of(_behaviour(document)) == expected
        assert [
            reference for _name, reference, _dest in _accesses(_runnable(document, WRITER), "WRITTEN-LOCAL-VARIABLES")
        ] == [f"{expected}/{FILTERED}"]
        assert _path_of(_prototype(document, FILTERED)) == f"{expected}/{FILTERED}"


# The refusal matrix: every class check-model reports


def _unknown_variable(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, LOGGER)["reads_variables"].append({"variable": "Missing"}))


def _undeclared_type(root: Path) -> None:
    _component(root, lambda d: _variable(d, SPARE).update(implementation_type="Nowhere_T"))


def _pointer_type(root: Path) -> None:
    put(root, "data_types/implementation/Ptr_T.yaml", {"name": "Ptr_T", "kind": "pointer", "target": "uint8"})
    _component(root, lambda d: _variable(d, SPARE).update(implementation_type="Ptr_T"))


def _pointer_through_a_reference_chain(root: Path) -> None:
    put(root, "data_types/implementation/Ptr_T.yaml", {"name": "Ptr_T", "kind": "pointer", "target": "uint8"})
    put(
        root,
        "data_types/implementation/Ptr_Alias.yaml",
        {"name": "Ptr_Alias", "kind": "reference_type", "base": "Ptr_T"},
    )
    _component(root, lambda d: _variable(d, SPARE).update(implementation_type="Ptr_Alias"))


def _pointer_through_a_mapping_set(root: Path) -> None:
    put(root, "data_types/implementation/Ptr_T.yaml", {"name": "Ptr_T", "kind": "pointer", "target": "uint8"})
    change(root, "data_types/mapping_sets/SpeedMapping.yaml", lambda d: d["maps"][0].update(implementation="Ptr_T"))


def _init_shape(root: Path) -> None:
    _component(root, lambda d: _variable(d, PEAK).update(init={"a": 1}))


def _init_out_of_range(root: Path) -> None:
    put(
        root,
        "data_types/implementation/Small_T.yaml",
        {"name": "Small_T", "kind": "value_type", "base": "uint8_base", "value_max": 3},
    )
    _component(root, lambda d: _variable(d, PEAK).update(implementation_type="Small_T", init=9))


def _init_on_an_external_type(root: Path) -> None:
    put(
        root,
        "data_types/implementation/Foreign_T.yaml",
        {
            "name": "Foreign_T",
            "kind": "external_type",
            "external_ref": "/Foreign/Foreign_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        },
    )
    _component(root, lambda d: _variable(d, PEAK).update(implementation_type="Foreign_T"))


def _unmapped_application_type(root: Path) -> None:
    put(root, "data_types/application/Torque.yaml", {"name": "Torque", "kind": "application_type"})

    def mutate(document: dict[str, Any]) -> None:
        variable = _variable(document, SPARE)
        variable.pop("implementation_type")
        variable["application_type"] = "Torque"

    _component(root, mutate)


def _named_like_a_runnable(root: Path) -> None:
    _component(root, lambda d: _variable(d, SPARE).update(name=LOGGER))


def _named_like_a_runnable_but_for_case(root: Path) -> None:
    _component(root, lambda d: _variable(d, SPARE).update(name=LOGGER.lower()))


def _named_like_an_event(root: Path) -> None:
    _component(root, lambda d: _variable(d, SPARE).update(name=f"TMT_{WRITER}"))


def _stated_access_name_equal_to_a_derived_one(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, MONITOR)["writes_variables"][0].update(name=f"RV_{FILTERED}"))


def _illegal_variable_name(root: Path) -> None:
    _component(root, lambda d: _variable(d, SPARE).update(name="spare counter"))


def _illegal_stated_access_name(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, LOGGER)["reads_variables"][0].update(name="logged speed"))


def _overlong_derived_name(list_name: str) -> Any:
    def mutate(root: Path) -> None:
        long_name = "V" * 126

        def edit(document: dict[str, Any]) -> None:
            _variable(document, SPARE).update(name=long_name, init=0)
            _runnable_entry(document, WRITER).setdefault(list_name, []).append({"variable": long_name})

        _component(root, edit)

    return mutate


def _variables_on_an_external_stub(root: Path) -> None:
    put(
        root,
        "swc/chassis/Foreign.yaml",
        {
            "name": "Foreign",
            "external": True,
            "external_ref": "/Foreign/Foreign",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "variables": [{"name": "Hidden", "access": "explicit", "implementation_type": "uint8"}],
        },
    )


#: Each negative class, the change that produces it, and the rule ``check-model``
#: reports it under. The writer is asked the same model and must refuse it.
REFUSED = {
    "unknown-variable": (_unknown_variable, "swc.variable-access.unknown-variable"),
    "undeclared-type": (_undeclared_type, "swc.variable.undeclared-type"),
    "pointer-type": (_pointer_type, "swc.variable.pointer-type"),
    "pointer-through-a-reference-chain": (_pointer_through_a_reference_chain, "swc.variable.pointer-type"),
    "pointer-through-a-mapping-set": (_pointer_through_a_mapping_set, "swc.variable.pointer-type"),
    "init-shape": (_init_shape, "interface.init-shape-mismatch"),
    "init-out-of-range": (_init_out_of_range, "interface.init-out-of-range"),
    "init-on-an-external-type": (_init_on_an_external_type, "interface.init-on-external-type"),
    "unmapped-application-type": (_unmapped_application_type, "data-type.application-type-unmapped"),
    "named-like-a-runnable": (_named_like_a_runnable, "name.name-space-collision"),
    "named-like-a-runnable-but-for-case": (_named_like_a_runnable_but_for_case, "name.name-space-collision"),
    "named-like-an-event": (_named_like_an_event, "name.name-space-collision"),
    "stated-access-name-collision": (_stated_access_name_equal_to_a_derived_one, "swc.access.name-collision"),
    "illegal-variable-name": (_illegal_variable_name, "identifier.declared"),
    "illegal-stated-access-name": (_illegal_stated_access_name, "identifier.declared"),
    "overlong-RV-name": (_overlong_derived_name("reads_variables"), "identifier.generated"),
    "overlong-WV-name": (_overlong_derived_name("writes_variables"), "identifier.generated"),
    "variables-on-an-external-stub": (_variables_on_an_external_stub, "swc.external-with-ports"),
}

#: The classes the writer refuses in the validator's words: its message carries every finding verbatim.
SHARED_PREDICATE = frozenset(
    {
        "unknown-variable",
        "undeclared-type",
        "pointer-type",
        "pointer-through-a-reference-chain",
        "pointer-through-a-mapping-set",
        "init-shape",
        "init-out-of-range",
        "init-on-an-external-type",
        "named-like-a-runnable",
        "named-like-a-runnable-but-for-case",
        "named-like-an-event",
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
    def test_check_model_reports_the_class(self, case: str, model: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        result = check_cross_layer_consistency(str(model))

        assert rule in {record.rule for record in result.records}
        assert rule not in WARNING_RULES

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_generate_arxml_refuses_it_and_writes_nothing(self, case: str, model: Path, tmp_path: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        message = _refusal(model, tmp_path)

        if case in SHARED_PREDICATE:
            findings = [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]
            assert findings, "the case must produce a finding, or the loop below proves nothing"
            for finding in findings:
                assert finding.message in message

    def test_each_refusal_names_its_offender(self, model: Path, tmp_path: Path) -> None:
        """The classes refused by a kept guard name the offending name, as the gate does."""
        expectations = {
            "unmapped-application-type": "'Torque'",
            "stated-access-name-collision": f"'RV_{FILTERED}'",
            "illegal-variable-name": "'spare counter'",
            "illegal-stated-access-name": "'logged speed'",
            "overlong-RV-name": f"'RV_{'V' * 126}' (129 characters)",
            "overlong-WV-name": f"'WV_{'V' * 126}' (129 characters)",
            "variables-on-an-external-stub": "external stub 'Foreign' declares variables",
        }
        for case, offender in expectations.items():
            root = variable_model(tmp_path / case / "model")
            REFUSED[case][0](root)

            assert offender in _refusal(root, tmp_path / case), case

    def test_a_derived_name_on_its_budget_generates(self, model: Path, tmp_path: Path) -> None:
        on_budget = "V" * 125

        def mutate(document: dict[str, Any]) -> None:
            _variable(document, SPARE).update(name=on_budget, init=0)
            _runnable_entry(document, MONITOR)["reads_variables"].append({"variable": on_budget})

        _component(model, mutate)
        monitor = _runnable(_generate(model, tmp_path / "out.arxml"), MONITOR)

        assert f"{READ_VARIABLE_PREFIX}{on_budget}" in [name for name, *_ in _accesses(monitor, "READ-LOCAL-VARIABLES")]

    def test_an_xml_unrepresentable_description_is_refused_and_writes_nothing(
        self, model: Path, tmp_path: Path
    ) -> None:
        _component(model, lambda d: _variable(d, SPARE).update(description="bell \x07 here"))
        (record,) = check_xml_representable_text(str(model)).records
        assert record.rule == "text.xml-unrepresentable-character"

        message = _refusal(model, tmp_path)

        assert f"variable '{SPARE}'" in message
        assert "U+0007" in message


class TestTheWarningIsNoRefusal:
    def test_a_read_of_a_variable_without_an_init_generates(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _variable(d, FILTERED).pop("init"))
        result = check_cross_layer_consistency(str(model))
        assert result.consistent
        assert [record.rule for record in result.warnings] == ["swc.variable.read-without-init"]

        prototype = _prototype(_generate(model, tmp_path / "out.arxml"), FILTERED)

        assert prototype.find("ar:INIT-VALUE", namespaces=NS) is None


class TestTheFixtureIsClean:
    def test_check_model_reports_nothing_about_either_model(self, tmp_path: Path) -> None:
        for name, build in (("plain", variable_model), ("every-shape", every_variable_shape_model)):
            result = check_cross_layer_consistency(str(build(tmp_path / name)))
            assert (result.consistent, result.issues, result.warnings) == (True, [], ()), name
