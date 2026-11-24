"""Tests for writer/mutations.py: create_data_type."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import layer_directories, model_element_path
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import find_data_type_file, find_element_file, read_data_types, read_layer
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import create_data_type, generate_arxml
from swcraft_tools.writer.arxml_generation import COMPU_METHOD_SUFFIX, UNIT_SUFFIX

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(tmp_path):
    """Return a fresh AuditLog for testing."""
    return AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))


def _read_base_types(model_root) -> list:
    """Every base type: the elements of a layer of their own."""
    return read_layer(str(model_root), "base_types")


def _read_both_layers(model_root) -> list:
    """Every element of the two data description layers, base types first."""
    return [*_read_base_types(model_root), *read_data_types(str(model_root))]


def _read_application_types(model_root) -> list:
    """Every application type: the elements of a layer of their own."""
    return read_layer(str(model_root), "application_types")


def _find_application_type_file(model_root, name: str) -> str | None:
    """The file of application type *name*, looked up in the ``application_types`` layer."""
    return find_element_file(str(model_root), "application_types", name)


def _find_base_type_file(model_root, name: str) -> str | None:
    """The file of base type *name*, looked up in the ``base_types`` layer."""
    return find_element_file(str(model_root), "base_types", name)


# ---------------------------------------------------------------------------
# Success — all 5 kinds
# ---------------------------------------------------------------------------


def test_create_data_type_base_type_creates_yaml(tmp_path):
    path = create_data_type(
        str(tmp_path),
        name="float32",
        kind="base_type",
        size=32,
        encoding="IEEE754",
        native_declaration="float",
    )
    expected = model_element_path(tmp_path, "float32", kind="base_type")
    assert expected.exists(), f"Expected {expected} to exist"
    assert path == str(expected)

    dts = _read_base_types(tmp_path)
    assert len(dts) == 1
    dt = dts[0]
    assert dt.name == "float32"
    assert dt.kind == "base_type"
    assert dt.size == 32
    assert dt.encoding == "IEEE754"
    assert dt.native_declaration == "float"


def test_create_data_type_value_type_creates_yaml(tmp_path):
    path = create_data_type(
        str(tmp_path),
        name="VehicleSpeed",
        kind="value_type",
        base="float32",
        unit="km_h",
        value_min=0.0,
        value_max=300.0,
        description="Vehicle longitudinal speed",
    )
    expected = model_element_path(tmp_path, "VehicleSpeed", kind="value_type")
    assert expected.exists()
    assert path == str(expected)

    dts = read_data_types(str(tmp_path))
    assert len(dts) == 1
    dt = dts[0]
    assert dt.name == "VehicleSpeed"
    assert dt.kind == "value_type"
    assert dt.base == "float32"
    assert dt.unit == "km_h"
    assert dt.value_min == 0.0
    assert dt.value_max == 300.0
    assert dt.description == "Vehicle longitudinal speed"


def test_create_data_type_enum_creates_yaml(tmp_path):
    path = create_data_type(
        str(tmp_path),
        name="GearPosition",
        kind="enum",
        literals=[{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
    )
    expected = model_element_path(tmp_path, "GearPosition", kind="enum")
    assert expected.exists()
    assert path == str(expected)

    dts = _read_application_types(tmp_path)
    assert len(dts) == 1
    dt = dts[0]
    assert dt.name == "GearPosition"
    assert dt.kind == "enum"
    assert len(dt.literals) == 2
    assert dt.literals[0].name == "PARK"
    assert dt.literals[0].value == 0
    assert dt.literals[1].name == "DRIVE"
    assert dt.literals[1].value == 1


def test_create_data_type_record_creates_yaml(tmp_path):
    path = create_data_type(
        str(tmp_path),
        name="VehicleState",
        kind="record",
        fields=[{"name": "speed", "type": "float32"}, {"name": "ignition_on", "type": "bool"}],
    )
    expected = model_element_path(tmp_path, "VehicleState", kind="record")
    assert expected.exists()
    assert path == str(expected)

    dts = _read_application_types(tmp_path)
    assert len(dts) == 1
    dt = dts[0]
    assert dt.name == "VehicleState"
    assert dt.kind == "record"
    assert len(dt.fields) == 2
    assert dt.fields[0].name == "speed"
    assert dt.fields[0].type == "float32"


def test_create_data_type_array_creates_yaml(tmp_path):
    path = create_data_type(
        str(tmp_path),
        name="WheelSpeedArray",
        kind="array",
        element_type="float32",
        length=4,
    )
    expected = model_element_path(tmp_path, "WheelSpeedArray", kind="array")
    assert expected.exists()
    assert path == str(expected)

    dts = _read_application_types(tmp_path)
    assert len(dts) == 1
    dt = dts[0]
    assert dt.name == "WheelSpeedArray"
    assert dt.kind == "array"
    assert dt.element_type == "float32"
    assert dt.length == 4


# ---------------------------------------------------------------------------
# Error cases — common guards
# ---------------------------------------------------------------------------


def test_create_data_type_empty_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name must not be empty"):
        create_data_type(str(tmp_path), name="", kind="base_type", size=32, encoding="NONE")


def test_create_data_type_whitespace_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name must not be empty"):
        create_data_type(str(tmp_path), name="   ", kind="base_type", size=32, encoding="NONE")


def test_create_data_type_path_traversal_slash_raises(tmp_path):
    with pytest.raises(ValueError, match="path separators"):
        create_data_type(str(tmp_path), name="../../../etc/evil", kind="base_type", size=32, encoding="NONE")


def test_create_data_type_path_traversal_backslash_raises(tmp_path):
    with pytest.raises(ValueError, match="path separators"):
        create_data_type(str(tmp_path), name="..\\evil", kind="base_type", size=32, encoding="NONE")


def test_create_data_type_path_traversal_dotdot_raises(tmp_path):
    with pytest.raises(ValueError, match="path separators"):
        create_data_type(str(tmp_path), name="..", kind="base_type", size=32, encoding="NONE")


def test_create_data_type_duplicate_raises(tmp_path):
    create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")
    with pytest.raises(ValueError, match="float32"):
        create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")


def test_create_data_type_whose_file_name_another_type_holds_takes_a_collision_suffix(tmp_path):
    """The file name is held, the identity is free: the store keeps both names.

    The identity is what refuses (``find_data_type_file`` scans content); the file
    name only takes the nearest free ``~N``, and the file that held it is untouched.
    """
    import yaml as _yaml

    # A file whose path says float32.yaml but whose YAML name is float64
    dt_dir = tmp_path / "data_types" / "base"
    dt_dir.mkdir(parents=True)
    occupant = _yaml.safe_dump(
        {"name": "float64", "kind": "base_type", "size": 64, "encoding": "IEEE754"},
        default_flow_style=False,
    )
    (dt_dir / "float32.yaml").write_text(occupant, encoding="utf-8")

    result = create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")

    assert Path(result).relative_to(tmp_path).as_posix() == "data_types/base/float32~1.yaml"
    assert _yaml.safe_load(Path(result).read_text(encoding="utf-8"))["name"] == "float32"
    assert (dt_dir / "float32.yaml").read_text(encoding="utf-8") == occupant


# ---------------------------------------------------------------------------
# Error cases — missing required fields per kind
# ---------------------------------------------------------------------------


def test_create_data_type_base_type_missing_size_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="mytype", kind="base_type", encoding="NONE")


def test_create_data_type_base_type_missing_encoding_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="mytype", kind="base_type", size=32)


def test_create_data_type_value_type_missing_base_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="Speed", kind="value_type")


def test_create_data_type_enum_missing_literals_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="GearPos", kind="enum")


def test_create_data_type_enum_empty_literals_raises(tmp_path):
    with pytest.raises(ValueError, match="literals must not be empty"):
        create_data_type(str(tmp_path), name="GearPos", kind="enum", literals=[])


def test_create_data_type_record_missing_fields_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="State", kind="record")


def test_create_data_type_record_empty_fields_raises(tmp_path):
    with pytest.raises(ValueError, match="fields must not be empty"):
        create_data_type(str(tmp_path), name="State", kind="record", fields=[])


def test_create_data_type_array_missing_element_type_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="SpeedArr", kind="array", length=4)


def test_create_data_type_array_missing_length_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="SpeedArr", kind="array", element_type="float32")


def test_create_data_type_array_zero_length_raises(tmp_path):
    with pytest.raises(ValueError, match="length must be > 0"):
        create_data_type(str(tmp_path), name="SpeedArr", kind="array", element_type="float32", length=0)


def test_create_data_type_value_type_whose_base_shares_its_name_is_accepted(tmp_path):
    """A value type whose ``base`` shares its name is accepted.

    ``base`` names an element of the ``base_types`` layer, so sharing the value type's own name is no self-reference."""
    create_data_type(str(tmp_path), name="Speed", kind="base_type", size=32, encoding="IEEE754")
    path = create_data_type(str(tmp_path), name="Speed", kind="value_type", base="Speed")
    assert path == str(model_element_path(tmp_path, "Speed", kind="value_type"))
    assert [(dt.kind, dt.name) for dt in _read_both_layers(tmp_path)] == [
        ("base_type", "Speed"),
        ("value_type", "Speed"),
    ]


def test_create_data_type_unknown_kind_raises(tmp_path):
    with pytest.raises(ValueError):
        create_data_type(str(tmp_path), name="mytype", kind="bogus")


# ---------------------------------------------------------------------------
# find_data_type_file locates file after creation
# ---------------------------------------------------------------------------


def test_find_data_type_file_after_creation(tmp_path):
    create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")
    found = _find_base_type_file(tmp_path, "float32")
    assert found is not None
    assert found == str(model_element_path(tmp_path, "float32", kind="base_type"))


def test_find_data_type_file_not_found_before_creation(tmp_path):
    assert find_data_type_file(str(tmp_path), "float32") is None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_create_data_type_with_auditor(tmp_path):
    auditor = _audit(tmp_path)
    create_data_type(
        str(tmp_path),
        name="float32",
        kind="base_type",
        size=32,
        encoding="IEEE754",
        native_declaration="float",
        auditor=auditor,
    )
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_data_type"
    assert entries[0]["args"]["name"] == "float32"
    assert entries[0]["args"]["kind"] == "base_type"


def test_create_data_type_no_auditor_does_not_raise(tmp_path):
    # Without auditor, should succeed silently
    path = create_data_type(str(tmp_path), name="uint8", kind="base_type", size=8, encoding="NONE")
    assert path is not None


# ---------------------------------------------------------------------------
# YAML content checks
# ---------------------------------------------------------------------------


def test_create_data_type_base_type_yaml_content(tmp_path):
    create_data_type(
        str(tmp_path),
        name="uint32",
        kind="base_type",
        size=32,
        encoding="NONE",
        native_declaration="uint32_t",
        description="Unsigned 32-bit integer",
    )
    data = yaml.safe_load((model_element_path(tmp_path, "uint32", kind="base_type")).read_text(encoding="utf-8"))
    assert data["name"] == "uint32"
    assert data["kind"] == "base_type"
    assert data["size"] == 32
    assert data["encoding"] == "NONE"
    assert data["native_declaration"] == "uint32_t"
    assert data["description"] == "Unsigned 32-bit integer"


def test_create_data_type_value_type_optional_fields_omitted_when_none(tmp_path):
    """value_min/value_max should be omitted from YAML when not provided."""
    create_data_type(
        str(tmp_path),
        name="RawSpeed",
        kind="value_type",
        base="float32",
    )
    data = yaml.safe_load((model_element_path(tmp_path, "RawSpeed", kind="value_type")).read_text(encoding="utf-8"))
    assert "value_min" not in data
    assert "value_max" not in data
    assert "description" not in data


def test_create_data_type_multiple_files(tmp_path):
    """Multiple data types coexist in the same directory."""
    create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")
    create_data_type(str(tmp_path), name="uint8", kind="base_type", size=8, encoding="NONE")
    create_data_type(
        str(tmp_path),
        name="Speed",
        kind="value_type",
        base="float32",
    )
    dts = _read_both_layers(tmp_path)
    names = {dt.name for dt in dts}
    assert names == {"float32", "uint8", "Speed"}


class TestCreateDataTypeRefusesReservedNames:
    """The write tool must refuse what the ARXML writer will refuse.

    The data-type twin of ``TestCreateSwcRefusesReservedNames``: an accepted name
    that ``check-model`` and ``generate_arxml`` then refuse would leave a state no
    call could undo, which the recoverability invariant forbids.

    The kind-scoped reservation left is ``{enum}_CompuMethod``;
    ``TestTheRetiredReservationsAreWithdrawnInBothDirections`` is the inversion. The
    method lies in the enum's package, so only a declared type of that package
    collides with it: the occupants below are application types, and a base type
    under the name is written.
    """

    @staticmethod
    def _primitive(tmp_path, name: str) -> str:
        return create_data_type(str(tmp_path), name=name, kind="base_type", size=32, encoding="IEEE754")

    @staticmethod
    def _application(tmp_path, name: str) -> str:
        """An application type, an occupant of the enum's own package."""
        return create_data_type(str(tmp_path), name=name, kind="application_type")

    @staticmethod
    def _enum(tmp_path, name: str) -> str:
        """An enum, the one kind a generated data-type name is still reserved for."""
        return create_data_type(str(tmp_path), name=name, kind="enum", literals=[{"name": "PARK", "value": 0}])

    def test_the_compu_method_name_beside_its_enum_is_refused(self, tmp_path):
        self._enum(tmp_path, "Gear_T")
        with pytest.raises(ValueError, match="collides with the COMPU-METHOD auto-generated for 'Gear_T'"):
            self._application(tmp_path, f"Gear_T{COMPU_METHOD_SUFFIX}")

    def test_the_reverse_order_is_refused_too(self, tmp_path):
        """Either order produces the same collision, exactly like the SWC guard."""
        self._application(tmp_path, f"Gear_T{COMPU_METHOD_SUFFIX}")
        with pytest.raises(ValueError, match="collides with the existing data type"):
            self._enum(tmp_path, "Gear_T")

    def test_a_type_of_another_package_under_the_name_is_allowed(self, tmp_path):
        """A base type lies in ``/DataTypes/Base``, the method in ``/DataTypes/Application``."""
        self._enum(tmp_path, "Gear_T")
        self._primitive(tmp_path, f"Gear_T{COMPU_METHOD_SUFFIX}")
        assert _find_base_type_file(tmp_path, f"Gear_T{COMPU_METHOD_SUFFIX}") is not None

    def test_a_suffixed_name_with_no_base_is_allowed(self, tmp_path):
        """``other_CompuMethod`` alone collides with nothing.

        Refusing it would reject a legal name."""
        self._primitive(tmp_path, f"other{COMPU_METHOD_SUFFIX}")
        assert [dt.name for dt in _read_base_types(tmp_path)] == [f"other{COMPU_METHOD_SUFFIX}"]

    def test_the_rules_are_kind_scoped(self, tmp_path):
        """The writer emits a compu method only for an enum, so a record base reserves nothing.

        A stricter guard would refuse a name generation accepts."""
        create_data_type(str(tmp_path), name="Pose_T", kind="record", fields=[{"name": "x", "type": "float32"}])

        self._application(tmp_path, f"Pose_T{COMPU_METHOD_SUFFIX}")

        assert _find_application_type_file(tmp_path, f"Pose_T{COMPU_METHOD_SUFFIX}") is not None

    def test_the_application_forms_are_field_scoped(self, tmp_path):
        """``{x}_Unit`` is reserved only where ``x`` declares a ``unit``."""
        create_data_type(str(tmp_path), name="Bare", kind="base_type", size=8, encoding="NONE")
        # An application type declaring no `unit` mints no `{name}_Unit`, so the name is ordinary.
        create_data_type(str(tmp_path), name="Speed", kind="application_type")
        self._application(tmp_path, f"Speed{UNIT_SUFFIX}")
        assert _find_application_type_file(tmp_path, f"Speed{UNIT_SUFFIX}") is not None

    def test_a_refused_name_leaves_no_file(self, tmp_path):
        self._enum(tmp_path, "Gear_T")
        with pytest.raises(ValueError):
            self._application(tmp_path, f"Gear_T{COMPU_METHOD_SUFFIX}")

        assert not (tmp_path / "data_types" / "application" / f"Gear_T{COMPU_METHOD_SUFFIX}.yaml").exists()

    def test_what_the_guard_prevents_really_is_ungenerable(self, tmp_path):
        """The claim behind the refusal: the model the write would have produced."""
        model_root = tmp_path / "model"
        # Every required layer, off the contract: a missing directory a later check
        # reads would make the refusal below untestable rather than untested.
        for subdir in layer_directories(required_only=True):
            (model_root / subdir).mkdir(parents=True)
        create_data_type(str(model_root), name="Gear_T", kind="enum", literals=[{"name": "PARK", "value": 0}])
        # The name the guard refuses, written directly.
        (model_element_path(model_root, f"Gear_T{COMPU_METHOD_SUFFIX}", kind="application_type")).write_text(
            yaml.safe_dump({"name": f"Gear_T{COMPU_METHOD_SUFFIX}", "kind": "application_type"}),
            encoding="utf-8",
        )

        # The collision itself, not only some refusal of the bare model: a base type
        # occupant passed both lines through its missing `native_declaration` alone.
        assert "data-type.compu-method-name-collision" in {
            record.rule for record in check_cross_layer_consistency(str(model_root)).records
        }
        with pytest.raises(ValueError, match="collide with auto-generated '{base}_CompuMethod'"):
            generate_arxml(str(model_root), str(tmp_path / "never-written.arxml"))


# ---------------------------------------------------------------------------
# The serializer's reference-type arm
# ---------------------------------------------------------------------------


class TestAReferenceTypeIsSerializedAndRoundTrips:
    """``data_type_to_dict`` is a field list written by hand.

    ``writer-mutations`` is a ``total`` consumer, so no coverage gate notices a kind it does not name."""

    def test_the_serializer_writes_every_declared_field_of_a_reference_type(self):
        from swcraft_tools.schema import ReferenceDataType
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        described = ReferenceDataType(name="SpeedRef", kind="reference_type", base="Speed_T", description="a speed")
        assert data_type_to_dict(described) == {
            "name": "SpeedRef",
            "kind": "reference_type",
            "base": "Speed_T",
            "description": "a speed",
        }
        bare = ReferenceDataType(name="SpeedRef", kind="reference_type", base="Speed_T")
        assert data_type_to_dict(bare) == {"name": "SpeedRef", "kind": "reference_type", "base": "Speed_T"}

    def test_create_then_read_is_equal(self, tmp_path):
        from swcraft_tools.schema import ReferenceDataType

        create_data_type(str(tmp_path), name="float32_base", kind="base_type", size=32, encoding="IEEE754")
        create_data_type(str(tmp_path), name="float32", kind="value_type", base="float32_base")
        path = create_data_type(
            str(tmp_path), name="SpeedRef", kind="reference_type", base="float32", description="a speed"
        )

        assert path == str(model_element_path(tmp_path, "SpeedRef", kind="reference_type"))
        by_name = {dt.name: dt for dt in read_data_types(str(tmp_path))}
        assert by_name["SpeedRef"] == ReferenceDataType(
            name="SpeedRef", kind="reference_type", base="float32", description="a speed"
        )
        # The scratch root holds no hardware or composition, so the whole-model gate
        # is not the question; the data-type references are.
        from swcraft_tools.validator import check_data_type_references

        assert check_data_type_references(str(tmp_path)).consistent

    def test_a_reference_type_needs_a_base(self, tmp_path):
        with pytest.raises(ValueError, match="'base' is required for reference_type"):
            create_data_type(str(tmp_path), name="SpeedRef", kind="reference_type")


class TestTheRetiredReservationsAreWithdrawnInBothDirections:
    """``{name}_impl``, ``{name}_AppView`` and ``DataTypeMappingSet``: names the writer no longer mints.

    A guard refusing any of them would be stricter than the writer, so each is
    asserted **accepted**, in both orders where an order exists.
    """

    def test_an_impl_suffixed_name_beside_a_base_type_is_accepted(self, tmp_path):
        create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")
        create_data_type(str(tmp_path), name="float32_impl", kind="value_type", base="float32")
        assert {dt.name for dt in _read_both_layers(tmp_path)} == {"float32", "float32_impl"}

    def test_a_base_type_beside_an_existing_impl_suffixed_name_is_accepted(self, tmp_path):
        create_data_type(str(tmp_path), name="u_base", kind="base_type", size=8, encoding="NONE")
        create_data_type(str(tmp_path), name="float32_impl", kind="value_type", base="u_base")
        create_data_type(str(tmp_path), name="float32", kind="base_type", size=32, encoding="IEEE754")
        assert {dt.name for dt in _read_both_layers(tmp_path)} == {"u_base", "float32_impl", "float32"}

    def test_an_app_view_name_beside_a_value_type_is_accepted(self, tmp_path):
        """No application view is generated, in either order."""
        create_data_type(str(tmp_path), name="u_base", kind="base_type", size=8, encoding="NONE")
        create_data_type(str(tmp_path), name="u8", kind="value_type", base="u_base")
        create_data_type(str(tmp_path), name="u8_AppView", kind="value_type", base="u_base")
        assert "u8_AppView" in {dt.name for dt in read_data_types(str(tmp_path))}

        other = tmp_path / "other"
        create_data_type(str(other), name="u_base", kind="base_type", size=8, encoding="NONE")
        create_data_type(str(other), name="Count_T_AppView", kind="value_type", base="u_base")
        create_data_type(str(other), name="u8", kind="value_type", base="u_base")
        create_data_type(str(other), name="Count_T", kind="reference_type", base="u8")
        assert {"Count_T", "Count_T_AppView"} <= {dt.name for dt in read_data_types(str(other))}

    def test_the_former_mapping_set_name_is_accepted(self, tmp_path):
        """``DataTypeMappingSet`` is an ordinary data type name: the writer owns no fixed name."""
        create_data_type(str(tmp_path), name="DataTypeMappingSet", kind="base_type", size=32, encoding="IEEE754")
        assert [dt.name for dt in _read_base_types(tmp_path)] == ["DataTypeMappingSet"]


# ---------------------------------------------------------------------------
# The application type's create arm
# ---------------------------------------------------------------------------


class TestAnApplicationTypeIsCreatedAndRoundTrips:
    """``create_data_type``'s application type arm.

    It and ``writer/elements.write_element`` (the editing protocol's path) must
    accept and refuse the same model.

    ``unit``, ``conversion`` and ``range`` are each optional and each omitted at
    its default, so a type declaring none round-trips byte-identically. The
    asymmetry between a blank ``unit`` (no unit) and ``conversion: {}`` (the
    identity conversion, a declaration) is the schema's and is asserted here
    because it decides which generated names are reserved.
    """

    def test_every_declared_field_is_written_and_read_back(self, tmp_path):
        from swcraft_tools.schema import ApplicationDataType, Conversion, ValueRange

        path = create_data_type(
            str(tmp_path),
            name="VehicleSpeed_Kph",
            kind="application_type",
            unit="kph",
            conversion={"factor": 0.1, "offset": -40.0},
            range={"min": 0.0, "max": 300.0},
            description="Vehicle speed, physical view",
        )

        assert path == str(model_element_path(tmp_path, "VehicleSpeed_Kph", kind="application_type"))
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert list(document) == ["name", "kind", "unit", "conversion", "range", "description"]
        assert document["conversion"] == {"factor": 0.1, "offset": -40.0}
        assert document["range"] == {"min": 0.0, "max": 300.0}

        by_name = {dt.name: dt for dt in _read_application_types(tmp_path)}
        assert by_name["VehicleSpeed_Kph"] == ApplicationDataType(
            name="VehicleSpeed_Kph",
            kind="application_type",
            unit="kph",
            conversion=Conversion(factor=0.1, offset=-40.0),
            range=ValueRange(min=0.0, max=300.0),
            description="Vehicle speed, physical view",
        )

    def test_the_three_optional_fields_are_omitted_at_their_defaults(self, tmp_path):
        path = create_data_type(str(tmp_path), name="Bare", kind="application_type")

        assert yaml.safe_load(Path(path).read_text(encoding="utf-8")) == {
            "name": "Bare",
            "kind": "application_type",
        }

    def test_an_identity_conversion_is_a_declaration_and_is_written(self, tmp_path):
        """``conversion: {}`` is not an absent conversion: both fields default."""
        path = create_data_type(str(tmp_path), name="Counts", kind="application_type", conversion={})

        assert yaml.safe_load(Path(path).read_text(encoding="utf-8")) == {
            "name": "Counts",
            "kind": "application_type",
            "conversion": {"factor": 1.0, "offset": 0.0},
        }

    def test_one_bound_of_a_range_is_written_alone(self, tmp_path):
        path = create_data_type(str(tmp_path), name="NonNegative", kind="application_type", range={"min": 0.0})

        assert yaml.safe_load(Path(path).read_text(encoding="utf-8"))["range"] == {"min": 0.0}

    def test_an_incoherent_range_is_refused_by_the_schema(self, tmp_path):
        with pytest.raises(ValueError, match="range 'min' must be <= 'max'"):
            create_data_type(str(tmp_path), name="Backwards", kind="application_type", range={"min": 10.0, "max": 1.0})
        assert not (tmp_path / "data_types").exists()

    def test_a_foreign_key_inside_conversion_is_refused(self, tmp_path):
        """``Conversion`` closes with ``extra="forbid"``.

        A dropped key is a wrong physical value, not a missing note."""
        with pytest.raises(ValueError):
            create_data_type(str(tmp_path), name="Typo", kind="application_type", conversion={"scale": 2.0})

    def test_the_audit_entry_names_the_kind(self, tmp_path):
        auditor = _audit(tmp_path)
        create_data_type(str(tmp_path), name="VehicleSpeed_Kph", kind="application_type", unit="kph", auditor=auditor)

        entries = auditor.get_entries()
        assert entries[0]["args"] == {"name": "VehicleSpeed_Kph", "kind": "application_type", "unit": "kph"}

    def test_the_file_name_is_derived_from_the_identity_and_takes_a_collision_suffix(self, tmp_path):
        directory = tmp_path / "data_types" / "application"
        directory.mkdir(parents=True)
        occupant = "name: Occupant\nkind: application_type\n"
        (directory / "vehiclespeed_kph.yaml").write_text(occupant, encoding="utf-8", newline="\n")

        result = create_data_type(str(tmp_path), name="VehicleSpeed_Kph", kind="application_type")

        assert Path(result).relative_to(tmp_path).as_posix() == "data_types/application/VehicleSpeed_Kph~1.yaml"
        assert (directory / "vehiclespeed_kph.yaml").read_text(encoding="utf-8") == occupant

    def test_an_application_type_names_no_base(self, tmp_path):
        """An application type references no data type at all, so ``base`` is written nowhere.

        That is what keeps it out of every cycle.

        A ``base=`` handed to this arm is **ignored** rather than refused, as every
        arm ignores the parameters of the other kinds (``create_data_type`` has no
        ``_refuse_foreign_fields`` twin).
        """
        path = create_data_type(str(tmp_path), name="Speed", kind="application_type", base="float32")

        assert "base" not in yaml.safe_load(Path(path).read_text(encoding="utf-8"))


class TestTheGeneratedFormsAreGuardedOnTheCreatePath:
    """The budget and the collision of the three generated forms, on the narrow writer.

    ``check-model``, the write path and the exporter refuse the same names. A form
    collides inside its owner's package only, so the colliding occupant is an
    application type.
    """

    def test_an_over_long_unit_name_is_refused_before_anything_is_written(self, tmp_path):
        from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH

        name = "U" * (MAX_IDENTIFIER_LENGTH - len(UNIT_SUFFIX) + 1)
        with pytest.raises(ValueError, match="generated UNIT name"):
            create_data_type(str(tmp_path), name=name, kind="application_type", unit="kph")
        assert not (tmp_path / "data_types").exists()

    def test_a_type_declaring_no_unit_is_held_to_the_declared_length_alone(self, tmp_path):
        """The forms are reserved **per declared field**.

        No ``unit``, no ``{name}_Unit``, so the same name is legal."""
        from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH

        name = "U" * (MAX_IDENTIFIER_LENGTH - len(UNIT_SUFFIX) + 1)
        create_data_type(str(tmp_path), name=name, kind="application_type")
        assert [dt.name for dt in _read_application_types(tmp_path)] == [name]

    def test_the_unit_name_beside_its_application_type_is_refused(self, tmp_path):
        create_data_type(str(tmp_path), name="Speed", kind="application_type", unit="kph")
        with pytest.raises(ValueError, match="collides with the UNIT auto-generated for 'Speed'"):
            create_data_type(str(tmp_path), name=f"Speed{UNIT_SUFFIX}", kind="application_type")

    def test_the_reverse_order_is_refused_too(self, tmp_path):
        create_data_type(str(tmp_path), name=f"Speed{UNIT_SUFFIX}", kind="application_type")
        with pytest.raises(ValueError, match="collides with the existing data type"):
            create_data_type(str(tmp_path), name="Speed", kind="application_type", unit="kph")

    def test_a_type_declaring_no_unit_reserves_nothing(self, tmp_path):
        create_data_type(str(tmp_path), name="Speed", kind="application_type")
        create_data_type(str(tmp_path), name=f"Speed{UNIT_SUFFIX}", kind="application_type")
        assert f"Speed{UNIT_SUFFIX}" in {dt.name for dt in _read_application_types(tmp_path)}

    def test_a_base_type_under_the_unit_name_is_allowed(self, tmp_path):
        """A base type lies in ``/DataTypes/Base``, the ``UNIT`` in ``/DataTypes/Application``."""
        create_data_type(str(tmp_path), name="Speed", kind="application_type", unit="kph")
        create_data_type(str(tmp_path), name=f"Speed{UNIT_SUFFIX}", kind="base_type", size=8, encoding="NONE")
        assert f"Speed{UNIT_SUFFIX}" in {dt.name for dt in _read_base_types(tmp_path)}
