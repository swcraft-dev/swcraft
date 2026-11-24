"""The merge verdict over ``swcraft import model``: two declarations of one name are one element, or none.

A group is keyed ``(layer, kind, SHORT-NAME)``. Most cases run on the shape corpus (``fixtures/arxml_import_shapes/``)
through ``read_arxml``; kinds the corpus carries no duplicate of are built as small inline ``ImportModel``s.

What this module does **not** cover:

* the rendered report beyond the merge and conflict lines; the summary counts, the ``--dry-run`` identity and the
  path-free rule are ``test_arxml_import_cli.py``'s;
* ``--packages`` re-admitting the one surviving member of a conflicting group;
* the unresolved-reference policy's own behaviour (``test_arxml_import_unresolved.py``); this module asserts only
  whether a reference compared as content is one the policy writes into the model;
* a real tool export.
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest
from arxml_import_shapes_support import raw_element, read_shape_model, shape_path
from conftest import model_element_path
from swcraft_tools.arxml_import import (
    map_compositions,
    map_data_types,
    map_interfaces,
    map_swcs,
    type_mappers,
)
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED, GroupKey
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.report import _merged_lines, selection_reach
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import (
    AppPrimitiveDataTypeInfo,
    AssemblyConnectorInfo,
    ComponentPrototypeInfo,
    CompositionInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConnectorPortRef,
    ConstantScalar,
    ConstantSpecInfo,
    CSArgumentInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    DelegationConnectorInfo,
    DisabledModeInfo,
    ImplDataTypeInfo,
    ImportModel,
    SkipCategory,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwBaseTypeInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    ValueForm,
    read_arxml,
)
from swcraft_tools.cli import main

APPLICATION_DEST = "APPLICATION-SW-COMPONENT-TYPE"


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


def _group(decisions, key: str):
    """The one group whose ``str(key)`` is *key*."""
    matches = [group for group in decisions.groups if str(group.key) == key]
    assert len(matches) == 1, f"expected exactly one group {key!r}, found {[str(g.key) for g in decisions.groups]}"
    return matches[0]


def _skip_reasons(skip_log, path: str) -> list[str]:
    return [entry.reason for entry in skip_log if entry.path == path]


def _map_types(model):
    """``map_data_types`` under the verdict this model's own declarations take."""
    decisions = decide_merges(model)
    data_types, registry, skips = map_data_types(model, merges=decisions, elements=[])
    return decisions, data_types, registry, skips


def _map_through_interfaces(model):
    decisions = decide_merges(model)
    forms: list = []
    data_types, registry, type_skips = map_data_types(model, merges=decisions, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=decisions, elements=forms)
    return decisions, data_types, registry, type_skips, interfaces, iface_skips


def _map_all(model):
    """The whole mapping chain in ``run_import``'s order, under the verdict and the unresolved-reference policy.

    Both are threaded because ``run_import`` runs only that combination: without the policy a dangling reference drops
    its port, so a wrong merge of two such references would look harmless.
    """
    decisions = decide_merges(model)
    unresolved = UnresolvedReferences(model, decisions)
    forms: list = []
    data_types, registry, type_skips = map_data_types(model, merges=decisions, unresolved=unresolved, elements=forms)
    interfaces, iface_skips = map_interfaces(
        model, registry, data_types, merges=decisions, unresolved=unresolved, elements=forms
    )
    swcs, swc_skips = map_swcs(
        model,
        interfaces,
        iface_skip_log=iface_skips,
        merges=decisions,
        unresolved=unresolved,
        port_inits=registry.port_inits,
        variable_types={},
    )
    mapped, composition_skips = map_compositions(
        model,
        swcs,
        interfaces,
        iface_skips,
        registry,
        merges=decisions,
        unresolved=unresolved,
        swc_skip_log=swc_skips,
    )
    return {
        "decisions": decisions,
        "unresolved": unresolved,
        "data_types": data_types,
        "registry": registry,
        "type_skips": type_skips,
        "interfaces": interfaces,
        "iface_skips": iface_skips,
        "swcs": swcs,
        "swc_skips": swc_skips,
        "mapped": mapped,
        "composition_skips": composition_skips,
    }


def _reach(model, selection: set[str]):
    """``report.selection_reach`` under the un-narrowed verdict, as ``run_import`` calls it."""
    return selection_reach(model, selection, decide_merges(model))


def _base(path: str, name: str, *, size: int = 8, description: str = "") -> SwBaseTypeInfo:
    return SwBaseTypeInfo(
        path=path,
        name=name,
        size=size,
        encoding="NONE",
        native_declaration="uint8",
        description=description,
    )


def _sr(path: str, name: str, *, element: str = "value", type_ref: str) -> SRInterfaceInfo:
    return SRInterfaceInfo(
        path=path,
        name=name,
        description="",
        data_elements=[SRDataElementInfo(name=element, type_ref=type_ref)],
    )


def _port(path: str, name: str, kind: str, interface_ref: str) -> SwcPortInfo:
    return SwcPortInfo(path=path, name=name, kind=kind, interface_ref=interface_ref, description="")


def _swc(path: str, name: str, ports: list[SwcPortInfo]) -> SwcInfo:
    return SwcInfo(path=path, name=name, description="", ports=ports, runnables=[], events=[])


def _root_over(model: ImportModel, *paths: str) -> None:
    """A root composition placing *paths*, appended to *model*.

    ``derive_root`` refuses two uninstantiated compositions, so a conflicting composition pair needs a root placing
    both.
    """
    model.compositions.append(
        CompositionInfo(
            path="/Acme/Root",
            name="Root",
            description="",
            ports=[],
            prototypes=[
                ComponentPrototypeInfo(
                    path=f"/Acme/Root/part{index}",
                    name=f"part{index}",
                    type_ref=path,
                    dest="COMPOSITION-SW-COMPONENT-TYPE",
                )
                for index, path in enumerate(paths)
            ],
            assembly_connectors=[],
            delegation_connectors=[],
        )
    )


class TestIdenticalDuplicatesAreOneElement:
    """Two declarations nothing SWCraft reads distinguishes are one element, every member path mapped onto it.

    Does not cover what the report prints, or a group of three members (every corpus group has two).
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def mapped():
        return _map_through_interfaces(read_shape_model("identical_duplicates.arxml"))

    def test_the_group_is_merged_and_names_both_declarations(self, mapped) -> None:
        decisions = mapped[0]
        group = _group(decisions, "base_types/SW-BASE-TYPE/Flag")

        assert group.verdict == MERGED
        assert group.paths == ("/Acme/Left/Types/Flag", "/Acme/Right/Types/Flag")
        assert group.difference is None
        assert group.inherited_from is None
        assert group.cyclic is False

    def test_one_element_is_written_under_the_declared_name(self, mapped) -> None:
        _decisions, data_types, _registry, _skips, _interfaces, _iface_skips = mapped

        assert [dt.name for dt in data_types if dt.name.startswith("Flag")] == ["Flag"]

    def test_both_member_paths_resolve_to_that_one_name(self, mapped) -> None:
        _decisions, _data_types, registry, _skips, _interfaces, _iface_skips = mapped

        assert registry.resolve("/Acme/Left/Types/Flag") == "Flag"
        assert registry.resolve("/Acme/Right/Types/Flag") == "Flag"

    def test_the_second_declaration_is_not_a_skip_and_not_a_rename(self, mapped) -> None:
        # A merge is neither lossiness of the skip kind nor a rename: nothing is
        # lost and nothing is called something else. It has a category of its
        # own, which is what the verdict record is.
        _decisions, _data_types, registry, skips, _interfaces, _iface_skips = mapped

        assert _skip_reasons(skips, "/Acme/Right/Types/Flag") == []
        assert registry.renames == []

    def test_the_interface_group_is_one_interface_too(self, mapped) -> None:
        _decisions, _data_types, _registry, _skips, interfaces, iface_skips = mapped

        assert [iface.name for iface in interfaces] == ["ILevel"]
        assert _skip_reasons(iface_skips, "/Acme/Right/Interfaces/ILevel") == []


class TestADescriptionDifferingAloneIsAMerge:
    """``DESC`` is excluded from equality: the merge keeps the first member's text and records the other.

    A conflict drops both elements and dangles every reference, so a documentation difference must not cause one.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def mapped():
        return _map_through_interfaces(read_shape_model("identical_duplicates.arxml"))

    def test_the_group_merges_although_the_two_descriptions_differ(self, mapped) -> None:
        group = _group(mapped[0], "interfaces/SENDER-RECEIVER-INTERFACE/ILevel")

        assert group.verdict == MERGED

    def test_the_first_in_source_path_order_keeps_its_text(self, mapped) -> None:
        interfaces = mapped[4]
        group = _group(mapped[0], "interfaces/SENDER-RECEIVER-INTERFACE/ILevel")

        assert group.primary == "/Acme/Left/Interfaces/ILevel"
        assert "left-hand" in interfaces[0].description

    def test_the_record_names_the_path_whose_description_was_not_carried(self, mapped) -> None:
        group = _group(mapped[0], "interfaces/SENDER-RECEIVER-INTERFACE/ILevel")

        assert [path for path, _text in group.dropped_descriptions] == ["/Acme/Right/Interfaces/ILevel"]
        assert "right-hand" in group.dropped_descriptions[0][1]

    def test_a_description_declared_by_neither_drops_nothing(self, mapped) -> None:
        # The guard against a record that reports a loss on every merge: `Flag`
        # carries no description at all, and its record has to say so by
        # carrying nothing.
        assert _group(mapped[0], "base_types/SW-BASE-TYPE/Flag").dropped_descriptions == ()


class TestWhatAMergeDropsIsNamedExactly:
    """A text the merged element does not keep is a line, compared exactly as read.

    A composition's ``LONG-NAME`` is treated as ``DESC`` is.
    """

    @staticmethod
    def _composition(package: str, *, description: str = "", long_name: str | None = None) -> CompositionInfo:
        return CompositionInfo(
            path=f"{package}/Top",
            name="Top",
            description=description,
            ports=[],
            prototypes=[],
            assembly_connectors=[],
            delegation_connectors=[],
            long_name=long_name,
        )

    def _merged(self, first: CompositionInfo, second: CompositionInfo):
        model = ImportModel(source_path="inline-test")
        model.compositions.extend([first, second])
        (group,) = decide_merges(model).groups
        assert group.verdict == MERGED
        return group

    def test_a_later_members_differing_long_name_is_named_with_its_path(self) -> None:
        group = self._merged(
            self._composition("/A", long_name="First title"), self._composition("/B", long_name="Second title")
        )

        assert group.dropped_long_names == (("/B/Top", "Second title"),)
        assert _merged_lines(group)[1:] == ["      title not carried, from /B/Top: Second title"]

    def test_a_long_name_where_the_primary_has_none_is_named(self) -> None:
        group = self._merged(self._composition("/A"), self._composition("/B", long_name="Only title"))

        assert group.dropped_long_names == (("/B/Top", "Only title"),)

    @pytest.mark.parametrize("long_name", [None, "Same title"])
    def test_an_equal_or_absent_long_name_drops_nothing(self, long_name: str | None) -> None:
        group = self._merged(
            self._composition("/A", long_name="Same title"), self._composition("/B", long_name=long_name)
        )

        assert group.dropped_long_names == ()
        assert len(_merged_lines(group)) == 1

    def test_a_description_differing_in_whitespace_alone_is_named(self) -> None:
        group = self._merged(self._composition("/A", description="Text"), self._composition("/B", description="Text "))

        assert group.dropped_descriptions == (("/B/Top", "Text "),)

    def test_an_equal_description_drops_nothing(self) -> None:
        group = self._merged(self._composition("/A", description="Text"), self._composition("/B", description="Text"))

        assert group.dropped_descriptions == ()


class TestAValueConflictImportsNothing:
    """A compu method conflicting on its scales is inherited by the enum naming it: neither group is imported."""

    @pytest.fixture(scope="class")
    @staticmethod
    def mapped():
        return _map_types(read_shape_model("value_conflict_enum.arxml"))

    def test_the_group_conflicts_on_its_own_property(self, mapped) -> None:
        method = _group(mapped[0], "compu_methods/COMPU-METHOD/DriveModeEnum_CompuMethod")
        group = _group(mapped[0], "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/DriveModeEnum")

        assert (method.verdict, method.difference) == (CONFLICTING, "COMPU-SCALES")
        assert group.verdict == CONFLICTING
        assert group.difference is None
        assert group.inherited_from == method.key
        assert group.paths == ("/Acme/Left/AppTypes/DriveModeEnum", "/Acme/Right/AppTypes/DriveModeEnum")

    def test_neither_declaration_is_imported(self, mapped) -> None:
        _decisions, data_types, _registry, _skips = mapped

        assert [dt.name for dt in data_types if dt.name.startswith("DriveMode")] == []

    def test_no_member_path_resolves(self, mapped) -> None:
        _decisions, _data_types, registry, _skips = mapped

        assert registry.resolve("/Acme/Left/AppTypes/DriveModeEnum") is None
        assert registry.resolve("/Acme/Right/AppTypes/DriveModeEnum") is None

    def test_no_winner_is_picked_and_nothing_is_renamed(self, mapped) -> None:
        _decisions, _data_types, registry, _skips = mapped

        assert registry.renames == []
        assert registry.reductions == []


class TestAnOldAndANewSwcraftEnumDeclarationConflict:
    """An enum written as ``TEXTTABLE`` (SWCraft up to 0.13) and as ``VALUE`` conflicts; both ``VALUE`` merge.

    A stated limit, not a defect: the enum row compares the type's own ``CATEGORY``.
    """

    @staticmethod
    def _two_generations(old_category: str) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Common/CompuMethods/Gear_CompuMethod",
                name="Gear_CompuMethod",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=0, literal="PARK"), CompuScaleInfo(value=1, literal="DRIVE")],
                description="",
            )
        )
        for tree, category in (("Old", old_category), ("New", "VALUE")):
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/{tree}/DataTypes/Gear",
                    name="Gear",
                    compu_method_ref="/Common/CompuMethods/Gear_CompuMethod",
                    description="",
                    category=category,
                )
            )
        return model

    def test_a_texttable_and_a_value_declaration_conflict_on_the_category(self) -> None:
        decisions, data_types, registry, _skips = _map_types(self._two_generations("TEXTTABLE"))

        group = _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Gear")
        assert group.verdict == CONFLICTING
        assert group.difference == "CATEGORY"
        assert group.inherited_from is None
        assert group.paths == ("/New/DataTypes/Gear", "/Old/DataTypes/Gear")
        assert [dt.name for dt in data_types if dt.name == "Gear"] == []
        assert registry.resolve("/Old/DataTypes/Gear") is None
        assert registry.resolve("/New/DataTypes/Gear") is None

    def test_two_value_declarations_are_one_enum(self) -> None:
        decisions, data_types, _registry, _skips = _map_types(self._two_generations("VALUE"))

        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Gear").verdict == MERGED
        assert [dt.kind for dt in data_types if dt.name == "Gear"] == ["enum"]

    @staticmethod
    def _two_generations_document(old_category: str) -> str:
        """The same pair as ARXML text: ``/Old`` and ``/New`` each declare ``Gear`` over one method."""
        types = "".join(
            f"""
    <AR-PACKAGE>
      <SHORT-NAME>{tree}</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>DataTypes</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Gear</SHORT-NAME>
              <CATEGORY>{category}</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <COMPU-METHOD-REF DEST="COMPU-METHOD">/Common/CompuMethods/Gear_CompuMethod</COMPU-METHOD-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>"""
            for tree, category in (("Old", old_category), ("New", "VALUE"))
        )
        scales = "".join(
            f"""
                  <COMPU-SCALE>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">{value}</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">{value}</UPPER-LIMIT>
                    <COMPU-CONST><VT>{literal}</VT></COMPU-CONST>
                  </COMPU-SCALE>"""
            for value, literal in ((0, "PARK"), (1, "DRIVE"))
        )
        return f"""<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Common</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>CompuMethods</SHORT-NAME>
          <ELEMENTS>
            <COMPU-METHOD>
              <SHORT-NAME>Gear_CompuMethod</SHORT-NAME>
              <CATEGORY>TEXTTABLE</CATEGORY>
              <COMPU-INTERNAL-TO-PHYS>
                <COMPU-SCALES>{scales}
                </COMPU-SCALES>
              </COMPU-INTERNAL-TO-PHYS>
            </COMPU-METHOD>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>{types}
  </AR-PACKAGES>
</AUTOSAR>
"""

    @pytest.mark.parametrize(("old_category", "conflicts"), [("TEXTTABLE", True), ("VALUE", False)])
    def test_import_model_lists_the_pair_as_conflicting_and_writes_neither(
        self, tmp_path: Path, old_category: str, conflicts: bool
    ) -> None:
        """Through ``import model``: a conflict is reported and no file written; a ``VALUE`` pair merges."""
        from swcraft_tools.arxml_import import run_import

        source = tmp_path / "two_generations.arxml"
        source.write_text(self._two_generations_document(old_category), encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        rendered = report.render()
        line = (
            "  - application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Gear declared at /New/DataTypes/Gear, "
            "/Old/DataTypes/Gear -- not imported: the declarations differ in CATEGORY"
        )
        assert (line in rendered.split("\n")) is conflicts
        assert ("Conflicting declarations" in rendered) is conflicts
        assert (model_element_path(project / "model", "Gear", kind="enum")).exists() is not conflicts


class TestATransitiveConflictNamesTheGroupItInherits:
    """A group conflicting only through the group it references names that group in its line."""

    @pytest.fixture(scope="class")
    @staticmethod
    def mapped():
        return _map_types(read_shape_model("transitive_conflict.arxml"))

    def test_the_referenced_group_conflicts_on_its_own_property(self, mapped) -> None:
        group = _group(mapped[0], "base_types/SW-BASE-TYPE/Word16")

        assert group.verdict == CONFLICTING
        assert group.difference == "BASE-TYPE-SIZE"

    def test_the_referring_group_conflicts_and_names_the_group_it_inherits_from(self, mapped) -> None:
        group = _group(mapped[0], "implementation_types/IMPLEMENTATION-DATA-TYPE/Distance")

        assert group.verdict == CONFLICTING
        assert group.difference is None
        assert group.inherited_from == GroupKey("base_types", "SW-BASE-TYPE", "Word16")

    def test_neither_group_imports_anything(self, mapped) -> None:
        _decisions, data_types, _registry, _skips = mapped

        assert [dt.name for dt in data_types] == []


class TestASingletonOverAConflictedBaseTypeIsKeptUnresolved:
    """A value type declared once over a conflicting base type is kept unresolved, not skipped.

    Guards a defect where only an absent target reached the unresolved-reference policy and the value type was skipped.
    """

    @staticmethod
    def _model(*, right_size: int) -> ImportModel:
        """Two ``Word16`` declarations and a ``Speed`` value type over the first.

        *right_size* decides whether the pair conflicts or merges.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Left/Types/Word16", "Word16", size=16))
        model.sw_base_types.append(_base("/Acme/Right/Types/Word16", "Word16", size=right_size))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Only/Impl/Speed",
                name="Speed",
                category="VALUE",
                base_type_ref="/Acme/Left/Types/Word16",
                impl_type_ref=None,
                description="",
            )
        )
        for declaration in (*model.sw_base_types, *model.impl_data_types):
            model.path_index[declaration.path] = raw_element(declaration.path)
        return model

    def test_the_value_type_is_kept_and_the_conflict_is_recorded(self) -> None:
        from swcraft_tools.arxml_import.unresolved import CAUSE_CONFLICT

        model = self._model(right_size=32)
        decisions = decide_merges(model)
        assert _group(decisions, "base_types/SW-BASE-TYPE/Word16").verdict == CONFLICTING

        unresolved = UnresolvedReferences(model, decisions)
        data_types, _registry, skips = map_data_types(model, merges=decisions, unresolved=unresolved, elements=[])

        # The value type survives, carrying the path exactly as the export
        # spelled it -- `check-model` reports it under the rule the role has.
        assert [(dt.name, dt.kind, dt.base) for dt in data_types] == [
            ("Speed", "value_type", "/Acme/Left/Types/Word16")
        ]
        assert _skip_reasons(skips, "/Acme/Only/Impl/Speed") == []
        record = next(record for record in unresolved.references if record.target == "/Acme/Left/Types/Word16")
        assert record.cause == CAUSE_CONFLICT
        assert record.conflict_group == "base_types/SW-BASE-TYPE/Word16"
        assert [referrer.field for referrer in record.referrers] == ["implementation_types.value_type.base"]

    def test_a_selection_excluded_base_type_reaches_the_role_too(self) -> None:
        """A declared target excluded by the selection reaches the unresolved-reference policy the same way."""
        from swcraft_tools.arxml_import.merge import narrow_to_selection
        from swcraft_tools.arxml_import.unresolved import CAUSE_SELECTION

        model = self._model(right_size=32)
        decisions = narrow_to_selection(
            decide_merges(model),
            selected=frozenset({"/Acme/Right/Types/Word16"}),
            reached=frozenset(),
        )
        group = _group(decisions, "base_types/SW-BASE-TYPE/Word16")
        assert (group.verdict, group.primary) == ("selected", "/Acme/Right/Types/Word16")

        unresolved = UnresolvedReferences(model, decisions)
        data_types, _registry, _skips = map_data_types(model, merges=decisions, unresolved=unresolved, elements=[])

        assert ("Speed", "/Acme/Left/Types/Word16") in [(dt.name, getattr(dt, "base", None)) for dt in data_types]
        assert [(record.target, record.cause) for record in unresolved.references] == [
            ("/Acme/Left/Types/Word16", CAUSE_SELECTION)
        ]

    def test_a_target_skipped_for_its_own_defect_still_keeps_todays_chain_skip(self) -> None:
        """A declared target refused for its own defect is no unresolved cause: the value type is still skipped."""
        model = ImportModel(source_path="inline")
        # `size=None` fails the base type's own mapping, so it is declared,
        # skipped, and in no group at all.
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path="/Acme/Only/Types/Word16",
                name="Word16",
                size=None,
                encoding="NONE",
                native_declaration="uint16",
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Only/Impl/Speed",
                name="Speed",
                category="VALUE",
                base_type_ref="/Acme/Only/Types/Word16",
                impl_type_ref=None,
                description="",
            )
        )
        for declaration in (*model.sw_base_types, *model.impl_data_types):
            model.path_index[declaration.path] = raw_element(declaration.path)

        unresolved = UnresolvedReferences(model, decide_merges(model))
        data_types, _registry, skips = map_data_types(
            model, merges=decide_merges(model), unresolved=unresolved, elements=[]
        )

        assert [dt.name for dt in data_types] == []
        assert any("base type was skipped" in reason for reason in _skip_reasons(skips, "/Acme/Only/Impl/Speed"))
        assert unresolved.references == ()


class TestTheFixpointIsNotADeclarationLocalPass:
    """Three stacked duplicate groups, only the bottom byte-identical, all merge at the fixpoint.

    A declaration-local comparison would answer one merge and two conflicts: the reference paths differ, the elements do
    not.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def mapped():
        return _map_through_interfaces(read_shape_model("three_level_fixpoint.arxml"))

    def test_all_three_groups_merge(self, mapped) -> None:
        decisions = mapped[0]

        assert {str(group.key): group.verdict for group in decisions.groups} == {
            "base_types/SW-BASE-TYPE/Byte8": MERGED,
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Level": MERGED,
            "interfaces/SENDER-RECEIVER-INTERFACE/ILevel": MERGED,
        }

    def test_only_the_bottom_group_is_identical_declaration_locally(self, mapped) -> None:
        # The upper groups differ only in the text of a reference.
        model = read_shape_model("three_level_fixpoint.arxml")
        left, right = sorted(model.impl_data_types, key=lambda info: info.path)

        assert left.base_type_ref != right.base_type_ref
        assert _group(mapped[0], "implementation_types/IMPLEMENTATION-DATA-TYPE/Level").verdict == MERGED

    def test_one_element_per_layer_comes_out(self, mapped) -> None:
        _decisions, data_types, _registry, _skips, interfaces, _iface_skips = mapped

        assert sorted(dt.name for dt in data_types) == ["Byte8", "Level"]
        assert [iface.name for iface in interfaces] == ["ILevel"]

    def test_the_value_type_rests_on_the_merged_base_type(self, mapped) -> None:
        _decisions, data_types, registry, _skips, _interfaces, _iface_skips = mapped
        level = next(dt for dt in data_types if dt.name == "Level")

        assert level.base == "Byte8"
        assert registry.resolve("/Acme/Right/Types/Byte8") == "Byte8"


class TestAGroupInAReferenceCycleIsConflicting:
    """Two groups referencing each other in a cycle are both conflicting, recorded ``cyclic``.

    Fail-closed: the standard defines no equality of definition. The type graph cannot reach this (the reader refuses a
    cyclic ``TYPE_REFERENCE`` chain); a malformed composition graph can.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def decisions():
        return decide_merges(read_shape_model("cyclic_composition_graph.arxml"))

    def test_both_groups_in_the_cycle_conflict(self, decisions) -> None:
        assert {str(group.key): group.verdict for group in decisions.groups} == {
            "compositions/COMPOSITION-SW-COMPONENT-TYPE/Lower": CONFLICTING,
            "compositions/COMPOSITION-SW-COMPONENT-TYPE/Upper": CONFLICTING,
        }

    def test_the_record_says_cyclic_rather_than_naming_a_property(self, decisions) -> None:
        for group in decisions.groups:
            assert group.cyclic is True
            assert group.difference is None
            assert group.inherited_from is None

    def test_a_composition_that_places_its_own_type_is_a_cycle_of_length_one(self) -> None:
        # The self-edge, which the group graph keeps deliberately: dropping it
        # would decide the group merged on the strength of its own verdict.
        model = ImportModel(source_path="inline")
        for tree in ("Left", "Right"):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/{tree}/Comps/Holder",
                    name="Holder",
                    description="",
                    ports=[],
                    prototypes=[
                        ComponentPrototypeInfo(
                            path=f"/Acme/{tree}/Comps/Holder/self",
                            name="self",
                            type_ref=f"/Acme/{tree}/Comps/Holder",
                            dest="COMPOSITION-SW-COMPONENT-TYPE",
                        )
                    ],
                    assembly_connectors=[],
                    delegation_connectors=[],
                )
            )

        decisions = decide_merges(model)

        group = _group(decisions, "compositions/COMPOSITION-SW-COMPONENT-TYPE/Holder")
        assert (group.verdict, group.cyclic) == (CONFLICTING, True)


class TestTheContestedNameStaysClaimed:
    """A conflicting group imports nothing but still claims its name against a cross-kind declaration.

    Otherwise repairing the conflict at the source would silently rename a different element.
    """

    @staticmethod
    def _model(*, with_the_conflicting_group: bool) -> ImportModel:
        from swcraft_tools.arxml_reader import (
            AppArrayDataTypeInfo,
            AppRecordDataTypeInfo,
            RecordElementInfo,
        )

        model = ImportModel(source_path="inline")
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Acme/Common/AppTypes/Flag", name="Flag", compu_method_ref=None, description=""
            )
        )
        if with_the_conflicting_group:
            for tree, element in (("Left", "a"), ("Right", "b")):
                model.app_record_data_types.append(
                    AppRecordDataTypeInfo(
                        path=f"/Acme/{tree}/Records/Speed",
                        name="Speed",
                        elements=[RecordElementInfo(name=element, type_ref="/Acme/Common/AppTypes/Flag")],
                        description="",
                    )
                )
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Acme/Common/Arrays/Speed",
                name="Speed",
                element_type_ref="/Acme/Common/AppTypes/Flag",
                max_number_of_elements=4,
                description="",
            )
        )
        return model

    def test_the_group_is_dropped(self) -> None:
        decisions, data_types, _registry, _skips = _map_types(self._model(with_the_conflicting_group=True))

        assert _group(decisions, "application_types/APPLICATION-RECORD-DATA-TYPE/Speed").verdict == CONFLICTING
        assert "Speed" not in {dt.name for dt in data_types}

    def test_the_cross_kind_declaration_is_renamed_identically_in_both_worlds(self) -> None:
        with_group = _map_types(self._model(with_the_conflicting_group=True))
        without_group = _map_types(self._model(with_the_conflicting_group=False))

        assert with_group[2].resolve("/Acme/Common/Arrays/Speed") == "Speed_app"
        assert without_group[2].resolve("/Acme/Common/Arrays/Speed") == "Speed"

    def test_the_claim_is_what_does_it_and_not_a_rename_line(self) -> None:
        # A group that imported nothing may not appear in the rename category.
        decisions, _data_types, registry, _skips = _map_types(self._model(with_the_conflicting_group=True))

        assert "Speed" in registry.claimed_names("application_types")
        assert [line for line in registry.renames if "Arrays/Speed" in line]
        assert not [line for line in registry.renames if "Left/Records/Speed" in line]
        assert decisions.conflicting[0].paths == ("/Acme/Left/Records/Speed", "/Acme/Right/Records/Speed")


class TestEveryLayerLevelKindTakesTheVerdict:
    """The verdict reaches every layer-level kind the importer maps, and no *first wins* skip line remains."""

    @staticmethod
    def _interface_pair(*, differing: bool) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Left/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        model.sr_interfaces.append(
            _sr(
                "/Acme/Right/If/ILevel",
                "ILevel",
                element="other" if differing else "value",
                type_ref="/Acme/Common/Impl/Level",
            )
        )
        return model

    @staticmethod
    def _client_server_pair(*, differing: bool) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        for tree, direction in (("Left", "IN"), ("Right", "OUT" if differing else "IN")):
            model.cs_interfaces.append(
                CSInterfaceInfo(
                    path=f"/Acme/{tree}/If/ICalc",
                    name="ICalc",
                    description="",
                    operations=[
                        CSOperationInfo(
                            name="Compute",
                            arguments=[
                                CSArgumentInfo(
                                    name="value",
                                    type_ref="/Acme/Common/Impl/Level",
                                    direction=direction,
                                )
                            ],
                        )
                    ],
                )
            )
        return model

    @staticmethod
    def _component_pair(*, differing: bool) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        for tree, kind in (
            ("Left", "P-PORT-PROTOTYPE"),
            ("Right", "R-PORT-PROTOTYPE" if differing else "P-PORT-PROTOTYPE"),
        ):
            model.swcs.append(
                _swc(
                    f"/Acme/{tree}/Components/Sensor",
                    "Sensor",
                    [_port(f"/Acme/{tree}/Components/Sensor/pLevel", "pLevel", kind, "/Acme/Common/If/ILevel")],
                )
            )
        return model

    @staticmethod
    def _composition_pair(*, differing: bool) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Sensor",
                "Sensor",
                [
                    _port(
                        "/Acme/Common/Components/Sensor/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel"
                    )
                ],
            )
        )
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Other",
                "Other",
                [_port("/Acme/Common/Components/Other/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel")],
            )
        )
        for tree, member_type in (("Left", "Sensor"), ("Right", "Other" if differing else "Sensor")):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/{tree}/Comps/Cluster",
                    name="Cluster",
                    description="",
                    ports=[],
                    prototypes=[
                        ComponentPrototypeInfo(
                            path=f"/Acme/{tree}/Comps/Cluster/part",
                            name="part",
                            type_ref=f"/Acme/Common/Components/{member_type}",
                            dest=APPLICATION_DEST,
                        )
                    ],
                    assembly_connectors=[],
                    delegation_connectors=[],
                )
            )
        _root_over(model, "/Acme/Left/Comps/Cluster", "/Acme/Right/Comps/Cluster")
        return model

    @staticmethod
    def _connector_pair(*, differing: bool) -> ImportModel:
        """Two same-name assembly connectors, one per copy of a duplicated composition.

        *differing* wires another requester in the second copy, so the connector and composition groups conflict.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Sensor",
                "Sensor",
                [
                    _port(
                        "/Acme/Common/Components/Sensor/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel"
                    )
                ],
            )
        )
        for consumer in ("Brain", "Spare"):
            model.swcs.append(
                _swc(
                    f"/Acme/Common/Components/{consumer}",
                    consumer,
                    [
                        _port(
                            f"/Acme/Common/Components/{consumer}/rLevel",
                            "rLevel",
                            "R-PORT-PROTOTYPE",
                            "/Acme/Common/If/ILevel",
                        )
                    ],
                )
            )
        for tree, consumer in (("Left", "Brain"), ("Right", "Spare" if differing else "Brain")):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/{tree}/Comps/Cluster",
                    name="Cluster",
                    description="",
                    ports=[],
                    prototypes=[
                        ComponentPrototypeInfo(
                            path=f"/Acme/{tree}/Comps/Cluster/sensor",
                            name="sensor",
                            type_ref="/Acme/Common/Components/Sensor",
                            dest=APPLICATION_DEST,
                        ),
                        ComponentPrototypeInfo(
                            path=f"/Acme/{tree}/Comps/Cluster/consumer",
                            name="consumer",
                            type_ref=f"/Acme/Common/Components/{consumer}",
                            dest=APPLICATION_DEST,
                        ),
                    ],
                    assembly_connectors=[
                        AssemblyConnectorInfo(
                            path=f"/Acme/{tree}/Comps/Cluster/Wire",
                            name="Wire",
                            provider=ConnectorPortRef(
                                component_ref=f"/Acme/{tree}/Comps/Cluster/sensor",
                                port_ref="/Acme/Common/Components/Sensor/pLevel",
                            ),
                            requester=ConnectorPortRef(
                                component_ref=f"/Acme/{tree}/Comps/Cluster/consumer",
                                port_ref=f"/Acme/Common/Components/{consumer}/rLevel",
                            ),
                        )
                    ],
                    delegation_connectors=[],
                )
            )
        _root_over(model, "/Acme/Left/Comps/Cluster", "/Acme/Right/Comps/Cluster")
        return model

    @pytest.mark.parametrize(
        ("field", "left", "right", "expected"),
        [
            ("byte_order", "MOST-SIGNIFICANT-BYTE-FIRST", "MOST-SIGNIFICANT-BYTE-LAST", "BYTE-ORDER"),
            ("mem_alignment", "8", "16", "MEM-ALIGNMENT"),
            ("category", "FIXED_LENGTH", "VARIABLE_LENGTH", "CATEGORY"),
        ],
    )
    def test_a_base_type_pairs_three_comparison_only_properties_are_consulted(
        self, field: str, left: str, right: str, expected: str
    ) -> None:
        """A base type's three comparison-only properties decide the verdict.

        Guards a wrong merge of base types differing only in byte order or alignment.
        """
        model = ImportModel(source_path="inline")
        for tree, value in (("Left", left), ("Right", right)):
            base = _base(f"/Acme/{tree}/Types/Word", "Word")
            setattr(base, field, value)
            model.sw_base_types.append(base)

        group = _group(decide_merges(model), "base_types/SW-BASE-TYPE/Word")

        assert (group.verdict, group.difference) == (CONFLICTING, expected)

    def test_an_implementation_pair_of_two_categories_is_one_group_whose_category_conflicts(self) -> None:
        """``value_type`` and ``reference_type`` share one ARXML tag: one group whose ``CATEGORY`` conflicts."""
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Left/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Right/Impl/Level",
                name="Level",
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref="/Acme/Left/Impl/Level",
                description="",
            )
        )

        decisions = decide_merges(model)
        group = _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Level")

        assert (group.verdict, group.difference) == (CONFLICTING, "CATEGORY")
        assert group.paths == ("/Acme/Left/Impl/Level", "/Acme/Right/Impl/Level")

    def test_a_non_twin_mapping_entry_now_tells_the_SETS_apart_and_not_the_primitives(self) -> None:
        """Two mapping sets binding one name to different types conflict; the primitives merge.

        Guards a silent retyping of one tree's interfaces onto the other tree's type.
        """
        model = ImportModel(source_path="inline")
        for tree, size in (("Left", 8), ("Right", 32)):
            model.sw_base_types.append(_base(f"/Acme/{tree}/Types/Word", f"Word{size}", size=size))
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Impl/Raw{size}",
                    name=f"Raw{size}",
                    category="VALUE",
                    base_type_ref=f"/Acme/{tree}/Types/Word",
                    impl_type_ref=None,
                    description="",
                )
            )
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Speed", name="Speed", compu_method_ref=None, description=""
                )
            )
            model.data_type_mapping_sets.append(
                DataTypeMappingSetInfo(
                    path=f"/Acme/{tree}/Maps/Set",
                    name="Set",
                    maps=[
                        DataTypeMapInfo(
                            application_ref=f"/Acme/{tree}/Types/Speed",
                            implementation_ref=f"/Acme/{tree}/Impl/Raw{size}",
                        )
                    ],
                )
            )

        decisions = decide_merges(model)

        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed").verdict == MERGED
        sets = _group(decisions, "mapping_sets/DATA-TYPE-MAPPING-SET/Set")
        assert (sets.verdict, sets.difference) == (CONFLICTING, "DATA-TYPE-MAPS")

    def test_two_application_primitives_mapped_onto_one_merged_type_still_merge(self) -> None:
        """The mapping target is compared resolved: two targets that merged are one type."""
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Left/Types/Word", "Word"))
        model.sw_base_types.append(_base("/Acme/Right/Types/Word", "Word"))
        for tree in ("Left", "Right"):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Impl/Raw",
                    name="Raw",
                    category="VALUE",
                    base_type_ref=f"/Acme/{tree}/Types/Word",
                    impl_type_ref=None,
                    description="",
                )
            )
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Speed", name="Speed", compu_method_ref=None, description=""
                )
            )
            model.data_type_mapping_sets.append(
                DataTypeMappingSetInfo(
                    path=f"/Acme/{tree}/Maps/Set",
                    name="Set",
                    maps=[
                        DataTypeMapInfo(
                            application_ref=f"/Acme/{tree}/Types/Speed",
                            implementation_ref=f"/Acme/{tree}/Impl/Raw",
                        )
                    ],
                )
            )

        decisions = decide_merges(model)

        assert _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw").verdict == MERGED
        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed").verdict == MERGED

    #: The ways a twin can be **declared** and still register nothing, one per
    #: guard ``merge._implementation_types_that_certainly_register`` models.
    #: Each is applied identically in both trees, so the twins still merge.
    TWIN_DEFECTS = ("no-base-type-ref", "unsupported-category", "reference-onto-a-base-type", "base-type-unreadable")

    @staticmethod
    def _twin_and_generic_model(
        *,
        compu_by_tree: dict[str, str | None] | None = None,
        twin_defect: str | None = None,
        generic_entry: bool = True,
    ) -> ImportModel:
        """Two copies of one cluster, each primitive mapped by a tree-local generic entry first and its twin last.

        The twins merge; the generic entries name two different implementation types. *twin_defect* breaks the twin in
        both trees so it registers nothing; ``generic_entry=False`` leaves the twin as the only entry.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        if twin_defect == "base-type-unreadable":
            # Skipped for a missing BASE-TYPE-SIZE, so the VALUE type over it is
            # skipped in turn -- the only guard that is a property of another
            # declaration.
            model.sw_base_types.append(
                SwBaseTypeInfo(
                    path="/Acme/Common/Types/Unreadable",
                    name="Unreadable",
                    size=None,
                    encoding="NONE",
                    native_declaration="uint8",
                    description="",
                )
            )
        for tree, size in (("Left", 8), ("Right", 32)):
            model.sw_base_types.append(_base(f"/Acme/{tree}/Types/Spare", f"Spare{size}", size=size))
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Impl/Spare{size}",
                    name=f"Spare{size}",
                    category="VALUE",
                    base_type_ref=f"/Acme/{tree}/Types/Spare",
                    impl_type_ref=None,
                    description="",
                )
            )
            twin = ImplDataTypeInfo(
                path=f"/Acme/{tree}/Impl/Speed",
                name="Speed",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
            if twin_defect == "no-base-type-ref":
                twin.base_type_ref = None
            elif twin_defect == "unsupported-category":
                twin.category = "STRUCTURE"
            elif twin_defect == "reference-onto-a-base-type":
                twin.category = "TYPE_REFERENCE"
                twin.base_type_ref = None
                twin.impl_type_ref = "/Acme/Common/Types/Word"
            elif twin_defect == "base-type-unreadable":
                twin.base_type_ref = "/Acme/Common/Types/Unreadable"
            elif twin_defect is not None:
                raise AssertionError(f"unknown twin defect {twin_defect!r}")
            model.impl_data_types.append(twin)
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Speed",
                    name="Speed",
                    compu_method_ref=(compu_by_tree or {}).get(tree),
                    description="",
                )
            )
            entries = [
                DataTypeMapInfo(
                    application_ref=f"/Acme/{tree}/Types/Speed",
                    implementation_ref=f"/Acme/{tree}/Impl/Spare{size}",
                ),
                DataTypeMapInfo(
                    application_ref=f"/Acme/{tree}/Types/Speed",
                    implementation_ref=f"/Acme/{tree}/Impl/Speed",
                ),
            ]
            if not generic_entry:
                del entries[0]
            model.data_type_mapping_sets.append(
                DataTypeMappingSetInfo(path=f"/Acme/{tree}/Maps/Set", name="Set", maps=entries)
            )
            model.sr_interfaces.append(
                _sr(f"/Acme/{tree}/If/ISpeed", "ISpeed", element="speed", type_ref=f"/Acme/{tree}/Types/Speed")
            )
        return model

    def test_an_unused_generic_entry_does_not_refuse_a_pair_the_twin_collapses(self) -> None:
        """The entry compared is the one the mapping layer acts on.

        Guards an over-conflict where two unread generic entries refused the pair.
        """
        decisions, _data_types, _registry, _type_skips, interfaces, _iface_skips = _map_through_interfaces(
            self._twin_and_generic_model()
        )

        assert _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed").verdict == MERGED
        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed").verdict == MERGED
        assert _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISpeed").verdict == MERGED
        assert [(iface.name, iface.data_elements[0].implementation_type) for iface in interfaces] == [
            ("ISpeed", "Speed")
        ]

    def test_a_twin_collapse_still_compares_the_backing_compu_method(self) -> None:
        """A deliberate over-conflict: two primitives on one twin with different compu methods conflict.

        A twin that fails to register falls back to the enum branch, where the compu method decides the element.
        """
        model = self._twin_and_generic_model(compu_by_tree={"Left": "/Acme/Left/Compu/Mode", "Right": None})
        model.compu_methods.append(
            CompuMethodInfo(
                path="/Acme/Left/Compu/Mode",
                name="Mode",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=0, literal="OFF")],
                description="",
            )
        )

        group = _group(decide_merges(model), "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed")

        assert (group.verdict, group.difference) == (CONFLICTING, "COMPU-METHOD")

    @pytest.mark.parametrize("twin_defect", TWIN_DEFECTS)
    def test_a_declared_twin_that_registers_nothing_no_longer_drops_the_pair(self, twin_defect: str) -> None:
        """A declared twin that registers nothing leaves the primitives one element; the sets conflict.

        One case per guard of ``merge._implementation_types_that_certainly_register``.
        """
        model = self._twin_and_generic_model(twin_defect=twin_defect)

        decisions, data_types, registry, _type_skips, interfaces, _iface_skips = _map_through_interfaces(model)

        assert _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed").verdict == MERGED
        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed").verdict == MERGED
        sets = _group(decisions, "mapping_sets/DATA-TYPE-MAPPING-SET/Set")
        assert (sets.verdict, sets.difference) == (CONFLICTING, "DATA-TYPE-MAPS")
        assert "Speed" in [data_type.name for data_type in data_types]
        assert registry.resolve("/Acme/Left/Types/Speed") == "Speed"
        assert registry.resolve("/Acme/Right/Types/Speed") == "Speed"
        assert [iface.name for iface in interfaces] == ["ISpeed"]

    def test_a_twin_that_registers_nothing_but_falls_back_to_itself_still_merges(self) -> None:
        """A twin that is the only mapping entry falls back to itself, so the two copies still merge."""
        model = self._twin_and_generic_model(twin_defect="no-base-type-ref", generic_entry=False)

        decisions = decide_merges(model)

        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed").verdict == MERGED
        assert _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISpeed").verdict == MERGED

    def test_a_primitive_whose_twin_group_conflicts_inherits_that_conflict(self) -> None:
        """A twin group's conflict is inherited by the primitive group, not merged over.

        ``_implementation_types_that_certainly_register`` does not read the verdict, so it answers a conflicting twin
        *certain*; that is safe only because the target is a graph edge.
        """
        model = self._twin_and_generic_model()
        model.sw_base_types.append(_base("/Acme/Right/Types/Other", "Other", size=32))
        twin = next(impl for impl in model.impl_data_types if impl.path == "/Acme/Right/Impl/Speed")
        twin.base_type_ref = "/Acme/Right/Types/Other"

        decisions = decide_merges(model)

        twins = _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed")
        assert (twins.verdict, twins.difference) == (CONFLICTING, "BASE-TYPE-REF")
        primitives = _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Speed")
        assert primitives.verdict == CONFLICTING
        assert primitives.difference is None
        assert primitives.inherited_from == twins.key

    def test_two_enums_whose_entries_differ_merge_and_their_SETS_do_not(self) -> None:
        """Two enums with equal literals merge whatever their mapping entries say; the sets conflict."""
        model = ImportModel(source_path="inline")
        for tree, size in (("Left", 8), ("Right", 32)):
            model.sw_base_types.append(_base(f"/Acme/{tree}/Types/Word", f"Word{size}", size=size))
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Impl/Raw{size}",
                    name=f"Raw{size}",
                    category="VALUE",
                    base_type_ref=f"/Acme/{tree}/Types/Word",
                    impl_type_ref=None,
                    description="",
                )
            )
            model.compu_methods.append(
                CompuMethodInfo(
                    path=f"/Acme/{tree}/Compu/Mode",
                    name="Mode",
                    category="TEXTTABLE",
                    scales=[CompuScaleInfo(value=0, literal="OFF"), CompuScaleInfo(value=1, literal="ON")],
                    description="",
                )
            )
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Mode",
                    name="Mode",
                    compu_method_ref=f"/Acme/{tree}/Compu/Mode",
                    description="",
                )
            )
            model.data_type_mapping_sets.append(
                DataTypeMappingSetInfo(
                    path=f"/Acme/{tree}/Maps/Set",
                    name="Set",
                    maps=[
                        DataTypeMapInfo(
                            application_ref=f"/Acme/{tree}/Types/Mode",
                            implementation_ref=f"/Acme/{tree}/Impl/Raw{size}",
                        )
                    ],
                )
            )

        decisions = decide_merges(model)

        assert _group(decisions, "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Mode").verdict == MERGED
        sets = _group(decisions, "mapping_sets/DATA-TYPE-MAPPING-SET/Set")
        assert (sets.verdict, sets.difference) == (CONFLICTING, "DATA-TYPE-MAPS")

    def test_the_three_primitive_data_type_tags_group(self) -> None:
        # Three of the five data-type tags; the record and array tags are the
        # next test's. A base type beside an implementation type of one name is
        # never a group at all.
        keys = set()
        for filename in (
            "identical_duplicates.arxml",
            "three_level_fixpoint.arxml",
            "value_conflict_enum.arxml",
        ):
            keys |= {group.key.kind for group in decide_merges(read_shape_model(filename)).groups}
        keys |= {
            group.key.kind
            for group in decide_merges(read_shape_model("same_name_base_and_implementation.arxml")).groups
        }

        assert "SW-BASE-TYPE" in keys
        assert "IMPLEMENTATION-DATA-TYPE" in keys
        assert "APPLICATION-PRIMITIVE-DATA-TYPE" in keys

    def test_a_record_pair_and_an_array_pair_take_the_verdict(self) -> None:
        model = read_shape_model("identical_duplicates.arxml")
        from swcraft_tools.arxml_reader import (
            AppArrayDataTypeInfo,
            AppRecordDataTypeInfo,
            RecordElementInfo,
        )

        for tree, length in (("Left", 4), ("Right", 4)):
            model.app_record_data_types.append(
                AppRecordDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Pair",
                    name="Pair",
                    elements=[RecordElementInfo(name="a", type_ref="/Acme/Common/Types/LevelValue")],
                    description="",
                )
            )
            model.app_array_data_types.append(
                AppArrayDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Buffer",
                    name="Buffer",
                    element_type_ref="/Acme/Common/Types/LevelValue",
                    max_number_of_elements=length,
                    description="",
                )
            )
        model.app_array_data_types[-1] = AppArrayDataTypeInfo(
            path="/Acme/Right/Types/Buffer",
            name="Buffer",
            element_type_ref="/Acme/Common/Types/LevelValue",
            max_number_of_elements=8,
            description="",
        )

        decisions = decide_merges(model)

        assert _group(decisions, "application_types/APPLICATION-RECORD-DATA-TYPE/Pair").verdict == MERGED
        buffer = _group(decisions, "application_types/APPLICATION-ARRAY-DATA-TYPE/Buffer")
        assert (buffer.verdict, buffer.difference) == (CONFLICTING, "MAX-NUMBER-OF-ELEMENTS")

    @pytest.mark.parametrize("differing", [False, True])
    def test_a_sender_receiver_pair_takes_the_verdict(self, differing) -> None:
        model = self._interface_pair(differing=differing)
        decisions, _data_types, registry, _skips = _map_types(model)
        interfaces, iface_skips = map_interfaces(model, registry, _data_types, merges=decisions, elements=())

        expected = CONFLICTING if differing else MERGED
        assert _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ILevel").verdict == expected
        assert [iface.name for iface in interfaces] == ([] if differing else ["ILevel"])
        # Neither a merge nor a conflict is a skip.
        assert [entry.reason for entry in iface_skips if "duplicate interface name" in entry.reason] == []

    @pytest.mark.parametrize("differing", [False, True])
    def test_a_client_server_pair_takes_the_verdict(self, differing) -> None:
        model = self._client_server_pair(differing=differing)
        decisions, _data_types, registry, _skips = _map_types(model)
        interfaces, iface_skips = map_interfaces(model, registry, _data_types, merges=decisions, elements=())

        expected = CONFLICTING if differing else MERGED
        assert _group(decisions, "interfaces/CLIENT-SERVER-INTERFACE/ICalc").verdict == expected
        assert [iface.name for iface in interfaces] == ([] if differing else ["ICalc"])
        assert [entry.reason for entry in iface_skips if "duplicate interface name" in entry.reason] == []

    @pytest.mark.parametrize("differing", [False, True])
    def test_a_component_pair_takes_the_verdict(self, differing) -> None:
        outcome = _map_all(self._component_pair(differing=differing))

        expected = CONFLICTING if differing else MERGED
        assert _group(outcome["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Sensor").verdict == expected
        assert [imported.swc.name for imported in outcome["swcs"]] == ([] if differing else ["Sensor"])
        assert [entry.reason for entry in outcome["swc_skips"] if "duplicate SWC name" in entry.reason] == []

    @pytest.mark.parametrize("differing", [False, True])
    def test_a_composition_pair_takes_the_verdict(self, differing) -> None:
        outcome = _map_all(self._composition_pair(differing=differing))

        expected = CONFLICTING if differing else MERGED
        assert _group(outcome["decisions"], "compositions/COMPOSITION-SW-COMPONENT-TYPE/Cluster").verdict == expected
        assert [c.name for c in outcome["mapped"].compositions] == (["Root"] if differing else ["Cluster", "Root"])
        assert [
            entry.reason for entry in outcome["composition_skips"] if "duplicate component-type name" in entry.reason
        ] == []
        assert [c.name for c in outcome["mapped"].compositions if c.name == "Root"] == ["Root"]

    @pytest.mark.parametrize("differing", [False, True])
    def test_a_connector_pair_takes_the_verdict(self, differing) -> None:
        outcome = _map_all(self._connector_pair(differing=differing))

        expected = CONFLICTING if differing else MERGED
        assert _group(outcome["decisions"], "connectors/ASSEMBLY-SW-CONNECTOR/Wire").verdict == expected
        assert [c.name for c in outcome["mapped"].connectors] == ([] if differing else ["Wire"])
        assert [
            entry.reason for entry in outcome["composition_skips"] if "duplicate connector name" in entry.reason
        ] == []

    def test_the_two_compositions_case_of_the_component_type_skip_is_retired(self) -> None:
        # Two compositions of one name take the verdict; only a composition colliding
        # with a component (cross-kind) takes `duplicate component-type name`.
        outcome = _map_all(self._composition_pair(differing=True))

        assert _group(outcome["decisions"], "compositions/COMPOSITION-SW-COMPONENT-TYPE/Cluster").verdict == CONFLICTING
        assert [
            entry.reason for entry in outcome["composition_skips"] if "duplicate component-type name" in entry.reason
        ] == []


class TestTheRegistrationGuardsRowsOneToThreeApply:
    """The guards ``merge._implementation_types_that_certainly_register`` rests on, read off the modules.

    Every ``skip_log.record`` under rows 1-3's tags in the ``arxml_import`` modules is classified below; a new guard
    fails by name until classified, a removed one until its row goes.

    Does not reach: a row that stops registering a type without recording a skip (the verdict, a ``--packages``
    exclusion), and whether the predicate implements this classification (``TestEveryLayerLevelKindTakesTheVerdict``).
    """

    #: ``(tag, normalized reason)`` -> how the predicate answers that guard.
    #: *refuses* = the predicate reads the same condition and answers **no**;
    #: *not certain* = it cannot read it and answers **no** for that reason,
    #: which is the fail-closed direction. A base type and an implementation
    #: type of one name are two layers and both register.
    GUARDS: dict[tuple[str, str], str] = {
        ("SW-BASE-TYPE", "missing or blank SHORT-NAME"): "refuses: a blank base-type name",
        (
            "SW-BASE-TYPE",
            "missing BASE-TYPE-SIZE, or missing/blank BASE-TYPE-ENCODING",
        ): "refuses: an absent size or a blank encoding",
        ("SW-BASE-TYPE", "BASE-TYPE-SIZE must be > 0, got {}"): "refuses: a non-positive size",
        (
            "SW-BASE-TYPE",
            "referenced base type was itself skipped",
        ): "refuses: the same four guards, asked of the base type a VALUE type names",
        ("IMPLEMENTATION-DATA-TYPE", "cyclic type reference"): "refuses: a reference cycle is never certain",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {}",
        ): "refuses: the path names no declared implementation type",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} (the file declares no implementation data type at that path)",
        ): "refuses: the path names no declared implementation type (the outside-the-file half)",
        ("IMPLEMENTATION-DATA-TYPE", "missing or blank SHORT-NAME"): "refuses: a blank implementation-type name",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "VALUE category impl type has no BASE-TYPE-REF",
        ): "refuses: a VALUE type without a BASE-TYPE-REF -- the third round's own case",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} (base type)",
        ): "not certain: the importer may keep the type with the export's own path instead",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} (the file declares no base type at that path)",
        ): "not certain: the importer may keep the type with the export's own path instead (the outside-the-file half)",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} (base type was skipped)",
        ): "refuses: the base type's own guards, asked recursively",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "TYPE_REFERENCE category impl type has no IMPLEMENTATION-DATA-TYPE-REF",
        ): "refuses: a TYPE_REFERENCE without its reference",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "TYPE_REFERENCE names the SW-BASE-TYPE {}; a reference type rests on an "
            "implementation data type, never on a base type",
        ): "refuses: the reference names a declared base type",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} (base implementation type)",
        ): "refuses: the referenced implementation type's own answer, recursively",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unsupported category '{}'",
        ): "refuses: a category outside the five mapped ones registers nothing",
        # The STRUCTURE, ARRAY and DATA_REFERENCE rows: the predicate does not
        # walk their members and answers `not certain` for all three, so each of
        # their guards is already on the fail-closed side.
        (
            "IMPLEMENTATION-DATA-TYPE",
            "<shape>",
        ): "not certain: a composite category is never answered certain (`_unsupported_shape`'s sentence)",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "{} names the SW-BASE-TYPE {}; it names an implementation data type, never a base type",
        ): "not certain: a composite category is never answered certain (a member naming a base type)",
        (
            "IMPLEMENTATION-DATA-TYPE",
            "unresolved reference {} ({})",
        ): "not certain: a composite category is never answered certain (a member that registers nothing)",
    }

    @staticmethod
    def _reason(node: ast.expr) -> str:
        """One ``skip_log.record`` reason argument as a comparable string.

        A literal as itself, an f-string with ``{}`` per expression, a call by its name, a bare local as ``<name>``; so
        a reworded guard fails and a renamed local inside an f-string does not.
        """
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.JoinedStr):
            return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            # The reader's reason factories carry the sentence as their argument
            # (`blank_short_name()` its default), so they are read through.
            if node.func.id in {"blank_short_name", "not_supported"}:
                return (
                    TestTheRegistrationGuardsRowsOneToThreeApply._reason(node.args[0])
                    if node.args
                    else "missing or blank SHORT-NAME"
                )
            return f"{node.func.id}()"
        if isinstance(node, ast.Name):
            # A sentence held in a local (`_unsupported_shape`'s answer, `shape`)
            # is read by the local's name, so renaming it fails as a reworded
            # guard does.
            return f"<{node.id}>"
        raise AssertionError(f"unrecognized skip reason expression {ast.dump(node)}")

    def test_the_guards_rows_one_to_three_apply_are_the_ones_the_predicate_reads(self) -> None:
        # Rows 1-3 are mapped across the package's modules, so every module is read.
        trees = [
            ast.parse(path.read_text(encoding="utf-8")) for path in Path(type_mappers.__file__).parent.glob("*.py")
        ]
        nodes = [node for tree in trees for node in ast.walk(tree)]
        found: set[tuple[str, str]] = set()
        # `_implementation_member` records its two refusals from its arguments,
        # so they are read at each call site instead -- the base-type sentence
        # as written there, and the unresolved-reference sentence with the
        # site's own role -- and the helper's own two `record` calls, which
        # would read as `<base_type_refusal>` and `({})`, are not counted.
        (helper,) = (
            node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == "_implementation_member"
        )
        inside_helper = {id(node) for node in ast.walk(helper)}
        for node in nodes:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == helper.name:
                role = node.args[4]
                found.add(("IMPLEMENTATION-DATA-TYPE", self._reason(node.args[5])))
                found.add(
                    (
                        "IMPLEMENTATION-DATA-TYPE",
                        f"unresolved reference {{}} ({role.value})"
                        if isinstance(role, ast.Constant)
                        else "unresolved reference {} ({})",
                    )
                )
                continue
            if id(node) in inside_helper:
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "record_chained_to":
                # A guard recorded as a chain behind the type it names (the
                # import report's chain inheritance): `record_chained_to(log,
                # declared, path, tag, reason, ...)`, read the way a `record`
                # call is, so moving a guard onto it is no escape from this test.
                tag = node.args[3]
                if isinstance(tag, ast.Constant) and tag.value in {"SW-BASE-TYPE", "IMPLEMENTATION-DATA-TYPE"}:
                    found.add((str(tag.value), self._reason(node.args[4])))
                continue
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            # `record_reason` too: `_unsupported_shape` answers a `SkipReason`,
            # recorded whole.
            if node.func.attr not in {"record", "record_reason"} or len(node.args) != 3:
                continue
            tag = node.args[1]
            if not isinstance(tag, ast.Constant) or tag.value not in {"SW-BASE-TYPE", "IMPLEMENTATION-DATA-TYPE"}:
                continue
            found.add((str(tag.value), self._reason(node.args[2])))

        assert found == set(self.GUARDS)


class TestTheOwnerScopedDuplicateRulesAreUntouched:
    """Duplicate content inside one element keeps its own skip and reason under the verdict.

    Member names, a type placed twice, port names, connector and delegation links: the verdict judges a declaration's
    identity, not these.
    """

    @staticmethod
    def _model() -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        # A duplicate base type, so the verdict really is active.
        model.sw_base_types.append(_base("/Acme/Left/Types/Flag", "Flag"))
        model.sw_base_types.append(_base("/Acme/Right/Types/Flag", "Flag"))
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Sensor",
                "Sensor",
                [
                    _port(
                        "/Acme/Common/Components/Sensor/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel"
                    ),
                    _port(
                        "/Acme/Common/Components/Sensor/pLevel2", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel"
                    ),
                ],
            )
        )
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Brain",
                "Brain",
                [_port("/Acme/Common/Components/Brain/rLevel", "rLevel", "R-PORT-PROTOTYPE", "/Acme/Common/If/ILevel")],
            )
        )
        model.compositions.append(
            CompositionInfo(
                path="/Acme/Common/Comps/Cluster",
                name="Cluster",
                description="",
                ports=[
                    _port("/Acme/Common/Comps/Cluster/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel"),
                ],
                prototypes=[
                    ComponentPrototypeInfo(
                        path="/Acme/Common/Comps/Cluster/sensor",
                        name="sensor",
                        type_ref="/Acme/Common/Components/Sensor",
                        dest=APPLICATION_DEST,
                    ),
                    # A duplicate MEMBER name.
                    ComponentPrototypeInfo(
                        path="/Acme/Common/Comps/Cluster/sensor2",
                        name="sensor",
                        type_ref="/Acme/Common/Components/Brain",
                        dest=APPLICATION_DEST,
                    ),
                    ComponentPrototypeInfo(
                        path="/Acme/Common/Comps/Cluster/brain",
                        name="brain",
                        type_ref="/Acme/Common/Components/Brain",
                        dest=APPLICATION_DEST,
                    ),
                    # The SAME TYPE placed a second time.
                    ComponentPrototypeInfo(
                        path="/Acme/Common/Comps/Cluster/brainAgain",
                        name="brainAgain",
                        type_ref="/Acme/Common/Components/Brain",
                        dest=APPLICATION_DEST,
                    ),
                ],
                assembly_connectors=[
                    AssemblyConnectorInfo(
                        path="/Acme/Common/Comps/Cluster/WireA",
                        name="WireA",
                        provider=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/sensor",
                            port_ref="/Acme/Common/Components/Sensor/pLevel",
                        ),
                        requester=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/brain",
                            port_ref="/Acme/Common/Components/Brain/rLevel",
                        ),
                    ),
                    # The SAME LINK under a second SHORT-NAME.
                    AssemblyConnectorInfo(
                        path="/Acme/Common/Comps/Cluster/WireB",
                        name="WireB",
                        provider=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/sensor",
                            port_ref="/Acme/Common/Components/Sensor/pLevel",
                        ),
                        requester=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/brain",
                            port_ref="/Acme/Common/Components/Brain/rLevel",
                        ),
                    ),
                ],
                delegation_connectors=[
                    DelegationConnectorInfo(
                        path="/Acme/Common/Comps/Cluster/DelA",
                        name="DelA",
                        inner=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/sensor",
                            port_ref="/Acme/Common/Components/Sensor/pLevel",
                        ),
                        outer_port_ref="/Acme/Common/Comps/Cluster/pOut",
                    ),
                    # The SAME DELEGATION LINK under a second SHORT-NAME.
                    DelegationConnectorInfo(
                        path="/Acme/Common/Comps/Cluster/DelB",
                        name="DelB",
                        inner=ConnectorPortRef(
                            component_ref="/Acme/Common/Comps/Cluster/sensor",
                            port_ref="/Acme/Common/Components/Sensor/pLevel",
                        ),
                        outer_port_ref="/Acme/Common/Comps/Cluster/pOut",
                    ),
                ],
            )
        )
        return model

    @pytest.fixture(scope="class")
    @staticmethod
    def outcome():
        return _map_all(TestTheOwnerScopedDuplicateRulesAreUntouched._model())

    def test_the_verdict_really_is_active(self, outcome) -> None:
        assert _group(outcome["decisions"], "base_types/SW-BASE-TYPE/Flag").verdict == MERGED

    def test_a_duplicate_port_name_keeps_its_skip_and_its_reason(self, outcome) -> None:
        assert _skip_reasons(outcome["swc_skips"], "/Acme/Common/Components/Sensor/pLevel2") == [
            "duplicate port name 'pLevel' on SWC 'Sensor'"
        ]

    def test_a_duplicate_member_name_keeps_its_skip_and_its_reason(self, outcome) -> None:
        assert _skip_reasons(outcome["composition_skips"], "/Acme/Common/Comps/Cluster/sensor2") == [
            "duplicate member name 'sensor' in composition 'Cluster'"
        ]

    def test_a_type_placed_twice_keeps_its_skip_and_its_reason(self, outcome) -> None:
        reasons = _skip_reasons(outcome["composition_skips"], "/Acme/Common/Comps/Cluster/brainAgain")

        assert len(reasons) == 1
        assert "already places type 'Brain'" in reasons[0]

    def test_a_duplicate_connector_link_keeps_its_skip_and_its_reason(self, outcome) -> None:
        reasons = _skip_reasons(outcome["composition_skips"], "/Acme/Common/Comps/Cluster/WireB")

        assert len(reasons) == 1
        assert "duplicate connector link" in reasons[0]
        assert "`rule:connector.duplicate-link`" in reasons[0]

    def test_a_duplicate_delegation_link_keeps_its_skip_and_its_reason(self, outcome) -> None:
        reasons = _skip_reasons(outcome["composition_skips"], "/Acme/Common/Comps/Cluster/DelB")

        assert len(reasons) == 1
        assert "duplicate delegation link" in reasons[0]
        assert "`rule:composition.duplicate-delegation-link`" in reasons[0]


class TestImportComponentIsUnchanged:
    """The verdict is ``import model``'s alone: ``import component`` renames where the verdict drops both.

    Does not cover the unresolved-reference half of the same guarantee.
    """

    @pytest.fixture()
    def project(self, tmp_path: Path) -> Path:
        return _init_project(tmp_path) / "swcraft.yaml"

    def test_the_model_importer_drops_both_declarations(self) -> None:
        decisions, data_types, _registry, _skips = _map_types(read_shape_model("component_same_name_pair.arxml"))

        assert _group(decisions, "base_types/SW-BASE-TYPE/Octet").verdict == CONFLICTING
        assert "Octet" not in {dt.name for dt in data_types}

    def test_the_component_importer_refuses_the_rename_as_it_did_before(self, project, tmp_path) -> None:
        from swcraft_tools.component_exchange import import_component
        from swcraft_tools.component_exchange.import_ import ReservedNameError

        source = tmp_path / "component.arxml"
        shutil.copyfile(shape_path("component_same_name_pair.arxml"), source)

        with pytest.raises(ReservedNameError) as excinfo:
            import_component(str(source), str(project), dry_run=True)

        # `import component` refuses a rename inside a populated project; the
        # verdict would have dropped both declarations and never asked for a
        # rename, so that it still asks is the fence.
        assert "/Acme/Right/Types/Octet -> Octet_app" in str(excinfo.value)

    def test_the_component_importer_takes_no_verdict_at_all(self, tmp_path) -> None:
        # The mapping functions default to no policy.
        model = read_arxml(str(shape_path("component_same_name_pair.arxml")))

        data_types, registry, _skips = map_data_types(model, elements=[])

        assert {dt.name for dt in data_types} >= {"Octet", "Octet_app"}
        assert registry.resolve("/Acme/Left/Types/Octet") == "Octet"
        assert registry.resolve("/Acme/Right/Types/Octet") == "Octet_app"


class TestTheMergeAndConflictReportCategories:
    """A merged and a conflicting group each get a category of one line per group: kind, name, every source path.

    A conflict line ends in one sentence per way the verdict is reached (``difference``, ``inherited_from``,
    ``cyclic``). Does not cover the unresolved category a conflict leaves (``test_arxml_import_unresolved.py``), the
    summary counts, ``--dry-run`` or the path-free rule (``test_arxml_import_cli.py``).
    """

    @staticmethod
    def _section(rendered: str, heading_word: str) -> list[str]:
        """The lines of the section whose heading starts with *heading_word*."""
        lines = rendered.split("\n")
        starts = [index for index, line in enumerate(lines) if line.startswith(heading_word)]
        assert len(starts) == 1, f"expected one {heading_word!r} heading, found {len(starts)}"
        start = starts[0]
        # The next blank line or the report's end: no fixed line closes every report.
        end = lines.index("", start) if "" in lines[start:] else len(lines)
        return lines[start:end]

    @staticmethod
    def _report(tmp_path: Path, fixture: str):
        from swcraft_tools.arxml_import import run_import

        project = _init_project(tmp_path)
        return run_import(str(shape_path(fixture)), str(project / "swcraft.yaml"), dry_run=True)

    def test_a_merged_group_is_one_line_naming_every_source_path(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "identical_duplicates.arxml")

        section = self._section(report.render(), "Merged declarations")

        assert section[0].startswith("Merged declarations (2;")
        assert (
            "  - base_types/SW-BASE-TYPE/Flag declared at /Acme/Left/Types/Flag, "
            "/Acme/Right/Types/Flag -- kept as one element"
        ) in section

    def test_a_description_the_merged_element_did_not_carry_is_named_with_its_path(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "identical_duplicates.arxml")

        section = self._section(report.render(), "Merged declarations")
        dropped = [line for line in section if "description not carried" in line]

        assert len(dropped) == 1
        assert dropped[0].startswith("      description not carried, from /Acme/Right/Interfaces/ILevel: ")
        assert dropped[0].endswith("deliberately different).")

    def test_a_conflict_of_its_own_names_the_property_the_declarations_differ_in(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "transitive_conflict.arxml")

        section = self._section(report.render(), "Conflicting declarations")

        assert section[0].startswith("Conflicting declarations (2;")
        assert (
            "  - base_types/SW-BASE-TYPE/Word16 declared at /Acme/Left/Types/Word16, "
            "/Acme/Right/Types/Word16 -- not imported: the declarations differ in BASE-TYPE-SIZE"
        ) in section

    def test_an_inherited_conflict_names_the_group_it_inherits_from(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "transitive_conflict.arxml")

        section = self._section(report.render(), "Conflicting declarations")
        line = next(line for line in section if "/Distance " in line)

        assert line == (
            "  - implementation_types/IMPLEMENTATION-DATA-TYPE/Distance declared at /Acme/Left/Types/Distance, "
            "/Acme/Right/Types/Distance -- not imported: it references base_types/SW-BASE-TYPE/Word16, "
            "which is itself declared more than once with different definitions"
        )
        # The difference is in the group it inherits from, not in this one.
        assert "differ in" not in line

    def test_a_cyclic_group_says_so_rather_than_naming_a_property(self, tmp_path: Path) -> None:
        import dataclasses

        decisions = decide_merges(read_shape_model("cyclic_composition_graph.arxml"))
        # The cyclic fixture has no root composition, so `run_import` refuses it
        # before any report exists; the renderer is asked directly.
        report = dataclasses.replace(
            self._report(tmp_path, "identical_duplicates.arxml"),
            merged_groups=[],
            conflicting_groups=list(decisions.conflicting),
        )

        section = self._section(report.render(), "Conflicting declarations")

        assert len(section) == 3
        for line in section[1:]:
            assert line.endswith("-- not imported: the verdict depends on itself through a reference cycle")

    def test_neither_category_is_rendered_when_the_export_declares_every_name_once(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, "unresolved_reference_roles.arxml")

        rendered = report.render()

        assert (report.merged_groups, report.conflicting_groups) == ([], [])
        assert "Merged declarations" not in rendered
        assert "Conflicting declarations" not in rendered


class TestTheVerdictsCrossKindAndRootConsequences:
    """What the verdict costs the mapping layer **outside** the group's own layer."""

    @staticmethod
    def _component_group_beside_a_composition_of_that_name() -> ImportModel:
        """Two differing components named ``Sensor``, and a composition of that name placed by a root."""
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        model.swcs.append(
            _swc(
                "/Acme/Left/Components/Sensor",
                "Sensor",
                [_port("/Acme/Left/Components/Sensor/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel")],
            )
        )
        model.swcs.append(_swc("/Acme/Right/Components/Sensor", "Sensor", []))
        model.compositions.append(
            CompositionInfo(
                path="/Acme/Comps/Sensor",
                name="Sensor",
                description="",
                ports=[],
                prototypes=[],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
        _root_over(model, "/Acme/Comps/Sensor")
        return model

    def test_a_conflicting_component_group_holds_its_name_against_a_composition(self) -> None:
        """A conflicting component group's name still refuses a composition of that name.

        Otherwise repairing the export would make the composition vanish from the next import.
        """
        outcome = _map_all(self._component_group_beside_a_composition_of_that_name())

        assert _group(outcome["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Sensor").verdict == CONFLICTING
        assert [imported.swc.name for imported in outcome["swcs"]] == []
        assert [c.name for c in outcome["mapped"].compositions] == ["Root"]
        assert [
            entry.reason
            for entry in outcome["composition_skips"]
            if entry.path == "/Acme/Comps/Sensor" and "duplicate component-type name" in entry.reason
        ] != []

    def test_the_composition_is_refused_in_both_worlds(self) -> None:
        """With identical components merged, the composition of that name is refused for the same reason."""
        model = self._component_group_beside_a_composition_of_that_name()
        model.swcs[1] = _swc(
            "/Acme/Right/Components/Sensor",
            "Sensor",
            [_port("/Acme/Right/Components/Sensor/pLevel", "pLevel", "P-PORT-PROTOTYPE", "/Acme/Common/If/ILevel")],
        )

        outcome = _map_all(model)

        assert _group(outcome["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Sensor").verdict == MERGED
        assert [imported.swc.name for imported in outcome["swcs"]] == ["Sensor"]
        assert [c.name for c in outcome["mapped"].compositions] == ["Root"]

    def test_a_merged_root_pair_is_one_root_and_not_a_refused_import(self) -> None:
        """``derive_root`` reads the verdict, not the raw declarations.

        Guards a defect where a merged pair of top compositions read as two roots and aborted the import.
        """
        from swcraft_tools.arxml_import.connectors import derive_root

        model = ImportModel(source_path="inline")
        for tree in ("Left", "Right"):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/{tree}/Top",
                    name="Top",
                    description="",
                    ports=[],
                    prototypes=[],
                    assembly_connectors=[],
                    delegation_connectors=[],
                )
            )
        decisions = decide_merges(model)

        assert _group(decisions, "compositions/COMPOSITION-SW-COMPONENT-TYPE/Top").verdict == MERGED
        assert derive_root(model, decisions).path == "/Acme/Left/Top"
        # With no verdict -- ``import component``'s value -- two roots are refused.
        with pytest.raises(ValueError, match="no single root composition"):
            derive_root(model)

    def test_a_conflicting_root_pair_is_refused_naming_the_verdict(self) -> None:
        """A conflicting root pair imports nothing, so the import is refused for having no root."""
        from swcraft_tools.arxml_import.connectors import derive_root

        model = ImportModel(source_path="inline")
        model.compositions.append(
            CompositionInfo(
                path="/Acme/Left/Top",
                name="Top",
                description="",
                ports=[],
                prototypes=[
                    ComponentPrototypeInfo(
                        path="/Acme/Left/Top/part", name="part", type_ref="/Acme/Absent", dest=APPLICATION_DEST
                    )
                ],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
        model.compositions.append(
            CompositionInfo(
                path="/Acme/Right/Top",
                name="Top",
                description="",
                ports=[],
                prototypes=[],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
        decisions = decide_merges(model)

        assert _group(decisions, "compositions/COMPOSITION-SW-COMPONENT-TYPE/Top").verdict == CONFLICTING
        with pytest.raises(ValueError, match="no root composition"):
            derive_root(model, decisions)

    def test_two_connectors_of_one_name_in_two_compositions_are_both_dropped(self) -> None:
        """A decided loss: two compositions' connectors of one name conflict on ``composition``.

        The ``connectors/`` layer is flat while a connector's name is composition-scoped; *first wins* would be silent.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Common/If/ILevel", "ILevel", type_ref="/Acme/Common/Impl/Level"))
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Producer",
                "Producer",
                [
                    _port(
                        "/Acme/Common/Components/Producer/pLevel",
                        "pLevel",
                        "P-PORT-PROTOTYPE",
                        "/Acme/Common/If/ILevel",
                    )
                ],
            )
        )
        model.swcs.append(
            _swc(
                "/Acme/Common/Components/Consumer",
                "Consumer",
                [
                    _port(
                        "/Acme/Common/Components/Consumer/rLevel",
                        "rLevel",
                        "R-PORT-PROTOTYPE",
                        "/Acme/Common/If/ILevel",
                    )
                ],
            )
        )
        for owner in ("Alpha", "Beta"):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/Comps/{owner}",
                    name=owner,
                    description="",
                    ports=[],
                    prototypes=[
                        ComponentPrototypeInfo(
                            path=f"/Acme/Comps/{owner}/producer",
                            name="producer",
                            type_ref="/Acme/Common/Components/Producer",
                            dest=APPLICATION_DEST,
                        ),
                        ComponentPrototypeInfo(
                            path=f"/Acme/Comps/{owner}/consumer",
                            name="consumer",
                            type_ref="/Acme/Common/Components/Consumer",
                            dest=APPLICATION_DEST,
                        ),
                    ],
                    assembly_connectors=[
                        AssemblyConnectorInfo(
                            path=f"/Acme/Comps/{owner}/Wire",
                            name="Wire",
                            provider=ConnectorPortRef(
                                component_ref=f"/Acme/Comps/{owner}/producer",
                                port_ref="/Acme/Common/Components/Producer/pLevel",
                            ),
                            requester=ConnectorPortRef(
                                component_ref=f"/Acme/Comps/{owner}/consumer",
                                port_ref="/Acme/Common/Components/Consumer/rLevel",
                            ),
                        )
                    ],
                    delegation_connectors=[],
                )
            )
        _root_over(model, "/Acme/Comps/Alpha", "/Acme/Comps/Beta")

        outcome = _map_all(model)
        group = _group(outcome["decisions"], "connectors/ASSEMBLY-SW-CONNECTOR/Wire")

        assert (group.verdict, group.difference) == (CONFLICTING, "composition")
        assert [c.name for c in outcome["mapped"].connectors] == []
        # The loss is the conflict record's, not a duplicate-name skip's.
        assert [
            entry.reason for entry in outcome["composition_skips"] if "duplicate connector name" in entry.reason
        ] == []

    def test_an_excluded_member_binds_no_port_to_the_kept_twin(self) -> None:
        """A ``--packages``-excluded member records no alias, so a port naming it binds to nothing.

        Guards a defect where ``build_interface_path_index`` bound the port to the kept twin.
        """
        from swcraft_tools.arxml_import.merge import narrow_to_selection
        from swcraft_tools.arxml_import.unresolved import CAUSE_SELECTION

        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/App/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/App/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/App/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/TreeA/If/ISig", "ISig", element="alpha", type_ref="/Acme/App/Impl/Level"))
        model.sr_interfaces.append(_sr("/Acme/TreeB/If/ISig", "ISig", element="beta", type_ref="/Acme/App/Impl/Level"))
        model.swcs.append(
            _swc(
                "/Acme/App/Components/Ctrl",
                "Ctrl",
                [_port("/Acme/App/Components/Ctrl/rSig", "rSig", "R-PORT-PROTOTYPE", "/Acme/TreeB/If/ISig")],
            )
        )
        # `UnresolvedReferences` asks the path index whether the export declares
        # the target; without it the cause would be `absent`, not the selection.
        for declaration in (*model.sr_interfaces, *model.swcs):
            model.path_index[declaration.path] = raw_element(declaration.path)

        decisions = narrow_to_selection(
            decide_merges(model),
            selected=frozenset({"/Acme/TreeA/If/ISig", "/Acme/App/Components/Ctrl"}),
            reached=frozenset(),
        )
        group = _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISig")
        assert (group.verdict, group.primary) == ("selected", "/Acme/TreeA/If/ISig")

        forms: list = []
        data_types, registry, _ = map_data_types(model, merges=decisions, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=decisions, elements=forms)
        assert [(iface.name, [element.name for element in iface.data_elements]) for iface in interfaces] == [
            ("ISig", ["alpha"])
        ]

        unresolved = UnresolvedReferences(model, decisions)
        swcs, _ = map_swcs(
            model,
            interfaces,
            iface_skip_log=iface_skips,
            merges=decisions,
            unresolved=unresolved,
            port_inits={},
            variable_types={},
        )

        # Kept with the export's own path, not bound to TreeA's `ISig`.
        assert [(port.name, port.interface) for port in swcs[0].swc.ports] == [("rSig", "/Acme/TreeB/If/ISig")]
        assert [(record.target, record.cause) for record in unresolved.references] == [
            ("/Acme/TreeB/If/ISig", CAUSE_SELECTION)
        ]

    def test_a_merged_group_whose_primary_was_skipped_binds_no_port_either(self) -> None:
        """An ``ALIAS`` member binds a port only while its group's primary was mapped.

        Guards a silent cross-kind binding onto a same-name sender-receiver interface; ``merges=None`` is the oracle.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/App/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/App/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/App/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/App/If/IX", "IX", element="alpha", type_ref="/Acme/App/Impl/Level"))
        for tree in ("TreeA", "TreeB"):
            model.cs_interfaces.append(
                CSInterfaceInfo(
                    path=f"/Acme/{tree}/If/IX",
                    name="IX",
                    description="",
                    operations=[
                        CSOperationInfo(
                            name="compute",
                            arguments=[CSArgumentInfo(name="request", type_ref="/Acme/App/Impl/Level", direction="IN")],
                        )
                    ],
                )
            )
        model.swcs.append(
            _swc(
                "/Acme/App/Components/Ctrl",
                "Ctrl",
                [_port("/Acme/App/Components/Ctrl/rX", "rX", "R-PORT-PROTOTYPE", "/Acme/TreeB/If/IX")],
            )
        )

        decisions = decide_merges(model)
        group = _group(decisions, "interfaces/CLIENT-SERVER-INTERFACE/IX")
        assert (group.verdict, group.primary) == (MERGED, "/Acme/TreeA/If/IX")

        forms: list = []
        data_types, registry, _ = map_data_types(model, merges=decisions, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=decisions, elements=forms)
        assert [(iface.name, iface.kind) for iface in interfaces] == [("IX", "sender_receiver")]
        assert any(
            "duplicate interface name 'IX'" in reason for reason in _skip_reasons(iface_skips, "/Acme/TreeA/If/IX")
        )

        swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, merges=decisions, port_inits={}, variable_types={}
        )

        assert [port.name for port in swcs[0].swc.ports] == []
        assert any(
            "unresolved reference /Acme/TreeB/If/IX" in reason
            for reason in _skip_reasons(swc_skips, "/Acme/App/Components/Ctrl/rX")
        )

        # The oracle: `merges=None` answers the same.
        bare_types, bare_registry, _ = map_data_types(model, elements=forms)
        bare_interfaces, bare_iface_skips = map_interfaces(model, bare_registry, bare_types, elements=forms)
        bare_swcs, _ = map_swcs(
            model, bare_interfaces, iface_skip_log=bare_iface_skips, port_inits={}, variable_types={}
        )
        assert [port.name for port in bare_swcs[0].swc.ports] == []


class TestAContestedNameAlreadyTakenClaimsTheReplacement:
    """``PathRegistry.claim``'s collision branch: a taken contested name claims the replacement it would have had.

    Getting it wrong shifts every later cross-kind declaration one suffix.
    """

    @staticmethod
    def _model(*, with_the_conflicting_group: bool) -> ImportModel:
        from swcraft_tools.arxml_reader import (
            AppArrayDataTypeInfo,
            AppRecordDataTypeInfo,
            RecordElementInfo,
        )

        model = ImportModel(source_path="inline")
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Acme/Common/AppTypes/Speed", name="Speed", compu_method_ref=None, description=""
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Acme/Common/AppTypes/Level", name="Level", compu_method_ref=None, description=""
            )
        )
        if with_the_conflicting_group:
            for tree, element in (("Left", "a"), ("Right", "b")):
                model.app_record_data_types.append(
                    AppRecordDataTypeInfo(
                        path=f"/Acme/{tree}/Records/Speed",
                        name="Speed",
                        elements=[RecordElementInfo(name=element, type_ref="/Acme/Common/AppTypes/Level")],
                        description="",
                    )
                )
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Acme/Common/Arrays/Speed",
                name="Speed",
                element_type_ref="/Acme/Common/AppTypes/Level",
                max_number_of_elements=4,
                description="",
            )
        )
        return model

    def test_the_claim_consumes_the_replacement_the_group_would_have_taken(self) -> None:
        decisions, _data_types, registry, _skips = _map_types(self._model(with_the_conflicting_group=True))

        assert _group(decisions, "application_types/APPLICATION-RECORD-DATA-TYPE/Speed").verdict == CONFLICTING
        # The group holds the first free suffix although nothing was imported under it.
        assert "Speed_app" in registry.claimed_names("application_types")

    def test_the_later_cross_kind_declaration_lands_one_candidate_beyond(self) -> None:
        with_group = _map_types(self._model(with_the_conflicting_group=True))
        without_group = _map_types(self._model(with_the_conflicting_group=False))

        assert with_group[2].resolve("/Acme/Common/Arrays/Speed") == "Speed_app_arrays"
        assert without_group[2].resolve("/Acme/Common/Arrays/Speed") == "Speed_app"
        # Repairing the record conflict moves the array's name and nothing else.
        assert with_group[2].resolve("/Acme/Common/AppTypes/Speed") == "Speed"
        assert without_group[2].resolve("/Acme/Common/AppTypes/Speed") == "Speed"


class TestABaseTypeAndASameNameImplementationTypeAreTwoLayers:
    """A same-name ``SW-BASE-TYPE`` and ``IMPLEMENTATION-DATA-TYPE`` are two layers.

    Nothing is refused, and a conflicting base-type group claims the name in ``base_types`` alone.
    """

    @staticmethod
    def _model(*, with_the_conflicting_group: bool) -> ImportModel:
        model = ImportModel(source_path="inline")
        if with_the_conflicting_group:
            model.sw_base_types.append(_base("/Acme/Left/Types/Speed", "Speed", size=8))
            model.sw_base_types.append(_base("/Acme/Right/Types/Speed", "Speed", size=16))
        else:
            model.sw_base_types.append(_base("/Acme/Left/Types/Speed", "Speed", size=8))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Speed",
                name="Speed",
                category="VALUE",
                base_type_ref="/Acme/Left/Types/Speed",
                impl_type_ref=None,
                description="",
            )
        )
        return model

    def test_the_group_claims_the_name_in_the_base_layer_alone(self) -> None:
        decisions, data_types, registry, skips = _map_types(self._model(with_the_conflicting_group=True))

        assert _group(decisions, "base_types/SW-BASE-TYPE/Speed").verdict == CONFLICTING
        assert [dt.name for dt in data_types] == []
        assert registry.claimed_names("base_types") == {"Speed"}
        assert registry.claimed_names("implementation_types") == frozenset()
        # Dropped only with its base type: no same-name refusal is recorded.
        assert [(entry.path, entry.reason) for entry in skips] == [
            ("/Acme/Common/Impl/Speed", "unresolved reference /Acme/Left/Types/Speed (base type was skipped)")
        ]

    def test_the_pair_alone_is_two_elements_under_one_name(self) -> None:
        """With no group, both are imported under the declared name, the value type resting on the base type."""
        _decisions, data_types, registry, skips = _map_types(self._model(with_the_conflicting_group=False))

        assert [(dt.kind, dt.name) for dt in data_types] == [("base_type", "Speed"), ("value_type", "Speed")]
        assert data_types[1].base == "Speed"
        assert registry.claimed_names("base_types") == {"Speed"}
        assert registry.claimed_names("implementation_types") == {"Speed"}
        assert list(skips) == []
        assert registry.renames == []


class TestTheComparisonsFailClosedFallbacks:
    """Four fail-closed branches of ``_signature``'s helpers, each a stated decision."""

    @staticmethod
    def _interfaces_with_inits(left_ref: str | None, right_ref: str | None) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        for tree, ref in (("Left", left_ref), ("Right", right_ref)):
            model.sr_interfaces.append(
                SRInterfaceInfo(
                    path=f"/Acme/{tree}/If/ILevel",
                    name="ILevel",
                    description="",
                    data_elements=[SRDataElementInfo(name="value", type_ref="/Acme/Common/Impl/Level", init_ref=ref)],
                )
            )
        return model

    @staticmethod
    def _constant(path: str, value, unreadable: str | None = None):
        return ConstantSpecInfo(
            path=path,
            name=path.rsplit("/", 1)[-1],
            value=None if value is None else ConstantScalar(value),
            unreadable_reason=unreadable,
        )

    def test_two_interfaces_differing_only_in_an_init_value_conflict(self) -> None:
        """Two interfaces differing only in an init value conflict: the constant is compared by value, not reference."""
        model = self._interfaces_with_inits("/Acme/Left/Consts/Zero", "/Acme/Right/Consts/Zero")
        model.constant_specs.append(self._constant("/Acme/Left/Consts/Zero", 0))
        model.constant_specs.append(self._constant("/Acme/Right/Consts/Zero", 7))

        group = _group(decide_merges(model), "interfaces/SENDER-RECEIVER-INTERFACE/ILevel")

        assert (group.verdict, group.difference) == (CONFLICTING, "DATA-ELEMENTS")

    def test_two_interfaces_whose_two_constants_carry_one_value_merge(self) -> None:
        model = self._interfaces_with_inits("/Acme/Left/Consts/Zero", "/Acme/Right/Consts/Zero")
        model.constant_specs.append(self._constant("/Acme/Left/Consts/Zero", 0))
        model.constant_specs.append(self._constant("/Acme/Right/Consts/Zero", 0))

        assert _group(decide_merges(model), "interfaces/SENDER-RECEIVER-INTERFACE/ILevel").verdict == MERGED

    def test_a_constant_the_file_does_not_declare_falls_back_to_its_own_token(self) -> None:
        """Two undeclared constants at two paths are two tokens, so the pair conflicts."""
        model = self._interfaces_with_inits("/Acme/Left/Consts/Zero", "/Acme/Right/Consts/Zero")

        group = _group(decide_merges(model), "interfaces/SENDER-RECEIVER-INTERFACE/ILevel")

        assert (group.verdict, group.difference) == (CONFLICTING, "DATA-ELEMENTS")

    def test_a_constant_the_reader_could_not_read_carries_its_reason_into_the_comparison(self) -> None:
        model = self._interfaces_with_inits("/Acme/Common/Consts/Zero", "/Acme/Common/Consts/Zero")
        model.constant_specs.append(self._constant("/Acme/Common/Consts/Zero", None, "unsupported value shape"))

        # One constant, one token: the fallback refuses to assume a value, not a
        # pair naming the same unreadable constant.
        assert _group(decide_merges(model), "interfaces/SENDER-RECEIVER-INTERFACE/ILevel").verdict == MERGED

    def test_an_application_primitive_whose_compu_method_is_not_declared_conflicts(self) -> None:
        """``_enum_signature`` refuses a pair whose backing method cannot be resolved."""
        model = ImportModel(source_path="inline")
        for tree in ("Left", "Right"):
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"/Acme/{tree}/Types/Mode",
                    name="Mode",
                    compu_method_ref=f"/Acme/{tree}/Compu/ModeMethod",
                    description="",
                )
            )

        group = _group(decide_merges(model), "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/Mode")

        assert (group.verdict, group.difference) == (CONFLICTING, "COMPU-METHOD")

    def test_a_blank_short_name_is_not_a_declaration_of_a_name(self) -> None:
        """Two blanks are not a group: that would refuse two elements over a defect each already has."""
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Left/Types/", "", size=8))
        model.sw_base_types.append(_base("/Acme/Right/Types/", "  ", size=16))

        assert decide_merges(model).groups == ()

    def test_a_reference_to_nothing_is_compared_as_the_export_spelled_it(self) -> None:
        """An absent target is compared by its own path: two absent targets conflict, one shared merges."""
        model = ImportModel(source_path="inline")
        for tree, target in (("Left", "/Vendor/A"), ("Right", "/Vendor/B")):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Impl/Level",
                    name="Level",
                    category="TYPE_REFERENCE",
                    base_type_ref=None,
                    impl_type_ref=target,
                    description="",
                )
            )

        differing = _group(decide_merges(model), "implementation_types/IMPLEMENTATION-DATA-TYPE/Level")
        assert (differing.verdict, differing.difference) == (CONFLICTING, "IMPLEMENTATION-DATA-TYPE-REF")

        model.impl_data_types[1] = ImplDataTypeInfo(
            path="/Acme/Right/Impl/Level",
            name="Level",
            category="TYPE_REFERENCE",
            base_type_ref=None,
            impl_type_ref="/Vendor/A",
            description="",
        )
        assert _group(decide_merges(model), "implementation_types/IMPLEMENTATION-DATA-TYPE/Level").verdict == MERGED


class TestTheSelectionReachWalksEveryDeclarationKind:
    """``report.selection_reach`` over every declaration kind, and a diamond.

    It reads the closure off paths, not emitted names; a wrong answer refuses a narrowed import or re-admits the wrong
    member. It must declare every target the mapping layer can rest on, since the twin branch is decided after
    narrowing. A component's ``DATA-TYPE-MAPPING-REF`` is followed; a map's two sides from the set are not (they would
    re-admit every type one global set names).

    Does not reach the references the walk deliberately skips (a compu method, a constant, a map's sides from the set),
    a delegation connector, or a connector's or delegation's endpoint; the function's docstring argues each.
    """

    @staticmethod
    def _model() -> ImportModel:
        """One root placing one component whose interface reaches one base type through record, array and operation.

        The base type is a diamond, so the walk meets it more than once and the already-reached guard runs.
        """
        from swcraft_tools.arxml_reader import (
            AppArrayDataTypeInfo,
            AppRecordDataTypeInfo,
            RecordElementInfo,
        )

        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Deep/Types/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Deep/Impl/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Deep/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Acme/Deep/AppTypes/Mode", name="Mode", compu_method_ref=None, description=""
            )
        )
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Acme/Deep/AppTypes/Status",
                name="Status",
                elements=[RecordElementInfo(name="level", type_ref="/Acme/Deep/Impl/Level")],
                description="",
            )
        )
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Acme/Deep/AppTypes/History",
                name="History",
                element_type_ref="/Acme/Deep/AppTypes/Status",
                max_number_of_elements=4,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Deep/If/IHistory", "IHistory", type_ref="/Acme/Deep/AppTypes/History"))
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Acme/Deep/If/ICalc",
                name="ICalc",
                description="",
                operations=[
                    CSOperationInfo(
                        name="compute",
                        arguments=[CSArgumentInfo(name="request", type_ref="/Acme/Deep/Impl/Level", direction="IN")],
                    )
                ],
            )
        )
        model.swcs.append(
            _swc(
                "/Acme/Deep/Components/Sensor",
                "Sensor",
                [
                    _port(
                        "/Acme/Deep/Components/Sensor/rHistory",
                        "rHistory",
                        "R-PORT-PROTOTYPE",
                        "/Acme/Deep/If/IHistory",
                    ),
                    _port("/Acme/Deep/Components/Sensor/rCalc", "rCalc", "R-PORT-PROTOTYPE", "/Acme/Deep/If/ICalc"),
                ],
            )
        )
        model.compositions.append(
            CompositionInfo(
                path="/Acme/App/Comps/Root",
                name="Root",
                description="",
                ports=[
                    SwcPortInfo(
                        path="/Acme/App/Comps/Root/rHistory",
                        name="rHistory",
                        kind="R-PORT-PROTOTYPE",
                        interface_ref="/Acme/Deep/If/IHistory",
                        description="",
                    )
                ],
                prototypes=[
                    ComponentPrototypeInfo(
                        path="/Acme/App/Comps/Root/sensor",
                        name="sensor",
                        type_ref="/Acme/Deep/Components/Sensor",
                        dest=APPLICATION_DEST,
                    )
                ],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
        return model

    def test_the_closure_reaches_every_kind_on_the_path_and_nothing_beside_it(self) -> None:

        inside, reached = _reach(self._model(), {"App"})

        assert inside == frozenset({"/Acme/App/Comps/Root"})
        assert reached == frozenset(
            {
                "/Acme/App/Comps/Root",
                "/Acme/Deep/Components/Sensor",
                "/Acme/Deep/If/IHistory",
                "/Acme/Deep/If/ICalc",
                "/Acme/Deep/AppTypes/History",
                "/Acme/Deep/AppTypes/Status",
                "/Acme/Deep/Impl/Level",
                "/Acme/Deep/Types/Word",
            }
        )
        # What makes the assertion above a closure and not a listing.
        assert "/Acme/Deep/AppTypes/Mode" not in reached

    def test_every_kind_that_takes_a_verdict_is_declared(self) -> None:
        """One declaration of each ``merge.LAYER_BY_TAG`` tag in one selected package; ``inside`` names each.

        A kind added to the verdict without a ``declare`` call fails by name. The three form tags are declared by no
        call: a form has no place in the closure, so a conflicting form group stays conflicting.
        """
        from swcraft_tools.arxml_import.merge import LAYER_BY_TAG
        from swcraft_tools.arxml_reader import (
            AppArrayDataTypeInfo,
            AppRecordDataTypeInfo,
            RecordElementInfo,
        )

        model = ImportModel(source_path="inline")
        path_by_tag: dict[str, str] = {}

        path_by_tag["SW-BASE-TYPE"] = "/Acme/App/Word"
        model.sw_base_types.append(_base("/Acme/App/Word", "Word"))
        path_by_tag["IMPLEMENTATION-DATA-TYPE"] = "/Acme/App/Level"
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/App/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/App/Word",
                impl_type_ref=None,
                description="",
            )
        )
        path_by_tag["APPLICATION-PRIMITIVE-DATA-TYPE"] = "/Acme/App/Mode"
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(path="/Acme/App/Mode", name="Mode", compu_method_ref=None, description="")
        )
        path_by_tag["DATA-TYPE-MAPPING-SET"] = "/Acme/App/Maps"
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Acme/App/Maps",
                name="Maps",
                maps=[DataTypeMapInfo(application_ref="/Acme/App/Mode", implementation_ref="/Acme/App/Level")],
            )
        )
        path_by_tag["APPLICATION-RECORD-DATA-TYPE"] = "/Acme/App/Status"
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Acme/App/Status",
                name="Status",
                elements=[RecordElementInfo(name="level", type_ref="/Acme/App/Level")],
                description="",
            )
        )
        path_by_tag["APPLICATION-ARRAY-DATA-TYPE"] = "/Acme/App/History"
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/Acme/App/History",
                name="History",
                element_type_ref="/Acme/App/Status",
                max_number_of_elements=4,
                description="",
            )
        )
        path_by_tag["SENDER-RECEIVER-INTERFACE"] = "/Acme/App/ISig"
        model.sr_interfaces.append(_sr("/Acme/App/ISig", "ISig", type_ref="/Acme/App/History"))
        path_by_tag["CLIENT-SERVER-INTERFACE"] = "/Acme/App/ICalc"
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Acme/App/ICalc",
                name="ICalc",
                description="",
                operations=[
                    CSOperationInfo(
                        name="compute",
                        arguments=[CSArgumentInfo(name="request", type_ref="/Acme/App/Level", direction="IN")],
                    )
                ],
            )
        )
        path_by_tag["APPLICATION-SW-COMPONENT-TYPE"] = "/Acme/App/Sensor"
        model.swcs.append(
            _swc(
                "/Acme/App/Sensor",
                "Sensor",
                [_port("/Acme/App/Sensor/rSig", "rSig", "R-PORT-PROTOTYPE", "/Acme/App/ISig")],
            )
        )
        path_by_tag["COMPOSITION-SW-COMPONENT-TYPE"] = "/Acme/App/Root"
        path_by_tag["ASSEMBLY-SW-CONNECTOR"] = "/Acme/App/Root/Wire"
        model.compositions.append(
            CompositionInfo(
                path="/Acme/App/Root",
                name="Root",
                description="",
                ports=[],
                prototypes=[
                    ComponentPrototypeInfo(
                        path="/Acme/App/Root/sensor",
                        name="sensor",
                        type_ref="/Acme/App/Sensor",
                        dest=APPLICATION_DEST,
                    )
                ],
                assembly_connectors=[
                    AssemblyConnectorInfo(
                        path="/Acme/App/Root/Wire",
                        name="Wire",
                        provider=ConnectorPortRef(
                            component_ref="/Acme/App/Root/sensor", port_ref="/Acme/App/Sensor/rSig"
                        ),
                        requester=ConnectorPortRef(
                            component_ref="/Acme/App/Root/sensor", port_ref="/Acme/App/Sensor/rSig"
                        ),
                    )
                ],
                delegation_connectors=[],
            )
        )

        assert set(path_by_tag) == set(LAYER_BY_TAG) - {"UNIT", "COMPU-METHOD", "DATA-CONSTR"}, (
            "every tag that takes a verdict needs a declaration in this model, and a declare() "
            "call in `selection_reach`"
        )

        inside, reached = _reach(model, {"App"})

        assert inside == set(path_by_tag.values())
        # The connector joins the closure by containment in its composition.
        assert "/Acme/App/Root/Wire" in reached

    def test_the_walk_terminates_on_a_cycle(self) -> None:
        """The already-reached guard stops the walk on two implementation types naming each other."""

        model = ImportModel(source_path="inline")
        for left, right in (("A", "B"), ("B", "A")):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/Deep/Impl/{left}",
                    name=left,
                    category="TYPE_REFERENCE",
                    base_type_ref=None,
                    impl_type_ref=f"/Acme/Deep/Impl/{right}",
                    description="",
                )
            )
        model.swcs.append(_swc("/Acme/App/Components/Sensor", "Sensor", []))
        model.impl_data_types[0].impl_type_ref = "/Acme/Deep/Impl/B"
        model.swcs[0] = _swc(
            "/Acme/App/Components/Sensor",
            "Sensor",
            [_port("/Acme/App/Components/Sensor/rA", "rA", "R-PORT-PROTOTYPE", "/Acme/Deep/Impl/A")],
        )

        _inside, reached = _reach(model, {"App"})

        assert reached == frozenset({"/Acme/App/Components/Sensor", "/Acme/Deep/Impl/A", "/Acme/Deep/Impl/B"})

    def _mapping_set_model(self, generic_target_name: str) -> ImportModel:
        """One selected primitive whose set carries a generic entry first and its twin last.

        *generic_target_name* equal to the twin's puts the generic target in the twin's group; different, its own.
        """
        from swcraft_tools.arxml_reader import DataTypeMapInfo, DataTypeMappingSetInfo

        model = ImportModel(source_path="inline")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Deep/Impl/Raw",
                name="Raw",
                category="VALUE",
                base_type_ref="/Acme/Deep/Types/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Other/Impl/Fallback",
                name=generic_target_name,
                category="VALUE",
                base_type_ref="/Acme/Other/Types/Octet",
                impl_type_ref=None,
                description="",
            )
        )
        model.sw_base_types.append(_base("/Acme/Deep/Types/Word", "Word"))
        model.sw_base_types.append(_base("/Acme/Other/Types/Octet", "Octet"))
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(path="/Acme/App/Types/Raw", name="Raw", compu_method_ref=None, description="")
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Acme/App/Maps/Set",
                name="Set",
                maps=[
                    DataTypeMapInfo(
                        application_ref="/Acme/App/Types/Raw",
                        implementation_ref="/Acme/Other/Impl/Fallback",
                    ),
                    DataTypeMapInfo(application_ref="/Acme/App/Types/Raw", implementation_ref="/Acme/Deep/Impl/Raw"),
                ],
            )
        )
        model.sr_interfaces.append(_sr("/Acme/App/If/IRaw", "IRaw", type_ref="/Acme/App/Types/Raw"))
        model.swcs.append(
            _swc(
                "/Acme/App/Components/Sensor",
                "Sensor",
                [_port("/Acme/App/Components/Sensor/rRaw", "rRaw", "R-PORT-PROTOTYPE", "/Acme/App/If/IRaw")],
            )
        )
        return model

    def test_an_application_primitives_fall_back_mapping_target_is_an_edge_too(self) -> None:
        """Every ``DATA-TYPE-MAP`` target the mapping layer can come to rest on is an edge.

        A twin narrowed to another member registers nothing and the layer falls through to the generic entry, so that
        entry and its base type must be reached too.
        """

        _inside, reached = _reach(self._mapping_set_model("Fallback"), {"App"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
                "/Acme/Deep/Impl/Raw",
                "/Acme/Other/Impl/Fallback",
                "/Acme/Deep/Types/Word",
                "/Acme/Other/Types/Octet",
            }
        )

    def test_a_fall_back_inside_the_twins_own_group_is_not_a_second_edge(self) -> None:
        """A generic target in the twin's own group is not a second edge.

        Reaching two members keeps the group conflicting, so declaring both would drop the member one edge re-admits.
        """

        _inside, reached = _reach(self._mapping_set_model("Raw"), {"App"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
                "/Acme/Deep/Impl/Raw",
                "/Acme/Deep/Types/Word",
                # Not `/Acme/Other/Impl/Fallback`, the second member of the
                # twin's own group, nor the base type under it.
            }
        )

    @pytest.mark.parametrize(
        ("selection", "member", "member_base"),
        [
            pytest.param(
                {"App", "Other"},
                "/Acme/Other/Impl/Fallback",
                "/Acme/Other/Types/Octet",
                id="the-packages-name-the-fall-back",
            ),
            pytest.param(
                {"App", "Deep"},
                "/Acme/Deep/Impl/Raw",
                "/Acme/Deep/Types/Word",
                id="the-packages-name-the-twin",
            ),
        ],
    )
    def test_the_one_group_exception_declares_the_member_the_packages_name(
        self, selection: set[str], member: str, member_base: str
    ) -> None:
        """Of one group's two candidates the walk declares the member the packages name.

        Guards a defect where the walk followed the twin's base type, narrowing a downstream group to an unwritten
        member.
        """

        _inside, reached = _reach(self._mapping_set_model("Raw"), selection)

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
                member,
                member_base,
            }
        )

    def test_a_one_group_pair_the_packages_name_both_of_is_no_edge_at_all(self) -> None:
        """Both members named: the group stays conflicting, so the primitive rests on none and there is no edge."""

        _inside, reached = _reach(self._mapping_set_model("Raw"), {"App", "Deep", "Other"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
            }
        )

    @staticmethod
    def _merged_one_group_model() -> ImportModel:
        """A merged one-group row with both members named, over a conflicting group one singleton further down.

        ``Word``'s two identical implementation declarations merge and rest on the singleton ``/Acme/Shared/Level``,
        which rests on one member of the conflicting ``Speed`` group, a group with no member inside a selected
        package. Edge inheritance does not reach past the singleton, so the merged group keeps its verdict.
        """
        from swcraft_tools.arxml_reader import DataTypeMapInfo, DataTypeMappingSetInfo

        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Bits/Uint16", "Uint16", size=16))
        model.sw_base_types.append(_base("/Acme/Bits/Uint32", "Uint32", size=32))
        for path, base in (("/Acme/TreeX/Speed", "/Acme/Bits/Uint16"), ("/Acme/TreeY/Speed", "/Acme/Bits/Uint32")):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=path,
                    name="Speed",
                    category="VALUE",
                    base_type_ref=base,
                    impl_type_ref=None,
                    description="",
                )
            )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Shared/Level",
                name="Level",
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref="/Acme/TreeX/Speed",
                description="",
            )
        )
        for path in ("/Acme/Selected/TreeA/Word", "/Acme/Selected/TreeB/Word"):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=path,
                    name="Word",
                    category="TYPE_REFERENCE",
                    base_type_ref=None,
                    impl_type_ref="/Acme/Shared/Level",
                    description="",
                )
            )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(path="/Acme/Selected/App/Word", name="Word", compu_method_ref=None, description="")
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Acme/Selected/Maps/Set",
                name="Set",
                # One entry, so the twin and the generic entry are one path, a
                # member of the merged pair above.
                maps=[
                    DataTypeMapInfo(
                        application_ref="/Acme/Selected/App/Word",
                        implementation_ref="/Acme/Selected/TreeA/Word",
                    )
                ],
            )
        )
        model.sr_interfaces.append(_sr("/Acme/Selected/If/IWord", "IWord", type_ref="/Acme/Selected/App/Word"))
        model.swcs.append(
            _swc(
                "/Acme/Selected/Components/Sensor",
                "Sensor",
                [
                    _port(
                        "/Acme/Selected/Components/Sensor/rWord", "rWord", "R-PORT-PROTOTYPE", "/Acme/Selected/If/IWord"
                    )
                ],
            )
        )
        return model

    def test_a_merged_one_group_pair_the_packages_name_both_of_is_still_an_edge(self) -> None:
        """A merged group both of whose members are named is still an edge, at its primary.

        Guards a defect where the walk answered *no edge* and never reached the singleton or the group below it.
        """
        _inside, reached = _reach(self._merged_one_group_model(), {"Selected"})

        assert reached == frozenset(
            {
                "/Acme/Selected/Components/Sensor",
                "/Acme/Selected/If/IWord",
                "/Acme/Selected/App/Word",
                # The merged group's primary, and on through what it rests on.
                "/Acme/Selected/TreeA/Word",
                "/Acme/Shared/Level",
                "/Acme/TreeX/Speed",
                "/Acme/Bits/Uint16",
            }
        )

    def test_the_merged_pairs_edge_re_admits_the_group_below_the_singleton(self) -> None:
        """The merged pair's edge narrows the conflicting group below the singleton.

        Without it the conflict line would name a declaration from a package the user excluded.
        """
        from swcraft_tools.arxml_import.merge import SELECTED, narrow_to_selection

        model = self._merged_one_group_model()
        inside, reached = _reach(model, {"Selected"})
        narrowed = narrow_to_selection(decide_merges(model), selected=inside, reached=reached)

        speed = _group(narrowed, "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed")
        assert (speed.verdict, speed.selected_path) == (SELECTED, "/Acme/TreeX/Speed")
        assert _group(narrowed, "implementation_types/IMPLEMENTATION-DATA-TYPE/Word").verdict == MERGED

    def test_a_group_the_packages_keep_at_a_third_member_is_no_edge_either(self) -> None:
        """The group is read whole: kept at a third named member, neither the twin nor the generic entry is an edge."""

        model = self._mapping_set_model("Raw")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Third/Impl/Raw",
                name="Raw",
                category="VALUE",
                base_type_ref="/Acme/Third/Types/Byte",
                impl_type_ref=None,
                description="",
            )
        )
        model.sw_base_types.append(_base("/Acme/Third/Types/Byte", "Byte"))

        _inside, reached = _reach(model, {"App", "Third"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
            }
        )

    def _one_group_model_whose_twin_cannot_register(self, trigger: str) -> ImportModel:
        """:meth:`_mapping_set_model`'s one-group row with the twin broken so rows 1-3 register nothing for it.

        Two triggers, so the branch is tied to neither: a base type without ``BASE-TYPE-SIZE`` and an unmapped
        ``CATEGORY``.
        """
        model = self._mapping_set_model("Raw")
        if trigger == "an-unregisterable-base-type":
            # `/Acme/Deep/Types/Word`, the twin's own base type.
            model.sw_base_types[0].size = None
        else:
            model.impl_data_types[0].category = "STRUCTURE"
        return model

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param("an-unregisterable-base-type", id="an-unregisterable-base-type"),
            pytest.param("an-unsupported-category", id="an-unsupported-category"),
        ],
    )
    def test_the_one_group_exception_declares_the_member_the_declarations_say_registers(self, trigger: str) -> None:
        """A twin the declarations say registers nothing yields to the generic entry.

        Declaring the twin would exclude the entry the layer falls through to, losing the primitive and its ports.
        """
        from swcraft_tools.arxml_import.merge import SELECTED, narrow_to_selection

        model = self._one_group_model_whose_twin_cannot_register(trigger)
        inside, reached = _reach(model, {"App"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
                "/Acme/Other/Impl/Fallback",
                "/Acme/Other/Types/Octet",
            }
        )
        narrowed = narrow_to_selection(decide_merges(model), selected=inside, reached=reached)
        group = _group(narrowed, "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw")
        assert (group.verdict, group.selected_path) == (SELECTED, "/Acme/Other/Impl/Fallback")

    @pytest.mark.parametrize(
        "break_the_fallback_too",
        [
            pytest.param(False, id="the-twin-registers"),
            pytest.param(True, id="neither-member-registers"),
        ],
    )
    def test_the_one_group_exception_stays_with_the_twin_unless_told_otherwise(
        self, break_the_fallback_too: bool
    ) -> None:
        """The walk leaves the twin only when the twin registers nothing and the fallback registers."""
        model = self._mapping_set_model("Raw")
        if break_the_fallback_too:
            model.sw_base_types[0].size = None
            model.sw_base_types[1].size = None

        _inside, reached = _reach(model, {"App"})

        assert reached == frozenset(
            {
                "/Acme/App/Components/Sensor",
                "/Acme/App/If/IRaw",
                "/Acme/App/Types/Raw",
                "/Acme/Deep/Impl/Raw",
                "/Acme/Deep/Types/Word",
            }
        )

    def test_two_candidates_in_two_groups_are_both_walked_over_reach_and_all(self) -> None:
        """A known limit: two candidates in two groups are both walked, over-reaching one group deeper.

        ``SHORT-NAME`` equality cannot answer which branch the layer takes; closing it needs the un-narrowed verdict
        passed into the walk.
        """
        from swcraft_tools.arxml_import.merge import narrow_to_selection
        from swcraft_tools.arxml_reader import DataTypeMapInfo, DataTypeMappingSetInfo

        model = ImportModel(source_path="inline")
        for path, name, base in (
            ("/Acme/Deep/Impl/Raw", "Raw", "/Acme/Deep/Types/Word"),
            ("/Acme/Other/Impl/Fallback", "Fallback", "/Acme/Other/Types/Word"),
        ):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=path,
                    name=name,
                    category="VALUE",
                    base_type_ref=base,
                    impl_type_ref=None,
                    description="",
                )
            )
        # One name, two sizes: a conflicting group under BOTH candidates.
        model.sw_base_types.append(_base("/Acme/Deep/Types/Word", "Word", size=8))
        model.sw_base_types.append(_base("/Acme/Other/Types/Word", "Word", size=32))
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(path="/Acme/App/Types/Raw", name="Raw", compu_method_ref=None, description="")
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Acme/App/Maps/Set",
                name="Set",
                maps=[
                    DataTypeMapInfo(
                        application_ref="/Acme/App/Types/Raw",
                        implementation_ref="/Acme/Other/Impl/Fallback",
                    ),
                    DataTypeMapInfo(application_ref="/Acme/App/Types/Raw", implementation_ref="/Acme/Deep/Impl/Raw"),
                ],
            )
        )
        model.sr_interfaces.append(_sr("/Acme/App/If/IRaw", "IRaw", type_ref="/Acme/App/Types/Raw"))
        model.swcs.append(
            _swc(
                "/Acme/App/Components/Sensor",
                "Sensor",
                [_port("/Acme/App/Components/Sensor/rRaw", "rRaw", "R-PORT-PROTOTYPE", "/Acme/App/If/IRaw")],
            )
        )

        inside, reached = _reach(model, {"App"})

        assert {"/Acme/Deep/Types/Word", "/Acme/Other/Types/Word"} <= reached
        narrowed = narrow_to_selection(decide_merges(model), selected=inside, reached=reached)
        assert _group(narrowed, "base_types/SW-BASE-TYPE/Word").verdict == CONFLICTING

    def test_narrowing_leaves_a_merged_group_alone(self) -> None:
        """``narrow_to_selection`` over both verdicts narrows the conflicting group and keeps the merged one."""
        from swcraft_tools.arxml_import.merge import SELECTED, narrow_to_selection

        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/TreeA/Types/Flag", "Flag"))
        model.sw_base_types.append(_base("/Acme/TreeB/Types/Flag", "Flag"))
        model.sw_base_types.append(_base("/Acme/TreeA/Types/Speed", "Speed", size=8))
        model.sw_base_types.append(_base("/Acme/TreeB/Types/Speed", "Speed", size=16))

        decisions = decide_merges(model)
        assert _group(decisions, "base_types/SW-BASE-TYPE/Flag").verdict == MERGED
        assert _group(decisions, "base_types/SW-BASE-TYPE/Speed").verdict == CONFLICTING

        narrowed = narrow_to_selection(
            decisions,
            selected=frozenset({"/Acme/TreeA/Types/Flag", "/Acme/TreeA/Types/Speed"}),
            reached=frozenset(),
        )

        assert _group(narrowed, "base_types/SW-BASE-TYPE/Flag").verdict == MERGED
        speed = _group(narrowed, "base_types/SW-BASE-TYPE/Speed")
        assert (speed.verdict, speed.selected_path) == (SELECTED, "/Acme/TreeA/Types/Speed")


# Ways the comparison or the mapping could read a difference that is not one,
# and a silent drop


class TestAMergedGroupsOtherMemberIsNeverRegisteredUnderAnInventedName:
    """``register_twin`` resolves the implementation path through the merge.

    Guards a defect where an entry naming a non-primary member re-registered it under ``_app``, overriding the merge.
    """

    @staticmethod
    def _model(mapped_onto: str) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Word", "Word"))
        for tree in ("Left", "Right"):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/Acme/{tree}/Speed",
                    name="Speed",
                    category="VALUE",
                    base_type_ref="/Acme/Common/Word",
                    impl_type_ref=None,
                    description="",
                )
            )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Acme/App/Speed",
                name="Speed",
                compu_method_ref=None,
                description="",
            )
        )
        model.data_type_mapping_sets.append(
            DataTypeMappingSetInfo(
                path="/Acme/Maps/Map1",
                name="Map1",
                maps=[DataTypeMapInfo(application_ref="/Acme/App/Speed", implementation_ref=mapped_onto)],
            )
        )
        model.sr_interfaces.append(_sr("/Acme/If/ISpeed", "ISpeed", type_ref="/Acme/App/Speed"))
        return model

    @pytest.mark.parametrize("mapped_onto", ["/Acme/Left/Speed", "/Acme/Right/Speed"])
    def test_the_twin_collapses_onto_the_merged_name_whichever_member_is_mapped(self, mapped_onto: str) -> None:
        decisions, data_types, registry, _skips, interfaces, _iface_skips = _map_through_interfaces(
            self._model(mapped_onto)
        )

        assert _group(decisions, "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed").verdict == MERGED
        assert registry.resolve("/Acme/Left/Speed") == "Speed"
        assert registry.resolve("/Acme/Right/Speed") == "Speed"
        assert registry.resolve("/Acme/App/Speed") == "Speed"
        assert [dt.name for dt in data_types] == ["Word", "Speed"]
        # No rename: a group reported MERGED may not also be reported renamed.
        assert registry.source_names == {}
        assert [(iface.name, iface.data_elements[0].implementation_type) for iface in interfaces] == [
            ("ISpeed", "Speed")
        ]


class TestAnExportersIndentationIsNotAPropertyTwoDeclarationsDifferIn:
    """Whitespace inside a whitespace-collapsing element is not a difference.

    Guards a defect where a pretty-printed ``CATEGORY`` made two identical base types conflict. Not bought: ``8``
    against ``08`` and an absent against a declared empty property still differ.
    """

    _SOURCE = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Acme</SHORT-NAME>\n"
        "      <AR-PACKAGES>\n"
        "        <AR-PACKAGE>\n"
        "          <SHORT-NAME>Left</SHORT-NAME>\n"
        "          <ELEMENTS>\n"
        "            <SW-BASE-TYPE>\n"
        "              <SHORT-NAME>Flag</SHORT-NAME>\n"
        "              <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
        "              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
        "              <BASE-TYPE-ENCODING>BOOLEAN</BASE-TYPE-ENCODING>\n"
        "              {LEFT}\n"
        "              <NATIVE-DECLARATION>boolean</NATIVE-DECLARATION>\n"
        "            </SW-BASE-TYPE>\n"
        "          </ELEMENTS>\n"
        "        </AR-PACKAGE>\n"
        "        <AR-PACKAGE>\n"
        "          <SHORT-NAME>Right</SHORT-NAME>\n"
        "          <ELEMENTS>\n"
        "            <SW-BASE-TYPE>\n"
        "              <SHORT-NAME>Flag</SHORT-NAME>\n"
        "              <CATEGORY>\n                FIXED_LENGTH\n              </CATEGORY>\n"
        "              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
        "              <BASE-TYPE-ENCODING>BOOLEAN</BASE-TYPE-ENCODING>\n"
        "              {RIGHT}\n"
        "              <NATIVE-DECLARATION>\n                boolean\n              </NATIVE-DECLARATION>\n"
        "            </SW-BASE-TYPE>\n"
        "          </ELEMENTS>\n"
        "        </AR-PACKAGE>\n"
        "      </AR-PACKAGES>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n"
    )

    def _decide(self, tmp_path: Path, left: str, right: str):
        source = tmp_path / "indentation.arxml"
        body = self._SOURCE.replace("{LEFT}", left).replace("{RIGHT}", right)
        source.write_text(body, encoding="utf-8", newline="\n")
        model = read_arxml(str(source))
        return decide_merges(model), model

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (
                "<MEM-ALIGNMENT>8</MEM-ALIGNMENT>",
                "<MEM-ALIGNMENT>\n                8\n              </MEM-ALIGNMENT>",
            ),
            ("<MEM-ALIGNMENT/>", "<MEM-ALIGNMENT>\n              </MEM-ALIGNMENT>"),
            (
                "<BYTE-ORDER>MOST-SIGNIFICANT-BYTE-FIRST</BYTE-ORDER>",
                "<BYTE-ORDER>\n                MOST-SIGNIFICANT-BYTE-FIRST\n              </BYTE-ORDER>",
            ),
        ],
    )
    def test_two_declarations_differing_only_in_indentation_merge(self, tmp_path: Path, left: str, right: str) -> None:
        decisions, model = self._decide(tmp_path, left, right)

        assert _group(decisions, "base_types/SW-BASE-TYPE/Flag").verdict == MERGED
        data_types, _registry, _skips = map_data_types(model, merges=decisions, elements=[])
        assert [dt.name for dt in data_types] == ["Flag"]

    def test_the_native_declaration_written_is_still_the_export_own_text(self, tmp_path: Path) -> None:
        """``NATIVE-DECLARATION`` is a schema string read RAW; only the comparison strips it."""
        _decisions, model = self._decide(tmp_path, "<MEM-ALIGNMENT/>", "<MEM-ALIGNMENT/>")

        by_path = {info.path: info for info in model.sw_base_types}
        assert by_path["/Acme/Left/Flag"].native_declaration == "boolean"
        assert by_path["/Acme/Right/Flag"].native_declaration == "\n                boolean\n              "

    def test_an_absent_property_still_differs_from_a_declared_empty_one(self, tmp_path: Path) -> None:
        """*The export does not say* is not *the export says nothing*, and a merge may assume neither."""
        decisions, _model = self._decide(tmp_path, "<MEM-ALIGNMENT/>", "")

        group = _group(decisions, "base_types/SW-BASE-TYPE/Flag")
        assert (group.verdict, group.difference) == (CONFLICTING, "MEM-ALIGNMENT")

    def test_a_lexical_difference_is_still_a_difference(self, tmp_path: Path) -> None:
        """``8`` and ``08`` are one AUTOSAR alignment and stay two texts: the deliberate fail-closed trade."""
        decisions, _model = self._decide(
            tmp_path, "<MEM-ALIGNMENT>8</MEM-ALIGNMENT>", "<MEM-ALIGNMENT>08</MEM-ALIGNMENT>"
        )

        group = _group(decisions, "base_types/SW-BASE-TYPE/Flag")
        assert (group.verdict, group.difference) == (CONFLICTING, "MEM-ALIGNMENT")


class TestANoBreakSpaceIsATextTwoDeclarationsDifferIn:
    """A no-break space is a difference: only XML white space is stripped from a compared leaf.

    A no-break space is no XML white space and no character these fields' grammars admit; ``str.strip()`` made the two
    one.
    """

    @staticmethod
    def _source(left: str, right: str) -> str:
        packages = "".join(
            f"<AR-PACKAGE><SHORT-NAME>{side}</SHORT-NAME><ELEMENTS>{element}</ELEMENTS></AR-PACKAGE>"
            for side, element in (("Left", left), ("Right", right))
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0"><AR-PACKAGES>'
            f"<AR-PACKAGE><SHORT-NAME>Acme</SHORT-NAME><AR-PACKAGES>{packages}</AR-PACKAGES></AR-PACKAGE>"
            "</AR-PACKAGES></AUTOSAR>\n"
        )

    def _decide(self, tmp_path: Path, left: str, right: str):
        source = tmp_path / "padding.arxml"
        source.write_text(self._source(left, right), encoding="utf-8", newline="\n")
        return decide_merges(read_arxml(str(source)))

    _BASE = (
        "<SW-BASE-TYPE><SHORT-NAME>Flag</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>BOOLEAN</BASE-TYPE-ENCODING>{PROPERTY}"
        "<NATIVE-DECLARATION>boolean</NATIVE-DECLARATION></SW-BASE-TYPE>"
    )
    _UNIT = "<UNIT><SHORT-NAME>Kmh</SHORT-NAME><DISPLAY-NAME>km/h</DISPLAY-NAME>{PROPERTY}</UNIT>"
    _CONSTR = (
        "<DATA-CONSTR><SHORT-NAME>Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>{PROPERTY}"
        '<PHYS-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT></PHYS-CONSTRS>'
        "</DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
    )

    CASES = [
        pytest.param(
            _BASE, "<MEM-ALIGNMENT>{TEXT}</MEM-ALIGNMENT>", "8", "base_types/SW-BASE-TYPE/Flag", id="mem-alignment"
        ),
        pytest.param(
            _BASE,
            "<BYTE-ORDER>{TEXT}</BYTE-ORDER>",
            "MOST-SIGNIFICANT-BYTE-FIRST",
            "base_types/SW-BASE-TYPE/Flag",
            id="byte-order",
        ),
        pytest.param(
            _UNIT, "<FACTOR-SI-TO-UNIT>{TEXT}</FACTOR-SI-TO-UNIT>", "3.6", "units/UNIT/Kmh", id="factor-si-to-unit"
        ),
        pytest.param(
            _CONSTR, "<CONSTR-LEVEL>{TEXT}</CONSTR-LEVEL>", "1", "data_constraints/DATA-CONSTR/Range", id="constr-level"
        ),
        pytest.param(
            _CONSTR.replace("{PROPERTY}", ""),
            "",
            "",
            "data_constraints/DATA-CONSTR/Range",
            id="phys-constrs-limit",
        ),
    ]

    @staticmethod
    def _pair(element: str, prop: str, token: str, pad: str) -> tuple[str, str]:
        if not prop:
            # The PHYS-CONSTRS limit itself is the padded text.
            return element, element.replace(">0</LOWER-LIMIT>", f">{pad}0{pad}</LOWER-LIMIT>")
        plain = element.replace("{PROPERTY}", prop.replace("{TEXT}", token))
        padded = element.replace("{PROPERTY}", prop.replace("{TEXT}", f"{pad}{token}{pad}"))
        return plain, padded

    @pytest.mark.parametrize(("element", "prop", "token", "key"), CASES)
    def test_a_no_break_space_is_a_difference(self, tmp_path: Path, element, prop, token, key) -> None:
        group = _group(self._decide(tmp_path, *self._pair(element, prop, token, "\u00a0")), key)

        assert group.verdict == CONFLICTING

    @pytest.mark.parametrize(("element", "prop", "token", "key"), CASES)
    def test_xml_whitespace_is_still_none(self, tmp_path: Path, element, prop, token, key) -> None:
        group = _group(self._decide(tmp_path, *self._pair(element, prop, token, " \t\r\n")), key)

        assert group.verdict == MERGED


class TestAnInitValuesSourceRadixIsNotPartOfTheValue:
    """``0x0A`` and ``10`` are one init value.

    ``radix_source`` reaches no written file. Guards a defect where comparing it dropped both interfaces.
    """

    @staticmethod
    def _model(left_hex: bool, *, right_value: int = 10) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Count",
                name="Count",
                category="VALUE",
                base_type_ref="/Acme/Common/Word",
                impl_type_ref=None,
                description="",
            )
        )
        for tree, hexed, value in (("Left", left_hex, 10), ("Right", False, right_value)):
            model.constant_specs.append(
                ConstantSpecInfo(
                    path=f"/Acme/{tree}/ISig_v_Init",
                    name="ISig_v_Init",
                    value=ConstantScalar(value=value, radix_source="0x0A" if hexed else None),
                    unreadable_reason=None,
                )
            )
            model.sr_interfaces.append(
                SRInterfaceInfo(
                    path=f"/Acme/{tree}/ISig",
                    name="ISig",
                    description="",
                    data_elements=[
                        SRDataElementInfo(
                            name="v",
                            type_ref="/Acme/Common/Count",
                            init_ref=f"/Acme/{tree}/ISig_v_Init",
                        )
                    ],
                )
            )
        return model

    @pytest.mark.parametrize("left_hex", [True, False])
    def test_the_spelling_alone_does_not_conflict(self, left_hex: bool) -> None:
        decisions, _dt, _registry, _skips, interfaces, _iface_skips = _map_through_interfaces(self._model(left_hex))

        assert _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISig").verdict == MERGED
        assert [(iface.name, iface.data_elements[0].init) for iface in interfaces] == [("ISig", 10)]

    def test_a_different_value_still_conflicts(self) -> None:
        """The control: a real difference in the value still conflicts."""
        decisions, _dt, _registry, _skips, _interfaces, _iface_skips = _map_through_interfaces(
            self._model(True, right_value=11)
        )

        group = _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISig")
        assert (group.verdict, group.difference) == (CONFLICTING, "DATA-ELEMENTS")


class TestAnInitValuesFormIsNotPartOfTheValueAndItsTextIs:
    """The value key signs a text leaf as text and not its form: a leaf differing only in form is one value."""

    @staticmethod
    def _model(left: ConstantScalar, right: ConstantScalar) -> ImportModel:
        model = TestAnInitValuesSourceRadixIsNotPartOfTheValue._model(False)
        for constant, value in zip(model.constant_specs, (left, right), strict=True):
            constant.value = value
        return model

    def _verdict(self, left: ConstantScalar, right: ConstantScalar) -> tuple[str, str | None]:
        decisions, _dt, _registry, _skips, _interfaces, _iface_skips = _map_through_interfaces(self._model(left, right))
        group = _group(decisions, "interfaces/SENDER-RECEIVER-INTERFACE/ISig")
        return group.verdict, group.difference

    def test_one_number_in_two_forms_is_one_value(self) -> None:
        application = ConstantScalar(value=10, form=ValueForm.APPLICATION, unit_ref="/Acme/Units/U")
        assert self._verdict(application, ConstantScalar(value=10)) == (MERGED, None)

    def test_one_text_in_two_forms_is_one_value(self) -> None:
        vt = ConstantScalar(value="ON", form=ValueForm.APPLICATION)
        text = ConstantScalar(value="ON", form=ValueForm.TEXT)
        assert self._verdict(vt, text) == (MERGED, None)

    def test_a_text_that_spells_a_number_is_not_that_number(self) -> None:
        text = ConstantScalar(value="10", form=ValueForm.APPLICATION)
        assert self._verdict(text, ConstantScalar(value=10)) == (CONFLICTING, "DATA-ELEMENTS")

    def test_two_texts_differ_as_texts(self) -> None:
        on = ConstantScalar(value="ON", form=ValueForm.APPLICATION)
        off = ConstantScalar(value="OFF", form=ValueForm.APPLICATION)
        assert self._verdict(on, off) == (CONFLICTING, "DATA-ELEMENTS")

    def test_the_key_names_the_text_as_a_text(self) -> None:
        from swcraft_tools.arxml_import.merge_verdict import _value_key

        assert _value_key(ConstantScalar(value="10", form=ValueForm.TEXT)) == ("text", "10")
        assert _value_key(ConstantScalar(value=10, form=ValueForm.APPLICATION)) == ("scalar", 10)


class TestAReferenceToAKindTheImporterNeverReadsIsComparedByItsShortName:
    """A mode, trigger, parameter or NV port onto a declared path does not make two copies conflict.

    ``map_ports`` drops such a port, so the path reaches no model field and is compared by short name. A target
    declared nowhere takes the opposite answer (the class below), so the fixture records the declaration.
    """

    @staticmethod
    def _model(*, second_tree: bool, mode_port: bool, mode_name: str = "MStates", declared: bool = True) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Acme/Common/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Common/Level",
                name="Level",
                category="VALUE",
                base_type_ref="/Acme/Common/Word",
                impl_type_ref=None,
                description="",
            )
        )
        trees = ("TreeA", "TreeB") if second_tree else ("TreeA",)
        for tree in trees:
            model.sr_interfaces.append(_sr(f"/Acme/{tree}/ILevel", "ILevel", type_ref="/Acme/Common/Level"))
            ports = [_port(f"/Acme/{tree}/LevelSwc/pLevel", "pLevel", "P-PORT-PROTOTYPE", f"/Acme/{tree}/ILevel")]
            if mode_port:
                name = mode_name if tree == "TreeA" else "MStates"
                if declared:
                    # What `read_arxml` does with a MODE-SWITCH-INTERFACE:
                    # recorded under its own path and never indexed.
                    model.skip_log.record(
                        f"/Acme/{tree}/{name}",
                        "MODE-SWITCH-INTERFACE",
                        "unrecognized element tag inside ELEMENTS",
                        category=SkipCategory.NOT_SUPPORTED,
                        effect="abort",
                    )
                ports.append(_port(f"/Acme/{tree}/LevelSwc/pMode", "pMode", "R-PORT-PROTOTYPE", f"/Acme/{tree}/{name}"))
            model.swcs.append(_swc(f"/Acme/{tree}/LevelSwc", "LevelSwc", ports))
        return model

    def test_two_copies_carrying_one_merge(self) -> None:
        result = _map_all(self._model(second_tree=True, mode_port=True))

        assert _group(result["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/LevelSwc").verdict == MERGED
        assert [entry.swc.name for entry in result["swcs"]] == ["LevelSwc"]
        # The merge buys the component, never the port.
        assert [entry.path for entry in result["swc_skips"]] == ["/Acme/TreeA/LevelSwc/pMode"]
        # The comparison's premise: neither path reaches a model field or an unresolved-reference
        # record. (Other records exist: an inline `ImportModel` has no
        # `path_index`, so lower references read as absent here.)
        assert [port.interface for entry in result["swcs"] for port in entry.swc.ports] == ["ILevel"]
        assert [record.target for record in result["unresolved"].references if record.target.endswith("MStates")] == []

    def test_two_copies_naming_two_different_short_names_still_conflict(self) -> None:
        """The fail-closed half: prefix-free is not name-free."""
        result = _map_all(self._model(second_tree=True, mode_port=True, mode_name="MOther"))

        group = _group(result["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/LevelSwc")
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")

    def test_one_copy_alone_is_unchanged(self) -> None:
        """The control: with no group the port keeps its skip."""
        result = _map_all(self._model(second_tree=False, mode_port=True))

        assert [entry.swc.name for entry in result["swcs"]] == ["LevelSwc"]
        assert [entry.path for entry in result["swc_skips"]] == ["/Acme/TreeA/LevelSwc/pMode"]

    def test_the_declaration_is_what_decides_it_on_one_fixture(self) -> None:
        """The same two paths declared by nothing are two absent targets, so the pair conflicts."""
        result = _map_all(self._model(second_tree=True, mode_port=True, declared=False))

        group = _group(result["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/LevelSwc")
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")


class TestAReferenceToAPathTheExportDeclaresNowhereIsComparedWhole:
    """A port's reference to a target declared nowhere is compared by its whole path.

    ``map_ports`` keeps such a port with the path in the model, so the reference is content.

    Does not cover the unresolved-reference report lines (``test_arxml_import_unresolved.py``) or what ``check-model``
    makes of the path.
    """

    @staticmethod
    def _swc_model(*, tree_b_target: str) -> ImportModel:
        """Two copies of one component, each with an R-port onto a missing interface (``TreeB``'s is the caller's)."""
        model = ImportModel(source_path="inline")
        for tree, target in (("TreeA", "/Acme/TreeA/Interfaces/IMissing"), ("TreeB", tree_b_target)):
            model.swcs.append(
                _swc(
                    f"/Acme/{tree}/Components/Ctrl",
                    "Ctrl",
                    [_port(f"/Acme/{tree}/Components/Ctrl/pIn", "pIn", "R-PORT-PROTOTYPE", target)],
                )
            )
        return model

    @staticmethod
    def _composition_model(*, tree_b_target: str) -> ImportModel:
        model = ImportModel(source_path="inline")
        for tree, target in (("TreeA", "/Acme/TreeA/Interfaces/IMissing"), ("TreeB", tree_b_target)):
            model.compositions.append(
                CompositionInfo(
                    path=f"/Acme/{tree}/Comps/Cluster",
                    name="Cluster",
                    description="",
                    ports=[_port(f"/Acme/{tree}/Comps/Cluster/pIn", "pIn", "R-PORT-PROTOTYPE", target)],
                    prototypes=[],
                    assembly_connectors=[],
                    delegation_connectors=[],
                )
            )
        _root_over(model, "/Acme/TreeA/Comps/Cluster", "/Acme/TreeB/Comps/Cluster")
        return model

    def test_one_copy_alone_writes_the_absent_path_into_the_model(self) -> None:
        """With no group, the port is kept with the export's own path in it."""
        model = ImportModel(source_path="inline")
        model.swcs.append(
            _swc(
                "/Acme/TreeA/Components/Ctrl",
                "Ctrl",
                [
                    _port(
                        "/Acme/TreeA/Components/Ctrl/pIn", "pIn", "R-PORT-PROTOTYPE", "/Acme/TreeA/Interfaces/IMissing"
                    )
                ],
            )
        )

        result = _map_all(model)

        assert [port.interface for entry in result["swcs"] for port in entry.swc.ports] == [
            "/Acme/TreeA/Interfaces/IMissing"
        ]
        assert [record.target for record in result["unresolved"].references] == ["/Acme/TreeA/Interfaces/IMissing"]

    def test_two_copies_naming_two_different_absent_targets_conflict(self) -> None:
        """Two absent targets differing only by the package prefix conflict; neither path is written over the other."""
        result = _map_all(self._swc_model(tree_b_target="/Acme/TreeB/Interfaces/IMissing"))

        group = _group(result["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")
        assert result["swcs"] == []
        assert [record.target for record in result["unresolved"].references] == []

    def test_a_composition_port_takes_the_same_answer(self) -> None:
        """``_port_signature`` signs a composition's boundary ports too."""
        result = _map_all(self._composition_model(tree_b_target="/Acme/TreeB/Interfaces/IMissing"))

        group = _group(result["decisions"], "compositions/COMPOSITION-SW-COMPONENT-TYPE/Cluster")
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")
        assert [composition.name for composition in result["mapped"].compositions] == ["Root"]

    def test_an_absent_target_does_not_merge_with_a_declared_unmapped_one(self) -> None:
        """A declared-but-refused target (port dropped) and an absent one (port kept) conflict."""
        model = self._swc_model(tree_b_target="/Acme/TreeB/Interfaces/IMissing")
        model.skip_log.record(
            "/Acme/TreeA/Interfaces/IMissing",
            "MODE-SWITCH-INTERFACE",
            "unrecognized element tag inside ELEMENTS",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )

        result = _map_all(model)

        group = _group(result["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")
        assert result["swcs"] == []


class TestASelectedConnectorGroupsExcludedMemberIsReported:
    """The connector member a ``--packages`` selection did not keep gets a report line.

    Its composition can still enter through the closure, but ``connectors/`` is flat, so the line is what is owed.
    """

    @staticmethod
    def _model() -> ImportModel:
        """Two differently named compositions, each with a connector ``Wire``, placed by one root.

        Same-named compositions would form a group and narrow whole; the connector key has no composition in it.
        """
        model = ImportModel(source_path="inline")
        model.sw_base_types.append(_base("/Common/Word", "Word"))
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Common/Count",
                name="Count",
                category="VALUE",
                base_type_ref="/Common/Word",
                impl_type_ref=None,
                description="",
            )
        )
        model.sr_interfaces.append(_sr("/Common/ISig", "ISig", type_ref="/Common/Count"))
        model.swcs.append(
            _swc("/Common/SwcX", "SwcX", [_port("/Common/SwcX/pOut", "pOut", "P-PORT-PROTOTYPE", "/Common/ISig")])
        )
        model.swcs.append(
            _swc("/Common/SwcY", "SwcY", [_port("/Common/SwcY/rIn", "rIn", "R-PORT-PROTOTYPE", "/Common/ISig")])
        )
        for package, composition, provider, requester in (
            ("PkgA", "CompA", "mX", "mY"),
            ("PkgB", "CompB", "mP", "mQ"),
        ):
            model.compositions.append(
                CompositionInfo(
                    path=f"/{package}/{composition}",
                    name=composition,
                    description="",
                    ports=[],
                    prototypes=[
                        ComponentPrototypeInfo(
                            path=f"/{package}/{composition}/{provider}",
                            name=provider,
                            type_ref="/Common/SwcX",
                            dest=APPLICATION_DEST,
                        ),
                        ComponentPrototypeInfo(
                            path=f"/{package}/{composition}/{requester}",
                            name=requester,
                            type_ref="/Common/SwcY",
                            dest=APPLICATION_DEST,
                        ),
                    ],
                    assembly_connectors=[
                        AssemblyConnectorInfo(
                            path=f"/{package}/{composition}/Wire",
                            name="Wire",
                            provider=ConnectorPortRef(
                                component_ref=f"/{package}/{composition}/{provider}",
                                port_ref="/Common/SwcX/pOut",
                            ),
                            requester=ConnectorPortRef(
                                component_ref=f"/{package}/{composition}/{requester}",
                                port_ref="/Common/SwcY/rIn",
                            ),
                        )
                    ],
                    delegation_connectors=[],
                )
            )
        model.compositions.append(
            CompositionInfo(
                path="/Common/Top",
                name="Top",
                description="",
                ports=[],
                prototypes=[
                    ComponentPrototypeInfo(
                        path="/Common/Top/a",
                        name="a",
                        type_ref="/PkgA/CompA",
                        dest="COMPOSITION-SW-COMPONENT-TYPE",
                    ),
                    ComponentPrototypeInfo(
                        path="/Common/Top/b",
                        name="b",
                        type_ref="/PkgB/CompB",
                        dest="COMPOSITION-SW-COMPONENT-TYPE",
                    ),
                ],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
        return model

    def test_the_excluded_member_is_skipped_with_a_reason_naming_the_kept_one(self) -> None:
        from swcraft_tools.arxml_import.merge import SELECTED, narrow_to_selection

        model = self._model()
        decisions = decide_merges(model)
        assert _group(decisions, "connectors/ASSEMBLY-SW-CONNECTOR/Wire").verdict == CONFLICTING

        narrowed = narrow_to_selection(
            decisions,
            selected=frozenset({"/PkgA/CompA", "/PkgA/CompA/Wire"}),
            reached=frozenset(),
        )
        assert _group(narrowed, "connectors/ASSEMBLY-SW-CONNECTOR/Wire").verdict == SELECTED

        forms: list = []
        data_types, registry, _dt_skips = map_data_types(model, merges=narrowed, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, merges=narrowed, elements=forms)
        swcs, swc_skips = map_swcs(
            model, interfaces, iface_skip_log=iface_skips, merges=narrowed, port_inits={}, variable_types={}
        )
        mapped, composition_skips = map_compositions(
            model, swcs, interfaces, iface_skips, registry, merges=narrowed, swc_skip_log=swc_skips
        )

        # The composition holding the excluded member is kept.
        assert sorted(composition.name for composition in mapped.compositions) == ["CompA", "CompB", "Top"]
        assert [connector.name for connector in mapped.connectors] == ["Wire"]
        excluded = [entry for entry in composition_skips if entry.path == "/PkgB/CompB/Wire"]
        assert len(excluded) == 1
        assert excluded[0].tag == "ASSEMBLY-SW-CONNECTOR"
        # The kept declaration, the contested name and this one's composition.
        assert "/PkgA/CompA/Wire" in excluded[0].reason
        assert "'Wire'" in excluded[0].reason
        assert "'CompB'" in excluded[0].reason


# An operation event's IREF and a runnable's concurrency sign


class TestAnOperationEventAndConcurrencyJoinTheComponentRow:
    """The component row signs an operation event's IREF and ``CAN-BE-INVOKED-CONCURRENTLY``.

    The model writes both, so merging a difference there would drop a server binding.
    """

    SERVER = "swc/APPLICATION-SW-COMPONENT-TYPE/Server"

    @staticmethod
    def _declaration(
        tree: str,
        *,
        port: str = "pServe",
        operation: str = "Get",
        concurrent: bool | None = False,
        unreadable: str | None = None,
        unreadable_interval: str | None = None,
        unreadable_period: str | None = None,
    ) -> SwcInfo:
        swc_path = f"/Acme/{tree}/Components/Server"
        behavior = f"{swc_path}/Server_InternalBehavior"
        return SwcInfo(
            path=swc_path,
            name="Server",
            description="",
            ports=[
                _port(f"{swc_path}/pServe", "pServe", "P-PORT-PROTOTYPE", "/Acme/Common/If/IServe"),
                _port(f"{swc_path}/pOther", "pOther", "P-PORT-PROTOTYPE", "/Acme/Common/If/IServe"),
            ],
            runnables=[
                SwcRunnableInfo(
                    path=f"{behavior}/Serve",
                    name="Serve",
                    min_start_interval=None,
                    description="",
                    can_be_invoked_concurrently=concurrent,
                    unreadable_concurrency=unreadable,
                    unreadable_min_start_interval=unreadable_interval,
                )
            ],
            events=[
                SwcEventInfo(
                    path=f"{behavior}/OIT_Serve",
                    name="OIT_Serve",
                    kind="OPERATION-INVOKED-EVENT",
                    runnable_ref=f"{behavior}/Serve",
                    period=None,
                    context_port_ref=f"{swc_path}/{port}",
                    target_operation_ref=f"/Acme/Common/If/IServe/{operation}",
                    unreadable_period=unreadable_period,
                )
            ],
        )

    @classmethod
    def _pair(
        cls,
        *,
        port: str = "pServe",
        operation: str = "Get",
        concurrent: bool | None = False,
        unreadable: str | None = None,
        unreadable_interval: str | None = None,
        unreadable_period: str | None = None,
    ) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Acme/Common/If/IServe",
                name="IServe",
                description="",
                operations=[
                    CSOperationInfo(name="Get", arguments=[]),
                    CSOperationInfo(name="Set", arguments=[]),
                ],
            )
        )
        model.swcs.append(cls._declaration("Left"))
        model.swcs.append(
            cls._declaration(
                "Right",
                port=port,
                operation=operation,
                concurrent=concurrent,
                unreadable=unreadable,
                unreadable_interval=unreadable_interval,
                unreadable_period=unreadable_period,
            )
        )
        return model

    def test_two_identical_server_declarations_still_merge(self) -> None:
        assert _group(decide_merges(self._pair()), self.SERVER).verdict == MERGED

    def test_an_event_naming_another_port_conflicts_on_the_events(self) -> None:
        group = _group(decide_merges(self._pair(port="pOther")), self.SERVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "EVENTS")

    def test_an_event_naming_another_operation_conflicts_on_the_events(self) -> None:
        group = _group(decide_merges(self._pair(operation="Set")), self.SERVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "EVENTS")

    @pytest.mark.parametrize(
        ("concurrent", "unreadable"),
        [pytest.param(True, None, id="true"), pytest.param(None, "yes", id="unreadable")],
    )
    def test_a_runnable_differing_in_concurrency_conflicts_on_the_runnables(
        self, concurrent: bool | None, unreadable: str | None
    ) -> None:
        """``true`` beside ``false``, or an unreadable value, is two components (fail-closed)."""
        group = _group(decide_merges(self._pair(concurrent=concurrent, unreadable=unreadable)), self.SERVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "RUNNABLES")

    def test_an_unreadable_minimum_start_interval_beside_an_absent_one_conflicts_on_the_runnables(self) -> None:
        """An unreadable ``MINIMUM-START-INTERVAL`` beside an absent one (written 0) is two components."""
        group = _group(decide_merges(self._pair(unreadable_interval="1_000")), self.SERVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "RUNNABLES")

    def test_an_unreadable_period_beside_an_absent_one_conflicts_on_the_events(self) -> None:
        group = _group(decide_merges(self._pair(unreadable_period="1_000")), self.SERVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "EVENTS")

    def test_an_absent_concurrency_beside_false_still_merges(self) -> None:
        """The two write the same ``concurrent: false``, so they are one value."""
        assert _group(decide_merges(self._pair(concurrent=None)), self.SERVER).verdict == MERGED


class TestAStandaloneConstraintsInfiniteLimitIsInfiniteOnBothSides:
    """``_signature`` compares a ``DATA-CONSTR`` limit with its ``INTERVAL-TYPE``, absent read as ``CLOSED``.

    An ``INFINITE`` limit bounds nothing whatever its number, so two are one limit; ``NaN`` is compared as spelled.
    """

    KEY = "data_constraints/DATA-CONSTR/Range"

    @staticmethod
    def _constraint(upper: str) -> str:
        return (
            "<DATA-CONSTR><SHORT-NAME>Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE><PHYS-CONSTRS>"
            f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>{upper}'
            "</PHYS-CONSTRS></DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
        )

    @pytest.mark.parametrize(
        ("left", "right", "verdict"),
        [
            pytest.param(
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">5</UPPER-LIMIT>',
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">9</UPPER-LIMIT>',
                MERGED,
                id="infinite-5-vs-infinite-9",
            ),
            pytest.param(
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">5</UPPER-LIMIT>',
                '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">5</UPPER-LIMIT>',
                CONFLICTING,
                id="infinite-5-vs-closed-5",
            ),
            pytest.param(
                "<UPPER-LIMIT>5</UPPER-LIMIT>",
                '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">5</UPPER-LIMIT>',
                MERGED,
                id="omitted-vs-closed",
            ),
            pytest.param(
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">5</UPPER-LIMIT>',
                "<UPPER-LIMIT>INF</UPPER-LIMIT>",
                MERGED,
                id="infinite-number-vs-infinite-spelling",
            ),
            pytest.param(
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">NaN</UPPER-LIMIT>',
                '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">9</UPPER-LIMIT>',
                CONFLICTING,
                id="infinite-nan-is-no-absent-limit",
            ),
        ],
    )
    def test_the_merge_row(self, tmp_path: Path, left: str, right: str, verdict: str) -> None:
        source = tmp_path / "constraints.arxml"
        source.write_text(
            TestANoBreakSpaceIsATextTwoDeclarationsDifferIn._source(self._constraint(left), self._constraint(right)),
            encoding="utf-8",
            newline="\n",
        )

        group = _group(decide_merges(read_arxml(str(source))), self.KEY)

        assert group.verdict == verdict


# A data-received event's IREF and its port's DEST sign


class TestADataReceivedEventJoinsTheComponentRow:
    """The component row signs a data-received event's ``DATA-IREF`` and the context port's ``DEST``.

    The identical pair under two package prefixes shows the halves are compared resolved, not by path.
    """

    RECEIVER = "swc/APPLICATION-SW-COMPONENT-TYPE/Receiver"

    @staticmethod
    def _declaration(
        tree: str,
        *,
        port: str = "rSpeed",
        element: str = "FrontLeft",
        dest: str = "R-PORT-PROTOTYPE",
        mode_ref: str | None = None,
    ) -> SwcInfo:
        swc_path = f"/Acme/{tree}/Components/Receiver"
        behavior = f"{swc_path}/Receiver_InternalBehavior"
        modes = (
            [] if mode_ref is None else [DisabledModeInfo(f"{swc_path}/rMode", "/Acme/Common/If/IMode/mode", mode_ref)]
        )
        return SwcInfo(
            path=swc_path,
            name="Receiver",
            description="",
            ports=[
                _port(f"{swc_path}/rSpeed", "rSpeed", "R-PORT-PROTOTYPE", "/Acme/Common/If/ISpeed"),
                _port(f"{swc_path}/rOther", "rOther", "R-PORT-PROTOTYPE", "/Acme/Common/If/ISpeed"),
            ],
            runnables=[
                SwcRunnableInfo(path=f"{behavior}/Filter", name="Filter", min_start_interval=None, description="")
            ],
            events=[
                SwcEventInfo(
                    path=f"{behavior}/DRT_rSpeed_FrontLeft",
                    name="DRT_rSpeed_FrontLeft",
                    kind="DATA-RECEIVED-EVENT",
                    runnable_ref=f"{behavior}/Filter",
                    period=None,
                    disabled_modes=modes,
                    context_r_port_ref=f"{swc_path}/{port}",
                    context_r_port_dest=dest,
                    target_data_element_ref=f"/Acme/Common/If/ISpeed/{element}",
                )
            ],
        )

    @classmethod
    def _pair(cls, **right: str) -> ImportModel:
        model = ImportModel(source_path="inline")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Acme/Common/If/ISpeed",
                name="ISpeed",
                description="",
                data_elements=[
                    SRDataElementInfo(name="FrontLeft", type_ref="/Acme/Common/Impl/Speed"),
                    SRDataElementInfo(name="FrontRight", type_ref="/Acme/Common/Impl/Speed"),
                ],
            )
        )
        model.swcs.append(cls._declaration("Left", mode_ref="/Acme/Common/Modes/M/OFF"))
        model.swcs.append(cls._declaration("Right", **{"mode_ref": "/Acme/Common/Modes/M/OFF", **right}))
        return model

    def test_the_infos_default_the_three_fields_so_a_hand_built_one_still_constructs(self) -> None:
        event = SwcEventInfo(path="/P/B/T", name="T", kind="TIMING-EVENT", runnable_ref="/P/B/R", period=0.01)

        assert (event.context_r_port_ref, event.context_r_port_dest, event.target_data_element_ref) == (
            None,
            None,
            None,
        )

    def test_two_identical_receiver_declarations_still_merge(self) -> None:
        assert _group(decide_merges(self._pair()), self.RECEIVER).verdict == MERGED

    @pytest.mark.parametrize(
        "right",
        [
            pytest.param({"port": "rOther"}, id="port"),
            pytest.param({"element": "FrontRight"}, id="element"),
            pytest.param({"dest": "PR-PORT-PROTOTYPE"}, id="port-dest"),
            pytest.param({"mode_ref": "/Acme/Common/Modes/M/ON"}, id="disabled-mode"),
        ],
    )
    def test_an_event_differing_in_its_trigger_conflicts_on_the_events(self, right: dict[str, str]) -> None:
        group = _group(decide_merges(self._pair(**right)), self.RECEIVER)

        assert (group.verdict, group.difference) == (CONFLICTING, "EVENTS")
