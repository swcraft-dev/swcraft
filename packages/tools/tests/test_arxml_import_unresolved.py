"""The unresolved-reference policy over ``swcraft import model``: the element is kept, the path is recorded.

One test per row of the design's role table -- which reference a dependent element may carry
unresolved, which two roles keep their earlier answer, and the recovery behind a kept port. Most
run on the shape corpus (``fixtures/arxml_import_shapes/``) through
``swcraft_tools.arxml_reader.read_arxml``; the rest build small ``ImportModel``s inline.

Does not cover:

* the rendered report beyond the category's own lines; the summary counts, the ``--dry-run``
  identity and the path-free rule are ``test_arxml_import_cli.py``'s;
* ``CAUSE_SELECTION``'s producer, the ``--packages`` narrowing;
* a target the export **does** declare and SWCraft did not import for a reason of its own: not
  one of the three causes, it keeps the chain skip;
* the real tool exports held privately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from arxml_import_shapes_support import raw_element, read_shape_model, shape_path
from swcraft_tools.arxml_import import (
    map_compositions,
    map_data_types,
    map_interfaces,
    map_swcs,
)
from swcraft_tools.arxml_import.reconcile import reconcile_reserved_names
from swcraft_tools.arxml_import.swc_mapping import finish_stub_members
from swcraft_tools.arxml_import.unresolved import (
    CAUSE_ABSENT,
    UnresolvedReferences,
)
from swcraft_tools.arxml_reader import (
    AssemblyConnectorInfo,
    ComponentPrototypeInfo,
    CompositionInfo,
    ConnectorPortRef,
    ImportModel,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwcInfo,
    SwcPortInfo,
    read_arxml,
)
from swcraft_tools.cli import main
from swcraft_tools.schema import (
    ArrayDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    ValueDataType,
)

ROLES = "unresolved_reference_roles.arxml"


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """Scaffold a fresh SWCraft project through the real ``swcraft init`` CLI path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _map(model: ImportModel):
    """Every layer of *model* mapped in ``run_import``'s order, threaded with one :class:`UnresolvedReferences`."""
    unresolved = UnresolvedReferences(model)
    forms: list = []
    data_types, registry, dt_skips = map_data_types(model, unresolved=unresolved, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, unresolved=unresolved, elements=forms)
    swcs, swc_skips = map_swcs(
        model, interfaces, iface_skip_log=iface_skips, unresolved=unresolved, port_inits={}, variable_types={}
    )
    mapped, comp_skips = map_compositions(
        model, swcs, interfaces, iface_skips, registry, unresolved=unresolved, swc_skip_log=swc_skips
    )
    return {
        "data_types": data_types,
        "interfaces": interfaces,
        "swcs": swcs,
        "compositions": mapped.compositions,
        "connectors": mapped.connectors,
        "stubs": mapped.stubs,
        "registry": registry,
        "unresolved": unresolved,
        "skips": [*dt_skips, *iface_skips, *swc_skips, *comp_skips],
    }


def _record(unresolved: UnresolvedReferences, target: str):
    """The one record for *target*."""
    matches = [record for record in unresolved.references if record.target == target]
    assert len(matches) == 1, f"expected one record for {target!r}, found {[r.target for r in unresolved.references]}"
    return matches[0]


def _by_name(elements):
    return {element.name: element for element in elements}


class TestTheRoleTableRowsAreKept:
    """One case per row of the role table that the unresolved-reference policy changes.

    Each asserts the referring element is mapped, its field holds the target path exactly as the
    export spelled it, and one record carries the target, its ``DEST`` and the referrer.
    """

    def test_a_data_elements_type(self):
        mapped = _map(read_shape_model(ROLES))

        interface = _by_name(mapped["interfaces"])["ILevel"]
        assert isinstance(interface, SRInterface)
        assert [(e.name, e.implementation_type) for e in interface.data_elements] == [
            ("level", "/Acme/Chassis/Types/AbsentElementType")
        ]

        record = _record(mapped["unresolved"], "/Acme/Chassis/Types/AbsentElementType")
        assert (record.cause, record.dest) == (CAUSE_ABSENT, "IMPLEMENTATION-DATA-TYPE")
        assert [(r.path, r.field, r.member) for r in record.referrers] == [
            ("/Acme/Chassis/Interfaces/ILevel", "interfaces.data_element.implementation_type", "level")
        ]

    def test_an_arguments_type(self):
        mapped = _map(read_shape_model("unresolved_argument_type.arxml"))

        interface = _by_name(mapped["interfaces"])["ICalibrationService"]
        operation = interface.operations[0]
        assert [(a.name, a.implementation_type) for a in operation.arguments] == [
            ("request", "Level"),
            ("result", "/Acme/Chassis/Types/AbsentResultType"),
        ]

        record = _record(mapped["unresolved"], "/Acme/Chassis/Types/AbsentResultType")
        assert record.cause == CAUSE_ABSENT
        assert [(r.field, r.member) for r in record.referrers] == [
            ("interfaces.argument.implementation_type", "result")
        ]

    def test_a_record_fields_type(self):
        mapped = _map(read_shape_model(ROLES))

        record_type = _by_name(mapped["data_types"])["StatusRecord"]
        assert isinstance(record_type, RecordDataType)
        assert [(f.name, f.type) for f in record_type.fields] == [("flag", "/Acme/Chassis/AppTypes/AbsentFieldType")]

        record = _record(mapped["unresolved"], "/Acme/Chassis/AppTypes/AbsentFieldType")
        assert (record.cause, record.dest) == (CAUSE_ABSENT, "APPLICATION-PRIMITIVE-DATA-TYPE")
        assert [r.field for r in record.referrers] == ["application_types.record_field.type"]

    def test_an_array_elements_type(self):
        mapped = _map(read_shape_model(ROLES))

        array_type = _by_name(mapped["data_types"])["StatusArray"]
        assert isinstance(array_type, ArrayDataType)
        assert (array_type.element_type, array_type.length) == ("/Acme/Chassis/AppTypes/AbsentItemType", 4)

        record = _record(mapped["unresolved"], "/Acme/Chassis/AppTypes/AbsentItemType")
        assert [r.field for r in record.referrers] == ["application_types.array.element_type"]

    def test_a_value_types_base(self):
        mapped = _map(read_shape_model(ROLES))

        value_type = _by_name(mapped["data_types"])["Level"]
        assert isinstance(value_type, ValueDataType)
        assert value_type.base == "/Vendor/BaseTypes/Word"

        record = _record(mapped["unresolved"], "/Vendor/BaseTypes/Word")
        assert (record.cause, record.dest) == (CAUSE_ABSENT, "SW-BASE-TYPE")
        assert [(r.attribute, r.field) for r in record.referrers] == [
            ("BASE-TYPE-REF", "implementation_types.value_type.base")
        ]

    def test_a_reference_types_base(self):
        mapped = _map(read_shape_model("unresolved_implementation_type_reference.arxml"))

        reference_type = _by_name(mapped["data_types"])["SampleAlias"]
        assert isinstance(reference_type, ReferenceDataType)
        assert reference_type.base == "/Vendor/Types/BaseSample"

        record = _record(mapped["unresolved"], "/Vendor/Types/BaseSample")
        assert (record.cause, record.dest) == (CAUSE_ABSENT, "IMPLEMENTATION-DATA-TYPE")
        assert [(r.attribute, r.field) for r in record.referrers] == [
            ("IMPLEMENTATION-DATA-TYPE-REF", "implementation_types.reference_type.base")
        ]

    def test_a_component_ports_interface(self):
        mapped = _map(read_shape_model(ROLES))

        swc = _by_name([imported.swc for imported in mapped["swcs"]])["ReporterSwc"]
        assert [(p.name, p.direction, p.interface) for p in swc.ports] == [
            ("pStatus", "provide", "/Acme/Chassis/Interfaces/AbsentProvidedInterface")
        ]

        record = _record(mapped["unresolved"], "/Acme/Chassis/Interfaces/AbsentProvidedInterface")
        assert (record.cause, record.dest) == (CAUSE_ABSENT, "SENDER-RECEIVER-INTERFACE")
        assert [(r.tag, r.attribute, r.field) for r in record.referrers] == [
            ("P-PORT-PROTOTYPE", "PROVIDED-INTERFACE-TREF", "swc.port.interface")
        ]

    def test_a_composition_boundary_ports_interface(self):
        mapped = _map(read_shape_model(ROLES))

        composition = _by_name(mapped["compositions"])["Root"]
        assert [(p.name, p.direction, p.interface) for p in composition.ports] == [
            ("pOuterStatus", "provide", "/Acme/Chassis/Interfaces/AbsentOuterInterface")
        ]

        record = _record(mapped["unresolved"], "/Acme/Chassis/Interfaces/AbsentOuterInterface")
        assert [r.field for r in record.referrers] == ["compositions.composition_port.interface"]

    def test_every_reference_is_represented_once(self):
        """The corpus's six unresolved references give six records, each recorded once."""
        mapped = _map(read_shape_model(ROLES))

        assert len(mapped["unresolved"].references) == 6
        assert mapped["unresolved"].total_referrers == 6
        assert {record.cause for record in mapped["unresolved"].references} == {CAUSE_ABSENT}


class TestTheTwoUnchangedRows:
    """The two rows the design marks *unchanged*: the first still is, and contract 7.6.0 changed the second."""

    def test_a_prototype_of_an_absent_type_still_mints_the_external_stub(self):
        mapped = _map(read_shape_model("prototype_of_an_absent_type.arxml"))

        stubs = [imported.swc for imported in mapped["stubs"]]
        assert [(s.name, s.external_ref, s.external_dest) for s in stubs] == [
            ("RemoteSwc_ext0", "/Vendor/Components/RemoteSwc", "APPLICATION-SW-COMPONENT-TYPE")
        ]
        assert stubs[0].ports == []
        # The stub carries the reference itself, so the policy mints no record for it
        # and no second stub kind is invented.
        assert [r.target for r in mapped["unresolved"].references] == []

    def test_a_connector_on_a_stub_port_is_kept_since_contract_7_6_0(self):
        """A connector on a stub's port is kept, and the policy mints no record for either reference.

        The end names a port under the prototype's own ``TYPE-TREF``, so the stub mints that port
        by name and ``DEST`` and answers the reference itself.
        """
        mapped = _map(read_shape_model("connector_to_a_stub_port.arxml"))

        assert [d.name for c in mapped["compositions"] for d in c.delegations] == ["DEL_Local"]
        assert [(c.name, c.from_.swc, c.from_.port) for c in mapped["connectors"]] == [
            ("Assembly_Remote_to_Local", "RemoteSwc_ext0", "pLevel")
        ]
        assert [entry.reason for entry in mapped["skips"] if "Assembly_Remote_to_Local" in entry.path] == []
        (stub,) = [imported.swc for imported in mapped["stubs"]]
        assert [(p.name, p.direction, p.interface) for p in stub.ports] == [("pLevel", "provide", None)]
        assert [r.target for r in mapped["unresolved"].references] == []


class TestADeclaredButSkippedTargetIsNotAnUnresolvedReference:
    """A target the export declares and SWCraft did not import keeps its chain skip, and mints no record.

    *The export declares no such element* is one of the category's three causes; a declared path is not.
    """

    def test_a_port_onto_an_interface_the_importer_skipped(self):
        model = ImportModel(source_path="inline")
        model.path_index["/Acme/Interfaces/IEmpty"] = raw_element("/Acme/Interfaces/IEmpty")
        model.sr_interfaces.append(
            SRInterfaceInfo(path="/Acme/Interfaces/IEmpty", name="IEmpty", description="", data_elements=[])
        )
        model.swcs.append(
            SwcInfo(
                path="/Acme/Comps/Swc",
                name="Swc",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Acme/Comps/Swc/rEmpty",
                        name="rEmpty",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref="/Acme/Interfaces/IEmpty",
                        description="",
                    )
                ],
                runnables=[],
                events=[],
            )
        )

        mapped = _map(model)

        assert mapped["interfaces"] == []
        assert [imported.swc.ports for imported in mapped["swcs"]] == [[]]
        assert [r.target for r in mapped["unresolved"].references] == []
        assert any("unresolved reference /Acme/Interfaces/IEmpty" in entry.reason for entry in mapped["skips"])

    def test_a_port_onto_a_declaration_kind_the_reader_does_not_recognize(self, tmp_path: Path):
        """A `MODE-SWITCH-INTERFACE` is a declaration the reader refuses, not an absent one.

        The reader records it without indexing it, so a policy reading the path index alone would
        report the export as declaring nothing there.
        """
        source = tmp_path / "mode_switch.arxml"
        source.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">\n'
            "  <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Acme</SHORT-NAME><AR-PACKAGES>\n"
            "    <AR-PACKAGE><SHORT-NAME>Interfaces</SHORT-NAME><ELEMENTS>\n"
            "      <MODE-SWITCH-INTERFACE><SHORT-NAME>IMode</SHORT-NAME></MODE-SWITCH-INTERFACE>\n"
            "    </ELEMENTS></AR-PACKAGE>\n"
            "    <AR-PACKAGE><SHORT-NAME>Components</SHORT-NAME><ELEMENTS>\n"
            "      <APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>ModeSwc</SHORT-NAME><PORTS>\n"
            "        <R-PORT-PROTOTYPE><SHORT-NAME>rMode</SHORT-NAME>\n"
            '          <REQUIRED-INTERFACE-TREF DEST="MODE-SWITCH-INTERFACE">'
            "/Acme/Interfaces/IMode</REQUIRED-INTERFACE-TREF>\n"
            "        </R-PORT-PROTOTYPE>\n"
            "      </PORTS></APPLICATION-SW-COMPONENT-TYPE>\n"
            "    </ELEMENTS></AR-PACKAGE>\n"
            "  </AR-PACKAGES></AR-PACKAGE></AR-PACKAGES>\n"
            "</AUTOSAR>\n",
            encoding="utf-8",
            newline="\n",
        )

        mapped = _map(read_arxml(str(source)))

        assert [imported.swc.ports for imported in mapped["swcs"]] == [[]]
        assert [r.target for r in mapped["unresolved"].references] == []
        assert any("unresolved reference /Acme/Interfaces/IMode" in entry.reason for entry in mapped["skips"])


class TestARecordIsNotKeptForAnElementThatWasAbandoned:
    """A staged reference whose referring element never reached the model mints no record.

    A data element, a client-server argument, a record field and an array element are members a
    later member can still refuse whole, so a record is committed only once its element is
    written; otherwise the report would count a reference against a file the import did not write.
    """

    def test_a_sender_receiver_interface_abandoned_by_a_later_element(self):
        """Element ``a`` stages an absent type, element ``b`` names a SW-BASE-TYPE and refuses the interface."""
        from swcraft_tools.arxml_reader import SwBaseTypeInfo

        model = ImportModel(source_path="inline")
        model.path_index["/Pkg/Types/U8"] = raw_element("/Pkg/Types/U8")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/U8", name="U8", size=8, encoding="NONE", native_declaration="uint8", description=""
            )
        )
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/IF",
                name="IF",
                description="",
                data_elements=[
                    SRDataElementInfo(name="a", type_ref="/Missing/T"),
                    SRDataElementInfo(name="b", type_ref="/Pkg/Types/U8"),
                ],
            )
        )

        mapped = _map(model)

        assert mapped["interfaces"] == []
        assert mapped["unresolved"].references == ()
        assert mapped["unresolved"].total_referrers == 0

    def test_a_client_server_interface_abandoned_by_a_later_operation(self):
        """An argument stages an absent type; a later argument's unknown direction refuses the interface.

        The direction is one ``CS_DIRECTION_MAP`` does not know.
        """
        from swcraft_tools.arxml_reader import CSArgumentInfo, CSInterfaceInfo, CSOperationInfo

        model = ImportModel(source_path="inline")
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/ICalc",
                name="ICalc",
                description="",
                operations=[
                    CSOperationInfo(
                        name="first",
                        arguments=[CSArgumentInfo(name="request", type_ref="/Missing/T", direction="IN")],
                    ),
                    CSOperationInfo(
                        name="second",
                        arguments=[CSArgumentInfo(name="reply", type_ref="/Missing/T", direction="SIDEWAYS")],
                    ),
                ],
            )
        )

        mapped = _map(model)

        assert mapped["interfaces"] == []
        assert mapped["unresolved"].references == ()

    def test_a_record_abandoned_by_a_later_blank_field_name(self):
        from swcraft_tools.arxml_reader import AppRecordDataTypeInfo, RecordElementInfo

        model = ImportModel(source_path="inline")
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Pkg/Status",
                name="Status",
                elements=[
                    RecordElementInfo(name="level", type_ref="/Missing/T"),
                    RecordElementInfo(name="", type_ref="/Missing/T"),
                ],
                description="",
            )
        )

        mapped = _map(model)

        assert mapped["data_types"] == []
        assert mapped["unresolved"].references == ()

    def test_an_array_abandoned_by_its_own_length(self):
        from swcraft_tools.arxml_reader import AppArrayDataTypeInfo

        model = ImportModel(source_path="inline")
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Pkg/History",
                name="History",
                element_type_ref="/Missing/T",
                max_number_of_elements=None,
                description="",
            )
        )

        mapped = _map(model)

        assert mapped["data_types"] == []
        assert mapped["unresolved"].references == ()

    def test_one_kept_referrer_beside_one_abandoned_keeps_only_the_kept_one(self):
        """The record survives its abandoned referrer, carrying only the kept one.

        The record is one per ``(target, cause)``, so a second, abandoned referrer must not remove it."""
        from swcraft_tools.arxml_reader import SwBaseTypeInfo

        model = ImportModel(source_path="inline")
        model.path_index["/Pkg/Types/U8"] = raw_element("/Pkg/Types/U8")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/U8", name="U8", size=8, encoding="NONE", native_declaration="uint8", description=""
            )
        )
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/IKept",
                name="IKept",
                description="",
                data_elements=[SRDataElementInfo(name="a", type_ref="/Missing/T")],
            )
        )
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/IGone",
                name="IGone",
                description="",
                data_elements=[
                    SRDataElementInfo(name="a", type_ref="/Missing/T"),
                    SRDataElementInfo(name="b", type_ref="/Pkg/Types/U8"),
                ],
            )
        )

        mapped = _map(model)

        assert [interface.name for interface in mapped["interfaces"]] == ["IKept"]
        record = _record(mapped["unresolved"], "/Missing/T")
        assert [referrer.path for referrer in record.referrers] == ["/Pkg/IKept"]
        assert mapped["unresolved"].total_referrers == 1


class TestAMemberWithNoNameKeepsNothing:
    """The unresolved-reference policy does not repair a blank ``SHORT-NAME``, and must not stage against one.

    The three member loops that can stage an unresolved reference (a record's elements, a client-server
    operation's arguments, a sender-receiver interface's data elements) decide a blank member name before
    staging anything; a sender-receiver interface with a blank data element is skipped whole under
    :data:`BLANK_REASON`. A kept
    ``DataElement(name="")`` is no AUTOSAR ``Identifier``, and ``reconcile.identifier_violations``
    would refuse the **whole** import.
    """

    BLANK_REASON = "a data element has missing or blank SHORT-NAME"

    @classmethod
    def _chained(cls, name: str) -> str:
        """Each member of the skipped interface, chained to its line."""
        return f"chain-skipped: interface '{name}' was skipped ({cls.BLANK_REASON})"

    def test_a_blank_data_element_stages_no_reference_and_abandons_its_interface(self):
        model = ImportModel(source_path="inline")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/IBlank",
                name="IBlank",
                description="",
                data_elements=[SRDataElementInfo(name="", type_ref="/Missing/T")],
            )
        )

        mapped = _map(model)

        assert mapped["interfaces"] == []
        assert mapped["unresolved"].references == ()
        assert mapped["unresolved"].total_referrers == 0
        assert [skip.reason for skip in mapped["skips"]] == [self.BLANK_REASON, self._chained("IBlank")]

    @pytest.mark.parametrize("ghost", [False, True], ids=["resolvable-type", "unresolvable-type"])
    def test_the_import_completes_and_skips_the_interface(self, tmp_path: Path, ghost: bool):
        """End to end: the import completes and skips only the blank-named interface.

        Guards a blank name with a resolvable type refusing the whole import.
        """
        from swcraft_tools.arxml_import import run_import

        source = tmp_path / "blank_element.arxml"
        text = BLANK_DATA_ELEMENT_ARXML
        if not ghost:
            text = text.replace("/Nowhere/Ghost", "/Demo/Types/Counter")
        source.write_text(text, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert (report.imported_data_types, report.imported_interfaces, report.imported_swcs) == (2, 1, 1)
        assert [(skip.path, skip.tag, skip.reason) for skip in report.skip_entries] == [
            ("/Demo/Ifs/IBlank", "SENDER-RECEIVER-INTERFACE", self.BLANK_REASON),
            ("/Demo/Ifs/IBlank/", "VARIABLE-DATA-PROTOTYPE", self._chained("IBlank")),
        ]
        assert report.unresolved_references == []
        # A blank name is the file's defect, cited.
        assert f"1 × SENDER-RECEIVER-INTERFACE (R4.2.2 [TPS_GST_00057]): {self.BLANK_REASON}" in report.render()
        assert (project / "model" / "interfaces" / "IGood.yaml").is_file()
        assert not (project / "model" / "interfaces" / "IBlank.yaml").exists()

    def test_a_blank_element_after_a_named_one_stages_nothing_either(self):
        """A blank element after a named one drops the interface, and the named one's staged record is never promoted.

        The decision is at the top of the iteration, so the earlier element that staged an unresolved reference is
        dropped with its interface; `commit()` is reached only by a written interface.
        """
        model = ImportModel(source_path="inline")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/IHalf",
                name="IHalf",
                description="",
                data_elements=[
                    SRDataElementInfo(name="first", type_ref="/Missing/T"),
                    SRDataElementInfo(name="  ", type_ref="/Missing/T"),
                ],
            )
        )

        mapped = _map(model)

        assert mapped["interfaces"] == []
        assert mapped["unresolved"].references == ()
        assert [skip.reason for skip in mapped["skips"]] == [self.BLANK_REASON] + [self._chained("IHalf")] * 2


BLANK_DATA_ELEMENT_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Demo</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Types</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Counter</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Demo/Types/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Ifs</SHORT-NAME>
          <ELEMENTS>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IBlank</SHORT-NAME>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME></SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Nowhere/Ghost</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IGood</SHORT-NAME>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>Value</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Demo/Types/Counter</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Swc</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SwcA</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pOut</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Demo/Ifs/IGood</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


class TestTheRecoveryBehindAKeptPort:
    """A kept port's connectors resolve against the model although its interface does not.

    Without the unresolved-reference policy a skipped port takes its connectors with it.
    """

    def _model(self) -> ImportModel:
        model = ImportModel(source_path="inline")
        for name, port, kind in (("Sender", "pOut", "P-PORT-PROTOTYPE"), ("Receiver", "rIn", "R-PORT-PROTOTYPE")):
            model.swcs.append(
                SwcInfo(
                    path=f"/Acme/Comps/{name}",
                    name=name,
                    description="",
                    ports=[
                        SwcPortInfo(
                            path=f"/Acme/Comps/{name}/{port}",
                            name=port,
                            kind=kind,
                            interface_ref="/Vendor/Interfaces/IAbsent",
                            description="",
                        )
                    ],
                    runnables=[],
                    events=[],
                )
            )
        model.compositions.append(
            CompositionInfo(
                path="/Acme/Comps/Root",
                name="Root",
                description="",
                ports=[],
                prototypes=[
                    ComponentPrototypeInfo(
                        path=f"/Acme/Comps/Root/{name}Part",
                        name=f"{name}Part",
                        type_ref=f"/Acme/Comps/{name}",
                        dest="APPLICATION-SW-COMPONENT-TYPE",
                    )
                    for name in ("Sender", "Receiver")
                ],
                assembly_connectors=[
                    AssemblyConnectorInfo(
                        path="/Acme/Comps/Root/Asm",
                        name="Asm",
                        provider=ConnectorPortRef(
                            component_ref="/Acme/Comps/Root/SenderPart", port_ref="/Acme/Comps/Sender/pOut"
                        ),
                        requester=ConnectorPortRef(
                            component_ref="/Acme/Comps/Root/ReceiverPart", port_ref="/Acme/Comps/Receiver/rIn"
                        ),
                    )
                ],
                delegation_connectors=[],
            )
        )
        return model

    def test_the_connector_is_lost_without_the_policy_and_recovered_with_it(self):
        model = self._model()

        # Without the policy (`import component`'s value): both ports are
        # skipped and the connector falls with them.
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})
        before, _ = map_compositions(model, swcs, interfaces, iface_skips, registry, swc_skip_log=swc_skips)
        assert [imported.swc.ports for imported in swcs] == [[], []]
        assert before.connectors == []

        after = _map(self._model())

        assert [len(imported.swc.ports) for imported in after["swcs"]] == [1, 1]
        assert [(c.name, c.from_.port, c.to.port) for c in after["connectors"]] == [("Asm", "pOut", "rIn")]
        record = _record(after["unresolved"], "/Vendor/Interfaces/IAbsent")
        assert len(record.referrers) == 2


class TestReconcileIsUnchanged:
    """``reconcile.py`` substitutes by NAME, and a source path is not a name.

    The reserved-name pass may touch neither a field holding a source path nor a merged element's
    source paths, and its own rename must still reach every reference that names the renamed element.
    """

    def test_a_field_holding_a_source_path_is_left_alone_while_a_rename_is_followed(self):
        model = ImportModel(source_path="inline")
        # `ISpeed_speed_Init` is what the writer derives for the declared pair
        # (ISpeed, speed), so the declared interface of that name is renamed.
        for name, elements in (
            ("ISpeed", [SRDataElementInfo(name="speed", type_ref="/Vendor/Types/Absent")]),
            ("ISpeed_speed_Init", [SRDataElementInfo(name="value", type_ref="/Vendor/Types/Absent")]),
        ):
            model.sr_interfaces.append(
                SRInterfaceInfo(path=f"/Acme/Interfaces/{name}", name=name, description="", data_elements=elements)
            )
        model.swcs.append(
            SwcInfo(
                path="/Acme/Comps/Swc",
                name="Swc",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Acme/Comps/Swc/pInit",
                        name="pInit",
                        kind="P-PORT-PROTOTYPE",
                        interface_ref="/Acme/Interfaces/ISpeed_speed_Init",
                        description="",
                    ),
                    SwcPortInfo(
                        path="/Acme/Comps/Swc/rDangling",
                        name="rDangling",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref="/Vendor/Interfaces/IAbsent",
                        description="",
                    ),
                ],
                runnables=[],
                events=[],
            )
        )

        mapped = _map(model)
        reconciled = reconcile_reserved_names(
            mapped["data_types"],
            mapped["interfaces"],
            mapped["swcs"],
            mapped["connectors"],
            mapped["compositions"],
            mapped["registry"],
            [],
        )

        ports = {port.name: port.interface for port in reconciled.swcs[0].swc.ports}
        assert ports["pInit"] == "ISpeed_speed_Init_app"
        assert ports["rDangling"] == "/Vendor/Interfaces/IAbsent"
        assert {
            element.implementation_type for interface in reconciled.interfaces for element in interface.data_elements
        } == {"/Vendor/Types/Absent"}

    def test_a_merged_element_carrying_several_source_paths(self):
        """Both member paths of a merged group still resolve after the pass.

        The merged interface is renamed, so the substitution reaches it through the one emitted
        name both paths map onto -- which keeps the merge and the unresolved-reference policy independent.
        """
        model = read_arxml(str(shape_path("identical_duplicates.arxml")))
        from swcraft_tools.arxml_import.merge_verdict import decide_merges

        merges = decide_merges(model)
        unresolved = UnresolvedReferences(model, merges)
        forms: list = []
        data_types, registry, _ = map_data_types(model, merges=merges, unresolved=unresolved, elements=forms)
        interfaces, iface_skips = map_interfaces(
            model, registry, data_types, merges=merges, unresolved=unresolved, elements=forms
        )
        swcs, swc_skips = map_swcs(
            model,
            interfaces,
            iface_skip_log=iface_skips,
            merges=merges,
            unresolved=unresolved,
            port_inits={},
            variable_types={},
        )
        mapped, _ = map_compositions(
            model,
            swcs,
            interfaces,
            iface_skips,
            registry,
            merges=merges,
            unresolved=unresolved,
            swc_skip_log=swc_skips,
        )

        reconciled = reconcile_reserved_names(
            data_types, interfaces, swcs, mapped.connectors, mapped.compositions, registry, []
        )

        merged = [group for group in merges.groups if group.verdict == "merged"]
        assert merged, "the fixture's whole point is that it merges"
        # Both member paths of every merged DATA TYPE group resolve to the one
        # emitted name after the pass (the interface namespace has no path map
        # of its own -- `map_interfaces` never renames, so its merge is visible
        # as one element rather than as two resolving paths).
        for group in merged:
            if group.key.layer != "data_types":
                continue
            resolved = {registry.resolve(path) for path in group.paths}
            assert resolved == {registry.resolve(group.primary)}
            assert None not in resolved
        assert {dt.name for dt in reconciled.data_types} == {dt.name for dt in data_types}
        assert len(reconciled.interfaces) == len(interfaces)


class TestImportComponentTakesItsOwnPolicy:
    """``import component`` takes its own unresolved-reference policy and no merge verdict.

    Asserted at the CLI product and at the seam, where only ``map_swcs`` takes a policy, the one
    ``UnresolvedReferences.for_component_import`` builds (a port's interface, resolved late).
    """

    def test_the_seam_takes_no_policy(self):
        """No mapper call of ``import component`` passes a merge verdict, and only ``map_swcs`` an unresolved policy.

        Read off the module's AST; the call count is asserted first, so a renamed, re-indented or
        deleted call site fails instead of passing vacuously.

        Does not reach: a call through an alias or a ``getattr``, a policy threaded through a wrapper
        ``import_.py`` defines, or a mapper call in a sibling module of ``component_exchange`` -- only
        ``import_.py``'s source is read; the product-level sibling below is the behavioural half.
        """
        import ast
        import inspect

        from swcraft_tools.component_exchange import import_ as component_import

        tree = ast.parse(inspect.getsource(component_import))
        # `map_mapping_sets` takes BOTH parameters, so threading a verdict or an
        # unresolved policy into `import component` through it must be caught too.
        wanted = {"map_data_types", "map_interfaces", "map_swcs", "map_mapping_sets"}
        found: dict[str, list[ast.Call]] = {name: [] for name in wanted}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in wanted:
                found[node.func.id].append(node)

        # Non-vacuity: the calls really are in this module.
        assert {name: len(calls) for name, calls in found.items()} == {name: 1 for name in sorted(wanted)}
        for name, calls in found.items():
            for call in calls:
                keywords = {keyword.arg for keyword in call.keywords}
                assert "merges" not in keywords, f"{name}() in import_.py passes a merge verdict"
                if name == "map_swcs":
                    assert "unresolved" in keywords, "map_swcs() in import_.py lost the component policy"
                else:
                    assert "unresolved" not in keywords, f"{name}() in import_.py passes an unresolved-reference policy"
        # The one policy passed is the component policy, built by its own constructor.
        built = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and [target.id for target in node.targets if isinstance(target, ast.Name)] == ["unresolved"]
        ]
        assert len(built) == 1
        policy = built[0].value
        assert isinstance(policy, ast.Call)
        assert isinstance(policy.func, ast.Attribute)
        assert policy.func.attr == "for_component_import"

    def test_the_product_keeps_the_port_on_a_stub(self, tmp_path):
        """``import component`` keeps the port on an interface stub carrying the source path.

        Guards the dangling interface refusing the whole file.
        """
        from swcraft_tools.component_exchange import import_component
        from swcraft_tools.project import require_project

        project = _init_project(tmp_path)
        model_root = require_project(str(project / "swcraft.yaml")).resolved_model_root

        report = import_component(
            source=str(shape_path("component_dangling_interface.arxml")),
            model_root=str(model_root),
        )

        assert report.swc_action == "created"
        loaded = [
            yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted((Path(model_root) / "interfaces").glob("*.yaml"), key=lambda path: path.as_posix())
        ]
        stubs = [element for element in loaded if element["kind"] == "external_interface"]
        assert [(stub["external_ref"], stub["external_dest"]) for stub in stubs] == [
            ("/Vendor/Interfaces/IStatus", "SENDER-RECEIVER-INTERFACE")
        ]
        (written,) = list((Path(model_root) / "swc").rglob("*.yaml"))
        ports = yaml.safe_load(written.read_text(encoding="utf-8"))["ports"]
        assert {"name": "rStatus", "direction": "require", "interface": stubs[0]["name"]} in ports


class TestTheImportedProjectIsReadableAndRefusesToGenerate:
    """The unresolved-reference policy's downstream half, on a project the importer really wrote.

    ``check-model`` reports exactly the kept references and nothing else; ``generate arxml`` refuses
    before its first byte, so ``dist/`` is absent rather than present and stale.
    """

    #: The three absent targets in roles no external stub may take -- a value type's base, a record
    #: field's and an array element's type. The fixture's other three are stubs (:data:`STUBBED`).
    TARGETS = (
        "/Vendor/BaseTypes/Word",
        "/Acme/Chassis/AppTypes/AbsentFieldType",
        "/Acme/Chassis/AppTypes/AbsentItemType",
    )
    STUBBED = (
        "/Acme/Chassis/Types/AbsentElementType",
        "/Acme/Chassis/Interfaces/AbsentProvidedInterface",
        "/Acme/Chassis/Interfaces/AbsentOuterInterface",
    )

    def _imported(self, tmp_path: Path):
        from swcraft_tools.arxml_import import run_import

        project = _init_project(tmp_path)
        report = run_import(str(shape_path(ROLES)), str(project / "swcraft.yaml"))
        return project, report

    def test_check_model_reports_exactly_the_kept_references(self, tmp_path: Path) -> None:
        from swcraft_tools.project import require_project
        from swcraft_tools.validator import check_cross_layer_consistency

        project, _ = self._imported(tmp_path)
        model_root = require_project(str(project / "swcraft.yaml")).resolved_model_root

        result = check_cross_layer_consistency(model_root=str(model_root))

        assert not result.consistent
        assert sorted(issue.target or "" for issue in result.records) == sorted(self.TARGETS)

    def test_generate_arxml_refuses_and_writes_nothing(self, tmp_path: Path, monkeypatch) -> None:
        project, _ = self._imported(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(project / "swcraft.yaml")])

        with pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        assert not (project / "dist").exists()

    def test_the_report_carries_a_line_for_every_unresolved_reference(self, tmp_path: Path) -> None:
        _, report = self._imported(tmp_path)

        assert sorted(record.target for record in report.unresolved_references) == sorted(self.TARGETS)
        assert sorted(
            stub.target for stub in [*report.external_interface_stubs, *report.external_type_stubs]
        ) == sorted(self.STUBBED)
        rendered = report.render()
        for target in self.TARGETS:
            assert target in rendered


class TestTheUnresolvedReferenceReportCategory:
    """One line per ``(target path, cause)``, three referrers named.

    The line leads with the target path and its ``DEST`` and ends in the sentence naming the repair
    owed (a missing package, a selection or a duplicated declaration -- never a referrer). The
    referrers follow indented, cut at :data:`~swcraft_tools.arxml_import.report.UNRESOLVED_REFERRERS_NAMED`
    and listed whole under ``--verbose``; the cut hides a path, never a reference.

    Not covered here: the ``outside the selected packages`` case renders a hand-built record; the
    merge and conflict categories are ``test_arxml_import_merges.py``'s; the summary counts, the
    ``--dry-run`` identity and the path-free rule are ``test_arxml_import_cli.py``'s.
    """

    SELECTION_CONFLICT = "packages_selection_conflict.arxml"

    @staticmethod
    def _section(rendered: str) -> list[str]:
        lines = rendered.split("\n")
        starts = [index for index, line in enumerate(lines) if line.startswith("Unresolved references")]
        assert len(starts) == 1, f"expected one heading, found {len(starts)}"
        return lines[starts[0] : lines.index("", starts[0])]

    @staticmethod
    def _report(tmp_path: Path, fixture: str):
        from swcraft_tools.arxml_import import run_import

        project = _init_project(tmp_path)
        return run_import(str(shape_path(fixture)), str(project / "swcraft.yaml"), dry_run=True)

    def test_an_absent_target_is_one_line_with_its_dest_and_its_referrer(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, ROLES)

        section = self._section(report.render())

        # Three of the six absent targets are external stubs.
        assert section[0].startswith("Unresolved references (3 reference(s) to 3 target(s);")
        assert (
            "  - /Vendor/BaseTypes/Word (DEST SW-BASE-TYPE) is declared by no element of this export; "
            "1 referring element(s):"
        ) in section
        assert (
            "      /Acme/Chassis/Types/Level "
            "(IMPLEMENTATION-DATA-TYPE BASE-TYPE-REF -> implementation_types.value_type.base)"
        ) in section

    def test_every_reference_is_represented_by_the_count_on_its_line(self, tmp_path: Path) -> None:
        """Every reference is represented once, against the external oracle's own figure.

        The oracle knows no stub: its six absent references are the category's
        three plus the three the external stubs answer."""
        from arxml_import_shapes_support import SHAPES

        report = self._report(tmp_path, ROLES)

        counted = sum(
            int(line.rsplit("; ", 1)[-1].split(" ", 1)[0])
            for line in self._section(report.render())
            if line.startswith("  - ")
        )

        stubbed = sum(len(stub.referrers) for stub in [*report.external_interface_stubs, *report.external_type_stubs])
        assert counted == sum(len(record.referrers) for record in report.unresolved_references) == 3
        assert counted + stubbed == SHAPES[ROLES].oracle.unresolved == 6

    def test_a_conflicting_target_names_the_conflicting_declaration(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, self.SELECTION_CONFLICT)

        section = self._section(report.render())

        assert [line for line in section if line.startswith("  - ")] == [
            "  - /Acme/TreeA/Types/Ratio (DEST IMPLEMENTATION-DATA-TYPE) was declared more than once "
            "with different definitions and was not imported -- see the conflicting declaration "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio; 1 referring element(s):"
        ]
        # The conflict line the sentence points at is above it in the same report.
        rendered = report.render()
        assert rendered.index("Conflicting declarations") < rendered.index("Unresolved references")

    def test_a_target_outside_the_selection_says_so(self, tmp_path: Path) -> None:
        """The third sentence, rendered from a record built by hand.

        Its producer runs only under a ``--packages`` selection over a
        conflicting group, which this module does not build.
        """
        import dataclasses

        from swcraft_tools.arxml_import.unresolved import (
            CAUSE_SELECTION,
            UnresolvedReference,
            UnresolvedReferrer,
        )

        record = UnresolvedReference(
            target="/Acme/Other/Types/Speed",
            dest="IMPLEMENTATION-DATA-TYPE",
            cause=CAUSE_SELECTION,
            referrers=[
                UnresolvedReferrer(
                    path="/Acme/Kept/Interfaces/ISpeed/speed",
                    tag="SENDER-RECEIVER-INTERFACE",
                    attribute="TYPE-TREF",
                    field="interfaces.data_element.implementation_type",
                )
            ],
        )
        report = dataclasses.replace(self._report(tmp_path, ROLES), unresolved_references=[record])

        assert self._section(report.render())[1] == (
            "  - /Acme/Other/Types/Speed (DEST IMPLEMENTATION-DATA-TYPE) is outside the selected "
            "packages; 1 referring element(s):"
        )

    def _five_referrers(self) -> ImportModel:
        """Five components, one port each, all naming one interface the export does not declare."""
        model = ImportModel(source_path="inline")
        for index in range(5):
            model.swcs.append(
                SwcInfo(
                    path=f"/Acme/Comps/Swc{index}",
                    name=f"Swc{index}",
                    description="",
                    ports=[
                        SwcPortInfo(
                            path=f"/Acme/Comps/Swc{index}/rIn",
                            name="rIn",
                            kind="R-PORT-PROTOTYPE",
                            interface_ref="/Vendor/Interfaces/IAbsent",
                            description="",
                        )
                    ],
                    runnables=[],
                    events=[],
                )
            )
        return model

    def _rendered_cut(self, tmp_path: Path, *, verbose: bool) -> list[str]:
        import dataclasses

        mapped = _map(self._five_referrers())
        report = dataclasses.replace(
            self._report(tmp_path, ROLES),
            unresolved_references=list(mapped["unresolved"].references),
        )
        return self._section(report.render(verbose=verbose))

    def test_three_referrers_are_named_and_the_rest_counted(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_import.report import UNRESOLVED_REFERRERS_NAMED

        section = self._rendered_cut(tmp_path, verbose=False)

        assert UNRESOLVED_REFERRERS_NAMED == 3
        assert section[0].startswith("Unresolved references (5 reference(s) to 1 target(s);")
        assert section[1].endswith("; 5 referring element(s):")
        assert [line.strip().split(" ")[0] for line in section[2:5]] == [
            "/Acme/Comps/Swc0/rIn",
            "/Acme/Comps/Swc1/rIn",
            "/Acme/Comps/Swc2/rIn",
        ]
        assert section[5] == "      … and 2 more"
        assert len(section) == 6

    def test_verbose_lists_every_referrer_and_cuts_nothing(self, tmp_path: Path) -> None:
        section = self._rendered_cut(tmp_path, verbose=True)

        assert len(section) == 7
        assert [line.strip().split(" ")[0] for line in section[2:]] == [
            f"/Acme/Comps/Swc{index}/rIn" for index in range(5)
        ]
        assert not any("more" in line for line in section)

    def test_an_import_with_no_unresolved_reference_renders_no_category(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "identical_duplicates.arxml")

        assert report.unresolved_references == []
        assert "Unresolved references" not in report.render()


class TestARunnableAccessThroughAKeptPort:
    """A runnable access through a kept unresolved port whose ``Port.interface`` resolves to no ``Interface``.

    The shape corpus cannot reach it: none of its files declares a ``RUNNABLE-ENTITY``. Does not
    reach the ``import component`` path, which takes its own policy.
    """

    @staticmethod
    def _model() -> ImportModel:
        from swcraft_tools.arxml_reader import SwcAccessInfo, SwcRunnableInfo

        model = ImportModel(source_path="inline")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Acme/If/IKnown",
                name="IKnown",
                description="",
                data_elements=[SRDataElementInfo(name="value", type_ref="/Acme/Types/Level")],
            )
        )
        model.swcs.append(
            SwcInfo(
                path="/Acme/Components/Sensor",
                name="Sensor",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Acme/Components/Sensor/rAbsent",
                        name="rAbsent",
                        kind="R-PORT-PROTOTYPE",
                        # Declared by nothing in this export: the policy keeps the port
                        # and writes this path into `Port.interface`.
                        interface_ref="/Vendor/Interfaces/IStatus",
                        description="",
                    )
                ],
                runnables=[
                    SwcRunnableInfo(
                        path="/Acme/Components/Sensor/IB/Run",
                        name="Run",
                        min_start_interval=None,
                        description="",
                        accesses=[
                            SwcAccessInfo(
                                path="/Acme/Components/Sensor/IB/Run/R_rAbsent_value",
                                name="R_rAbsent_value",
                                kind="DATA-RECEIVE-POINT-BY-ARGUMENTS",
                                tag="VARIABLE-ACCESS",
                                port_ref="/Acme/Components/Sensor/rAbsent",
                                target_ref="/Vendor/Interfaces/IStatus/value",
                            )
                        ],
                    )
                ],
                events=[],
            )
        )
        return model

    def test_the_entry_is_skipped_naming_the_port_and_the_reference_and_the_port_is_not(self) -> None:
        model = self._model()
        unresolved = UnresolvedReferences(model)
        forms: list = []
        data_types, registry, _ = map_data_types(model, unresolved=unresolved, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, unresolved=unresolved, elements=forms)
        swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, unresolved=unresolved, port_inits={}, variable_types={}
        )

        assert [imported.swc.name for imported in swcs] == ["Sensor"]
        assert [(port.name, port.interface) for port in swcs[0].swc.ports] == [
            ("rAbsent", "/Vendor/Interfaces/IStatus")
        ]
        assert swcs[0].swc.runnables[0].reads == []
        # The entry is held for the stub decision, which `run_import` takes after
        # mapping and which this case never takes -- so no stub lists its member,
        # and finishing it records the line the mapper recorded.
        assert [held.path for held in swcs[0].held_stub_records] == ["/Acme/Components/Sensor/IB/Run/R_rAbsent_value"]
        finished = finish_stub_members(swcs[0], unresolved, swc_skips)
        assert finished.swc.runnables[0].reads == []
        assert finished.held_stub_records == []

        entries = [entry for entry in swc_skips if entry.path == "/Acme/Components/Sensor/IB/Run/R_rAbsent_value"]
        assert [entry.tag for entry in entries] == ["VARIABLE-ACCESS"]
        assert entries[0].reason == (
            "reads access through port 'rAbsent' whose interface reference "
            "/Vendor/Interfaces/IStatus is not declared by this export"
        )
        # No skip about the port itself: the port survives the reference its
        # access could not be judged against.
        assert [entry.path for entry in swc_skips if entry.path.endswith("/rAbsent")] == []

    def test_without_the_policy_the_port_is_dropped_and_the_entry_chain_skips(self) -> None:
        """Without a policy the port is dropped and the entry chain-skips, so the branch above is the policy's own."""
        model = self._model()
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})

        assert [port.name for port in swcs[0].swc.ports] == []
        assert [entry.reason for entry in swc_skips if entry.path.endswith("/rAbsent")] == [
            "unresolved reference /Vendor/Interfaces/IStatus (the file declares no interface at that path)"
        ]
        assert [
            entry.reason for entry in swc_skips if entry.path == "/Acme/Components/Sensor/IB/Run/R_rAbsent_value"
        ] == [
            "chain-skipped: port 'rAbsent' was skipped (unresolved reference "
            "/Vendor/Interfaces/IStatus (the file declares no interface at that path))"
        ]


class TestNarrowingReadsAPortsOwnerAndKeepsPartOfARecord:
    """``narrowed`` reads a port's owner and keeps a subset of a record's referrers.

    A port is recorded under its own path, its component one segment above it; the narrowed record
    keeps the *N referring element(s)* count honest.
    """

    @staticmethod
    def _references() -> UnresolvedReferences:
        from swcraft_tools.arxml_import.unresolved import UnresolvedReferrer

        model = ImportModel(source_path="inline")
        references = UnresolvedReferences(model)
        for owner in ("Kept", "Dropped"):
            port_path = f"/Acme/Components/{owner}/rStatus"
            assert (
                references.keep(
                    "/Vendor/Interfaces/IStatus",
                    UnresolvedReferrer(
                        path=port_path,
                        tag="R-PORT-PROTOTYPE",
                        attribute="REQUIRED-INTERFACE-TREF",
                        field="swc.port.interface",
                    ),
                )
                == "/Vendor/Interfaces/IStatus"
            )
            # `keep` only STAGES; `map_ports` commits beside the append that
            # puts the port into the list it returns, and this fixture stands
            # in for that call. Narrowing composes with the commit rather than
            # replacing it, so an uncommitted referrer would never reach it.
            references.commit(port_path)
        return references

    def test_a_ports_record_survives_under_its_owners_path(self) -> None:
        references = self._references()

        narrowed = references.narrowed(frozenset({"/Acme/Components/Kept"}))

        assert [(record.target, len(record.referrers)) for record in narrowed] == [("/Vendor/Interfaces/IStatus", 1)]
        assert [referrer.path for referrer in narrowed[0].referrers] == ["/Acme/Components/Kept/rStatus"]
        # The record the whole import measured is untouched: `narrowed` answers,
        # it does not mutate.
        assert references.total_referrers == 2

    def test_a_record_every_one_of_whose_owners_went_is_gone(self) -> None:
        references = self._references()

        assert references.narrowed(frozenset({"/Acme/Components/Other"})) == ()


class TestTheTwoUnresolvedSentencesNothingRead:
    """``unstated``, and the conflict line's own fallback.

    A record whose ``DEST`` the referrer did not write (``_dest_of`` answers ``""`` for an unindexed
    referrer), and a ``CAUSE_CONFLICT`` record with no group name, built without the verdict.
    """

    @staticmethod
    def _reference(cause: str, *, dest: str = "", conflict_group: str | None = None):
        from swcraft_tools.arxml_import.unresolved import UnresolvedReference, UnresolvedReferrer

        return UnresolvedReference(
            target="/Vendor/Interfaces/IStatus",
            dest=dest,
            cause=cause,
            referrers=[
                UnresolvedReferrer(
                    path="/Acme/Components/Sensor/rStatus",
                    tag="R-PORT-PROTOTYPE",
                    attribute="REQUIRED-INTERFACE-TREF",
                    field="swc.port.interface",
                )
            ],
            conflict_group=conflict_group,
        )

    def test_a_reference_whose_dest_was_not_read_says_unstated(self) -> None:
        from swcraft_tools.arxml_import.report import _unresolved_lines

        lines = _unresolved_lines(self._reference(CAUSE_ABSENT), verbose=False)

        assert lines[0].startswith("  - /Vendor/Interfaces/IStatus (DEST unstated) ")
        assert "is declared by no element of this export" in lines[0]

    def test_a_dest_the_referrer_really_wrote_is_quoted(self) -> None:
        from swcraft_tools.arxml_import.report import _unresolved_lines

        lines = _unresolved_lines(self._reference(CAUSE_ABSENT, dest="SENDER-RECEIVER-INTERFACE"), verbose=False)

        assert "(DEST SENDER-RECEIVER-INTERFACE)" in lines[0]

    def test_a_conflict_record_with_no_group_names_the_declaration_above(self) -> None:
        from swcraft_tools.arxml_import.report import _unresolved_lines
        from swcraft_tools.arxml_import.unresolved import CAUSE_CONFLICT

        lines = _unresolved_lines(self._reference(CAUSE_CONFLICT), verbose=False)

        assert "see the conflicting declaration the conflicting declaration above" in lines[0]

    def test_a_conflict_record_naming_its_group_quotes_the_group(self) -> None:
        from swcraft_tools.arxml_import.report import _unresolved_lines
        from swcraft_tools.arxml_import.unresolved import CAUSE_CONFLICT

        lines = _unresolved_lines(
            self._reference(CAUSE_CONFLICT, conflict_group="interfaces/SENDER-RECEIVER-INTERFACE/IStatus"),
            verbose=False,
        )

        assert "see the conflicting declaration interfaces/SENDER-RECEIVER-INTERFACE/IStatus" in lines[0]

    def test_a_referrer_the_reader_never_indexed_reads_back_an_empty_dest(self) -> None:
        """``_dest_of``'s own last line, which makes ``unstated`` reachable rather than only defensive."""
        from swcraft_tools.arxml_import.unresolved import UnresolvedReferrer

        model = ImportModel(source_path="inline")
        references = UnresolvedReferences(model)
        references.keep(
            "/Vendor/Interfaces/IStatus",
            UnresolvedReferrer(
                path="/Acme/Components/Sensor/rStatus",
                tag="R-PORT-PROTOTYPE",
                attribute="REQUIRED-INTERFACE-TREF",
                field="swc.port.interface",
            ),
        )
        # `keep` stages; the mapper that kept the port is what commits it.
        references.commit("/Acme/Components/Sensor/rStatus")

        assert [record.dest for record in references.references] == [""]
