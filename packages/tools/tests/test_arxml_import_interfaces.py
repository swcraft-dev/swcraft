"""Tests for `arxml_import.type_mappers.map_interfaces`: SR and CS mapping and the interface `description` field.

Fixture cases map `minimal_swc.arxml` (SR) and `client_server.arxml` (CS with IN/OUT/INOUT arguments);
record/array, enum and error cases are built as small inline `ImportModel`s.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import INIT_VALUES_MODEL_ROOT
from swcraft_tools.arxml_import import map_data_types, map_interfaces
from swcraft_tools.arxml_import.type_mapping import NOT_AN_ENUMERATION_TEXT, _map_init_value
from swcraft_tools.arxml_reader import (
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    CompositionInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ComSpecInfo,
    ConstantArray,
    ConstantRecord,
    ConstantRecordField,
    ConstantScalar,
    ConstantSpecInfo,
    CSArgumentInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DataElementComSpecInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImportModel,
    PortComSpecInitInfo,
    RawElement,
    RecordElementInfo,
    SkipCategory,
    SkipLog,
    SRDataElementInfo,
    SRInterfaceInfo,
    StandardRule,
    SwBaseTypeInfo,
    SwcInfo,
    SwcPortInfo,
    ValueForm,
    read_arxml,
)
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import (
    ArrayDataType,
    CSInterface,
    EnumDataType,
    EnumLiteral,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    ValueDataType,
)
from swcraft_tools.writer import generate_arxml

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
MINIMAL_SWC = FIXTURES_DIR / "minimal_swc.arxml"
CLIENT_SERVER = FIXTURES_DIR / "client_server.arxml"


def _by_name(items, name):
    matches = [item for item in items if item.name == name]
    assert len(matches) == 1, f"expected exactly one item named {name!r}, found {len(matches)}"
    return matches[0]


def _skip_reason(skip_log, path) -> str | None:
    for entry in skip_log:
        if entry.path == path:
            return entry.reason
    return None


#: A VALUE implementation type over the inline tests' ``/Pkg/Types/uint8``: a prototype typed by the
#: ``SW-BASE-TYPE`` itself skips its interface (``_resolve_interface_type``).
_UINT8_IMPL = ImplDataTypeInfo(
    path="/Pkg/Types/uint8_impl",
    name="uint8_impl",
    category="VALUE",
    base_type_ref="/Pkg/Types/uint8",
    impl_type_ref=None,
    description="",
)


def _map(model: ImportModel):
    """Map the data types, then the interfaces, of one ImportModel."""
    forms: list = []
    data_types, registry, type_skip_log = map_data_types(model, elements=forms)
    interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms)
    return data_types, registry, type_skip_log, interfaces, iface_skip_log


# Fixture-driven success cases


@pytest.fixture(scope="module")
def minimal_swc_mapped():
    model = read_arxml(str(MINIMAL_SWC))
    return _map(model)


@pytest.fixture(scope="module")
def client_server_mapped():
    model = read_arxml(str(CLIENT_SERVER))
    return _map(model)


def test_sr_interface_data_element_type_resolved_via_shared_registry(minimal_swc_mapped):
    _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = minimal_swc_mapped
    assert len(iface_skip_log) == 0

    iface = _by_name(interfaces, "IWheelSpeed")
    assert isinstance(iface, SRInterface)
    assert iface.kind == "sender_receiver"
    assert len(iface.data_elements) == 1
    assert iface.data_elements[0].name == "speedValue"
    # A plain VALUE implementation type resolves to itself, unreduced.
    assert iface.data_elements[0].implementation_type == "WheelSpeedValue_impl"


def test_sr_interface_description_is_preserved(minimal_swc_mapped):
    _data_types, _registry, _type_skip_log, interfaces, _iface_skip_log = minimal_swc_mapped
    iface = _by_name(interfaces, "IWheelSpeed")
    assert iface.description == "Wheel speed sender/receiver interface (anonymized fixture)."


def test_cs_interface_operations_and_directional_arguments(client_server_mapped):
    _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = client_server_mapped
    assert len(iface_skip_log) == 0

    iface = _by_name(interfaces, "IBrakeControlService")
    assert isinstance(iface, CSInterface)
    assert iface.kind == "client_server"
    assert len(iface.operations) == 2

    set_mode = next(op for op in iface.operations if op.name == "SetBrakeMode")
    assert [(a.name, a.direction, a.implementation_type) for a in set_mode.arguments] == [
        ("requestedMode", "in", "uint8_impl"),
        ("acceptedMode", "out", "uint8_impl"),
    ]

    adjust_bias = next(op for op in iface.operations if op.name == "AdjustBrakeBias")
    assert [(a.name, a.direction, a.implementation_type) for a in adjust_bias.arguments] == [
        ("biasAdjustment", "inout", "int16_impl"),
    ]


def test_cs_interface_description_is_preserved(client_server_mapped):
    _data_types, _registry, _type_skip_log, interfaces, _iface_skip_log = client_server_mapped
    iface = _by_name(interfaces, "IBrakeControlService")
    assert iface.description == "Brake control diagnostic service (anonymized fixture)."


# Record/array-typed interface elements import


def test_sr_interface_with_record_data_element_imports_successfully():
    """A record-typed data element resolves like a primitive one; the interface is not skipped."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    # A record field's or an array element's type is an ApplicationDataType: an
    # implementation type there is refused.
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/Flag",
            name="Flag",
            compu_method_ref=None,
            description="",
            category="VALUE",
        )
    )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/StatusRecord",
            name="StatusRecord",
            elements=[RecordElementInfo(name="flag", type_ref="/Pkg/AppTypes/Flag")],
            description="",
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IStatus",
            name="IStatus",
            description="",
            data_elements=[SRDataElementInfo(name="status", type_ref="/Pkg/AppTypes/StatusRecord")],
        )
    )

    data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

    assert len(iface_skip_log) == 0
    record = _by_name(data_types, "StatusRecord")
    assert isinstance(record, RecordDataType)

    iface = _by_name(interfaces, "IStatus")
    assert isinstance(iface, SRInterface)
    assert len(iface.data_elements) == 1
    assert iface.data_elements[0].name == "status"
    assert (iface.data_elements[0].application_type, iface.data_elements[0].implementation_type) == (
        "StatusRecord",
        None,
    )


def test_cs_operation_argument_referencing_a_record_imports_successfully():
    """A CS operation argument typed by a record imports, as the SR case does."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    # A record field's or an array element's type is an ApplicationDataType: an
    # implementation type there is refused.
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/Flag",
            name="Flag",
            compu_method_ref=None,
            description="",
            category="VALUE",
        )
    )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/StatusRecord",
            name="StatusRecord",
            elements=[RecordElementInfo(name="flag", type_ref="/Pkg/AppTypes/Flag")],
            description="",
        )
    )
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/Interfaces/IStatusService",
            name="IStatusService",
            description="",
            operations=[
                CSOperationInfo(
                    name="GetStatus",
                    arguments=[
                        CSArgumentInfo(
                            name="status",
                            type_ref="/Pkg/AppTypes/StatusRecord",
                            direction="OUT",
                        )
                    ],
                )
            ],
        )
    )

    data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

    assert len(iface_skip_log) == 0
    record = _by_name(data_types, "StatusRecord")
    assert isinstance(record, RecordDataType)

    iface = _by_name(interfaces, "IStatusService")
    assert isinstance(iface, CSInterface)
    op = next(op for op in iface.operations if op.name == "GetStatus")
    assert [(a.name, a.direction, a.application_type, a.implementation_type) for a in op.arguments] == [
        ("status", "out", "StatusRecord", None),
    ]


def test_sr_interface_with_array_data_element_imports_successfully():
    """An array-typed data element resolves directly, unreduced."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    # A record field's or an array element's type is an ApplicationDataType: an
    # implementation type there is refused.
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/Sample",
            name="Sample",
            compu_method_ref=None,
            description="",
            category="VALUE",
        )
    )
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/AppTypes/SampleArray",
            name="SampleArray",
            element_type_ref="/Pkg/AppTypes/Sample",
            max_number_of_elements=8,
            description="",
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/ISamples",
            name="ISamples",
            description="",
            data_elements=[SRDataElementInfo(name="samples", type_ref="/Pkg/AppTypes/SampleArray")],
        )
    )

    data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

    assert len(iface_skip_log) == 0
    array = _by_name(data_types, "SampleArray")
    assert isinstance(array, ArrayDataType)

    iface = _by_name(interfaces, "ISamples")
    assert isinstance(iface, SRInterface)
    assert len(iface.data_elements) == 1
    assert iface.data_elements[0].name == "samples"
    assert (iface.data_elements[0].application_type, iface.data_elements[0].implementation_type) == (
        "SampleArray",
        None,
    )


# An interface's reference to an enum is written as the export declared it;
# the implementation type that stores it is a mapping set's business.


def test_sr_interface_enum_element_names_the_enum_the_export_named():
    """An enum-typed element names the enum the export named, and nothing is recorded.

    ModeEnum's bare name differs from its mapping-set twin's, so it is not twin-collapsed.
    """
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/ModeRaw_impl",
            name="ModeRaw_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/ModeEnum_CompuMethod",
            name="ModeEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[CompuScaleInfo(value=0, literal="OFF"), CompuScaleInfo(value=1, literal="ON")],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/ModeEnum",
            name="ModeEnum",
            compu_method_ref="/Pkg/CompuMethods/ModeEnum_CompuMethod",
            description="",
        )
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/Pkg/Mapping/PkgMapping",
            name="PkgMapping",
            maps=[
                DataTypeMapInfo(
                    application_ref="/Pkg/AppTypes/ModeEnum",
                    implementation_ref="/Pkg/Types/ModeRaw_impl",
                )
            ],
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IMode",
            name="IMode",
            description="",
            data_elements=[SRDataElementInfo(name="mode", type_ref="/Pkg/AppTypes/ModeEnum")],
        )
    )

    data_types, registry, type_skip_log, interfaces, iface_skip_log = _map(model)

    assert len(type_skip_log) == 0
    enum = _by_name(data_types, "ModeEnum")
    assert isinstance(enum, EnumDataType)

    assert len(iface_skip_log) == 0
    iface = _by_name(interfaces, "IMode")
    assert (iface.data_elements[0].application_type, iface.data_elements[0].implementation_type) == ("ModeEnum", None)

    # No twin, so no reduction of any kind.
    assert registry.reductions == []
    # The mapped implementation type is written beside the enum, untouched: the pair the RTE needs.
    assert isinstance(_by_name(data_types, "ModeRaw_impl"), ValueDataType)


def test_an_enum_mapped_onto_a_type_reference_leaves_both_written():
    """An enum mapped onto a TYPE_REFERENCE implementation type leaves both written, the interface kept."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8", name="uint8", size=8, encoding="NONE", native_declaration="uint8", description=""
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/ModeRaw_impl",
            name="ModeRaw_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/ModeRef",
            name="ModeRef",
            category="TYPE_REFERENCE",
            base_type_ref=None,
            impl_type_ref="/Pkg/Types/ModeRaw_impl",
            description="",
        )
    )
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/ModeEnum_CompuMethod",
            name="ModeEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[CompuScaleInfo(value=0, literal="OFF"), CompuScaleInfo(value=1, literal="ON")],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/ModeEnum",
            name="ModeEnum",
            compu_method_ref="/Pkg/CompuMethods/ModeEnum_CompuMethod",
            description="",
        )
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/Pkg/Mapping/PkgMapping",
            name="PkgMapping",
            maps=[DataTypeMapInfo(application_ref="/Pkg/AppTypes/ModeEnum", implementation_ref="/Pkg/Types/ModeRef")],
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IMode",
            name="IMode",
            description="",
            data_elements=[SRDataElementInfo(name="mode", type_ref="/Pkg/AppTypes/ModeEnum")],
        )
    )

    data_types, registry, type_skip_log, interfaces, iface_skip_log = _map(model)

    assert len(type_skip_log) == 0
    assert _by_name(data_types, "ModeRef") == ReferenceDataType(
        name="ModeRef", kind="reference_type", base="ModeRaw_impl"
    )
    assert len(iface_skip_log) == 0
    assert _by_name(interfaces, "IMode").data_elements[0].application_type == "ModeEnum"
    assert _by_name(interfaces, "IMode").data_elements[0].implementation_type is None
    assert registry.reductions == []


def test_enum_with_no_mapping_set_entry_keeps_its_interface():
    """An enum with no DATA-TYPE-MAPPING-SET entry keeps its interface, written as declared.

    The missing pair is ``check-model``'s ``rule:data-type.application-type-unmapped`` to report."""
    model = ImportModel(source_path="inline-test")
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/UnmappedEnum_CompuMethod",
            name="UnmappedEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[CompuScaleInfo(value=0, literal="A"), CompuScaleInfo(value=1, literal="B")],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/UnmappedEnum",
            name="UnmappedEnum",
            compu_method_ref="/Pkg/CompuMethods/UnmappedEnum_CompuMethod",
            description="",
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IUnmapped",
            name="IUnmapped",
            description="",
            data_elements=[SRDataElementInfo(name="mode", type_ref="/Pkg/AppTypes/UnmappedEnum")],
        )
    )

    _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

    assert [interface.name for interface in interfaces] == ["IUnmapped"]
    assert (interfaces[0].data_elements[0].application_type, interfaces[0].data_elements[0].implementation_type) == (
        "UnmappedEnum",
        None,
    )
    assert _skip_reason(iface_skip_log, "/Pkg/Interfaces/IUnmapped") is None


# Error scenarios


class TestErrorScenarios:
    def test_unresolved_data_element_type_ref_skips_whole_interface(self):
        model = ImportModel(source_path="inline-test")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/Interfaces/IDangling",
                name="IDangling",
                description="",
                data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/DoesNotExist")],
            )
        )

        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert interfaces == []
        reason = _skip_reason(iface_skip_log, "/Pkg/Interfaces/IDangling")
        assert reason is not None
        assert "unresolved reference" in reason.lower()
        assert "/Pkg/Types/DoesNotExist" in reason
        assert "x" in reason

    def test_operation_argument_referencing_a_skipped_type_chains_reason(self):
        """An argument whose type map_data_types skipped chains that reason into the interface's skip."""
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        # A record field's or an array element's type is an ApplicationDataType: an
        # implementation type there is refused.
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Item",
                name="Item",
                compu_method_ref=None,
                description="",
                category="VALUE",
            )
        )
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Pkg/AppTypes/Empty",
                name="Empty",
                element_type_ref="/Pkg/AppTypes/Item",
                max_number_of_elements=0,
                description="",
            )
        )
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/Interfaces/IBad",
                name="IBad",
                description="",
                operations=[
                    CSOperationInfo(
                        name="DoIt",
                        arguments=[
                            CSArgumentInfo(
                                name="value",
                                type_ref="/Pkg/AppTypes/Empty",
                                direction="IN",
                            )
                        ],
                    )
                ],
            )
        )

        _data_types, _registry, type_skip_log, interfaces, iface_skip_log = _map(model)

        type_reason = _skip_reason(type_skip_log, "/Pkg/AppTypes/Empty")
        assert type_reason is not None
        assert "MAX-NUMBER-OF-ELEMENTS" in type_reason

        assert interfaces == []
        reason = _skip_reason(iface_skip_log, "/Pkg/Interfaces/IBad")
        assert reason is not None
        assert "DoIt" in reason
        assert "value" in reason
        assert "/Pkg/AppTypes/Empty" in reason

    def test_cs_operation_blank_name_skips_only_that_operation(self):
        """A blank operation name skips only that operation, never the interface; the import does not crash."""
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        # A prototype typed by the SW-BASE-TYPE itself would skip the whole interface before the blank name.
        model.impl_data_types.append(_UINT8_IMPL)
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/Interfaces/IPartlyBad",
                name="IPartlyBad",
                description="",
                operations=[
                    CSOperationInfo(
                        name="   ",  # blank SHORT-NAME
                        arguments=[CSArgumentInfo(name="a", type_ref="/Pkg/Types/uint8_impl", direction="IN")],
                    ),
                    CSOperationInfo(
                        name="GoodOp",
                        arguments=[CSArgumentInfo(name="b", type_ref="/Pkg/Types/uint8_impl", direction="IN")],
                    ),
                ],
            )
        )

        # Must not raise pydantic.ValidationError.
        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert len(interfaces) == 1
        assert [op.name for op in interfaces[0].operations] == ["GoodOp"]
        # The entry names the operation's path, never the interface's: `build_interface_path_index` reads a
        # skip at a declaration's path as "this declaration did not survive". The unnamed operation's last
        # segment is `type_mapping.UNNAMED_MEMBER`.
        assert _skip_reason(iface_skip_log, "/Pkg/Interfaces/IPartlyBad") is None
        reason = _skip_reason(iface_skip_log, "/Pkg/Interfaces/IPartlyBad/<unnamed>")
        assert reason is not None
        assert "blank" in reason.lower() or "missing" in reason.lower()

    def test_cs_argument_blank_name_skips_only_that_operation(self):
        """A blank argument name skips only its operation; the import does not crash."""
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(_UINT8_IMPL)  # see the blank-operation test above
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/Interfaces/IPartlyBadArg",
                name="IPartlyBadArg",
                description="",
                operations=[
                    CSOperationInfo(
                        name="BadArgOp",
                        arguments=[CSArgumentInfo(name="   ", type_ref="/Pkg/Types/uint8_impl", direction="IN")],
                    ),
                    CSOperationInfo(
                        name="GoodOp",
                        arguments=[CSArgumentInfo(name="b", type_ref="/Pkg/Types/uint8_impl", direction="IN")],
                    ),
                ],
            )
        )

        # Must not raise pydantic.ValidationError.
        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert len(interfaces) == 1
        assert [op.name for op in interfaces[0].operations] == ["GoodOp"]
        # Recorded under the operation's own path, as in the blank-operation-name test, here with its name.
        assert _skip_reason(iface_skip_log, "/Pkg/Interfaces/IPartlyBadArg") is None
        reason = _skip_reason(iface_skip_log, "/Pkg/Interfaces/IPartlyBadArg/BadArgOp")
        assert reason is not None
        assert "BadArgOp" in reason
        assert "blank" in reason.lower() or "missing" in reason.lower()

    def test_cs_interface_with_only_blank_named_operation_is_skipped_wholesale(self):
        """An interface whose every operation is dropped is skipped, never built with an empty operation list."""
        model = ImportModel(source_path="inline-test")
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/Interfaces/IAllBad",
                name="IAllBad",
                description="",
                operations=[
                    CSOperationInfo(name="   ", arguments=[]),
                ],
            )
        )

        # Must not raise pydantic.ValidationError.
        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert interfaces == []
        # Two entries at two paths: the blank name is the operation's defect, the empty list the interface's.
        assert any(
            "blank" in entry.reason.lower() or "missing" in entry.reason.lower()
            for entry in iface_skip_log
            if entry.path == "/Pkg/Interfaces/IAllBad/<unnamed>"
        )
        assert any(
            "no operations" in entry.reason.lower()
            for entry in iface_skip_log
            if entry.path == "/Pkg/Interfaces/IAllBad"
        )

    def test_duplicate_sr_interface_name_is_skipped_keeping_first(self):
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(_UINT8_IMPL)  # see the blank-operation test above
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/PkgA/Interfaces/IDup",
                name="IDup",
                description="first declaration",
                data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8_impl")],
            )
        )
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/PkgB/Interfaces/IDup",
                name="IDup",
                description="second declaration",
                data_elements=[SRDataElementInfo(name="y", type_ref="/Pkg/Types/uint8_impl")],
            )
        )

        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert len(interfaces) == 1
        assert interfaces[0].description == "first declaration"
        reason = _skip_reason(iface_skip_log, "/PkgB/Interfaces/IDup")
        assert reason is not None
        assert "duplicate" in reason.lower()
        assert "/PkgA/Interfaces/IDup" in reason

    def test_sr_interface_blank_name_is_skipped_not_crashed(self):
        """A blank SR interface name is skipped with a reason; the import does not crash."""
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/Interfaces/",
                name="   ",
                description="",
                data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8")],
            )
        )

        # Must not raise pydantic.ValidationError.
        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert interfaces == []
        reason = _skip_reason(iface_skip_log, "/Pkg/Interfaces/")
        assert reason is not None
        assert "blank" in reason.lower() or "missing" in reason.lower()

    def test_duplicate_interface_name_across_sr_and_cs_is_skipped(self):
        """interfaces/ is one flat namespace, so an SR and a CS interface sharing a name collide."""
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(_UINT8_IMPL)  # see the blank-operation test above
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/PkgA/Interfaces/IShared",
                name="IShared",
                description="",
                data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8_impl")],
            )
        )
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/PkgB/Interfaces/IShared",
                name="IShared",
                description="",
                operations=[
                    CSOperationInfo(
                        name="Op",
                        arguments=[CSArgumentInfo(name="a", type_ref="/Pkg/Types/uint8_impl", direction="IN")],
                    )
                ],
            )
        )

        _data_types, _registry, _type_skip_log, interfaces, iface_skip_log = _map(model)

        assert len(interfaces) == 1
        assert isinstance(interfaces[0], SRInterface)
        reason = _skip_reason(iface_skip_log, "/PkgB/Interfaces/IShared")
        assert reason is not None
        assert "duplicate" in reason.lower()


# Every mapped interface is a real, validated schema.py Pydantic model


def test_every_mapped_interface_is_a_schema_pydantic_instance(minimal_swc_mapped, client_server_mapped):
    allowed = (SRInterface, CSInterface)
    all_interfaces = minimal_swc_mapped[3] + client_server_mapped[3]
    assert all_interfaces, "expected at least one mapped interface from the fixtures"
    for iface in all_interfaces:
        assert isinstance(iface, allowed)


# The element-level init value: the mapping itself (shape walk, by-value enum rule, skip-with-reason);
# the written-YAML assertions live in test_arxml_import_cli.py.


def _model_with_types() -> ImportModel:
    """Return an ImportModel with a primitive, a two-field record and a three-long array.

    Record and array elements name the application primitive ``Count``; a scalar element names the
    implementation type ``Counter_impl``."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Counter_impl",
            name="Counter_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/Pkg/AppTypes/Count", name="Count", compu_method_ref=None, description="")
    )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/Point",
            name="Point",
            elements=[
                RecordElementInfo(name="x", type_ref="/Pkg/AppTypes/Count"),
                RecordElementInfo(name="y", type_ref="/Pkg/AppTypes/Count"),
            ],
            description="",
        )
    )
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/AppTypes/Triple",
            name="Triple",
            element_type_ref="/Pkg/AppTypes/Count",
            max_number_of_elements=3,
            description="",
        )
    )
    return model


def _with_interface(
    model: ImportModel, *, type_ref: str, init_ref: str | None, init_unreadable: bool = False
) -> ImportModel:
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IValue",
            name="IValue",
            description="",
            data_elements=[
                SRDataElementInfo(name="value", type_ref=type_ref, init_ref=init_ref, init_unreadable=init_unreadable)
            ],
        )
    )
    return model


def _constant(model: ImportModel, value, *, path="/Pkg/Constants/SR_IValue_value_Init", reason=None) -> ImportModel:
    model.constant_specs.append(
        ConstantSpecInfo(path=path, name=path.rsplit("/", 1)[1], value=value, unreadable_reason=reason)
    )
    return model


def _one_init(model: ImportModel, **kwargs):
    """Map `model` and return (the one data element's init, the skip log)."""
    _data_types, _registry, _type_skips, interfaces, iface_skip_log = (
        _map(model) if not kwargs else _map_with(model, **kwargs)
    )
    iface = _by_name(interfaces, "IValue")
    return iface.data_elements[0].init, iface_skip_log


def _map_with(model: ImportModel, **kwargs):
    forms: list = []
    data_types, registry, type_skip_log = map_data_types(model, elements=forms)
    interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms, **kwargs)
    return data_types, registry, type_skip_log, interfaces, iface_skip_log


class TestTheMappedInitValueFollowsTheDeclaredType:
    def test_a_primitive_takes_a_scalar(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=255))
        init, skips = _one_init(model)
        assert init == 255
        assert len(skips) == 0

    def test_an_absent_init_is_none_and_is_not_a_finding(self):
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=None)
        init, skips = _one_init(model)
        assert init is None
        assert len(skips) == 0

    def test_a_record_is_matched_positionally_and_the_labels_are_ignored(self):
        """A record init is matched by declared field order; the members' SHORT-LABELs are ignored."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Point", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(
            model,
            ConstantRecord(
                fields=[
                    ConstantRecordField(label="SR_IValue_LongDecorativeName_Init", value=ConstantScalar(value=5)),
                    ConstantRecordField(label=None, value=ConstantScalar(value=10)),
                ]
            ),
        )
        init, skips = _one_init(model)
        assert init == {"x": 5, "y": 10}
        assert len(skips) == 0

    def test_an_array_takes_exactly_its_declared_length(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Triple", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(
            model,
            ConstantArray(members=[ConstantScalar(value=0), ConstantScalar(value=1), ConstantScalar(value=16)]),
        )
        init, skips = _one_init(model)
        assert init == [0, 1, 16]
        assert len(skips) == 0

    def test_a_nested_composite_recurses(self):
        """A nested composite follows the declared type at every depth."""
        model = _model_with_types()
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Pkg/AppTypes/PointPair",
                name="PointPair",
                element_type_ref="/Pkg/AppTypes/Point",
                max_number_of_elements=2,
                description="",
            )
        )
        _with_interface(model, type_ref="/Pkg/AppTypes/PointPair", init_ref="/Pkg/Constants/SR_IValue_value_Init")
        _constant(
            model,
            ConstantArray(
                members=[
                    ConstantRecord(
                        fields=[
                            ConstantRecordField(label=None, value=ConstantScalar(value=1)),
                            ConstantRecordField(label=None, value=ConstantScalar(value=2)),
                        ]
                    ),
                    ConstantRecord(
                        fields=[
                            ConstantRecordField(label=None, value=ConstantScalar(value=3)),
                            ConstantRecordField(label=None, value=ConstantScalar(value=4)),
                        ]
                    ),
                ]
            ),
        )
        init, skips = _one_init(model)
        assert init == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        assert len(skips) == 0


class TestAShapeTheDeclaredTypeCannotHoldIsASkipWithReason:
    """A shape the declared type cannot hold lands the element without its init, naming the cause."""

    def _refused(self, model):
        init, skips = _one_init(model)
        assert init is None
        assert len(skips) == 1
        assert list(skips)[0].path == "/Pkg/Interfaces/IValue/value"
        assert list(skips)[0].tag == "INIT-VALUE"
        return list(skips)[0].reason

    def test_a_composite_where_a_scalar_is_declared(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantArray(members=[ConstantScalar(value=1)]))
        reason = self._refused(model)
        assert "takes a single value" in reason

    def test_a_scalar_where_a_record_is_declared(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Point", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=1))
        assert "needs a RECORD-VALUE-SPECIFICATION" in self._refused(model)

    def test_a_scalar_where_an_array_is_declared(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Triple", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=1))
        assert "needs an ARRAY-VALUE-SPECIFICATION" in self._refused(model)

    def test_a_record_member_count_mismatch_names_both_counts(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Point", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantRecord(fields=[ConstantRecordField(label=None, value=ConstantScalar(value=1))]))
        reason = self._refused(model)
        assert "declares 2 fields" in reason
        assert "carries 1 members" in reason

    def test_an_array_length_mismatch_names_both_lengths(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Triple", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantArray(members=[ConstantScalar(value=1), ConstantScalar(value=2)]))
        reason = self._refused(model)
        assert "declares length 3" in reason
        assert "carries 2 members" in reason

    def test_a_failure_nested_inside_a_composite_says_where(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Point", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(
            model,
            ConstantRecord(
                fields=[
                    ConstantRecordField(label=None, value=ConstantScalar(value=1)),
                    ConstantRecordField(label=None, value=ConstantArray(members=[])),
                ]
            ),
        )
        assert "init.y" in self._refused(model)

    def test_a_dangling_constant_reference_names_the_path_it_could_not_find(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/Missing_Init"
        )
        reason = self._refused(model)
        assert "/Pkg/Constants/Missing_Init" in reason
        assert "not a CONSTANT-SPECIFICATION in this file" in reason

    def test_an_unreadable_constant_carries_the_readers_own_reason(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, None, reason="value 'GEAR_NEUTRAL' is not a number this reader can read")
        reason = self._refused(model)
        assert "GEAR_NEUTRAL" in reason


class TestTheEnumInitIsMatchedByValue:
    """An enumeration's init is matched by the literal's NAME, the text of a ``VT``.

    A number is read as a literal only through the file's table, by the walk over the source's types; else
    that walk names it (``[constr_1221]`` for a prototype's own value). The mapper reads no number."""

    def _enum_model(self, scales: list[CompuScaleInfo]) -> ImportModel:
        model = _model_with_types()
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Pkg/CompuMethods/Gear_CompuMethod",
                name="Gear_CompuMethod",
                category="TEXTTABLE",
                scales=scales,
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Gear",
                name="Gear",
                compu_method_ref="/Pkg/CompuMethods/Gear_CompuMethod",
                description="",
            )
        )
        return _with_interface(model, type_ref="/Pkg/AppTypes/Gear", init_ref="/Pkg/Constants/SR_IValue_value_Init")

    def test_a_number_equal_to_a_literals_value_is_read_as_that_literal(self):
        """A number equal to a literal's value is read through the table as that literal and carried.

        It is still the file's defect (``[constr_1221]``: a prototype's own value of an enumeration is an
        application value whose text names a scale), so the line stays in section 1, descriptive in effect."""
        model = self._enum_model(
            [CompuScaleInfo(value=0, literal="GEAR_NEUTRAL"), CompuScaleInfo(value=1, literal="GEAR_DRIVE")]
        )
        _constant(model, ConstantScalar(value=1))
        init, skips = _one_init(model)
        assert init == "GEAR_DRIVE"
        (entry,) = list(skips)
        assert (entry.path, entry.tag, entry.category, entry.rule, entry.effect) == (
            "/Pkg/Constants/SR_IValue_value_Init",
            "VALUE-SPEC",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
            "report",
        )
        assert entry.reason == (
            "init value carried: given as the number 1, carried as the literal GEAR_DRIVE its type's table gives "
            "that number"
        )

    def test_the_enumeration_names_its_table_and_the_tables_entries_answer_the_init(self):
        """An enumeration naming a ``texttable`` answers the init from the table's entries."""
        model = self._enum_model(
            [CompuScaleInfo(value=0, literal="GEAR_NEUTRAL"), CompuScaleInfo(value=1, literal="GEAR_DRIVE")]
        )
        _constant(model, ConstantScalar(value="GEAR_DRIVE", form=ValueForm.APPLICATION))
        data_types, _registry, _type_skips, interfaces, skips = _map(model)
        (gear,) = [dt for dt in data_types if dt.name == "Gear"]
        assert (gear.literals, gear.compu_method_ref) == (None, "Gear_CompuMethod")
        assert _by_name(interfaces, "IValue").data_elements[0].init == "GEAR_DRIVE"
        assert len(skips) == 0

    def test_a_table_in_a_conflicting_group_answers_no_init(self):
        """A table whose name is declared twice, differently, answers no init.

        One not-supported line, the export's path kept.
        """
        from swcraft_tools.arxml_import.merge_verdict import decide_merges
        from swcraft_tools.arxml_import.unresolved import UnresolvedReferences

        model = self._enum_model(
            [CompuScaleInfo(value=0, literal="GEAR_NEUTRAL"), CompuScaleInfo(value=1, literal="GEAR_DRIVE")]
        )
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Other/CompuMethods/Gear_CompuMethod",
                name="Gear_CompuMethod",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=0, literal="GEAR_PARK")],
                description="",
            )
        )
        # A text naming a scale of the source's table: the walk names nothing, and the mapper's line stands.
        _constant(model, ConstantScalar(value="GEAR_DRIVE", form=ValueForm.APPLICATION))
        # Both declarations are the export's: the reader indexes them.
        from xml.etree import ElementTree

        for method in model.compu_methods:
            model.path_index[method.path] = RawElement(method.path, "COMPU-METHOD", ElementTree.Element("COMPU-METHOD"))
        merges = decide_merges(model)
        unresolved = UnresolvedReferences(model, merges)
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, merges=merges, unresolved=unresolved, elements=forms)
        interfaces, skips = map_interfaces(
            model, registry, data_types, merges=merges, unresolved=unresolved, elements=forms
        )

        assert [str(group.key) for group in merges.groups] == ["compu_methods/COMPU-METHOD/Gear_CompuMethod"]
        (gear,) = [dt for dt in data_types if dt.name == "Gear"]
        assert gear.compu_method_ref == "/Pkg/CompuMethods/Gear_CompuMethod"
        assert _by_name(interfaces, "IValue").data_elements[0].init is None
        assert [entry.reason for entry in skips] == [
            "init: enum type 'Gear' names a compu method this import does not hold, so the value cannot be "
            "matched with a literal"
        ]

    def test_a_number_of_no_literal_is_the_same_one_line(self):
        """A number of no literal is not carried, with the same one line (the file breaks ``[constr_1221]``)."""
        model = self._enum_model(
            [CompuScaleInfo(value=1, literal="GEAR_DRIVE"), CompuScaleInfo(value=2, literal="GEAR_REVERSE")]
        )
        _constant(model, ConstantScalar(value=7))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert entry.reason == f"the init value is not carried: {NOT_AN_ENUMERATION_TEXT.words} (1 leaf)"
        assert (entry.category, entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
        )

    def test_the_mapper_reads_no_number_as_a_literal_even_where_two_literals_share_it(self):
        """The mapper reads no number as a literal, so two literals of one value are never an ambiguity.

        Reached through `_map_init_value` directly: no ARXML route leads to a duplicate-valued enum.
        """
        ambiguous = EnumDataType(
            name="Gear",
            kind="enum",
            literals=[EnumLiteral(name="GEAR_DRIVE", value=1), EnumLiteral(name="GEAR_FORWARD", value=1)],
        )
        mapped, reason = _map_init_value(
            ConstantScalar(value=1), "Gear", DataDescription.last_declared([ambiguous]), "init", application=True
        )
        assert mapped is None
        assert reason.text == (
            "init: enum type 'Gear' takes the name of one of its literals, and SWCraft reads the number 1 as a "
            "literal only through the file's own table"
        )
        assert (reason.category, reason.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_composite_value_for_an_enum_is_refused(self):
        model = self._enum_model([CompuScaleInfo(value=1, literal="GEAR_DRIVE")])
        _constant(model, ConstantArray(members=[ConstantScalar(value=1)]))
        init, skips = _one_init(model)
        assert init is None
        assert "takes a single value, not a composite one" in list(skips)[0].reason

    @pytest.mark.parametrize("number", [1, 1.5], ids=["whole", "not whole"])
    def test_the_mapper_gives_every_number_under_an_enum_one_sentence(self, number):
        """Where the walk named nothing, the mapper gives every number under an enum one sentence."""
        gear = EnumDataType(name="Gear", kind="enum", literals=[EnumLiteral(name="GEAR_DRIVE", value=1)])
        mapped, reason = _map_init_value(
            ConstantScalar(value=number), "Gear", DataDescription.last_declared([gear]), "init", application=True
        )
        assert mapped is None
        assert reason.text == (
            f"init: enum type 'Gear' takes the name of one of its literals, and SWCraft reads the number {number} "
            "as a literal only through the file's own table"
        )
        assert (reason.category, reason.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_float_value_for_an_enum_is_refused(self):
        """A number that is not whole is no literal: the walk names it (``[constr_1221]``).

        A whole one, ``1.0`` included, is read through the table (``test_enum_number_init_import.py``).
        """
        model = self._enum_model([CompuScaleInfo(value=1, literal="GEAR_DRIVE")])
        _constant(model, ConstantScalar(value=1.5))
        init, skips = _one_init(model)
        assert init is None
        assert list(skips)[0].rule == StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE

    def test_a_mapped_enum_still_shapes_its_init_against_the_enum(self):
        """A mapped enum's init is shaped against the enum, and the model carries the literal's NAME."""
        model = self._enum_model([CompuScaleInfo(value=1, literal="GEAR_DRIVE")])
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/GearRaw_impl",
                name="GearRaw_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/uint8",
                impl_type_ref=None,
                description="",
            )
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Pkg/Mapping/PkgMapping",
                name="PkgMapping",
                maps=[
                    DataTypeMapInfo(application_ref="/Pkg/AppTypes/Gear", implementation_ref="/Pkg/Types/GearRaw_impl")
                ],
            )
        )
        _constant(model, ConstantScalar(value="GEAR_DRIVE", form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init == "GEAR_DRIVE"
        assert len(skips) == 0


class TestATextLeafIsReadAsTheEntryItNames:
    """A text under an enumeration is the literal of that name; a number is never read as a literal.

    The text is judged against the source's type first (`judge_init_value`).
    """

    _GEAR = [CompuScaleInfo(value=0, literal="GEAR_NEUTRAL"), CompuScaleInfo(value=1, literal="GEAR_DRIVE")]

    def _enum_model(self) -> ImportModel:
        return TestTheEnumInitIsMatchedByValue()._enum_model(list(self._GEAR))

    def test_a_text_naming_a_literal_is_that_literal(self):
        model = self._enum_model()
        _constant(model, ConstantScalar(value="GEAR_DRIVE", form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init == "GEAR_DRIVE"
        assert len(skips) == 0

    def test_the_same_text_as_a_text_value_is_the_files_defect(self):
        """The same text as a text value is the file's defect.

        A prototype's own value of an application primitive is an application value, ``[constr_1221]``.
        """
        model = self._enum_model()
        _constant(model, ConstantScalar(value="GEAR_DRIVE", form=ValueForm.TEXT))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert (entry.category, entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
        )

    def test_a_text_inside_a_record_names_the_members_literal(self):
        model = self._enum_model()
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Pkg/AppTypes/GearPair",
                name="GearPair",
                elements=[
                    RecordElementInfo(name="now", type_ref="/Pkg/AppTypes/Gear"),
                    RecordElementInfo(name="count", type_ref="/Pkg/AppTypes/Count"),
                ],
                description="",
            )
        )
        model.sr_interfaces[0].data_elements[0].type_ref = "/Pkg/AppTypes/GearPair"
        _constant(
            model,
            ConstantRecord(
                fields=[
                    ConstantRecordField(
                        label="now", value=ConstantScalar(value="GEAR_NEUTRAL", form=ValueForm.APPLICATION)
                    ),
                    ConstantRecordField(label="count", value=ConstantScalar(value=3)),
                ]
            ),
        )
        init, skips = _one_init(model)
        assert init == {"now": "GEAR_NEUTRAL", "count": 3}
        assert len(skips) == 0

    def test_a_number_in_the_application_form_names_no_literal(self):
        """A number no single scale names is not read as a literal, in either form."""
        model = self._enum_model()
        _constant(model, ConstantScalar(value=7, form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init is None
        assert list(skips)[0].rule == StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE

    def test_a_text_naming_no_literal_is_the_files_defect(self):
        """``[constr_1221]``: an enumeration's value matches one of its texts."""
        model = self._enum_model()
        _constant(model, ConstantScalar(value="GEAR_PARK", form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        # The walk over the source's types names it first.
        assert entry.reason == f"the init value is not carried: {NOT_AN_ENUMERATION_TEXT.words} (1 leaf)"
        assert (entry.category, entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
        )

    def test_a_text_spelling_a_literals_number_is_not_that_literal(self):
        model = self._enum_model()
        _constant(model, ConstantScalar(value="1", form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init is None
        assert list(skips)[0].reason == f"the init value is not carried: {NOT_AN_ENUMERATION_TEXT.words} (1 leaf)"

    def test_a_number_under_an_application_type_is_carried_in_the_application_form(self):
        """A number under an application type is carried in the application form with no line at all.

        ``[constr_1221]``'s first sentence is met, and the value carries no unit its type does not state.
        """
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Count", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=12, form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init == 12
        assert len(skips) == 0

    def test_a_number_under_an_application_type_in_the_numerical_form_is_the_files_defect(self):
        """A number in the numerical form under an application type is the file's defect: section 1, abort.

        A prototype's own value of a non-enumeration application primitive is an application value (``[constr_1221]``).
        """
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Count", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=12, form=ValueForm.NUMERICAL))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert entry.reason == (
            "the init value is not carried: a number in the numerical form where its application primitive "
            "type takes an application value (1 leaf)"
        )
        assert (entry.category, entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
        )

    def test_a_text_under_an_application_type_is_not_carried(self):
        """A text in the application form under a numeric type is section 2, never ``[constr_1221]``."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Count", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value="FULL", form=ValueForm.APPLICATION))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert entry.reason == (
            "the init value is not carried: a text in the application form under a type that takes a number (1 leaf)"
        )
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_text_under_an_implementation_type_is_not_carried(self):
        """A text value under an implementation type that references no table is not carried (``[constr_1284]``).

        It is judged against the file's own type before the mapper.
        """
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value="FULL", form=ValueForm.TEXT))
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert entry.reason.endswith(
            "the init value is not carried: a text value under a type that references no TEXTTABLE or "
            "BITFIELD_TEXTTABLE implementation compu method (1 leaf)"
        )
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.TEXT_VALUE_UNDER_A_TABLE_ONLY)

    def test_the_application_form_under_an_implementation_type_is_the_files_defect(self):
        """An implementation-typed prototype's own value is never an application value (``[constr_1385]``).

        This holds whether it holds a text or a number.
        """
        for value in ("FULL", 12):
            model = _with_interface(
                _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
            )
            _constant(model, ConstantScalar(value=value, form=ValueForm.APPLICATION))
            init, skips = _one_init(model)
            assert init is None, value
            (entry,) = list(skips)
            assert entry.reason == (
                "the init value is not carried: an application value where its implementation type takes none (1 leaf)"
            )
            assert (entry.category, entry.rule) == (
                SkipCategory.EXPORT_DEFECT,
                StandardRule.NO_APPLICATION_VALUE_FOR_AN_IMPLEMENTATION_TYPE,
            )

    def test_a_text_is_never_a_hexadecimal_rewrite(self):
        from swcraft_tools.arxml_import.type_mapping import radix_spellings

        assert radix_spellings(ConstantScalar(value="GEAR_DRIVE", form=ValueForm.APPLICATION), "GEAR_DRIVE") == []


class TestTheMapperRefusesATypeItCannotResolve:
    """`_map_init_value` refuses a type it cannot resolve, reached directly: no ARXML route gets there."""

    def test_a_type_name_that_is_not_among_the_mapped_types_is_named(self):
        mapped, reason = _map_init_value(
            ConstantScalar(value=1), "Missing_T", DataDescription.last_declared([]), "init", application=False
        )
        assert mapped is None
        assert reason.text == "init: declared type 'Missing_T' is not among the mapped data types"
        assert (reason.category, reason.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_the_where_prefix_carries_the_nested_path(self):
        """A nested member's reason names where in the value it sits."""
        mapped, reason = _map_init_value(
            ConstantScalar(value=1), "Missing_T", DataDescription.last_declared([]), "init[2].load", application=False
        )
        assert mapped is None
        assert reason.text.startswith("init[2].load: declared type 'Missing_T'")


class TestTheRadixNormalizationIsRecordedRatherThanHidden:
    """A re-spelled value is recorded in the import report: the value survives, the spelling does not."""

    def _registry_lines(self, model: ImportModel, **kwargs) -> list[str]:
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        map_interfaces(model, registry, data_types, elements=forms, **kwargs)
        return [entry.line for entry in registry.init_radix_normalizations]

    def _registry_owners(self, model: ImportModel, **kwargs) -> list[str]:
        """The interface each held line belongs to, which `orchestrator.py`'s `--packages` filter reads."""
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        map_interfaces(model, registry, data_types, elements=forms, **kwargs)
        return [entry.interface for entry in registry.init_radix_normalizations]

    def test_each_line_names_the_interface_that_committed_it(self):
        """Each line names its interface, so the `--packages` closure can filter it out with the interface."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=255, radix_source="0xFF"))

        assert self._registry_owners(model) == ["IValue"]

    def test_a_hexadecimal_scalar_is_recorded_with_its_spelling(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=255, radix_source="0xFF"))
        assert self._registry_lines(model) == ["/Pkg/Interfaces/IValue/value: init read from 0xFF, written decimal"]

    def test_a_decimal_scalar_records_nothing(self):
        """The non-vacuity control: the report counts REWRITES, not reads."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantScalar(value=255))
        assert self._registry_lines(model) == []

    def test_a_nested_hexadecimal_member_is_recorded_too(self):
        """A nested hexadecimal member is recorded like a top-level scalar."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/AppTypes/Triple", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(
            model,
            ConstantArray(
                members=[
                    ConstantScalar(value=0),
                    ConstantScalar(value=1),
                    ConstantScalar(value=16, radix_source="0x10"),
                ]
            ),
        )
        assert self._registry_lines(model) == ["/Pkg/Interfaces/IValue/value: init read from 0x10, written decimal"]

    def test_a_refused_init_records_no_normalization(self):
        """A refused init records no normalization: its skip line says what happened."""
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref="/Pkg/Constants/SR_IValue_value_Init"
        )
        _constant(model, ConstantArray(members=[ConstantScalar(value=255, radix_source="0xFF")]))
        assert self._registry_lines(model) == []

    def test_an_interface_the_import_abandoned_records_nothing(self):
        """An interface the import abandoned records no radix line for its earlier elements.

        A line would name an element in no written file, so the recording cannot happen inside the element loop.
        """
        model = _model_with_types()
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/Interfaces/IValue",
                name="IValue",
                description="",
                data_elements=[
                    SRDataElementInfo(
                        name="good",
                        type_ref="/Pkg/Types/Counter_impl",
                        init_ref="/Pkg/Constants/SR_IValue_value_Init",
                    ),
                    SRDataElementInfo(name="bad", type_ref="/Pkg/Types/NoSuchType", init_ref=None),
                ],
            )
        )
        _constant(model, ConstantScalar(value=255, radix_source="0xFF"))

        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        interfaces, skip_log = map_interfaces(model, registry, data_types, elements=forms)

        assert interfaces == []
        reason = (
            "data element 'bad': unresolved reference /Pkg/Types/NoSuchType "
            "(the file declares no data type at that path)"
        )
        # Each member is chained to the interface's line; none is a radix line.
        assert [entry.reason for entry in skip_log] == [reason] + [
            f"chain-skipped: interface 'IValue' was skipped ({reason})"
        ] * 2
        assert [entry.line for entry in registry.init_radix_normalizations] == []

    def test_an_enum_typed_element_records_nothing_because_the_written_form_is_a_name(self):
        """An enum-typed element records no radix line: a name is written, not a decimal number.

        The value's one line is the reading's, naming the spelling ``0x02`` and citing ``[constr_1221]``.
        """
        model = _model_with_types()
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Pkg/CompuMethods/Gear_CompuMethod",
                name="Gear_CompuMethod",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=2, literal="GEAR_REVERSE")],
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Gear",
                name="Gear",
                compu_method_ref="/Pkg/CompuMethods/Gear_CompuMethod",
                description="",
            )
        )
        _with_interface(model, type_ref="/Pkg/AppTypes/Gear", init_ref="/Pkg/Constants/SR_IValue_value_Init")
        _constant(model, ConstantScalar(value=2, radix_source="0x02"))

        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)

        assert _by_name(interfaces, "IValue").data_elements[0].init == "GEAR_REVERSE"
        assert [(entry.rule, entry.reason) for entry in skips] == [
            (
                StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
                "init value carried: given as the number 0x02, carried as the literal GEAR_REVERSE its type's "
                "table gives that number",
            )
        ]
        assert [entry.line for entry in registry.init_radix_normalizations] == []

    def test_a_hexadecimal_member_beside_an_enum_typed_one_is_still_recorded(self):
        """The radix line is per leaf: a hexadecimal primitive member beside an enum-typed one is recorded."""
        model = _model_with_types()
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Pkg/CompuMethods/Gear_CompuMethod",
                name="Gear_CompuMethod",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=2, literal="GEAR_REVERSE")],
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Gear",
                name="Gear",
                compu_method_ref="/Pkg/CompuMethods/Gear_CompuMethod",
                description="",
            )
        )
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Pkg/AppTypes/GearedCounter",
                name="GearedCounter",
                elements=[
                    RecordElementInfo(name="gear", type_ref="/Pkg/AppTypes/Gear"),
                    RecordElementInfo(name="count", type_ref="/Pkg/AppTypes/Count"),
                ],
                description="",
            )
        )
        _with_interface(model, type_ref="/Pkg/AppTypes/GearedCounter", init_ref="/Pkg/Constants/SR_IValue_value_Init")
        _constant(
            model,
            ConstantRecord(
                fields=[
                    ConstantRecordField(
                        label=None, value=ConstantScalar(value="GEAR_REVERSE", form=ValueForm.APPLICATION)
                    ),
                    ConstantRecordField(label=None, value=ConstantScalar(value=16, radix_source="0x10")),
                ]
            ),
        )

        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)

        assert _by_name(interfaces, "IValue").data_elements[0].init == {"gear": "GEAR_REVERSE", "count": 16}
        assert len(skips) == 0
        assert [entry.line for entry in registry.init_radix_normalizations] == [
            "/Pkg/Interfaces/IValue/value: init read from 0x10, written decimal"
        ]


class TestThePortLevelInitIsThePortsOwnValue:
    """A port COM-SPEC with a value of its own is the port's value; a data element's ``init`` is the element's.

    ``[TPS_SWCT_01182]``: a definition on the com-spec supersedes one on the data prototype, on that port.
    ``map_interfaces`` decides each such value and hands it out on ``PathRegistry.port_inits``. A com-spec naming the
    element's own constant is no override.
    """

    PORT = "/Pkg/Components/Swc/pValue"

    def _with_port(
        self,
        model: ImportModel,
        *,
        constant_ref: str | None,
        element_ref: str | None = "/Pkg/Interfaces/IValue/value",
        unreadable_reason: str | None = None,
        value=None,
        owner: str = "swc",
    ) -> ImportModel:
        """Add one port with one sender COM-SPEC init, on an SWC or a composition (both are walked)."""
        owner_name = "Swc" if owner == "swc" else "Comp"
        port_path = f"/Pkg/Components/{owner_name}/pValue"
        data = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
            data_element_ref=element_ref,
            block_tag="PROVIDED-COM-SPECS",
        )
        port = SwcPortInfo(
            path=port_path,
            name="pValue",
            kind="P-PORT-PROTOTYPE",
            interface_ref="/Pkg/Interfaces/IValue",
            description="",
            com_spec_inits=[
                PortComSpecInitInfo(
                    port_path=port_path,
                    com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                    data_element_ref=element_ref,
                    constant_ref=constant_ref,
                    unreadable_reason=unreadable_reason,
                    block_tag="PROVIDED-COM-SPECS",
                    value=value,
                )
            ],
            data_element_com_specs=[data],
            com_spec_shapes=[
                ComSpecInfo(
                    block_tag="PROVIDED-COM-SPECS",
                    tag="NONQUEUED-SENDER-COM-SPEC",
                    child_tags=("DATA-ELEMENT-REF", "INIT-VALUE"),
                    data=data,
                )
            ],
        )
        if owner == "swc":
            model.swcs.append(
                SwcInfo(
                    path="/Pkg/Components/Swc",
                    name="Swc",
                    description="",
                    ports=[port],
                    runnables=[],
                    events=[],
                )
            )
        else:
            model.compositions.append(
                CompositionInfo(
                    path="/Pkg/Components/Comp",
                    name="Comp",
                    description="",
                    ports=[port],
                    prototypes=[],
                    assembly_connectors=[],
                    delegation_connectors=[],
                )
            )
        return model

    @staticmethod
    def _valued(element_init_ref: str | None = "/Pkg/Constants/SR_IValue_value_Init") -> ImportModel:
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=element_init_ref)
        if element_init_ref is not None:
            _constant(model, ConstantScalar(value=255))
        _constant(model, ConstantScalar(value=0), path="/Pkg/Constants/Other_Init")
        return model

    def _port_key(self, owner: str = "swc") -> tuple[str, str, str, str, int, int]:
        """The outcome's key (``registry.PortInitKey``) for the first com-spec of the owner's first port."""
        owner_name = "Swc" if owner == "swc" else "Comp"
        return (
            f"/Pkg/Components/{owner_name}/pValue",
            "PROVIDED-COM-SPECS",
            "NONQUEUED-SENDER-COM-SPEC",
            "/Pkg/Interfaces/IValue/value",
            0,
            0,
        )

    @staticmethod
    def _swc_port(model: ImportModel, registry, interfaces, iface_skips):
        from swcraft_tools.arxml_import import map_swcs

        swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, port_inits=registry.port_inits, variable_types={}
        )
        (imported,) = swcs
        (port,) = imported.swc.ports
        return port, swc_skips

    def test_a_port_referencing_the_same_constant_is_no_override(self):
        model = self._valued()
        self._with_port(model, constant_ref="/Pkg/Constants/SR_IValue_value_Init")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init == 255
        assert len(skips) == 0
        assert registry.port_inits == {}

    def test_a_port_referencing_a_different_constant_keeps_the_element_s_own_and_is_carried(self):
        model = self._with_port(self._valued(), constant_ref="/Pkg/Constants/Other_Init")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)

        assert _by_name(interfaces, "IValue").data_elements[0].init == 255
        assert len(skips) == 0, "the element's own init is no longer refused for the port's"
        assert registry.port_inits[self._port_key()].value == 0
        port, swc_skips = self._swc_port(model, registry, interfaces, skips)
        assert [(entry.data_element, entry.init) for entry in port.com_specs] == [("value", 0)]
        assert list(swc_skips) == []

    def test_a_composition_s_port_carries_none_and_names_it(self):
        """A composition's port carries no ``com_specs``, so its override is one F4 line at the port's key."""
        from swcraft_tools.arxml_import.swc_mapping import map_ports

        model = self._with_port(self._valued(), constant_ref="/Pkg/Constants/Other_Init", owner="composition")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init == 255
        assert len(skips) == 0
        log = SkipLog()
        map_ports(
            "Comp",
            model.compositions[0].ports,
            {"/Pkg/Interfaces/IValue": "IValue"},
            log,
            variant_rich=False,
            declared=frozenset(),
            chain_logs=(),
            report_interfaces={iface.name: iface for iface in interfaces},
            port_inits=registry.port_inits,
        )
        (entry,) = list(log)
        assert (entry.path, entry.tag, entry.effect) == (
            "/Pkg/Components/Comp/pValue/PROVIDED-COM-SPECS",
            "INIT-VALUE",
            "report",
        )
        assert entry.category is SkipCategory.NOT_SUPPORTED
        assert "a composition's port carries no com-spec values" in entry.reason

    def test_a_port_initializing_an_element_that_declares_none_is_carried(self):
        model = self._with_port(self._valued(element_init_ref=None), constant_ref="/Pkg/Constants/Other_Init")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init is None
        assert len(skips) == 0
        port, _swc_skips = self._swc_port(model, registry, interfaces, skips)
        assert [(entry.data_element, entry.init) for entry in port.com_specs] == [("value", 0)]

    def test_a_port_value_equal_to_the_element_s_through_another_constant_is_no_entry(self):
        model = self._valued()
        _constant(model, ConstantScalar(value=255), path="/Pkg/Constants/Same_Init")
        self._with_port(model, constant_ref="/Pkg/Constants/Same_Init")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        outcome = registry.port_inits[self._port_key()]
        assert (outcome.value, outcome.reason) == (None, None)
        port, swc_skips = self._swc_port(model, registry, interfaces, skips)
        assert port.com_specs == []
        assert list(swc_skips) == []

    def test_a_port_init_the_reader_could_not_follow_quotes_its_reason_and_leaves_the_element_s_own(self):
        """The line quotes the reader's own reason: the reader knows which inner tag it found."""
        model = self._with_port(
            self._valued(),
            constant_ref=None,
            unreadable_reason="INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF",
        )
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init == 255
        assert len(skips) == 0
        port, swc_skips = self._swc_port(model, registry, interfaces, skips)
        assert port.com_specs == []
        (entry,) = list(swc_skips)
        assert (entry.path, entry.tag) == (f"{self.PORT}/PROVIDED-COM-SPECS", "INIT-VALUE")
        assert "INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF" in entry.reason
        assert "of a com-spec not carried" in entry.reason

    def test_a_value_written_in_place_is_carried(self):
        model = self._with_port(self._valued(), constant_ref=None, value=ConstantScalar(value=9))
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        port, swc_skips = self._swc_port(model, registry, interfaces, skips)
        assert [(entry.data_element, entry.init) for entry in port.com_specs] == [("value", 9)]
        assert list(swc_skips) == []

    def test_an_element_whose_own_init_was_unreadable_keeps_none_and_the_port_s_is_carried(self):
        model = _with_interface(
            _model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=None, init_unreadable=True
        )
        _constant(model, ConstantScalar(value=0), path="/Pkg/Constants/Other_Init")
        self._with_port(model, constant_ref="/Pkg/Constants/Other_Init")
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init is None
        assert len(skips) == 0, "the reader's own line names the unreadable INIT-VALUE"
        assert registry.port_inits[self._port_key()].value == 0

    @pytest.mark.parametrize("element_ref", [None, "", "   "], ids=["absent", "empty", "blank"])
    def test_a_com_spec_that_names_no_data_element_is_reported_against_its_port(self, element_ref: str | None):
        """A COM-SPEC without a DATA-ELEMENT-REF is reported against its port, naming the init's constant.

        The element keeps its own init.
        """
        model = self._with_port(self._valued(), constant_ref="/Pkg/Constants/Other_Init", element_ref=element_ref)
        init, skips = _one_init(model)
        assert init == 255
        assert len(skips) == 1
        entry = list(skips)[0]
        assert entry.path == f"{self.PORT}/PROVIDED-COM-SPECS"
        assert entry.tag == "INIT-VALUE"
        # The DISTINGUISHING half of the reason, not the shared substrings.
        assert "missing or blank DATA-ELEMENT-REF" in entry.reason
        assert "for which this import mapped no data element" not in entry.reason
        assert "/Pkg/Constants/Other_Init" in entry.reason

    def test_an_unbindable_com_spec_whose_init_was_unreadable_quotes_the_readers_reason(self):
        model = self._with_port(
            self._valued(),
            constant_ref=None,
            element_ref=None,
            unreadable_reason="INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF",
        )
        init, skips = _one_init(model)
        assert init == 255
        assert len(skips) == 1
        assert "INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF" in list(skips)[0].reason

    def test_a_port_naming_another_element_is_not_decided_against_this_one(self):
        """An unrelated port's init is no override of this element: the lookup is by the element's own path."""
        model = self._with_port(
            self._valued(), constant_ref="/Pkg/Constants/Other_Init", element_ref="/Pkg/Interfaces/IOther/value"
        )
        _data_types, registry, _type_skips, interfaces, skips = _map(model)
        assert _by_name(interfaces, "IValue").data_elements[0].init == 255
        assert [(entry.path, entry.tag) for entry in skips] == [(f"{self.PORT}/PROVIDED-COM-SPECS", "INIT-VALUE")]
        assert registry.port_inits == {}


class TestAComSpecRefNoElementReadsIsReportedRatherThanDropped:
    """A port COM-SPEC init whose present DATA-ELEMENT-REF no element reads is reported, not dropped.

    `report_unread_port_com_spec_inits` runs at the end of `map_interfaces`, the only loop that knows which
    keys were read. A ref goes unread dangling, padded with whitespace, or into a skipped interface (below), or
    under a merged alias's path (:class:`TestAMergedAliasIsNamedAmongTheComSpecCauses`).
    """

    _INIT_REF = "/Pkg/Constants/SR_IValue_value_Init"

    def _with_port(self, model: ImportModel, **kwargs) -> ImportModel:
        """Add a port with the sibling class's builder: the same COM-SPEC, read by the same walk."""
        return TestThePortLevelInitIsThePortsOwnValue._with_port(
            TestThePortLevelInitIsThePortsOwnValue(), model, **kwargs
        )

    @pytest.mark.parametrize("owner", ["swc", "composition"])
    def test_a_dangling_element_ref_is_reported_against_its_port(self, owner: str):
        """A dangling element ref is reported against its port, for both port owners."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        _constant(model, ConstantScalar(value=0), path="/Pkg/Constants/Other_Init")
        self._with_port(
            model,
            constant_ref="/Pkg/Constants/Other_Init",
            element_ref="/Pkg/Interfaces/IValue/missing",
            owner=owner,
        )
        init, skips = _one_init(model)

        # A COM-SPEC that binds to no element is evidence about no element in particular.
        assert init == 255
        assert len(skips) == 1
        entry = list(skips)[0]
        assert entry.path == (
            "/Pkg/Components/Swc/pValue/PROVIDED-COM-SPECS"
            if owner == "swc"
            else "/Pkg/Components/Comp/pValue/PROVIDED-COM-SPECS"
        )
        assert entry.tag == "INIT-VALUE"
        assert "/Pkg/Interfaces/IValue/missing" in entry.reason
        assert "/Pkg/Constants/Other_Init" in entry.reason
        assert "mapped no data element" in entry.reason

    def test_a_whitespace_padded_ref_is_reported_and_not_silently_matched(self):
        """A padded ref is reported, never matched to the element it resembles: no ref is stripped."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        self._with_port(model, constant_ref=self._INIT_REF, element_ref="  /Pkg/Interfaces/IValue/value  ")
        init, skips = _one_init(model)

        assert init == 255
        assert len(skips) == 1
        assert "/Pkg/Interfaces/IValue/value" in list(skips)[0].reason

    def test_a_ref_into_an_interface_that_was_skipped_whole_is_reported_beside_it(self):
        """A ref into an interface skipped whole is reported for its port, after the interface's own line."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Missing", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        self._with_port(model, constant_ref=self._INIT_REF)
        _, _registry, _dt_skips, interfaces, skips = _map(model)

        assert interfaces == []
        assert [(entry.path, entry.tag) for entry in skips] == [
            ("/Pkg/Interfaces/IValue", "SENDER-RECEIVER-INTERFACE"),
            # The interface's member, chained to its line.
            ("/Pkg/Interfaces/IValue/value", "VARIABLE-DATA-PROTOTYPE"),
            ("/Pkg/Components/Swc/pValue/PROVIDED-COM-SPECS", "INIT-VALUE"),
        ]
        # The reason names the one cause that holds: the interface was skipped.
        assert "its interface '/Pkg/Interfaces/IValue' was skipped" in list(skips)[2].reason
        assert "unresolvable" not in list(skips)[2].reason
        assert "merged with another declaration" not in list(skips)[2].reason
        assert list(skips)[2].category is SkipCategory.NOT_SUPPORTED

    def test_an_unread_ref_whose_init_was_unreadable_quotes_the_readers_reason(self):
        """An unread ref with an unreadable init quotes the reader's own reason."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        self._with_port(
            model,
            constant_ref=None,
            element_ref="/Pkg/Interfaces/IValue/missing",
            unreadable_reason="INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF",
        )
        init, skips = _one_init(model)

        assert init == 255
        assert len(skips) == 1
        assert "INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF" in list(skips)[0].reason

    def test_a_ref_an_element_did_read_produces_no_such_line(self):
        """The control: a ref an element read produces no line."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        self._with_port(model, constant_ref=self._INIT_REF)
        init, skips = _one_init(model)

        assert init == 255
        assert len(skips) == 0

    def test_a_port_s_own_value_still_counts_as_having_read_the_key(self):
        """A port's own value decided against the element counts as read."""
        model = _with_interface(_model_with_types(), type_ref="/Pkg/Types/Counter_impl", init_ref=self._INIT_REF)
        _constant(model, ConstantScalar(value=255))
        _constant(model, ConstantScalar(value=0), path="/Pkg/Constants/Other_Init")
        self._with_port(model, constant_ref="/Pkg/Constants/Other_Init")
        init, skips = _one_init(model)

        assert init == 255
        assert list(skips) == []


# The mapper over the WRITER's own output


def _map_the_writers_own_output(arxml_path: str):
    """Run ``map_data_types`` -> ``map_interfaces`` over a SWCraft-written document (no mapping set)."""
    model = read_arxml(arxml_path)
    forms: list = []
    data_types, registry, type_skips = map_data_types(model, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
    skips = [f"{e.tag} at {e.path}: {e.reason}" for log in (model.skip_log, type_skips, iface_skips) for e in log]
    return data_types, interfaces, skips


@pytest.fixture(scope="module")
def swcraft_written_init_values(tmp_path_factory):
    """Return ``fixtures/init_values/model`` generated once, and mapped.

    SWCraft's output differs from a tool's export (constants in ``/PortInterfaces``, a scalar as
    ``NUMERICAL-VALUE-SPECIFICATION``, field-name ``SHORT-LABEL``s), and the mapper needs no change for it.
    """
    output = tmp_path_factory.mktemp("init-values-mapped") / "model.arxml"
    generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output))
    return output, _map_the_writers_own_output(str(output))


class TestTheMapperReadsTheWritersOwnInitValues:
    """The mapper over the writer's own init values: declared type walked, enum by value, record by position.

    Does not reach a record with an array-typed field: ``fixtures/init_values/model`` declares none. That
    shape is held by ``test_arxml_init_writer.py``
    and ``test_arxml_xsd_conformance.py``'s ``TestARecordWithAnArrayFieldConforms``.
    """

    def test_the_writers_own_document_maps_with_no_skip_at_all(self, swcraft_written_init_values) -> None:
        _output, (_data_types, interfaces, skips) = swcraft_written_init_values

        assert skips == []
        assert [i.name for i in interfaces] == ["ISignals"]

    def test_the_enum_leaf_comes_back_as_its_literal_name(self, swcraft_written_init_values) -> None:
        """The writer's literal ``value`` comes back as the literal's name."""
        _output, (_data_types, interfaces, _skips) = swcraft_written_init_values
        elements = {e.name: e.init for e in interfaces[0].data_elements}

        assert elements["mode"] == "ON"
        assert elements["deep"] == [
            {"row": {"index": 1, "mode": "OFF"}},
            {"row": {"index": 2, "mode": "ON"}},
        ]

    def test_a_record_is_matched_positionally_past_swcrafts_labels(self, swcraft_written_init_values, tmp_path) -> None:
        """A record is matched by position: overwriting every ``SHORT-LABEL`` changes nothing."""
        output, (_data_types, interfaces, _skips) = swcraft_written_init_values
        text = output.read_text(encoding="utf-8")
        relabelled = tmp_path / "relabelled.arxml"
        replaced = re.subn(r"<SHORT-LABEL>[^<]*</SHORT-LABEL>", "<SHORT-LABEL>zzz</SHORT-LABEL>", text)
        assert replaced[1] > 0, "the writer emitted no SHORT-LABEL, so this control proves nothing"
        relabelled.write_text(replaced[0], encoding="utf-8", newline="\n")

        _relabelled_types, relabelled_interfaces, relabelled_skips = _map_the_writers_own_output(str(relabelled))

        # The label lines (F12, F10) are descriptive: the value itself is carried.
        assert relabelled_skips
        assert [line for line in relabelled_skips if not line.startswith("SHORT-LABEL at ")] == []
        assert {e.name: e.init for e in relabelled_interfaces[0].data_elements} == {
            e.name: e.init for e in interfaces[0].data_elements
        }

    def test_every_shape_the_writer_emits_comes_back_typed(self, swcraft_written_init_values) -> None:
        """Every shape the writer emits comes back typed; ``flag`` is a recorded loss (a ``bool`` written ``1``)."""
        _output, (_data_types, interfaces, _skips) = swcraft_written_init_values
        elements = {e.name: e.init for e in interfaces[0].data_elements}

        assert elements["speed"] == 7
        assert elements["plain"] is None
        assert elements["flag"] == 1 and type(elements["flag"]) is int
        assert elements["bytes"] == [1, 2]
        assert elements["limits"] == {"low": -1e20, "high": 1e20, "tiny": 1e-7, "whole": 100.0}
        assert all(type(leaf) is float for leaf in elements["limits"].values())


# The source path of every mapped interface -- the whole-export importer's
# write order for `interfaces`


class TestTheRegistryRecordsTheSourcePathOfEveryMappedInterface:
    """``map_interfaces`` records the name -> path map of every committed interface on ``PathRegistry``.

    A skipped duplicate or an abandoned interface is written nowhere and is not recorded.
    """

    def test_the_fixture_interfaces_are_recorded_with_their_own_paths(self, minimal_swc_mapped, client_server_mapped):
        assert minimal_swc_mapped[1].interface_source_paths == {"IWheelSpeed": "/Acme/Chassis/Interfaces/IWheelSpeed"}
        assert client_server_mapped[1].interface_source_paths == {
            "IBrakeControlService": "/Acme/Chassis/Interfaces/IBrakeControlService"
        }

    def test_only_a_committed_interface_is_recorded_and_a_duplicate_keeps_the_first_path(self):
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(_UINT8_IMPL)  # a prototype is never typed by the SW-BASE-TYPE itself
        for path, name, type_ref in (
            ("/PkgA/Interfaces/IDup", "IDup", "/Pkg/Types/uint8_impl"),
            ("/PkgB/Interfaces/IDup", "IDup", "/Pkg/Types/uint8_impl"),
            ("/PkgA/Interfaces/IBroken", "IBroken", "/Pkg/Types/missing"),
        ):
            model.sr_interfaces.append(
                SRInterfaceInfo(
                    path=path,
                    name=name,
                    description="",
                    data_elements=[SRDataElementInfo(name="x", type_ref=type_ref)],
                )
            )

        _data_types, registry, _type_skip_log, interfaces, _iface_skip_log = _map(model)

        assert [interface.name for interface in interfaces] == ["IDup"]
        assert registry.interface_source_paths == {"IDup": "/PkgA/Interfaces/IDup"}


# An operation-level refusal is the OPERATION's, and a merged group's alias is
# a cause of its own


class TestAPortSurvivesAnOperationLevelSkipOnItsInterface:
    """A refusal of one operation does not drop the ports naming its interface.

    ``swc_mapping.build_interface_path_index`` reads a skip at a declaration's path as "did not survive", so
    an operation's refusal must sit at the operation's path; a merged group's alias asks about its primary.

    Does not reach: nothing enforces that a member-level refusal names the member's path; this class is the
    gate that catches a new one recorded at its owner's path.
    """

    @staticmethod
    def _model(*, trees: tuple[str, ...], port_names: str) -> ImportModel:
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/Counter_impl",
                name="Counter_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/uint8",
                impl_type_ref=None,
                description="",
            )
        )
        for tree in trees:
            model.cs_interfaces.append(
                CSInterfaceInfo(
                    path=f"/Pkg/{tree}/ISvc",
                    name="ISvc",
                    description="",
                    operations=[
                        CSOperationInfo(
                            name="   ",  # blank SHORT-NAME: this operation alone is refused
                            arguments=[CSArgumentInfo(name="a", type_ref="/Pkg/Types/Counter_impl", direction="IN")],
                        ),
                        CSOperationInfo(
                            name="Ping",
                            arguments=[CSArgumentInfo(name="b", type_ref="/Pkg/Types/Counter_impl", direction="IN")],
                        ),
                    ],
                )
            )
        model.swcs.append(
            SwcInfo(
                path="/Pkg/Components/Caller",
                name="Caller",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Pkg/Components/Caller/rSvc",
                        name="rSvc",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref=f"/Pkg/{port_names}/ISvc",
                        description="",
                    )
                ],
                runnables=[],
                events=[],
            )
        )
        return model

    @staticmethod
    def _run(model: ImportModel, *, merges=None):
        from swcraft_tools.arxml_import import map_swcs
        from swcraft_tools.arxml_import.merge_verdict import decide_merges

        merges = decide_merges(model) if merges is None else merges
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, merges=merges, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=merges, elements=forms)
        swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, merges=merges, port_inits={}, variable_types={}
        )
        return interfaces, iface_skips, swcs, swc_skips

    def test_the_port_binds_although_an_operation_was_refused(self) -> None:
        interfaces, iface_skips, swcs, swc_skips = self._run(self._model(trees=("Svc",), port_names="Svc"))

        assert [(iface.name, [op.name for op in iface.operations]) for iface in interfaces] == [("ISvc", ["Ping"])]
        # The refusal names the OPERATION's path, not the interface's.
        assert [(entry.path, entry.tag) for entry in iface_skips] == [
            ("/Pkg/Svc/ISvc/<unnamed>", "CLIENT-SERVER-OPERATION")
        ]
        assert [entry.swc.ports[0].interface for entry in swcs] == ["ISvc"]
        assert list(swc_skips) == []

    @pytest.mark.parametrize("port_names", ["TreeA", "TreeB"])
    def test_a_merged_groups_members_both_bind(self, port_names: str) -> None:
        """A port naming either member of a merged group binds to the element the merge kept."""
        interfaces, _iface_skips, swcs, swc_skips = self._run(
            self._model(trees=("TreeA", "TreeB"), port_names=port_names)
        )

        assert [iface.name for iface in interfaces] == ["ISvc"]
        assert [entry.swc.ports[0].interface for entry in swcs] == ["ISvc"]
        assert list(swc_skips) == []


class TestAnOperationTheMapperDroppedPromotesNoUnresolvedRecord:
    """An operation the mapper dropped promotes no unresolved-reference record.

    ``unresolved.commit`` commits once per interface, so the blank argument name is decided before any type
    is resolved, and a dropped operation stages nothing.
    """

    @staticmethod
    def _model(*, blank_sibling: bool) -> ImportModel:
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/Counter_impl",
                name="Counter_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/uint8",
                impl_type_ref=None,
                description="",
            )
        )
        arguments = [CSArgumentInfo(name="result", type_ref="/Pkg/Types/Absent", direction="OUT")]
        if blank_sibling:
            arguments.append(CSArgumentInfo(name="   ", type_ref="/Pkg/Types/Counter_impl", direction="IN"))
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Pkg/Interfaces/ICalibration",
                name="ICalibration",
                description="",
                operations=[
                    CSOperationInfo(name="Calibrate", arguments=arguments),
                    CSOperationInfo(
                        name="Ping",
                        arguments=[CSArgumentInfo(name="b", type_ref="/Pkg/Types/Counter_impl", direction="IN")],
                    ),
                ],
            )
        )
        return model

    @staticmethod
    def _run(model: ImportModel):
        from swcraft_tools.arxml_import.merge_verdict import decide_merges
        from swcraft_tools.arxml_import.unresolved import UnresolvedReferences

        merges = decide_merges(model)
        unresolved = UnresolvedReferences(model, merges)
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, merges=merges, unresolved=unresolved, elements=forms)
        interfaces, iface_skips = map_interfaces(
            model, registry, data_types, merges=merges, unresolved=unresolved, elements=forms
        )
        return interfaces, iface_skips, unresolved

    @staticmethod
    def _argument_referrers(unresolved) -> list[tuple[str, str]]:
        """Every committed ``interfaces.argument.implementation_type`` referrer, as ``(target, member)``.

        Scoped to the role: an inline ``ImportModel`` has no reader path index, so type-layer references
        count as absent.
        """
        return [
            (record.target, referrer.member)
            for record in unresolved.references
            for referrer in record.referrers
            if referrer.field == "interfaces.argument.implementation_type"
        ]

    def test_a_dropped_operation_leaves_no_record_behind(self) -> None:
        interfaces, iface_skips, unresolved = self._run(self._model(blank_sibling=True))

        assert [(iface.name, [op.name for op in iface.operations]) for iface in interfaces] == [
            ("ICalibration", ["Ping"])
        ]
        assert [entry.path for entry in iface_skips] == ["/Pkg/Interfaces/ICalibration/Calibrate"]
        # No referrer, and therefore no line: one here would name an argument in
        # no written file.
        assert self._argument_referrers(unresolved) == []

    def test_the_control_still_records_the_reference_it_really_wrote(self) -> None:
        """The control: without the blank sibling the argument's export path is written and recorded."""
        interfaces, iface_skips, unresolved = self._run(self._model(blank_sibling=False))

        argument = _by_name(interfaces, "ICalibration").operations[0].arguments[0]
        assert (argument.name, argument.implementation_type) == ("result", "/Pkg/Types/Absent")
        assert list(iface_skips) == []
        assert self._argument_referrers(unresolved) == [("/Pkg/Types/Absent", "result")]


class TestAMergedAliasIsNamedAmongTheComSpecCauses:
    """An unread port COM-SPEC key under a merged alias's path names that cause.

    The interface loop reads keys of the primary only. The key is not resolved through the merge: a port's
    value is decided by the constant's path (``port_init_overrides``), the merge compares by value.
    """

    @staticmethod
    def _model(element_ref: str) -> ImportModel:
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint8",
                name="uint8",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/Counter_impl",
                name="Counter_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/uint8",
                impl_type_ref=None,
                description="",
            )
        )
        model.constant_specs.append(
            ConstantSpecInfo(
                path="/Pkg/Constants/SigInit",
                name="SigInit",
                value=ConstantScalar(value=3),
                unreadable_reason=None,
            )
        )
        for tree in ("TreeA", "TreeB"):
            model.sr_interfaces.append(
                SRInterfaceInfo(
                    path=f"/Pkg/{tree}/ISig",
                    name="ISig",
                    description="",
                    data_elements=[SRDataElementInfo(name="sig", type_ref="/Pkg/Types/Counter_impl")],
                )
            )
        model.swcs.append(
            SwcInfo(
                path="/Pkg/Components/Reader",
                name="Reader",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Pkg/Components/Reader/rSig",
                        name="rSig",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref="/Pkg/TreeB/ISig",
                        description="",
                        com_spec_inits=[
                            PortComSpecInitInfo(
                                port_path="/Pkg/Components/Reader/rSig",
                                com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC",
                                data_element_ref=element_ref,
                                constant_ref="/Pkg/Constants/SigInit",
                                unreadable_reason=None,
                            )
                        ],
                    )
                ],
                runnables=[],
                events=[],
            )
        )
        return model

    @staticmethod
    def _run(model: ImportModel):
        from swcraft_tools.arxml_import import map_swcs
        from swcraft_tools.arxml_import.merge_verdict import decide_merges

        merges = decide_merges(model)
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, merges=merges, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=merges, elements=forms)
        swcs, swc_skips = map_swcs(
            model,
            interfaces,
            iface_skip_log=iface_skips,
            merges=merges,
            port_inits=registry.port_inits,
            variable_types={},
        )
        return interfaces, iface_skips, swcs, swc_skips, registry

    def test_the_alias_keyed_com_spec_is_reported_under_a_reason_that_is_true(self) -> None:
        interfaces, iface_skips, swcs, swc_skips, _registry = self._run(self._model("/Pkg/TreeB/ISig/sig"))

        # The group merged and the port bound, so neither older cause describes this line.
        assert [iface.name for iface in interfaces] == ["ISig"]
        assert [entry.swc.ports[0].interface for entry in swcs] == ["ISig"]
        assert list(swc_skips) == []
        entries = [entry for entry in iface_skips if entry.path.endswith("/REQUIRED-COM-SPECS")]
        assert [entry.path for entry in entries] == ["/Pkg/Components/Reader/rSig/REQUIRED-COM-SPECS"]
        assert "merged with another declaration" in entries[0].reason
        assert "/Pkg/TreeB/ISig/sig" in entries[0].reason

    def test_the_primary_keyed_com_spec_is_read_and_produces_no_line(self) -> None:
        """The control: a COM-SPEC naming the kept member is read and produces no line."""
        _interfaces, iface_skips, _swcs, swc_skips, registry = self._run(self._model("/Pkg/TreeA/ISig/sig"))

        assert [entry for entry in iface_skips if entry.path.endswith("/REQUIRED-COM-SPECS")] == []
        assert [outcome.value for outcome in registry.port_inits.values()] == [3]
        assert list(swc_skips) == []
