"""Tests for reader.py."""

from __future__ import annotations

import json
import re
import shutil

import pytest
import yaml
from conftest import REPO_ROOT, model_element_path, read_golden, write_model_element
from node_support import requires_node
from swcraft_tools.component_exchange.export import export_component
from swcraft_tools.model_registry import production_registry
from swcraft_tools.reader import (
    find_connector_file,
    find_data_type_file,
    find_element_file,
    find_requirement_file,
    find_swc_file,
    read_compositions,
    read_connectors,
    read_data_types,
    read_hardware,
    read_interfaces,
    read_layer,
    read_requirements,
    read_swc_model,
    read_swc_model_with_placement,
)
from swcraft_tools.schema import (
    ArrayDataType,
    BaseDataType,
    CSInterface,
    EnumDataType,
    RecordDataType,
    SRInterface,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


VALID_SWC = {
    "name": "FooSWC",
    "type": "application",
    "description": "Foo component",
    "ports": [{"name": "pOut", "direction": "provide", "interface": "IFoo", "description": "Output"}],
    "runnables": [],
}

VALID_INTERFACE = {
    "name": "IFoo",
    "kind": "sender_receiver",
    "data_elements": [{"name": "value", "implementation_type": "float32"}],
}

VALID_CS_INTERFACE = {
    "name": "IDiagnosticService",
    "kind": "client_server",
    "operations": [
        {
            "name": "ReadDtc",
            "arguments": [
                {"name": "request", "direction": "in", "implementation_type": "uint32"},
                {"name": "response", "direction": "out", "implementation_type": "uint32"},
            ],
        }
    ],
}

VALID_REQUIREMENT = {
    "id": "REQ_001",
    "title": "Foo requirement",
    "description": "The system shall provide foo functionality.",
    "req_type": "req",
    "version": 2,
    "source": "external",
    "derived_from": ["REQ_000@1"],
}

VALID_COMPOSITION = {
    "name": "SpeedControl",
    "title": "Speed Control",
    "root": True,
    "members": [{"name": "FooSWC", "type": "FooSWC"}],
    "description": "Speed regulation subsystem",
}

VALID_CONNECTOR = {
    "name": "Foo_to_Bar",
    "from": {"swc": "FooSWC", "port": "pOut"},
    "to": {"swc": "BarSWC", "port": "pIn"},
    "description": "Foo data consumed by Bar",
}


def _write_needs_json(model_root: str, output_file: str) -> None:
    """``generate needs`` for *model_root*, under a fixed project name: the name is no model input."""
    generate_needs(model_root, output_file, "basic")


# ---------------------------------------------------------------------------
# read_swc_model
# ---------------------------------------------------------------------------


class TestReadSwcModel:
    def test_one_valid_yaml_returns_list_of_one(self, tmp_path):
        _write_yaml(tmp_path / "swc" / "body" / "FooSWC.yaml", VALID_SWC)
        swcs = read_swc_model(str(tmp_path))
        assert len(swcs) == 1
        assert swcs[0].name == "FooSWC"

    def test_two_yamls_in_different_subfolders_returns_list_of_two(self, tmp_path):
        _write_yaml(tmp_path / "swc" / "body" / "FooSWC.yaml", VALID_SWC)
        bar_data = {**VALID_SWC, "name": "BarSWC"}
        _write_yaml(tmp_path / "swc" / "powertrain" / "BarSWC.yaml", bar_data)
        swcs = read_swc_model(str(tmp_path))
        assert len(swcs) == 2
        names = {s.name for s in swcs}
        assert names == {"FooSWC", "BarSWC"}

    def test_invalid_yaml_raises_value_error_naming_the_file(self, tmp_path):
        invalid = {"name": "", "type": "invalid_type"}
        _write_yaml(tmp_path / "swc" / "body" / "Bad.yaml", invalid)
        with pytest.raises(ValueError):
            read_swc_model(str(tmp_path))

    def test_nonexistent_model_root_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_swc_model(str(tmp_path / "nonexistent"))

    def test_empty_swc_dir_returns_empty_list(self, tmp_path):
        (tmp_path / "swc").mkdir()
        swcs = read_swc_model(str(tmp_path))
        assert swcs == []


# ---------------------------------------------------------------------------
# find_swc_file
# ---------------------------------------------------------------------------


class TestFindSwcFile:
    def test_finds_existing_swc_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "swc" / "example" / "FooSWC.yaml", VALID_SWC)
        result = find_swc_file(str(tmp_path), "FooSWC")
        assert result is not None
        assert "FooSWC.yaml" in result

    def test_nonexistent_swc_returns_none(self, tmp_path):
        (tmp_path / "swc").mkdir()
        result = find_swc_file(str(tmp_path), "NonExistent")
        assert result is None


# ---------------------------------------------------------------------------
# read_interfaces
# ---------------------------------------------------------------------------


class TestReadInterfaces:
    def test_one_sr_interface_yaml_returns_list_of_one(self, tmp_path):
        _write_yaml(tmp_path / "interfaces" / "IFoo.yaml", VALID_INTERFACE)
        interfaces = read_interfaces(str(tmp_path))
        assert len(interfaces) == 1
        assert interfaces[0].name == "IFoo"
        assert interfaces[0].kind == "sender_receiver"
        assert len(interfaces[0].data_elements) == 1

    def test_missing_interfaces_dir_returns_empty_list(self, tmp_path):
        interfaces = read_interfaces(str(tmp_path))
        assert interfaces == []

    def test_invalid_interface_yaml_raises_value_error_naming_the_file(self, tmp_path):
        # data_elements must not be empty per SRInterface validator
        invalid = {"name": "IBad", "kind": "sender_receiver", "data_elements": []}
        _write_yaml(tmp_path / "interfaces" / "IBad.yaml", invalid)
        with pytest.raises(ValueError):
            read_interfaces(str(tmp_path))

    def test_sr_only_directory_returns_all_as_sr_interface(self, tmp_path):
        _write_yaml(tmp_path / "interfaces" / "IFoo.yaml", VALID_INTERFACE)
        bar = {**VALID_INTERFACE, "name": "IBar"}
        _write_yaml(tmp_path / "interfaces" / "IBar.yaml", bar)
        interfaces = read_interfaces(str(tmp_path))
        assert len(interfaces) == 2
        assert all(isinstance(iface, SRInterface) for iface in interfaces)
        assert all(iface.kind == "sender_receiver" for iface in interfaces)

    def test_mixed_sr_and_cs_directory_returns_both_types(self, tmp_path):
        _write_yaml(tmp_path / "interfaces" / "IFoo.yaml", VALID_INTERFACE)
        _write_yaml(tmp_path / "interfaces" / "IDiagnosticService.yaml", VALID_CS_INTERFACE)
        interfaces = read_interfaces(str(tmp_path))
        assert len(interfaces) == 2
        kinds = {iface.kind for iface in interfaces}
        assert kinds == {"sender_receiver", "client_server"}
        sr_ifaces = [i for i in interfaces if isinstance(i, SRInterface)]
        cs_ifaces = [i for i in interfaces if isinstance(i, CSInterface)]
        assert len(sr_ifaces) == 1
        assert len(cs_ifaces) == 1
        assert cs_ifaces[0].name == "IDiagnosticService"
        assert cs_ifaces[0].operations[0].name == "ReadDtc"

    def test_unknown_kind_raises_value_error_naming_the_file(self, tmp_path):
        unknown = {
            "name": "IUnknown",
            "kind": "totally_unsupported",
            "data_elements": [{"name": "x", "implementation_type": "float32"}],
        }
        _write_yaml(tmp_path / "interfaces" / "IUnknown.yaml", unknown)
        with pytest.raises(ValueError):
            read_interfaces(str(tmp_path))


# ---------------------------------------------------------------------------
# read_requirements
# ---------------------------------------------------------------------------


class TestReadRequirements:
    def test_one_requirement_yaml_returns_list_of_one(self, tmp_path):
        _write_yaml(tmp_path / "requirements" / "REQ_001.yaml", VALID_REQUIREMENT)
        reqs = read_requirements(str(tmp_path))
        assert len(reqs) == 1
        assert reqs[0].id == "REQ_001"
        assert (reqs[0].version, reqs[0].source, reqs[0].derived_from) == (2, "external", ["REQ_000@1"])

    def test_invalid_requirement_yaml_raises_value_error_naming_the_file(self, tmp_path):
        invalid = {"id": "", "title": "Bad", "description": "bad requirement"}
        _write_yaml(tmp_path / "requirements" / "Bad.yaml", invalid)
        with pytest.raises(ValueError):
            read_requirements(str(tmp_path))


# ---------------------------------------------------------------------------
# find_requirement_file
# ---------------------------------------------------------------------------


class TestFindRequirementFile:
    def test_finds_existing_requirement_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "requirements" / "REQ_001.yaml", VALID_REQUIREMENT)
        result = find_requirement_file(str(tmp_path), "REQ_001")
        assert result is not None
        assert "REQ_001.yaml" in result

    def test_nonexistent_requirement_returns_none(self, tmp_path):
        (tmp_path / "requirements").mkdir()
        result = find_requirement_file(str(tmp_path), "REQ_999")
        assert result is None


# ---------------------------------------------------------------------------
# read_compositions
# ---------------------------------------------------------------------------


class TestReadCompositions:
    def test_one_composition_yaml_returns_list_of_one(self, tmp_path):
        _write_yaml(tmp_path / "compositions" / "SpeedControl.yaml", VALID_COMPOSITION)
        compositions = read_compositions(str(tmp_path))
        assert len(compositions) == 1
        assert compositions[0].name == "SpeedControl"
        assert compositions[0].root is True
        assert compositions[0].members[0].type == "FooSWC"

    def test_missing_compositions_dir_returns_empty_list(self, tmp_path):
        compositions = read_compositions(str(tmp_path))
        assert compositions == []

    def test_invalid_composition_yaml_raises_value_error_naming_the_file(self, tmp_path):
        # `Composition.title` is required; omitting it must be reported with the file named
        invalid = {"name": "SpeedControl"}
        _write_yaml(tmp_path / "compositions" / "SpeedControl.yaml", invalid)
        with pytest.raises(ValueError):
            read_compositions(str(tmp_path))


# ---------------------------------------------------------------------------
# read_connectors
# ---------------------------------------------------------------------------


class TestReadConnectors:
    def test_one_connector_yaml_returns_list_of_one(self, tmp_path):
        _write_yaml(tmp_path / "connectors" / "Foo_to_Bar.yaml", VALID_CONNECTOR)
        connectors = read_connectors(str(tmp_path))
        assert len(connectors) == 1
        assert connectors[0].name == "Foo_to_Bar"
        assert connectors[0].from_.swc == "FooSWC"
        assert connectors[0].to.port == "pIn"

    def test_missing_connectors_dir_returns_empty_list(self, tmp_path):
        connectors = read_connectors(str(tmp_path))
        assert connectors == []

    def test_invalid_connector_yaml_raises_value_error_naming_the_file(self, tmp_path):
        invalid = {
            "name": "",
            "from": {"swc": "FooSWC", "port": "pOut"},
            "to": {"swc": "BarSWC", "port": "pIn"},
        }
        _write_yaml(tmp_path / "connectors" / "Bad.yaml", invalid)
        with pytest.raises(ValueError):
            read_connectors(str(tmp_path))


# ---------------------------------------------------------------------------
# find_connector_file
# ---------------------------------------------------------------------------


class TestFindConnectorFile:
    def test_finds_existing_connector_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "connectors" / "Foo_to_Bar.yaml", VALID_CONNECTOR)
        result = find_connector_file(str(tmp_path), "Foo_to_Bar")
        assert result is not None
        assert "Foo_to_Bar.yaml" in result

    def test_nonexistent_connector_returns_none(self, tmp_path):
        (tmp_path / "connectors").mkdir()
        result = find_connector_file(str(tmp_path), "Missing")
        assert result is None


# ---------------------------------------------------------------------------
# Data type fixtures
# ---------------------------------------------------------------------------

VALID_PRIMITIVE = {
    "name": "float32",
    "kind": "base_type",
    "size": 32,
    "encoding": "IEEE754",
    "native_declaration": "float",
}

VALID_ALIAS = {
    "name": "VehicleSpeed",
    "kind": "value_type",
    "base": "float32",
    "unit": "km_h",
    "value_min": 0.0,
    "value_max": 300.0,
    "description": "Vehicle longitudinal speed",
}

VALID_ENUM = {
    "name": "GearPosition",
    "kind": "enum",
    "literals": [
        {"name": "PARK", "value": 0},
        {"name": "DRIVE", "value": 1},
    ],
}

VALID_RECORD = {
    "name": "VehicleState",
    "kind": "record",
    "fields": [
        {"name": "speed", "type": "VehicleSpeed"},
        {"name": "ignition_on", "type": "bool"},
    ],
}

VALID_ARRAY = {
    "name": "WheelSpeedArray",
    "kind": "array",
    "element_type": "float32",
    "length": 4,
}


# ---------------------------------------------------------------------------
# read_data_types
# ---------------------------------------------------------------------------


class TestReadDataTypes:
    def test_missing_data_types_dir_returns_empty_list(self, tmp_path):
        result = read_data_types(str(tmp_path))
        assert result == []

    def test_all_five_kinds_are_loaded_and_validated(self, tmp_path):
        dt_dir = tmp_path / "data_types" / "implementation"
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), VALID_PRIMITIVE)
        _write_yaml(dt_dir / "VehicleSpeed.yaml", VALID_ALIAS)
        # The three application kinds are the application_types layer's.
        _write_yaml(model_element_path(tmp_path, "GearPosition", kind="enum"), VALID_ENUM)
        _write_yaml(model_element_path(tmp_path, "VehicleState", kind="record"), VALID_RECORD)
        _write_yaml(model_element_path(tmp_path, "WheelSpeedArray", kind="array"), VALID_ARRAY)

        # A base type is the base_types layer's element and an application type the
        # application_types layer's; read_data_types reads the implementation_types layer alone.
        result = (
            read_layer(str(tmp_path), "base_types")
            + read_data_types(str(tmp_path))
            + read_layer(str(tmp_path), "application_types")
        )
        assert len(result) == 5
        names = {dt.name for dt in result}
        assert names == {"float32", "VehicleSpeed", "GearPosition", "VehicleState", "WheelSpeedArray"}

    def test_kinds_are_correct_types(self, tmp_path):
        dt_dir = tmp_path / "data_types" / "implementation"
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), VALID_PRIMITIVE)
        _write_yaml(dt_dir / "VehicleSpeed.yaml", VALID_ALIAS)
        _write_yaml(model_element_path(tmp_path, "GearPosition", kind="enum"), VALID_ENUM)
        _write_yaml(model_element_path(tmp_path, "VehicleState", kind="record"), VALID_RECORD)
        _write_yaml(model_element_path(tmp_path, "WheelSpeedArray", kind="array"), VALID_ARRAY)

        result = (
            read_layer(str(tmp_path), "base_types")
            + read_data_types(str(tmp_path))
            + read_layer(str(tmp_path), "application_types")
        )
        by_name = {dt.name: dt for dt in result}
        assert isinstance(by_name["float32"], BaseDataType)
        assert isinstance(by_name["VehicleSpeed"], ValueDataType)
        assert isinstance(by_name["GearPosition"], EnumDataType)
        assert isinstance(by_name["VehicleState"], RecordDataType)
        assert isinstance(by_name["WheelSpeedArray"], ArrayDataType)

    def test_malformed_yaml_raises_value_error_naming_the_file(self, tmp_path):
        # Missing required 'size' field for a base type
        bad = {"name": "bad_type", "kind": "base_type", "encoding": "NONE"}
        _write_yaml(model_element_path(tmp_path, "bad_type", kind="base_type"), bad)
        with pytest.raises(ValueError):
            read_layer(str(tmp_path), "base_types")

    def test_single_base_type_returns_correct_fields(self, tmp_path):
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), VALID_PRIMITIVE)
        result = read_layer(str(tmp_path), "base_types")
        assert len(result) == 1
        dt = result[0]
        assert dt.name == "float32"
        assert dt.kind == "base_type"
        assert dt.size == 32
        assert dt.encoding == "IEEE754"
        assert dt.native_declaration == "float"


# ---------------------------------------------------------------------------
# find_data_type_file
# ---------------------------------------------------------------------------


class TestFindDataTypeFile:
    def test_finds_existing_data_type_returns_path(self, tmp_path):
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), VALID_PRIMITIVE)
        result = find_element_file(str(tmp_path), "base_types", "float32")
        assert result is not None
        assert "float32.yaml" in result

    def test_nonexistent_data_type_returns_none(self, tmp_path):
        (tmp_path / "data_types" / "implementation").mkdir(parents=True)
        result = find_data_type_file(str(tmp_path), "nonexistent")
        assert result is None

    def test_missing_data_types_dir_returns_none(self, tmp_path):
        result = find_data_type_file(str(tmp_path), "float32")
        assert result is None

    def test_content_scan_finds_by_name_field_not_filename(self, tmp_path):
        # The content scan matches the name field, not the filename.
        data = {**VALID_PRIMITIVE, "name": "float32"}
        _write_yaml(model_element_path(tmp_path, "my_float", kind="base_type"), data)
        result = find_element_file(str(tmp_path), "base_types", "float32")
        assert result is not None
        assert "my_float.yaml" in result


class TestTheReadersOrderIsIdentityNotFileName:
    """``read_layer``, a wrapper and the hardware reader answer identity order, not file-name order.

    ``fixtures/hardware/`` is the authored tree where the two differ (``Core0.yaml`` declares ``CORE0``).
    """

    def test_read_layer_and_its_wrapper_answer_identity_order(self, tmp_path):
        _write_yaml(model_element_path(tmp_path, "b", kind="base_type"), {**VALID_PRIMITIVE, "name": "A"})
        _write_yaml(model_element_path(tmp_path, "a", kind="base_type"), {**VALID_PRIMITIVE, "name": "B"})

        _write_yaml(tmp_path / "data_types" / "implementation" / "b.yaml", {**VALID_ALIAS, "name": "C"})
        _write_yaml(tmp_path / "data_types" / "implementation" / "a.yaml", {**VALID_ALIAS, "name": "D"})

        assert [element.name for element in read_layer(str(tmp_path), "base_types")] == ["A", "B"]
        assert [element.name for element in read_layer(str(tmp_path), "implementation_types")] == ["C", "D"]
        assert [element.name for element in read_data_types(str(tmp_path))] == ["C", "D"]

    def test_the_content_scan_still_walks_files_in_path_order(self, tmp_path):
        """``find_element_file`` is a file-level consumer and keeps ``scan_layer``'s order: first file wins."""
        _write_yaml(model_element_path(tmp_path, "b", kind="base_type"), {**VALID_PRIMITIVE, "name": "Twice"})
        _write_yaml(model_element_path(tmp_path, "a", kind="base_type"), {**VALID_PRIMITIVE, "name": "Twice"})

        assert find_element_file(str(tmp_path), "base_types", "Twice").endswith("a.yaml")
        _write_yaml(tmp_path / "data_types" / "implementation" / "b.yaml", {**VALID_ALIAS, "name": "Again"})
        _write_yaml(tmp_path / "data_types" / "implementation" / "a.yaml", {**VALID_ALIAS, "name": "Again"})

        assert find_data_type_file(str(tmp_path), "Again").endswith("a.yaml")

    def test_read_hardware_answers_the_fixture_in_identity_order(self):
        model_root = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "hardware" / "model"
        assert [element.name for element in read_hardware(str(model_root))] == [
            "CORE0",
            "CORE1",
            "CORE2",
            "CORE3",
            "CPU",
            # One partition per core.
            "PARTITION0",
            "PARTITION1",
            "PARTITION2",
            "PARTITION3",
            "RAM",
            "ROM",
        ]


# ---------------------------------------------------------------------------
# Every layer names the offending file
# ---------------------------------------------------------------------------


class TestSchemaErrorsNameTheOffendingFile:
    """A schema error says which file to fix, in every layer.

    A bare ``pydantic.ValidationError`` names a field and no path, so a model that stops
    loading after a schema tightening could not tell which file was at fault.
    """

    def test_a_blank_encoding_is_reported_with_its_path_and_a_remedy(self, tmp_path):
        write_model_element(tmp_path, {"name": "legacy_u8", "kind": "base_type", "size": 8, "encoding": ""})

        with pytest.raises(ValueError) as exc_info:
            read_layer(str(tmp_path), "base_types")

        message = str(exc_info.value)
        assert "legacy_u8.yaml" in message
        assert "encoding must not be blank" in message
        # Actionable: the message names what a valid token looks like.
        assert "NONE" in message

    def test_malformed_yaml_syntax_is_reported_with_its_path(self, tmp_path):
        bad = tmp_path / "swc" / "body" / "Broken.yaml"
        bad.parent.mkdir(parents=True)
        bad.write_text("name: Broken\n  ports: [oops\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Broken.yaml"):
            read_swc_model(str(tmp_path))

    @pytest.mark.parametrize("layer", sorted(production_registry().layer_names))
    def test_every_layer_prefixes_the_path(self, tmp_path, layer):
        """Every declared layer's refusal names the offending file by its full path, never the field alone.

        The empty document is invalid for every layer (each declares a required identity field),
        so no row passes by failing to fail;
        ``test_reader_generic.py::test_every_read_wrapper_returns_exactly_what_the_generic_reader_returns``
        pins each public wrapper against ``read_layer``.
        """
        record = production_registry().storage(layer)
        bad = tmp_path / record.directory / f"Bad{record.file_suffix}"
        _write_yaml(bad, {})

        with pytest.raises(ValueError) as exc_info:
            read_layer(str(tmp_path), layer)

        assert str(bad) in exc_info.value.render(full_path=True)


# ---------------------------------------------------------------------------
# SWC placement metadata
# ---------------------------------------------------------------------------


class TestSwcPlacementMetadata:
    """``swc.group`` is captured beside the element, never inside it.

    The directory is the group's only representation, so it is read back as *placement*
    metadata rather than promoted into ``SWComponent``, where it would be a second source of truth.
    """

    def test_swcomponent_still_has_no_group_field(self):
        """The whole design rests on this: a Pydantic field is NOT how group is carried."""
        assert "group" not in SWComponent.model_fields

    def test_a_grouped_component_reports_its_directory(self, tmp_path):
        _write_yaml(tmp_path / "swc" / "sensors" / "FooSWC.yaml", VALID_SWC)

        pairs = read_swc_model_with_placement(str(tmp_path))

        assert [(swc.name, placement) for swc, placement in pairs] == [("FooSWC", {"group": "sensors"})]

    def test_a_flat_component_reports_the_documented_default(self, tmp_path):
        """``swc/FooSWC.yaml`` has no group directory, so the group is the empty string.

        ``create_swc`` refuses an empty group, but the reader reports what is on disk
        instead of inventing a name.
        """
        _write_yaml(tmp_path / "swc" / "FooSWC.yaml", VALID_SWC)

        pairs = read_swc_model_with_placement(str(tmp_path))

        placement_field = production_registry().layer("swc").storage.nesting
        assert pairs[0][1] == {placement_field: ""}
        assert placement_field == "group"

    def test_a_deeper_tree_is_reported_verbatim(self, tmp_path):
        """``swc/a/b/Foo.yaml`` reports ``a/b`` — a read reports, it does not validate.

        Such a value is refused one layer up (the registry's placement rule), not normalized here.
        """
        _write_yaml(tmp_path / "swc" / "a" / "b" / "FooSWC.yaml", VALID_SWC)

        assert read_swc_model_with_placement(str(tmp_path))[0][1] == {"group": "a/b"}

    def test_a_filename_disagreeing_with_the_declared_name_keeps_read_side_behaviour(self, tmp_path):
        """The element's identity comes from the file's contents, as everywhere else."""
        _write_yaml(tmp_path / "swc" / "sensors" / "wrong_file_name.yaml", VALID_SWC)

        pairs = read_swc_model_with_placement(str(tmp_path))

        assert [swc.name for swc, _ in pairs] == ["FooSWC"]
        assert pairs[0][1] == {"group": "sensors"}

    def test_read_swc_model_returns_exactly_the_paired_elements(self, tmp_path):
        """``read_swc_model`` answers the same elements in the same order, without placement."""
        _write_yaml(tmp_path / "swc" / "sensors" / "FooSWC.yaml", VALID_SWC)
        _write_yaml(tmp_path / "swc" / "actuators" / "BarSWC.yaml", {**VALID_SWC, "name": "BarSWC"})

        elements = read_swc_model(str(tmp_path))
        pairs = read_swc_model_with_placement(str(tmp_path))

        assert elements == [swc for swc, _ in pairs]
        assert [swc.name for swc in elements] == ["BarSWC", "FooSWC"]
        for swc in elements:
            assert "group" not in swc.model_dump()

    def test_a_missing_model_root_still_raises_filenotfounderror(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_swc_model_with_placement(str(tmp_path / "nope"))

    def test_a_missing_swc_dir_is_an_empty_list(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        assert read_swc_model_with_placement(str(tmp_path)) == []

    def test_a_malformed_file_is_still_reported_with_its_path(self, tmp_path):
        _write_yaml(tmp_path / "swc" / "sensors" / "Bad.yaml", {"name": "", "type": "application"})

        with pytest.raises(ValueError, match="Bad.yaml"):
            read_swc_model_with_placement(str(tmp_path))


class TestPlacementReachesNoGeneratedArtifact:
    """Moving components to another group changes no byte of the ARXML or the component export.

    The ARXML is also compared against a committed golden, a record independent of this suite's
    output. Two deliberate exceptions are asserted rather than dropped: ``needs.json``'s
    ``swcraft_path`` and the diagram pages' ``swcraft-model``/``swcraft-elements`` blocks name each
    element's file by its model-root-relative storage name, which carries the SWC group directory;
    nothing else of either moves.
    """

    GROUP_RENAME = "zzz_regrouped"

    def _regrouped_model(self, tmp_path):
        """A copy of examples/basic whose SWC files moved to another group.

        The new group sorts every file into the same relative order as ``example/``, so a
        difference can only come from the group value, never from glob ordering.
        """
        source = REPO_ROOT / "examples" / "basic" / "model"
        target = tmp_path / "regrouped_model"
        shutil.copytree(source, target)
        moved = target / "swc" / self.GROUP_RENAME
        moved.mkdir(parents=True)
        for yaml_file in sorted((target / "swc" / "example").glob("*.yaml"), key=lambda path: path.as_posix()):
            yaml_file.rename(moved / yaml_file.name)
        (target / "swc" / "example").rmdir()
        return target

    def test_generated_arxml_is_byte_identical_to_the_pre_change_golden(self, tmp_path):
        output = tmp_path / "model.arxml"
        generate_arxml(str(REPO_ROOT / "examples" / "basic" / "model"), str(output))

        assert output.read_text(encoding="utf-8") == read_golden("basic-4.2.2")

    def test_moving_a_component_between_groups_changes_no_output_byte(self, tmp_path):
        original = tmp_path / "original" / "model.arxml"
        regrouped = tmp_path / "regrouped" / "model.arxml"
        original.parent.mkdir()
        regrouped.parent.mkdir()

        generate_arxml(str(REPO_ROOT / "examples" / "basic" / "model"), str(original))
        generate_arxml(str(self._regrouped_model(tmp_path)), str(regrouped))

        assert original.read_bytes() == regrouped.read_bytes()

    def test_moving_a_component_between_groups_moves_only_its_needs_swcraft_path(self, tmp_path):
        """Only each need's ``swcraft_path``, the file declaring it, moves with the component."""
        original = tmp_path / "original" / "needs.json"
        regrouped = tmp_path / "regrouped" / "needs.json"
        _write_needs_json(str(REPO_ROOT / "examples" / "basic" / "model"), str(original))
        _write_needs_json(str(self._regrouped_model(tmp_path)), str(regrouped))

        def needs(path):
            document = json.loads(path.read_text(encoding="utf-8"))
            return document["versions"][document["current_version"]]["needs"]

        before, after = needs(original), needs(regrouped)
        paths_before = {need_id: need.pop("swcraft_path") for need_id, need in before.items()}
        paths_after = {need_id: need.pop("swcraft_path") for need_id, need in after.items()}
        assert before == after
        assert paths_after == {
            need_id: path.replace("swc/example/", f"swc/{self.GROUP_RENAME}/") for need_id, path in paths_before.items()
        }
        assert paths_after != paths_before

    def test_moving_a_component_between_groups_moves_only_the_pages_storage_names(self, tmp_path):
        """The diagrams draw no group: every ``.svg`` and ``report.txt`` is byte-identical.

        Each ``.html`` page carries every drawn element's storage name (a view page's
        ``swcraft-model`` block, the index's ``swcraft-elements`` block), which holds the
        group directory: each page is identical outside those blocks, and each block
        differs exactly by the moved files' group.
        """
        node = requires_node()
        original = tmp_path / "original" / "views"
        regrouped = tmp_path / "regrouped" / "views"

        generate_diagrams(str(REPO_ROOT / "examples" / "basic" / "model"), str(original), "basic", node=node)
        generate_diagrams(str(self._regrouped_model(tmp_path)), str(regrouped), "basic", node=node)

        before = {entry.name: entry.read_bytes() for entry in original.iterdir()}
        after = {entry.name: entry.read_bytes() for entry in regrouped.iterdir()}
        assert before and before.keys() == after.keys()
        assert {name: data for name, data in before.items() if not name.endswith(".html")} == {
            name: data for name, data in after.items() if not name.endswith(".html")
        }

        block = re.compile(r'<script type="application/json" id="(swcraft-model|swcraft-elements)">(.*?)</script>')

        def moved(storage_name):
            return storage_name.replace("swc/example/", f"swc/{self.GROUP_RENAME}/", 1)

        def split(data):
            text = data.decode("utf-8")
            blocks = {match.group(1): json.loads(match.group(2)) for match in block.finditer(text)}
            return block.sub(lambda match: match.group(0)[: match.start(2) - match.start(0)], text), blocks

        pages = sorted(name for name in before if name.endswith(".html"))
        assert "index.html" in pages and len(pages) > 1
        for name in pages:
            outside_before, blocks_before = split(before[name])
            outside_after, blocks_after = split(after[name])
            assert outside_before == outside_after, name
            expected = ["swcraft-elements"] if name == "index.html" else ["swcraft-model"]
            assert sorted(blocks_before) == sorted(blocks_after) == expected, name
            if name == "index.html":
                elements = blocks_before["swcraft-elements"]
                assert blocks_after["swcraft-elements"] == {moved(key): value for key, value in elements.items()}
                assert any(key.startswith("swc/example/") for key in elements)
            else:
                entries = blocks_before["swcraft-model"]
                assert blocks_after["swcraft-model"] == {
                    key: {**entry, "file": moved(entry["file"])} for key, entry in entries.items()
                }, name
        assert before != after

    def test_component_export_stays_group_free_and_byte_identical(self, tmp_path):
        """Capturing placement in the reader does not disturb the component export.

        ``export.py`` materializes into a FIXED group precisely because the original one is not part of ``SWComponent``.
        """
        original = tmp_path / "original" / "ExampleSWC.arxml"
        regrouped = tmp_path / "regrouped" / "ExampleSWC.arxml"

        export_component(str(REPO_ROOT / "examples" / "basic" / "model"), "ExampleSWC", str(original))
        export_component(str(self._regrouped_model(tmp_path)), "ExampleSWC", str(regrouped))

        assert original.read_bytes() == regrouped.read_bytes()
