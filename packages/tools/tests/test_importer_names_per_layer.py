"""The importer renames no type: names are reserved per layer (contract ``8.0.0``).

The data types are three layers -- application, implementation and base types -- each with its own
name space, so the importer registers every type under its declared ``SHORT-NAME`` in its own
layer: the implementation member of a kept same-name pair takes no ``{name}_impl``, a base type
and an implementation type of one name are both imported, rule 2's ``_app`` answers a collision
inside one layer only, a type stub avoids the implementation layer's names alone, and a
reserved-name rename of one member of a same-name pair rewrites only the references that mean that
member. The collapse of a BARE same-name pair is untouched.

Every name here is synthetic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from arxml_import_shapes_support import shape_path
from swcraft_tools.arxml_import import map_data_types, map_interfaces, run_import
from swcraft_tools.arxml_import.merge import LAYER_APPLICATION_TYPES, LAYER_BASE_TYPES, LAYER_IMPLEMENTATION_TYPES
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.reconcile import reconcile_reserved_names
from swcraft_tools.arxml_import.registry import RENAME_SUFFIX, PathRegistry
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_import.type_mapping import map_mapping_sets
from swcraft_tools.arxml_reader import (
    REPORT,
    AppPrimitiveDataTypeInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImportModel,
    SkipCategory,
    StandardRule,
    SwBaseTypeInfo,
    read_arxml,
)
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    DataElement,
    DataTypeMap,
    EnumDataType,
    EnumLiteral,
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    MappingSet,
    PointerDataType,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    SRInterface,
    ValueDataType,
)

APP, IMPL, BASE = LAYER_APPLICATION_TYPES, LAYER_IMPLEMENTATION_TYPES, LAYER_BASE_TYPES


def _init_project(tmp_path: Path) -> Path:
    """Scaffold a fresh project through the real ``swcraft init`` CLI path."""
    from swcraft_tools.cli import main

    target = tmp_path / "proj"
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _import(tmp_path: Path, source: Path) -> tuple[ImportReport, Path]:
    project = _init_project(tmp_path)
    report = run_import(str(source), str(project / "swcraft.yaml"))
    return report, project / "model"


def _layer(model_root: Path, directory: str) -> dict[str, dict[str, Any]]:
    """Every element of one data description layer directory, by its declared name."""
    return {
        document["name"]: document
        for document in (
            yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted((model_root / "data_types" / directory).glob("*.yaml"), key=lambda p: p.as_posix())
        )
    }


class TestTheRegistryReservesNamesPerLayer:
    """`PathRegistry`'s per-layer name spaces, read through its public answers."""

    def test_one_name_in_two_layers_is_two_elements_and_no_rename(self) -> None:
        registry = PathRegistry()

        assert registry.register("/P/Impl/Speed", "Speed", IMPL) == "Speed"
        assert registry.register("/P/App/Speed", "Speed", APP) == "Speed"
        assert registry.register("/P/Base/Speed", "Speed", BASE) == "Speed"

        assert registry.renames == []
        assert registry.source_names == {}
        assert registry.layer_of("/P/App/Speed") == APP
        assert registry.layer_of("/P/Impl/Speed") == IMPL
        assert registry.source_paths == {
            (IMPL, "Speed"): "/P/Impl/Speed",
            (APP, "Speed"): "/P/App/Speed",
            (BASE, "Speed"): "/P/Base/Speed",
        }

    def test_rule_two_answers_a_collision_inside_one_layer(self) -> None:
        """Two application kinds of one name in two packages: the second takes ``_app``."""
        registry = PathRegistry()
        registry.register("/P/Impl/Mode", "Mode", IMPL)

        assert registry.register("/P/A/Mode", "Mode", APP) == "Mode"
        assert registry.register("/P/B/Mode", "Mode", APP) == f"Mode{RENAME_SUFFIX}"
        assert registry.claimed_names(APP) == {"Mode", f"Mode{RENAME_SUFFIX}"}
        assert registry.claimed_names(IMPL) == {"Mode"}

    def test_a_claim_reserves_the_name_in_its_layer_alone(self) -> None:
        registry = PathRegistry()
        registry.claim("Gear", APP, "/P/A/Gear", group="conflicting group")

        assert registry.register("/P/Impl/Gear", "Gear", IMPL) == "Gear"
        assert registry.register("/P/B/Gear", "Gear", APP) == f"Gear{RENAME_SUFFIX}"

    def test_a_collapsed_application_path_answers_the_implementation_layer(self) -> None:
        """The bare-pair collapse is untouched: one element, in the implementation layer."""
        registry = PathRegistry()
        registry.register("/P/Impl/Level", "Level", IMPL)

        assert registry.register_twin("/P/App/Level", "/P/Impl/Level", "Level") == "Level"
        assert registry.layer_of("/P/App/Level") == IMPL
        assert registry.claimed_names(APP) == frozenset()
        assert len(registry.reductions) == 1
        assert registry.carried_reductions({(IMPL, "Level")}) == registry.reductions
        assert registry.carried_reductions({(APP, "Level")}) == []

    def test_a_rename_re_points_the_paths_of_its_layer_alone(self) -> None:
        registry = PathRegistry()
        registry.register("/P/Impl/Mode_CompuMethod", "Mode_CompuMethod", IMPL)
        registry.register("/P/App/Mode_CompuMethod", "Mode_CompuMethod", APP)

        registry.rename("Mode_CompuMethod", "Mode_CompuMethod_app", "collides", layer=APP)

        assert registry.resolve("/P/App/Mode_CompuMethod") == "Mode_CompuMethod_app"
        assert registry.resolve("/P/Impl/Mode_CompuMethod") == "Mode_CompuMethod"
        assert registry.source_names == {(APP, "Mode_CompuMethod_app"): "Mode_CompuMethod"}


class TestAKeptPairKeepsOneNameInTwoLayers:
    """A same-name pair whose application side carries physics: two elements, one name, no rename line."""

    FIXTURE = "application_twin_with_physics.arxml"

    def test_both_members_are_written_under_the_declared_name(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, shape_path(self.FIXTURE))

        assert _layer(model_root, "application")["Speed"]["kind"] == "application_type"
        assert _layer(model_root, "implementation")["Speed"]["kind"] == "value_type"
        assert report.rename_lines == []

    def test_the_map_pairs_the_two_and_the_prototype_states_the_application_side(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, shape_path(self.FIXTURE))

        (mapping_set,) = [
            yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in (model_root / "data_types" / "mapping_sets").glob("*.yaml")
        ]
        assert {"application": "Speed", "implementation": "Speed"} in mapping_set["maps"]
        interface = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        assert interface["data_elements"][0]["application_type"] == "Speed"
        assert "implementation_type" not in interface["data_elements"][0]


class TestABasePairIsTwoElements:
    """A SW-BASE-TYPE and an IMPLEMENTATION-DATA-TYPE of one name: no refusal."""

    FIXTURE = "same_name_base_and_implementation.arxml"

    def test_both_are_imported_and_the_value_type_rests_on_the_base_type(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, shape_path(self.FIXTURE))

        assert _layer(model_root, "base")["Sample"]["kind"] == "base_type"
        value_type = _layer(model_root, "implementation")["Sample"]
        assert (value_type["kind"], value_type["base"]) == ("value_type", "Sample")
        assert not [entry for entry in report.skip_entries if "duplicate type name" in entry.reason]
        assert report.rename_lines == []


_STUB_SOURCE = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Types</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>HeldByBase</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>HeldByImpl</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>
                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Types/HeldByBase</BASE-TYPE-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>HeldByApp</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Interfaces</SHORT-NAME>
          <ELEMENTS>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IHeld</SHORT-NAME>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>own</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/HeldByImpl</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>a</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Vendor/Types/HeldByApp</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>b</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Vendor/Types/HeldByBase</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>i</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Vendor/Types/HeldByImpl</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


class TestAStubAvoidsTheImplementationLayerAlone:
    """A type stub is an element of ``implementation_types``, named ``<leaf>_ext<X>`` whatever any layer holds.

    The suffix is the stub marker, so a type of any layer sharing the leaf is left alone and
    every stub reads ``_ext0`` (no two stubs share a leaf here).
    """

    def test_a_stub_named_like_a_type_of_each_layer(self, tmp_path: Path) -> None:
        source = tmp_path / "stubs.arxml"
        source.write_text(_STUB_SOURCE, encoding="utf-8", newline="\n")

        report, model_root = _import(tmp_path, source)

        stubs = {
            document["external_ref"]: document["name"]
            for document in _layer(model_root, "implementation").values()
            if document["kind"] == "external_type"
        }
        assert stubs == {
            "/Vendor/Types/HeldByApp": "HeldByApp_ext0",
            "/Vendor/Types/HeldByBase": "HeldByBase_ext0",
            "/Vendor/Types/HeldByImpl": "HeldByImpl_ext0",
        }
        assert sorted(report.rename_lines) == [
            f"{leaf} -> {leaf}_ext0 (imported stub, namesake 0 of 1 -- the stub of /Vendor/Types/{leaf})"
            for leaf in ("HeldByApp", "HeldByBase", "HeldByImpl")
        ]


class TestAReservedNameRenameRewritesOneSide:
    """Phase B over a same-name pair: the member whose name is reserved moves, and only its references."""

    def test_the_application_member_moves_and_the_implementation_member_stays(self) -> None:
        reserved = "Mode_CompuMethod"
        data_types = [
            EnumDataType(name="Mode", kind="enum", literals=[EnumLiteral(name="Off", value=0)]),
            BaseDataType(name="Octet", kind="base_type", size=8, encoding="NONE", native_declaration="uint8"),
            ApplicationDataType(name=reserved, kind="application_type"),
            ValueDataType(name=reserved, kind="value_type", base="Octet"),
        ]
        interface = SRInterface(
            name="IMode",
            kind="sender_receiver",
            data_elements=[
                DataElement(name="physical", application_type=reserved),
                DataElement(name="stored", implementation_type=reserved),
            ],
        )
        mapping_set = MappingSet(
            name="Maps", kind="mapping_set", maps=[DataTypeMap(application=reserved, implementation=reserved)]
        )
        registry = PathRegistry()
        registry.register("/P/Base/Octet", "Octet", BASE)
        registry.register(f"/P/Impl/{reserved}", reserved, IMPL)
        registry.register("/P/App/Mode", "Mode", APP)
        registry.register(f"/P/App/{reserved}", reserved, APP)

        reconciled = reconcile_reserved_names(
            data_types=data_types,
            interfaces=[interface],
            swcs=[],
            connectors=[],
            compositions=[],
            registry=registry,
            mapping_sets=[mapping_set],
        )

        moved = f"{reserved}{RENAME_SUFFIX}"
        names = {(type(dt).__name__, dt.name) for dt in reconciled.data_types}
        assert ("ApplicationDataType", moved) in names
        assert ("ValueDataType", reserved) in names
        (written,) = reconciled.interfaces
        assert [(e.application_type, e.implementation_type) for e in written.data_elements] == [
            (moved, None),
            (None, reserved),
        ]
        (maps,) = reconciled.mapping_sets
        assert [(m.application, m.implementation) for m in maps.maps] == [(moved, reserved)]
        assert registry.resolve(f"/P/App/{reserved}") == moved
        assert registry.resolve(f"/P/Impl/{reserved}") == reserved
        assert reconciled.data_type_source_names == {(APP, moved): reserved}

    def test_every_type_reference_role_follows_only_the_member_it_means(self) -> None:
        """Each of the seven type-to-type roles of ``_renamed_data_type`` follows only the member it means.

        Only the application member is reserved (``Mode``'s compu method), so only a record field
        and an array element move; the five roles that mean the member of their own layer keep
        the name, and a name-only lookup in any of them would move it.
        """
        reserved = "Mode_CompuMethod"
        data_types = [
            EnumDataType(name="Mode", kind="enum", literals=[EnumLiteral(name="Off", value=0)]),
            BaseDataType(name="Octet", kind="base_type", size=8, encoding="NONE", native_declaration="uint8"),
            BaseDataType(name=reserved, kind="base_type", size=8, encoding="NONE", native_declaration="uint8"),
            ApplicationDataType(name=reserved, kind="application_type"),
            ValueDataType(name=reserved, kind="value_type", base="Octet"),
            ValueDataType(name="ProbeOnBase", kind="value_type", base=reserved),
            ReferenceDataType(name="ProbeRef", kind="reference_type", base=reserved),
            ImplementationRecordDataType(
                name="ProbeImplRec",
                kind="implementation_record",
                elements=[ImplementationElement(name="e", type=reserved)],
            ),
            ImplementationArrayDataType(
                name="ProbeImplArr", kind="implementation_array", element_type=reserved, length=2, element_name="el"
            ),
            PointerDataType(name="ProbePtr", kind="pointer", target=reserved),
            RecordDataType(name="ProbeRec", kind="record", fields=[RecordField(name="f", type=reserved)]),
            ArrayDataType(name="ProbeArr", kind="array", element_type=reserved, length=2),
        ]
        registry = PathRegistry()
        registry.register("/P/Base/Octet", "Octet", BASE)
        registry.register(f"/P/Base/{reserved}", reserved, BASE)
        registry.register(f"/P/Impl/{reserved}", reserved, IMPL)
        registry.register("/P/App/Mode", "Mode", APP)
        registry.register(f"/P/App/{reserved}", reserved, APP)

        reconciled = reconcile_reserved_names(
            data_types=data_types,
            interfaces=[],
            swcs=[],
            connectors=[],
            compositions=[],
            registry=registry,
            mapping_sets=[],
        )

        moved = f"{reserved}{RENAME_SUFFIX}"
        by_name = {(type(dt).__name__, dt.name): dt for dt in reconciled.data_types}
        assert ("ApplicationDataType", moved) in by_name
        assert ("ValueDataType", reserved) in by_name
        assert ("BaseDataType", reserved) in by_name
        assert by_name[("RecordDataType", "ProbeRec")].fields[0].type == moved
        assert by_name[("ArrayDataType", "ProbeArr")].element_type == moved
        assert by_name[("ValueDataType", "ProbeOnBase")].base == reserved
        assert by_name[("ReferenceDataType", "ProbeRef")].base == reserved
        assert by_name[("ImplementationRecordDataType", "ProbeImplRec")].elements[0].type == reserved
        assert by_name[("ImplementationArrayDataType", "ProbeImplArr")].element_type == reserved
        assert by_name[("PointerDataType", "ProbePtr")].target == reserved
        assert reconciled.data_type_source_names == {(APP, moved): reserved}


# The importer's layer-keyed answers, each on a synthetic export or a hand-built registry

_BASE_OCTET = (
    "<SW-BASE-TYPE><SHORT-NAME>ProbeOctet</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
    "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
    "<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION></SW-BASE-TYPE>"
)


def _value(name: str) -> str:
    """A ``VALUE`` IMPLEMENTATION-DATA-TYPE over ``/Acme/Base/ProbeOctet``."""
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Base/ProbeOctet</BASE-TYPE-REF>'
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        "</IMPLEMENTATION-DATA-TYPE>"
    )


def _primitive(name: str) -> str:
    """A bare ``VALUE`` APPLICATION-PRIMITIVE-DATA-TYPE: no unit, no compu method, no range."""
    return (
        f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "</APPLICATION-PRIMITIVE-DATA-TYPE>"
    )


def _record(name: str, field_ref: str, dest: str = "APPLICATION-PRIMITIVE-DATA-TYPE") -> str:
    return (
        f"<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY><ELEMENTS>"
        f'<APPLICATION-RECORD-ELEMENT><SHORT-NAME>f</SHORT-NAME><TYPE-TREF DEST="{dest}">{field_ref}</TYPE-TREF>'
        "</APPLICATION-RECORD-ELEMENT></ELEMENTS></APPLICATION-RECORD-DATA-TYPE>"
    )


def _array(name: str, element_ref: str) -> str:
    return (
        f"<APPLICATION-ARRAY-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>ARRAY</CATEGORY><ELEMENT>"
        f'<SHORT-NAME>e</SHORT-NAME><TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">{element_ref}</TYPE-TREF>'
        "<ARRAY-SIZE-SEMANTICS>FIXED-SIZE</ARRAY-SIZE-SEMANTICS><MAX-NUMBER-OF-ELEMENTS>2</MAX-NUMBER-OF-ELEMENTS>"
        "</ELEMENT></APPLICATION-ARRAY-DATA-TYPE>"
    )


def _map(application_ref: str, implementation_ref: str, application_dest: str) -> str:
    return (
        f'<DATA-TYPE-MAP><APPLICATION-DATA-TYPE-REF DEST="{application_dest}">{application_ref}'
        '</APPLICATION-DATA-TYPE-REF><IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
        f"{implementation_ref}</IMPLEMENTATION-DATA-TYPE-REF></DATA-TYPE-MAP>"
    )


def _export(packages: dict[str, str]) -> str:
    """One ``/Acme`` root holding a ``Base`` package with ``ProbeOctet`` and *packages* (name -> elements XML)."""
    body = "".join(
        f"<AR-PACKAGE><SHORT-NAME>{name}</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS></AR-PACKAGE>"
        for name, elements in {"Base": _BASE_OCTET, **packages}.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0"><AR-PACKAGES>'
        f"<AR-PACKAGE><SHORT-NAME>Acme</SHORT-NAME><AR-PACKAGES>{body}</AR-PACKAGES></AR-PACKAGE>"
        "</AR-PACKAGES></AUTOSAR>\n"
    )


def _import_text(tmp_path: Path, text: str) -> tuple[ImportReport, Path]:
    source = tmp_path / "probe.arxml"
    source.write_text(text, encoding="utf-8", newline="\n")
    return _import(tmp_path, source)


def _fresh(directory: Path) -> Path:
    directory.mkdir()
    return directory


def _read_model(tmp_path: Path, text: str) -> ImportModel:
    source = tmp_path / "probe.arxml"
    source.write_text(text, encoding="utf-8", newline="\n")
    return read_arxml(str(source))


class TestARecordTypedByAnImplementationTypeIsAbsentEndToEnd:
    """A record field typed by an IMPLEMENTATION-DATA-TYPE beside a same-name application type.

    The field's name ``ProbeShared`` is held by both layers, so writing it would
    bind the field to the application type. The record is refused instead
    (``[TPS_GST_00020]``), and the map naming the record as its application side
    is dropped pointing at that refusal.
    """

    SOURCE = _export(
        {
            "Impl": _value("ProbeShared") + _value("ProbeRecRaw"),
            "App": _primitive("ProbeShared")
            + _record("ProbeRec", "/Acme/Impl/ProbeShared", dest="IMPLEMENTATION-DATA-TYPE"),
            "Maps": "<DATA-TYPE-MAPPING-SET><SHORT-NAME>ProbeMaps</SHORT-NAME><DATA-TYPE-MAPS>"
            + _map("/Acme/App/ProbeRec", "/Acme/Impl/ProbeRecRaw", "APPLICATION-RECORD-DATA-TYPE")
            + "</DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>",
        }
    )

    def test_the_record_is_absent_and_its_map_dropped(self, tmp_path: Path) -> None:
        report, model_root = _import_text(tmp_path, self.SOURCE)

        application = _layer(model_root, "application")
        assert "ProbeRec" not in application
        assert application["ProbeShared"]["kind"] == "application_type"
        assert _layer(model_root, "implementation")["ProbeShared"]["kind"] == "value_type"
        record_entry = next(entry for entry in report.skip_entries if entry.path == "/Acme/App/ProbeRec")
        assert record_entry.reason == (
            "field 'f' is typed by the IMPLEMENTATION-DATA-TYPE /Acme/Impl/ProbeShared -- a record element's type "
            "is an ApplicationDataType, which an ImplementationDataType is not"
        )
        assert (record_entry.category, record_entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.REFERENCE_DESTINATION_TYPE,
        )
        (map_entry,) = [entry for entry in report.skip_entries if entry.path == "/Acme/Maps/ProbeMaps/DATA-TYPE-MAPS"]
        assert map_entry.reason == (
            "application side /Acme/App/ProbeRec was not imported as a data type (see the skipped list for its own "
            "reason)"
        )
        assert map_entry.category == SkipCategory.NOT_SUPPORTED
        (mapping_set,) = _layer(model_root, "mapping_sets").values()
        assert mapping_set["maps"] == []


class TestACompositeNamingAMergedApplicationMemberIsNotRefused:
    """A composite naming a non-primary member of a merged application group is not refused.

    The member never reaches the collapsed state: the collapse is decided per member by raw
    path (``merge.collapses_onto_twin``) and the merge verdict compares that decision
    (``merge._mapping_chain``), so

    * a composite naming **both** members keeps both from collapsing, the group merges, and
      the alias answers the application layer;
    * a composite naming **one** member makes the two decisions differ, so the group is
      *conflicting* and nothing of it is imported -- the composite keeps the alias path under
      the unresolved-reference policy, never the implementation-type refusal.
    """

    @staticmethod
    def _source(record_ref: str, array_ref: str, mapped: tuple[str, ...]) -> str:
        return _export(
            {
                "Impl": _value("ProbeLvl"),
                "AppA": _primitive("ProbeLvl"),
                "AppB": _primitive("ProbeLvl"),
                "Comp": _record("ProbeRec", record_ref) + _array("ProbeArr", array_ref),
                "Maps": "<DATA-TYPE-MAPPING-SET><SHORT-NAME>ProbeMaps</SHORT-NAME><DATA-TYPE-MAPS>"
                + "".join(
                    _map(f"/Acme/{package}/ProbeLvl", "/Acme/Impl/ProbeLvl", "APPLICATION-PRIMITIVE-DATA-TYPE")
                    for package in mapped
                )
                + "</DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>",
            }
        )

    @pytest.mark.parametrize(
        ("record_ref", "array_ref"),
        [("/Acme/AppA/ProbeLvl", "/Acme/AppB/ProbeLvl"), ("/Acme/AppB/ProbeLvl", "/Acme/AppA/ProbeLvl")],
        ids=["array-names-the-alias", "record-names-the-alias"],
    )
    def test_both_members_named_merge_and_the_alias_answers_the_application_layer(
        self, tmp_path: Path, record_ref: str, array_ref: str
    ) -> None:
        text = self._source(record_ref, array_ref, mapped=("AppA", "AppB"))
        model = _read_model(tmp_path, text)
        merges = decide_merges(model)
        _data_types, registry, type_skips = map_data_types(model, merges, elements=[])

        assert registry.merge_aliases == {"/Acme/AppB/ProbeLvl": "/Acme/AppA/ProbeLvl"}
        assert registry.layer_of("/Acme/AppB/ProbeLvl") == APP
        assert type_skips.first_at("/Acme/Comp/ProbeRec") is None
        assert type_skips.first_at("/Acme/Comp/ProbeArr") is None

        report, model_root = _import(_fresh(tmp_path / "run"), tmp_path / "probe.arxml")
        application = _layer(model_root, "application")
        assert application["ProbeLvl"]["kind"] == "application_type"
        assert application["ProbeRec"]["fields"] == [{"name": "f", "type": "ProbeLvl"}]
        assert application["ProbeArr"]["element_type"] == "ProbeLvl"
        assert not [entry for entry in report.skip_entries if "IMPLEMENTATION-DATA-TYPE" in entry.reason]

    def test_one_member_named_conflicts_and_is_never_refused_as_an_implementation_type(self, tmp_path: Path) -> None:
        text = self._source("/Acme/AppB/ProbeLvl", "/Acme/AppB/ProbeLvl", mapped=("AppA",))
        model = _read_model(tmp_path, text)
        merges = decide_merges(model)
        _data_types, registry, type_skips = map_data_types(model, merges, elements=[])

        assert registry.merge_aliases == {}
        assert registry.layer_of("/Acme/AppB/ProbeLvl") is None
        # Without the unresolved-reference policy (no `unresolved`), each composite chains to the
        # conflicting member.
        record_entry = type_skips.first_at("/Acme/Comp/ProbeRec")
        array_entry = type_skips.first_at("/Acme/Comp/ProbeArr")
        assert record_entry is not None and array_entry is not None
        assert record_entry.reason == "unresolved reference /Acme/AppB/ProbeLvl (field 'f' type was skipped)"
        assert array_entry.reason == "unresolved reference /Acme/AppB/ProbeLvl (element type was skipped)"

        report, model_root = _import(_fresh(tmp_path / "run"), tmp_path / "probe.arxml")
        assert [group.paths for group in report.conflicting_groups] == [("/Acme/AppA/ProbeLvl", "/Acme/AppB/ProbeLvl")]
        application = _layer(model_root, "application")
        assert "ProbeLvl" not in application
        assert application["ProbeRec"]["fields"] == [{"name": "f", "type": "/Acme/AppB/ProbeLvl"}]
        assert application["ProbeArr"]["element_type"] == "/Acme/AppB/ProbeLvl"
        assert not [entry for entry in report.skip_entries if "IMPLEMENTATION-DATA-TYPE" in entry.reason]


class TestTheFoldedRowIsMatchedThroughTheMergeAliases:
    """The folded-row decision reads both sides through ``merge_aliases``.

    Two packages each declare the bare pair ``ProbeLvl``; ``decide_merges`` merges the two
    application members and the two implementation members, and the pair
    collapses once. ``map_mapping_sets`` matches a row against the
    ``TwinReduction`` with both the record's paths and the row's normalised
    through the aliases, so every row naming either declaration of the pair is
    the collapse's own row: folded, never the self-map refusal. One case needs the
    record-side normalisation alone, the other the row-side one.
    """

    @pytest.mark.parametrize(
        ("rows", "recorded"),
        [
            (
                [("/Acme/AppA/ProbeLvl", "/Acme/ImplB/ProbeLvl"), ("/Acme/AppB/ProbeLvl", "/Acme/ImplB/ProbeLvl")],
                ("/Acme/AppA/ProbeLvl", "/Acme/ImplB/ProbeLvl"),
            ),
            (
                [("/Acme/AppB/ProbeLvl", "/Acme/ImplB/ProbeLvl"), ("/Acme/AppA/ProbeLvl", "/Acme/ImplA/ProbeLvl")],
                ("/Acme/AppA/ProbeLvl", "/Acme/ImplA/ProbeLvl"),
            ),
        ],
        ids=["record-names-an-alias", "row-names-the-aliases"],
    )
    def test_every_row_of_the_merged_pair_is_folded(
        self, tmp_path: Path, rows: list[tuple[str, str]], recorded: tuple[str, str]
    ) -> None:
        text = _export(
            {
                "ImplA": _value("ProbeLvl"),
                "ImplB": _value("ProbeLvl"),
                "AppA": _primitive("ProbeLvl"),
                "AppB": _primitive("ProbeLvl"),
                "Maps": "<DATA-TYPE-MAPPING-SET><SHORT-NAME>ProbeMaps</SHORT-NAME><DATA-TYPE-MAPS>"
                + "".join(
                    _map(application, implementation, "APPLICATION-PRIMITIVE-DATA-TYPE")
                    for application, implementation in rows
                )
                + "</DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>",
            }
        )
        model = _read_model(tmp_path, text)
        merges = decide_merges(model)
        data_types, registry, _type_skips = map_data_types(model, merges, elements=[])

        mapped, skips = map_mapping_sets(model, registry, data_types, merges)

        assert registry.merge_aliases == {
            "/Acme/AppB/ProbeLvl": "/Acme/AppA/ProbeLvl",
            "/Acme/ImplB/ProbeLvl": "/Acme/ImplA/ProbeLvl",
        }
        assert [(record.app_path, record.impl_path) for record in registry.reduction_records] == [recorded]
        folded = (
            "the map row of the same-name pair 'ProbeLvl' is folded into its collapse: both sides are the one "
            "implementation type 'ProbeLvl'"
        )
        assert [(entry.tag, entry.reason, entry.effect) for entry in skips] == [
            ("DATA-TYPE-MAP", folded, REPORT),
            ("DATA-TYPE-MAP", folded, REPORT),
        ]
        assert [mapping_set.maps for mapping_set in mapped.sets] == [[]]


def _mapping_model(*, same_name_in_the_right_layer: bool) -> ImportModel:
    """Two sets whose maps each point one side into the wrong layer.

    ``WrongApp`` maps ``/P/Impl/ProbeImplV`` (an implementation type) as its
    application side; ``WrongImpl`` maps ``/P/App/ProbeAppP`` (an application
    type) as its implementation side. With *same_name_in_the_right_layer* an
    application type ``ProbeImplV`` and an implementation type ``ProbeAppP`` are
    declared too, so a lookup by name alone would find a type of an admitted kind
    and write the map.
    """
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/P/Base/ProbeOctet",
            name="ProbeOctet",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    impl_names = ["ProbeImplV", "ProbeImplW"] + (["ProbeAppP"] if same_name_in_the_right_layer else [])
    for name in impl_names:
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=f"/P/Impl/{name}",
                name=name,
                category="VALUE",
                base_type_ref="/P/Base/ProbeOctet",
                impl_type_ref=None,
                description="",
            )
        )
    app_names = ["ProbeAppP", "ProbeAppQ"] + (["ProbeImplV"] if same_name_in_the_right_layer else [])
    for name in app_names:
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(path=f"/P/App/{name}", name=name, compu_method_ref=None, description="")
        )
    model.data_type_mapping_sets.extend(
        [
            DataTypeMappingSetInfo(
                path="/P/Maps/WrongApp",
                name="WrongApp",
                maps=[DataTypeMapInfo(application_ref="/P/Impl/ProbeImplV", implementation_ref="/P/Impl/ProbeImplW")],
            ),
            DataTypeMappingSetInfo(
                path="/P/Maps/WrongImpl",
                name="WrongImpl",
                maps=[DataTypeMapInfo(application_ref="/P/App/ProbeAppQ", implementation_ref="/P/App/ProbeAppP")],
            ),
        ]
    )
    return model


class TestAMapSideInTheWrongLayerIsRefused:
    """``map_mapping_sets``' side-kind refusals, asked of the path's layer.

    The lookup is ``(layer_of(path), name)`` (``type_mapping._kind_in``): a side pointing
    into the wrong layer is refused with its own line even when the right layer holds a
    type of the same name and an admitted kind.
    """

    LINES = {
        "/P/Maps/WrongApp/DATA-TYPE-MAPS": (
            "application side resolves to the data type 'ProbeImplV', which is a 'value_type'; only an application "
            "type, an enum, a record or an array may be mapped"
        ),
        "/P/Maps/WrongImpl/DATA-TYPE-MAPS": (
            "implementation side resolves to the data type 'ProbeAppP', which is a 'application_type'; only a value "
            "type, a reference type, an implementation record, an implementation array or a pointer is written on "
            "that side"
        ),
    }

    @pytest.mark.parametrize("same_name_in_the_right_layer", [False, True], ids=["alone", "same-name-in-right-layer"])
    def test_each_side_is_refused_with_its_line_and_no_entry(self, same_name_in_the_right_layer: bool) -> None:
        model = _mapping_model(same_name_in_the_right_layer=same_name_in_the_right_layer)
        data_types, registry, _type_skips = map_data_types(model, elements=[])

        mapped, skips = map_mapping_sets(model, registry, data_types)

        assert {mapping_set.name: mapping_set.maps for mapping_set in mapped.sets} == {"WrongApp": [], "WrongImpl": []}
        for path, line in self.LINES.items():
            (entry,) = [entry for entry in skips if entry.path == path]
            assert (entry.tag, entry.reason) == ("DATA-TYPE-MAP", line)
            assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)


class TestAMergedAliasAnswersItsPrimarysLayer:
    """``PathRegistry.layer_of``'s merge-alias branch."""

    def test_an_alias_answers_the_layer_of_its_primary(self) -> None:
        registry = PathRegistry()
        registry.register("/P/Impl/ProbeLvl", "ProbeLvl", IMPL)
        registry.register("/P/AppA/ProbeLvl", "ProbeLvl", APP)
        registry.merge_aliases["/P/AppB/ProbeLvl"] = "/P/AppA/ProbeLvl"
        registry.register("/P/ImplA/ProbeRaw", "ProbeRaw", IMPL)
        registry.merge_aliases["/P/ImplB/ProbeRaw"] = "/P/ImplA/ProbeRaw"

        assert registry.layer_of("/P/AppB/ProbeLvl") == registry.layer_of("/P/AppA/ProbeLvl") == APP
        assert registry.layer_of("/P/ImplB/ProbeRaw") == registry.layer_of("/P/ImplA/ProbeRaw") == IMPL
        assert registry.layer_of("/P/Nowhere/ProbeLvl") is None

    @pytest.mark.parametrize("named", ["AppA", "AppB"], ids=["primary", "alias"])
    def test_a_data_element_naming_either_member_states_the_application_side(self, tmp_path: Path, named: str) -> None:
        """An implementation type ``ProbeLvl`` is declared too, so the name alone cannot say the layer."""
        model = _read_model(
            tmp_path,
            _export(
                {
                    "Impl": _value("ProbeLvl"),
                    "AppA": _primitive("ProbeLvl"),
                    "AppB": _primitive("ProbeLvl"),
                    "If": "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>IProbe</SHORT-NAME><DATA-ELEMENTS>"
                    "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>lvl</SHORT-NAME>"
                    f'<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/{named}/ProbeLvl</TYPE-TREF>'
                    "</VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>",
                }
            ),
        )
        merges = decide_merges(model)
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, merges, elements=forms)

        (interface,), interface_skips = map_interfaces(model, registry, data_types, merges=merges, elements=forms)

        assert registry.merge_aliases == {"/Acme/AppB/ProbeLvl": "/Acme/AppA/ProbeLvl"}
        assert [(e.application_type, e.implementation_type) for e in interface.data_elements] == [("ProbeLvl", None)]
        assert list(interface_skips) == []


class TestACaseOnlyPairAcrossTwoLayersTakesNoCaseVariant:
    """Two layers are two name spaces, so a case-only pair across them is no collision.

    The control is the same pair inside one layer, which ``place_final_packages``
    does place -- so the assertion of *no* placement can fail.
    """

    def test_an_application_and_an_implementation_type_differing_by_case(self, tmp_path: Path) -> None:
        report, model_root = _import_text(
            tmp_path, _export({"Impl": _value("PROBECASE"), "App": _primitive("ProbeCase")})
        )

        application = _layer(model_root, "application")["ProbeCase"]
        implementation = _layer(model_root, "implementation")["PROBECASE"]
        assert "package" not in application
        assert "package" not in implementation
        assert report.package_placement_lines == []
        assert report.file_suffix_lines == []

    def test_the_control_one_layer_places_one_member(self, tmp_path: Path) -> None:
        report, model_root = _import_text(
            tmp_path, _export({"App": _primitive("ProbeCase"), "AppX": _primitive("PROBECASE")})
        )

        packages = {name: document.get("package") for name, document in _layer(model_root, "application").items()}
        assert sorted(packages) == ["PROBECASE", "ProbeCase"]
        assert [package for package in packages.values() if package is not None] != []
        assert report.package_placement_lines != []
