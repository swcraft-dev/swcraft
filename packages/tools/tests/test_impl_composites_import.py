"""The importer over implementation composites and pointers: both are imported.

Each class pins one importer behaviour on a synthetic export whose names are invented
(``impl_composites_import_support``): the ``STRUCTURE``, ``ARRAY`` and ``DATA_REFERENCE``
shapes imported, each unsupported shape reported and never normalised, the same-name pair
kept under one name in two layers, an incompatible map kept with its finding, the merge and
conflict verdicts on differing composite content, the type stubs in the three composite roles, a
rename propagated through the composite references, an ``init`` shaped on these kinds, and
``--packages`` walking the composite references.

What this module does **not** cover:

* the real tool exports: their acceptance is the closed suites' (``dev.py test-internal``);
* ``import component``, whose round trip over the new kinds is
  ``test_impl_composites_exchange.py::TestTheRoundTrip``;
* the validator's rules themselves -- ``check-model`` is run here only to show that a
  kept map is reported under the rule that owns it, and that a clean import is clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from impl_composites_import_support import (
    APP,
    ARRAY,
    CONSTANTS,
    IMPL,
    INTERFACES,
    MAPS,
    PRIMITIVE,
    RECORD,
    TYPES,
    acme,
    alias,
    app_record,
    app_value,
    application_types,
    array,
    base_types,
    components,
    composites,
    constant,
    constants,
    document,
    impl_ref,
    inline_value_sub,
    interfaces,
    mapping_set,
    maps,
    nested_sub,
    numerical,
    other_category,
    package,
    pointer,
    pointer_to,
    props,
    record_value,
    sr_interface,
    structure,
    sub,
    the_export,
    value,
    write,
)
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.report import INCOMPATIBLE_MAP_HEADING, ImportReport, selection_reach
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.cli import main
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_interfaces, read_layer, read_mapping_sets
from swcraft_tools.schema import (
    ArrayDataType,
    CSInterface,
    ExternalDataType,
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
)
from swcraft_tools.validator import check_cross_layer_consistency


def _init_project(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _import(tmp_path: Path, text: str, name: str = "proj", **options) -> tuple[str, ImportReport]:
    """Write *text* as an export, import it into a fresh project, return its model root and the report."""
    source = write(tmp_path, text, f"{name}.arxml")
    project = _init_project(tmp_path, name)
    report = run_import(str(source), str(project / "swcraft.yaml"), **options)
    return str(require_project(str(project / "swcraft.yaml")).resolved_model_root), report


_DATA_TYPE_LAYERS = ("base_types", "application_types", "implementation_types")


def _types(model_root: str) -> dict[tuple[str, str], object]:
    """Every data type keyed ``(layer, name)``, all three data description layers.

    Each layer is its own name space: a kept same-name pair is one name in two layers, so a
    key by name alone would let one member silently overwrite the other.
    """
    by_key: dict[tuple[str, str], object] = {}
    for layer in _DATA_TYPE_LAYERS:
        for data_type in read_layer(model_root, layer):
            assert (layer, data_type.name) not in by_key, f"{data_type.name} declared twice in {layer}"
            by_key[(layer, data_type.name)] = data_type
    return by_key


def _impl(model_root: str) -> dict[str, object]:
    """The ``implementation_types`` layer by name."""
    return {name: t for (layer, name), t in _types(model_root).items() if layer == "implementation_types"}


def _app(model_root: str) -> dict[str, object]:
    """The ``application_types`` layer by name."""
    return {name: t for (layer, name), t in _types(model_root).items() if layer == "application_types"}


def _names(model_root: str) -> set[str]:
    """Every name any data description layer declares."""
    return {name for _, name in _types(model_root)}


def _maps(model_root: str) -> list[tuple[str, str]]:
    return [(entry.application, entry.implementation) for s in read_mapping_sets(model_root) for entry in s.maps]


def _rules(model_root: str) -> list[str]:
    return [record.rule for record in check_cross_layer_consistency(model_root).records]


def _reasons(report: ImportReport, path: str) -> list[str]:
    return [entry.reason for entry in report.skip_entries if entry.path == path]


@pytest.fixture
def imported(tmp_path: Path) -> tuple[str, ImportReport]:
    return _import(tmp_path, the_export())


# The supported shapes


class TestEachSupportedShape:
    def test_a_structure_is_an_implementation_record_with_its_elements_in_order(self, imported) -> None:
        model_root, _ = imported

        types = _impl(model_root)
        assert types["WheelImpl"] == ImplementationRecordDataType(
            name="WheelImpl",
            kind="implementation_record",
            elements=[ImplementationElement(name="samples", type="Samples")],
        )
        assert [(e.name, e.type) for e in types["Frame"].elements] == [("speed", "u16"), ("flags", "u8")]

    def test_a_fixed_size_array_is_an_implementation_array_keeping_its_element_name(self, imported) -> None:
        model_root, _ = imported

        assert _impl(model_root)["Samples"] == ImplementationArrayDataType(
            name="Samples", kind="implementation_array", element_type="u16", length=4, element_name="Element"
        )

    def test_a_pointer_names_its_target_and_a_pointer_to_a_named_pointer_is_kept(self, imported) -> None:
        model_root, _ = imported

        types = _impl(model_root)
        assert types["FramePtr"] == PointerDataType(name="FramePtr", kind="pointer", target="Frame")
        assert types["FramePtrPtr"] == PointerDataType(name="FramePtrPtr", kind="pointer", target="FramePtr")

    def test_an_alias_of_a_structure_is_a_reference_type_over_it(self, imported) -> None:
        model_root, _ = imported

        assert _impl(model_root)["FrameAlias"] == ReferenceDataType(
            name="FrameAlias", kind="reference_type", base="Frame"
        )

    def test_no_implementation_type_is_skipped(self, imported) -> None:
        _, report = imported

        assert [entry for entry in report.skip_entries if entry.tag == "IMPLEMENTATION-DATA-TYPE"] == []

    def test_a_pointer_typed_argument_keeps_its_type(self, imported) -> None:
        model_root, _ = imported

        (service,) = [i for i in read_interfaces(model_root) if isinstance(i, CSInterface)]
        assert [(a.name, a.implementation_type) for op in service.operations for a in op.arguments] == [
            ("frame", "FramePtr")
        ]

    def test_a_record_declared_before_the_array_its_field_names_is_imported(self, imported) -> None:
        # `Wheel` is declared before the application array its one field names; a record
        # mapped before every array would skip it.
        model_root, report = imported

        wheel = _app(model_root)["Wheel"]
        assert isinstance(wheel, RecordDataType)
        assert [(f.name, f.type) for f in wheel.fields] == [("samples", "Samples")]
        assert _reasons(report, f"{APP}/Wheel") == []

    def test_every_map_of_the_set_is_kept(self, imported) -> None:
        model_root, _ = imported

        assert _maps(model_root) == [
            ("Speed", "u16"),
            ("Flags", "u8"),
            ("Frame", "Frame"),
            ("Samples", "Samples"),
            ("Wheel", "WheelImpl"),
        ]

    def test_the_imported_project_is_check_model_clean(self, imported) -> None:
        model_root, _ = imported

        assert _rules(model_root) == []


# The shapes that stay unsupported

_VOID_TARGET = "<TARGET-CATEGORY>VALUE</TARGET-CATEGORY>" + props(
    f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{TYPES}/uint8</BASE-TYPE-REF>'
)
_INLINE_POINTER_TO_POINTER = "<TARGET-CATEGORY>DATA_REFERENCE</TARGET-CATEGORY>" + props(
    "<SW-POINTER-TARGET-PROPS><TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>"
    + props(impl_ref(f"{TYPES}/u16"))
    + "</SW-POINTER-TARGET-PROPS>"
)
_FUNCTION_TARGET = "<TARGET-CATEGORY>FUNCTION_REFERENCE</TARGET-CATEGORY>"
_CONST_TARGET = "<TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>" + props(
    impl_ref(f"{TYPES}/u16") + "<SW-IMPL-POLICY>CONST</SW-IMPL-POLICY>"
)
_NO_TARGET_REF = "<TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>"
_ELEMENT_WITHOUT_REF = (
    "<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>a</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
    "</IMPLEMENTATION-DATA-TYPE-ELEMENT>"
)
_FIXED_FOUR = "<ARRAY-SIZE>4</ARRAY-SIZE><ARRAY-SIZE-SEMANTICS>FIXED-SIZE</ARRAY-SIZE-SEMANTICS>"

UNSUPPORTED = {
    "inline VALUE member": structure("Bad", inline_value_sub("raw", f"{TYPES}/uint8")),
    "nested inline STRUCTURE": structure("Bad", nested_sub("outer", sub("inner", f"{TYPES}/u16"))),
    "no SUB-ELEMENTS": structure("Bad"),
    "VARIABLE-SIZE": array("Bad", "Element", f"{TYPES}/u16", 4, semantics="VARIABLE-SIZE"),
    "no ARRAY-SIZE-SEMANTICS": array("Bad", "Element", f"{TYPES}/u16", 4, semantics=None),
    "ARRAY-SIZE-HANDLING": array("Bad", "Element", f"{TYPES}/u16", 4, handling="ALL-INDICES-SAME-ARRAY-SIZE"),
    "ARRAY-SIZE": array("Bad", "Element", f"{TYPES}/u16", 0),
    "unsupported category 'UNION'": other_category("Bad", "UNION"),
    "unsupported category 'FUNCTION_REFERENCE'": other_category("Bad", "FUNCTION_REFERENCE"),
    "void": pointer("Bad", _VOID_TARGET),
    "inline pointer to a pointer": pointer("Bad", _INLINE_POINTER_TO_POINTER),
    "TARGET-CATEGORY 'FUNCTION_REFERENCE'": pointer("Bad", _FUNCTION_TARGET),
    "carry SW-IMPL-POLICY, which a pointer does not hold": pointer("Bad", _CONST_TARGET),
    "declares no target IMPLEMENTATION-DATA-TYPE-REF": pointer("Bad", _NO_TARGET_REF),
    "SHORT-NAME(s) ['a'] more than once": structure("Bad", sub("a", f"{TYPES}/u16"), sub("a", f"{TYPES}/u8")),
    "ARRAY declares 2 sub-elements": structure(
        "Bad",
        sub("E1", f"{TYPES}/u16", extra=_FIXED_FOUR),
        sub("E2", f"{TYPES}/u16", extra=_FIXED_FOUR),
        category="ARRAY",
    ),
    "missing or blank SHORT-NAME": structure("Bad", sub(" ", f"{TYPES}/u16")),
    "sub-element 'a' declares no IMPLEMENTATION-DATA-TYPE-REF": structure("Bad", _ELEMENT_WITHOUT_REF),
}


class TestEachUnsupportedShapeIsReported:
    @pytest.mark.parametrize("shape", sorted(UNSUPPORTED))
    def test_the_type_is_skipped_with_a_reason_naming_its_shape(self, tmp_path: Path, shape: str) -> None:
        model_root, report = _import(tmp_path, the_export(types=(*base_types(), *composites(), UNSUPPORTED[shape])))

        assert "Bad" not in _names(model_root)
        (reason,) = _reasons(report, f"{TYPES}/Bad")
        assert shape in reason

    @pytest.mark.parametrize("shape", sorted(UNSUPPORTED))
    def test_nothing_else_is_normalised_in_its_place(self, tmp_path: Path, shape: str) -> None:
        model_root, _ = _import(tmp_path, the_export(types=(*base_types(), *composites(), UNSUPPORTED[shape])))

        # The supported shapes beside it are imported exactly as without it.
        assert set(_types(model_root)) == set(_types(_import(tmp_path, the_export(), "plain")[0]))

    def test_a_type_many_referrers_reach_is_skipped_once(self, tmp_path: Path) -> None:
        """One skip entry per type, not per referrer; each referrer keeps its own."""
        referrers = (
            structure("UserA", sub("bad", f"{TYPES}/Bad")),
            structure("UserB", sub("bad", f"{TYPES}/Bad")),
            alias("BadAlias", f"{TYPES}/Bad"),
        )
        text = the_export(types=(*base_types(), *composites(), UNSUPPORTED["inline VALUE member"], *referrers))
        model_root, report = _import(tmp_path, text)

        assert {"Bad", "UserA", "UserB", "BadAlias"}.isdisjoint(_names(model_root))
        (reason,) = _reasons(report, f"{TYPES}/Bad")
        assert "inline VALUE member" in reason
        for referrer in ("UserA", "UserB", "BadAlias"):
            assert len(_reasons(report, f"{TYPES}/{referrer}")) == 1, referrer

    def test_a_recursive_pointer_graph_is_refused_as_a_cycle(self, tmp_path: Path) -> None:
        graph = (structure("Node", sub("next", f"{TYPES}/NodePtr")), pointer_to("NodePtr", f"{TYPES}/Node"))
        model_root, report = _import(tmp_path, the_export(types=(*base_types(), *composites(), *graph)))

        assert {"Node", "NodePtr"}.isdisjoint(_names(model_root))
        assert "cyclic type reference" in _reasons(report, f"{TYPES}/Node")

    def test_a_map_onto_an_unsupported_shape_is_dropped_with_the_skipped_list_named(self, tmp_path: Path) -> None:
        text = the_export(
            types=(*base_types(), *composites(), UNSUPPORTED["VARIABLE-SIZE"]),
            app=(*application_types(), app_value("Dyn")),
            mapping=(mapping_set("Maps", (f"{APP}/Dyn", PRIMITIVE, f"{TYPES}/Bad")),),
        )
        model_root, report = _import(tmp_path, text)

        assert _maps(model_root) == []
        assert (
            f"implementation side {TYPES}/Bad was not imported as a data type (see the skipped list for its own reason)"
            in _reasons(report, f"{MAPS}/Maps/DATA-TYPE-MAPS")
        )


# The same-name pair: one name in two layers


class TestTheSameNamePairKeepsOneNameInTwoLayers:
    """Application and implementation types are two name spaces: a pair keeps its name in both, no ``_impl`` minted."""

    def test_both_members_keep_the_declared_name_and_nothing_is_renamed(self, imported) -> None:
        model_root, report = imported

        app, impl = _app(model_root), _impl(model_root)
        assert isinstance(app["Frame"], RecordDataType)
        assert isinstance(impl["Frame"], ImplementationRecordDataType)
        assert isinstance(app["Samples"], ArrayDataType)
        assert isinstance(impl["Samples"], ImplementationArrayDataType)
        assert report.rename_lines == []

    def test_an_exports_own_impl_suffixed_name_is_an_ordinary_type(self, tmp_path: Path) -> None:
        """No suffix is minted, so an export's own ``Frame_impl`` contests nothing."""
        text = the_export(types=(*base_types(), *composites(), value("Frame_impl", "uint16")))
        model_root, report = _import(tmp_path, text)

        impl = _impl(model_root)
        # The export's own `Frame_impl` keeps its name and kind ...
        assert impl["Frame_impl"].kind == "value_type"
        # ... and the structure keeps `Frame`, which the map names.
        assert isinstance(impl["Frame"], ImplementationRecordDataType)
        assert ("Frame", "Frame") in _maps(model_root)
        assert report.rename_lines == []


# An incompatible map is kept with its finding


class TestAnIncompatibleMapIsKeptWithItsFinding:
    def test_a_member_count_mismatch_is_kept_and_reported(self, tmp_path: Path) -> None:
        app = (
            app_value("Speed"),
            app_value("Flags"),
            app_record("Wheel", ("samples", f"{APP}/Samples", ARRAY)),
            app_record(
                "Frame",
                ("speed", f"{APP}/Speed", PRIMITIVE),
                ("flags", f"{APP}/Flags", PRIMITIVE),
                ("extra", f"{APP}/Flags", PRIMITIVE),
            ),
            application_types()[-1],
        )
        model_root, report = _import(tmp_path, the_export(app=app))

        assert ("Frame", "Frame") in _maps(model_root)
        assert _rules(model_root) == ["mapping-set.incompatible-composite-map"]
        (line,) = report.incompatible_map_lines
        assert line.startswith("Maps: Frame -> Frame: ") and "implementation record 'Frame'" in line
        rendered = report.render()
        assert f"{INCOMPATIBLE_MAP_HEADING} (1):" in rendered
        assert f"  - {line}" in rendered

    def test_a_map_onto_a_pointer_is_kept_and_reported(self, tmp_path: Path) -> None:
        text = the_export(
            app=(*application_types(), app_value("Handle")),
            mapping=(mapping_set("Maps", (f"{APP}/Handle", PRIMITIVE, f"{TYPES}/FramePtr")),),
        )
        model_root, report = _import(tmp_path, text)

        assert _maps(model_root) == [("Handle", "FramePtr")]
        assert _rules(model_root) == ["mapping-set.pointer-mapped"]
        (line,) = report.incompatible_map_lines
        assert line.startswith("Maps: Handle -> FramePtr: ")
        assert "pointer 'FramePtr'" in line and "[constr_1066]" in line

    def test_renamed_members_are_accepted_and_kept_exactly(self, tmp_path: Path) -> None:
        types = (
            *base_types(),
            structure("Frame", sub("left", f"{TYPES}/u16"), sub("right", f"{TYPES}/u8")),
            *composites()[1:],
        )
        model_root, report = _import(tmp_path, the_export(types=types))

        assert [e.name for e in _impl(model_root)["Frame"].elements] == ["left", "right"]
        assert [f.name for f in _app(model_root)["Frame"].fields] == ["speed", "flags"]
        assert ("Frame", "Frame") in _maps(model_root)
        assert _rules(model_root) == []
        assert report.incompatible_map_lines == []

    def test_a_clean_import_renders_no_incompatible_map_section(self, imported) -> None:
        _, report = imported

        assert report.incompatible_map_lines == []
        assert "composite check" not in report.render()


# The merge and conflict verdicts on differing composite content


def _two_trees(left: str, right: str) -> str:
    return document(
        acme(Types=base_types()),
        package("Left", left),
        package("Right", right),
    )


class TestTheMergeVerdictComparesTheContent:
    def test_two_structures_differing_in_an_element_type_conflict(self, tmp_path: Path) -> None:
        text = _two_trees(
            structure("Shared", sub("a", f"{TYPES}/u16")),
            structure("Shared", sub("a", f"{TYPES}/u8")),
        )
        model_root, report = _import(tmp_path, text)

        assert "Shared" not in _names(model_root)
        (group,) = report.conflicting_groups
        assert group.difference == "SUB-ELEMENTS"

    def test_two_arrays_differing_in_length_conflict(self, tmp_path: Path) -> None:
        text = _two_trees(array("Shared", "E", f"{TYPES}/u16", 4), array("Shared", "E", f"{TYPES}/u16", 5))
        _, report = _import(tmp_path, text)

        (group,) = report.conflicting_groups
        assert group.difference == "SUB-ELEMENTS"

    def test_two_pointers_to_different_targets_conflict(self, tmp_path: Path) -> None:
        text = _two_trees(pointer_to("Shared", f"{TYPES}/u16"), pointer_to("Shared", f"{TYPES}/u8"))
        _, report = _import(tmp_path, text)

        (group,) = report.conflicting_groups
        assert group.difference == "SW-POINTER-TARGET-PROPS"

    def test_two_equal_structures_merge_into_one(self, tmp_path: Path) -> None:
        text = _two_trees(
            structure("Shared", sub("a", f"{TYPES}/u16")),
            structure("Shared", sub("a", f"{TYPES}/u16")),
        )
        model_root, report = _import(tmp_path, text)

        assert report.conflicting_groups == []
        assert len(report.merged_groups) == 1
        assert isinstance(_impl(model_root)["Shared"], ImplementationRecordDataType)


# Type stubs in the three new roles

_REMOTE = "/Remote/Types"


class TestTypeStubsInTheNewRoles:
    def test_one_stub_is_shared_by_an_old_role_and_the_three_new_ones(self, tmp_path: Path) -> None:
        types = (
            *base_types(),
            *composites(),
            alias("ExtAlias", f"{_REMOTE}/Ext"),
            structure("Holder", sub("ext", f"{_REMOTE}/Ext")),
            array("ExtArray", "Element", f"{_REMOTE}/Ext", 2),
            pointer_to("ExtPtr", f"{_REMOTE}/Ext"),
        )
        model_root, report = _import(tmp_path, the_export(types=types))

        types_ = _impl(model_root)
        assert types_["Ext_ext0"] == ExternalDataType(
            name="Ext_ext0", kind="external_type", external_ref=f"{_REMOTE}/Ext", external_dest=IMPL
        )
        assert types_["ExtAlias"].base == "Ext_ext0"
        assert [e.type for e in types_["Holder"].elements] == ["Ext_ext0"]
        assert types_["ExtArray"].element_type == "Ext_ext0"
        assert types_["ExtPtr"].target == "Ext_ext0"
        (stub,) = report.external_type_stubs
        assert sorted(r.field for r in stub.referrers) == [
            "implementation_types.implementation_array.element_type",
            "implementation_types.implementation_element.type",
            "implementation_types.pointer.target",
            "implementation_types.reference_type.base",
        ]

    def test_conflicting_dests_mint_no_stub_and_keep_the_path(self, tmp_path: Path) -> None:
        types = (
            *base_types(),
            *composites(),
            structure("Holder", sub("ext", f"{_REMOTE}/Ext")),
            pointer_to("ExtPtr", f"{_REMOTE}/Ext", dest=PRIMITIVE),
        )
        model_root, report = _import(tmp_path, the_export(types=types))

        types_ = _impl(model_root)
        assert "Ext" not in _names(model_root)
        assert [e.type for e in types_["Holder"].elements] == [f"{_REMOTE}/Ext"]
        assert types_["ExtPtr"].target == f"{_REMOTE}/Ext"
        (disagreement,) = report.dest_disagreements
        assert disagreement.target == f"{_REMOTE}/Ext"

    def test_a_stub_never_takes_a_declared_leaf_name(self, tmp_path: Path) -> None:
        types = (*base_types(), *composites(), structure("Holder", sub("frame", f"{_REMOTE}/Frame")))
        model_root, _ = _import(tmp_path, the_export(types=types))

        types_ = _impl(model_root)
        (stub,) = [t for t in types_.values() if isinstance(t, ExternalDataType)]
        assert stub.name != "Frame" and stub.external_ref == f"{_REMOTE}/Frame"
        assert [e.type for e in types_["Holder"].elements] == [stub.name]

    def test_a_stub_only_an_unselected_element_names_is_pruned_by_packages(self, tmp_path: Path) -> None:
        text = document(
            acme(
                Types=(*base_types(), *composites()),
                AppTypes=application_types(),
                Maps=maps(),
                Interfaces=interfaces(),
                Constants=constants(),
                Components=components(),
            ),
            package("Elsewhere", structure("Holder", sub("ext", f"{_REMOTE}/Ext"))),
        )
        model_root, report = _import(tmp_path, text, packages=["Components"])

        assert "Holder" not in _names(model_root) and "Ext" not in _names(model_root)
        assert report.external_type_stubs == []


# A rename propagated through the new references


class TestARenameIsPropagated:
    def test_a_writer_reserved_name_is_renamed_in_every_new_reference(self, tmp_path: Path) -> None:
        # `Level` is a bounded value type, so the writer generates `Level_Range`
        # beside it and phase B renames the implementation type of that name. The
        # stem is an implementation type because a form collides only with a type
        # of its owner's package.
        bounded = (
            "<DATA-CONSTR><SHORT-NAME>LevelLimits</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
            '<INTERNAL-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">100</UPPER-LIMIT></INTERNAL-CONSTRS>'
            "</DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>\n"
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>Level</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            + props(
                f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{TYPES}/uint8</BASE-TYPE-REF>'
                f'<DATA-CONSTR-REF DEST="DATA-CONSTR">{TYPES}/LevelLimits</DATA-CONSTR-REF>'
            )
            + "</IMPLEMENTATION-DATA-TYPE>\n"
        )
        types = (
            *base_types(),
            *composites(),
            bounded,
            value("Level_Range", "uint8"),
            structure("Holder", sub("level", f"{TYPES}/Level_Range")),
            array("Levels", "Element", f"{TYPES}/Level_Range", 2),
            pointer_to("LevelPtr", f"{TYPES}/Level_Range"),
        )
        model_root, report = _import(tmp_path, the_export(types=types))

        types_ = _impl(model_root)
        assert (types_["Level"].value_min, types_["Level"].value_max) == (0.0, 100.0)
        assert "Level_Range" not in types_
        (renamed,) = [n for n, t in types_.items() if n.startswith("Level_Range")]
        assert [e.type for e in types_["Holder"].elements] == [renamed]
        assert types_["Levels"].element_type == renamed
        assert types_["LevelPtr"].target == renamed
        assert any(renamed in line for line in report.rename_lines)
        assert _rules(model_root) == []


# init on the new kinds


def _elements(model_root: str, interface: str) -> dict[str, object]:
    (sr,) = [i for i in read_interfaces(model_root) if i.name == interface]
    assert isinstance(sr, SRInterface)
    return {element.name: element.init for element in sr.data_elements}


class TestAnInitOnTheNewKinds:
    def test_a_structure_typed_element_takes_a_map_of_its_elements(self, imported) -> None:
        model_root, _ = imported

        assert _elements(model_root, "IFrame")["frame"] == {"speed": 3, "flags": 1}

    def test_an_array_typed_element_takes_a_list_of_its_length(self, imported) -> None:
        model_root, _ = imported

        assert _elements(model_root, "IFrame")["samples"] == [1, 2, 3, 4]

    def test_an_alias_of_a_structure_takes_the_structures_shape(self, imported) -> None:
        model_root, _ = imported

        assert _elements(model_root, "IFrame")["aliased"] == {"speed": 5, "flags": 0}

    def test_a_record_value_of_another_member_count_is_dropped_with_its_reason(self, tmp_path: Path) -> None:
        consts = (
            constant("CFrame", record_value(numerical(3))),
            *constants()[1:],
        )
        model_root, report = _import(tmp_path, the_export(consts=consts))

        assert _elements(model_root, "IFrame")["frame"] is None
        (reason,) = _reasons(report, f"{INTERFACES}/IFrame/frame")
        assert "declares 2 elements" in reason

    def test_a_pointer_typed_element_takes_no_init(self, tmp_path: Path) -> None:
        ifaces = (
            sr_interface("IFrame", ("handle", f"{TYPES}/FramePtr", f"{CONSTANTS}/CZero")),
            interfaces()[1],
        )
        consts = (*constants(), constant("CZero", numerical(0)))
        model_root, report = _import(tmp_path, the_export(ifaces=ifaces, consts=consts))

        assert _elements(model_root, "IFrame")["handle"] is None
        (reason,) = _reasons(report, f"{INTERFACES}/IFrame/handle")
        assert "pointer" in reason and "no init value" in reason

    def test_an_init_reaching_a_stub_inside_a_structure_is_dropped_with_the_stub_named(self, tmp_path: Path) -> None:
        # A member typed through a reference type whose base the export declares
        # nowhere ends at a type stub, and no value can be shaped against it -- the
        # validator refuses such an init, so the import drops it.
        types = (
            *base_types(),
            *composites()[1:],
            alias("ExtAlias", f"{_REMOTE}/Ext"),
            structure("Frame", sub("speed", f"{TYPES}/ExtAlias"), sub("flags", f"{TYPES}/u8")),
        )
        model_root, report = _import(tmp_path, the_export(types=types))

        assert _elements(model_root, "IFrame")["frame"] is None
        reasons = _reasons(report, f"{INTERFACES}/IFrame/frame")
        assert any("external type stub 'Ext_ext0'" in reason for reason in reasons)
        assert _rules(model_root) == []


# --packages walks the new references


class TestPackagesWalkTheNewReferences:
    @staticmethod
    def _split_export() -> str:
        """The composites' leaf types and the pointer's target sit in a package of their own."""
        deep = "/Acme/Deep"
        return document(
            acme(
                Types=(
                    *base_types(),
                    structure("Frame", sub("speed", f"{deep}/Leaf"), sub("flags", f"{TYPES}/u8")),
                    array("Samples", "Element", f"{deep}/Leaf", 4),
                    structure("WheelImpl", sub("samples", f"{TYPES}/Samples")),
                    pointer_to("FramePtr", f"{deep}/Target"),
                    pointer_to("FramePtrPtr", f"{TYPES}/FramePtr"),
                    alias("FrameAlias", f"{TYPES}/Frame"),
                ),
                Deep=(value("Leaf", "uint16"), structure("Target", sub("x", f"{TYPES}/u8"))),
                AppTypes=application_types(),
                Maps=maps(),
                Interfaces=interfaces(),
                Constants=constants(),
                Components=components(),
            )
        )

    def test_the_closure_keeps_every_type_the_new_references_reach(self, tmp_path: Path) -> None:
        model_root, _ = _import(tmp_path, self._split_export(), packages=["Components"])

        assert {"Frame", "Samples", "FramePtr", "Leaf", "Target", "u8"} <= set(_impl(model_root))
        assert {"uint16", "uint8"} <= {name for layer, name in _types(model_root) if layer == "base_types"}
        assert _rules(model_root) == []

    def test_the_selection_reach_follows_the_new_edges(self, tmp_path: Path) -> None:
        model = read_arxml(str(write(tmp_path, self._split_export())))

        inside, reached = selection_reach(model, {"Components"}, decide_merges(model))

        assert {
            f"{TYPES}/Frame",
            f"{TYPES}/Samples",
            f"{TYPES}/FramePtr",
            "/Acme/Deep/Leaf",
            "/Acme/Deep/Target",
        } <= reached

    def test_a_record_reaches_the_implementation_composite_its_map_names(self, tmp_path: Path) -> None:
        # An interface typed by the APPLICATION record reaches the
        # implementation composite the mapping set pairs it with, and that one's
        # own element types, so a conflicting group behind it can be re-admitted.
        ifaces = (sr_interface("IFrame", ("frame", f"{APP}/Frame", None)), interfaces()[1])
        text = the_export(ifaces=ifaces)
        text = text.replace(
            f'<TYPE-TREF DEST="{IMPL}">{APP}/Frame</TYPE-TREF>', f'<TYPE-TREF DEST="{RECORD}">{APP}/Frame</TYPE-TREF>'
        )
        model = read_arxml(str(write(tmp_path, text)))

        _, reached = selection_reach(model, {"Components"}, decide_merges(model))

        assert {f"{APP}/Frame", f"{TYPES}/Frame", f"{TYPES}/u16", f"{TYPES}/u8"} <= reached

    def test_a_kept_record_keeps_its_map_and_the_composite_it_names(self, tmp_path: Path) -> None:
        ifaces = (sr_interface("IFrame", ("frame", f"{APP}/Frame", None)), interfaces()[1])
        text = the_export(ifaces=ifaces).replace(
            f'<TYPE-TREF DEST="{IMPL}">{APP}/Frame</TYPE-TREF>', f'<TYPE-TREF DEST="{RECORD}">{APP}/Frame</TYPE-TREF>'
        )
        model_root, _ = _import(tmp_path, text, packages=["Components"])

        assert ("Frame", "Frame") in _maps(model_root)
        assert {("application_types", "Frame"), ("implementation_types", "Frame")} <= set(_types(model_root))
        assert {"u16", "u8"} <= set(_impl(model_root))
