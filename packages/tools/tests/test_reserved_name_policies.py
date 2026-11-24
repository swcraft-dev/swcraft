"""The two first-party name policies must not mint what the other refuses.

SWCraft has two places that invent a data-type ``SHORT-NAME``:

* ``arxml_import/registry.py``'s ``_reserve()``, which resolves a bare-name collision
  by renaming the loser to ``{bare}_app`` (then ``{bare}_app_{lower_snake_package}``,
  then the unbounded counter form ``{bare}_{k}_app``). AUTOSAR scopes a short name to
  its package, so a bare-name collision is an ordinary import shape.
* ``writer/arxml_generation.py``, which generates ``{x}_CompuMethod`` for an enum,
  ``{swc}_Implementation`` for a component and ``{x}_Unit`` / ``{x}_Compu`` /
  ``{x}_Range`` for an application type declaring the matching field, and *reserves*
  each: a model declaring one is refused by ``check-model`` and by the writer. The
  writer owns no fixed name. A form the writer stops generating is dropped by every
  policy below: a guard refusing a name generation accepts is stricter than the writer.

The disjointness is asserted over the whole reserved set rather than one name,
because the same collision is one lowercase suffix away for every generated name.
The end-to-end cases feed a real import back to ``check-model`` and the writer; the
other import tests stop at the mapped objects.

The writer's identifier refusals belong here too (their reporting twin is
``test_validator_identifier.py``): four guards carry them, ``_check_connector_names``
among them.

The write path: refusing at generation time alone would let an MCP call report
success and leave a written file ``check-model`` refuses. The ``mutations.py``
classes therefore assert that the call is refused and that it wrote **nothing**.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from itertools import islice
from pathlib import Path

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    _root_composition,
    _write_manifest,
    arxml_layer_directories,
    make_empty_model_root,
    model_element_path,
    read_golden,
    write_model_element,
)
from swcraft_tools.arxml_import import (
    ImportedSwc,
    NameCollisionError,
    PathRegistry,
    free_name,
    identifier_violations,
    map_data_types,
    reconcile,
    reconcile_reserved_names,
    reserved_data_type_names,
    reserved_interface_names,
    reserved_swc_names,
    run_import,
)
from swcraft_tools.arxml_import.registry import RENAME_SUFFIX, snake_case, stub_name
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.auditor import AuditLog
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, REASON_LENGTH, REASON_PATTERN, check_identifier
from swcraft_tools.reader import read_data_types, read_layer, read_swc_model
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    EnumDataType,
    EnumLiteral,
    Port,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    Runnable,
    SRInterface,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.validator import (
    check_cross_layer_consistency,
    check_identifier_conformance,
    check_init_conformance,
    data_type_layer,
    suggest_identifier,
)
from swcraft_tools.writer import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    create_connector,
    create_cs_interface,
    create_data_type,
    create_interface,
    create_swc,
    generate_arxml,
)
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    COMPOSITION_SWC_TAG,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_SUFFIX,
    INIT_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    OPERATION_EVENT_PREFIX,
    RANGE_SUFFIX,
    READ_ACCESS_PREFIX,
    RESERVED_DATA_TYPE_RULES,
    TIMING_EVENT_PREFIX,
    TIMING_SUFFIX,
    UNIT_SUFFIX,
    WRITE_ACCESS_PREFIX,
    _check_connector_names,
    access_point_name,
    init_constant_name,
)
from swcraft_tools.writer.elements import write_element
from swcraft_tools.writer.mutations import ReservedNameError, create_hardware_element

#: The suffix of the ``{name}_impl`` twin the writer no longer generates. A literal
#: because this module still spells names ending in it, now ordinary names.
IMPL_SUFFIX = "_impl"

#: The same for the retired application *view*'s suffix and the fixed name of the
#: single ``DATA-TYPE-MAPPING-SET`` the writer no longer invents; the cases spelling
#: them record that each policy stopped refusing them.
APP_VIEW_SUFFIX = "_AppView"
MAPPING_SET_NAME = "DataTypeMappingSet"

#: The eighth generated form's joint budget: ``_`` and ``_Init`` cost six of the
#: 128, so an interface name and each of its data element names get **122
#: between them**.
JOINT_BUDGET = MAX_IDENTIFIER_LENGTH - len(INIT_CONSTANT_SUFFIX) - 1

#: The three access-point forms' joint budgets: a prefix and the separating ``_``
#: are what a port name and the member name it names share the 128 with, so
#: ``R_``/``W_`` leave **125** between them and ``Call_`` leaves **122**.
ACCESS_BUDGET = {
    prefix: MAX_IDENTIFIER_LENGTH - len(prefix) - 1
    for prefix in (READ_ACCESS_PREFIX, WRITE_ACCESS_PREFIX, CALL_POINT_PREFIX)
}

#: Which of a runnable's three lists each access-point form is derived from.
ACCESS_LIST_OF = {
    READ_ACCESS_PREFIX: "reads",
    WRITE_ACCESS_PREFIX: "writes",
    CALL_POINT_PREFIX: "calls",
}

DATA_TYPES_FIXTURE = Path(__file__).parent / "fixtures" / "arxml" / "data_types.arxml"

#: Every suffix the ARXML writer reserves. Kept as literals of the constants so a
#: future rename has to come through this module. The three application-type forms
#: and ``_Timing`` are reserved only beside a declared field or a timed component,
#: but unconditionally in the token space this tuple is about: what the importer
#: may never mint.
_WRITER_RESERVED_SUFFIXES = (
    COMPU_METHOD_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_SUFFIX,
    UNIT_SUFFIX,
    COMPU_SUFFIX,
    RANGE_SUFFIX,
    TIMING_SUFFIX,
)

#: **Empty**: the writer owns no fixed ``SHORT-NAME``. Kept, and quantified over
#: below, so it re-arms the day the writer mints a fixed name again; until then the
#: property's non-vacuous half is the suffix one.
_WRITER_RESERVED_NAMES: tuple[str, ...] = ()


class TestTheImporterCannotMintAReservedName:
    """The property, not the coincidence: no ``_reserve()`` output is reserved."""

    @pytest.mark.parametrize(
        "bare_name",
        [
            "Level",
            "DoorStateEnum",
            # names that already look like one of the writer's generated forms:
            # the kind-scoped one and one of the field-scoped three
            f"Speed{COMPU_METHOD_SUFFIX}",
            f"Speed{UNIT_SUFFIX}",
            # and one that looks like a form the writer has RETIRED, which the
            # registry may mint onto freely -- the disjointness must not depend
            # on a name nothing generates any more
            f"Speed{APP_VIEW_SUFFIX}",
            f"Speed{IMPL_SUFFIX}",
        ],
    )
    def test_no_rename_the_registry_mints_ends_in_a_reserved_suffix(self, bare_name: str) -> None:
        """``_reserve()`` has three rename shapes; none of them may be reserved.

        Every shape ends in the lowercase ``_app`` token and appends nothing but
        ``[a-z0-9_]``, while every writer-reserved suffix is a different word or
        differently cased, so the token spaces are disjoint by construction. The
        counter shapes are taken **from the generator**, so a fourth shape cannot be
        added without coming through this assertion. The fixed-name half is vacuous
        while :data:`_WRITER_RESERVED_NAMES` is empty.
        """
        # One layer throughout: rule 2 fires only for a collision inside one.
        registry = PathRegistry()
        registry.register("/P/A/" + bare_name, bare_name, "application_types")
        first = registry.register("/P/B/" + bare_name, bare_name, "application_types")
        second = registry.register("/P/C/" + bare_name, bare_name, "application_types")
        candidates = list(islice(registry._collision_candidates(bare_name, "/P/C/" + bare_name), 8))

        assert first == f"{bare_name}_app"
        assert second == f"{bare_name}_app_{snake_case('/P/C/' + bare_name)}"
        assert candidates[:3] == [first, second, f"{bare_name}_2_app"]
        for minted in (first, second, *candidates):
            assert minted not in _WRITER_RESERVED_NAMES
            for suffix in _WRITER_RESERVED_SUFFIXES:
                assert not minted.endswith(suffix), (
                    f"the importer mints {minted!r}, which the writer reserves as {suffix!r}"
                )

    @pytest.mark.parametrize("suffix", [UNIT_SUFFIX, COMPU_SUFFIX, RANGE_SUFFIX], ids=repr)
    def test_each_application_type_form_is_unmintable_by_the_registry(self, suffix: str) -> None:
        """The three application-type forms are unmintable by the registry.

        ``_reserve()``'s shapes are lowercase and decimal, so none can carry a
        capital. The case does not rely on the forms being field-scoped: the scope
        is the writer's to change and the token disjointness is not.
        """
        assert suffix != RENAME_SUFFIX
        assert suffix.lower() != suffix, (
            f"the application-type suffix {suffix!r} must contain a capital: a fully lowercase suffix "
            "could be reached by _reserve()'s '_app_{lower_snake_package}' and '_{k}_app' forms"
        )
        assert snake_case("/Acme/Chassis/Types/Level") == snake_case("/Acme/Chassis/Types/Level").lower()

        registry = PathRegistry()
        registry.register("/P/A/Level", "Level", "application_types")
        for minted in islice(registry._collision_candidates("Level", "/P/B/Level"), 8):
            assert not minted.endswith(suffix), minted

    def test_the_init_constant_suffix_is_unmintable_by_the_registry(self) -> None:
        """The eighth form's ``_Init`` is unmintable by the registry: no shape carries a capital.

        Phase A mints into the *data type* namespace and the constant is a sibling
        of the *interfaces*, so this is a disjointness the two namespaces would hide
        rather than one they enforce.
        """
        assert INIT_CONSTANT_SUFFIX.lower() != INIT_CONSTANT_SUFFIX, (
            "the init-constant suffix must contain a capital: a fully lowercase suffix could be "
            "reached by _reserve()'s '_app_{lower_snake_package}' and '_{k}_app' forms"
        )
        registry = PathRegistry()
        registry.register("/P/A/Level", "Level", "application_types")
        candidates = list(islice(registry._collision_candidates("Level", "/P/B/Level"), 8))
        for minted in candidates:
            assert not minted.endswith(INIT_CONSTANT_SUFFIX), minted

    def test_the_package_segment_a_mint_carries_is_sanitized(self) -> None:
        """The one place an *unchecked* string reaches an emitted name.

        Nothing in SWCraft checks a package ``SHORT-NAME``, and rule 2's third
        candidate carries its characters into a data-type ``name`` and filename:
        unsanitized, ``/Acme/My Pkg-2/Foo`` minted ``Foo_app_my _pkg-2``, which
        ``check-model`` and ``generate arxml`` refuse. Needs a three-way collision.
        """
        registry = PathRegistry()
        registry.register("/Acme/A/Foo", "Foo", "application_types")
        registry.register("/Acme/B/Foo", "Foo", "application_types")

        minted = registry.register("/Acme/My Pkg-2/Foo", "Foo", "application_types")

        assert check_identifier(minted) is None, minted
        assert minted == "Foo_app_my__pkg_2"
        claimed = registry.claimed_names("application_types")
        assert all(check_identifier(name) is None for name in claimed), claimed


def _import_into_fresh_project(tmp_path: Path, source: Path) -> tuple[Path, object]:
    """Run the real ``run_import()`` into an empty project; return (model root, report).

    Never ``map_data_types()`` alone: reserved-name reconciliation is **phase B**,
    a pass in the orchestrator, and must stay out of the mapping pipeline that
    ``import component`` reuses and that must stay rename-free.
    """
    make_empty_model_root(tmp_path)
    manifest = _write_manifest(tmp_path)
    report = run_import(str(source), str(manifest))
    return tmp_path / "model", report


class TestARhapsodyImportProducesABuildableProject:
    """A real import of ``data_types.arxml`` fed back to the validator and the writer.

    The fixture declares ``DoorStateEnum`` as both an implementation type and an
    unrelated application enum (two layers, so both keep the name), and an
    implementation type literally named ``uint16_impl`` beside the base type
    ``uint16`` (an ordinary name, renamed by nothing).
    """

    @staticmethod
    def _imported(tmp_path: Path) -> tuple[Path, object]:
        return _import_into_fresh_project(tmp_path, DATA_TYPES_FIXTURE)

    def test_the_fixture_really_exercises_the_same_name_pair_and_the_impl_name(self, tmp_path: Path) -> None:
        """Precondition: the fixture's same-name pair and its ``uint16_impl`` both reach the project."""
        model_root, _ = self._imported(tmp_path)

        # Keyed by directory, so the same-name pair is two entries rather than one
        # overwriting the other.
        names = {
            (directory.name, path.stem)
            for directory in (model_root / "data_types" / "implementation", model_root / "data_types" / "application")
            for path in directory.glob("*.yaml")
        }

        assert ("implementation", "DoorStateEnum") in names, "the implementation type keeps the bare name"
        assert ("application", "DoorStateEnum") in names, "the application type keeps the bare name too"
        assert not any(name == "DoorStateEnum_app" for _, name in names), "nothing is renamed"
        names = {name for _, name in names}
        # `uint16_impl` is an ordinary name: phase B leaves it alone.
        assert f"uint16{IMPL_SUFFIX}" in names
        assert f"uint16{IMPL_SUFFIX}{RENAME_SUFFIX}" not in names

    def test_the_source_declared_clash_survives_a_mapping_only_call(self, tmp_path: Path) -> None:
        """``map_data_types()``, which ``import component`` reuses, hands back the unreconciled name.

        Otherwise the rename policy has leaked into the shared pipeline and the
        fail-closed component import has become a renaming one.
        """
        data_types, _registry, _skips = map_data_types(read_arxml(str(DATA_TYPES_FIXTURE)), elements=[])

        assert f"uint16{IMPL_SUFFIX}" in {dt.name for dt in data_types}

    def test_the_imported_project_is_not_refused_by_check_model(self, tmp_path: Path) -> None:
        """The import's own output, plus what an import never supplies, passes ``check-model``.

        An ARXML export carries no processor for ``hardware/`` and the importer writes
        no root composition here, so both are declared, as a modeller would, and what
        this case reports is the imported content.
        """
        model_root, _ = self._imported(tmp_path)
        create_hardware_element(str(model_root), "CPU", "processor")
        _root_composition(model_root, sorted(swc.name for swc in read_swc_model(str(model_root))))

        issues = check_cross_layer_consistency(str(model_root)).issues

        assert issues == [], issues

    def test_the_imported_project_generates(self, tmp_path: Path) -> None:
        """``generate_arxml`` over exactly what the importer wrote."""
        model_root, _ = self._imported(tmp_path)

        output = generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

        text = Path(output).read_text(encoding="utf-8")
        # The same-name pair is two elements under one name, one per layer's package.
        assert "<SHORT-NAME>DoorStateEnum_app</SHORT-NAME>" not in text
        assert text.count("<SHORT-NAME>DoorStateEnum</SHORT-NAME>") == 2

    def test_every_emitted_short_name_is_unique_within_its_parent(self, tmp_path: Path) -> None:
        """Keeping both names produced no ARXML collision.

        Uniqueness is asked of each package's ``ELEMENTS`` and sub-packages, which
        is where ``[constr_2508]`` asks it -- never of the whole document.
        """
        model_root, _ = self._imported(tmp_path)

        tree = ET.parse(generate_arxml(str(model_root), str(tmp_path / "out.arxml")))
        duplicates = []
        for parent in tree.iter():
            if parent.tag.rsplit("}", 1)[-1] not in {"ELEMENTS", "AR-PACKAGES"}:
                continue
            names = [child.findtext("{*}SHORT-NAME") for child in parent]
            duplicates.extend(sorted({name for name in names if names.count(name) > 1}))
        assert duplicates == [], duplicates

    def test_an_impl_named_type_keeps_its_name_and_the_reference_to_it_names_it(self, tmp_path: Path) -> None:
        """The ``uint16_impl`` type keeps its name, and the reference type over it names it.

        The rename-follow promise lives in :class:`TestAReferenceToARenamedTypeFollowsTheRename`.
        """
        model_root, _ = self._imported(tmp_path)

        reference = yaml.safe_load(
            (model_element_path(model_root, "WheelTravelRange_impl", kind="reference_type")).read_text(encoding="utf-8")
        )
        kept = yaml.safe_load(
            (model_element_path(model_root, f"uint16{IMPL_SUFFIX}", kind="value_type")).read_text(encoding="utf-8")
        )

        assert (reference["kind"], reference["base"]) == ("reference_type", f"uint16{IMPL_SUFFIX}")
        assert kept["name"] == f"uint16{IMPL_SUFFIX}"
        stale = [
            path.name
            for path in sorted(model_root.rglob("*.yaml"), key=lambda path: path.as_posix())
            if f"uint16{IMPL_SUFFIX}{RENAME_SUFFIX}" in path.read_text(encoding="utf-8")
        ]
        assert stale == []

    def test_the_import_report_claims_no_rename(self, tmp_path: Path) -> None:
        """The import report claims no rename: neither the ``_impl`` nor the same-name pair moves."""
        _, report = self._imported(tmp_path)

        rendered = report.render()

        assert report.rename_lines == [], report.rename_lines
        assert "DoorStateEnum_app" not in rendered
        assert not any(f"uint16{IMPL_SUFFIX} ->" in line for line in report.rename_lines)
        assert "IMPLEMENTATION-DATA-TYPE auto-generated for 'uint16'" not in rendered


def _renamed_types_export() -> str:
    """An export in which phase B renames one implementation type and leaves two alone.

    A generated form collides inside its owner's package only, so a base type is
    never renamed and an implementation type collides only with a bounded value
    type's ``{x}_Range``. ``Raw_CompuMethod`` (a base type) and ``Speed_CompuMethod``
    (a value type) sit outside the enum's package and are kept; ``Ratio_Range``
    sits beside the bounded value type ``Ratio`` and moves. Each type is named by
    the one reference that can name it. The compu methods are named ``…Scale`` so
    nothing depends on a COMPU-METHOD *element* being invisible to the data-type
    namespace.
    """

    def base(name: str) -> str:
        return (
            f"<SW-BASE-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
            "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
            "<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION></SW-BASE-TYPE>"
        )

    def implementation(name: str, category: str, ref: str) -> str:
        return (
            f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            f"{ref}</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</IMPLEMENTATION-DATA-TYPE>"
        )

    def value(name: str, base_name: str, constr: str = "") -> str:
        constr_ref = f'<DATA-CONSTR-REF DEST="DATA-CONSTR">/Pkg/Types/{constr}</DATA-CONSTR-REF>' if constr else ""
        return implementation(
            name, "VALUE", f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">/Pkg/Types/{base_name}</BASE-TYPE-REF>{constr_ref}'
        )

    def data_constr(name: str) -> str:
        return (
            f"<DATA-CONSTR><SHORT-NAME>{name}</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
            '<INTERNAL-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">100</UPPER-LIMIT></INTERNAL-CONSTRS>'
            "</DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
        )

    def reference(name: str, target: str) -> str:
        ref = (
            '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            f"/Pkg/Types/{target}</IMPLEMENTATION-DATA-TYPE-REF>"
        )
        return implementation(name, "TYPE_REFERENCE", ref)

    def compu_method(name: str) -> str:
        return (
            f"<COMPU-METHOD><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
            "<COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>'
            "<COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE>"
            "</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
        )

    def enum(name: str, method: str) -> str:
        return (
            f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            f'<COMPU-METHOD-REF DEST="COMPU-METHOD">/Pkg/Types/{method}</COMPU-METHOD-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )

    elements = "".join(
        (
            base("uint8"),
            compu_method("RawScale"),
            enum("Raw", "RawScale"),
            base(f"Raw{COMPU_METHOD_SUFFIX}"),
            value("Counter", f"Raw{COMPU_METHOD_SUFFIX}"),
            compu_method("SpeedScale"),
            enum("Speed", "SpeedScale"),
            value(f"Speed{COMPU_METHOD_SUFFIX}", "uint8"),
            reference("SpeedRef", f"Speed{COMPU_METHOD_SUFFIX}"),
            data_constr("RatioLimits"),
            value("Ratio", "uint8", "RatioLimits"),
            value(f"Ratio{RANGE_SUFFIX}", "uint8"),
            reference("RatioRef", f"Ratio{RANGE_SUFFIX}"),
        )
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><AR-PACKAGES>"
        f"<AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS></AR-PACKAGE>"
        "</AR-PACKAGES></AR-PACKAGE></AR-PACKAGES></AUTOSAR>"
    )


class TestAReferenceToARenamedTypeFollowsTheRename:
    """A reference type's ``base`` naming a renamed implementation type follows the rename, end to end.

    A value type's ``base`` names a base type, which no generated form collides
    with, so that half is the acceptance that the base type keeps its name.
    """

    @staticmethod
    def _imported(tmp_path: Path) -> tuple[Path, object]:
        source = tmp_path / "renamed_types.arxml"
        source.write_text(_renamed_types_export(), encoding="utf-8", newline="\n")
        return _import_into_fresh_project(tmp_path, source)

    def test_the_export_renames_exactly_the_same_package_collision(self, tmp_path: Path) -> None:
        """Precondition: the export renames exactly one type and keeps the other two."""
        model_root, report = self._imported(tmp_path)

        names = {dt.name for dt in read_data_types(str(model_root))}
        base_names = {dt.name for dt in read_layer(str(model_root), "base_types")}

        assert f"Ratio{RANGE_SUFFIX}{RENAME_SUFFIX}" in names
        assert f"Ratio{RANGE_SUFFIX}" not in names
        assert f"Raw{COMPU_METHOD_SUFFIX}" in base_names
        assert f"Speed{COMPU_METHOD_SUFFIX}" in names
        assert len([line for line in report.rename_lines if RANGE_SUFFIX in line]) == 1
        assert [line for line in report.rename_lines if COMPU_METHOD_SUFFIX in line] == []

    def test_a_value_types_base_keeps_naming_a_base_type_no_form_collides_with(self, tmp_path: Path) -> None:
        """A value type's ``base`` keeps naming a base type: the enum's form lies in another package."""
        model_root, _ = self._imported(tmp_path)

        by_name = {dt.name: dt for dt in read_data_types(str(model_root))}

        assert by_name["Counter"] == ValueDataType(name="Counter", kind="value_type", base=f"Raw{COMPU_METHOD_SUFFIX}")

    def test_a_reference_types_base_follows_a_renamed_implementation_type(self, tmp_path: Path) -> None:
        model_root, _ = self._imported(tmp_path)

        by_name = {dt.name: dt for dt in read_data_types(str(model_root))}

        assert by_name["RatioRef"] == ReferenceDataType(
            name="RatioRef", kind="reference_type", base=f"Ratio{RANGE_SUFFIX}{RENAME_SUFFIX}"
        )
        assert by_name["SpeedRef"] == ReferenceDataType(
            name="SpeedRef", kind="reference_type", base=f"Speed{COMPU_METHOD_SUFFIX}"
        )

    def test_no_data_type_reference_is_left_dangling(self, tmp_path: Path) -> None:
        model_root, _ = self._imported(tmp_path)

        records = check_cross_layer_consistency(str(model_root)).records

        assert [record.rule for record in records if record.rule.startswith("data-type.")] == []


# ---------------------------------------------------------------------------
# Phase B: the importer reconciles with the writer's reserved names
#
# Phase A (`PathRegistry._reserve()`) resolves bare-name collisions while the
# mapping passes walk paths. Phase B resolves collisions with the names the
# WRITER reserves, which phase A structurally cannot see: the reservations are
# kind-scoped and the registry is handed one path at a time. The unit cases
# below drive `reconcile_reserved_names()` directly because that is where the
# rules live; the end-to-end shapes go through `run_import()`.
# ---------------------------------------------------------------------------


#: Four declarations of one bare name in four packages whose immediate parent
#: segment snake-cases identically. The implementation declaration is in a layer
#: of its own, so the three application ones use the named candidates and the
#: counter is pinned on the registry alone; the shape proves there is no rule-3
#: refusal to reach.
def _four_way_collision_arxml() -> str:
    """One ``SHORT-NAME`` declared four times, by four different KINDS.

    Cross-kind on purpose: two declarations of one name in one kind merge into one
    element, and rule 2 never runs. The implementation ``X`` is in a layer of its
    own and keeps its name; the three application declarations share one layer, so
    rule 2 gives ``X``, ``X_app``, ``X_app_types``. The ``SW-BASE-TYPE`` has a name
    of its own, and the record's and array's members are typed by the application
    primitive ``Level``, because an implementation type is no member's type. Every
    declaration sits under a package named ``Types``, so one layer shares one
    candidate sequence.
    """
    elements = {
        "A": (
            "<SW-BASE-TYPE><SHORT-NAME>Octet</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
            "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
            "<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION></SW-BASE-TYPE>"
        ),
        "B": (
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>X</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/A/Types/Octet</BASE-TYPE-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</IMPLEMENTATION-DATA-TYPE>"
        ),
        "C": (
            "<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>X</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
            "<ELEMENTS><APPLICATION-RECORD-ELEMENT><SHORT-NAME>f</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/F/Types/Level</TYPE-TREF>'
            "</APPLICATION-RECORD-ELEMENT></ELEMENTS></APPLICATION-RECORD-DATA-TYPE>"
        ),
        "D": (
            "<APPLICATION-ARRAY-DATA-TYPE><SHORT-NAME>X</SHORT-NAME><CATEGORY>ARRAY</CATEGORY>"
            "<ELEMENT><SHORT-NAME>e</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/F/Types/Level</TYPE-TREF>'
            "<MAX-NUMBER-OF-ELEMENTS>4</MAX-NUMBER-OF-ELEMENTS></ELEMENT>"
            "</APPLICATION-ARRAY-DATA-TYPE>"
        ),
        "E": (
            "<COMPU-METHOD><SHORT-NAME>X_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
            "<COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>'
            "<COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE>"
            "</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>X</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<COMPU-METHOD-REF DEST="COMPU-METHOD">/E/Types/X_CompuMethod</COMPU-METHOD-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        ),
        # The record's field and the array's element type: an application type,
        # since the importer skips a member typed by an implementation type.
        "F": (
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>Level</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        ),
    }
    packages = "".join(
        f"<AR-PACKAGE><SHORT-NAME>{group}</SHORT-NAME><AR-PACKAGES>"
        f"<AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME><ELEMENTS>{element}</ELEMENTS></AR-PACKAGE>"
        "</AR-PACKAGES></AR-PACKAGE>"
        for group, element in elements.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
        f"<AR-PACKAGES>{packages}</AR-PACKAGES></AUTOSAR>"
    )


def _by_name(data_types: list, name: str):
    return next(dt for dt in data_types if dt.name == name)


def _primitive(name: str) -> BaseDataType:
    return BaseDataType(name=name, kind="base_type", size=8, encoding="NONE", native_declaration="uint8")


def _alias(name: str, base: str) -> ValueDataType:
    return ValueDataType(name=name, kind="value_type", base=base)


def _reference(name: str, base: str) -> ReferenceDataType:
    return ReferenceDataType(name=name, kind="reference_type", base=base)


def _enum(name: str) -> EnumDataType:
    return EnumDataType(name=name, kind="enum", literals=[EnumLiteral(name="ON", value=1)])


def _record(name: str, field_type: str) -> RecordDataType:
    return RecordDataType(name=name, kind="record", fields=[RecordField(name="f", type=field_type)])


def _array(name: str, element_type: str) -> ArrayDataType:
    return ArrayDataType(name=name, kind="array", element_type=element_type, length=4)


def _application(name: str, **declared: object) -> ApplicationDataType:
    """An application type declaring exactly the fields *declared* names.

    Which of the three application-type forms it mints is a function of those
    fields alone (``generated_application_forms``).
    """
    return ApplicationDataType.model_validate({"name": name, "kind": "application_type", **declared})


def _imported_swc(name: str) -> ImportedSwc:
    return ImportedSwc(swc=SWComponent(name=name, type="application"), group="Components", path=f"/Pkg/{name}")


def _reconcile(
    data_types: list | None = None,
    interfaces: list | None = None,
    swcs: list | None = None,
    connectors: list | None = None,
    registry: PathRegistry | None = None,
    compositions: list | None = None,
):
    """Run phase B over hand-built elements, registering each type first.

    The registry's path map is one of the two name structures a rename writes back
    to, so registration mirrors what the mapping passes did.
    """
    registry = registry if registry is not None else PathRegistry()
    data_types = data_types or []
    for dt in data_types:
        # Per layer: a same-name pair of two layers is two registrations, the
        # second at a path of its own.
        layer = data_type_layer(dt)
        if dt.name not in registry.claimed_names(layer):
            path = f"/Pkg/Types/{dt.name}"
            if registry.resolve(path) is not None:
                path = f"/Pkg/{layer}/{dt.name}"
            registry.register(path, dt.name, layer)
    return registry, reconcile_reserved_names(
        data_types=data_types,
        interfaces=interfaces or [],
        swcs=swcs or [],
        connectors=connectors or [],
        compositions=compositions or [],
        registry=registry,
        mapping_sets=[],
    )


def _names(reconciled) -> set[str]:
    return {dt.name for dt in reconciled.data_types}


class TestTheKindScopedRulesPhaseBCanFinallyEvaluate:
    """The reservations are kind-scoped, and that is the load-bearing part.

    ``_reserve()`` sees a bare name and a path — never a kind, never the final
    set — so these rules are only evaluable after mapping. A pass that ignored
    the kind would rename names generation accepts; one that kept renaming for a
    *retired* form would do the same. Both are pinned against
    :data:`RESERVED_DATA_TYPE_RULES` as the writer states it. The three
    application-type forms are not kind-scoped (``reserved_data_type_names`` reads
    them per declared field); their end-to-end shape is
    ``test_arxml_import_application_types.py``'s and
    :class:`TestAPhaseBRenameBesideAnImportedApplicationType`.
    """

    def test_impl_conflicts_with_no_base_since_the_twin_is_retired(self) -> None:
        """The retired ``_impl`` form is reserved beside no kind."""
        assert not reserved_data_type_names({"x": _primitive("x"), f"x{IMPL_SUFFIX}": _primitive(f"x{IMPL_SUFFIX}")})

        assert not reserved_data_type_names({"x": _enum("x"), f"x{IMPL_SUFFIX}": _primitive(f"x{IMPL_SUFFIX}")})

    def test_compu_method_conflicts_only_when_the_base_is_an_enum(self) -> None:
        """``{x}_CompuMethod`` is reserved beside an enum only.

        The occupant is a record, of the enum's own package: a form collides inside
        its owner's package only.
        """
        suffixed = f"x{COMPU_METHOD_SUFFIX}"

        assert reserved_data_type_names({"x": _enum("x"), suffixed: _record(suffixed, "u8")})

        assert not reserved_data_type_names({"x": _primitive("x"), suffixed: _record(suffixed, "u8")})

    @pytest.mark.parametrize(
        "base",
        [_primitive("x"), _alias("x", "u8"), _reference("x", "u8")],
        ids=["base_type", "value_type", "reference_type"],
    )
    def test_the_retired_application_view_conflicts_with_nothing(self, base) -> None:
        """The retired ``{x}_AppView`` is an ordinary name beside every kind, the base type included."""
        suffixed = f"x{APP_VIEW_SUFFIX}"

        reserved = reserved_data_type_names({"x": base, suffixed: _primitive(suffixed)})

        assert reserved == {}

    def test_a_same_named_type_of_another_kind_is_not_touched(self) -> None:
        """A record gets no computation method, so ``Pose_T_CompuMethod`` is legal."""
        suffixed = f"Pose_T{COMPU_METHOD_SUFFIX}"

        reserved = reserved_data_type_names({"Pose_T": _record("Pose_T", "u8"), suffixed: _primitive(suffixed)})

        assert reserved == {}

    def test_the_kind_scope_is_read_from_the_writer_not_restated(self) -> None:
        """Every rule the writer publishes is one this pass evaluates.

        The occupant is of the owner's own kind, so of its package: a form collides
        inside its owner's package only.
        """
        for suffix, _element, kinds in RESERVED_DATA_TYPE_RULES:
            base = {BaseDataType: _primitive, ValueDataType: _alias, EnumDataType: _enum, ReferenceDataType: _reference}
            builder = base[kinds[0]]

            def build(name: str, builder=builder, kind=kinds[0]):
                return builder(name, "u8") if kind in (ValueDataType, ReferenceDataType) else builder(name)

            reserved = reserved_data_type_names({"x": build("x"), f"x{suffix}": build(f"x{suffix}")})

            assert f"x{suffix}" in reserved, f"the {suffix!r} rule is not evaluated by phase B"


#: The export :func:`_export_with_a_declared_unit_name` writes. A module-level
#: literal rather than an inline string, so the XML is not indented inside a
#: function body and every ``SHORT-NAME`` reads as it will be parsed.
_DECLARED_UNIT_NAME_EXPORT = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <ELEMENTS>
        <SW-BASE-TYPE>
          <SHORT-NAME>Word</SHORT-NAME>
          <CATEGORY>FIXED_LENGTH</CATEGORY>
          <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
          <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
        </SW-BASE-TYPE>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>SpeedRaw</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Word</BASE-TYPE-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </IMPLEMENTATION-DATA-TYPE>
        <UNIT>
          <SHORT-NAME>SpeedUnit</SHORT-NAME>
          <DISPLAY-NAME>km/h</DISPLAY-NAME>
          <FACTOR-SI-TO-UNIT>1</FACTOR-SI-TO-UNIT>
        </UNIT>
        <APPLICATION-PRIMITIVE-DATA-TYPE>
          <SHORT-NAME>Speed</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <UNIT-REF DEST="UNIT">/Acme/SpeedUnit</UNIT-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </APPLICATION-PRIMITIVE-DATA-TYPE>
        <APPLICATION-PRIMITIVE-DATA-TYPE>
          <SHORT-NAME>Speed_Unit</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
        </APPLICATION-PRIMITIVE-DATA-TYPE>
        <DATA-TYPE-MAPPING-SET>
          <SHORT-NAME>TypeMaps</SHORT-NAME>
          <DATA-TYPE-MAPS>
            <DATA-TYPE-MAP>
              <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Speed\
</APPLICATION-DATA-TYPE-REF>
              <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/SpeedRaw\
</IMPLEMENTATION-DATA-TYPE-REF>
            </DATA-TYPE-MAP>
            <DATA-TYPE-MAP>
              <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Speed_Unit\
</APPLICATION-DATA-TYPE-REF>
              <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/SpeedRaw\
</IMPLEMENTATION-DATA-TYPE-REF>
            </DATA-TYPE-MAP>
          </DATA-TYPE-MAPS>
        </DATA-TYPE-MAPPING-SET>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>ISpeed</SHORT-NAME>
          <IS-SERVICE>false</IS-SERVICE>
          <DATA-ELEMENTS>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>speed</SHORT-NAME>
              <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Speed</TYPE-TREF>
            </VARIABLE-DATA-PROTOTYPE>
          </DATA-ELEMENTS>
        </SENDER-RECEIVER-INTERFACE>
        <APPLICATION-SW-COMPONENT-TYPE>
          <SHORT-NAME>SpeedSwc</SHORT-NAME>
          <PORTS>
            <P-PORT-PROTOTYPE>
              <SHORT-NAME>pSpeed</SHORT-NAME>
              <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/ISpeed</PROVIDED-INTERFACE-TREF>
            </P-PORT-PROTOTYPE>
          </PORTS>
          <INTERNAL-BEHAVIORS>
            <SWC-INTERNAL-BEHAVIOR>
              <SHORT-NAME>SpeedSwc_InternalBehavior</SHORT-NAME>
              <DATA-TYPE-MAPPING-REFS>
                <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Acme/TypeMaps</DATA-TYPE-MAPPING-REF>
              </DATA-TYPE-MAPPING-REFS>
            </SWC-INTERNAL-BEHAVIOR>
          </INTERNAL-BEHAVIORS>
        </APPLICATION-SW-COMPONENT-TYPE>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def _export_with_a_declared_unit_name(tmp_path):
    """Write an export declaring ``Speed`` with a ``UNIT-REF`` beside an application type ``Speed_Unit``.

    Both map to ``SpeedRaw`` in a ``DATA-TYPE-MAPPING-SET``, whose application side
    has to follow the rename. Inline rather than in ``fixtures/arxml_import_shapes/``,
    whose corpus is pinned as a set against its directory listing.
    """
    source = tmp_path / "declared_unit_name.arxml"
    source.write_text(_DECLARED_UNIT_NAME_EXPORT, encoding="utf-8", newline="\n")
    return source


class TestAPhaseBRenameBesideAnImportedApplicationType:
    """The field-scoped forms, end to end: a declared ``Speed_Unit`` beside a ``Speed`` with a unit.

    The writer mints ``Speed_Unit`` for that application type, so phase B moves the
    **derived** side to ``Speed_Unit_app``, and **every reference follows it** --
    including the ``mapping_sets/`` element's, or ``check-model`` reports
    ``rule:mapping-set.unknown-implementation`` and ``generate arxml`` refuses the
    project a clean import wrote.
    """

    @staticmethod
    def _imported(tmp_path):
        return _import_into_fresh_project(tmp_path, _export_with_a_declared_unit_name(tmp_path))

    def test_the_declared_unit_name_moves_and_the_references_follow(self, tmp_path: Path) -> None:
        model_root, _report = self._imported(tmp_path)

        names = {
            path.stem
            for directory in (model_root / "data_types" / "implementation", model_root / "data_types" / "application")
            for path in directory.glob("*.yaml")
        }
        assert names == {"SpeedRaw", "Speed", f"Speed{UNIT_SUFFIX}{RENAME_SUFFIX}"}
        assert {path.stem for path in (model_root / "data_types" / "base").glob("*.yaml")} == {"Word"}

        mapping_set = yaml.safe_load(
            (model_element_path(model_root, "TypeMaps", layer="mapping_sets")).read_text(encoding="utf-8")
        )
        assert mapping_set["maps"] == [
            {"application": "Speed", "implementation": "SpeedRaw"},
            {"application": f"Speed{UNIT_SUFFIX}{RENAME_SUFFIX}", "implementation": "SpeedRaw"},
        ]

    def test_the_written_project_carries_no_unresolved_mapping_reference(self, tmp_path: Path) -> None:
        """The import leaves a project the gate does not report for a name it moved."""
        model_root, _report = self._imported(tmp_path)

        issues = check_cross_layer_consistency(str(model_root)).issues

        assert [issue for issue in issues if "mapping set" in issue.lower()] == [], issues

    def test_the_rename_is_reported(self, tmp_path: Path) -> None:
        _model_root, report = self._imported(tmp_path)

        assert f"Speed{UNIT_SUFFIX}{RENAME_SUFFIX}" in report.render()


class TestTheFixedReservedNamesADataTypeOnlyPassWouldMiss:
    """The name that is schema-valid ARXML and unbuildable once imported.

    It lives in the **SWC** namespace, which a data-type-only pass never visits.
    The writer owns no fixed name any more; the *claimant* namespace covers a
    composition name (a composition and a component are siblings in
    ``/ComponentTypes``) and the *base* namespace does not, which the last two
    cases pin.
    """

    def test_the_retired_mapping_set_name_is_reserved_by_nothing(self) -> None:
        """``DataTypeMappingSet`` is reserved by nothing: the writer mints no mapping set name."""
        name = MAPPING_SET_NAME

        assert reserved_data_type_names({name: _primitive(name)}) == {}

    def test_an_implementation_named_sibling_is_reserved(self) -> None:
        assert reserved_swc_names({"A", f"A{IMPLEMENTATION_SUFFIX}"}, {"A"}, set()) == {
            f"A{IMPLEMENTATION_SUFFIX}": "collides with the SWC-IMPLEMENTATION auto-generated for 'A'"
        }

    def test_an_implementation_named_swc_alone_is_legal(self) -> None:
        """Only a collision once the base component is declared too."""
        assert reserved_swc_names({f"A{IMPLEMENTATION_SUFFIX}"}, set(), set()) == {}

    def test_the_internal_behavior_suffix_is_not_reserved(self) -> None:
        """Deliberately absent, and stated so a reader does not "fix" it.

        That element is nested inside its own component, so
        ``/ComponentTypes/B/B_InternalBehavior`` and a component named
        ``B_InternalBehavior`` are distinct AUTOSAR paths.
        """
        assert reserved_swc_names({"B", f"B{INTERNAL_BEHAVIOR_SUFFIX}"}, {"B"}, {"B"}) == {}

    def test_a_composition_is_a_claimant_and_never_a_base(self) -> None:
        """A composition emits no implementation, so ``A_Implementation`` beside it is ordinary.

        ``arxml_generation._check_component_type_names``' base set holds no
        composition, so reserving it would rename a name generation accepts.
        """
        claimants = {"A", f"A{IMPLEMENTATION_SUFFIX}"}

        assert reserved_swc_names(claimants, set(), set()) == {}

    def test_an_external_stub_is_a_claimant_and_never_a_base(self) -> None:
        """``_emit_swcs`` emits nothing at all for a stub, so it is no base either."""
        claimants = {"ForeignSwc", f"ForeignSwc{IMPLEMENTATION_SUFFIX}"}

        assert reserved_swc_names(claimants, set(), set()) == {}


class TestWhichSideOfTheCollisionMoves:
    """One test per row of the rename-target table.

    The element whose name is the *derived* form moves, because the base name is
    the one a reference is likely to use; renaming the base instead would be a
    silently different migration for every project.
    """

    def test_a_declared_impl_beside_a_base_type_moves_nothing(self) -> None:
        """A declared ``_impl`` beside a base type moves nothing: the form is retired."""
        derived = f"u16{IMPL_SUFFIX}"

        _registry, reconciled = _reconcile(data_types=[_primitive("u16"), _primitive(derived)])

        assert _names(reconciled) == {"u16", derived}

    def test_a_declared_compu_method_moves_and_the_enum_stays(self) -> None:
        derived = f"Mode{COMPU_METHOD_SUFFIX}"

        # An application type: a form collides inside its owner's package only, and
        # the enum's package is the application one.
        _registry, reconciled = _reconcile(data_types=[_enum("Mode"), _application(derived)])

        assert _names(reconciled) == {"Mode", f"{derived}{RENAME_SUFFIX}"}

    def test_a_declared_unit_name_moves_and_the_application_type_stays(self) -> None:
        """The field-scoped forms: ``Level`` declares a ``unit``, so the declared ``Level_Unit`` moves."""
        derived = f"Level{UNIT_SUFFIX}"

        # The occupant is an application type, of ``Level``'s own package: a form
        # collides inside its owner's package only.
        _registry, reconciled = _reconcile(
            data_types=[_primitive("u8"), _application("Level", unit="mps"), _application(derived)],
        )

        assert _names(reconciled) == {"u8", "Level", f"{derived}{RENAME_SUFFIX}"}

    def test_a_declared_unit_name_beside_a_type_that_declares_none_moves_nothing(self) -> None:
        """No ``unit``, no ``Level_Unit``: the declared name is ordinary and neither side moves."""
        derived = f"Level{UNIT_SUFFIX}"

        _registry, reconciled = _reconcile(
            data_types=[_primitive("u8"), _application("Level"), _primitive(derived)],
        )

        assert _names(reconciled) == {"u8", "Level", derived}

    def test_a_declared_implementation_component_moves_and_the_base_swc_stays(self) -> None:
        derived = f"Brake{IMPLEMENTATION_SUFFIX}"

        _registry, reconciled = _reconcile(swcs=[_imported_swc("Brake"), _imported_swc(derived)])

        assert {imported.swc.name for imported in reconciled.swcs} == {"Brake", f"{derived}{RENAME_SUFFIX}"}

    def test_one_pass_renames_across_both_namespaces_at_once(self) -> None:
        """One reconcile carrying both namespaces' reservations moves both.

        Every row on either side is a *derived* form whose base stays put, so what
        the single-namespace cases above cannot show is that one pass reaches both.
        """
        data_derived = f"Mode{COMPU_METHOD_SUFFIX}"
        swc_derived = f"Brake{IMPLEMENTATION_SUFFIX}"

        _registry, reconciled = _reconcile(
            data_types=[_enum("Mode"), _application(data_derived)],
            swcs=[_imported_swc("Brake"), _imported_swc(swc_derived)],
        )

        assert _names(reconciled) == {"Mode", f"{data_derived}{RENAME_SUFFIX}"}
        assert {imported.swc.name for imported in reconciled.swcs} == {
            "Brake",
            f"{swc_derived}{RENAME_SUFFIX}",
        }


class TestEveryReferenceFollowsARenamedType:
    """The half a registry-only change would miss.

    The mapped model is a graph of Pydantic objects that reference each other
    **by emitted name**, so every referring shape is covered: one missed shape is
    a dangling reference `check_cross_layer_consistency` reports on a project the
    import just called successful.
    """

    def _renamed_impl(self) -> tuple[str, str]:
        """A derived form that still collides (``u16_CompuMethod`` beside the enum ``u16``), and its rename."""
        return f"u16{COMPU_METHOD_SUFFIX}", f"u16{COMPU_METHOD_SUFFIX}{RENAME_SUFFIX}"

    def test_an_alias_base_names_a_base_type_no_form_collides_with(self) -> None:
        """A value type's ``base`` names a base type, which no generated form collides with: nothing moves."""
        old, _new = self._renamed_impl()
        data_types = [_enum("u16"), _primitive(old), _alias("Level", old)]

        _registry, reconciled = _reconcile(data_types=list(data_types))

        assert reconciled.data_types == data_types

    @pytest.mark.parametrize("renamed_kind", ["value_type", "reference_type"])
    def test_a_reference_type_base_follows(self, renamed_kind: str) -> None:
        """A reference type's ``base`` follows a renamed implementation type.

        ``{x}_Range`` beside the **bounded value type** ``x`` is the one form in the
        implementation package a value or reference type can collide with.
        """
        old = f"Speed{RANGE_SUFFIX}"
        new = f"{old}{RENAME_SUFFIX}"
        renamed = _alias(old, "u8") if renamed_kind == "value_type" else _reference(old, "Phys")
        bounded = ValueDataType(name="Speed", kind="value_type", base="u8", value_min=0, value_max=100)

        _registry, reconciled = _reconcile(
            data_types=[_primitive("u8"), _alias("Phys", "u8"), bounded, renamed, _reference("SpeedRef", old)]
        )

        assert new in _names(reconciled)
        assert _by_name(reconciled.data_types, "SpeedRef").base == new

    def test_a_record_field_type_follows(self) -> None:
        old, new = self._renamed_impl()

        _registry, reconciled = _reconcile(data_types=[_enum("u16"), _application(old), _record("Pose", old)])

        assert [f.type for f in _by_name(reconciled.data_types, "Pose").fields] == [new]

    def test_an_array_element_type_follows(self) -> None:
        old, new = self._renamed_impl()

        _registry, reconciled = _reconcile(data_types=[_enum("u16"), _application(old), _array("Buf", old)])

        assert _by_name(reconciled.data_types, "Buf").element_type == new

    def test_a_sender_receiver_data_element_type_follows(self) -> None:
        """The element names the renamed application type by ``application_type``, which follows.

        A rename rewrites only the references whose field targets the renamed type's layer.
        """
        old, new = self._renamed_impl()
        iface = SRInterface(
            name="ISpeed", kind="sender_receiver", data_elements=[DataElement(name="v", application_type=old)]
        )

        _registry, reconciled = _reconcile(data_types=[_enum("u16"), _application(old)], interfaces=[iface])

        assert [element.application_type for element in reconciled.interfaces[0].data_elements] == [new]

    def test_a_client_server_operation_argument_type_follows(self) -> None:
        """Named by ``application_type``, as the data element above is."""
        old, new = self._renamed_impl()
        iface = CSInterface(
            name="IDiag",
            kind="client_server",
            operations=[
                CSOperation(name="read", arguments=[CSArgument(name="out", direction="out", application_type=old)])
            ],
        )

        _registry, reconciled = _reconcile(data_types=[_enum("u16"), _application(old)], interfaces=[iface])

        assert [arg.application_type for arg in reconciled.interfaces[0].operations[0].arguments] == [new]

    def test_the_registrys_own_name_sets_follow(self) -> None:
        """Both structures follow, not just the map.

        A stale ``_used_names`` entry would let a later mint hand the freed name out a second time."""
        old, new = self._renamed_impl()

        registry, _reconciled = _reconcile(data_types=[_enum("u16"), _application(old)])

        assert registry.resolve(f"/Pkg/Types/{old}") == new
        assert new in registry.claimed_names("application_types")
        assert old not in registry.claimed_names("application_types")

    def test_a_conforming_model_is_returned_untouched(self) -> None:
        """The normal case: nothing collides, nothing is rewritten."""
        data_types = [_primitive("u16"), _alias("Level", "u16")]

        _registry, reconciled = _reconcile(data_types=list(data_types))

        assert reconciled.data_types == data_types


class TestEverythingFollowsARenamedSwc:
    """A component name is embedded in more places than a type name is."""

    @staticmethod
    def _connector(from_swc: str, to_swc: str) -> Connector:
        return Connector(
            name="Assembly1",
            **{"from": ConnectorEndpoint(swc=from_swc, port="pOut")},
            to=ConnectorEndpoint(swc=to_swc, port="rIn"),
            composition="Root",
        )

    def test_the_connector_endpoints_follow_and_the_name_does_not_move(self) -> None:
        """A component rename re-points the connector's endpoints and leaves its name where it was.

        The connector's ``SHORT-NAME`` is the export's own."""
        derived = f"Brake{IMPLEMENTATION_SUFFIX}"
        renamed = f"{derived}{RENAME_SUFFIX}"

        _registry, reconciled = _reconcile(
            swcs=[_imported_swc("Brake"), _imported_swc(derived)],
            connectors=[self._connector("Brake", derived)],
        )

        connector = reconciled.connectors[0]
        assert connector.to.swc == renamed
        assert connector.name == "Assembly1"
        assert connector.composition == "Root"

    def test_a_composition_member_naming_the_renamed_component_follows(self) -> None:
        """A composition member naming the renamed component follows.

        Otherwise ``rule:composition.member-unknown-type`` reports it on the next ``check-model``."""
        derived = f"Brake{IMPLEMENTATION_SUFFIX}"
        renamed = f"{derived}{RENAME_SUFFIX}"
        composition = Composition(
            name="Root",
            title="Root",
            root=True,
            members=[
                CompositionMember(name="Brake_1", type="Brake"),
                CompositionMember(name="Impl_1", type=derived),
            ],
        )

        _registry, reconciled = _reconcile(
            swcs=[_imported_swc("Brake"), _imported_swc(derived)],
            compositions=[composition],
        )

        assert [m.type for m in reconciled.compositions[0].members] == ["Brake", renamed]

    def test_an_untouched_connector_is_returned_as_is(self) -> None:
        connector = self._connector("Brake", "Wheel")

        _registry, reconciled = _reconcile(
            swcs=[_imported_swc("Brake"), _imported_swc("Wheel")],
            connectors=[connector],
        )

        assert reconciled.connectors == [connector]


class TestARenameLeavesEverythingElseAlone:
    """With a real rename, the *unrelated* elements come back identical.

    The untouched-element cases elsewhere declare no reserved name, so the pass
    short-circuits before any rewriter runs; a rewriter that clobbered an
    unrelated element would be invisible there."""

    def test_an_unaffected_connector_is_the_same_object(self) -> None:
        derived = f"Brake{IMPLEMENTATION_SUFFIX}"
        untouched = TestEverythingFollowsARenamedSwc._connector("Wheel", "Hub")

        _registry, reconciled = _reconcile(
            swcs=[_imported_swc("Brake"), _imported_swc(derived), _imported_swc("Wheel"), _imported_swc("Hub")],
            connectors=[TestEverythingFollowsARenamedSwc._connector("Brake", derived), untouched],
        )

        assert reconciled.connectors[1] is untouched

    def test_an_unaffected_sender_receiver_interface_is_the_same_object(self) -> None:
        old = f"u16{COMPU_METHOD_SUFFIX}"  # an application type: same package as the enum
        # Both name application types, so by `application_type`.
        untouched = SRInterface(
            name="IOther", kind="sender_receiver", data_elements=[DataElement(name="v", application_type="u16")]
        )

        _registry, reconciled = _reconcile(
            data_types=[_enum("u16"), _application(old)],
            interfaces=[
                SRInterface(
                    name="ISpeed",
                    kind="sender_receiver",
                    data_elements=[DataElement(name="v", application_type=old)],
                ),
                untouched,
            ],
        )

        assert reconciled.interfaces[0].data_elements[0].application_type == f"{old}{RENAME_SUFFIX}"
        assert reconciled.interfaces[1] is untouched

    def test_an_unaffected_client_server_interface_is_the_same_object(self) -> None:
        old = f"u16{COMPU_METHOD_SUFFIX}"  # an application type: same package as the enum
        untouched = CSInterface(
            name="IOther",
            kind="client_server",
            operations=[
                CSOperation(name="read", arguments=[CSArgument(name="out", direction="out", application_type="u16")])
            ],
        )

        # Both name application types, so by `application_type`.
        _registry, reconciled = _reconcile(
            data_types=[_enum("u16"), _application(old)],
            interfaces=[
                CSInterface(
                    name="IDiag",
                    kind="client_server",
                    operations=[
                        CSOperation(name="r", arguments=[CSArgument(name="o", direction="out", application_type=old)])
                    ],
                ),
                untouched,
            ],
        )

        assert reconciled.interfaces[0].operations[0].arguments[0].application_type == f"{old}{RENAME_SUFFIX}"
        assert reconciled.interfaces[1] is untouched

    def test_a_data_type_sharing_a_renamed_swcs_name_is_left_alone(self) -> None:
        """A data type and an SWC may legally carry one name — separate layers.

        The SWC half reports its renames through ``registry.record_rename``, not
        ``registry.rename``: the latter re-points every **path** emitted under that
        name and frees it in the name set, which for a component (in no registry
        map) could only hit an unrelated data type.
        """
        derived = f"A{IMPLEMENTATION_SUFFIX}"
        registry = PathRegistry()

        registry, reconciled = _reconcile(
            data_types=[_primitive(derived)],
            swcs=[_imported_swc("A"), _imported_swc(derived)],
            registry=registry,
        )

        assert {swc.swc.name for swc in reconciled.swcs} == {"A", f"{derived}{RENAME_SUFFIX}"}
        assert _by_name(reconciled.data_types, derived).name == derived
        assert registry.resolve(f"/Pkg/Types/{derived}") == derived, "the data type's path must still resolve to it"
        assert derived in registry.claimed_names("base_types"), (
            "freeing a name still in use would let a later mint reuse it"
        )
        assert any(line.startswith(f"{derived} -> ") for line in registry.renames), (
            "the SWC rename must still reach the one rename category ImportReport renders"
        )


class TestTheFixedPointBound:
    """``_MAX_PASSES`` bounds the rename **rounds**; the scan needs one check more.

    A set that settles on the last permitted round must be accepted. The bound is
    exercised by shrinking it: an 8-deep chain of ``_Implementation`` names does
    not fit in 128 characters, so the real constant cannot be driven from a model."""

    @staticmethod
    def _impl_chain(depth: int) -> list:
        """``u`` plus `depth` enums, each reserved only once the previous one is renamed.

        ``_CompuMethod`` over an enum is the rule whose derived name can itself be a base.
        """
        names = []
        current = f"u{COMPU_METHOD_SUFFIX}"
        for _ in range(depth):
            names.append(current)
            current = f"{current}{RENAME_SUFFIX}{COMPU_METHOD_SUFFIX}"
        return [_enum("u"), *[_enum(name) for name in names]]

    def test_a_chain_that_settles_on_the_last_permitted_round_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reconcile, "_MAX_PASSES", 3)

        _registry, reconciled = _reconcile(data_types=self._impl_chain(3))

        assert reserved_data_type_names({dt.name: dt for dt in reconciled.data_types}) == {}

    def test_a_chain_one_round_too_deep_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(reconcile, "_MAX_PASSES", 3)

        with pytest.raises(NameCollisionError, match="did not settle"):
            _reconcile(data_types=self._impl_chain(4))

    def test_the_swc_half_has_the_same_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(reconcile, "_MAX_PASSES", 1)
        derived = f"A{IMPLEMENTATION_SUFFIX}"
        deeper = f"{derived}{RENAME_SUFFIX}{IMPLEMENTATION_SUFFIX}"

        with pytest.raises(NameCollisionError, match="did not settle"):
            _reconcile(swcs=[_imported_swc("A"), _imported_swc(derived), _imported_swc(deeper)])

    def test_the_real_bound_is_eight(self) -> None:
        """Pinned so shrinking it in the source is a deliberate act."""
        assert reconcile._MAX_PASSES == 8


class TestAViolationIsReportedUnderTheNameTheExportDeclared:
    """A violation names the name the export declared, not the renamed one.

    `identifier_violations()` runs **after** both rename phases, so a renamed
    illegal name has already moved when it is checked. The two `*_source_names`
    maps carry the declared name forward — the data-type one is
    `PathRegistry.source_names`, which **both** phases write; they are per
    namespace because a data type and an SWC may legally share one name.
    """

    def test_a_bare_name_collision_rename_is_reported_under_its_source_name(self) -> None:
        """Phase A's half: rule 2 minted `My Type_app` with no reserved-name rename involved."""
        # Both base types, so one layer: rule 2 fires inside one only.
        registry = PathRegistry()
        registry.register("/A/Types/My Type", "My Type", "base_types")
        registry.register("/B/Types/My Type", "My Type", "base_types")

        _registry, reconciled = _reconcile(
            data_types=[_primitive("My Type"), _primitive("My Type_app")], registry=registry
        )

        assert reconciled.data_type_source_names == {("base_types", "My Type_app"): "My Type"}
        assert identifier_violations(reconciled, []) == [
            f"data type 'My Type': {REASON_PATTERN}",
            f"data type 'My Type' (renamed to 'My Type_app' by this import): {REASON_PATTERN}",
        ]

    def test_a_renamed_data_type_is_reported_under_its_source_name(self) -> None:
        # The occupant is an application type: a form collides inside its owner's
        # package only.
        derived = f"My Type{COMPU_METHOD_SUFFIX}"
        _registry, reconciled = _reconcile(data_types=[_enum("My Type"), _application(derived)])

        assert reconciled.data_type_source_names == {("application_types", f"{derived}{RENAME_SUFFIX}"): derived}
        assert identifier_violations(reconciled, []) == [
            f"data type 'My Type': {REASON_PATTERN}",
            f"data type '{derived}' (renamed to '{derived}{RENAME_SUFFIX}' by this import): {REASON_PATTERN}",
        ]

    def test_a_renamed_swc_is_reported_under_its_source_name(self) -> None:
        derived = f"A B{IMPLEMENTATION_SUFFIX}"
        _registry, reconciled = _reconcile(swcs=[_imported_swc("A B"), _imported_swc(derived)])

        assert reconciled.swc_source_names == {f"{derived}{RENAME_SUFFIX}": derived}
        assert [line for line in identifier_violations(reconciled, []) if "renamed to" in line] == [
            f"SWC '{derived}' (renamed to '{derived}{RENAME_SUFFIX}' by this import): {REASON_PATTERN}"
        ]

    def test_a_member_of_a_renamed_type_is_reported_under_the_declared_container(self) -> None:
        """The container name is part of the subject, so it maps back too."""
        record = RecordDataType(name=f"Pose{IMPL_SUFFIX}", kind="record", fields=[RecordField(name="x y", type="u16")])
        _registry, reconciled = _reconcile(data_types=[_primitive("Pose"), _primitive("u16"), record])

        assert [line for line in identifier_violations(reconciled, []) if "field" in line] == [
            f"record 'Pose{IMPL_SUFFIX}' field 'x y': {REASON_PATTERN}"
        ]

    def test_an_illegal_emitted_name_is_caught_even_when_the_declaration_conforms(self) -> None:
        """A conforming declaration reached under a non-conforming emitted name is still a violation.

        The check does not rest on every minting site being incapable of an illegal
        string (a property broken twice before), and the message leads with the
        string the reader can find in the export.
        """
        emitted = "Foo_app_my _pkg-2"
        reconciled = reconcile.ReconciledModel(
            data_types=[_primitive(emitted)],
            interfaces=[],
            swcs=[],
            connectors=[],
            data_type_source_names={("base_types", emitted): "Foo"},  # keyed `(layer, name)`
        )

        assert identifier_violations(reconciled, []) == [
            f"data type 'Foo' (renamed to '{emitted}' by this import): {REASON_PATTERN}"
        ]

    def test_a_conforming_reconciliation_reports_nothing_and_maps_nothing_back(self) -> None:
        """The normal case: renames happen, and no violation line is produced."""
        derived = f"u16{COMPU_METHOD_SUFFIX}"  # an application type: same package as the enum
        _registry, reconciled = _reconcile(data_types=[_enum("u16"), _application(derived)])

        assert reconciled.data_type_source_names == {("application_types", f"{derived}{RENAME_SUFFIX}"): derived}
        assert identifier_violations(reconciled, []) == []


# A connector keeps the export's own ``SHORT-NAME``, which a component rename does
# not touch, so phase B has no connector name to refuse; a duplicate connector name
# is ``map_compositions``' skip (``test_arxml_import_compositions.py``).


class TestTheShorteningPolicy:
    """A candidate over 128 characters keeps its suffix and loses stem."""

    def test_an_over_long_candidate_keeps_the_rename_token_last(self) -> None:
        original = "A" * MAX_IDENTIFIER_LENGTH

        candidate = free_name(original, taken=set())

        assert len(candidate) == MAX_IDENTIFIER_LENGTH
        assert candidate.endswith(RENAME_SUFFIX)
        assert not candidate.endswith(f"{RENAME_SUFFIX}_2"), (
            "the rename token must stay the final one, or the name stops reading as the derived form it is"
        )

    def test_a_truncation_collision_inserts_the_counter_before_the_suffix(self) -> None:
        original = "A" * MAX_IDENTIFIER_LENGTH
        first = free_name(original, taken=set())

        second = free_name(original, taken={first})

        assert second.endswith(f"_2{RENAME_SUFFIX}")
        assert len(second) == MAX_IDENTIFIER_LENGTH, "the stem is re-truncated so the total still fits"
        assert second != first

    def test_the_counter_is_unbounded_and_still_terminates(self) -> None:
        """At 100+ collisions each extra digit costs one more character of stem."""
        original = "A" * MAX_IDENTIFIER_LENGTH
        taken: set[str] = set()

        for _ in range(150):
            candidate = free_name(original, taken)
            assert check_identifier(candidate) is None
            assert candidate not in taken
            taken.add(candidate)

        assert len(taken) == 150

    def test_a_shortened_rename_is_still_reported(self) -> None:
        long_base = "B" * 120
        derived = f"{long_base}{COMPU_METHOD_SUFFIX}"  # an application type: same package as the enum

        registry, reconciled = _reconcile(data_types=[_enum(long_base), _application(derived)])

        renamed = next(name for name in _names(reconciled) if name != long_base)
        assert len(renamed) == MAX_IDENTIFIER_LENGTH
        assert any(renamed in line for line in registry.renames), (
            "a truncated rename is still a rename and must reach the report"
        )


class TestTheNamesTheImporterMints:
    """The names the importer concatenates itself are owned by the shared length cap.

    They are not reserved-name collisions, so the reconciliation pass does not own them.
    """

    def test_an_external_stub_name_that_has_to_be_disambiguated_still_conforms(self) -> None:
        """An external stub's ``<leaf>_ext<X>`` name, appended through ``fit_identifier``, conforms."""
        segment = "S" * MAX_IDENTIFIER_LENGTH

        name, rank = stub_name(segment, 0, set())

        assert check_identifier(name) is None
        assert (name, rank) == (f"{segment[:123]}_ext0", 0)

    def test_every_name_a_real_import_hands_out_conforms(self, tmp_path: Path) -> None:
        """Both halves of the rule, over everything a real import wrote.

        The ``{bare}_app_{package}`` fallback can fail only the **length** half: an
        ARXML ``SHORT-NAME`` is an ``IDENTIFIER--SIMPLE`` at source.
        """
        model_root, report = _import_into_fresh_project(tmp_path, DATA_TYPES_FIXTURE)

        # The ARXML-bound layers only: a requirement id is emitted into needs.json, never
        # as an AUTOSAR SHORT-NAME. The import writes no requirement, so the filter
        # excludes nothing today and states that scope.
        written = [
            Path(path).stem
            for path in report.written_paths
            if Path(path).parent.name != "requirements" and Path(path).parent.parent.name != "requirements"
        ]

        assert written
        for name in written:
            assert check_identifier(name) is None, name


class TestPhaseBNeverProducesADuplicate:
    """The one outcome that must not happen quietly."""

    def test_a_rename_that_creates_a_new_collision_is_resolved_too(self) -> None:
        """A rename that creates a new collision is resolved too: the pass re-scans to a fixed point."""
        first = f"u16{IMPL_SUFFIX}"
        second = f"{first}{RENAME_SUFFIX}{IMPL_SUFFIX}"

        _registry, reconciled = _reconcile(
            data_types=[_primitive("u16"), _primitive(first), _primitive(second)],
        )

        assert reserved_data_type_names({dt.name: dt for dt in reconciled.data_types}) == {}
        assert len(_names(reconciled)) == 3, "no rename may collapse two types onto one name"

    def test_a_four_way_bare_name_collision_resolves_rather_than_refusing(self) -> None:
        """Phase A does not run out of names: a fourth declaration gets a fourth distinct name.

        The counter form is the one :func:`free_name` uses, so the two phases mint
        into the shared namespace in one shape.
        """
        # One layer: rule 2 fires only for a collision inside one.
        registry = PathRegistry()
        emitted = [registry.register(f"/{group}/Types/X", "X", "application_types") for group in ("A", "B", "C", "D")]

        assert emitted == ["X", f"X{RENAME_SUFFIX}", f"X{RENAME_SUFFIX}_types", f"X_2{RENAME_SUFFIX}"]
        assert len(set(emitted)) == 4
        for name in emitted:
            assert check_identifier(name) is None

    def test_a_four_way_collision_imports_and_writes_four_distinct_types(self, tmp_path: Path) -> None:
        """The same shape end to end: four files, no refusal, nothing merged.

        *Nothing merged* is a claim about **rule 2** and a CROSS-KIND collision
        alone: two declarations of one name in one kind are merged into one element.
        The identifier refusals' no-output half is
        ``test_arxml_import_cli.py::TestEveryNameTheImportMintsConforms::``
        ``test_an_over_length_declared_name_is_refused_not_only_a_pattern_illegal_one``.
        """
        source = tmp_path / "four-way.arxml"
        source.write_text(_four_way_collision_arxml(), encoding="utf-8")
        make_empty_model_root(tmp_path)
        manifest = _write_manifest(tmp_path)

        report = run_import(str(source), str(manifest))

        # Each layer is its own name space: the implementation `X` keeps the name
        # beside the application `X`, and rule 2 renames only the two further
        # application declarations. Keyed by directory, so the same-name pair is two
        # entries. The counter form is not reachable end to end (one layer holds
        # three application kinds) and is pinned on the registry above.
        written = sorted(
            (Path(path).parent.name, Path(path).stem)
            for path in report.written_paths
            if Path(path).parent.name in {"implementation", "application"}
        )
        written_base = sorted(Path(path).stem for path in report.written_paths if Path(path).parent.name == "base")
        # `Octet` is the base type the implementation type rests on and `Level` the
        # record's and the array's member type -- neither is one of the four contenders.
        assert written == sorted(
            [
                ("implementation", "X"),
                ("application", "X"),
                ("application", f"X{RENAME_SUFFIX}"),
                ("application", f"X{RENAME_SUFFIX}_types"),
                ("application", "Level"),
            ]
        )
        assert written_base == ["Octet"]
        for name in [name for _, name in written] + written_base:
            assert check_identifier(name) is None

    def test_nothing_is_written_under_a_pre_reconciliation_name(self, tmp_path: Path) -> None:
        """The write loop runs after the pass, so no file is written under a name the final model lacks."""
        model_root, report = _import_into_fresh_project(tmp_path, DATA_TYPES_FIXTURE)

        # Asserted over every written data type: each file is written under a name
        # the final model declares.
        declared = {dt.name for dt in read_data_types(str(model_root))}
        written = {
            Path(path).stem
            for path in report.written_paths
            if Path(path).parent == model_root / "data_types" / "implementation"
        }
        assert written and written <= declared
        assert (model_element_path(model_root, f"uint16{IMPL_SUFFIX}", kind="value_type")).exists()


# ---------------------------------------------------------------------------
# The writer refuses a name that is not a legal AUTOSAR Identifier
# ---------------------------------------------------------------------------

#: A name failing the pattern half in the most ordinary way a human produces one.
BAD_NAME = "My SWC"

FLOAT32 = {
    "name": "float32",
    "kind": "base_type",
    "size": 32,
    "encoding": "IEEE754",
    "native_declaration": "float",
}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _seed_float32(root: Path) -> None:
    """Seed ``float32`` as a value type over the base type ``float32_base``.

    A base type may type no data element, and the writer refuses one that does.
    """
    write_model_element(root, {**FLOAT32, "name": "float32_base"})
    write_model_element(root, {"name": "float32", "kind": "value_type", "base": "float32_base"})


def _model_root(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    # Every layer ``generate_arxml()`` reads exists, so a guard is never skipped
    # for a missing directory.
    for subdir in arxml_layer_directories():
        (root / subdir).mkdir(parents=True, exist_ok=True)
    return root


def _swc(name: str, ports: list | None = None, runnables: list | None = None) -> dict:
    """One application component declaring ``no_support`` (a component without it is refused)."""
    return {
        "name": name,
        "type": "application",
        "ports": ports or [],
        "runnables": runnables or [],
        "handle_termination_and_restart": "no_support",
    }


def _seed_swc(root: Path, swc: dict) -> None:
    _write_yaml(root / "swc" / "group" / f"{swc['name'].replace('/', '_')}.yaml", swc)


def _sr_interface(name: str = "ISensor", element_name: str = "value") -> dict:
    return {
        "name": name,
        "kind": "sender_receiver",
        "data_elements": [{"name": element_name, "implementation_type": "float32"}],
    }


def _refused(root: Path, output: Path) -> str:
    """Generate, expect a refusal, and assert no output file was left behind.

    A refused model leaves nothing on disk, the guards that run at emit time (enum
    literals, record fields) included, so the assertion is made for all of them.
    """
    with pytest.raises(ValueError) as excinfo:
        generate_arxml(str(root), str(output))
    assert not output.exists(), "a refused model must leave no output file"
    return str(excinfo.value)


class TestTheConnectorGuardThatDidNotExist:
    """Connector names are guarded by ``_check_connector_names``.

    ``connector.name`` becomes ``ASSEMBLY-SW-CONNECTOR``'s ``SHORT-NAME``; the guard
    is a sibling of the other three ``_check_*_names`` guards rather than part of
    ``_check_connectors()``, which is the *compatibility* gate.
    """

    @staticmethod
    def _two_swc_model(tmp_path: Path, connector_name: str) -> Path:
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(root / "interfaces" / "i.yaml", _sr_interface())
        _seed_swc(root, _swc("Producer", ports=[{"name": "Out", "direction": "provide", "interface": "ISensor"}]))
        _seed_swc(root, _swc("Consumer", ports=[{"name": "In", "direction": "require", "interface": "ISensor"}]))
        _write_yaml(
            root / "connectors" / "c.yaml",
            {
                "name": connector_name,
                "from": {"swc": "Producer", "port": "Out"},
                "to": {"swc": "Consumer", "port": "In"},
            },
        )
        # A connector is drawn inside a *declared* composition, so a model with
        # connectors needs a root for the writer to place them.
        _root_composition(root, ["Producer", "Consumer"])
        return root

    def test_a_hand_edited_connector_name_is_refused(self, tmp_path: Path) -> None:
        """A consistent connector with an invalid ``SHORT-NAME`` is refused for the name."""
        root = self._two_swc_model(tmp_path, "My Connector")

        message = _refused(root, tmp_path / "out.arxml")

        assert "'My Connector'" in message
        assert REASON_PATTERN in message
        assert "connectors/" in message

    def test_the_same_model_generates_with_a_conforming_connector_name(self, tmp_path: Path) -> None:
        """The control: nothing but the name is wrong with that model."""
        root = self._two_swc_model(tmp_path, "MyConnector")

        text = Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).read_text(encoding="utf-8")

        assert "<SHORT-NAME>MyConnector</SHORT-NAME>" in text

    def test_it_runs_before_the_compatibility_check(self, tmp_path: Path) -> None:
        """The guard runs before the compatibility check and before anything is emitted.

        A connector both badly named and inconsistent is refused for the name.
        """
        root = self._two_swc_model(tmp_path, "My Connector")
        _write_yaml(
            root / "connectors" / "c.yaml",
            {
                "name": "My Connector",
                "from": {"swc": "Producer", "port": "Out"},
                "to": {"swc": "Nonexistent", "port": "In"},
            },
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert "'My Connector'" in message
        assert "Connector consistency check failed" not in message

    def test_a_connector_less_model_is_still_not_connector_checked(self, tmp_path: Path) -> None:
        """A model with neither connectors nor compositions generates; the guard is a no-op on an empty list.

        The emptiness is measured on the tag: the writer invents no composition, so
        the document carries no ``COMPOSITION-SW-COMPONENT-TYPE`` element.
        """
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(root / "interfaces" / "i.yaml", _sr_interface())
        _seed_swc(root, _swc("Producer", ports=[{"name": "Out", "direction": "provide", "interface": "ISensor"}]))

        text = Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).read_text(encoding="utf-8")

        assert COMPOSITION_SWC_TAG not in text
        assert _check_connector_names([]) is None


class TestEveryGuardRefusesADeclaredName:
    """One case per domain, including the members no ``_check_*_names`` sees."""

    def test_a_data_type_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _write_yaml(model_element_path(root, "t", kind="base_type"), {**FLOAT32, "name": "my type"})

        message = _refused(root, tmp_path / "out.arxml")

        assert "'my type'" in message
        assert "data_types/" in message

    def test_an_interface_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(root / "interfaces" / "i.yaml", _sr_interface(name="I-Sensor"))

        assert "'I-Sensor'" in _refused(root, tmp_path / "out.arxml")

    def test_a_data_element_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(root / "interfaces" / "i.yaml", _sr_interface(element_name="my value"))

        message = _refused(root, tmp_path / "out.arxml")

        assert "'my value'" in message
        assert "Interface 'ISensor' data element name" in message

    def test_an_operation_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _write_yaml(
            root / "interfaces" / "i.yaml",
            {"name": "ICtl", "kind": "client_server", "operations": [{"name": "do it", "arguments": []}]},
        )

        assert "'do it'" in _refused(root, tmp_path / "out.arxml")

    def test_an_operation_argument_name(self, tmp_path: Path) -> None:
        """A legal interface name with an illegal argument is still refused.

        The argument is the deepest nested ``SHORT-NAME`` the interface guard owns."""
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(
            root / "interfaces" / "i.yaml",
            {
                "name": "ICtl",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "Do",
                        "arguments": [{"name": "in.value", "direction": "in", "implementation_type": "float32"}],
                    }
                ],
            },
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert "'in.value'" in message
        assert "operation 'Do' argument name" in message

    def test_a_swc_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_swc(root, _swc(BAD_NAME))

        message = _refused(root, tmp_path / "out.arxml")

        assert f"'{BAD_NAME}'" in message
        assert "swc/" in message

    def test_a_port_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(root / "interfaces" / "i.yaml", _sr_interface())
        _seed_swc(root, _swc("A", ports=[{"name": "my port", "direction": "provide", "interface": "ISensor"}]))

        assert "'my port'" in _refused(root, tmp_path / "out.arxml")

    def test_a_runnable_name(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", runnables=[{"name": "9run"}]))

        assert "'9run'" in _refused(root, tmp_path / "out.arxml")

    def test_an_enum_literal_name(self, tmp_path: Path) -> None:
        """Enum literals are checked at emit time by ``_check_enum_literals``, not ``_check_data_type_names``."""
        root = _model_root(tmp_path)
        _write_yaml(
            model_element_path(root, "e", kind="enum"),
            {"name": "Gear_T", "kind": "enum", "literals": [{"name": "1st", "value": 0}]},
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert "'1st'" in message
        assert "Gear_T" in message

    def test_a_record_field_name(self, tmp_path: Path) -> None:
        """The record twin, validated by ``_check_record_fields``."""
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(
            model_element_path(root, "r", kind="record"),
            {"name": "Pose_T", "kind": "record", "fields": [{"name": "x pos", "type": "float32"}]},
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert "'x pos'" in message
        assert "Pose_T" in message


class TestEveryGeneratedFormIsRefusedToo:
    """The declared name does not predict the emitted one: twelve forms lengthen it.

    ``_InternalBehavior`` (17 characters) and the two event prefixes are the ones no
    declared-name check can catch. Eight are single-name forms; the four joint ones
    have classes of their own (:class:`TestTheFourGuardsAgreeAboutTheInitConstant`,
    :class:`TestTheFourGuardsAgreeAboutTheAccessPoints`). ``{x}_Unit``, ``{x}_Compu``
    and ``{x}_Range`` are reserved per *declared field*; their reporting half is also
    pinned in ``test_validator_identifier.py``. A base type and a value type are
    charged for **nothing**: their former forms are retired.
    """

    def test_a_base_type_has_no_generated_form_to_refuse(self, tmp_path: Path) -> None:
        """A base type generates no form: a name that would have overflowed one generates cleanly."""
        name = "P" * (MAX_IDENTIFIER_LENGTH - len(IMPL_SUFFIX) + 1)
        root = _model_root(tmp_path)
        _write_yaml(model_element_path(root, "t", kind="base_type"), {**FLOAT32, "name": name})
        assert check_identifier(name) is None

        text = Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).read_text(encoding="utf-8")

        assert f"<SHORT-NAME>{name}</SHORT-NAME>" in text
        assert f"{name}{IMPL_SUFFIX}" not in text
        assert f"{name}{APP_VIEW_SUFFIX}" not in text

    def test_a_value_type_has_no_generated_form_to_refuse(self, tmp_path: Path) -> None:
        """A value type at the **full** 128 characters generates: it is charged for no retired ``_AppView``."""
        name = "V" * MAX_IDENTIFIER_LENGTH
        root = _model_root(tmp_path)
        _write_yaml(model_element_path(root, "b", kind="base_type"), FLOAT32)
        _write_yaml(
            model_element_path(root, "t", kind="value_type"), {"name": name, "kind": "value_type", "base": "float32"}
        )
        assert check_identifier(name) is None

        text = Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).read_text(encoding="utf-8")

        assert f"<SHORT-NAME>{name}</SHORT-NAME>" in text
        assert f"{name}{APP_VIEW_SUFFIX}" not in text

    def test_an_application_types_declared_forms_are_refused(self, tmp_path: Path) -> None:
        """The three field-scoped forms, through ``create_data_type``.

        The scope is the *declared field*, not the kind: declaring all three is
        charged for each at a length that overflows it, and the same name declaring
        **none** is written.
        """
        root = _model_root(tmp_path)
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        declared: dict[str, dict[str, object]] = {
            UNIT_SUFFIX: {"unit": "km/h"},
            COMPU_SUFFIX: {"conversion": {"factor": 1.0, "offset": 0.0}},
            RANGE_SUFFIX: {"range": {"min": 0.0, "max": 1.0}},
        }
        for suffix, fields in declared.items():
            name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
            assert check_identifier(name) is None, "the DECLARED name is legal; only the derived one is not"

            with pytest.raises(ValueError) as excinfo:
                create_data_type(str(root), name, "application_type", auditor=auditor, **fields)

            assert f"{name}{suffix}" in str(excinfo.value)
            assert not list((root / "data_types").rglob("*.yaml")), "a refusal leaves no file"

        # The inversion: declaring NONE of the fields generates no form, so the
        # name is ordinary. A guard scoped to the kind would refuse this.
        longest = "A" * (MAX_IDENTIFIER_LENGTH - len(UNIT_SUFFIX) + 1)
        create_data_type(str(root), longest, "application_type", auditor=auditor)

        assert [path.relative_to(root).as_posix() for path in (root / "data_types").rglob("*.yaml")] == [
            f"data_types/application/{longest}.yaml"
        ]

    def test_the_compu_method_form_of_an_enum(self, tmp_path: Path) -> None:
        name = "E" * (MAX_IDENTIFIER_LENGTH - len(COMPU_METHOD_SUFFIX) + 1)
        root = _model_root(tmp_path)
        _write_yaml(
            model_element_path(root, "t", kind="enum"),
            {"name": name, "kind": "enum", "literals": [{"name": "A", "value": 0}]},
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert f"{name}{COMPU_METHOD_SUFFIX}" in message
        assert f"{name}{UNIT_SUFFIX}" not in message, "an enum declares no unit — see the field scope"

    def test_the_implementation_form_of_a_component(self, tmp_path: Path) -> None:
        name = "S" * (MAX_IDENTIFIER_LENGTH - len(IMPLEMENTATION_SUFFIX) + 1)
        root = _model_root(tmp_path)
        _seed_swc(root, _swc(name))

        assert f"{name}{IMPLEMENTATION_SUFFIX}" in _refused(root, tmp_path / "out.arxml")

    def test_a_112_character_component_is_refused_for_its_internal_behavior(self, tmp_path: Path) -> None:
        """At 112 characters ``_Implementation`` is legal and ``_InternalBehavior`` is not."""
        name = "S" * 112
        assert check_identifier(f"{name}{IMPLEMENTATION_SUFFIX}") is None
        root = _model_root(tmp_path)
        _seed_swc(root, _swc(name))

        message = _refused(root, tmp_path / "out.arxml")

        assert f"{name}{INTERNAL_BEHAVIOR_SUFFIX}" in message
        assert f"{name}{IMPLEMENTATION_SUFFIX}'" not in message

    def test_the_timing_event_form(self, tmp_path: Path) -> None:
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) + 1)
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", runnables=[{"name": runnable, "events": [{"type": "timing", "period": 0.01}]}]))

        assert f"{TIMING_EVENT_PREFIX}{runnable}" in _refused(root, tmp_path / "out.arxml")

    def test_the_init_event_form(self, tmp_path: Path) -> None:
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(INIT_EVENT_PREFIX) + 1)
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", runnables=[{"name": runnable, "events": [{"type": "init"}]}]))

        assert f"{INIT_EVENT_PREFIX}{runnable}" in _refused(root, tmp_path / "out.arxml")

    def test_an_event_name_that_only_overflows_after_the_retroactive_counter(self, tmp_path: Path) -> None:
        """The check runs on the name the writer actually emits.

        ``_unique_event_names`` renames the **first** event retroactively, so a legal
        127-character ``TMT_`` name becomes 129 characters once a second event lands
        on the runnable; the guard reads the final list.
        """
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) - 1)
        assert check_identifier(f"{TIMING_EVENT_PREFIX}{runnable}") is None
        root = _model_root(tmp_path)
        _seed_swc(
            root,
            _swc(
                "A",
                runnables=[
                    {
                        "name": runnable,
                        "events": [{"type": "timing", "period": 0.01}, {"type": "timing", "period": 0.02}],
                    }
                ],
            ),
        )

        message = _refused(root, tmp_path / "out.arxml")

        assert f"{TIMING_EVENT_PREFIX}{runnable}_0" in message
        assert f"{TIMING_EVENT_PREFIX}{runnable}_1" in message

    def test_the_same_runnable_with_one_event_still_generates(self, tmp_path: Path) -> None:
        """The control for the case above: no counter, no overflow, no refusal."""
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) - 1)
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", runnables=[{"name": runnable, "events": [{"type": "timing", "period": 0.01}]}]))

        assert Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).exists()

    def test_an_event_form_is_not_refused_when_the_runnable_name_already_is(self, tmp_path: Path) -> None:
        """One root cause, one offender: a bad runnable name is not reported again as its event.

        The name fails the **pattern** half, since ``TMT_9run`` is itself legal.
        """
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", runnables=[{"name": "my run", "events": [{"type": "timing", "period": 0.01}]}]))
        assert check_identifier(f"{TIMING_EVENT_PREFIX}my run") == REASON_PATTERN

        message = _refused(root, tmp_path / "out.arxml")

        assert "'my run'" in message
        assert TIMING_EVENT_PREFIX not in message, "the emitted event name is the same root cause, reported once"


class TestTheWriterAndTheValidatorAgree:
    """Every refusal ``generate_arxml`` can raise is also a validator check.

    Agreement is on the offending **name** and the shared **reason** constant, not
    on the sentence: the writer batches a guard's offenders into one ``ValueError``
    while the validator emits one issue each."""

    @staticmethod
    def _both(root: Path, tmp_path: Path) -> tuple[str, list[str]]:
        message = _refused(root, tmp_path / "out.arxml")
        return message, check_identifier_conformance(str(root)).issues

    def test_a_declared_name_fails_both_with_the_same_reason(self, tmp_path: Path) -> None:
        root = _model_root(tmp_path)
        _seed_swc(root, _swc(BAD_NAME))

        message, issues = self._both(root, tmp_path)

        assert len(issues) == 1, issues
        assert f"'{BAD_NAME}'" in message and f"'{BAD_NAME}'" in issues[0]
        assert REASON_PATTERN in message and REASON_PATTERN in issues[0]

    def test_a_generated_form_fails_both_with_the_same_reason(self, tmp_path: Path) -> None:
        name = "S" * 112
        root = _model_root(tmp_path)
        _seed_swc(root, _swc(name))

        message, issues = self._both(root, tmp_path)

        assert len(issues) == 1, issues
        emitted = f"{name}{INTERNAL_BEHAVIOR_SUFFIX}"
        assert emitted in message and emitted in issues[0]
        assert REASON_LENGTH in message and REASON_LENGTH in issues[0]

    def test_the_writer_batches_where_the_validator_lists(self, tmp_path: Path) -> None:
        """Two offenders in one guard's domain are one ``ValueError`` and two issues.

        The deliberate difference in shape, pinned so nobody 'fixes' it into message equality."""
        root = _model_root(tmp_path)
        _seed_swc(root, _swc("A", ports=[{"name": "p 1", "direction": "provide", "interface": "I"}]))
        _seed_swc(root, _swc("B", runnables=[{"name": "r 2"}]))

        message, issues = self._both(root, tmp_path)

        assert "'p 1'" in message and "'r 2'" in message
        assert len(issues) == 2, issues


class TestAConformingModelIsUnaffected:
    """No name in the goldens' models violates the rule, so their output does not move.

    A moved golden is a finding to report, not a golden to regenerate
    (``fixtures/arxml_golden/README.md``)."""

    @pytest.mark.parametrize("name", sorted(GOLDEN_MODEL_ROOTS))
    def test_the_populated_goldens_are_byte_identical(self, name: str, tmp_path: Path) -> None:
        output = generate_arxml(str(GOLDEN_MODEL_ROOTS[name]), str(tmp_path / f"{name}.arxml"))

        assert Path(output).read_text(encoding="utf-8") == read_golden(name)

    def test_the_empty_golden_is_byte_identical(self, tmp_path: Path) -> None:
        """The degenerate model every guard sees as an empty list, the connector guard included."""
        output = generate_arxml(str(make_empty_model_root(tmp_path)), str(tmp_path / "empty.arxml"))

        assert Path(output).read_text(encoding="utf-8") == read_golden("empty-4.2.2")


# ---------------------------------------------------------------------------
# The write path refuses before anything reaches disk
# ---------------------------------------------------------------------------


def _refused_write(call, root: Path) -> str:
    """Call *call*, expect a refusal, and assert the model tree is unchanged.

    A refused call must leave no state ``check-model`` refuses.
    """
    before = (
        sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix())
        if root.exists()
        else []
    )
    with pytest.raises(ValueError) as excinfo:
        call()
    after = (
        sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix())
        if root.exists()
        else []
    )
    assert after == before, "a refused mutation must write nothing"
    return str(excinfo.value)


def _writable_root(tmp_path: Path) -> Path:
    """A model root the mutations create their own layer directories under."""
    root = tmp_path / "model"
    root.mkdir(parents=True)
    return root


class TestEveryCreatorRefusesANonConformingName:
    """The five top-level creators — note there are **two** interface creators.

    ``create_cs_interface`` is separate from ``create_interface``, so a guard on
    the sender-receiver one alone would leave the client-server surface open.
    """

    def test_create_swc(self, tmp_path: Path) -> None:
        """``create_swc(model_root, "My SWC")`` is refused and writes nothing."""
        root = _writable_root(tmp_path)

        message = _refused_write(lambda: create_swc(str(root), BAD_NAME, "sensors"), root)

        assert f"'{BAD_NAME}'" in message
        assert REASON_PATTERN in message

    def test_create_interface(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_interface(str(root), "I Sensor", [{"name": "value", "implementation_type": "float32"}]), root
        )

        assert "'I Sensor'" in message

    def test_create_cs_interface(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(lambda: create_cs_interface(str(root), "I Ctl", [{"name": "Do"}]), root)

        assert "'I Ctl'" in message

    def test_create_data_type(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_data_type(str(root), name="my type", kind="base_type", size=32, encoding="IEEE754"), root
        )

        assert "'my type'" in message

    def test_create_connector(self, tmp_path: Path) -> None:
        """Refused on the name alone, before the endpoints are even resolved."""
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_connector(str(root), "My Connector", "Producer", "Out", "Consumer", "In"), root
        )

        assert "'My Connector'" in message
        assert REASON_PATTERN in message


class TestTheNestedNamesOneCallWrites:
    """Several mutations write more than one ``SHORT-NAME`` per call.

    A legal interface name with an illegal operation argument is still an invalid
    document, so each nested name is refused at write time too.
    """

    def test_a_sender_receiver_data_element(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_interface(str(root), "ISensor", [{"name": "my value", "implementation_type": "float32"}]),
            root,
        )

        assert "'my value'" in message
        assert "Interface 'ISensor' data element name" in message

    def test_a_client_server_operation(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(lambda: create_cs_interface(str(root), "ICtl", [{"name": "do it"}]), root)

        assert "'do it'" in message

    def test_a_client_server_operation_argument(self, tmp_path: Path) -> None:
        """The deepest nested name the client-server shape carries."""
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_cs_interface(
                str(root),
                "ICtl",
                [
                    {
                        "name": "Do",
                        "arguments": [{"name": "in.value", "direction": "in", "implementation_type": "float32"}],
                    }
                ],
            ),
            root,
        )

        assert "'in.value'" in message
        assert "operation 'Do' argument name" in message

    def test_an_enum_literal(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_data_type(str(root), name="Gear_T", kind="enum", literals=[{"name": "1st", "value": 0}]),
            root,
        )

        assert "'1st'" in message
        assert "Enum 'Gear_T' literal name" in message

    def test_a_record_field(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_data_type(
                str(root), name="Pose_T", kind="record", fields=[{"name": "x pos", "type": "float32"}]
            ),
            root,
        )

        assert "'x pos'" in message
        assert "Record 'Pose_T' field name" in message


class TestTheMemberAddingMutations:
    """``add_*`` writes into a file that already exists.

    So "wrote nothing" here means the SWC file is byte-identical afterwards."""

    @staticmethod
    def _swc_with_runnable(tmp_path: Path, runnable: str = "Step") -> tuple[Path, str]:
        root = _writable_root(tmp_path)
        swc_file = create_swc(str(root), "Sensor", "sensors")
        add_runnable(str(root), "Sensor", runnable)
        return root, swc_file

    def test_add_port(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)
        swc_file = create_swc(str(root), "Sensor", "sensors")
        before = Path(swc_file).read_text(encoding="utf-8")

        message = _refused_write(lambda: add_port(str(root), "Sensor", "my port", "provide", "ISensor"), root)

        assert "'my port'" in message
        assert Path(swc_file).read_text(encoding="utf-8") == before

    def test_add_runnable(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)
        swc_file = create_swc(str(root), "Sensor", "sensors")
        before = Path(swc_file).read_text(encoding="utf-8")

        message = _refused_write(lambda: add_runnable(str(root), "Sensor", "9run"), root)

        assert "'9run'" in message
        assert Path(swc_file).read_text(encoding="utf-8") == before

    def test_add_timing_event_on_a_runnable_whose_event_name_overflows(self, tmp_path: Path) -> None:
        """A runnable legal when written is refused an event whose ``TMT_`` name overflows."""
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) + 1)
        root, swc_file = self._swc_with_runnable(tmp_path, runnable)
        assert check_identifier(runnable) is None
        before = Path(swc_file).read_text(encoding="utf-8")

        message = _refused_write(lambda: add_timing_event(str(root), "Sensor", runnable, 0.01), root)

        assert f"{TIMING_EVENT_PREFIX}{runnable}" in message
        assert REASON_LENGTH in message
        assert Path(swc_file).read_text(encoding="utf-8") == before

    def test_add_init_event_on_a_runnable_whose_event_name_overflows(self, tmp_path: Path) -> None:
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(INIT_EVENT_PREFIX) + 1)
        root, swc_file = self._swc_with_runnable(tmp_path, runnable)
        before = Path(swc_file).read_text(encoding="utf-8")

        message = _refused_write(lambda: add_init_event(str(root), "Sensor", runnable), root)

        assert f"{INIT_EVENT_PREFIX}{runnable}" in message
        assert Path(swc_file).read_text(encoding="utf-8") == before

    def test_the_same_runnable_one_character_shorter_still_takes_an_event(self, tmp_path: Path) -> None:
        """The control: the guard is the length of the *emitted* name, not a new rule about runnables."""
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX))
        root, swc_file = self._swc_with_runnable(tmp_path, runnable)

        add_timing_event(str(root), "Sensor", runnable, 0.01)

        assert "timing" in Path(swc_file).read_text(encoding="utf-8")


class TestTheGeneratedFormsAtWriteTime:
    """A declared name inside the limit can still produce an emitted one outside it.

    The write path says so when the name is chosen."""

    def test_a_component_whose_internal_behavior_form_is_too_long(self, tmp_path: Path) -> None:
        """At 112 characters ``_Implementation`` is legal and ``_InternalBehavior`` is not."""
        name = "S" * 112
        assert check_identifier(f"{name}{IMPLEMENTATION_SUFFIX}") is None
        root = _writable_root(tmp_path)

        message = _refused_write(lambda: create_swc(str(root), name, "sensors"), root)

        assert f"{name}{INTERNAL_BEHAVIOR_SUFFIX}" in message
        assert f"at most {MAX_IDENTIFIER_LENGTH - len(INTERNAL_BEHAVIOR_SUFFIX)} characters" in message

    def test_a_component_whose_implementation_form_is_too_long(self, tmp_path: Path) -> None:
        name = "S" * (MAX_IDENTIFIER_LENGTH - len(IMPLEMENTATION_SUFFIX) + 1)
        root = _writable_root(tmp_path)

        message = _refused_write(lambda: create_swc(str(root), name, "sensors"), root)

        assert f"{name}{IMPLEMENTATION_SUFFIX}" in message

    def test_a_base_type_at_the_retired_impl_length_is_accepted(self, tmp_path: Path) -> None:
        """A base type generates no form, so the write path accepts it at the retired ``_impl`` length."""
        name = "P" * (MAX_IDENTIFIER_LENGTH - len(IMPL_SUFFIX) + 1)
        root = _writable_root(tmp_path)

        create_data_type(str(root), name=name, kind="base_type", size=32, encoding="IEEE754")

        assert [dt.name for dt in read_layer(str(root), "base_types")] == [name]

    def test_an_enum_whose_compu_method_form_is_too_long(self, tmp_path: Path) -> None:
        name = "E" * (MAX_IDENTIFIER_LENGTH - len(COMPU_METHOD_SUFFIX) + 1)
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_data_type(str(root), name=name, kind="enum", literals=[{"name": "A", "value": 0}]), root
        )

        assert f"{name}{COMPU_METHOD_SUFFIX}" in message
        assert f"{name}{IMPL_SUFFIX}" not in message, "an enum has no implementation twin — see the kind scope"

    def test_a_value_type_at_the_retired_app_view_length_is_accepted(self, tmp_path: Path) -> None:
        """A value type at the retired ``_AppView`` length is written: no retired form is charged.

        The file it writes proves the guard is not merely silent about the emitted form.
        """
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(APP_VIEW_SUFFIX) + 1)
        root = _writable_root(tmp_path)
        create_data_type(str(root), name="float32", kind="base_type", size=32, encoding="IEEE754")

        create_data_type(str(root), name=name, kind="value_type", base="float32")

        assert [dt.name for dt in read_data_types(str(root))] == [name]
        assert [dt.name for dt in read_layer(str(root), "base_types")] == ["float32"]

    def test_the_compu_method_collision_is_refused_in_both_directions(self, tmp_path: Path) -> None:
        """The ``_CompuMethod`` reservation holds whichever side is created first.

        Forward is the derived name created beside an existing enum; reverse is the
        enum created beside a name legal until that moment. The occupant is an
        application type, of the enum's own package: a form collides inside its
        owner's package only.
        """
        root = _writable_root(tmp_path)
        create_data_type(str(root), name="Mode", kind="enum", literals=[{"name": "A", "value": 0}])

        forward = _refused_write(
            lambda: create_data_type(str(root), name=f"Mode{COMPU_METHOD_SUFFIX}", kind="application_type"),
            root,
        )

        other = _writable_root(tmp_path / "other")
        create_data_type(str(other), name=f"Mode{COMPU_METHOD_SUFFIX}", kind="application_type")
        reverse = _refused_write(
            lambda: create_data_type(str(other), name="Mode", kind="enum", literals=[{"name": "A", "value": 0}]),
            other,
        )

        assert "COMPU-METHOD auto-generated for 'Mode'" in forward
        assert "collides with the existing data type" in reverse


class TestARefusedCallLeavesNothingToUndo:
    """The recoverability invariant, at the layer where it is cheapest to hold.

    A refused call must leave no state ``check-model`` refuses and no record of a
    write that did not happen; hence "no file" and "no audit entry" are asserted.
    """

    @staticmethod
    def _auditor(tmp_path: Path) -> tuple[AuditLog, Path]:
        log_path = tmp_path / "dist" / "audit.jsonl"
        return AuditLog(str(log_path)), log_path

    def test_a_refused_data_type_leaves_no_file_and_no_audit_entry(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)
        auditor, log_path = self._auditor(tmp_path)

        _refused_write(
            lambda: create_data_type(
                str(root), name="my type", kind="base_type", size=32, encoding="IEEE754", auditor=auditor
            ),
            root,
        )

        assert not (root / "data_types").exists()
        assert not log_path.exists(), "an unwritten mutation must not be audited"

    def test_a_refused_swc_leaves_no_file_and_no_audit_entry(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)
        auditor, log_path = self._auditor(tmp_path)

        _refused_write(lambda: create_swc(str(root), BAD_NAME, "sensors", "application", auditor), root)

        assert not (root / "swc").exists()
        assert not log_path.exists()


class TestTheWritePathAndTheValidatorSayTheSameThing:
    """One rule, two readers, one wording for the name the caller must change.

    The reason constant and the suggested name are shared. The *sentence* differs
    deliberately: the validator points at a file that exists, and the write path at
    an argument it has just refused to write.
    """

    def test_a_declared_name(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)
        refusal = _refused_write(lambda: create_swc(str(root), BAD_NAME, "sensors"), root)

        _seed_swc(_model_root(tmp_path / "hand_edited"), _swc(BAD_NAME))
        issues = check_identifier_conformance(str(tmp_path / "hand_edited" / "model")).issues

        assert len(issues) == 1, issues
        assert REASON_PATTERN in refusal and REASON_PATTERN in issues[0]
        assert suggest_identifier(BAD_NAME) in refusal and suggest_identifier(BAD_NAME) in issues[0]

    def test_a_generated_form(self, tmp_path: Path) -> None:
        name = "S" * 112
        root = _writable_root(tmp_path)
        refusal = _refused_write(lambda: create_swc(str(root), name, "sensors"), root)

        _seed_swc(_model_root(tmp_path / "hand_edited"), _swc(name))
        issues = check_identifier_conformance(str(tmp_path / "hand_edited" / "model")).issues

        budget = MAX_IDENTIFIER_LENGTH - len(INTERNAL_BEHAVIOR_SUFFIX)
        assert len(issues) == 1, issues
        assert REASON_LENGTH in refusal and REASON_LENGTH in issues[0]
        assert f"at most {budget} characters" in refusal and f"at most {budget} characters" in issues[0]


# ---------------------------------------------------------------------------
# The eighth generated form, and the four guards that must agree
# ---------------------------------------------------------------------------


def _valued_interface(name: str, element_name: str, init: object | None = None) -> dict:
    """One sender-receiver interface with one data element, optionally valued."""
    element: dict = {"name": element_name, "implementation_type": "float32"}
    if init is not None:
        element["init"] = init
    return {"name": name, "kind": "sender_receiver", "data_elements": [element]}


def _interfaces_model(tmp_path: Path, *interfaces: dict) -> Path:
    """A model root carrying ``float32`` and *interfaces*, one file each.

    ``float32`` is a value type over ``float32_base``: a data element may not be
    typed by a base type."""
    root = _model_root(tmp_path)
    write_model_element(root, {**FLOAT32, "name": "float32_base"})
    write_model_element(root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
    for iface in interfaces:
        _write_yaml(root / "interfaces" / f"{iface['name']}.yaml", iface)
    return root


class TestTheFourGuardsAgreeAboutTheInitConstant:
    """``{Interface}_{Element}_Init`` is derived from **two** declared names, with a *joint* budget.

    ``_`` and ``_Init`` cost six of the 128, so the two names get 122 between them.
    Four guards agree about it — ``check_identifier_conformance`` reports,
    ``_check_interface_names`` refuses at generation time, and ``create_interface``
    and ``write_element`` refuse before anything reaches disk — on the offending
    **pair** and the shared **reason** constant, never on a rendered sentence.
    """

    #: A pair whose two names sum to exactly the joint budget, so the constant is
    #: exactly ``MAX_IDENTIFIER_LENGTH`` characters — one more and every guard
    #: refuses.
    AT_THE_LIMIT = ("I" * 61, "E" * 61)
    #: The same pair one character over.
    OVER = ("I" * 62, "E" * 61)

    def test_a_pair_on_the_joint_budget_passes_all_four_guards(self, tmp_path: Path) -> None:
        interface_name, element_name = self.AT_THE_LIMIT
        assert len(interface_name) + len(element_name) == JOINT_BUDGET
        assert len(init_constant_name(interface_name, element_name)) == MAX_IDENTIFIER_LENGTH

        root = _interfaces_model(tmp_path, _valued_interface(interface_name, element_name, init=1.5))
        assert check_identifier_conformance(str(root)).issues == []
        generate_arxml(str(root), str(tmp_path / "ok.arxml"))

        written = _writable_root(tmp_path / "written")
        create_interface(
            str(written), interface_name, [{"name": element_name, "implementation_type": "float32", "init": 1.5}]
        )

        generic = _writable_root(tmp_path / "generic")
        write_element(
            str(generic),
            f"interfaces/{interface_name}.yaml",
            SRInterface(
                name=interface_name,
                kind="sender_receiver",
                data_elements=[{"name": element_name, "implementation_type": "float32", "init": 1.5}],
            ),
        )

    def test_one_character_over_is_reported_and_refused_by_all_four(self, tmp_path: Path) -> None:
        interface_name, element_name = self.OVER
        emitted = init_constant_name(interface_name, element_name)
        assert check_identifier(interface_name) is None and check_identifier(element_name) is None
        assert check_identifier(emitted) == REASON_LENGTH

        root = _interfaces_model(tmp_path, _valued_interface(interface_name, element_name, init=1.5))
        issues = check_identifier_conformance(str(root)).issues
        generation = _refused(root, tmp_path / "out.arxml")

        written = _writable_root(tmp_path / "written")
        mutation = _refused_write(
            lambda: create_interface(
                str(written), interface_name, [{"name": element_name, "implementation_type": "float32"}]
            ),
            written,
        )

        generic = _writable_root(tmp_path / "generic")
        element_writer = _refused_write(
            lambda: write_element(
                str(generic),
                f"interfaces/{interface_name}.yaml",
                SRInterface(
                    name=interface_name,
                    kind="sender_receiver",
                    data_elements=[{"name": element_name, "implementation_type": "float32"}],
                ),
            ),
            generic,
        )

        assert len(issues) == 1, issues
        for message in (issues[0], generation, mutation, element_writer):
            assert emitted in message, message
            assert f"'{interface_name}'" in message and f"'{element_name}'" in message, message
            assert REASON_LENGTH in message, message
            assert f"{JOINT_BUDGET} characters between them" in message, message

    def test_all_four_suggest_shortening_the_longer_name(self, tmp_path: Path) -> None:
        interface_name, element_name = "I" * 100, "E" * 30
        root = _interfaces_model(tmp_path, _valued_interface(interface_name, element_name))
        written = _writable_root(tmp_path / "written")
        generic = _writable_root(tmp_path / "generic")

        messages = [
            check_identifier_conformance(str(root)).issues[0],
            _refused(root, tmp_path / "out.arxml"),
            _refused_write(
                lambda: create_interface(
                    str(written), interface_name, [{"name": element_name, "implementation_type": "float32"}]
                ),
                written,
            ),
            _refused_write(
                lambda: write_element(
                    str(generic),
                    f"interfaces/{interface_name}.yaml",
                    SRInterface(
                        name=interface_name,
                        kind="sender_receiver",
                        data_elements=[{"name": element_name, "implementation_type": "float32"}],
                    ),
                ),
                generic,
            ),
        ]

        for message in messages:
            assert f"'{interface_name}' to at most 92 characters" in message, message

    def test_a_tie_suggests_the_data_element_everywhere(self, tmp_path: Path) -> None:
        """The name a designer changes with less blast radius."""
        interface_name, element_name = "I" * 62, "E" * 62
        root = _interfaces_model(tmp_path, _valued_interface(interface_name, element_name))
        written = _writable_root(tmp_path / "written")

        report = check_identifier_conformance(str(root)).issues[0]
        generation = _refused(root, tmp_path / "out.arxml")
        mutation = _refused_write(
            lambda: create_interface(
                str(written), interface_name, [{"name": element_name, "implementation_type": "float32"}]
            ),
            written,
        )

        for message in (report, generation, mutation):
            assert f"'{element_name}' to at most 60 characters" in message, message

    def test_the_write_path_clamps_the_suggestion(self, tmp_path: Path) -> None:
        """Two 125-character names overflow by 128, more than either is long: the suggestion is clamped.

        Unclamped, ``len(name) - overflow`` is negative and ``suggest_identifier``
        answers a name still over the limit instead of raising."""
        interface_name, element_name = "I" * 125, "E" * 125
        assert check_identifier(interface_name) is None and check_identifier(element_name) is None
        written = _writable_root(tmp_path / "written")

        message = _refused_write(
            lambda: create_interface(
                str(written), interface_name, [{"name": element_name, "implementation_type": "float32"}]
            ),
            written,
        )

        suggested = re.search(r"for example '([^']*)'", message)
        assert suggested is not None, message
        assert check_identifier(suggested.group(1)) is None, message
        assert "at most -" not in message

    def test_the_pair_is_checked_with_no_init_anywhere(self, tmp_path: Path) -> None:
        """The reservation is unconditional, like ``{enum}_CompuMethod``.

        A conditional rule would make the first ``init`` on one interface fail
        generation on a file nobody touched. The three application-type forms are
        conditional because the writer emits nothing for an undeclared field.
        """
        interface_name, element_name = self.OVER
        root = _interfaces_model(tmp_path, _valued_interface(interface_name, element_name))

        assert len(check_identifier_conformance(str(root)).issues) == 1
        assert init_constant_name(interface_name, element_name) in _refused(root, tmp_path / "out.arxml")

    def test_a_refused_interface_leaves_no_file_and_no_audit_entry(self, tmp_path: Path) -> None:
        interface_name, element_name = self.OVER
        root = _writable_root(tmp_path)
        log_path = tmp_path / "dist" / "audit.jsonl"
        auditor = AuditLog(str(log_path))

        _refused_write(
            lambda: create_interface(
                str(root), interface_name, [{"name": element_name, "implementation_type": "float32"}], auditor
            ),
            root,
        )

        assert not (root / "interfaces").exists()
        assert not log_path.exists(), "an unwritten mutation must not be audited"


class TestAnUnwritableInitValueIsRefusedBeforeItIsDumped:
    """An unwritable ``init`` is refused on the **write** path, before the model file's YAML dump.

    The change-set lifecycle materializes before pass D, so a value nothing can
    convert to decimal would reach ``yaml.safe_dump`` and raise CPython's own
    ``ValueError`` out of a well-formed request. The rule is a pass-D
    ``consistency-violation`` (``editing/diagnosis.py``), so the guard on the shared
    ``check_interface_init_constants`` refuses it first, for both write paths.

    The value is authored as hexadecimal on purpose: CPython bounds the decimal
    conversion and not ``int(s, 16)`` (``swcraft_tools.rendering``), so this is
    the one shape a hand-written model file can carry into the tool.
    """

    #: 5 000 hexadecimal digits — about 6 020 decimal ones, above the 4 300 a
    #: default interpreter will convert. Built with ``int(…, 16)``, the
    #: constructor the limit does not apply to, and spelled in a model file the
    #: same way, because ``yaml.safe_dump`` cannot write one.
    UNWRITABLE_HEX = "f" * 5000
    UNWRITABLE = int(UNWRITABLE_HEX, 16)
    #: The near miss: wide, and still convertible, so the refusal is about the
    #: limit rather than about the magnitude.
    WRITABLE = 10**4299

    def test_create_interface_refuses_it(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_interface(
                str(root), "ISensor", [{"name": "value", "implementation_type": "float32", "init": self.UNWRITABLE}]
            ),
            root,
        )

        assert "interface.init-unwritable-integer" in message
        assert not (root / "interfaces").exists()

    def test_write_element_refuses_it(self, tmp_path: Path) -> None:
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: write_element(
                str(root),
                "interfaces/ISensor.yaml",
                SRInterface(
                    name="ISensor",
                    kind="sender_receiver",
                    data_elements=[DataElement(name="value", implementation_type="float32", init=self.UNWRITABLE)],
                ),
            ),
            root,
        )

        assert "interface.init-unwritable-integer" in message

    def test_the_refusal_is_the_class_the_editing_engine_translates(self, tmp_path: Path) -> None:
        """The refusal is a ``ReservedNameError`` carrying ``{layer, element}``.

        ``engine._materialization_refusal`` turns it into ``consistency-violation``;
        a bare ``ValueError`` would be re-raised there instead."""
        root = _writable_root(tmp_path)

        with pytest.raises(ReservedNameError) as excinfo:
            create_interface(
                str(root), "ISensor", [{"name": "value", "implementation_type": "float32", "init": self.UNWRITABLE}]
            )

        assert excinfo.value.layer == "interfaces"
        assert excinfo.value.element == "ISensor"

    def test_the_value_is_named_by_its_bit_length_never_by_its_digits(self, tmp_path: Path) -> None:
        """The value is named by its bit length.

        Counting the digits *is* the conversion that raises, so the message may not do it (``swcraft_tools.rendering``).
        """
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_interface(
                str(root), "ISensor", [{"name": "value", "implementation_type": "float32", "init": self.UNWRITABLE}]
            ),
            root,
        )

        assert f"{self.UNWRITABLE.bit_length()} bits" in message

    def test_a_nested_value_is_found_and_located(self, tmp_path: Path) -> None:
        """``init`` is recursive, so the guard walks it — and says where."""
        root = _writable_root(tmp_path)

        message = _refused_write(
            lambda: create_interface(
                str(root),
                "ISensor",
                [{"name": "value", "implementation_type": "Rows_T", "init": [{"a": 1, "b": self.UNWRITABLE}]}],
            ),
            root,
        )

        assert "init[0].b" in message

    def test_a_wide_but_convertible_value_passes_both_write_paths(self, tmp_path: Path) -> None:
        """Non-vacuity: 4 300 digits is the limit, and this is one under it."""
        written = _writable_root(tmp_path / "written")
        create_interface(
            str(written), "ISensor", [{"name": "value", "implementation_type": "uint32", "init": self.WRITABLE}]
        )

        generic = _writable_root(tmp_path / "generic")
        write_element(
            str(generic),
            "interfaces/ISensor.yaml",
            SRInterface(
                name="ISensor",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", implementation_type="uint32", init=self.WRITABLE)],
            ),
        )

    def test_the_write_path_and_check_model_refuse_it_under_the_same_rule(self, tmp_path: Path) -> None:
        """``check-model`` reports what the two write paths refuse, under one identifier."""
        root = _interfaces_model(tmp_path)
        # Hand-written, because the value is exactly the one `yaml.safe_dump`
        # refuses: an authored `0x…` is how it reaches a project at all.
        (root / "interfaces" / "ISensor.yaml").write_text(
            "name: ISensor\nkind: sender_receiver\ndata_elements:\n"
            f"  - name: value\n    implementation_type: float32\n    init: 0x{self.UNWRITABLE_HEX}\n",
            encoding="utf-8",
            newline="\n",
        )

        records = check_init_conformance(str(root)).records

        assert [record.rule for record in records] == ["interface.init-unwritable-integer"]


class TestBothCollisionHalvesAreRefused:
    """The derivation is **not injective**, so the constant collides two ways.

    Against an *interface* — a declared interface literally named ``{I}_{E}_Init``
    is a sibling of the constant in ``PortInterfaces`` — and against another
    *constant*: ``A_B``/``C`` and ``A``/``B_C`` both derive ``A_B_C_Init``.

    The three write-side guards raise :class:`ReservedNameError` with
    ``layer="interfaces"``, so the editing engine answers ``reserved_name_violation``
    rather than letting a ``ValueError`` escape. The report under a rule identifier
    is the validator's; this class is about the refusals.
    """

    @staticmethod
    def _sr(name: str, element_name: str) -> SRInterface:
        return SRInterface(
            name=name, kind="sender_receiver", data_elements=[{"name": element_name, "implementation_type": "float32"}]
        )

    def test_the_constant_collides_with_a_declared_interface_name(self, tmp_path: Path) -> None:
        colliding = init_constant_name("ISensor", "value")
        root = _interfaces_model(
            tmp_path,
            _valued_interface("ISensor", "value"),
            _valued_interface(colliding, "other"),
        )

        assert colliding in _refused(root, tmp_path / "out.arxml")

    def test_the_mutation_refuses_the_collision_in_both_directions(self, tmp_path: Path) -> None:
        colliding = init_constant_name("ISensor", "value")

        forward = _writable_root(tmp_path / "forward")
        create_interface(str(forward), "ISensor", [{"name": "value", "implementation_type": "float32"}])
        with pytest.raises(ReservedNameError) as first:
            create_interface(str(forward), colliding, [{"name": "other", "implementation_type": "float32"}])

        reverse = _writable_root(tmp_path / "reverse")
        create_interface(str(reverse), colliding, [{"name": "other", "implementation_type": "float32"}])
        with pytest.raises(ReservedNameError) as second:
            create_interface(str(reverse), "ISensor", [{"name": "value", "implementation_type": "float32"}])

        for raised in (first, second):
            assert raised.value.layer == "interfaces"
            assert colliding in str(raised.value)

    def test_the_element_writer_refuses_the_collision(self, tmp_path: Path) -> None:
        colliding = init_constant_name("ISensor", "value")
        root = _writable_root(tmp_path)
        create_interface(str(root), "ISensor", [{"name": "value", "implementation_type": "float32"}])

        with pytest.raises(ReservedNameError) as raised:
            write_element(str(root), f"interfaces/{colliding}.yaml", self._sr(colliding, "other"))

        assert raised.value.layer == "interfaces"
        assert not (root / "interfaces" / f"{colliding}.yaml").exists()

    def test_the_underscore_ambiguity_is_refused_at_generation_time(self, tmp_path: Path) -> None:
        assert init_constant_name("A_B", "C") == init_constant_name("A", "B_C")
        root = _interfaces_model(tmp_path, _valued_interface("A_B", "C"), _valued_interface("A", "B_C"))

        message = _refused(root, tmp_path / "out.arxml")

        assert init_constant_name("A_B", "C") in message
        assert "A_B" in message and "B_C" in message

    def test_the_underscore_ambiguity_is_refused_by_the_two_write_paths(self, tmp_path: Path) -> None:
        shared = init_constant_name("A_B", "C")

        written = _writable_root(tmp_path / "written")
        create_interface(str(written), "A_B", [{"name": "C", "implementation_type": "float32"}])
        with pytest.raises(ReservedNameError) as mutation:
            create_interface(str(written), "A", [{"name": "B_C", "implementation_type": "float32"}])

        generic = _writable_root(tmp_path / "generic")
        create_interface(str(generic), "A_B", [{"name": "C", "implementation_type": "float32"}])
        with pytest.raises(ReservedNameError) as element_writer:
            write_element(str(generic), "interfaces/A.yaml", self._sr("A", "B_C"))

        for raised in (mutation, element_writer):
            assert raised.value.layer == "interfaces"
            assert shared in str(raised.value)
        assert not (generic / "interfaces" / "A.yaml").exists()

    def test_rewriting_one_interface_in_place_is_not_a_collision_with_itself(self, tmp_path: Path) -> None:
        """Rewriting one interface in place is no collision with itself.

        ``write_element`` is also the *update* primitive: the interface already on disk under the same name is not a
        second claimant."""
        root = _writable_root(tmp_path)
        create_interface(str(root), "ISensor", [{"name": "value", "implementation_type": "float32"}])

        write_element(str(root), "interfaces/ISensor.yaml", self._sr("ISensor", "value"))

    def test_the_collision_holds_with_no_init_anywhere(self, tmp_path: Path) -> None:
        """Unconditional, like the length half: neither pair carries a value."""
        root = _interfaces_model(tmp_path, _valued_interface("A_B", "C"), _valued_interface("A", "B_C"))

        assert init_constant_name("A_B", "C") in _refused(root, tmp_path / "out.arxml")


# ---------------------------------------------------------------------------
# The three access-point forms, and the four guards that must agree
# ---------------------------------------------------------------------------


def _access_component(
    port_name: str,
    member_name: str,
    *,
    list_name: str = "reads",
    swc_name: str = "A",
    interface_name: str = "IAcc",
) -> dict:
    """One component whose single runnable declares one access entry."""
    member_field = "operation" if list_name == "calls" else "element"
    return {
        "name": swc_name,
        "type": "application",
        "ports": [
            {
                "name": port_name,
                "direction": "provide" if list_name == "writes" else "require",
                "interface": interface_name,
            }
        ],
        "runnables": [{"name": "Run", list_name: [{"port": port_name, member_field: member_name}]}],
        "handle_termination_and_restart": "no_support",
    }


def _access_model(
    tmp_path: Path,
    port_name: str,
    member_name: str,
    *,
    list_name: str = "reads",
    interface_name: str = "IAcc",
) -> Path:
    """A model root whose one component reaches the access-point form once."""
    root = _model_root(tmp_path)
    _seed_float32(root)
    interface = (
        {"name": interface_name, "kind": "client_server", "operations": [{"name": member_name, "arguments": []}]}
        if list_name == "calls"
        else {
            "name": interface_name,
            "kind": "sender_receiver",
            "data_elements": [{"name": member_name, "implementation_type": "float32"}],
        }
    )
    _write_yaml(root / "interfaces" / f"{interface_name}.yaml", interface)
    _seed_swc(root, _access_component(port_name, member_name, list_name=list_name, interface_name=interface_name))
    return root


def _access_pair(prefix: str, port_seed: str = "P", member_seed: str = "M", *, over: int = 0) -> tuple[str, str]:
    """A port name and a member name summing to the form's budget plus *over*."""
    budget = ACCESS_BUDGET[prefix] + over
    port_length = budget // 2
    return port_seed * port_length, member_seed * (budget - port_length)


def _swc_element(port_name: str, member_name: str, list_name: str = "reads") -> SWComponent:
    """The same component as :func:`_access_component`, as a model object."""
    return SWComponent.model_validate(_access_component(port_name, member_name, list_name=list_name))


def _add_access_runnable(root: Path, port_name: str, member_name: str, list_name: str = "reads"):
    """The mutation path: the component and its port first, then the runnable."""
    create_swc(str(root), "A", "grp")
    add_port(str(root), "A", port_name, "provide" if list_name == "writes" else "require", "IAcc")
    member_field = "operation" if list_name == "calls" else "element"
    entry = [{"port": port_name, member_field: member_name}]
    return lambda: add_runnable(str(root), "A", "Run", **{list_name: entry})


class TestTheFourGuardsAgreeAboutTheAccessPoints:
    """``R_{port}_{element}``, ``W_{port}_{element}`` and ``Call_{port}_{operation}`` have *joint* budgets.

    The prefix and the separating ``_`` cost three characters of the 128 for the
    two data forms and six for the call form, so the two declared names get
    **125** and **122** between them. Four guards agree about them —
    ``check_identifier_conformance`` reports, ``_check_component_names`` refuses at
    generation time, and ``add_runnable`` and ``write_element`` refuse before
    anything reaches disk — on the offending **pair** and the shared **reason**
    constant, never on a rendered sentence. The two names are declared in
    **different layers** (port on the component, member on the interface), and the
    derived name is checked for every declared entry, whether or not it resolves.
    """

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGET), ids=repr)
    def test_a_pair_on_the_budget_passes_all_four_guards(self, prefix: str, tmp_path: Path) -> None:
        list_name = ACCESS_LIST_OF[prefix]
        port_name, member_name = _access_pair(prefix)
        assert len(port_name) + len(member_name) == ACCESS_BUDGET[prefix]
        assert len(access_point_name(prefix, port_name, member_name)) == MAX_IDENTIFIER_LENGTH

        root = _access_model(tmp_path, port_name, member_name, list_name=list_name)
        assert check_identifier_conformance(str(root)).issues == []
        generate_arxml(str(root), str(tmp_path / "ok.arxml"))

        written = _writable_root(tmp_path / "written")
        _add_access_runnable(written, port_name, member_name, list_name)()

        generic = _writable_root(tmp_path / "generic")
        write_element(str(generic), "swc/grp/A.yaml", _swc_element(port_name, member_name, list_name))

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGET), ids=repr)
    def test_one_character_over_is_reported_and_refused_by_all_four(self, prefix: str, tmp_path: Path) -> None:
        list_name = ACCESS_LIST_OF[prefix]
        port_name, member_name = _access_pair(prefix, over=1)
        emitted = access_point_name(prefix, port_name, member_name)
        assert check_identifier(port_name) is None and check_identifier(member_name) is None
        assert check_identifier(emitted) == REASON_LENGTH

        root = _access_model(tmp_path, port_name, member_name, list_name=list_name)
        issues = check_identifier_conformance(str(root)).issues
        generation = _refused(root, tmp_path / "out.arxml")

        written = _writable_root(tmp_path / "written")
        mutation = _refused_write(_add_access_runnable(written, port_name, member_name, list_name), written)

        generic = _writable_root(tmp_path / "generic")
        element_writer = _refused_write(
            lambda: write_element(str(generic), "swc/grp/A.yaml", _swc_element(port_name, member_name, list_name)),
            generic,
        )

        assert len(issues) == 1, issues
        for message in (issues[0], generation, mutation, element_writer):
            assert emitted in message, message
            assert f"'{port_name}'" in message and f"'{member_name}'" in message, message
            assert REASON_LENGTH in message, message
            assert f"{ACCESS_BUDGET[prefix]} characters between them" in message, message

    def test_all_four_suggest_shortening_the_longer_name(self, tmp_path: Path) -> None:
        port_name, member_name = "P" * 100, "M" * 30
        root = _access_model(tmp_path, port_name, member_name)
        written = _writable_root(tmp_path / "written")
        generic = _writable_root(tmp_path / "generic")

        messages = [
            check_identifier_conformance(str(root)).issues[0],
            _refused(root, tmp_path / "out.arxml"),
            _refused_write(_add_access_runnable(written, port_name, member_name), written),
            _refused_write(
                lambda: write_element(str(generic), "swc/grp/A.yaml", _swc_element(port_name, member_name)),
                generic,
            ),
        ]

        for message in messages:
            assert f"'{port_name}' to at most 95 characters" in message, message

    def test_a_tie_suggests_the_member_everywhere(self, tmp_path: Path) -> None:
        """The name a designer changes with less blast radius."""
        port_name, member_name = "P" * 63, "M" * 63
        root = _access_model(tmp_path, port_name, member_name)
        written = _writable_root(tmp_path / "written")

        report = check_identifier_conformance(str(root)).issues[0]
        generation = _refused(root, tmp_path / "out.arxml")
        mutation = _refused_write(_add_access_runnable(written, port_name, member_name), written)

        for message in (report, generation, mutation):
            assert f"'{member_name}' to at most 62 characters" in message, message

    def test_the_write_path_clamps_the_suggestion(self, tmp_path: Path) -> None:
        """Two 125-character names overflow by 125, more than either is long: the suggestion is clamped.

        Unclamped, ``len(name) - overflow`` is negative and ``suggest_identifier``
        answers a name still over the limit instead of raising."""
        port_name, member_name = "P" * 125, "M" * 125
        assert check_identifier(port_name) is None and check_identifier(member_name) is None
        written = _writable_root(tmp_path / "written")

        message = _refused_write(_add_access_runnable(written, port_name, member_name), written)

        suggested = re.search(r"for example '([^']*)'", message)
        assert suggested is not None, message
        assert check_identifier(suggested.group(1)) is None, message
        assert "at most -" not in message

    def test_a_refused_runnable_leaves_no_file_and_no_audit_entry(self, tmp_path: Path) -> None:
        port_name, member_name = _access_pair(READ_ACCESS_PREFIX, over=1)
        root = _writable_root(tmp_path)
        log_path = tmp_path / "dist" / "audit.jsonl"
        auditor = AuditLog(str(log_path))
        create_swc(str(root), "A", "grp")
        add_port(str(root), "A", port_name, "require", "IAcc")
        before = (root / "swc" / "grp" / "A.yaml").read_text(encoding="utf-8")

        _refused_write(
            lambda: add_runnable(
                str(root),
                "A",
                "Run",
                0.0,
                auditor,
                reads=[{"port": port_name, "element": member_name}],
            ),
            root,
        )

        assert (root / "swc" / "grp" / "A.yaml").read_text(encoding="utf-8") == before
        assert not log_path.exists(), "an unwritten mutation must not be audited"


class TestTheAccessPointUnderscoreAmbiguityIsRefused:
    """The derivation is **not injective**: ``a_b``/``c`` and ``a``/``b_c`` both derive ``R_a_b_c``.

    Two such entries under **one runnable** emit two elements under one
    ``SHORT-NAME``. The scope is the runnable, not the component: the four access
    containers are not path segments, so every access point of one runnable is a
    ``SHORT-NAME`` sibling of every other (``ShortNameScope``'s class docstring).

    The two write-side guards raise :class:`ReservedNameError` with
    ``layer="swc"``, so the editing engine answers ``consistency-violation``
    rather than letting a ``ValueError`` escape a well-formed change set. The
    report under a rule identifier is the validator's; this class is about the
    refusals.
    """

    #: Two entries of one runnable deriving one name, and the interface that
    #: declares both members so the model is otherwise resolvable.
    PAIRS = (("a_b", "c"), ("a", "b_c"))

    @classmethod
    def _component(cls) -> dict:
        return {
            "name": "A",
            "type": "application",
            "ports": [{"name": port, "direction": "require", "interface": "IAmb"} for port, _member in cls.PAIRS],
            "runnables": [{"name": "Run", "reads": [{"port": port, "element": member} for port, member in cls.PAIRS]}],
            "handle_termination_and_restart": "no_support",
        }

    def _model(self, tmp_path: Path) -> Path:
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(
            root / "interfaces" / "IAmb.yaml",
            {
                "name": "IAmb",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "c", "implementation_type": "float32"},
                    {"name": "b_c", "implementation_type": "float32"},
                ],
            },
        )
        _seed_swc(root, self._component())
        return root

    def test_the_two_entries_really_derive_one_name(self) -> None:
        assert access_point_name(READ_ACCESS_PREFIX, "a_b", "c") == access_point_name(READ_ACCESS_PREFIX, "a", "b_c")

    def test_it_is_refused_at_generation_time(self, tmp_path: Path) -> None:
        shared = access_point_name(READ_ACCESS_PREFIX, "a_b", "c")

        message = _refused(self._model(tmp_path), tmp_path / "out.arxml")

        assert shared in message
        assert "'a_b'" in message and "'b_c'" in message

    def test_it_is_refused_by_the_two_write_paths(self, tmp_path: Path) -> None:
        shared = access_point_name(READ_ACCESS_PREFIX, "a_b", "c")
        entries = [{"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"}]

        written = _writable_root(tmp_path / "written")
        create_swc(str(written), "A", "grp")
        with pytest.raises(ReservedNameError) as mutation:
            add_runnable(str(written), "A", "Run", reads=entries)

        generic = _writable_root(tmp_path / "generic")
        with pytest.raises(ReservedNameError) as element_writer:
            write_element(str(generic), "swc/grp/A.yaml", SWComponent.model_validate(self._component()))

        for raised in (mutation, element_writer):
            assert raised.value.layer == "swc"
            assert shared in str(raised.value)
        assert not (generic / "swc" / "grp" / "A.yaml").exists()

    def test_the_same_pair_in_two_runnables_is_not_a_collision(self, tmp_path: Path) -> None:
        """The scope is one runnable.

        Two runnables of one component each own their own ``RUNNABLE-ENTITY`` path segment, so two access points of one
        name under different runnables are not siblings."""
        root = _model_root(tmp_path)
        _seed_float32(root)
        _write_yaml(
            root / "interfaces" / "IAmb.yaml",
            {
                "name": "IAmb",
                "kind": "sender_receiver",
                "data_elements": [{"name": "c", "implementation_type": "float32"}],
            },
        )
        _seed_swc(
            root,
            {
                "name": "A",
                "type": "application",
                "ports": [{"name": "a_b", "direction": "require", "interface": "IAmb"}],
                "runnables": [
                    {"name": "One", "reads": [{"port": "a_b", "element": "c"}]},
                    {"name": "Two", "reads": [{"port": "a_b", "element": "c"}]},
                ],
                "handle_termination_and_restart": "no_support",
            },
        )

        assert Path(generate_arxml(str(root), str(tmp_path / "out.arxml"))).exists()


# ---------------------------------------------------------------------------
# The eighth form on the three name-minting policies
#
# Any writer-reserved name is checked against every minting policy SWCraft owns,
# and there are three — `arxml_import/registry.py`'s `_reserve()` (phase A, pinned
# by `TestTheImporterCannotMintAReservedName` above), `arxml_import/reconcile.py`
# (phase B, below) and `component_exchange/name_policies.py`'s two guards (in
# `test_component_import.py`, where the component path lives).
# ---------------------------------------------------------------------------

MINIMAL_SWC_FIXTURE = Path(__file__).parent / "fixtures" / "arxml" / "minimal_swc.arxml"


def _sr(name: str, element_name: str, type_name: str = "uint8") -> SRInterface:
    """One sender-receiver interface with one data element, as a model object."""
    return SRInterface(
        name=name, kind="sender_receiver", data_elements=[DataElement(name=element_name, implementation_type=type_name)]
    )


def _cs(name: str) -> CSInterface:
    return CSInterface(
        name=name,
        kind="client_server",
        operations=[
            CSOperation(name="Op", arguments=[CSArgument(name="a", direction="in", implementation_type="uint8")])
        ],
    )


def _swc_using(name: str, *interface_names: str) -> ImportedSwc:
    """An imported SWC with one require port per named interface."""
    return ImportedSwc(
        swc=SWComponent(
            name=name,
            type="application",
            ports=[
                Port(name=f"r{index}", direction="require", interface=interface)
                for index, interface in enumerate(interface_names)
            ],
        ),
        group="Components",
        path=f"/Pkg/{name}",
    )


def _second_sr_interface(interface_name: str, element_name: str) -> str:
    """The XML of one more SENDER-RECEIVER-INTERFACE for `minimal_swc.arxml`."""
    return (
        "                <SENDER-RECEIVER-INTERFACE>\n"
        f"                  <SHORT-NAME>{interface_name}</SHORT-NAME>\n"
        "                  <IS-SERVICE>false</IS-SERVICE>\n"
        "                  <DATA-ELEMENTS>\n"
        "                    <VARIABLE-DATA-PROTOTYPE>\n"
        f"                      <SHORT-NAME>{element_name}</SHORT-NAME>\n"
        '                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">'
        "/Acme/Chassis/DataTypes/WheelSpeedValue_impl</TYPE-TREF>\n"
        "                    </VARIABLE-DATA-PROTOTYPE>\n"
        "                  </DATA-ELEMENTS>\n"
        "                </SENDER-RECEIVER-INTERFACE>\n"
    )


def _export_with_a_second_interface(
    tmp_path: Path, interface_name: str, element_name: str, *, rename: dict[str, str] | None = None
) -> Path:
    """`minimal_swc.arxml` plus one more SR interface, required by one more port.

    SWCraft's writer refuses the shapes this section needs, so the interface is
    spliced in as text, with a port beside it so a rename has a *reference* to follow.
    """
    text = MINIMAL_SWC_FIXTURE.read_text(encoding="utf-8")
    for old, new in (rename or {}).items():
        text = text.replace(old, new)
    port = (
        "                    <R-PORT-PROTOTYPE>\n"
        f"                      <SHORT-NAME>rSecond</SHORT-NAME>\n"
        '                      <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">'
        f"/Acme/Chassis/Interfaces/{interface_name}</REQUIRED-INTERFACE-TREF>\n"
        "                    </R-PORT-PROTOTYPE>\n"
    )
    closing = "                </SENDER-RECEIVER-INTERFACE>\n"
    assert text.count(closing) == 1, "the fixture must declare exactly one SR interface"
    text = text.replace(closing, closing + _second_sr_interface(interface_name, element_name), 1)
    port_closing = "                    </R-PORT-PROTOTYPE>\n"
    text = text.replace(port_closing, port_closing + port, 1)
    # The behaviour states NO-SUPPORT: a component without a termination value is
    # refused, and `minimal_swc.arxml` is shared with the behaviour-loss suites.
    events_closing = "                      </EVENTS>\n"
    assert text.count(events_closing) == 1, "the fixture's behaviour must declare exactly one EVENTS list"
    text = text.replace(
        events_closing,
        events_closing
        + "                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\n",
        1,
    )
    source = tmp_path / "with_second_interface.arxml"
    source.write_text(text, encoding="utf-8", newline="\n")
    return source


class TestPhaseBReconcilesTheInitConstantAgainstInterfaceNames:
    """Phase B learns the eighth form, and only the half it *can* repair.

    An interface literally named ``{I}_{E}_Init`` beside the pair ``(I, E)`` is a
    sibling of the constant in ``PortInterfaces``; it has a derived side to move,
    so it is renamed and reported. The **constant↔constant** half has nothing to
    rename: all four declared names are legal and referenced, so such an export
    imports unrenamed and the writer refuses the project — the documented residual
    ``identifier_violations``' docstring names.
    """

    def test_a_declared_interface_named_like_a_pair_constant_moves(self) -> None:
        colliding = init_constant_name("ISensor", "value")

        _registry, reconciled = _reconcile(interfaces=[_sr("ISensor", "value"), _sr(colliding, "other")])

        names = {iface.name for iface in reconciled.interfaces}
        assert names == {"ISensor", f"{colliding}{RENAME_SUFFIX}"}, names

    def test_the_reservation_reads_the_writers_own_derivation(self) -> None:
        """The reservation reads the writer's own derivation, not a restated ``_Init`` suffix rule.

        An interface merely *ending* in ``_Init`` collides with nothing unless some declared pair derives it."""
        assert reserved_interface_names([_sr("ISensor", "value"), _sr("Unrelated_Init", "other")]) == {}

        reserved = reserved_interface_names([_sr("ISensor", "value"), _sr(init_constant_name("ISensor", "value"), "o")])

        assert list(reserved) == [init_constant_name("ISensor", "value")]
        assert "ISensor" in reserved[init_constant_name("ISensor", "value")]
        assert "value" in reserved[init_constant_name("ISensor", "value")]

    def test_a_client_server_interface_named_like_a_constant_moves_too(self) -> None:
        """A CS interface derives no constant and is still a *sibling* of one."""
        colliding = init_constant_name("ISensor", "value")

        _registry, reconciled = _reconcile(interfaces=[_sr("ISensor", "value"), _cs(colliding)])

        assert {iface.name for iface in reconciled.interfaces} == {"ISensor", f"{colliding}{RENAME_SUFFIX}"}

    def test_a_port_reference_follows_the_renamed_interface(self) -> None:
        colliding = init_constant_name("ISensor", "value")
        swc = _swc_using("Consumer", colliding, "ISensor")

        _registry, reconciled = _reconcile(interfaces=[_sr("ISensor", "value"), _sr(colliding, "other")], swcs=[swc])

        ports = {port.name: port.interface for port in reconciled.swcs[0].swc.ports}
        assert ports == {"r0": f"{colliding}{RENAME_SUFFIX}", "r1": "ISensor"}

    def test_an_untouched_swc_is_returned_as_is(self) -> None:
        swc = _swc_using("Consumer", "ISensor")

        _registry, reconciled = _reconcile(interfaces=[_sr("ISensor", "value")], swcs=[swc])

        assert reconciled.swcs[0] is swc

    def test_the_rename_is_recorded_with_its_reason(self) -> None:
        """The registry's record; `test_arxml_import_cli.py` pins the report line through `run_import`."""
        colliding = init_constant_name("ISensor", "value")

        registry, _reconciled = _reconcile(interfaces=[_sr("ISensor", "value"), _sr(colliding, "other")])

        assert any(f"{colliding} -> {colliding}{RENAME_SUFFIX}" in line for line in registry.renames), registry.renames
        assert any("CONSTANT-SPECIFICATION" in line for line in registry.renames), registry.renames

    def test_a_renamed_interface_is_reported_under_the_name_the_export_declared(self) -> None:
        """The third ``*_source_names`` map: an over-length, colliding interface is reported as declared.

        The rename caps it at 128 characters, so without the map it would be
        reported under a string the export never contained.
        """
        declared = "I" * 60 + "_" + "E" * 68 + "_Init"
        assert check_identifier(declared) == REASON_LENGTH
        assert declared == init_constant_name("I" * 60, "E" * 68)

        _registry, reconciled = _reconcile(interfaces=[_sr("I" * 60, "E" * 68), _sr(declared, "other")])

        violations = identifier_violations(reconciled, [])

        assert violations, reconciled.interface_source_names
        assert any(declared in line and REASON_LENGTH in line for line in violations), violations

    def test_a_conforming_interface_set_is_returned_untouched(self) -> None:
        interfaces = [_sr("ISensor", "value"), _cs("ICalib")]

        _registry, reconciled = _reconcile(interfaces=interfaces)

        assert reconciled.interfaces is interfaces
        assert reconciled.interface_source_names == {}

    def test_the_interface_half_has_the_same_fixed_point_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(reconcile, "_MAX_PASSES", 0)

        with pytest.raises(NameCollisionError, match="did not settle"):
            _reconcile(interfaces=[_sr("ISensor", "value"), _sr(init_constant_name("ISensor", "value"), "other")])

    def test_the_underscore_ambiguity_has_nothing_to_rename(self) -> None:
        """Both declared names are legal on both pairs, so phase B leaves them."""
        assert init_constant_name("A_B", "C") == init_constant_name("A", "B_C")
        interfaces = [_sr("A_B", "C"), _sr("A", "B_C")]

        _registry, reconciled = _reconcile(interfaces=interfaces)

        assert reconciled.interfaces is interfaces
        assert identifier_violations(reconciled, []) == []


class TestAnImportCarryingTheConstantCollision:
    """A real ``run_import()`` into a project, handed back to ``check-model`` and the writer.

    That is the only oracle for "did the import produce something buildable".
    """

    @staticmethod
    def _imported(tmp_path: Path, source: Path) -> Path:
        model_root, _report = _import_into_fresh_project(tmp_path, source)
        # An ARXML export carries no processor, and the root composition is added
        # as a modeller would, so what is checked is the imported content.
        create_hardware_element(str(model_root), "CPU", "processor")
        _root_composition(model_root, sorted(swc.name for swc in read_swc_model(str(model_root))))
        return model_root

    def test_the_colliding_interface_is_renamed_and_the_project_builds(self, tmp_path: Path) -> None:
        colliding = init_constant_name("IWheelSpeed", "speedValue")
        source = _export_with_a_second_interface(tmp_path, colliding, "other")

        model_root = self._imported(tmp_path, source)

        names = {path.stem for path in (model_root / "interfaces").glob("*.yaml")}
        assert names == {"IWheelSpeed", f"{colliding}{RENAME_SUFFIX}"}, names
        assert check_cross_layer_consistency(str(model_root)).issues == []
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_the_port_written_to_disk_names_the_renamed_interface(self, tmp_path: Path) -> None:
        colliding = init_constant_name("IWheelSpeed", "speedValue")
        source = _export_with_a_second_interface(tmp_path, colliding, "other")

        model_root = self._imported(tmp_path, source)

        swc = yaml.safe_load(next((model_root / "swc").rglob("WheelSpeedSensorSwc.yaml")).read_text(encoding="utf-8"))
        assert {port["interface"] for port in swc["ports"]} == {"IWheelSpeed", f"{colliding}{RENAME_SUFFIX}"}

    def test_the_underscore_ambiguity_imports_with_nothing_renamed(self, tmp_path: Path) -> None:
        source = _export_with_a_second_interface(
            tmp_path, "IWheelSpeed", "speed_Value", rename={"IWheelSpeed": "IWheelSpeed_speed", "speedValue": "Value"}
        )

        model_root, report = _import_into_fresh_project(tmp_path, source)

        assert report.rename_lines == [], report.rename_lines
        assert {path.stem for path in (model_root / "interfaces").glob("*.yaml")} == {
            "IWheelSpeed_speed",
            "IWheelSpeed",
        }

    def test_the_writer_refuses_the_imported_underscore_ambiguity(self, tmp_path: Path) -> None:
        """Phase B has nothing to rename, so the writer refuses the project it wrote."""
        source = _export_with_a_second_interface(
            tmp_path, "IWheelSpeed", "speed_Value", rename={"IWheelSpeed": "IWheelSpeed_speed", "speedValue": "Value"}
        )
        model_root = self._imported(tmp_path, source)
        shared = init_constant_name("IWheelSpeed_speed", "Value")
        assert shared == init_constant_name("IWheelSpeed", "speed_Value")

        with pytest.raises(ValueError, match="CONSTANT-SPECIFICATION") as raised:
            generate_arxml(str(model_root), str(tmp_path / "never-written.arxml"))

        assert shared in str(raised.value)
        assert not (tmp_path / "never-written.arxml").exists()

    def test_check_model_reports_the_imported_underscore_ambiguity(self, tmp_path: Path) -> None:
        """``check-model`` reports the project the case above imports, as the writer refuses it.

        Weakening this to what the validator says would hide a disagreement between
        the two surfaces.
        """
        source = _export_with_a_second_interface(
            tmp_path, "IWheelSpeed", "speed_Value", rename={"IWheelSpeed": "IWheelSpeed_speed", "speedValue": "Value"}
        )
        model_root = self._imported(tmp_path, source)
        shared = init_constant_name("IWheelSpeed_speed", "Value")

        issues = check_cross_layer_consistency(str(model_root)).issues

        assert any(shared in issue for issue in issues), issues


# ---------------------------------------------------------------------------
# The three access-point forms on the three name-minting policies
#
# The same sweep the eighth form took: `arxml_import/registry.py`'s `_reserve()`
# (phase A, below), `reconcile.py` (phase B, below) and
# `component_exchange/name_policies.py`'s guards (in `test_component_import.py`).
# Both conclusions are *negative* — phase A cannot mint one of these names and
# phase B has nothing to reserve for one — so they are pinned as claims.
# ---------------------------------------------------------------------------


def _access_imported_swc(port_name: str, member_name: str, *, name: str = "Consumer") -> ImportedSwc:
    """An imported SWC whose one runnable writes one `(port, element)` pair."""
    return ImportedSwc(
        swc=SWComponent(
            name=name,
            type="application",
            ports=[Port(name=port_name, direction="provide", interface="IAcc")],
            runnables=[Runnable(name="Run", writes=[{"port": port_name, "element": member_name}])],
        ),
        group="Components",
        path=f"/Pkg/{name}",
    )


class TestNoAccessPointFormIsMintableByPhaseA:
    """The three access-point forms are unmintable by ``_reserve()``, for two independent reasons.

    * **Shape.** Every candidate keeps the declared name's first characters and
      *appends* only ``[a-z0-9_]``; an access point's name is a **prefixed** form,
      and each prefix carries a capital.
    * **Namespace.** Phase A mints the bare name of a package-level *element*; an
      access point is a grandchild of a runnable, two levels below any element the
      registry sees.
    """

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGET), ids=repr)
    def test_each_prefix_carries_a_capital_no_mint_can_produce(self, prefix: str) -> None:
        assert prefix.lower() != prefix, (
            f"the access-point prefix {prefix!r} must contain a capital: a fully lowercase prefix "
            "could be reached by _reserve()'s '_app_{lower_snake_package}' and '_{k}_app' forms"
        )

    def test_every_candidate_only_appends_and_never_prepends(self) -> None:
        """Taken from the generator, so a fourth candidate shape has to come through this assertion."""
        registry = PathRegistry()
        registry.register("/P/A/Level", "Level", "application_types")
        candidates = list(islice(registry._collision_candidates("Level", "/P/B/Level"), 8))

        for minted in candidates:
            assert minted.startswith("Level"), minted
            assert re.fullmatch(r"[a-z0-9_]*", minted[len("Level") :]), minted
            for prefix in ACCESS_BUDGET:
                assert not minted.startswith(prefix), minted

    def test_a_real_import_carrying_access_points_mints_nothing(self, tmp_path: Path) -> None:
        """An export whose runnable declares an access point imports with an empty rename list."""
        source = _export_with_a_send_point(tmp_path, "pWheelSpeed", "speedValue")

        model_root, report = _import_into_fresh_project(tmp_path, source)

        assert report.rename_lines == [], report.rename_lines
        swc = yaml.safe_load(next((model_root / "swc").rglob("WheelSpeedSensorSwc.yaml")).read_text(encoding="utf-8"))
        assert swc["runnables"][0]["writes"] == [{"port": "pWheelSpeed", "element": "speedValue"}]


class TestNoOperationEventFormIsMintableByPhaseA:
    """``OIT_{port}_{operation}`` checked against the minting policies, for the same two reasons.

    * **Shape.** ``OIT_`` is a prefix carrying capitals, and every candidate
      ``_reserve()`` mints keeps the declared name's first characters and only
      appends ``[a-z0-9_]``, so no mint can produce it.
    * **Namespace.** An ``OPERATION-INVOKED-EVENT`` sits below a component's
      ``SWC-INTERNAL-BEHAVIOR``, no package-level element, and its one clash (two
      pairs deriving one base) is numbered by the writer's event counter, so there
      is nothing to reserve and nothing to rename.

    **What this does not reach:** the two refusing policies are not asked here.
    ``component_exchange/name_policies.illegal_identifiers`` checks the pair shape
    through the writer's own ``_event_base_name`` and ``_unique_event_names``
    (``test_operation_event_exchange.py``), and ``writer/mutations`` gains no
    reserved component name with this form.
    """

    def test_the_prefix_carries_a_capital_no_mint_can_produce(self) -> None:
        assert OPERATION_EVENT_PREFIX.lower() != OPERATION_EVENT_PREFIX, (
            f"the operation-event prefix {OPERATION_EVENT_PREFIX!r} must contain a capital: a fully lowercase "
            "prefix could be reached by _reserve()'s '_app_{lower_snake_package}' and '_{k}_app' forms"
        )

    def test_no_candidate_starts_with_the_prefix(self) -> None:
        """Taken from the generator, as the access-point case is."""
        registry = PathRegistry()
        registry.register("/P/A/Level", "Level", "application_types")
        candidates = list(islice(registry._collision_candidates("Level", "/P/B/Level"), 8))

        for minted in candidates:
            assert minted.startswith("Level"), minted
            assert not minted.startswith(OPERATION_EVENT_PREFIX), minted


class TestPhaseBHasNothingToReserveForAnAccessPoint:
    """Phase B has nothing to reserve for the three access-point forms.

    An access point is not a package-level element, and its one clash — with
    another entry of its own runnable — has no derived side to move. The length
    half is asymmetric on purpose: :func:`identifier_violations` checks *declared*
    names, so an export whose derived access point overflows **imports**, and
    ``check-model`` and the writer refuse the project it wrote — the documented
    residual every generated form has.
    """

    def test_a_reconcile_pass_leaves_an_access_carrying_swc_untouched(self) -> None:
        swc = _access_imported_swc("pSpeed", "speed")

        registry, reconciled = _reconcile(interfaces=[_sr("IAcc", "speed")], swcs=[swc])

        assert reconciled.swcs[0] is swc
        assert registry.renames == [], registry.renames
        assert reconciled.swc_source_names == {}

    def test_an_interface_named_like_an_access_point_is_not_reserved(self) -> None:
        """An interface may be named ``W_pSpeed_speed`` beside the pair.

        The constant is a *sibling* of the interfaces and an access point is not."""
        colliding = access_point_name(WRITE_ACCESS_PREFIX, "pSpeed", "speed")
        interfaces = [_sr("IAcc", "speed"), _sr(colliding, "other")]

        _registry, reconciled = _reconcile(interfaces=interfaces, swcs=[_access_imported_swc("pSpeed", "speed")])

        assert reconciled.interfaces is interfaces
        assert {iface.name for iface in reconciled.interfaces} == {"IAcc", colliding}

    def test_an_over_budget_pair_is_a_residual_and_not_a_violation(self) -> None:
        port_name, member_name = _access_pair(WRITE_ACCESS_PREFIX, over=1)
        assert check_identifier(port_name) is None and check_identifier(member_name) is None
        assert check_identifier(access_point_name(WRITE_ACCESS_PREFIX, port_name, member_name)) == REASON_LENGTH

        _registry, reconciled = _reconcile(
            interfaces=[_sr("IAcc", member_name)], swcs=[_access_imported_swc(port_name, member_name)]
        )

        assert identifier_violations(reconciled, []) == []


def _export_with_a_send_point(tmp_path: Path, port: str, element: str) -> Path:
    """``minimal_swc.arxml`` with a renamed port and element and one ``DATA-SEND-POINTS`` access.

    Its provide port and data element are renamed to *port* and *element*, and the access is spliced into its one
    runnable.

    Spliced as text because the writer refuses to emit these shapes. The access's
    ``SHORT-NAME`` is the derived name: the importer states a source name only
    where it differs, so the model keeps the derived name this section judges.
    """
    text = MINIMAL_SWC_FIXTURE.read_text(encoding="utf-8")
    text = text.replace("pWheelSpeed", port).replace("speedValue", element)
    derived = access_point_name(WRITE_ACCESS_PREFIX, port, element)
    access = (
        "                          <DATA-SEND-POINTS>\n"
        "                            <VARIABLE-ACCESS>\n"
        f"                              <SHORT-NAME>{derived}</SHORT-NAME>\n"
        "                              <ACCESSED-VARIABLE>\n"
        "                                <AUTOSAR-VARIABLE-IREF>\n"
        '                                  <PORT-PROTOTYPE-REF DEST="P-PORT-PROTOTYPE">'
        f"/Acme/Chassis/Components/WheelSpeedSensorSwc/{port}</PORT-PROTOTYPE-REF>\n"
        '                                  <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
        f"/Acme/Chassis/Interfaces/IWheelSpeed/{element}</TARGET-DATA-PROTOTYPE-REF>\n"
        "                                </AUTOSAR-VARIABLE-IREF>\n"
        "                              </ACCESSED-VARIABLE>\n"
        "                            </VARIABLE-ACCESS>\n"
        "                          </DATA-SEND-POINTS>\n"
    )
    anchor = "                          <SYMBOL>WheelSpeedSensorRun</SYMBOL>\n"
    assert text.count(anchor) == 1, "the fixture must declare exactly one runnable"
    text = text.replace(anchor, access + anchor)
    source = tmp_path / "with_send_point.arxml"
    source.write_text(text, encoding="utf-8", newline="\n")
    return source


class TestAnImportCarryingAnOverBudgetAccessPair:
    """The residual, end to end.

    ``run_import()`` succeeds and the project it writes is one ``check-model`` reports and the writer refuses.

    Imported with nothing renamed, reported, refused — "did the import produce
    something buildable" has no other oracle.
    """

    PORT, MEMBER = _access_pair(WRITE_ACCESS_PREFIX, over=1)

    @classmethod
    def _imported(cls, tmp_path: Path) -> Path:
        source = _export_with_a_send_point(tmp_path, cls.PORT, cls.MEMBER)
        model_root, report = _import_into_fresh_project(tmp_path, source)
        assert report.rename_lines == [], report.rename_lines
        # An ARXML export carries no processor, so one is declared.
        create_hardware_element(str(model_root), "CPU", "processor")
        return model_root

    def test_the_pair_imports_with_nothing_renamed(self, tmp_path: Path) -> None:
        model_root = self._imported(tmp_path)

        swc = yaml.safe_load(next((model_root / "swc").rglob("WheelSpeedSensorSwc.yaml")).read_text(encoding="utf-8"))
        assert swc["runnables"][0]["writes"] == [{"port": self.PORT, "element": self.MEMBER}]

    def test_check_model_reports_the_imported_pair(self, tmp_path: Path) -> None:
        model_root = self._imported(tmp_path)
        emitted = access_point_name(WRITE_ACCESS_PREFIX, self.PORT, self.MEMBER)

        issues = check_identifier_conformance(str(model_root)).issues

        assert any(emitted in issue and REASON_LENGTH in issue for issue in issues), issues

    def test_the_writer_refuses_the_imported_pair(self, tmp_path: Path) -> None:
        model_root = self._imported(tmp_path)
        emitted = access_point_name(WRITE_ACCESS_PREFIX, self.PORT, self.MEMBER)

        with pytest.raises(ValueError, match="access point") as raised:
            generate_arxml(str(model_root), str(tmp_path / "never-written.arxml"))

        assert emitted in str(raised.value)
        assert not (tmp_path / "never-written.arxml").exists()


# ---------------------------------------------------------------------------
# The case fold, held over the whole reserved set
# ---------------------------------------------------------------------------


def _generated_claim_case(suffix: str) -> tuple[str, list, list, list, dict]:
    """For *suffix*, a base the writer generates ``{base}{suffix}`` for, and a declared case-only spelling of it.

    Returns ``(claim, data_types, interfaces, swc names, application fields)``:
    the declared name that differs from the generated form only in case, and the
    declarations that make the writer generate the form. A data-type claim is an
    application type, of its owner's package: a form collides inside its owner's
    package only.
    """
    claim = f"base{suffix}".swapcase()
    if suffix == COMPU_METHOD_SUFFIX:
        return claim, [_enum("Base"), _application(claim)], [], [], {}
    if suffix in (IMPLEMENTATION_SUFFIX, TIMING_SUFFIX):
        return claim, [], [], ["Base", claim], {}
    if suffix == INIT_CONSTANT_SUFFIX:
        interfaces = [
            SRInterface(
                name="base", kind="sender_receiver", data_elements=[DataElement(name="x", implementation_type="t")]
            ),
            SRInterface(
                name="BASE_X_iNIT",
                kind="sender_receiver",
                data_elements=[DataElement(name="y", implementation_type="t")],
            ),
        ]
        return "BASE_X_iNIT", [], interfaces, [], {}
    field = {
        UNIT_SUFFIX: {"unit": "km/h"},
        COMPU_SUFFIX: {"conversion": {"factor": 2.0}},
        RANGE_SUFFIX: {"range": {"min": 0.0, "max": 1.0}},
    }[suffix]
    return claim, [_application("Base", **field), _application(claim)], [], [], field


class TestTheFoldIsHeldOverTheWholeReservedSet:
    """Every generated form's claim is compared ignoring case, at both gates.

    ``[constr_2508]`` makes a ``SHORT-NAME`` unique **ignoring case** within its
    package, and each of the seven forms is emitted beside the declared elements of
    its root package, so a case-only spelling is the same collision as the exact
    one: ``import model``'s phase B renames it and ``import component`` refuses it.

    Under the fold the suffixes' capital letters no longer keep the registry's
    lowercase mints off them: a ``{bare}_app_{lower_snake_package}`` mint *can*
    end in a folded suffix when the package is named like one. That name is then
    phase B's to rename, so the import still writes no project the writer refuses.
    """

    @pytest.mark.parametrize("suffix", _WRITER_RESERVED_SUFFIXES, ids=repr)
    def test_phase_b_reserves_the_case_only_spelling(self, suffix: str) -> None:
        claim, data_types, interfaces, swcs, _field = _generated_claim_case(suffix)
        if data_types:
            assert claim in reserved_data_type_names({dt.name: dt for dt in data_types})
        if interfaces:
            assert set(reserved_interface_names(interfaces)) == {claim}
        if swcs:
            # "Base" emits both forms: the timing base set is the implementation one here.
            assert set(reserved_swc_names(set(swcs), {"Base"}, {"Base"})) == {claim}

    @pytest.mark.parametrize("suffix", _WRITER_RESERVED_SUFFIXES, ids=repr)
    def test_import_component_refuses_the_case_only_spelling(self, suffix: str) -> None:
        from swcraft_tools.component_exchange.name_policies import reserved_name_violations

        claim, data_types, interfaces, swcs, _field = _generated_claim_case(suffix)
        # A budgeted incoming component, so it emits `Base_Timing` as well as `Base_Implementation`.
        budgeted = [Runnable(name="Run", wcet="1us", wcet_type="gross")]
        violations = reserved_name_violations(
            SWComponent(name="Base", runnables=budgeted) if swcs else SWComponent(name="Unrelated"),
            [dt for dt in data_types if dt.name == claim],
            {},
            {claim} if swcs else set(),
            {(data_type_layer(dt), dt.name): dt for dt in data_types if dt.name != claim},
            [iface for iface in interfaces if iface.name == claim],
            {iface.name: iface for iface in interfaces if iface.name != claim},
        )
        assert len(violations) == 1 and f"'{claim}'" in violations[0], violations

    @pytest.mark.parametrize("suffix", _WRITER_RESERVED_SUFFIXES, ids=repr)
    def test_a_replacement_is_never_a_case_only_spelling_of_a_taken_name(self, suffix: str) -> None:
        taken = {f"x{suffix}_app".upper()}
        assert free_name(f"x{suffix}", taken).casefold() not in {name.casefold() for name in taken}


class TestTheElementsNamesAreJudgedToo:
    """``identifier_violations(reconciled, elements)`` judges each element's name.

    It judges each unit, compu method and data constraint name and each table entry's name."""

    def test_an_element_name_and_a_table_entry_name_are_judged(self) -> None:
        from swcraft_tools.schema import DataConstr, TableEntry, TextTable, Unit

        _registry, reconciled = _reconcile()
        elements = [
            Unit(name="Probe Unit", display_name="km/h"),
            TextTable(name="ProbeTable", kind="texttable", entries=[TableEntry(name="Probe Off", value=0)]),
            DataConstr(name="ProbeLimits"),
        ]

        assert identifier_violations(reconciled, elements) == [
            f"unit 'Probe Unit': {REASON_PATTERN}",
            f"compu method 'ProbeTable' entry 'Probe Off': {REASON_PATTERN}",
        ]

    def test_an_import_stating_one_is_aborted_with_nothing_written(self, tmp_path: Path) -> None:
        from test_data_description_import import _import, application, package, texttable

        with pytest.raises(ValueError, match="Import aborted with nothing written") as raised:
            _import(
                tmp_path,
                package(
                    "ProbeTypes",
                    texttable("ProbeTable", ((0, "Probe Off", ""), (1, "ProbeOn", "")))
                    + application("ProbeMode", compu="/ProbeTypes/ProbeTable"),
                ),
            )

        assert "compu method 'ProbeTable' entry 'Probe Off'" in str(raised.value)
        model_root = tmp_path / "proj" / "model"
        assert not list((model_root / "data_types").rglob("*.yaml"))
