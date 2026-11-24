"""The importers record package placement, once.

Two declared elements that would share a root package (``/DataTypes``, ``/PortInterfaces`` or
``/ComponentTypes``) under names differing only in case break ``[constr_2508]`` (a ``SHORT-NAME`` is
unique ignoring case within its package). An importer keeps both, under their own names, and records
a ``package`` on the one that moves: the last segment of its own source package. This module holds:

* **the placement decision** (``reconcile.place_case_variants``) on hand-built members: the order that
  picks the member which stays, the *taken* rule, the reuse of a sub-package, the ``Identifier`` cap
  and the ``CaseVariant`` fallback, the same-source-package pair and the member with nowhere to go;
* **``import model``** on hand-built exports: the placement recorded and reported above the skipped
  list, never a skip; the same-package pair skipped with a ``[constr_2508]`` line; the reserved-name
  pass comparing a generated claim ignoring case; a declared pair placed and never renamed; the
  fail-closed check; the round trip;
* **``import component``**: a placed element kept, a collider placed, a root twin refused, target
  elements never moved, the package level checked once over the model the import leaves, and
  ``--update``'s carries;
* **the dumps**: ``package`` kept through an edit that does not touch it.

What it does **not** reach: real tool exports (the closed suites), and the name-space rule itself.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    _write_manifest,
    make_empty_model_root,
    model_element_path,
    model_element_storage_name,
    write_model_element,
)
from project_support import files_of, init_project
from swcraft_tools.arxml_import import orchestrator, run_import
from swcraft_tools.arxml_import.reconcile import (
    CASE_VARIANT_PACKAGE,
    CaseVariantPlacement,
    PlacementMember,
    place_case_variants,
    reserved_data_type_names,
    reserved_interface_names,
    reserved_swc_names,
)
from swcraft_tools.arxml_import.report import package_placement_heading
from swcraft_tools.arxml_reader import SkipCategory, StandardRule
from swcraft_tools.component_exchange import export_component, import_component
from swcraft_tools.component_exchange.import_ import ReservedNameError
from swcraft_tools.component_exchange.name_policies import reserved_name_violations
from swcraft_tools.data_description import read_data_description
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.reader import (
    read_compositions,
    read_data_types,
    read_interfaces,
    read_layer,
    read_mapping_sets,
    read_swc_model,
)
from swcraft_tools.schema import (
    ApplicationDataType,
    EnumDataType,
    EnumLiteral,
    SRInterface,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.validator import NAME_SPACE_LEVEL_PACKAGE, name_space_collisions
from swcraft_tools.writer import generate_arxml, mutations
from test_component_import import _dump, _physical_source

_NS = "http://autosar.org/schema/r4.0"

# ---------------------------------------------------------------------------
# Hand-built exports
# ---------------------------------------------------------------------------


def _base(name: str, size: int = 8) -> str:
    return (
        f"<SW-BASE-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        f"<BASE-TYPE-SIZE>{size}</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
        f"<NATIVE-DECLARATION>uint{size}</NATIVE-DECLARATION></SW-BASE-TYPE>"
    )


def _value(name: str, base_path: str) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{base_path}</BASE-TYPE-REF>'
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        "</IMPLEMENTATION-DATA-TYPE>"
    )


#: A method stating the physical-to-internal direction and a unit stating an SI relation are no
#: element, so a type naming one reads it inline and still generates its private form.
_NO_ELEMENT_COMPU = (
    "<COMPU-PHYS-TO-INTERNAL><COMPU-SCALES><COMPU-SCALE><COMPU-CONST><VT>OFF</VT></COMPU-CONST>"
    "</COMPU-SCALE></COMPU-SCALES></COMPU-PHYS-TO-INTERNAL>"
)
_NO_ELEMENT_UNIT = "<FACTOR-SI-TO-UNIT>1</FACTOR-SI-TO-UNIT>"


def _compu(name: str, *, inline: bool = False) -> str:
    return (
        f"<COMPU-METHOD><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
        "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
        '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT><UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
        "<COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE><COMPU-SCALE>"
        '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT><UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>'
        "<COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE>"
        "</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>" + (_NO_ELEMENT_COMPU if inline else "") + "</COMPU-METHOD>"
    )


def _enum(name: str, method_path: str) -> str:
    return (
        f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f'<COMPU-METHOD-REF DEST="COMPU-METHOD">{method_path}</COMPU-METHOD-REF>'
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        "</APPLICATION-PRIMITIVE-DATA-TYPE>"
    )


def _unit(name: str, *, inline: bool = False) -> str:
    extra = _NO_ELEMENT_UNIT if inline else ""
    return f"<UNIT><SHORT-NAME>{name}</SHORT-NAME><DISPLAY-NAME>{name}</DISPLAY-NAME>{extra}</UNIT>"


def _application(name: str, unit_path: str) -> str:
    return (
        f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f'<UNIT-REF DEST="UNIT">{unit_path}</UNIT-REF>'
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        "</APPLICATION-PRIMITIVE-DATA-TYPE>"
    )


def _sender_receiver(name: str, element: str, type_path: str, dest: str = "IMPLEMENTATION-DATA-TYPE") -> str:
    return (
        f"<SENDER-RECEIVER-INTERFACE><SHORT-NAME>{name}</SHORT-NAME><IS-SERVICE>false</IS-SERVICE>"
        f"<DATA-ELEMENTS><VARIABLE-DATA-PROTOTYPE><SHORT-NAME>{element}</SHORT-NAME>"
        f'<TYPE-TREF DEST="{dest}">{type_path}</TYPE-TREF>'
        "</VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
    )


def _mapping_set(name: str, application_path: str, implementation_path: str) -> str:
    return (
        f"<DATA-TYPE-MAPPING-SET><SHORT-NAME>{name}</SHORT-NAME><DATA-TYPE-MAPS><DATA-TYPE-MAP>"
        f'<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">{application_path}'
        "</APPLICATION-DATA-TYPE-REF>"
        f'<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">{implementation_path}'
        "</IMPLEMENTATION-DATA-TYPE-REF>"
        "</DATA-TYPE-MAP></DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>"
    )


def _component(name: str) -> str:
    return f"<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME></APPLICATION-SW-COMPONENT-TYPE>"


def _composition(name: str) -> str:
    return f"<COMPOSITION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME></COMPOSITION-SW-COMPONENT-TYPE>"


def _export(packages: dict[str, list[str]], *, variation_point: bool = False) -> str:
    """An AUTOSAR 4.3.0 file declaring each list of elements in the package its key names.

    Keys are absolute package paths; the packages are nested and emitted in the
    order the dict gives them, so a test can declare an order the paths do not.
    """
    tree: dict[str, Any] = {}
    for path, elements in packages.items():
        node = tree
        for segment in path.strip("/").split("/"):
            node = node.setdefault(segment, {})
        node.setdefault("", []).extend(elements)

    def render(node: dict[str, Any]) -> str:
        parts = []
        for segment, child in node.items():
            if segment == "":
                continue
            own = "".join(child.get("", []))
            nested = render(child)
            parts.append(
                f"<AR-PACKAGE><SHORT-NAME>{segment}</SHORT-NAME>"
                + (f"<ELEMENTS>{own}</ELEMENTS>" if own else "")
                + (f"<AR-PACKAGES>{nested}</AR-PACKAGES>" if nested else "")
                + "</AR-PACKAGE>"
            )
        return "".join(parts)

    body = render(tree)
    if variation_point:
        body += (
            "<AR-PACKAGE><SHORT-NAME>Variants</SHORT-NAME><ELEMENTS><APPLICATION-SW-COMPONENT-TYPE>"
            "<SHORT-NAME>VariantSwc</SHORT-NAME><VARIATION-POINT><SHORT-LABEL>vp</SHORT-LABEL></VARIATION-POINT>"
            "</APPLICATION-SW-COMPONENT-TYPE></ELEMENTS></AR-PACKAGE>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<AUTOSAR xmlns="{_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{_NS} AUTOSAR_4-3-0.xsd"><AR-PACKAGES>{body}</AR-PACKAGES></AUTOSAR>'
    )


def _import(tmp_path: Path, text: str, name: str = "project") -> tuple[Path, Any]:
    """``run_import`` of *text* into a fresh project; returns the model root and the report."""
    project = tmp_path / name
    make_empty_model_root(project)
    manifest = _write_manifest(project)
    source = tmp_path / f"{name}.arxml"
    source.write_text(text, encoding="utf-8", newline="\n")
    return project / "model", run_import(str(source), str(manifest))


def _both_type_layers(root: str) -> list[Any]:
    """Every element of the data description layers: ``base_types``, ``application_types``, ``data_types``."""
    return [*read_layer(root, "base_types"), *read_layer(root, "application_types"), *read_data_types(root)]


def _packages(model_root: Path) -> dict[str, str | None]:
    """Every data type's, interface's, component's, composition's and mapping set's ``package``, by name."""
    root = str(model_root)
    elements = [
        *_both_type_layers(root),
        *read_interfaces(root),
        *read_swc_model(root),
        *read_compositions(root),
        *read_mapping_sets(root),
    ]
    return {element.name: getattr(element, "package", None) for element in elements}


def _package_collisions(model_root: Path) -> list[Any]:
    root = str(model_root)
    return [
        collision
        for collision in name_space_collisions(
            read_swc_model(root),
            read_interfaces(root),
            _both_type_layers(root),
            read_compositions(root),
            [],
            read_mapping_sets(root),
            read_data_description(root).elements,
        )
        if collision.level == NAME_SPACE_LEVEL_PACKAGE
    ]


#: Two base types differing only in case, from two source packages. By full
#: source path ``/Acme/Foundation/…`` sorts first, so ``SInt8`` stays in the root
#: and takes the plain file, and ``sint8`` moves to its own segment.
_TWO_PACKAGES = {
    "/Acme/Foundation/Types": [_base("SInt8")],
    "/Acme/Platform/PlatformTypes": [_base("sint8")],
}


# ---------------------------------------------------------------------------
# The decision, on hand-built members
# ---------------------------------------------------------------------------


def _member(name: str, path: str, *, generated: tuple[str, ...] = (), **kwargs: Any) -> PlacementMember:
    return PlacementMember("data_types", name, path, generated, **kwargs)


def _placed(decision: CaseVariantPlacement) -> dict[str, str]:
    return {placed.declaration.name: placed.package for placed in decision.placed}


class TestThePlacementDecision:
    def test_the_first_by_source_path_stays_and_the_other_takes_its_segment(self) -> None:
        decision = place_case_variants(
            "DataTypes", [_member("sint8", "/P/Platform/sint8"), _member("SInt8", "/F/Types/SInt8")]
        )
        assert _placed(decision) == {"sint8": "Platform"}
        assert decision.same_package == () and decision.unplaceable == ()

    def test_rule_one_a_member_at_the_plain_root_path_stays_although_it_sorts_after(self) -> None:
        decision = place_case_variants(
            "DataTypes", [_member("zeta", "/DataTypes/zeta"), _member("Zeta", "/DataTypes/Alpha/Zeta")]
        )
        assert "/DataTypes/Alpha/Zeta" < "/DataTypes/zeta"
        assert _placed(decision) == {"Zeta": "Alpha"}

    def test_a_single_spelling_is_no_collision(self) -> None:
        decision = place_case_variants("DataTypes", [_member("A", "/X/A"), _member("B", "/Y/B")])
        assert decision == CaseVariantPlacement()

    def test_the_result_does_not_depend_on_the_order_of_the_members(self) -> None:
        members = [
            _member("Speed", "/A/One/Speed"),
            _member("speed", "/B/Shared/speed"),
            _member("Gear", "/A/One/Gear"),
            _member("gear", "/B/Shared/gear"),
        ]
        forward = place_case_variants("DataTypes", members)
        backward = place_case_variants("DataTypes", list(reversed(members)))
        assert forward == backward
        assert _placed(forward) == {"gear": "Shared", "speed": "Shared"}, "two groups share one sub-package"

    def test_a_segment_named_like_a_root_element_is_taken(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("SInt8", "/F/Types/SInt8"),
                _member("sint8", "/P/PlatformTypes/sint8"),
                _member("PlatformTypes", "/F/Types/PlatformTypes"),
            ],
        )
        assert _placed(decision) == {"sint8": "PlatformTypes_2"}

    def test_a_segment_named_like_a_generated_form_ignoring_case_is_taken(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("Mode", "/F/Types/Mode", generated=("Mode_CompuMethod",)),
                _member("Level", "/F/Types/Level"),
                _member("level", "/P/mode_compumethod/level"),
            ],
        )
        assert _placed(decision) == {"level": "mode_compumethod_2"}

    def test_a_sub_package_another_group_chose_in_another_spelling_is_taken(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("Alpha", "/A/One/Alpha"),
                _member("alpha", "/B/Shared/alpha"),
                _member("Beta", "/A/One/Beta"),
                _member("beta", "/B/SHARED/beta"),
            ],
        )
        assert _placed(decision) == {"alpha": "Shared", "beta": "SHARED_2"}

    def test_a_sub_package_is_reused_only_while_no_name_inside_it_collides(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("Alpha", "/A/One/Alpha"),
                _member("alpha", "/B/Shared/alpha", generated=("alpha_CompuMethod",)),
                _member("ALPHA_COMPUMETHOD", "/A/One/ALPHA_COMPUMETHOD"),
                _member("Alpha_CompuMethod", "/C/Shared/Alpha_CompuMethod"),
            ],
        )
        # `alpha` took `Shared` with its generated `alpha_CompuMethod`, so the
        # second group's member, whose own segment is also `Shared`, may not share it.
        assert _placed(decision) == {"alpha": "Shared", "Alpha_CompuMethod": "Shared_2"}

    def test_a_sub_package_another_member_of_the_same_group_took_is_taken(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [_member("Speed", "/A/One/Speed"), _member("speed", "/B/Types/speed"), _member("SPEED", "/C/Types/SPEED")],
        )
        assert _placed(decision) == {"SPEED": "Types_2", "speed": "Types"}

    def test_a_127_character_segment_stays_within_the_identifier_limit_with_its_suffix(self) -> None:
        segment = "S" * 127
        decision = place_case_variants(
            "DataTypes",
            [_member("Speed", "/A/One/Speed"), _member("speed", f"/B/{segment}/speed"), _member(segment, "/A/One/X")],
        )
        (package,) = _placed(decision).values()
        assert len(package) == MAX_IDENTIFIER_LENGTH and package.endswith("_2")

    @pytest.mark.parametrize("segment", ["Platform Types", "platform-types"])
    def test_a_segment_that_is_no_identifier_falls_back_to_case_variant(self, segment: str) -> None:
        decision = place_case_variants(
            "DataTypes", [_member("SInt8", "/A/F/SInt8"), _member("sint8", f"/B/{segment}/sint8")]
        )
        (placed,) = decision.placed
        assert (placed.package, placed.unused_segment) == (CASE_VARIANT_PACKAGE, segment)

    def test_case_variant_already_taken_moves_to_case_variant_2(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("SInt8", "/A/F/SInt8"),
                _member("sint8", "/B/Platform Types/sint8"),
                _member(CASE_VARIANT_PACKAGE, "/A/F/CaseVariant"),
            ],
        )
        assert _placed(decision) == {"sint8": f"{CASE_VARIANT_PACKAGE}_2"}

    def test_two_spellings_of_one_source_package_are_a_pair_and_neither_is_placed(self) -> None:
        decision = place_case_variants(
            "DataTypes", [_member("speed_t", "/A/Types/speed_t"), _member("Speed_T", "/A/Types/Speed_T")]
        )
        (pair,) = decision.same_package
        assert (pair.kept.name, pair.dropped.name) == ("Speed_T", "speed_t"), "the first by full path is kept"
        assert decision.placed == ()

    def test_an_exact_repeat_of_the_member_that_stays_is_left_to_model_identity(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [
                _member("SInt8", "/A/X/SInt8"),
                _member("SInt8", "/B/Y/SInt8"),
                _member("sint8", "/C/Z/sint8"),
            ],
        )
        assert _placed(decision) == {"sint8": "Z"}
        assert decision.same_package == () and decision.unplaceable == ()

    def test_a_fixed_member_stays_and_a_root_level_twin_has_nowhere_to_go(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [_member("speed_t", "", fixed=True), _member("Speed_T", "/DataTypes/Speed_T")],
        )
        (twin,) = decision.unplaceable
        assert (twin.declaration.name, twin.twin.name) == ("Speed_T", "speed_t")

    def test_a_fixed_member_never_moves_whatever_the_order(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [_member("Speed_T", "", fixed=True), _member("speed_t", "/A/AppTypes/speed_t")],
        )
        assert _placed(decision) == {"speed_t": "AppTypes"}

    def test_a_member_already_in_a_sub_package_is_not_a_root_collision(self) -> None:
        decision = place_case_variants(
            "DataTypes",
            [_member("Speed_T", "", fixed=True), _member("speed_t", "/DataTypes/Legacy/speed_t", package="Legacy")],
        )
        assert decision == CaseVariantPlacement()


# ---------------------------------------------------------------------------
# import model
# ---------------------------------------------------------------------------


class TestImportModelPlacesACaseOnlyPair:
    def test_the_member_that_stays_takes_the_plain_file_and_the_other_its_segment(self, tmp_path: Path) -> None:
        model_root, report = _import(tmp_path, _export(_TWO_PACKAGES))

        assert _packages(model_root) == {"SInt8": None, "sint8": "PlatformTypes"}
        plain = yaml.safe_load((model_element_path(model_root, "SInt8", kind="base_type")).read_text(encoding="utf-8"))
        assert plain["name"] == "SInt8" and "package" not in plain
        suffixed = yaml.safe_load(
            (model_element_path(model_root, "sint8~1", kind="base_type")).read_text(encoding="utf-8")
        )
        assert suffixed["package"] == "PlatformTypes"

    def test_the_report_names_it_above_the_skipped_list_and_skips_nothing(self, tmp_path: Path) -> None:
        _model_root, report = _import(tmp_path, _export(_TWO_PACKAGES))

        assert report.package_placement_lines == [
            "data type 'sint8' (/Acme/Platform/PlatformTypes/sint8) placed in /DataTypes/Base/PlatformTypes"
        ]
        assert report.total_skipped == 0
        assert not any("sint8" in entry.path for entry in report.skip_entries)
        text = report.render()
        heading = text.index("Placed in a sub-package to keep a case-insensitive name unique (1;")
        assert text.index("Files suffixed for a case-insensitive collision") < heading
        # The report no longer closes with a baseline-requirement line.
        assert "Generated baseline requirement" not in text

    def test_the_imported_project_holds_no_package_level_group_and_generates(self, tmp_path: Path) -> None:
        model_root, _report = _import(tmp_path, _export(_TWO_PACKAGES))

        assert _package_collisions(model_root) == []
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(model_root), str(output))
        text = output.read_text(encoding="utf-8")
        assert "<SHORT-NAME>PlatformTypes</SHORT-NAME>" in text

    def test_rule_one_keeps_the_member_at_the_plain_root_path(self, tmp_path: Path) -> None:
        model_root, _report = _import(
            tmp_path, _export({"/DataTypes/Base": [_base("zeta")], "/DataTypes/Base/Alpha": [_base("Zeta")]})
        )
        assert _packages(model_root) == {"zeta": None, "Zeta": "Alpha"}

    def test_two_runs_over_permuted_files_give_equal_models(self, tmp_path: Path) -> None:
        forward = {
            "/A/One": [_base("Speed"), _base("Gear")],
            "/B/Shared": [_base("speed"), _base("gear")],
        }
        backward = {
            "/B/Shared": [_base("gear"), _base("speed")],
            "/A/One": [_base("Gear"), _base("Speed")],
        }
        first, _ = _import(tmp_path, _export(forward), "forward")
        second, _ = _import(tmp_path, _export(backward), "backward")

        dump = sorted((dt.model_dump() for dt in _both_type_layers(str(first))), key=lambda item: item["name"])
        assert dump == sorted((dt.model_dump() for dt in _both_type_layers(str(second))), key=lambda item: item["name"])
        assert _packages(first) == {"Speed": None, "Gear": None, "speed": "Shared", "gear": "Shared"}

    def test_a_root_element_named_like_the_segment_takes_it(self, tmp_path: Path) -> None:
        model_root, _report = _import(tmp_path, _export({**_TWO_PACKAGES, "/Acme/Other": [_base("PlatformTypes", 16)]}))
        assert _packages(model_root)["sint8"] == "PlatformTypes_2"

    @pytest.mark.parametrize("segment", ["Platform Types", "platform-types"])
    def test_an_illegal_segment_falls_back_and_is_named_in_the_report(self, tmp_path: Path, segment: str) -> None:
        model_root, report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Foundation/Types": [_base("SInt8"), _base(CASE_VARIANT_PACKAGE, 16)],
                    f"/Acme/Platform/{segment}": [_base("sint8")],
                }
            ),
        )

        assert _packages(model_root)["sint8"] == f"{CASE_VARIANT_PACKAGE}_2"
        (line,) = report.package_placement_lines
        assert line.endswith(f"-- its source package name '{segment}' is not a legal AUTOSAR Identifier")
        assert _package_collisions(model_root) == []


class TestTwoSpellingsOfOneSourcePackage:
    _SAME = {"/Acme/Types": [_base("Speed_T"), _base("speed_t")]}

    def test_the_second_by_path_is_skipped_in_section_one_and_nothing_is_placed(self, tmp_path: Path) -> None:
        model_root, report = _import(tmp_path, _export(self._SAME))

        assert _packages(model_root) == {"Speed_T": None}
        (entry,) = [entry for entry in report.skip_entries if entry.path == "/Acme/Types/speed_t"]
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)
        assert "differs only in case from 'Speed_T' (/Acme/Types/Speed_T)" in entry.reason
        assert "SW-BASE-TYPE (R4.3.0 [constr_2508]): its SHORT-NAME differs only in case" in report.render()
        assert report.package_placement_lines == []

    def test_a_variant_rich_file_puts_the_line_in_section_two(self, tmp_path: Path) -> None:
        _model_root, report = _import(tmp_path, _export(self._SAME, variation_point=True))

        (entry,) = [entry for entry in report.skip_entries if entry.path == "/Acme/Types/speed_t"]
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert "SWCraft binds no variant" in entry.reason


class TestADeclaredNameCollidingWithAGeneratedOneIsRenamedIgnoringCase:
    """The reserved-name pass runs before placement, ignoring case for a generated claim."""

    @pytest.mark.parametrize("second_package", ["/Acme/Types", "/Acme/Other"], ids=["one-package", "two-packages"])
    def test_a_case_only_compu_method_name_moves_like_the_exact_one(self, tmp_path: Path, second_package: str) -> None:
        """A case-only compu method name moves like the exact one.

        A form collides only with a type of its owner's package, so the occupant is an application enum of ``Gear_T``'s
        package. The table is no element (it states the physical-to-internal direction), so ``Gear_T`` states its
        literals inline and generates ``Gear_T_CompuMethod``."""
        packages: dict[str, list[str]] = {
            "/Acme/Types": [_base("uint8"), _compu("GearScale", inline=True), _enum("Gear_T", "/Acme/Types/GearScale")],
        }
        packages.setdefault(second_package, []).append(_enum("gear_t_compumethod", "/Acme/Types/GearScale"))
        model_root, report = _import(tmp_path, _export(packages))

        names = set(_packages(model_root))
        assert "gear_t_compumethod_app" in names and "gear_t_compumethod" not in names
        assert any("gear_t_compumethod" in line and "gear_t_compumethod_app" in line for line in report.rename_lines)
        assert report.package_placement_lines == []

    def test_a_case_only_implementation_name_moves(self, tmp_path: Path) -> None:
        model_root, _report = _import(
            tmp_path,
            _export({"/Acme/Swcs": [_component("Brake")], "/Acme/Other": [_component("brake_implementation")]}),
        )
        assert {"Brake", "brake_implementation_app"} <= set(_packages(model_root))

    def test_a_case_only_init_constant_name_moves(self, tmp_path: Path) -> None:
        model_root, _report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Types": [_base("uint8"), _value("u8", "/Acme/Types/uint8")],
                    "/Acme/Ifaces": [
                        _sender_receiver("ISig", "v", "/Acme/Types/u8"),
                        _sender_receiver("isig_v_init", "w", "/Acme/Types/u8"),
                    ],
                }
            ),
        )
        assert {"ISig", "isig_v_init_app"} <= set(_packages(model_root))

    def test_a_case_only_unit_name_moves(self, tmp_path: Path) -> None:
        """The occupant is an application type of the owner's package, where a form collides.

        The unit is no element (it states an SI relation), so ``Speed`` states it inline and generates
        ``Speed_Unit``."""
        model_root, _report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Types": [
                        _base("uint8"),
                        _unit("Kmh", inline=True),
                        _application("Speed", "/Acme/Types/Kmh"),
                    ],
                    "/Acme/Other": [_application("speed_unit", "/Acme/Types/Kmh")],
                }
            ),
        )
        assert {"Speed", "speed_unit_app"} <= set(_packages(model_root))

    def test_a_mapping_set_named_like_a_generated_form_keeps_its_name(self, tmp_path: Path) -> None:
        """A mapping set lies in ``MappingSets`` and may take a form's name: neither renamed nor placed."""
        model_root, report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Types": [
                        _base("uint8"),
                        _value("u8", "/Acme/Types/uint8"),
                        _compu("GearScale"),
                        _enum("Gear_T", "/Acme/Types/GearScale"),
                    ],
                    "/Acme/Maps": [_mapping_set("gear_t_compumethod", "/Acme/Types/Gear_T", "/Acme/Types/u8")],
                }
            ),
        )
        assert {mapping_set.name for mapping_set in read_mapping_sets(str(model_root))} == {"gear_t_compumethod"}
        assert not any("gear_t_compumethod" in line for line in report.rename_lines)
        assert report.package_placement_lines == []


class TestTwoDeclaredElementsArePlacedNeverRenamed:
    def test_a_mapping_set_and_a_data_type_differing_only_in_case(self, tmp_path: Path) -> None:
        """The two lie in two layer packages, ``MappingSets`` and ``Implementation``: neither placed nor renamed."""
        model_root, report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Data": [
                        _base("uint8"),
                        _value("types", "/Acme/Data/uint8"),
                        _unit("Kmh"),
                        _application("Speed", "/Acme/Data/Kmh"),
                    ],
                    "/Acme/Mappings": [_mapping_set("Types", "/Acme/Data/Speed", "/Acme/Data/types")],
                }
            ),
        )
        packages = _packages(model_root)
        assert (packages["types"], packages["Types"]) == (None, None)
        assert not any("Types" in line for line in report.rename_lines)
        assert report.package_placement_lines == []
        assert _package_collisions(model_root) == []

    def test_an_exact_mapping_set_and_data_type_pair_keeps_both_names(self, tmp_path: Path) -> None:
        """The two lie in ``MappingSets`` and ``Implementation``: the set keeps its name with no rename line."""
        model_root, report = _import(
            tmp_path,
            _export(
                {
                    "/Acme/Types": [
                        _base("uint8"),
                        _value("Types", "/Acme/Types/uint8"),
                        _unit("Kmh"),
                        _application("Speed", "/Acme/Types/Kmh"),
                    ],
                    "/Acme/Mappings": [_mapping_set("Types", "/Acme/Types/Speed", "/Acme/Types/Types")],
                }
            ),
        )
        assert {mapping_set.name for mapping_set in read_mapping_sets(str(model_root))} == {"Types"}
        assert "Types" in _packages(model_root)
        assert not any("Types" in line for line in report.rename_lines)
        assert _package_collisions(model_root) == []

    def test_a_component_and_a_composition_differing_only_in_case(self, tmp_path: Path) -> None:
        model_root, report = _import(
            tmp_path, _export({"/Acme/Alpha": [_component("Brake")], "/Acme/Comps": [_composition("brake")]})
        )
        packages = _packages(model_root)
        assert (packages["Brake"], packages["brake"]) == (None, "Comps")
        assert report.package_placement_lines == [
            "composition 'brake' (/Acme/Comps/brake) placed in /ComponentTypes/Comps"
        ]


class TestTheFailClosedCheckAfterPlacement:
    def test_a_group_placement_did_not_resolve_is_refused_with_nothing_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crafted case the rules did not foresee: placement disabled, so the pair stays in one package."""
        monkeypatch.setattr(orchestrator, "place_case_variants", lambda root, members: CaseVariantPlacement())
        project = tmp_path / "project"
        model_root = make_empty_model_root(project)
        before = files_of(model_root)
        manifest = _write_manifest(project)
        source = tmp_path / "export.arxml"
        source.write_text(_export(_TWO_PACKAGES), encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"package '/DataTypes/Base' holds names equal ignoring case"):
            run_import(str(source), str(manifest))

        assert files_of(model_root) == before


class TestTheRoundTripInProcess:
    """Import, build, import the output: equal models, equal ``package`` values."""

    _EXPORT = {
        "/Acme/Foundation/Types": [_base("SInt8"), _value("Level", "/Acme/Foundation/Types/SInt8")],
        "/Acme/Platform/PlatformTypes": [_base("sint8"), _value("counter", "/Acme/Platform/PlatformTypes/sint8")],
        "/Acme/Platform/Zeta": [_value("Counter", "/Acme/Platform/PlatformTypes/sint8")],
        "/Acme/Ifaces": [_sender_receiver("ICount", "count", "/Acme/Platform/Zeta/Counter")],
    }

    def test_the_second_import_records_the_same_packages(self, tmp_path: Path) -> None:
        first, _ = _import(tmp_path, _export(self._EXPORT), "first")
        output = tmp_path / "built" / "model.arxml"
        generate_arxml(str(first), str(output))
        project = tmp_path / "second"
        make_empty_model_root(project)
        manifest = _write_manifest(project)
        run_import(str(output), str(manifest))
        second = project / "model"

        assert _packages(first) == {
            "SInt8": None,
            "sint8": "PlatformTypes",
            "Level": None,
            "counter": None,
            "Counter": "Zeta",
            "ICount": None,
        }
        assert _packages(second) == _packages(first)
        for reader in (_both_type_layers, read_interfaces):
            assert sorted((e.model_dump() for e in reader(str(second))), key=lambda item: item["name"]) == sorted(
                (e.model_dump() for e in reader(str(first))), key=lambda item: item["name"]
            )


# ---------------------------------------------------------------------------
# The reserved-name fold, at both gates
# ---------------------------------------------------------------------------


def _application_occupant(name: str) -> ApplicationDataType:
    return ApplicationDataType.model_validate({"name": name, "kind": "application_type"})


class TestTheReservedNameFold:
    """A generated form collides only with a type of its owner's package.

    Every occupant below is an application type, of the enum's and the application type's own package."""

    def test_import_model_folds_every_generated_claim(self) -> None:
        enum = EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="P", value=0)])
        app = ApplicationDataType.model_validate({"name": "Speed", "kind": "application_type", "unit": "km/h"})
        declared = {
            dt.name: dt
            for dt in (
                enum,
                app,
                _application_occupant("gear_t_compumethod"),
                _application_occupant("SPEED_UNIT"),
            )
        }
        assert set(reserved_data_type_names(declared)) == {"gear_t_compumethod", "SPEED_UNIT"}
        assert set(reserved_swc_names({"Brake", "brake_implementation"}, {"Brake"}, set())) == {"brake_implementation"}
        interfaces = [
            SRInterface(
                name="ISig", kind="sender_receiver", data_elements=[{"name": "v", "implementation_type": "u8"}]
            ),
            SRInterface(
                name="ISIG_V_INIT", kind="sender_receiver", data_elements=[{"name": "w", "implementation_type": "u8"}]
            ),
        ]
        assert set(reserved_interface_names(interfaces)) == {"ISIG_V_INIT"}

    def _violations(self, **kwargs: Any) -> list[str]:
        arguments: dict[str, Any] = {
            "swc": SWComponent(name="Brake"),
            "data_types": [],
            "target_swcs": {},
            "target_composition_names": set(),
            "target_types": {},
            "interfaces": [],
            "target_interfaces": {},
        }
        arguments.update(kwargs)
        return reserved_name_violations(**arguments)

    def test_import_component_refuses_a_case_only_implementation_name(self) -> None:
        (line,) = self._violations(target_composition_names={"BRAKE_implementation"})
        assert "SWC-IMPLEMENTATION" in line and "'Brake'" in line

    def test_import_component_refuses_a_case_only_compu_method_name(self) -> None:
        enum = EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="P", value=0)])
        (line,) = self._violations(
            data_types=[_application_occupant("gear_t_compumethod")],
            target_types={("application_types", "Gear_T"): enum},
        )
        assert "gear_t_compumethod" in line and "'Gear_T'" in line

    def test_import_component_refuses_a_case_only_unit_name(self) -> None:
        app = ApplicationDataType.model_validate({"name": "Speed", "kind": "application_type", "unit": "km/h"})
        (line,) = self._violations(
            data_types=[_application_occupant("speed_unit")], target_types={("application_types", "Speed"): app}
        )
        assert "speed_unit" in line

    def test_import_component_refuses_a_case_only_init_name(self) -> None:
        (line,) = self._violations(
            interfaces=[
                SRInterface(
                    name="isig_v_init",
                    kind="sender_receiver",
                    data_elements=[{"name": "w", "implementation_type": "u8"}],
                )
            ],
            target_interfaces={
                "ISig": SRInterface(
                    name="ISig", kind="sender_receiver", data_elements=[{"name": "v", "implementation_type": "u8"}]
                )
            },
        )
        assert "CONSTANT-SPECIFICATION" in line and "'isig_v_init'" in line

    def test_a_folded_unit_name_beside_a_type_that_declares_no_unit_is_legal(self) -> None:
        app = ApplicationDataType.model_validate({"name": "Speed", "kind": "application_type"})
        assert (
            self._violations(
                data_types=[_application_occupant("speed_unit")],
                target_types={("application_types", "Speed"): app},
            )
            == []
        )

    def test_a_case_only_pair_wholly_inside_the_target_is_not_this_imports(self) -> None:
        app = ApplicationDataType.model_validate({"name": "Speed", "kind": "application_type", "unit": "km/h"})
        enum = EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="P", value=0)])
        target = {
            ("application_types", "Speed"): app,
            ("application_types", "speed_unit"): _application_occupant("speed_unit"),
            ("application_types", "Gear_T"): enum,
            ("application_types", "gear_t_compumethod"): _application_occupant("gear_t_compumethod"),
        }
        assert self._violations(target_types=target) == []

    def test_a_name_folded_onto_two_bases_is_one_line_per_base(self) -> None:
        with_unit = {"kind": "application_type", "unit": "km/h"}
        lines = self._violations(
            data_types=[_application_occupant("speed_unit")],
            target_types={
                ("application_types", "Speed"): ApplicationDataType.model_validate({"name": "Speed", **with_unit}),
                ("application_types", "SPEED"): ApplicationDataType.model_validate({"name": "SPEED", **with_unit}),
            },
        )
        assert lines == [
            "data type name 'speed_unit' collides with the UNIT auto-generated for 'SPEED'",
            "data type name 'speed_unit' collides with the UNIT auto-generated for 'Speed'",
        ]

    def test_a_case_only_declared_pair_is_not_refused_and_an_exact_one_is(self) -> None:
        assert self._violations(target_composition_names={"brake"}) == []
        assert self._violations(target_composition_names={"Brake"}) != []


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


def _source(tmp_path: Path, tag: str, *, types: dict[str, dict[str, Any]] | None = None, **swc: Any) -> Path:
    """A source project with one component ``PlaceSwc`` providing ``ISpeed`` (``speed: Speed_T``)."""
    model_root = tmp_path / f"source_{tag}"
    documents = {
        "f32_base": {"name": "f32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
        "Speed_T": {"name": "Speed_T", "kind": "value_type", "base": "f32_base"},
        **(types or {}),
    }
    for name, document in documents.items():
        _dump(model_element_path(model_root, name, kind=document["kind"]), {"native_declaration": "float", **document})
    _dump(
        model_root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "Speed_T"}],
        },
    )
    _dump(
        model_root / "swc" / "body" / "PlaceSwc.yaml",
        {
            "name": "PlaceSwc",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "handle_termination_and_restart": "no_support",
            **swc,
        },
    )
    return model_root


def _exported(tmp_path: Path, source: Path, tag: str) -> Path:
    arxml = tmp_path / f"PlaceSwc_{tag}.arxml"
    export_component(str(source), "PlaceSwc", str(arxml))
    return arxml


def _fix_native_declarations(model_root: Path) -> None:
    for path in sorted((model_root / "data_types").rglob("*.yaml"), key=lambda p: p.as_posix()):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if document.get("kind") != "base_type":
            document.pop("native_declaration", None)
            _dump(path, document)


def _move_to_package(arxml: Path, name: str, root: str, package: str) -> None:
    """Move the element ``/{root}/{name}`` into a new top-level ``/{package}``, re-pointing every reference."""
    ET.register_namespace("", _NS)
    tree = ET.parse(arxml)
    document = tree.getroot()
    short = f"{{{_NS}}}SHORT-NAME"
    for parent in document.iter():
        for child in list(parent):
            if child.findtext(short) == name and parent.tag == f"{{{_NS}}}ELEMENTS":
                parent.remove(child)
                packages = document.find(f"{{{_NS}}}AR-PACKAGES")
                new = ET.SubElement(packages, f"{{{_NS}}}AR-PACKAGE")
                ET.SubElement(new, short).text = package
                ET.SubElement(new, f"{{{_NS}}}ELEMENTS").append(copy.deepcopy(child))
                old, new = f"/{root}/{name}", f"/{package}/{name}"
                for element in document.iter():
                    if element.text is not None and (element.text == old or element.text.startswith(old + "/")):
                        element.text = new + element.text[len(old) :]
                tree.write(arxml, xml_declaration=True, encoding="UTF-8")
                return
    raise AssertionError(f"no element {name!r} in {arxml}")


def _target(tmp_path: Path, name: str = "target") -> Path:
    return init_project(tmp_path / name) / "model"


def _read(model_root: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


class TestImportComponentCreate:
    def test_an_incoming_placed_element_keeps_its_package(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "placed")
        _fix_native_declarations(source)
        write_model_element(source, {"name": "Speed_T", "kind": "value_type", "base": "f32_base", "package": "Legacy"})
        arxml = _exported(tmp_path, source, "placed")
        model_root = _target(tmp_path)

        report = import_component(str(arxml), str(model_root))

        assert _read(model_root, model_element_storage_name("Speed_T", kind="value_type"))["package"] == "Legacy"
        assert report.package_placement_lines == []

    def test_an_incoming_collider_with_a_source_segment_is_placed(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "collider")
        _fix_native_declarations(source)
        arxml = _exported(tmp_path, source, "collider")
        _move_to_package(arxml, "Speed_T", "DataTypes/Implementation", "AppTypes")
        model_root = _target(tmp_path)
        write_model_element(model_root, {"name": "speed_t", "kind": "value_type", "base": "uint8"})
        write_model_element(
            model_root,
            {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        occupant = (model_element_path(model_root, "speed_t", kind="value_type")).read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert {dt.name: dt.package for dt in read_data_types(str(model_root))}["Speed_T"] == "AppTypes"
        assert (model_element_path(model_root, "speed_t", kind="value_type")).read_bytes() == occupant, (
            "a target element never moves"
        )
        assert report.package_placement_lines == [
            "data type 'Speed_T' (/AppTypes/Speed_T) placed in /DataTypes/Implementation/AppTypes"
        ]
        assert _package_collisions(model_root) == []
        # The rendered block: the shared heading, then the line indented under it, right after it.
        lines = report.render().splitlines()
        heading = lines.index(package_placement_heading(1))
        assert (
            lines[heading + 1]
            == "  - data type 'Speed_T' (/AppTypes/Speed_T) placed in /DataTypes/Implementation/AppTypes"
        )

    def test_one_its_file_puts_in_the_root_beside_a_target_twin_is_refused(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "root_twin")
        _fix_native_declarations(source)
        arxml = _exported(tmp_path, source, "root_twin")
        model_root = _target(tmp_path)
        # The twin is a value type, of the incoming Speed_T's layer: a base type lies in another package.
        write_model_element(model_root, {"name": "speed_t", "kind": "value_type", "base": "uint8"})
        write_model_element(
            model_root,
            {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        before = files_of(model_root)

        with pytest.raises(ReservedNameError, match=r"target's data type 'speed_t'.*set package on the target's"):
            import_component(str(arxml), str(model_root))

        assert files_of(model_root) == before

    def test_a_reused_definition_whose_package_names_a_target_element_does_not_refuse(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "reused")
        _fix_native_declarations(source)
        write_model_element(source, {"name": "Speed_T", "kind": "value_type", "base": "f32_base", "package": "Legacy"})
        arxml = _exported(tmp_path, source, "reused")
        model_root = _target(tmp_path)
        _dump(
            model_element_path(model_root, "f32_base", kind="base_type"),
            _read(source, model_element_storage_name("f32_base", kind="base_type")),
        )
        write_model_element(model_root, {"name": "Speed_T", "kind": "value_type", "base": "f32_base"})
        write_model_element(
            model_root,
            {"name": "Legacy", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        shared = (model_element_path(model_root, "Speed_T", kind="value_type")).read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert "Speed_T" in report.reused_data_types
        assert (model_element_path(model_root, "Speed_T", kind="value_type")).read_bytes() == shared

    def test_a_created_definition_that_really_collides_is_refused_with_nothing_written(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "created")
        _fix_native_declarations(source)
        write_model_element(source, {"name": "Speed_T", "kind": "value_type", "base": "f32_base", "package": "Legacy"})
        arxml = _exported(tmp_path, source, "created")
        model_root = _target(tmp_path)
        # A value type, of the incoming Speed_T's layer: its package holds both `legacy` and `Legacy`.
        write_model_element(model_root, {"name": "legacy", "kind": "value_type", "base": "uint8"})
        write_model_element(
            model_root,
            {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        before = files_of(model_root)

        with pytest.raises(
            ReservedNameError, match=r"package '/DataTypes/Implementation' holds names equal ignoring case"
        ):
            import_component(str(arxml), str(model_root))

        assert files_of(model_root) == before

    def test_a_case_only_component_beside_a_target_composition_is_placed(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "component")
        _fix_native_declarations(source)
        arxml = _exported(tmp_path, source, "component")
        _move_to_package(arxml, "PlaceSwc", "ComponentTypes", "Swcs")
        model_root = _target(tmp_path)
        _dump(model_root / "compositions" / "placeswc.yaml", {"name": "placeswc", "title": "t"})

        import_component(str(arxml), str(model_root))

        (swc,) = read_swc_model(str(model_root))
        assert (swc.name, swc.package) == ("PlaceSwc", "Swcs")


class TestImportComponentUpdate:
    def test_the_components_package_is_carried_from_the_target(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "update", package="Other")
        _fix_native_declarations(source)
        arxml = _exported(tmp_path, source, "update")
        model_root = _target(tmp_path)
        import_component(str(arxml), str(model_root))
        stored = model_root / "swc" / "components" / "PlaceSwc.yaml"
        document = _read(model_root, "swc/components/PlaceSwc.yaml")
        document["package"] = "Legacy"
        document["ports"] = []
        _dump(stored, document)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        written = _read(model_root, "swc/components/PlaceSwc.yaml")
        assert written["package"] == "Legacy" and written["ports"]

    def test_a_shared_definition_differing_only_in_package_is_untouched(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "shared")
        _fix_native_declarations(source)
        arxml = _exported(tmp_path, source, "shared")
        model_root = _target(tmp_path)
        import_component(str(arxml), str(model_root))
        target_file = model_element_path(model_root, "Speed_T", kind="value_type")
        _dump(
            target_file,
            {**_read(model_root, model_element_storage_name("Speed_T", kind="value_type")), "package": "Legacy"},
        )
        shared = target_file.read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert "Speed_T" in report.reused_data_types
        assert target_file.read_bytes() == shared

    def test_a_carried_mapping_set_gaining_maps_keeps_the_targets_package(self, tmp_path: Path) -> None:
        """``compare_carried_set`` merges the maps into the target's set; the incoming path is not read."""
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(_physical_source(tmp_path), "TargetSwc", str(arxml))
        target = Path(_physical_source(tmp_path / "target"))
        write_model_element(target, {"name": "PhysMaps", "maps": [], "package": "Legacy"}, layer="mapping_sets")

        report = import_component(str(arxml), str(target), update=True)

        assert report.extended_mapping_sets == {"PhysMaps": ["Spd_T -> u8"]}
        written = _read(target, model_element_storage_name("PhysMaps", layer="mapping_sets"))
        assert written["package"] == "Legacy" and written["maps"]


# ---------------------------------------------------------------------------
# The dumps keep it through an edit that does not touch it
# ---------------------------------------------------------------------------


class TestTheDumpsKeepThePackage:
    def test_a_component_keeps_it_through_add_port(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "dump", package="Legacy")
        mutations.add_port(str(source), "PlaceSwc", "pOther", "provide", "ISpeed")
        document = _read(source, "swc/body/PlaceSwc.yaml")
        assert document["package"] == "Legacy" and list(document)[-1] == "package"

    @pytest.mark.parametrize(
        ("dump", "element"),
        [
            ("composition_to_dict", {"name": "Top", "title": "t", "package": "Legacy"}),
            ("data_type_to_dict", {"name": "u8", "kind": "value_type", "base": "b", "package": "Legacy"}),
            ("mapping_set_to_dict", {"name": "Maps", "package": "Legacy"}),
        ],
    )
    def test_each_create_dump_writes_what_an_unrelated_edit_leaves(self, dump: str, element: dict[str, Any]) -> None:
        from swcraft_tools.schema import Composition, MappingSet
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        loaders = {
            "composition_to_dict": Composition.model_validate,
            "data_type_to_dict": ValueDataType.model_validate,
            "mapping_set_to_dict": MappingSet.model_validate,
        }
        model = loaders[dump](element)
        edited = model.model_copy(update={"description": "an edit that does not touch the package"})
        written = getattr(mutations, dump)(edited) if dump != "data_type_to_dict" else data_type_to_dict(edited)
        assert written["package"] == "Legacy" and list(written)[-1] == "package"


class TestACaseOnlyElementPairIsPlaced:
    """The three data description element layers take part in ``import model``'s case-variant placement.

    ``place_final_packages`` runs over the elements, each pair in its layer's package."""

    @pytest.mark.parametrize(
        ("layer", "reference", "noun", "home"),
        [
            ("units", "unit", "unit", "/DataTypes/Units"),
            ("compu_methods", "compu", "compu method", "/DataTypes/CompuMethods"),
            ("data_constraints", "constr", "data constraint", "/DataTypes/Constraints"),
        ],
    )
    def test_the_second_member_takes_its_source_segment(
        self, tmp_path: Path, layer: str, reference: str, noun: str, home: str
    ) -> None:
        from test_data_description_import import _import, _layer, application, constraint, package, texttable, unit

        form = {"units": unit, "compu_methods": texttable, "data_constraints": constraint}[layer]
        report, model_root = _import(
            tmp_path,
            package("ProbeA", form("ProbeForm") + application("ProbeFirst", **{reference: "/ProbeA/ProbeForm"})),
            package("ProbeB", form("probeform") + application("ProbeSecond", **{reference: "/ProbeB/probeform"})),
        )

        assert {name: document.get("package") for name, document in _layer(model_root, layer).items()} == {
            "ProbeForm": None,
            "probeform": "ProbeB",
        }
        assert report.package_placement_lines == [f"{noun} 'probeform' (/ProbeB/probeform) placed in {home}/ProbeB"]
        assert _package_collisions(model_root) == []
