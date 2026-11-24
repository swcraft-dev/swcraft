"""Tests for the writer's generators: build_workspace, generate_arxml and generate_needs (Sphinx-Needs JSON).

An assertion about the ARXML the writer *produces* is made on the **generated
file**, parsed and resolved by path with ``find_element()``, never on an
in-memory object graph. The assertions that need the object graph go through
``ArxmlDocument.find()``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import yaml
from conftest import _root_composition, model_element_path, write_model_element
from lxml import etree
from swcraft_tools.arxml_version import DEFAULT_ARXML_VERSION, ArxmlVersion
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import (
    ArrayDataType,
    BaseDataType,
    EnumDataType,
    EnumLiteral,
    RecordDataType,
    RecordField,
    ValueDataType,
)
from swcraft_tools.writer import (
    build_workspace,
    generate_arxml,
    generate_arxml_cli,
    generate_needs,
)
from swcraft_tools.writer.arxml_generation import _build_document, resolve_type_ref
from swcraft_tools.writer.arxml_xml import (
    ArxmlDocument,
    ShortNameScope,
    ar_qname,
    find_element,
    short_name_of,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Base type parameters matching the seed values used by ARXML generation.
_PRIMITIVE_PARAMS: dict[str, dict] = {
    "float32": {"size": 32, "encoding": "IEEE754", "native_declaration": "float"},
    "float64": {"size": 64, "encoding": "IEEE754", "native_declaration": "double"},
    "uint8": {"size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    "uint16": {"size": 16, "encoding": "NONE", "native_declaration": "uint16_t"},
    "uint32": {"size": 32, "encoding": "NONE", "native_declaration": "uint32_t"},
    "uint64": {"size": 64, "encoding": "NONE", "native_declaration": "uint64_t"},
    "int8": {"size": 8, "encoding": "2C", "native_declaration": "int8_t"},
    "int16": {"size": 16, "encoding": "2C", "native_declaration": "int16_t"},
    "int32": {"size": 32, "encoding": "2C", "native_declaration": "int32_t"},
    "int64": {"size": 64, "encoding": "2C", "native_declaration": "int64_t"},
    "bool": {"size": 8, "encoding": "BOOLEAN", "native_declaration": "boolean"},
    "boolean": {"size": 8, "encoding": "BOOLEAN", "native_declaration": "boolean"},
}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _generate(model_root: Path) -> etree._Element:
    """Generate ARXML for *model_root* and return the parsed ``AUTOSAR`` root.

    ``dist/`` is not a model layer, so writing into the model root is harmless.
    """
    output = model_root / "dist" / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return etree.parse(str(output)).getroot()


def _child_text(element: etree._Element, tag: str) -> str | None:
    """Return the text of *element*'s direct child *tag*, or None if it has none."""
    child = element.find(ar_qname(tag))
    return None if child is None else child.text


def _type_ref(root: etree._Element, path: str) -> str | None:
    """Return the ``TYPE-TREF`` of the element at *path*, or None."""
    element = find_element(root, path)
    assert element is not None, f"no element at {path}"
    return _child_text(element, "TYPE-TREF")


def _record_fields(root: etree._Element, path: str) -> list[tuple[str | None, str | None]]:
    """Return the ``(name, TYPE-TREF)`` pairs of a record's elements, in order."""
    record = find_element(root, path)
    assert record is not None, f"no record at {path}"
    elements = record.find(ar_qname("ELEMENTS"))
    assert elements is not None, f"record at {path} has no ELEMENTS"
    return [(short_name_of(field), _child_text(field, "TYPE-TREF")) for field in elements]


def _mapping_set_pairs(root: etree._Element, name: str) -> list[tuple[str | None, str | None]]:
    """The ``(application ref, implementation ref)`` pairs of one DATA-TYPE-MAPPING-SET.

    Empty when the document carries no set of that name. The writer invents none,
    so the name is always a **declared** element's.
    """
    mapping_set = find_element(root, f"/DataTypes/MappingSets/{name}")
    if mapping_set is None:
        return []
    maps = mapping_set.find(ar_qname("DATA-TYPE-MAPS"))
    assert maps is not None, "DATA-TYPE-MAPPING-SET has no DATA-TYPE-MAPS"
    return [
        (_child_text(entry, "APPLICATION-DATA-TYPE-REF"), _child_text(entry, "IMPLEMENTATION-DATA-TYPE-REF"))
        for entry in maps
    ]


def _write_primitive(tmp_path: Path, name: str) -> None:
    """Write a *name* value type: a ``{name}_base`` base type and a ``{name}`` value type over it.

    The base type carries the size, encoding and native declaration; ``{name}``
    is the implementation type a prototype may be typed by.
    """
    params = _PRIMITIVE_PARAMS[name]
    _write_yaml(
        model_element_path(tmp_path, f"{name}_base", kind="base_type"),
        {"name": f"{name}_base", "kind": "base_type", **params},
    )
    write_model_element(tmp_path, {"name": name, "kind": "value_type", "base": f"{name}_base"})


def _minimal_swc(name: str = "SpeedCtrlSWC", ports: list | None = None) -> dict:
    return {
        "name": name,
        "type": "application",
        "ports": ports or [],
        "runnables": [],
        "handle_termination_and_restart": "no_support",
    }


def _minimal_interface(name: str = "ISpeed") -> dict:
    return {
        "name": name,
        "kind": "sender_receiver",
        "data_elements": [{"name": "speed", "implementation_type": "float32"}],
    }


def _write_connector_model(tmp_path: Path) -> None:
    _write_primitive(tmp_path, "float32")
    _write_yaml(
        tmp_path / "interfaces" / "ISpeed.yaml",
        _minimal_interface(),
    )
    _write_yaml(
        tmp_path / "swc" / "sensors" / "SpeedSensorSWC.yaml",
        _minimal_swc(
            "SpeedSensorSWC",
            [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
        ),
    )
    _write_yaml(
        tmp_path / "swc" / "control" / "SpeedCtrlSWC.yaml",
        _minimal_swc(
            "SpeedCtrlSWC",
            [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
        ),
    )
    _write_yaml(
        tmp_path / "connectors" / "SpeedSensor_to_SpeedCtrl.yaml",
        {
            "name": "SpeedSensor_to_SpeedCtrl",
            "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
            "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
        },
    )
    # The connector names no composition, so it belongs to the root, which must
    # declare both endpoint types as members: the writer resolves an endpoint's
    # TYPE to the MEMBER of that type inside the connector's composition.
    _root_composition(tmp_path, ["SpeedSensorSWC", "SpeedCtrlSWC"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def model_with_composite_swc_and_iface(tmp_path: Path) -> Path:
    """model_root with one SWC whose provide port's SR interface carries a record -> array -> enum chain."""
    write_model_element(
        tmp_path,
        {
            "name": "GearPosition",
            "kind": "enum",
            "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
        },
    )
    write_model_element(
        tmp_path,
        {
            "name": "GearHistory",
            "kind": "array",
            "element_type": "GearPosition",
            "length": 3,
        },
    )
    write_model_element(
        tmp_path,
        {
            "name": "StatusRecord",
            "kind": "record",
            "fields": [{"name": "recentGears", "type": "GearHistory"}],
        },
    )
    _write_yaml(
        tmp_path / "interfaces" / "IStatus.yaml",
        {
            "name": "IStatus",
            "kind": "sender_receiver",
            "data_elements": [{"name": "status", "application_type": "StatusRecord"}],
        },
    )
    _write_yaml(
        tmp_path / "swc" / "body" / "StatusSWC.yaml",
        _minimal_swc(
            "StatusSWC",
            [{"name": "pStatus", "direction": "provide", "interface": "IStatus"}],
        ),
    )
    return tmp_path


@pytest.fixture
def model_with_swc_and_iface(tmp_path: Path) -> Path:
    """model_root with one SR interface and one SWC that uses it."""
    _write_primitive(tmp_path, "float32")
    _write_yaml(
        tmp_path / "interfaces" / "ISpeed.yaml",
        _minimal_interface(),
    )
    _write_yaml(
        tmp_path / "swc" / "body" / "SpeedCtrlSWC.yaml",
        {
            "name": "SpeedCtrlSWC",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "require", "interface": "ISpeed"}],
            "runnables": [
                {
                    "name": "Run1",
                    "min_start_interval": 0.01,
                    "events": [{"type": "timing", "period": 0.01}],
                }
            ],
            "handle_termination_and_restart": "no_support",
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_type_ref
# ---------------------------------------------------------------------------


class TestResolveTypeRef:
    """The five kinds a prototype may be typed by resolve; an undeclared name and a base type return None."""

    def _registry(self) -> dict:
        return {
            "float32": BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754"),
            "Speed": ValueDataType(name="Speed", kind="value_type", base="float32"),
            "GearPosition": EnumDataType(
                name="GearPosition",
                kind="enum",
                literals=[EnumLiteral(name="PARK", value=0)],
            ),
            "StatusRecord": RecordDataType(
                name="StatusRecord",
                kind="record",
                fields=[RecordField(name="gear", type="GearPosition")],
            ),
            "StatusArray": ArrayDataType(name="StatusArray", kind="array", element_type="StatusRecord", length=4),
        }

    def test_base_type_resolves_to_nothing(self) -> None:
        """A base type resolves to nothing: no data prototype may be typed by one, so every caller refuses it."""
        for implementation in (True, False):
            assert (
                resolve_type_ref(
                    "float32", DataDescription.last_declared(self._registry().values()), implementation=implementation
                )
                is None
            )

    def test_value_type_resolves_to_bare_path(self) -> None:
        assert (
            resolve_type_ref("Speed", DataDescription.last_declared(self._registry().values()), implementation=True)
            == "/DataTypes/Implementation/Speed"
        )

    def test_enum_resolves_to_bare_path(self) -> None:
        assert (
            resolve_type_ref(
                "GearPosition", DataDescription.last_declared(self._registry().values()), implementation=False
            )
            == "/DataTypes/Application/GearPosition"
        )

    def test_record_resolves_to_bare_path(self) -> None:
        assert (
            resolve_type_ref(
                "StatusRecord", DataDescription.last_declared(self._registry().values()), implementation=False
            )
            == "/DataTypes/Application/StatusRecord"
        )

    def test_array_resolves_to_bare_path(self) -> None:
        assert (
            resolve_type_ref(
                "StatusArray", DataDescription.last_declared(self._registry().values()), implementation=False
            )
            == "/DataTypes/Application/StatusArray"
        )

    def test_undeclared_name_returns_none(self) -> None:
        for implementation in (True, False):
            assert (
                resolve_type_ref(
                    "DoesNotExist",
                    DataDescription.last_declared(self._registry().values()),
                    implementation=implementation,
                )
                is None
            )


# ---------------------------------------------------------------------------
# build_workspace
# ---------------------------------------------------------------------------


class TestBuildWorkspaceSeam:
    """``build_workspace()`` is a pinned public seam.

    It keeps its **single-argument** signature and its place in
    ``writer/__init__.py``'s ``__all__``, and returns an :class:`ArxmlDocument`.
    The version is *not* a keyword here: it enters at ``generate_arxml()``, and an
    internal ``_build_document(model_root, version)`` carries it.
    """

    def test_is_exported_from_the_writer_package(self) -> None:
        import swcraft_tools.writer as writer_package

        assert "build_workspace" in writer_package.__all__
        assert writer_package.build_workspace is build_workspace

    def test_takes_exactly_one_parameter(self) -> None:
        assert list(inspect.signature(build_workspace).parameters) == ["model_root"]

    def test_returns_an_arxml_document(self, model_with_swc_and_iface: Path) -> None:
        assert isinstance(build_workspace(str(model_with_swc_and_iface)), ArxmlDocument)

    def test_the_returned_document_declares_the_default_version(self, model_with_swc_and_iface: Path) -> None:
        assert build_workspace(str(model_with_swc_and_iface)).version is DEFAULT_ARXML_VERSION

    def test_the_returned_document_resolves_paths(self, model_with_swc_and_iface: Path) -> None:
        """``document.find()`` resolves the declared elements by path."""
        document = build_workspace(str(model_with_swc_and_iface))
        assert document.find("/ComponentTypes/SpeedCtrlSWC") is not None
        assert document.find("/PortInterfaces/ISpeed") is not None
        assert document.find("/DataTypes/Implementation/float32") is not None
        assert document.find("/DataTypes/Base/float32_base") is not None

    def test_internal_build_document_carries_the_version(self, model_with_swc_and_iface: Path) -> None:
        document = _build_document(str(model_with_swc_and_iface), ArxmlVersion.V4_3_0)
        assert document.version is ArxmlVersion.V4_3_0


class TestGeneratedDocument:
    """What the generated ARXML contains, resolved by path."""

    def test_document_contains_swc(self, model_with_swc_and_iface: Path) -> None:
        assert find_element(_generate(model_with_swc_and_iface), "/ComponentTypes/SpeedCtrlSWC") is not None

    def test_document_contains_port_interface(self, model_with_swc_and_iface: Path) -> None:
        assert find_element(_generate(model_with_swc_and_iface), "/PortInterfaces/ISpeed") is not None

    def test_document_contains_base_type(self, model_with_swc_and_iface: Path) -> None:
        base = find_element(_generate(model_with_swc_and_iface), "/DataTypes/Base/float32_base")
        assert base is not None
        assert etree.QName(base).localname == "SW-BASE-TYPE"

    def test_document_contains_the_value_type(self, model_with_swc_and_iface: Path) -> None:
        """The implementation data type is the declared value type, and no ``_impl`` twin is generated."""
        root = _generate(model_with_swc_and_iface)
        impl = find_element(root, "/DataTypes/Implementation/float32")
        assert impl is not None
        assert etree.QName(impl).localname == "IMPLEMENTATION-DATA-TYPE"
        assert find_element(root, "/DataTypes/Implementation/float32_impl") is None
        assert find_element(root, "/DataTypes/Implementation/float32_base_impl") is None

    def test_document_contains_the_declared_root_composition(self, tmp_path: Path) -> None:
        """The composition emitted is the one the model declares, by its own name; none is invented."""
        _write_connector_model(tmp_path)

        document = _generate(tmp_path)
        assert find_element(document, "/ComponentTypes/Root") is not None
        assert find_element(document, "/ComponentTypes/SwCraftComposition") is None

    @pytest.mark.parametrize("type_name", ["uint32", "int8", "bool"])
    def test_declared_value_types_over_base_types(self, tmp_path: Path, type_name: str) -> None:
        """A value type emits its ``{name}_base`` SW-BASE-TYPE and the declared ``{name}``, and no ``_impl`` twin."""
        _write_primitive(tmp_path, type_name)
        _write_yaml(
            tmp_path / "interfaces" / "ITyped.yaml",
            {
                "name": "ITyped",
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": type_name}],
            },
        )
        root = _generate(tmp_path)
        assert find_element(root, f"/DataTypes/Base/{type_name}_base") is not None
        assert find_element(root, f"/DataTypes/Implementation/{type_name}") is not None
        assert find_element(root, f"/DataTypes/Implementation/{type_name}_impl") is None
        assert find_element(root, f"/DataTypes/Implementation/{type_name}_base_impl") is None

    def test_value_type_is_emitted_under_its_own_name(self, tmp_path: Path) -> None:
        """A value type produces an IMPLEMENTATION-DATA-TYPE named {name} (no _impl suffix)."""
        _write_primitive(tmp_path, "float32")
        write_model_element(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32_base"})
        _write_yaml(
            tmp_path / "interfaces" / "IVSpeed.yaml",
            {
                "name": "IVSpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "VehicleSpeed"}],
            },
        )
        root = _generate(tmp_path)
        assert find_element(root, "/DataTypes/Implementation/VehicleSpeed") is not None
        assert find_element(root, "/DataTypes/Implementation/VehicleSpeed_impl") is None

    def test_value_type_typeref_in_interface(self, tmp_path: Path) -> None:
        """Interface referencing a value type uses /DataTypes/{name} (not {name}_impl)."""
        _write_primitive(tmp_path, "float32")
        write_model_element(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32_base"})
        _write_yaml(
            tmp_path / "interfaces" / "IVSpeed.yaml",
            {
                "name": "IVSpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "VehicleSpeed"}],
            },
        )
        assert (
            _type_ref(_generate(tmp_path), "/PortInterfaces/IVSpeed/speed") == "/DataTypes/Implementation/VehicleSpeed"
        )

    def test_enum_not_referenced_is_still_emitted(self, tmp_path: Path) -> None:
        """An enum declared but referenced by no interface is emitted anyway.

        With its auto-generated COMPU-METHOD."""
        write_model_element(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        root = _generate(tmp_path)
        assert find_element(root, "/DataTypes/Application/GearPosition") is not None
        assert find_element(root, "/DataTypes/Application/GearPosition_CompuMethod") is not None

    def test_enum_referenced_by_interface_generates(self, tmp_path: Path) -> None:
        """Enum type referenced by an interface DataElement generates successfully."""
        write_model_element(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        _write_yaml(
            tmp_path / "interfaces" / "IGear.yaml",
            {
                "name": "IGear",
                "kind": "sender_receiver",
                "data_elements": [{"name": "gear", "application_type": "GearPosition"}],
            },
        )
        assert _type_ref(_generate(tmp_path), "/PortInterfaces/IGear/gear") == "/DataTypes/Application/GearPosition"

    def test_record_referenced_by_interface_generates(self, tmp_path: Path) -> None:
        """Record type referenced by an interface DataElement generates successfully."""
        _write_primitive(tmp_path, "uint8")
        write_model_element(
            tmp_path,
            {
                "name": "StatusRecord",
                "kind": "record",
                "fields": [{"name": "code", "type": "Code"}],
            },
        )
        write_model_element(tmp_path, {"name": "Code", "kind": "application_type"})
        write_model_element(
            tmp_path,
            {"name": "Codes", "maps": [{"application": "Code", "implementation": "uint8"}]},
            layer="mapping_sets",
        )
        _write_yaml(
            tmp_path / "interfaces" / "IStatus.yaml",
            {
                "name": "IStatus",
                "kind": "sender_receiver",
                "data_elements": [{"name": "status", "application_type": "StatusRecord"}],
            },
        )
        root = _generate(tmp_path)
        assert _type_ref(root, "/PortInterfaces/IStatus/status") == "/DataTypes/Application/StatusRecord"
        # The field references the DECLARED application type: AUTOSAR allows only
        # an APPLICATION-* DEST here.
        assert _record_fields(root, "/DataTypes/Application/StatusRecord") == [("code", "/DataTypes/Application/Code")]
        assert find_element(root, "/DataTypes/Application/Code") is not None
        assert _mapping_set_pairs(root, "Codes") == [("/DataTypes/Application/Code", "/DataTypes/Implementation/uint8")]

    def test_array_referenced_by_interface_generates(self, tmp_path: Path) -> None:
        """Array type referenced by an interface DataElement generates successfully."""
        _write_primitive(tmp_path, "uint8")
        write_model_element(
            tmp_path,
            {
                "name": "CodeArray",
                "kind": "array",
                "element_type": "Code",
                "length": 4,
            },
        )
        write_model_element(tmp_path, {"name": "Code", "kind": "application_type"})
        write_model_element(
            tmp_path,
            {"name": "Codes", "maps": [{"application": "Code", "implementation": "uint8"}]},
            layer="mapping_sets",
        )
        _write_yaml(
            tmp_path / "interfaces" / "ICodes.yaml",
            {
                "name": "ICodes",
                "kind": "sender_receiver",
                "data_elements": [{"name": "codes", "application_type": "CodeArray"}],
            },
        )
        root = _generate(tmp_path)
        assert _type_ref(root, "/PortInterfaces/ICodes/codes") == "/DataTypes/Application/CodeArray"
        element = find_element(root, "/DataTypes/Application/CodeArray/Element")
        assert element is not None
        # Same APPLICATION-* DEST restriction as the record field above.
        assert _child_text(element, "TYPE-TREF") == "/DataTypes/Application/Code"
        assert _child_text(element, "MAX-NUMBER-OF-ELEMENTS") == "4"
        assert _mapping_set_pairs(root, "Codes") == [("/DataTypes/Application/Code", "/DataTypes/Implementation/uint8")]

    def test_empty_data_types_dir_no_interfaces_generates(self, tmp_path: Path) -> None:
        """Empty data_types dir with no interfaces produces a valid, empty document."""
        (tmp_path / "data_types").mkdir()
        root = _generate(tmp_path)
        assert [short_name_of(package) for package in find_element(root, "/DataTypes").getparent()] == [
            "DataTypes",
            "PortInterfaces",
            "ComponentTypes",
        ]


class TestBuildWorkspace:
    def test_invalid_yaml_raises_naming_the_offending_file(self, tmp_path: Path) -> None:
        """A schema error is a ``ValueError`` naming the file, so a generation failure says which file to fix."""
        _write_yaml(
            tmp_path / "swc" / "body" / "BadSWC.yaml",
            {"name": "BadSWC", "type": "invalid_type"},  # invalid type literal
        )
        with pytest.raises(ValueError, match="BadSWC.yaml"):
            build_workspace(str(tmp_path))

    def test_workspace_unknown_type_raises(self, tmp_path: Path) -> None:
        """Undeclared type name referenced by interface raises ValueError."""
        _write_yaml(
            tmp_path / "interfaces" / "ICustom.yaml",
            {
                "name": "ICustom",
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": "my_custom_type"}],
            },
        )
        with pytest.raises(ValueError, match="not declared"):
            build_workspace(str(tmp_path))

    def test_value_type_over_value_type_raises(self, tmp_path: Path) -> None:
        """A value type whose base is another value type (not a base type) raises ValueError.

        A type over a type is a ``reference_type``; a ``value_type``'s base resolves
        in the base type layer alone, so a value type named there is not declared in it.
        """
        _write_primitive(tmp_path, "float32")
        write_model_element(tmp_path, {"name": "Speed", "kind": "value_type", "base": "float32_base"})
        write_model_element(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "Speed"})
        with pytest.raises(ValueError, match=r"base type 'Speed' is not declared in data_types/base/"):
            build_workspace(str(tmp_path))

    def test_deeply_nested_array_of_record_with_enum_field_generates(self, tmp_path: Path) -> None:
        """An array of a record with an enum field, referenced by an SR interface, generates.

        It exercises the topological order: the enum before the record before the array.
        """
        write_model_element(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        write_model_element(
            tmp_path,
            {
                "name": "StatusRecord",
                "kind": "record",
                "fields": [{"name": "gear", "type": "GearPosition"}],
            },
        )
        write_model_element(
            tmp_path,
            {
                "name": "StatusRecordArray",
                "kind": "array",
                "element_type": "StatusRecord",
                "length": 4,
            },
        )
        _write_yaml(
            tmp_path / "interfaces" / "IStatusHistory.yaml",
            {
                "name": "IStatusHistory",
                "kind": "sender_receiver",
                "data_elements": [{"name": "history", "application_type": "StatusRecordArray"}],
            },
        )
        root = _generate(tmp_path)
        assert _type_ref(root, "/PortInterfaces/IStatusHistory/history") == "/DataTypes/Application/StatusRecordArray"
        assert (
            _type_ref(root, "/DataTypes/Application/StatusRecordArray/Element") == "/DataTypes/Application/StatusRecord"
        )
        assert _record_fields(root, "/DataTypes/Application/StatusRecord") == [
            ("gear", "/DataTypes/Application/GearPosition")
        ]
        assert find_element(root, "/DataTypes/Application/GearPosition") is not None

    def test_array_element_type_undeclared_raises(self, tmp_path: Path) -> None:
        """An array whose element type is undeclared raises ValueError naming the array and the type.

        Mirrors the record field test."""
        write_model_element(
            tmp_path,
            {
                "name": "CodeArray",
                "kind": "array",
                "element_type": "Missing",
                "length": 4,
            },
        )
        with pytest.raises(ValueError) as exc_info:
            build_workspace(str(tmp_path))

        message = str(exc_info.value)
        assert "CodeArray" in message
        assert "Missing" in message

    def test_a_type_named_like_the_retired_impl_twin_builds(self, tmp_path: Path) -> None:
        """A declared ``float32_impl`` beside base type ``float32`` collides with nothing: no twin is generated."""
        write_model_element(
            tmp_path,
            {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float"},
        )
        write_model_element(tmp_path, {"name": "float32_impl", "kind": "value_type", "base": "float32"})
        document = build_workspace(str(tmp_path))
        assert document.find("/DataTypes/Implementation/float32_impl") is not None

    def test_compu_method_name_collision_raises(self, tmp_path: Path) -> None:
        """A declared type named '{x}_CompuMethod' collides with enum '{x}''s generated COMPU-METHOD."""
        write_model_element(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        # The occupant lies in the enum's own package, where a generated form
        # collides: an application type.
        write_model_element(tmp_path, {"name": "GearPosition_CompuMethod", "kind": "application_type"})
        with pytest.raises(ValueError, match="collide"):
            build_workspace(str(tmp_path))

    def test_duplicate_data_type_names_raises(self, tmp_path: Path) -> None:
        """Two YAML files with the same 'name' field raise ValueError before building registry."""
        write_model_element(tmp_path, {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
        # Second file has a different filename but the same internal name — simulates
        # a hand-edited duplicate that the filesystem wouldn't otherwise catch.
        _write_yaml(
            model_element_path(tmp_path, "float32_dup", kind="base_type"),
            {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
        )
        with pytest.raises(ValueError, match="Duplicate data type"):
            build_workspace(str(tmp_path))

    @pytest.mark.parametrize("swc_type", ["service", "ecu_abstraction"])
    def test_build_workspace_non_application_type_raises(self, tmp_path: Path, swc_type: str) -> None:
        """Non-application SWC types raise ValueError: only application components are generated."""
        _write_yaml(
            tmp_path / "swc" / "body" / "ServiceSWC.yaml",
            {
                "name": "ServiceSWC",
                "type": swc_type,
                "ports": [],
                "runnables": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        with pytest.raises(ValueError, match="only 'application' SWCs are generated"):
            build_workspace(str(tmp_path))


# ---------------------------------------------------------------------------
# SHORT-NAME uniqueness
# ---------------------------------------------------------------------------


class TestShortNameUniqueness:
    """Two model elements may never be emitted under one ``SHORT-NAME``.

    Each test below is one shape that really produced a duplicate ``SHORT-NAME``
    in a generated file.
    """

    def test_duplicate_swc_names_raise(self, tmp_path: Path) -> None:
        """Two SWC files declaring one name would emit the component twice; the writer refuses."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(tmp_path / "interfaces" / "ISpeed.yaml", _minimal_interface())
        _write_yaml(
            tmp_path / "swc" / "a" / "Foo.yaml",
            _minimal_swc("Foo", [{"name": "pOut", "direction": "provide", "interface": "ISpeed"}]),
        )
        _write_yaml(
            tmp_path / "swc" / "b" / "Foo_other_file.yaml",
            _minimal_swc("Foo", [{"name": "rIn", "direction": "require", "interface": "ISpeed"}]),
        )

        with pytest.raises(ValueError, match="Duplicate SWC names"):
            build_workspace(str(tmp_path))

    def test_duplicate_interface_names_raise_before_a_dangling_reference_is_written(self, tmp_path: Path) -> None:
        """The worst shape: a com-spec pointing into an element that is not the resolvable one.

        The interface map keeps the last file while every file is emitted, so the
        port's ``DATA-ELEMENT-REF`` named ``/PortInterfaces/ISame/b`` from the
        *second* definition while path resolution reached the *first*, which holds
        only ``a``.
        """
        _write_primitive(tmp_path, "float32")
        _write_yaml(
            tmp_path / "interfaces" / "ISame_first.yaml",
            {
                "name": "ISame",
                "kind": "sender_receiver",
                "data_elements": [{"name": "a", "implementation_type": "float32"}],
            },
        )
        _write_yaml(
            tmp_path / "interfaces" / "ISame_second.yaml",
            {
                "name": "ISame",
                "kind": "sender_receiver",
                "data_elements": [{"name": "b", "implementation_type": "float32"}],
            },
        )
        _write_yaml(
            tmp_path / "swc" / "a" / "S.yaml",
            _minimal_swc("S", [{"name": "pOut", "direction": "provide", "interface": "ISame"}]),
        )

        with pytest.raises(ValueError, match="Duplicate interface names"):
            build_workspace(str(tmp_path))

    def test_an_swc_named_after_another_swcs_implementation_raises(self, tmp_path: Path) -> None:
        """``A_Implementation`` beside ``A`` collides with A's auto-generated implementation sibling."""
        _write_yaml(tmp_path / "swc" / "a" / "A.yaml", _minimal_swc("A"))
        _write_yaml(tmp_path / "swc" / "a" / "AI.yaml", _minimal_swc("A_Implementation"))

        with pytest.raises(ValueError, match="collide with auto-generated"):
            build_workspace(str(tmp_path))

    def test_a_component_named_like_the_retired_composition_is_an_ordinary_name(self, tmp_path: Path) -> None:
        """``SwCraftComposition`` is an ordinary component name.

        The writer invents no composition, so reserving the name would be a guard
        stricter than the writer. A component sharing a ``SHORT-NAME`` with a
        DECLARED composition is still refused:
        ``test_a_component_and_a_composition_of_one_name_collide`` below.
        """
        _write_yaml(tmp_path / "swc" / "a" / "P.yaml", _minimal_swc("SwCraftComposition"))
        _root_composition(tmp_path, ["SwCraftComposition"])

        assert find_element(build_workspace(str(tmp_path)).root, "/ComponentTypes/SwCraftComposition") is not None

    def test_a_composition_named_like_the_retired_one_is_an_ordinary_name_too(self, tmp_path: Path) -> None:
        """A declared composition named ``SwCraftComposition`` is emitted under it like any other.

        The composition namespace's half: a guard still reserving the name there
        would pass the component-side case above.
        """
        _write_yaml(tmp_path / "swc" / "a" / "P.yaml", _minimal_swc("P"))
        _root_composition(tmp_path, ["P"], name="SwCraftComposition")

        root = build_workspace(str(tmp_path)).root

        assert find_element(root, "/ComponentTypes/SwCraftComposition") is not None
        assert find_element(root, "/ComponentTypes/SwCraftComposition/P") is not None

    def test_a_component_and_a_composition_of_one_name_collide(self, tmp_path: Path) -> None:
        """Both are elements of ``/ComponentTypes``, so one name is one element.

        ``_check_component_type_names`` reports it before emission, in the model's
        own terms: ``ArxmlPackage``'s ``SHORT-NAME`` scope sees only emitted
        elements, and an ``external: true`` component emits none. The
        ``check-model`` twin is ``rule:name.duplicate-component-type``.
        """
        _write_yaml(tmp_path / "swc" / "a" / "Root.yaml", _minimal_swc("Root"))
        _root_composition(tmp_path, ["Root"])

        with pytest.raises(ValueError, match="Colliding /ComponentTypes SHORT-NAME"):
            build_workspace(str(tmp_path))

    def test_an_swc_named_after_another_swcs_internal_behavior_is_allowed(self, tmp_path: Path) -> None:
        """``B_InternalBehavior`` is NOT reserved — that element is nested in its own component.

        ``/ComponentTypes/B/B_InternalBehavior`` and a component
        ``/ComponentTypes/B_InternalBehavior`` are distinct AUTOSAR paths and
        both resolve, so rejecting this would refuse a legal model.
        """
        _write_yaml(tmp_path / "swc" / "a" / "B.yaml", _minimal_swc("B"))
        _write_yaml(tmp_path / "swc" / "a" / "BI.yaml", _minimal_swc("B_InternalBehavior"))

        root = _generate(tmp_path)
        assert find_element(root, "/ComponentTypes/B/B_InternalBehavior") is not None
        assert find_element(root, "/ComponentTypes/B_InternalBehavior") is not None

    def test_no_duplicate_short_names_survive_into_the_reference_model(self) -> None:
        """The end-to-end claim, on real output: sibling names are unique per package.

        A generated document is the level the invariant is *about*; the guards
        above are the reachable model shapes that used to break it.
        """
        document = build_workspace(str(Path(__file__).resolve().parents[3] / "examples" / "basic" / "model"))
        for name in ArxmlDocument.PACKAGE_NAMES:
            package = document.package(name)
            if not package.has_elements:
                continue
            short_names = [short_name_of(element) for element in package.elements]
            assert len(short_names) == len(set(short_names)), f"duplicate SHORT-NAME in {name}"


class TestNestedShortNameUniqueness:
    """Nested siblings are guarded, not only ``AR-PACKAGE`` children.

    Each shape below once generated a file with two identical sibling
    ``SHORT-NAME``s — reachable, since ``swcraft generate arxml`` reads the model
    with no ``check-model`` gate in front of it. A duplicate breaks a reference
    that names it (``PORT-REF``, ``TARGET-P-PORT-REF``, ``START-ON-EVENT-REF``,
    ``DATA-ELEMENT-REF``, ``OPERATION-REF``): ``find_element()`` returns the first
    match and ``arxml_reader.py``'s path index keeps one of the pair with no
    skip-log entry.
    """

    def test_two_runnables_of_one_name_raise(self, tmp_path: Path) -> None:
        """Two ``<SHORT-NAME>R</SHORT-NAME>`` in one ``RUNNABLES``, both named by ``START-ON-EVENT-REF``."""
        swc = _minimal_swc("A")
        swc["runnables"] = [{"name": "R", "events": []}, {"name": "R", "events": []}]
        _write_yaml(tmp_path / "swc" / "a" / "A.yaml", swc)

        with pytest.raises(ValueError, match="duplicate runnable name"):
            build_workspace(str(tmp_path))

    def test_a_provide_and_a_require_port_of_one_name_raise(self, tmp_path: Path) -> None:
        """Direction is not part of a ``SHORT-NAME``: both ports are siblings in ``PORTS``."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(tmp_path / "interfaces" / "ISpeed.yaml", _minimal_interface())
        _write_yaml(
            tmp_path / "swc" / "a" / "A.yaml",
            _minimal_swc(
                "A",
                [
                    {"name": "p", "direction": "provide", "interface": "ISpeed"},
                    {"name": "p", "direction": "require", "interface": "ISpeed"},
                ],
            ),
        )

        with pytest.raises(ValueError, match="duplicate port name"):
            build_workspace(str(tmp_path))

    def test_two_data_elements_of_one_name_raise(self, tmp_path: Path) -> None:
        """A com-spec's ``DATA-ELEMENT-REF`` cannot tell the two apart."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(
            tmp_path / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "d", "implementation_type": "float32"},
                    {"name": "d", "implementation_type": "float32"},
                ],
            },
        )

        with pytest.raises(ValueError, match="duplicate data element name"):
            build_workspace(str(tmp_path))

    def test_two_operations_of_one_name_raise(self, tmp_path: Path) -> None:
        """The client-server twin: ``OPERATION-REF`` becomes ambiguous."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [{"name": "op", "arguments": []}, {"name": "op", "arguments": []}],
            },
        )

        with pytest.raises(ValueError, match="duplicate operation name"):
            build_workspace(str(tmp_path))

    def test_two_arguments_of_one_name_raise(self, tmp_path: Path) -> None:
        """One level deeper again: ``ARGUMENT-DATA-PROTOTYPE`` siblings in one ``ARGUMENTS``."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "op",
                        "arguments": [
                            {"name": "a", "implementation_type": "float32", "direction": "in"},
                            {"name": "a", "implementation_type": "float32", "direction": "in"},
                        ],
                    }
                ],
            },
        )

        with pytest.raises(ValueError, match="duplicate argument name"):
            build_workspace(str(tmp_path))

    def test_the_same_name_in_two_different_components_is_allowed(self, tmp_path: Path) -> None:
        """The rule is sibling-scoped, not global — rejecting this would refuse a legal model."""
        _write_primitive(tmp_path, "float32")
        _write_yaml(tmp_path / "interfaces" / "ISpeed.yaml", _minimal_interface())
        for swc_name in ("A", "B"):
            swc = _minimal_swc(swc_name, [{"name": "p", "direction": "provide", "interface": "ISpeed"}])
            swc["runnables"] = [{"name": "R", "events": []}]
            _write_yaml(tmp_path / "swc" / "a" / f"{swc_name}.yaml", swc)

        root = _generate(tmp_path)
        assert find_element(root, "/ComponentTypes/A/p") is not None
        assert find_element(root, "/ComponentTypes/B/p") is not None

    def test_the_emitter_backstop_raises_even_without_the_model_level_guard(self, tmp_path: Path) -> None:
        """``ShortNameScope`` is a structural backstop, not a duplicate of the model check.

        The model-level guards are what produce the readable message; this
        asserts the emitter itself would still refuse, which is what covers an
        emitter added later against a model shape nobody thought to check.
        """
        scope = ShortNameScope(etree.Element("PORTS"), "PORTS of SWC 'A'")
        scope.create("P-PORT-PROTOTYPE", "p")

        with pytest.raises(ValueError, match="Duplicate SHORT-NAME 'p' in PORTS of SWC 'A'"):
            scope.create("R-PORT-PROTOTYPE", "p")


# ---------------------------------------------------------------------------
# CS interface ARXML generation
# ---------------------------------------------------------------------------


class TestGeneratedCSInterface:
    """Client-server interface generation, asserted on the generated document."""

    def _write_cs_interface(self, tmp_path: Path, name: str = "IDiagnosticService") -> None:
        """Write a minimal CS interface YAML file."""
        _write_yaml(
            tmp_path / "interfaces" / f"{name}.yaml",
            {
                "name": name,
                "kind": "client_server",
                "operations": [
                    {
                        "name": "ReadDtc",
                        "arguments": [
                            {"name": "request", "direction": "in", "implementation_type": "uint8"},
                            {"name": "response", "direction": "out", "implementation_type": "uint8"},
                        ],
                    }
                ],
            },
        )

    def test_cs_interface_is_emitted(self, tmp_path: Path) -> None:
        """A CS interface lands in the PortInterfaces package as a CLIENT-SERVER-INTERFACE."""
        _write_primitive(tmp_path, "uint8")
        self._write_cs_interface(tmp_path)

        iface = find_element(_generate(tmp_path), "/PortInterfaces/IDiagnosticService")
        assert iface is not None
        assert etree.QName(iface).localname == "CLIENT-SERVER-INTERFACE"

    def test_cs_interface_operations_present(self, tmp_path: Path) -> None:
        """The interface carries its declared operation."""
        _write_primitive(tmp_path, "uint8")
        self._write_cs_interface(tmp_path)

        assert find_element(_generate(tmp_path), "/PortInterfaces/IDiagnosticService/ReadDtc") is not None

    def test_cs_interface_arguments_present(self, tmp_path: Path) -> None:
        """CS operation arguments are emitted with their names and uppercase directions."""
        _write_primitive(tmp_path, "uint8")
        self._write_cs_interface(tmp_path)

        root = _generate(tmp_path)
        request = find_element(root, "/PortInterfaces/IDiagnosticService/ReadDtc/request")
        response = find_element(root, "/PortInterfaces/IDiagnosticService/ReadDtc/response")
        assert request is not None
        assert response is not None
        assert _child_text(request, "DIRECTION") == "IN"
        assert _child_text(response, "DIRECTION") == "OUT"

    def test_cs_interface_with_value_type_argument(self, tmp_path: Path) -> None:
        """CS argument referencing a value type uses /DataTypes/{name} (no _impl suffix)."""
        _write_primitive(tmp_path, "uint8")
        write_model_element(tmp_path, {"name": "DtcCode", "kind": "value_type", "base": "uint8_base"})
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "GetDtc",
                        "arguments": [
                            {"name": "code", "direction": "out", "implementation_type": "DtcCode"},
                        ],
                    }
                ],
            },
        )

        assert (
            _type_ref(_generate(tmp_path), "/PortInterfaces/IDiag/GetDtc/code") == "/DataTypes/Implementation/DtcCode"
        )

    def test_cs_interface_with_record_type_argument(self, tmp_path: Path) -> None:
        """CS argument referencing a record type uses /DataTypes/{record name}."""
        _write_primitive(tmp_path, "uint8")
        write_model_element(
            tmp_path,
            {
                "name": "StatusRecord",
                "kind": "record",
                "fields": [{"name": "code", "type": "Code"}],
            },
        )
        write_model_element(tmp_path, {"name": "Code", "kind": "application_type"})
        write_model_element(
            tmp_path,
            {"name": "Codes", "maps": [{"application": "Code", "implementation": "uint8"}]},
            layer="mapping_sets",
        )
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "GetStatus",
                        "arguments": [
                            {"name": "status", "direction": "out", "application_type": "StatusRecord"},
                        ],
                    }
                ],
            },
        )

        assert (
            _type_ref(_generate(tmp_path), "/PortInterfaces/IDiag/GetStatus/status")
            == "/DataTypes/Application/StatusRecord"
        )

    def test_cs_interface_with_enum_type_argument(self, tmp_path: Path) -> None:
        """CS argument referencing an enum type uses /DataTypes/{enum name}."""
        write_model_element(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "GetGear",
                        "arguments": [
                            {"name": "gear", "direction": "out", "application_type": "GearPosition"},
                        ],
                    }
                ],
            },
        )

        assert (
            _type_ref(_generate(tmp_path), "/PortInterfaces/IDiag/GetGear/gear")
            == "/DataTypes/Application/GearPosition"
        )

    def test_cs_interface_with_array_type_argument(self, tmp_path: Path) -> None:
        """CS argument referencing an array type uses /DataTypes/{array name}."""
        _write_primitive(tmp_path, "uint8")
        write_model_element(
            tmp_path,
            {
                "name": "CodeArray",
                "kind": "array",
                "element_type": "Code",
                "length": 4,
            },
        )
        write_model_element(tmp_path, {"name": "Code", "kind": "application_type"})
        write_model_element(
            tmp_path,
            {"name": "Codes", "maps": [{"application": "Code", "implementation": "uint8"}]},
            layer="mapping_sets",
        )
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "GetCodes",
                        "arguments": [
                            {"name": "codes", "direction": "out", "application_type": "CodeArray"},
                        ],
                    }
                ],
            },
        )

        assert (
            _type_ref(_generate(tmp_path), "/PortInterfaces/IDiag/GetCodes/codes") == "/DataTypes/Application/CodeArray"
        )

    def test_cs_interface_undeclared_arg_type_raises(self, tmp_path: Path) -> None:
        """CS argument referencing undeclared type raises ValueError."""
        _write_yaml(
            tmp_path / "interfaces" / "IBad.yaml",
            {
                "name": "IBad",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "Op",
                        "arguments": [
                            {"name": "x", "direction": "in", "implementation_type": "UndeclaredType"},
                        ],
                    }
                ],
            },
        )

        with pytest.raises(ValueError, match="not declared"):
            build_workspace(str(tmp_path))

    def test_sr_interfaces_not_affected_by_cs_pass(self, tmp_path: Path) -> None:
        """Adding a CS interface alongside SR interfaces does not break SR ARXML generation."""
        _write_primitive(tmp_path, "float32")
        _write_primitive(tmp_path, "uint8")
        _write_yaml(
            tmp_path / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "float32"}],
            },
        )
        self._write_cs_interface(tmp_path)

        root = _generate(tmp_path)
        assert find_element(root, "/PortInterfaces/ISpeed") is not None
        assert find_element(root, "/PortInterfaces/IDiagnosticService") is not None

    def test_cs_interface_no_arguments_generates_successfully(self, tmp_path: Path) -> None:
        """CS operation with no arguments (empty list) generates successfully."""
        _write_yaml(
            tmp_path / "interfaces" / "ISimple.yaml",
            {
                "name": "ISimple",
                "kind": "client_server",
                "operations": [
                    {"name": "Ping", "arguments": []},
                ],
            },
        )

        assert find_element(_generate(tmp_path), "/PortInterfaces/ISimple/Ping") is not None


# ---------------------------------------------------------------------------
# generate_arxml
# ---------------------------------------------------------------------------


class TestGenerateArxml:
    def test_creates_file(self, model_with_swc_and_iface: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "model.arxml"
        generate_arxml(str(model_with_swc_and_iface), str(output))
        assert output.exists()

    def test_file_starts_with_xml_declaration(self, model_with_swc_and_iface: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "model.arxml"
        generate_arxml(str(model_with_swc_and_iface), str(output))
        assert output.read_text(encoding="utf-8").startswith("<?xml")

    def test_file_contains_swc_name(self, model_with_swc_and_iface: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "model.arxml"
        generate_arxml(str(model_with_swc_and_iface), str(output))
        assert "SpeedCtrlSWC" in output.read_text(encoding="utf-8")

    def test_creates_parent_directories(self, model_with_swc_and_iface: Path, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "deeply" / "output.arxml"
        generate_arxml(str(model_with_swc_and_iface), str(output))
        assert output.exists()

    def test_connector_model_generates_assembly_connector(self, tmp_path: Path) -> None:
        _write_connector_model(tmp_path)
        output = tmp_path / "dist" / "model.arxml"

        generate_arxml(str(tmp_path), str(output))

        content = output.read_text(encoding="utf-8")
        assert "COMPOSITION-SW-COMPONENT-TYPE" in content
        assert "ASSEMBLY-SW-CONNECTOR" in content
        assert "SpeedSensor_to_SpeedCtrl" in content

    def test_composite_model_end_to_end_emits_the_whole_chain(self, model_with_composite_swc_and_iface: Path) -> None:
        """A record -> array -> enum chain behind an SR interface: every link sits at its path and resolves."""
        root = _generate(model_with_composite_swc_and_iface)

        assert find_element(root, "/ComponentTypes/StatusSWC") is not None
        assert _type_ref(root, "/PortInterfaces/IStatus/status") == "/DataTypes/Application/StatusRecord"
        assert _record_fields(root, "/DataTypes/Application/StatusRecord") == [
            ("recentGears", "/DataTypes/Application/GearHistory")
        ]

        array_element = find_element(root, "/DataTypes/Application/GearHistory/Element")
        assert array_element is not None
        assert _child_text(array_element, "TYPE-TREF") == "/DataTypes/Application/GearPosition"
        assert _child_text(array_element, "MAX-NUMBER-OF-ELEMENTS") == "3"

        enum = find_element(root, "/DataTypes/Application/GearPosition")
        assert enum is not None
        # The enum's type is VALUE; TEXTTABLE is its compu method's category.
        assert _child_text(enum, "CATEGORY") == "VALUE"
        compu = find_element(root, "/DataTypes/Application/GearPosition_CompuMethod")
        assert compu is not None
        assert _child_text(compu, "CATEGORY") == "TEXTTABLE"

    def test_composite_dependency_cycle_raises_value_error(self, tmp_path: Path) -> None:
        """A record/array dependency cycle surfaces as a ValueError from build_workspace().

        Defense in depth for direct/unvalidated callers."""
        write_model_element(
            tmp_path,
            {
                "name": "CycleRecord",
                "kind": "record",
                "fields": [{"name": "arr", "type": "CycleArray"}],
            },
        )
        write_model_element(
            tmp_path,
            {
                "name": "CycleArray",
                "kind": "array",
                "element_type": "CycleRecord",
                "length": 2,
            },
        )

        with pytest.raises(ValueError, match="cycle"):
            build_workspace(str(tmp_path))


# ---------------------------------------------------------------------------
# generate_needs
# ---------------------------------------------------------------------------


def _needs(output: Path) -> dict:
    """The needs of the one version a written ``needs.json`` holds, by id."""
    document = json.loads(output.read_text(encoding="utf-8"))
    return document["versions"][document["current_version"]]["needs"]


class TestGenerateNeeds:
    """``generate_needs`` writes one ``needs.json``; ``test_needs_export.py`` holds its content."""

    @pytest.fixture
    def req_model(self, tmp_path: Path) -> Path:
        _write_yaml(
            tmp_path / "requirements" / "REQ_001.yaml",
            {
                "id": "REQ_001",
                "title": "Speed sensor provides vehicle speed",
                "description": "The system shall provide the current vehicle speed.",
                "req_type": "req",
                "version": 1,
                "source": "architecture",
            },
        )
        # The link lives in the component.
        _write_yaml(
            tmp_path / "swc" / "group" / "SpeedCtrlSWC.yaml",
            {"name": "SpeedCtrlSWC", "type": "application", "satisfies": ["REQ_001@1"]},
        )
        return tmp_path

    def test_creates_file(self, req_model: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "needs.json"
        assert generate_needs(str(req_model), str(output), "p") == str(output)
        assert sorted(path.name for path in output.parent.iterdir()) == ["needs.json"]

    def test_file_contains_the_requirement_need(self, req_model: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(req_model), str(output), "p")
        need = _needs(output)["REQ_001"]
        assert (need["type"], need["title"]) == ("arch_req", "Speed sensor provides vehicle speed")

    def test_file_contains_id_field(self, req_model: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(req_model), str(output), "p")
        assert _needs(output)["REQ_001"]["id"] == "REQ_001"

    def test_file_contains_the_satisfies_link(self, req_model: Path, tmp_path: Path) -> None:
        """The link stands on the satisfying element's own need, by the requirement's id."""
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(req_model), str(output), "p")
        assert _needs(output)["SWC-SpeedCtrlSWC"]["satisfies"] == ["REQ_001"]
        assert "satisfies" not in _needs(output)["REQ_001"]

    def test_two_architecture_requirements_both_present(self, tmp_path: Path) -> None:
        for req_id, title, req_type in (("REQ_001", "Speed signal", "req"), ("REQ_002", "Throttle signal", "spec")):
            _write_yaml(
                tmp_path / "requirements" / f"{req_id}.yaml",
                {
                    "id": req_id,
                    "title": title,
                    "description": "A requirement.",
                    "req_type": req_type,
                    "version": 1,
                    "source": "architecture",
                },
            )
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(tmp_path), str(output), "p")
        needs = _needs(output)

        assert (needs["REQ_001"]["type"], needs["REQ_001"]["title"]) == ("arch_req", "Speed signal")
        assert (needs["REQ_002"]["type"], needs["REQ_002"]["title"]) == ("arch_spec", "Throttle signal")

    def test_creates_parent_directories(self, req_model: Path, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "deeply" / "needs.json"
        generate_needs(str(req_model), str(output), "p")
        assert output.is_file()

    def test_an_empty_model_writes_a_file_with_no_need(self, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(tmp_path), str(output), "p")
        assert output.read_text(encoding="utf-8") == (
            '{\n  "current_version": "",\n  "project": "p",\n  "versions": {\n    "": {\n'
            '      "needs": {},\n      "needs_amount": 0\n    }\n  }\n}\n'
        )

    def test_a_previous_file_is_rewritten_whole(self, req_model: Path, tmp_path: Path) -> None:
        output = tmp_path / "dist" / "needs.json"
        output.parent.mkdir(parents=True)
        output.write_text("old\n", encoding="utf-8", newline="\n")

        generate_needs(str(req_model), str(output), "p")

        assert "REQ_001" in _needs(output)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


class TestCLIEntryPoints:
    def test_generate_arxml_cli_creates_file(
        self, model_with_swc_and_iface: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "dist" / "model.arxml"
        monkeypatch.setattr("sys.argv", ["swcraft-generate-arxml", str(model_with_swc_and_iface), str(output)])
        generate_arxml_cli()
        assert output.exists()

    def test_generate_arxml_cli_is_4_2_2_only(
        self, model_with_swc_and_iface: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shim, the fifth generation entry point, deliberately does **not** carry the version.

        It takes a bare ``<model_root> <output_path>``, has no production consumer
        and gains no ``--arxml-version`` flag; it emits :data:`DEFAULT_ARXML_VERSION`
        and nothing else.
        """
        output = tmp_path / "dist" / "model.arxml"
        monkeypatch.setattr("sys.argv", ["swcraft-generate-arxml", str(model_with_swc_and_iface), str(output)])
        generate_arxml_cli()

        assert DEFAULT_ARXML_VERSION is ArxmlVersion.V4_2_2
        assert f'xsi:schemaLocation="{ArxmlVersion.V4_2_2.schema_location}"' in output.read_text(encoding="utf-8")
        assert ArxmlVersion.V4_3_0.schema_filename not in output.read_text(encoding="utf-8")

    def test_generate_arxml_cli_keeps_a_differing_file_as_old_and_says_nothing(
        self,
        model_with_swc_and_iface: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The shim keeps one previous ARXML as the other callers do, and prints nothing about it."""
        output = tmp_path / "dist" / "model.arxml"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"<previous build/>\n")
        monkeypatch.setattr("sys.argv", ["swcraft-generate-arxml", str(model_with_swc_and_iface), str(output)])

        generate_arxml_cli()

        assert (tmp_path / "dist" / "model_old.arxml").read_bytes() == b"<previous build/>\n"
        assert output.read_bytes() != b"<previous build/>\n"
        assert capsys.readouterr().out == ""

    def test_generate_arxml_cli_no_args_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft-generate-arxml"])
        with pytest.raises(SystemExit):
            generate_arxml_cli()
