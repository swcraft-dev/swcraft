"""Tests for ``arxml_import.type_mappers.map_data_types`` and the registry's type-name normalization policy.

The shared ``data_types.arxml`` fixture carries two same-name application/implementation pairs: ``BrakeModeEnum``, a
TEXTTABLE pair linked by a ``DATA-TYPE-MAPPING-SET`` and kept as two elements (rule 1 collapses only a bare twin, read
from ``bare_application_twin.arxml``), and ``DoorStateEnum``, an application TEXTTABLE enum beside an unrelated
implementation type of its name. The layers are separate name spaces, so neither renames; rule 2's ``_app`` rename,
which answers a collision inside one layer, is exercised on the registry directly, and shapes the fixture cannot carry
are built inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arxml_import_shapes_support import read_shape_model, shape_path
from swcraft_tools.arxml_import import (
    RENAME_SUFFIX,
    NameCollisionError,
    PathRegistry,
    fit_identifier,
    map_data_types,
    map_interfaces,
)
from swcraft_tools.arxml_import.registry import snake_case
from swcraft_tools.arxml_reader import (
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImportModel,
    RecordElementInfo,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwBaseTypeInfo,
    read_arxml,
)
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, REASON_LENGTH, check_identifier
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    EnumDataType,
    RecordDataType,
    ReferenceDataType,
    ValueDataType,
)
from swcraft_tools.validator import data_type_layer

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
DATA_TYPES = FIXTURES_DIR / "data_types.arxml"
DATA_TYPES_4_3_0 = FIXTURES_DIR / "data_types_4_3_0.arxml"


def _by_name(data_types, name):
    matches = [dt for dt in data_types if dt.name == name]
    assert len(matches) == 1, f"expected exactly one data type named {name!r}, found {len(matches)}"
    return matches[0]


def _in_layer(data_types, layer: str, name: str):
    """The one data type *name* in *layer*: each layer is its own name space."""
    matches = [dt for dt in data_types if dt.name == name and data_type_layer(dt) == layer]
    assert len(matches) == 1, f"expected exactly one {layer} type named {name!r}, found {len(matches)}"
    return matches[0]


def _names(data_types) -> set[str]:
    return {dt.name for dt in data_types}


def _keys(data_types) -> set[tuple[str, str]]:
    """Every mapped type keyed ``(layer, name)``."""
    return {(data_type_layer(dt), dt.name) for dt in data_types}


def _skip_reason(skip_log, path) -> str | None:
    for entry in skip_log:
        if entry.path == path:
            return entry.reason
    return None


@pytest.fixture(scope="module")
def mapped():
    model = read_arxml(str(DATA_TYPES))
    return map_data_types(model, elements=[])


@pytest.fixture
def mapped_forms():
    """``mapped``'s run, with the units, compu methods and data constraints it created."""
    forms: list = []
    data_types, registry, skip_log = map_data_types(read_arxml(str(DATA_TYPES)), elements=forms)
    return data_types, registry, skip_log, forms


# Row 1: SW-BASE-TYPE -> primitive


def test_sw_base_type_maps_to_primitive(mapped):
    data_types, _registry, _skip_log = mapped
    uint8 = _by_name(data_types, "uint8")
    assert isinstance(uint8, BaseDataType)
    assert uint8.size == 8
    assert uint8.encoding == "NONE"
    assert uint8.native_declaration == "uint8"

    uint16 = _by_name(data_types, "uint16")
    assert isinstance(uint16, BaseDataType)
    assert uint16.size == 16
    assert uint16.encoding == "NONE"
    assert uint16.native_declaration == "uint16"


# Rows 2-3: IMPLEMENTATION-DATA-TYPE (VALUE -> value type over its base type,
# TYPE_REFERENCE -> reference type over the implementation type it names)


def test_impl_value_category_maps_to_a_value_type_over_its_base_type(mapped):
    """Row 3: one reference to the base type the BASE-TYPE-REF names, never a copy of it."""
    data_types, _registry, _skip_log = mapped
    uint16_impl = _by_name(data_types, "uint16_impl")
    assert isinstance(uint16_impl, ValueDataType)
    assert uint16_impl.base == "uint16"
    assert not hasattr(uint16_impl, "size")
    assert isinstance(_by_name(data_types, "uint16"), BaseDataType)


def test_every_base_type_is_a_sw_base_type_created_once(mapped):
    """The base types are exactly row 1's, one per SW-BASE-TYPE, and each VALUE type points at one."""
    data_types, _registry, _skip_log = mapped
    model = read_arxml(str(DATA_TYPES))
    base_types = [dt for dt in data_types if isinstance(dt, BaseDataType)]
    assert sorted(dt.name for dt in base_types) == sorted(b.name for b in model.sw_base_types)
    value_impls = [i for i in model.impl_data_types if i.category == "VALUE"]
    assert value_impls
    for impl in value_impls:
        # Looked up through the registry, in the layer the path resolves into:
        # the implementation member of a kept same-name pair shares its name
        # with the application member.
        emitted = _registry.resolve(impl.path)
        assert emitted is not None, impl.name
        assert _registry.layer_of(impl.path) == "implementation_types", impl.name
        mapped_type = _in_layer(data_types, "implementation_types", emitted)
        assert isinstance(mapped_type, ValueDataType), impl.name
        assert impl.base_type_ref is not None
        assert mapped_type.base == impl.base_type_ref.rsplit("/", 1)[-1], impl.name


def test_impl_type_reference_category_maps_to_a_reference_type_one_step(mapped):
    """Row 2: a reference type over the implementation type the IMPLEMENTATION-DATA-TYPE-REF names, not flattened."""
    data_types, _registry, _skip_log = mapped
    reference = _by_name(data_types, "WheelTravelRange_impl")
    assert isinstance(reference, ReferenceDataType)
    assert reference.base == "uint16_impl"
    assert "Alias-over-implementation-type example" in reference.description


def test_a_type_reference_names_the_implementation_type_and_not_its_base_type(mapped):
    """A reference type's base resolves to the VALUE implementation type, never the base type under it."""
    data_types, _registry, _skip_log = mapped
    reference = _by_name(data_types, "WheelTravelRange_impl")
    target = _by_name(data_types, reference.base)
    assert isinstance(target, ValueDataType)
    assert not isinstance(target, BaseDataType)


# Row 4: APPLICATION-PRIMITIVE-DATA-TYPE + TEXTTABLE COMPU-METHOD -> enum


def test_texttable_app_primitive_maps_to_enum_with_literals(mapped_forms):
    data_types, _registry, _skip_log, forms = mapped_forms
    # DoorStateEnum(app) shares its name with DoorStateEnum(impl) in another
    # layer, so no rename (see TestNameNormalization). The enumeration names its
    # TEXTTABLE, a `texttable` element under the source's name, whose entries
    # are its literals.
    enum = _in_layer(data_types, "application_types", "DoorStateEnum")
    assert isinstance(enum, EnumDataType)
    assert (enum.literals, enum.compu_method_ref) == (None, "DoorStateEnum_CompuMethod")
    (table,) = [form for form in forms if form.name == "DoorStateEnum_CompuMethod"]
    assert {(entry.name, entry.value) for entry in table.entries} == {("CLOSED", 0), ("OPEN", 1)}


# Row 5: APPLICATION-PRIMITIVE-DATA-TYPE (other) -> an application_type; a mapping set
# maps it, and an unmapped one is imported too


def test_an_unmapped_non_texttable_app_primitive_is_an_application_type(mapped):
    """An unmapped application primitive is still written as an ``application_type``, not skipped.

    The mapping is a ``mapping_sets/`` element, not a condition of import. Its LINEAR compu method has the
    COMPU-DENOMINATOR 100, which a ``conversion`` does not read, so the type imports with no conversion.
    """
    data_types, _registry, skip_log = mapped
    ratio = _by_name(data_types, "PressureRatio")
    assert isinstance(ratio, ApplicationDataType)
    assert (ratio.unit, ratio.conversion, ratio.range) == ("", None, None)
    assert _skip_reason(skip_log, "/Acme/Chassis/ApplicationTypes/PressureRatio") is None


def test_app_primitive_other_maps_to_an_application_type_beside_the_mapped_type():
    """Row 5: a mapped (non-twin) application primitive is its own ``application_type`` beside the mapped type."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint32",
            name="uint32",
            size=32,
            encoding="NONE",
            native_declaration="uint32",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Torque_impl",
            name="Torque_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint32",
            impl_type_ref=None,
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/Torque",
            name="Torque",
            compu_method_ref=None,
            description="Torque, mapped via DATA-TYPE-MAPPING-SET (no TEXTTABLE).",
        )
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/Pkg/Mapping/PkgMapping",
            name="PkgMapping",
            maps=[
                DataTypeMapInfo(
                    application_ref="/Pkg/AppTypes/Torque",
                    implementation_ref="/Pkg/Types/Torque_impl",
                )
            ],
        )
    )

    data_types, _registry, skip_log = map_data_types(model, elements=[])

    torque = _by_name(data_types, "Torque")
    assert torque == ApplicationDataType(
        name="Torque",
        kind="application_type",
        unit="",
        conversion=None,
        range=None,
        description="Torque, mapped via DATA-TYPE-MAPPING-SET (no TEXTTABLE).",
    )
    # The mapped implementation type is untouched: the two are two views of one
    # signal and the mapping between them is the set's to hold.
    assert isinstance(_by_name(data_types, "Torque_impl"), ValueDataType)
    assert len(skip_log) == 0


# Rows 6-7: APPLICATION-RECORD-DATA-TYPE -> record, APPLICATION-ARRAY-DATA-TYPE
# -> array


def test_the_record_and_the_array_no_longer_chain_skip_behind_an_unmapped_field(mapped):
    """A record field typed by an unmapped application primitive resolves, so neither record nor array skips.

    The chain-skip shape itself is exercised in ``unresolved_reference_roles.arxml``, not here.
    """
    data_types, _registry, skip_log = mapped
    record = _by_name(data_types, "WheelCornerStatus")
    assert isinstance(record, RecordDataType)
    assert [(f.name, f.type) for f in record.fields if f.name == "pressure"] == [("pressure", "PressureRatio")]
    assert isinstance(_by_name(data_types, "WheelCornerStatusArray"), ArrayDataType)

    assert _skip_reason(skip_log, "/Acme/Chassis/ApplicationTypes/WheelCornerStatus") is None
    assert _skip_reason(skip_log, "/Acme/Chassis/ApplicationTypes/WheelCornerStatusArray") is None


def test_record_maps_to_record_with_fields_and_array_maps_to_array_with_size():
    """Rows 6-7's clean success path, built inline.

    The fields are application-typed: a record element's type is an ApplicationDataType.
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
    for app_name in ("Flag", "Raw"):
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path=f"/Pkg/AppTypes/{app_name}",
                name=app_name,
                compu_method_ref=None,
                description="",
                category="VALUE",
            )
        )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/StatusRecord",
            name="StatusRecord",
            elements=[
                RecordElementInfo(name="flag", type_ref="/Pkg/AppTypes/Flag"),
                RecordElementInfo(name="raw", type_ref="/Pkg/AppTypes/Raw"),
            ],
            description="Two-field status record.",
        )
    )
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/AppTypes/StatusRecordArray",
            name="StatusRecordArray",
            element_type_ref="/Pkg/AppTypes/StatusRecord",
            max_number_of_elements=4,
            description="Fixed-size array of status records.",
        )
    )

    data_types, _registry, skip_log = map_data_types(model, elements=[])

    record = _by_name(data_types, "StatusRecord")
    assert isinstance(record, RecordDataType)
    assert [(f.name, f.type) for f in record.fields] == [
        ("flag", "Flag"),
        ("raw", "Raw"),
    ]
    assert record.description == "Two-field status record."

    array = _by_name(data_types, "StatusRecordArray")
    assert isinstance(array, ArrayDataType)
    assert array.element_type == "StatusRecord"
    assert array.length == 4
    assert len(skip_log) == 0


# Row 8: non-TEXTTABLE COMPU-METHOD (and an unconsumed TEXTTABLE one) -> skip


def test_non_texttable_compu_method_is_skipped(mapped):
    _data_types, _registry, skip_log = mapped
    reason = _skip_reason(skip_log, "/Acme/Chassis/CompuMethods/PressureRatio_CompuMethod")
    assert reason is not None
    assert "non-texttable" in reason.lower() or "linear" in reason.lower()


def test_the_compu_method_of_a_twinned_texttable_type_is_consumed(mapped):
    """A twinned TEXTTABLE type's compu method is consumed by its enum, not reported unused.

    Guards an ordering defect: the twin collapse ran before the TEXTTABLE check and the literals were lost.
    """
    _data_types, _registry, skip_log = mapped
    assert _skip_reason(skip_log, "/Acme/Chassis/CompuMethods/BrakeModeEnum_CompuMethod") is None


def test_texttable_compu_method_consumed_by_an_enum_is_not_skipped(mapped):
    _data_types, _registry, skip_log = mapped
    reason = _skip_reason(skip_log, "/Acme/Chassis/CompuMethods/DoorStateEnum_CompuMethod")
    assert reason is None


# Type-name normalization policy


class TestNameNormalization:
    def test_a_twinned_texttable_type_keeps_both_elements(self, mapped):
        """A TEXTTABLE application primitive and its implementation twin are both written, under one name.

        The two say different things (literals, memory layout), so rule 1 does not collapse them.
        """
        data_types, registry, _skip_log = mapped
        assert isinstance(_in_layer(data_types, "application_types", "BrakeModeEnum"), EnumDataType)
        assert isinstance(_in_layer(data_types, "implementation_types", "BrakeModeEnum"), ValueDataType)

        assert registry.resolve("/Acme/Chassis/ApplicationTypes/BrakeModeEnum") == "BrakeModeEnum"
        assert registry.layer_of("/Acme/Chassis/ApplicationTypes/BrakeModeEnum") == "application_types"
        assert registry.resolve("/Acme/Chassis/Types/BrakeModeEnum") == "BrakeModeEnum"
        assert registry.layer_of("/Acme/Chassis/Types/BrakeModeEnum") == "implementation_types"

    def test_the_kept_pair_is_reported_neither_as_a_rename_nor_as_a_reduction(self, mapped):
        """Two declarations became two elements under the export's name: no reduction, no rename."""
        _data_types, registry, _skip_log = mapped
        assert registry.reductions == []
        assert registry.renames == []
        assert registry.source_names == {}

    def test_a_bare_twin_still_collapses(self) -> None:
        """A bare app/impl twin still collapses (on the corpus fixture; the shared one has no bare pair)."""
        data_types, registry, _skip_log = map_data_types(read_shape_model("bare_application_twin.arxml"), elements=[])

        assert _names(data_types) == {"Word", "Level"}
        assert isinstance(_by_name(data_types, "Level"), ValueDataType)
        assert registry.resolve("/Acme/AppTypes/Level") == "Level"
        assert registry.resolve("/Acme/ImplTypes/Level") == "Level"
        assert registry.reductions == ["/Acme/AppTypes/Level + /Acme/ImplTypes/Level -> Level (app/impl twin collapse)"]

    def test_an_unrelated_same_name_pair_across_layers_keeps_the_name_in_both(self, mapped):
        """Rule 2's ``_app`` fires only inside one layer, so a same-name pair across layers is not renamed."""
        data_types, registry, _skip_log = mapped
        impl_derived = _in_layer(data_types, "implementation_types", "DoorStateEnum")
        assert isinstance(impl_derived, ValueDataType)
        app_derived = _in_layer(data_types, "application_types", "DoorStateEnum")
        assert isinstance(app_derived, EnumDataType)
        assert "DoorStateEnum_app" not in _names(data_types)

        assert registry.resolve("/Acme/Chassis/Types/DoorStateEnum") == "DoorStateEnum"
        assert registry.resolve("/Acme/Chassis/ApplicationTypes/DoorStateEnum") == "DoorStateEnum"

    def test_no_suffix_rename_is_reported_for_a_pair_across_layers(self, mapped):
        _data_types, registry, _skip_log = mapped
        assert not [line for line in registry.renames if "DoorStateEnum" in line]


class TestEveryReservedNameStaysWithinTheIdentifierLimit:
    """Every candidate rule 2 mints is capped at the identifier limit.

    A ``SHORT-NAME`` is legal at source, so only what this package appends can overflow it; the cap lives on
    :func:`fit_identifier` and every minting site routes through it.
    """

    def test_the_suffix_survives_and_the_stem_is_what_loses_characters(self):
        capped = fit_identifier("A" * 200, "_app")

        assert len(capped) == MAX_IDENTIFIER_LENGTH
        assert capped.endswith("_app"), "a truncation that ate the rename token would stop naming the policy"
        assert capped == "A" * (MAX_IDENTIFIER_LENGTH - 4) + "_app"

    def test_a_short_name_is_returned_untouched(self):
        assert fit_identifier("Level", "_app") == "Level_app"
        assert fit_identifier("Level") == "Level"

    def test_a_suffix_longer_than_the_whole_budget_still_returns_a_legal_name(self):
        """A suffix wider than the whole budget splits it: the stem keeps at most half, the suffix the rest.

        Reachable through ``_reserve()``'s ``_app_{snake_case(package)}``. The suffix must never vanish, or the fallback
        collapses onto its own stem.
        """
        result = fit_identifier("A" * 200, "_" * (MAX_IDENTIFIER_LENGTH + 2))

        assert len(result) == MAX_IDENTIFIER_LENGTH
        assert check_identifier(result) is None
        assert result == "A" * (MAX_IDENTIFIER_LENGTH // 2) + "_" * (MAX_IDENTIFIER_LENGTH // 2)

    def test_a_short_stem_keeps_all_of_itself_when_the_suffix_overflows(self):
        """The stem's half-budget is a cap, not an allotment: a short stem gives up nothing."""
        result = fit_identifier("Level", "_" * (MAX_IDENTIFIER_LENGTH + 2))

        assert result == "Level" + "_" * (MAX_IDENTIFIER_LENGTH - len("Level"))

    def test_an_over_long_bare_name_is_handed_back_uncapped(self):
        """An over-long declared name passes through uncapped for ``reconcile.identifier_violations()`` to refuse.

        Capping it would import the type under a name the importer invented.
        """
        registry = PathRegistry()
        long_name = "B" * 200

        emitted = registry.register("/Pkg/Types/" + long_name, long_name, "implementation_types")

        assert emitted == long_name
        assert check_identifier(emitted) == REASON_LENGTH
        assert registry.renames == [], "nothing was renamed, so nothing may be reported as one"
        assert registry.source_names == {}

    def test_the_app_fallback_of_an_over_long_name_still_conforms(self):
        registry = PathRegistry()
        long_name = "C" * 200
        # One layer, so the two collide (rule 2 is per layer).
        registry.register("/A/Types/" + long_name, long_name, "implementation_types")

        second = registry.register("/B/Types/" + long_name, long_name, "implementation_types")

        assert second.endswith("_app")
        assert check_identifier(second) is None

    def test_the_package_fallback_of_an_over_long_name_still_conforms(self):
        registry = PathRegistry()
        long_name = "D" * 200
        # One layer, so all three collide (rule 2 is per layer).
        registry.register("/A/Types/" + long_name, long_name, "application_types")
        registry.register("/B/Types/" + long_name, long_name, "application_types")

        third = registry.register("/C/ApplicationTypes/" + long_name, long_name, "application_types")

        assert third.endswith("_app_application_types")
        assert check_identifier(third) is None


class TestAThreeWayCollisionUnderALongPackage:
    """Rule 2's third candidate when type and package ``SHORT-NAME`` are both at the 128-character maximum.

    Guards a defect where truncation handed back the bare name and a legal export aborted with ``NameCollisionError``.
    """

    _NAME = "A" + "b" * (MAX_IDENTIFIER_LENGTH - 1)
    _PACKAGE = "P" + "q" * (MAX_IDENTIFIER_LENGTH - 1)
    #: All three in one layer, so they collide (rule 2 is per layer).
    _LAYER = "implementation_types"

    @pytest.fixture
    def registered(self) -> tuple[PathRegistry, str, str, str]:
        registry = PathRegistry()
        first = registry.register(f"/Acme/Pkg1/{self._NAME}", self._NAME, self._LAYER)
        second = registry.register(f"/Acme/Pkg2/{self._NAME}", self._NAME, self._LAYER)
        third = registry.register(f"/Acme/{self._PACKAGE}/{self._NAME}", self._NAME, self._LAYER)
        return registry, first, second, third

    def test_the_package_fallback_is_distinct_from_both_earlier_candidates(self, registered) -> None:
        _registry, first, second, third = registered

        assert third != first, "the fallback collapsed onto the bare name it exists to replace"
        assert third != second
        assert first == self._NAME

    def test_the_package_fallback_conforms(self, registered) -> None:
        _registry, _first, _second, third = registered

        assert len(third) <= MAX_IDENTIFIER_LENGTH
        assert check_identifier(third) is None

    def test_the_fallback_still_reads_as_a_rename_of_that_name(self, registered) -> None:
        """Both halves survive the split: the element's own name and the rename token."""
        _registry, _first, _second, third = registered

        assert third.startswith("Ab")
        assert f"{RENAME_SUFFIX}_" in third

    def test_all_three_paths_resolve_to_their_own_name(self, registered) -> None:
        registry, first, second, third = registered

        assert registry.resolve(f"/Acme/Pkg1/{self._NAME}") == first
        assert registry.resolve(f"/Acme/Pkg2/{self._NAME}") == second
        assert registry.resolve(f"/Acme/{self._PACKAGE}/{self._NAME}") == third
        assert len({first, second, third}) == 3

    def test_the_rename_is_reported_under_the_declared_name(self, registered) -> None:
        registry, _first, _second, third = registered

        assert f"/Acme/{self._PACKAGE}/{self._NAME} -> {third}" in registry.renames
        assert registry.source_names[(self._LAYER, third)] == self._NAME


class TestEveryBareNameCollisionResolves:
    """``_reserve()``'s invariant: every name handed out is a legal ``Identifier``, at most 128 characters, distinct.

    A bare-name collision between distinct paths is therefore never refused. Two candidates can collapse onto one string
    at the truncation boundary, so the stems are derived from the packages they defeat, and
    :meth:`test_the_mirroring_stem_really_mirrors` asserts the boundary is actually hit.
    """

    @staticmethod
    def _snake(package: str) -> str:
        return snake_case(f"/Root/{package}/Element")

    @classmethod
    def _mirroring_stem(cls, package: str) -> str:
        """A legal 128-character stem whose ``_app`` candidate IS its *package* one.

        The two are equal exactly when the ``_app_{snake}`` candidate already ends in ``_app``, hence the packages this
        is called with.
        """
        snake = cls._snake(package)
        head = MAX_IDENTIFIER_LENGTH - len(f"{RENAME_SUFFIX}_{snake}")
        mirrored = "A" * head + f"{RENAME_SUFFIX}_{snake}"
        return mirrored[: MAX_IDENTIFIER_LENGTH - len(RENAME_SUFFIX)] + "zzzz"

    @staticmethod
    def _register_all(stem: str, packages: list[str]) -> tuple[PathRegistry, list[str]]:
        """Register *stem* once under each package, all at distinct paths.

        The disambiguating segment is a grandparent, so ``snake_case`` still sees the package under test.
        """
        registry = PathRegistry()
        emitted = []
        for index, package in enumerate(packages):
            path = f"/Root/Lvl{index}/{package}/{stem}"
            # One layer, so every registration collides (rule 2 is per layer).
            name = registry.register(path, stem, "implementation_types")
            assert registry.resolve(path) == name
            emitted.append(name)
        return registry, emitted

    @staticmethod
    def _assert_invariant(stem: str, emitted: list[str]) -> None:
        assert emitted[0] == stem, "the declared name is claimed first, and uncapped"
        assert len(set(emitted)) == len(emitted), f"two paths collapsed onto one name: {emitted}"
        for name in emitted[1:]:
            assert check_identifier(name) is None, name
            assert len(name) <= MAX_IDENTIFIER_LENGTH, name

    _MIRRORING_PACKAGES = ["XApp", "MyApp", "SensorApp", "A"]

    def test_the_mirroring_stem_really_mirrors(self) -> None:
        """The mirroring construction is verified, not assumed."""
        for package in self._MIRRORING_PACKAGES[:-1]:
            stem = self._mirroring_stem(package)
            snake = self._snake(package)

            assert len(stem) == MAX_IDENTIFIER_LENGTH
            assert check_identifier(stem) is None
            assert fit_identifier(stem, RENAME_SUFFIX) == fit_identifier(stem, f"{RENAME_SUFFIX}_{snake}"), (
                f"the truncation boundary moved; this no longer reproduces the collapse for {package!r}"
            )

    def test_a_three_way_collision_at_the_mirroring_boundary_resolves(self) -> None:
        """Three legal declarations whose second and third candidates are one string resolve."""
        for package in self._MIRRORING_PACKAGES[:-1]:
            stem = self._mirroring_stem(package)

            _registry, emitted = self._register_all(stem, ["Pkg1", "Pkg2", package])

            self._assert_invariant(stem, emitted)

    def test_the_invariant_holds_around_the_whole_truncation_boundary(self) -> None:
        """The invariant holds for stem lengths on both sides of every budget the candidates use."""
        for package in self._MIRRORING_PACKAGES:
            mirroring = self._mirroring_stem(package)
            for length in (1, 8, 118, 120, 121, 122, 123, 124, 127, MAX_IDENTIFIER_LENGTH):
                stem = mirroring[:length] if length <= len(mirroring) else mirroring
                if check_identifier(stem) is not None:
                    continue

                _registry, emitted = self._register_all(stem, [package] * 6)

                self._assert_invariant(stem, emitted)

    def test_a_deep_collision_never_refuses_and_never_repeats(self) -> None:
        """A deep collision terminates and stays distinct across the counter's digit boundaries (9 -> 10, 99 -> 100)."""
        stem = self._mirroring_stem("XApp")

        _registry, emitted = self._register_all(stem, ["XApp"] * 200)

        self._assert_invariant(stem, emitted)

    def test_every_rename_is_reported_under_the_declared_name(self) -> None:
        """However deep the collision goes, the report still points at the export."""
        stem = self._mirroring_stem("XApp")

        registry, emitted = self._register_all(stem, ["Pkg1", "Pkg2", "XApp", "XApp"])

        assert len(registry.renames) == len(emitted) - 1
        for name in emitted[1:]:
            assert registry.source_names[("implementation_types", name)] == stem

    def test_a_duplicate_path_is_still_refused(self) -> None:
        """Rule 3: one path declared twice is still refused; only a name collision between paths resolves."""
        registry = PathRegistry()
        registry.register("/Acme/Foo", "Foo", "implementation_types")

        with pytest.raises(NameCollisionError):
            registry.register("/Acme/Foo", "Foo", "implementation_types")


# Error scenarios


class TestBlankBaseTypeEncoding:
    """A blank base type encoding skips that one element and reports it; the import never crashes.

    Guards a defect where a whitespace-only ``<BASE-TYPE-ENCODING>`` raised ``pydantic.ValidationError`` out of the run.
    """

    def _model_with(self, encoding: str) -> ImportModel:
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/uint16",
                name="uint16",
                size=16,
                encoding=encoding,
                native_declaration="uint16",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/uint16_impl",
                name="uint16_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/uint16",
                impl_type_ref=None,
                description="",
            )
        )
        return model

    @pytest.mark.parametrize("encoding", ["", " ", "\n                  ", "\t"])
    def test_a_blank_encoding_is_skip_logged_not_raised(self, encoding: str):
        data_types, _registry, skip_log = map_data_types(self._model_with(encoding), elements=[])

        assert data_types == []
        reason = _skip_reason(skip_log, "/Pkg/Types/uint16")
        assert reason is not None
        assert "BASE-TYPE-ENCODING" in reason

    def test_the_value_branch_skips_the_impl_twin_rather_than_copying_the_blank(self):
        """The ``resolve_impl_type`` VALUE branch is guarded upstream: row 1 never registered the skipped base type."""
        _data_types, _registry, skip_log = map_data_types(self._model_with("   "), elements=[])

        reason = _skip_reason(skip_log, "/Pkg/Types/uint16_impl")
        assert reason is not None
        assert "base type was skipped" in reason

    def test_a_real_encoding_still_maps(self):
        """The guard-fires control: the parametrization above is not passing vacuously."""
        data_types, _registry, _skip_log = map_data_types(self._model_with("NONE"), elements=[])

        assert _by_name(data_types, "uint16").encoding == "NONE"

    def test_a_whitespace_encoding_in_a_real_arxml_file_does_not_abort_the_import(self, tmp_path):
        """End to end from XML, since the trigger is a serialization habit, not a mapper input."""
        source = DATA_TYPES.read_text(encoding="utf-8").replace(
            "<BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>",
            "<BASE-TYPE-ENCODING>\n                  </BASE-TYPE-ENCODING>",
            1,
        )
        mutated = tmp_path / "blank_encoding.arxml"
        mutated.write_text(source, encoding="utf-8")

        data_types, _registry, skip_log = map_data_types(read_arxml(str(mutated)), elements=[])

        assert data_types, "the other types must still import"
        assert any("BASE-TYPE-ENCODING" in entry.reason for entry in skip_log)


class TestErrorScenarios:
    def _dangling_base_type_ref(self) -> ImportModel:
        model = ImportModel(source_path="inline-test")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/Dangling_impl",
                name="Dangling_impl",
                category="VALUE",
                base_type_ref="/Pkg/Types/DoesNotExist",
                impl_type_ref=None,
                description="",
            )
        )
        return model

    def test_unresolved_base_type_ref_is_skipped_with_reason(self):
        """With no unresolved-reference policy (``import component``'s case) the type is skipped with its reason."""
        data_types, _registry, skip_log = map_data_types(self._dangling_base_type_ref(), elements=[])

        assert data_types == []
        reason = _skip_reason(skip_log, "/Pkg/Types/Dangling_impl")
        assert reason is not None
        assert "unresolved reference" in reason.lower()
        assert "/Pkg/Types/DoesNotExist" in reason

    def test_with_the_unresolved_reference_policy_the_type_is_kept_with_the_path(self):
        """With the unresolved-reference policy the type is kept with the export's own path in ``base``."""
        from swcraft_tools.arxml_import.unresolved import CAUSE_ABSENT, UnresolvedReferences

        model = self._dangling_base_type_ref()
        unresolved = UnresolvedReferences(model)

        data_types, _registry, skip_log = map_data_types(model, unresolved=unresolved, elements=[])

        assert [(dt.name, dt.kind, dt.base) for dt in data_types] == [
            ("Dangling_impl", "value_type", "/Pkg/Types/DoesNotExist")
        ]
        assert _skip_reason(skip_log, "/Pkg/Types/Dangling_impl") is None
        records = unresolved.references
        assert [(record.target, record.cause) for record in records] == [("/Pkg/Types/DoesNotExist", CAUSE_ABSENT)]
        assert [referrer.field for referrer in records[0].referrers] == ["implementation_types.value_type.base"]

    def test_cyclic_type_reference_chain_is_skipped_not_infinite_looped(self):
        model = ImportModel(source_path="inline-test")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/A_impl",
                name="A_impl",
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref="/Pkg/Types/B_impl",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Pkg/Types/B_impl",
                name="B_impl",
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref="/Pkg/Types/A_impl",
                description="",
            )
        )

        data_types, _registry, skip_log = map_data_types(model, elements=[])

        assert data_types == []
        assert any("cyclic" in entry.reason.lower() for entry in skip_log)

    def test_identical_full_path_registered_twice_raises_name_collision_error(self):
        """Rule 3: two identical full paths raise ``NameCollisionError``.

        Forced at the mapper: ``read_arxml()`` keys its index by path, so an XML duplicate overwrites one entry.
        """
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/Dup",
                name="Dup",
                size=8,
                encoding="NONE",
                native_declaration="",
                description="first declaration",
            )
        )
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/Dup",
                name="Dup",
                size=16,
                encoding="NONE",
                native_declaration="",
                description="conflicting second declaration at the same path",
            )
        )

        with pytest.raises(NameCollisionError):
            map_data_types(model, elements=[])

    def test_twin_mapping_set_entry_pointing_at_non_impl_path_does_not_crash(self):
        """A mapping-set entry naming a same-name SW-BASE-TYPE path instead of an implementation type does not crash.

        Guards a defect where the twin-collapse guard accepted any registered path and raised ``KeyError``.
        """
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Pkg/Types/Foo",
                name="Foo",
                size=8,
                encoding="NONE",
                native_declaration="uint8",
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Foo",
                name="Foo",
                compu_method_ref=None,
                description="",
            )
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Pkg/MappingSet",
                name="MappingSet",
                maps=[
                    DataTypeMapInfo(
                        application_ref="/Pkg/AppTypes/Foo",
                        # Malformed: the SW-BASE-TYPE path, whose bare name still matches the app type's,
                        # so the twin-collapse name check fires.
                        implementation_ref="/Pkg/Types/Foo",
                    )
                ],
            )
        )

        # Must not raise KeyError. The type becomes row 5's `application_type`
        # and keeps the bare name, because the base type is another layer.
        data_types, registry, skip_log = map_data_types(model, elements=[])

        assert not any(
            isinstance(entry, Exception) for entry in skip_log
        )  # sanity: skip_log holds SkipEntry, never raised exceptions
        assert _keys(data_types) == {("base_types", "Foo"), ("application_types", "Foo")}
        assert isinstance(_in_layer(data_types, "application_types", "Foo"), ApplicationDataType)
        assert registry.renames == []
        assert registry.reductions == []  # never treated as a twin collapse

    def test_both_named_variants_taken_falls_through_to_the_counter(self):
        """With the bare name and both named variants taken, the counter form (``free_name``'s) resolves it."""
        registry = PathRegistry()
        # One layer, so every registration collides (rule 2 is per layer).
        layer = "implementation_types"
        registry.register("/A/X", "X", layer)
        registry.register("/B/X_app", "X_app", layer)
        registry.register("/Somewhere/X_app_pkg", "X_app_pkg", layer)

        # /Pkg/X's bare name "X" collides -> "X_app" collides -> the
        # snake-cased-parent fallback ("X_app_pkg", parent="Pkg") also collides.
        emitted = registry.register("/Pkg/X", "X", layer)

        assert emitted == f"X_2{RENAME_SUFFIX}"
        assert check_identifier(emitted) is None
        assert registry.source_names[(layer, emitted)] == "X"


# Every mapped element is a real, validated schema.py Pydantic model


def test_every_mapped_data_type_is_a_schema_pydantic_instance(mapped):
    data_types, _registry, _skip_log = mapped
    allowed = (
        BaseDataType,
        ValueDataType,
        ReferenceDataType,
        EnumDataType,
        RecordDataType,
        ArrayDataType,
        ApplicationDataType,
    )
    assert data_types, "expected at least one mapped data type from the fixture"
    for dt in data_types:
        assert isinstance(dt, allowed)


def test_fixture_produces_exactly_the_expected_eleven_data_types(mapped):
    """The layer, the name and the kind of each of the fixture's eleven types, keyed ``(layer, name)``."""
    data_types, _registry, _skip_log = mapped
    assert len(data_types) == 11
    assert {(data_type_layer(dt), dt.name): dt.kind for dt in data_types} == {
        ("base_types", "uint8"): "base_type",
        ("base_types", "uint16"): "base_type",
        ("implementation_types", "uint16_impl"): "value_type",
        ("implementation_types", "WheelTravelRange_impl"): "reference_type",
        ("application_types", "BrakeModeEnum"): "enum",
        ("implementation_types", "BrakeModeEnum"): "value_type",
        ("implementation_types", "DoorStateEnum"): "value_type",
        ("application_types", "DoorStateEnum"): "enum",
        ("application_types", "PressureRatio"): "application_type",
        ("application_types", "WheelCornerStatus"): "record",
        ("application_types", "WheelCornerStatusArray"): "array",
    }


# A non-positive BASE-TYPE-SIZE or MAX-NUMBER-OF-ELEMENTS skips and reports the
# element instead of raising `ValidationError` out of the whole import.


@pytest.mark.parametrize("bad_size", [0, -8])
def test_sw_base_type_non_positive_size_is_skipped_not_crashed(bad_size):
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/BadSize",
            name="BadSize",
            size=bad_size,
            encoding="NONE",
            native_declaration="",
            description="",
        )
    )

    # Must not raise pydantic.ValidationError.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert data_types == []
    reason = _skip_reason(skip_log, "/Pkg/Types/BadSize")
    assert reason is not None
    assert "size" in reason.lower()


@pytest.mark.parametrize("bad_length", [0, -1])
def test_app_array_non_positive_max_number_of_elements_is_skipped_not_crashed(bad_length):
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
    # An array element's type is an ApplicationDataType; an implementation type
    # there is refused, which is not this test's subject.
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
            path="/Pkg/AppTypes/BadArray",
            name="BadArray",
            element_type_ref="/Pkg/AppTypes/Item",
            max_number_of_elements=bad_length,
            description="",
        )
    )

    # Must not raise pydantic.ValidationError.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert "BadArray" not in _names(data_types)
    reason = _skip_reason(skip_log, "/Pkg/AppTypes/BadArray")
    assert reason is not None
    assert "max-number-of-elements" in reason.lower()


# A blank/missing SHORT-NAME defaults to "" in the readers, but schema.py
# requires a non-blank `name`: the element is skipped, not a crash.


def test_sw_base_type_blank_name_is_skipped_not_crashed():
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/",
            name="  ",
            size=8,
            encoding="NONE",
            native_declaration="",
            description="",
        )
    )

    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert data_types == []
    reason = _skip_reason(skip_log, "/Pkg/Types/")
    assert reason is not None
    assert "blank" in reason.lower() or "missing" in reason.lower()


def test_impl_data_type_blank_name_is_skipped_not_crashed():
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
            path="/Pkg/Types/Blank_impl",
            name="   ",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )

    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert _names(data_types) == {"uint8"}
    reason = _skip_reason(skip_log, "/Pkg/Types/Blank_impl")
    assert reason is not None
    assert "blank" in reason.lower() or "missing" in reason.lower()


# A present-but-empty `<VT></VT>` COMPU-SCALE literal passes the reader's
# `literal is not None` check (findtext returns "") and must not reach
# `EnumLiteral`'s non-blank validator.


def test_texttable_enum_blank_literal_skips_whole_enum_not_crashed():
    model = ImportModel(source_path="inline-test")
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/BadEnum_CompuMethod",
            name="BadEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[
                CompuScaleInfo(value=0, literal="OK"),
                CompuScaleInfo(value=1, literal="   "),  # blank VT
            ],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/BadEnum",
            name="BadEnum",
            compu_method_ref="/Pkg/CompuMethods/BadEnum_CompuMethod",
            description="",
        )
    )

    # Must not raise pydantic.ValidationError.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    # The table is what cannot be imported, so the table is reported and the
    # type still lands as an application type with no literals: skipping it
    # would take every record, array and interface typed by it along.
    assert isinstance(_by_name(data_types, "BadEnum"), ApplicationDataType)
    reason = next(
        (entry.line for entry in _registry.unread_physics if entry.source_path == "/Pkg/AppTypes/BadEnum"),
        None,
    )
    assert reason is not None
    assert "imported without its literals" in reason
    assert "blank" in reason.lower() or "missing" in reason.lower()


# A blank/missing SHORT-NAME on an APPLICATION-RECORD-ELEMENT defaults to "" in
# the reader, but RecordField.name rejects it: the whole record is skipped, as
# for an unresolved field type.


def test_record_element_blank_name_skips_whole_record_not_crashed():
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
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/BadRecord",
            name="BadRecord",
            elements=[
                RecordElementInfo(name="   ", type_ref="/Pkg/Types/uint8"),
            ],
            description="",
        )
    )

    # Must not raise pydantic.ValidationError.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert "BadRecord" not in _names(data_types)
    reason = _skip_reason(skip_log, "/Pkg/AppTypes/BadRecord")
    assert reason is not None
    assert "blank" in reason.lower() or "missing" in reason.lower()
    assert "blank" in reason.lower() or "missing" in reason.lower()


# The writer rejects duplicate literal names/values and duplicate record field
# names at ARXML-generation time, so the import catches them first rather than
# report as imported what a later `generate arxml`/`validate` refuses.


def test_texttable_enum_duplicate_literal_name_skips_whole_enum_not_deferred_to_writer():
    model = ImportModel(source_path="inline-test")
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/DupNameEnum_CompuMethod",
            name="DupNameEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[
                CompuScaleInfo(value=0, literal="OK"),
                CompuScaleInfo(value=1, literal="OK"),
            ],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/DupNameEnum",
            name="DupNameEnum",
            compu_method_ref="/Pkg/CompuMethods/DupNameEnum_CompuMethod",
            description="",
        )
    )

    # Must not silently import an enum that the writer would later reject.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    # The table is what cannot be imported, so the table is reported and the
    # type still lands as an application type with no literals: skipping it
    # would take every record, array and interface typed by it along.
    assert isinstance(_by_name(data_types, "DupNameEnum"), ApplicationDataType)
    reason = next(
        (entry.line for entry in _registry.unread_physics if entry.source_path == "/Pkg/AppTypes/DupNameEnum"),
        None,
    )
    assert reason is not None
    assert "imported without its literals" in reason
    assert "duplicate" in reason.lower()
    assert "OK" in reason


def test_texttable_enum_duplicate_literal_value_skips_whole_enum_not_deferred_to_writer():
    model = ImportModel(source_path="inline-test")
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/DupValueEnum_CompuMethod",
            name="DupValueEnum_CompuMethod",
            category="TEXTTABLE",
            scales=[
                CompuScaleInfo(value=0, literal="OK"),
                CompuScaleInfo(value=0, literal="ERROR"),
            ],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/DupValueEnum",
            name="DupValueEnum",
            compu_method_ref="/Pkg/CompuMethods/DupValueEnum_CompuMethod",
            description="",
        )
    )

    data_types, _registry, skip_log = map_data_types(model, elements=[])

    # The table is what cannot be imported, so the table is reported and the
    # type still lands as an application type with no literals: skipping it
    # would take every record, array and interface typed by it along.
    assert isinstance(_by_name(data_types, "DupValueEnum"), ApplicationDataType)
    reason = next(
        (entry.line for entry in _registry.unread_physics if entry.source_path == "/Pkg/AppTypes/DupValueEnum"),
        None,
    )
    assert reason is not None
    assert "imported without its literals" in reason
    assert "duplicate" in reason.lower()


def test_record_duplicate_field_name_skips_whole_record_not_deferred_to_writer():
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
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/DupFieldRecord",
            name="DupFieldRecord",
            elements=[
                RecordElementInfo(name="flag", type_ref="/Pkg/Types/uint8"),
                RecordElementInfo(name="flag", type_ref="/Pkg/Types/uint8"),
            ],
            description="",
        )
    )

    # Must not silently import a record that the writer would later reject.
    data_types, _registry, skip_log = map_data_types(model, elements=[])

    assert "DupFieldRecord" not in _names(data_types)
    reason = _skip_reason(skip_log, "/Pkg/AppTypes/DupFieldRecord")
    assert reason is not None
    assert "duplicate" in reason.lower()
    assert "flag" in reason


# AUTOSAR 4.3.0 input


@pytest.fixture(scope="module")
def mapped_4_3_0():
    """``mapped``'s twin, read from the 4.3.0-labelled copy of the same fixture."""
    return map_data_types(read_arxml(str(DATA_TYPES_4_3_0)), elements=[])


class TestDataTypeMappingAt4_3_0:
    """The whole type-mapping policy is reached identically from a 4.3.0-labelled source.

    ``data_types_4_3_0.arxml`` differs from ``data_types.arxml`` in one token, so this asserts that the declared version
    does not reach the mapper. Does not reach a real 4.3.0 export's dialect.
    """

    def test_maps_identically_to_the_4_2_2_twin(self, mapped_4_3_0, mapped):
        data_types_4_3_0, registry_4_3_0, skip_log_4_3_0 = mapped_4_3_0
        data_types_4_2_2, registry_4_2_2, skip_log_4_2_2 = mapped

        # Pydantic models compare by value, so this is full content equality -- kinds, sizes,
        # encodings, literals, fields, descriptions and the emitted names alike.
        assert data_types_4_3_0 == data_types_4_2_2
        # The ARXML-path -> emitted-name resolution table, which is what every later layer
        # (interfaces, SWC ports) reads to resolve its own references.
        assert registry_4_3_0._path_to_name == registry_4_2_2._path_to_name
        assert [(e.path, e.tag, e.reason) for e in skip_log_4_3_0] == [
            (e.path, e.tag, e.reason) for e in skip_log_4_2_2
        ]

    def test_the_mapped_kinds_are_reached(self, mapped_4_3_0):
        """Spelled out, because an equality assertion is also satisfied by two empty results."""
        data_types, _registry, _skip_log = mapped_4_3_0
        assert isinstance(_by_name(data_types, "uint8"), BaseDataType)
        assert isinstance(_by_name(data_types, "uint16_impl"), ValueDataType)
        assert isinstance(_by_name(data_types, "WheelTravelRange_impl"), ReferenceDataType)
        assert isinstance(_in_layer(data_types, "application_types", "DoorStateEnum"), EnumDataType)

    def test_both_same_name_pairs_keep_the_name_in_two_layers(self, mapped_4_3_0):
        """Neither the kept twin nor the unrelated same-name pair is renamed: each keeps the name in its layer."""
        data_types, registry, _skip_log = mapped_4_3_0
        keys = _keys(data_types)
        assert {("application_types", "BrakeModeEnum"), ("implementation_types", "BrakeModeEnum")} <= keys
        assert {("application_types", "DoorStateEnum"), ("implementation_types", "DoorStateEnum")} <= keys
        assert {"BrakeModeEnum_impl", "BrakeModeEnum_app", "DoorStateEnum_app"}.isdisjoint(_names(data_types))
        assert registry.renames == []

    def test_the_skip_and_report_verdicts_are_reached(self, mapped_4_3_0):
        """Every deliberate skip in the fixture is still reported, by path.

        A reader that bailed on the version would log nothing, which the twin equality alone would not catch.
        """
        _data_types, _registry, skip_log = mapped_4_3_0
        # The one deliberate skip: `PressureRatio`'s LINEAR method, refused as a
        # conversion because its COMPU-DENOMINATOR is 100, not 1.
        assert sorted(entry.path for entry in skip_log) == [
            "/Acme/Chassis/CompuMethods/PressureRatio_CompuMethod",
        ]


# Rows 2 and 5 again: a TYPE_REFERENCE chain deeper than one step (a ranged
# alias `Amps_Ranged` on `Amps` on `Float32`), and an application type mapped
# onto a TYPE_REFERENCE implementation type.


def _float_chain_model(depth: int) -> ImportModel:
    """``Float32`` (VALUE) under *depth* TYPE_REFERENCE types: ``Amps``, ``Amps_Ranged``, and so on."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Base/float32",
            name="float32",
            size=32,
            encoding="IEEE754",
            native_declaration="float",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Float32",
            name="Float32",
            category="VALUE",
            base_type_ref="/Pkg/Base/float32",
            impl_type_ref=None,
            description="",
        )
    )
    names = ["Amps", "Amps_Ranged", "Amps_Ranged_Derived"][:depth]
    previous = "/Pkg/Types/Float32"
    for name in names:
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=f"/Pkg/Types/{name}",
                name=name,
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref=previous,
                description="",
            )
        )
        previous = f"/Pkg/Types/{name}"
    return model


class TestATypeReferenceChainIsWrittenOneLinkAtATime:
    """Every reference type names the implementation type its TYPE_REFERENCE names, however deep the chain.

    Nothing is flattened, so nothing is recorded.
    """

    def test_a_one_step_chain_over_a_value_type_names_that_value_type(self):
        data_types, _registry, _skip_log = map_data_types(_float_chain_model(1), elements=[])
        assert _by_name(data_types, "Amps") == ReferenceDataType(name="Amps", kind="reference_type", base="Float32")

    def test_a_two_step_chain_names_each_link(self):
        data_types, _registry, skip_log = map_data_types(_float_chain_model(2), elements=[])
        assert list(skip_log) == []
        derived = _by_name(data_types, "Amps_Ranged")
        assert derived == ReferenceDataType(name="Amps_Ranged", kind="reference_type", base="Amps")
        # The intermediate type keeps its own place, one step over the value type.
        assert _by_name(data_types, "Amps").base == "Float32"
        for reference in (dt for dt in data_types if isinstance(dt, ReferenceDataType)):
            assert isinstance(_by_name(data_types, reference.base), (ValueDataType, ReferenceDataType)), reference.name

    def test_a_three_step_chain_names_each_link(self):
        data_types, _registry, _skip_log = map_data_types(_float_chain_model(3), elements=[])
        chain = ["Float32", "Amps", "Amps_Ranged", "Amps_Ranged_Derived"]
        for below, name in zip(chain, chain[1:]):
            assert _by_name(data_types, name).base == below, name

    def test_an_application_type_mapped_onto_a_type_reference_is_written_beside_it(self):
        """The mapping relates two written elements; the chain under the implementation type is untouched."""
        model = _float_chain_model(1)
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/AppTypes/Current",
                name="Current",
                compu_method_ref=None,
                description="",
            )
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Pkg/Mapping/PkgMapping",
                name="PkgMapping",
                maps=[DataTypeMapInfo(application_ref="/Pkg/AppTypes/Current", implementation_ref="/Pkg/Types/Amps")],
            )
        )
        data_types, _registry, skip_log = map_data_types(model, elements=[])
        assert list(skip_log) == []
        assert _by_name(data_types, "Current") == ApplicationDataType(
            name="Current", kind="application_type", unit="", conversion=None, range=None, description=""
        )
        assert _by_name(data_types, "Amps") == ReferenceDataType(name="Amps", kind="reference_type", base="Float32")

    def test_the_fixture_exports_type_reference_names_its_value_type(self, mapped):
        data_types, _registry, _skip_log = mapped
        references = [dt for dt in data_types if isinstance(dt, ReferenceDataType)]
        assert [(dt.name, dt.base) for dt in references] == [("WheelTravelRange_impl", "uint16_impl")]

    def test_the_report_has_no_flattening_section(self):
        """The report has no flattening field (a caller passing it is refused) and renders no such line."""
        from swcraft_tools.arxml_import.report import ImportReport

        kwargs = dict(
            source="export.arxml",
            packages=None,
            dry_run=True,
            imported_data_types=3,
            imported_interfaces=0,
            imported_swcs=0,
            imported_connectors=0,
            imported_compositions=0,
            skipped_by_tag={},
            skip_entries=[],
            reduction_lines=[],
            rename_lines=[],
            init_radix_lines=[],
            closure_lines=[],
        )
        with pytest.raises(TypeError, match="alias_chain_lines"):
            ImportReport(**kwargs, alias_chain_lines=["/Pkg/Types/Amps -> Float32 -> float32"])
        assert "flattened" not in ImportReport(**kwargs).render().lower()


# The importer writes what the source declared, one step, and records no
# flattening.


def _loop_model() -> ImportModel:
    """``_float_chain_model(1)`` with a two-type TYPE_REFERENCE cycle and a third type hanging off it."""
    model = _float_chain_model(1)
    for name, target in (("LoopA", "LoopB"), ("LoopB", "LoopA"), ("OnLoop", "LoopA")):
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=f"/Pkg/Types/{name}",
                name=name,
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref=f"/Pkg/Types/{target}",
                description="",
            )
        )
    return model


def _mapped_application_type(model: ImportModel, implementation_path: str) -> ImportModel:
    """`model` with an application primitive `Current` mapped onto `implementation_path`."""
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/Pkg/AppTypes/Current", name="Current", compu_method_ref=None, description="")
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/Pkg/Mapping/PkgMapping",
            name="PkgMapping",
            maps=[DataTypeMapInfo(application_ref="/Pkg/AppTypes/Current", implementation_ref=implementation_path)],
        )
    )
    return model


class TestTheImporterWritesWhatTheSourceDeclaredOneStep:
    """Rows 1, 3, 2 and 5 of the mapping table on the base/value/reference kinds, and the cycle guard."""

    def test_a_sw_base_type_is_a_base_type_and_nothing_else_carries_its_properties(self):
        data_types, _registry, skip_log = map_data_types(
            _mapped_application_type(_float_chain_model(3), "/Pkg/Types/Amps"), elements=[]
        )

        assert list(skip_log) == []
        base_types = [dt for dt in data_types if isinstance(dt, BaseDataType)]
        assert base_types == [
            BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754", native_declaration="float")
        ]
        copies = [
            dt.name
            for dt in data_types
            if dt not in base_types and {"size", "encoding", "native_declaration"} & set(type(dt).model_fields)
        ]
        assert copies == []

    def test_a_value_implementation_type_is_a_value_type_naming_its_base_type(self):
        data_types, _registry, skip_log = map_data_types(_float_chain_model(0), elements=[])

        assert list(skip_log) == []
        assert _by_name(data_types, "Float32") == ValueDataType(name="Float32", kind="value_type", base="float32")

    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_a_type_reference_chain_is_written_one_step_per_link(self, depth):
        data_types, _registry, skip_log = map_data_types(_float_chain_model(depth), elements=[])

        assert list(skip_log) == []
        chain = ["Float32", "Amps", "Amps_Ranged", "Amps_Ranged_Derived"][: depth + 1]
        for below, name in zip(chain, chain[1:]):
            assert _by_name(data_types, name) == ReferenceDataType(name=name, kind="reference_type", base=below)
        assert _by_name(data_types, "Float32") == ValueDataType(name="Float32", kind="value_type", base="float32")
        assert len(data_types) == depth + 2

    @pytest.mark.parametrize("target", ["Float32", "Amps"])
    def test_an_application_type_mapped_onto_an_implementation_type_is_written_beside_it(self, target):
        """Row 5: the application type is written as declared; its mapped implementation type keeps its own shape."""
        model = _mapped_application_type(_float_chain_model(1), f"/Pkg/Types/{target}")

        data_types, _registry, skip_log = map_data_types(model, elements=[])

        assert list(skip_log) == []
        assert _by_name(data_types, "Current") == ApplicationDataType(
            name="Current", kind="application_type", unit="", conversion=None, range=None, description=""
        )
        assert _by_name(data_types, target).name == target

    def test_no_flattening_is_recorded_anywhere(self):
        from dataclasses import fields

        from swcraft_tools.arxml_import.report import ImportReport

        _data_types, registry, _skip_log = map_data_types(_float_chain_model(3), elements=[])

        assert not hasattr(registry, "alias_chain_flattenings")
        assert not hasattr(registry, "declared_bases")
        assert not hasattr(registry, "record_alias_chain_flattening")
        assert "alias_chain_lines" not in {field.name for field in fields(ImportReport)}

    def test_a_cyclic_type_reference_chain_is_still_skipped_and_reported(self):
        data_types, _registry, skip_log = map_data_types(_loop_model(), elements=[])

        assert _names(data_types) == {"float32", "Float32", "Amps"}
        assert {"/Pkg/Types/LoopA", "/Pkg/Types/LoopB", "/Pkg/Types/OnLoop"} <= {entry.path for entry in skip_log}
        assert any(entry.reason == "cyclic type reference" for entry in skip_log)


# The source path of every emitted name -- the whole-export importer's write
# order for `data_types` (which of a case-only pair gets the plain file name is
# decided by the source path, the same on every platform)


class TestTheRegistryNamesTheSourcePathOfEveryEmittedType:
    def test_every_emitted_name_maps_to_the_path_it_was_first_registered_from(self, mapped_forms):
        data_types, registry, _skip_log, forms = mapped_forms

        assert registry.source_paths == {
            ("base_types", "uint8"): "/Acme/Chassis/Types/uint8",
            ("base_types", "uint16"): "/Acme/Chassis/Types/uint16",
            ("implementation_types", "uint16_impl"): "/Acme/Chassis/Types/uint16_impl",
            ("implementation_types", "WheelTravelRange_impl"): "/Acme/Chassis/Types/WheelTravelRange_impl",
            # The kept twin: two declarations, one name, two layers, two paths.
            ("application_types", "BrakeModeEnum"): "/Acme/Chassis/ApplicationTypes/BrakeModeEnum",
            ("implementation_types", "BrakeModeEnum"): "/Acme/Chassis/Types/BrakeModeEnum",
            # The unrelated same-name pair, likewise.
            ("implementation_types", "DoorStateEnum"): "/Acme/Chassis/Types/DoorStateEnum",
            ("application_types", "DoorStateEnum"): "/Acme/Chassis/ApplicationTypes/DoorStateEnum",
            ("application_types", "PressureRatio"): "/Acme/Chassis/ApplicationTypes/PressureRatio",
            ("application_types", "WheelCornerStatus"): "/Acme/Chassis/ApplicationTypes/WheelCornerStatus",
            ("application_types", "WheelCornerStatusArray"): "/Acme/Chassis/ApplicationTypes/WheelCornerStatusArray",
            # Each enumeration's TEXTTABLE, an element of its layer.
            ("compu_methods", "BrakeModeEnum_CompuMethod"): "/Acme/Chassis/CompuMethods/BrakeModeEnum_CompuMethod",
            ("compu_methods", "DoorStateEnum_CompuMethod"): "/Acme/Chassis/CompuMethods/DoorStateEnum_CompuMethod",
        }
        assert set(registry.source_paths) == _keys(data_types) | _keys(forms)

    def test_a_rename_carries_the_source_path_to_the_new_name(self):
        registry = PathRegistry()
        registry.register("/Pkg/Types/uint16_impl", "uint16_impl", "implementation_types")

        registry.rename("uint16_impl", "uint16_impl_app", "the writer reserves it", layer="implementation_types")

        assert registry.source_paths == {("implementation_types", "uint16_impl_app"): "/Pkg/Types/uint16_impl"}

    def test_the_answer_is_a_copy(self):
        registry = PathRegistry()
        registry.register("/Pkg/Types/uint8", "uint8", "base_types")

        registry.source_paths[("base_types", "uint8")] = "/Elsewhere/uint8"

        assert registry.source_paths == {("base_types", "uint8"): "/Pkg/Types/uint8"}


def _a_base_type_named_where_an_implementation_type_belongs() -> ImportModel:
    """One SW-BASE-TYPE named by every reference that must name an implementation or application type.

    A TYPE_REFERENCE, a record element, an array element and a data element, all malformed, and the one legal case: a
    mapping-set entry onto the base type.
    """
    base = "/Pkg/Base/float32"
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path=base, name="float32", size=32, encoding="IEEE754", native_declaration="float", description=""
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Ref",
            name="Ref",
            category="TYPE_REFERENCE",
            base_type_ref=None,
            impl_type_ref=base,
            description="",
        )
    )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/App/Rec", name="Rec", elements=[RecordElementInfo(name="x", type_ref=base)], description=""
        )
    )
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/App/Arr", name="Arr", element_type_ref=base, max_number_of_elements=2, description=""
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/Pkg/App/Mapped", name="Mapped", compu_method_ref=None, description="")
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/Pkg/Maps/Set",
            name="Set",
            maps=[DataTypeMapInfo(application_ref="/Pkg/App/Mapped", implementation_ref=base)],
        )
    )
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/If/IRaw",
            name="IRaw",
            description="",
            data_elements=[SRDataElementInfo(name="raw", type_ref=base)],
        )
    )
    return model


class TestABaseTypeNamedWhereAnImplementationTypeBelongsIsSkippedAndReported:
    """No SW-BASE-TYPE is written where ``check-model`` would refuse it.

    Each malformed shape is skipped with a reason; a mapping-set entry onto a base type is not skipped, and the
    application type it maps is written as declared.
    """

    def test_a_type_reference_naming_a_base_type_is_skipped(self):
        data_types, _registry, skip_log = map_data_types(
            _a_base_type_named_where_an_implementation_type_belongs(), elements=[]
        )

        assert "Ref" not in _names(data_types)
        assert "names the SW-BASE-TYPE /Pkg/Base/float32" in (_skip_reason(skip_log, "/Pkg/Types/Ref") or "")

    @pytest.mark.parametrize(("path", "name"), [("/Pkg/App/Rec", "Rec"), ("/Pkg/App/Arr", "Arr")])
    def test_a_record_or_array_element_typed_by_a_base_type_is_skipped(self, path, name):
        data_types, _registry, skip_log = map_data_types(
            _a_base_type_named_where_an_implementation_type_belongs(), elements=[]
        )

        assert name not in _names(data_types)
        assert "typed by the SW-BASE-TYPE /Pkg/Base/float32" in (_skip_reason(skip_log, path) or "")

    def test_a_data_element_typed_by_a_base_type_skips_its_interface(self):
        model = _a_base_type_named_where_an_implementation_type_belongs()
        forms: list = []
        data_types, registry, _skip_log = map_data_types(model, elements=forms)

        interfaces, skip_log = map_interfaces(model, registry, data_types, elements=forms)

        assert interfaces == []
        assert "references the SW-BASE-TYPE /Pkg/Base/float32 directly" in (
            _skip_reason(skip_log, "/Pkg/If/IRaw") or ""
        )

    def test_a_mapping_set_entry_onto_a_base_type_no_longer_decides_the_type_s_shape(self):
        """A mapping onto a base type does not shape the application primitive: it is written as declared.

        Whether a map may name it is ``rule:mapping-set.unknown-implementation``'s question.
        """
        data_types, _registry, _skip_log = map_data_types(
            _a_base_type_named_where_an_implementation_type_belongs(), elements=[]
        )

        mapped = _by_name(data_types, "Mapped")
        assert isinstance(mapped, ApplicationDataType)
        assert (mapped.unit, mapped.conversion, mapped.range) == ("", None, None)


# A SW-BASE-TYPE and an IMPLEMENTATION-DATA-TYPE of ONE SHORT-NAME


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """Scaffold a fresh project through the real ``swcraft init`` CLI path."""
    import sys

    from swcraft_tools.cli import main

    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _pair_model(
    impl_name: str = "Sample",
    *,
    third_declaration: bool = False,
    referrer: bool = False,
) -> ImportModel:
    """A base type named ``Sample`` and an implementation type over it.

    *impl_name* spells the implementation type's ``SHORT-NAME``; *third_declaration* adds an application record of the
    pair's name; *referrer* adds an interface typed by the implementation type.
    """
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Base/Sample",
            name="Sample",
            size=16,
            encoding="NONE",
            native_declaration="uint16",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path=f"/Pkg/Impl/{impl_name}",
            name=impl_name,
            category="VALUE",
            base_type_ref="/Pkg/Base/Sample",
            impl_type_ref=None,
            description="",
        )
    )
    if third_declaration:
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/App/Other",
                name="Other",
                compu_method_ref=None,
                description="",
                category="VALUE",
            )
        )
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Pkg/App/Sample",
                name="Sample",
                elements=[RecordElementInfo(name="raw", type_ref="/Pkg/App/Other")],
                description="",
            )
        )
    if referrer:
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Pkg/If/ISample",
                name="ISample",
                data_elements=[SRDataElementInfo(name="sample", type_ref=f"/Pkg/Impl/{impl_name}")],
                description="",
            )
        )
    return model


class TestTheSameNameBaseTypeAndImplementationTypeAreBothImported:
    """A ``SW-BASE-TYPE`` and an ``IMPLEMENTATION-DATA-TYPE`` of one ``SHORT-NAME`` are both imported.

    They are two layers, so both keep the declared name with no skip line and no rename.
    """

    _SHAPE = "same_name_base_and_implementation.arxml"
    _PAIR = {("base_types", "Sample"), ("implementation_types", "Sample")}

    def test_both_members_of_the_pair_are_imported_under_the_declared_name(self) -> None:
        data_types, _registry, _skip_log = map_data_types(read_shape_model(self._SHAPE), elements=[])

        assert _keys(data_types) == self._PAIR
        value_type = _in_layer(data_types, "implementation_types", "Sample")
        assert isinstance(value_type, ValueDataType)
        assert value_type.base == "Sample"
        assert isinstance(_in_layer(data_types, "base_types", "Sample"), BaseDataType)

    def test_no_member_is_skipped(self) -> None:
        _data_types, _registry, skip_log = map_data_types(read_shape_model(self._SHAPE), elements=[])

        assert list(skip_log) == []

    def test_nothing_is_reported_as_a_rename(self) -> None:
        _data_types, registry, _skip_log = map_data_types(read_shape_model(self._SHAPE), elements=[])

        assert registry.renames == []
        assert registry.source_names == {}

    def test_the_name_is_held_once_in_each_layer(self) -> None:
        _data_types, registry, _skip_log = map_data_types(read_shape_model(self._SHAPE), elements=[])

        assert registry.claimed_names("base_types") == frozenset({"Sample"})
        assert registry.claimed_names("implementation_types") == frozenset({"Sample"})
        assert registry.claimed_names("application_types") == frozenset()
        assert registry.resolve("/Acme/Chassis/BaseTypes/Sample") == "Sample"
        assert registry.layer_of("/Acme/Chassis/BaseTypes/Sample") == "base_types"
        assert registry.resolve("/Acme/Chassis/ImplTypes/Sample") == "Sample"
        assert registry.layer_of("/Acme/Chassis/ImplTypes/Sample") == "implementation_types"

    def test_an_unrelated_declaration_keeps_its_name_in_both_worlds(self) -> None:
        """Repairing the export at the source (renaming its implementation type) moves no third element."""
        paired, _registry, paired_skips = map_data_types(_pair_model(third_declaration=True), elements=[])
        repaired, _registry, repaired_skips = map_data_types(
            _pair_model("SampleImpl", third_declaration=True), elements=[]
        )

        record_and_field = {("application_types", "Sample"), ("application_types", "Other")}
        assert _keys(paired) == self._PAIR | record_and_field
        assert _keys(repaired) == {("base_types", "Sample"), ("implementation_types", "SampleImpl")} | record_and_field
        assert list(paired_skips) == list(repaired_skips) == []

    def test_a_pair_differing_only_in_case_is_imported_too(self) -> None:
        """A case-only pair is two identities, two files."""
        data_types, _registry, skip_log = map_data_types(_pair_model(impl_name="sample"), elements=[])

        assert _keys(data_types) == {("base_types", "Sample"), ("implementation_types", "sample")}
        assert list(skip_log) == []

    def test_a_record_of_the_base_types_name_is_not_renamed(self) -> None:
        """An application record named like a base type is another layer, so nothing is renamed."""
        model = _pair_model(impl_name="Raw", third_declaration=True)

        data_types, registry, skip_log = map_data_types(model, elements=[])

        assert _keys(data_types) == {
            ("base_types", "Sample"),
            ("implementation_types", "Raw"),
            ("application_types", "Sample"),
            ("application_types", "Other"),
        }
        assert list(skip_log) == []
        assert registry.renames == []

    def test_a_referrer_of_the_implementation_member_is_imported(self) -> None:
        """An interface typed by the implementation member resolves to it."""
        model = _pair_model(referrer=True)

        forms: list = []
        data_types, registry, _skip_log = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)

        assert [(i.name, [e.implementation_type for e in i.data_elements]) for i in interfaces] == [
            ("ISample", ["Sample"])
        ]
        assert list(iface_skips) == []

    def test_a_referrer_of_the_pair_is_neither_skipped_nor_unresolved(self, tmp_path: Path) -> None:
        """A referrer of a declared, imported path mints no *Unresolved references* record and no skip line."""
        from swcraft_tools.arxml_import import run_import

        source = tmp_path / "pair_and_referrer.arxml"
        source.write_text(_PAIR_COMPONENT_EXPORT, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        rendered = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True).render()

        assert "Unresolved references" not in rendered
        assert "unresolved reference" not in rendered
        assert "IMPORTED 2 data types" in rendered
        assert "IMPORTED 1 interfaces" in rendered
        assert "SKIPPED 0 elements" in rendered

    def test_the_rendered_report_names_no_refusal_and_no_rename(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_import import run_import

        project = _init_project(tmp_path)
        report = run_import(str(shape_path(self._SHAPE)), str(project / "swcraft.yaml"), dry_run=True)

        rendered = report.render()
        assert "IMPORTED 2 data types" in rendered
        assert "SKIPPED 0 elements" in rendered
        assert "duplicate type name 'Sample'" not in rendered
        assert "Sample_app" not in rendered
        assert report.rename_lines == []


class TestTheComponentImportPathAcceptsThePair:
    """``import component`` accepts the same-name pair, planning both members under the declared name.

    It is a surgical merge by name, so a rename would refuse it.
    """

    def test_the_mapping_renames_nothing(self) -> None:
        data_types, registry, skip_log = map_data_types(
            read_shape_model("same_name_base_and_implementation.arxml"), elements=[]
        )

        assert _keys(data_types) == {("base_types", "Sample"), ("implementation_types", "Sample")}
        assert list(skip_log) == []
        assert registry.renames == []

    def test_the_component_import_path_accepts_such_an_export(self, tmp_path: Path) -> None:
        from swcraft_tools.component_exchange.import_ import plan_component_import

        source = tmp_path / "pair_component.arxml"
        source.write_text(_PAIR_COMPONENT_EXPORT, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        plan = plan_component_import(str(source), str(project / "model"))

        assert plan.report.created_data_types == ["Sample", "Sample"]
        assert sorted(
            (target.layer, target.element.name) for target in plan.targets if target.layer.endswith("_types")
        ) == [("base_types", "Sample"), ("implementation_types", "Sample")]
        assert plan.report.skip_lines == []


#: One component, no composition, and the same-name pair -- the shape
#: ``import component`` accepts, built inline because the corpus fixture for
#: this pair is a whole-model export (``arxml_import_shapes_support``'s row).
_PAIR_COMPONENT_EXPORT = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>BaseTypes</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Sample</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>ImplTypes</SHORT-NAME>
          <ELEMENTS>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Sample</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/BaseTypes/Sample</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Interfaces</SHORT-NAME>
          <ELEMENTS>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISample</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>sample</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/ImplTypes/Sample</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SampleSwc</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pSample</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">\
/Acme/Interfaces/ISample</PROVIDED-INTERFACE-TREF>
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
