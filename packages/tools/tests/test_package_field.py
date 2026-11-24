"""Fifteen kinds carry an optional ``package``.

Every kind emitted directly into one of the three root packages (``/ComponentTypes``, ``/PortInterfaces``,
``/DataTypes``) has ``package``: the one sub-package of its root the element is emitted in, a single ``SHORT-NAME``
segment, ``None`` (the root itself) by default and declared **last** among its kind's fields. This module holds:

* **the contract and the schema**: the fifteen fields, their shape and place, ``rule:identifier.declared``'s
  ``appliesTo``, and none on the external kinds and the member kinds;
* **the YAML**: each kind loads with and without the key, absent from the file when ``None`` and last when set,
  through the derived serializer and every hand-written dump;
* **every calling mutation**: the six that rewrite a component keep its ``package``;
* **the Identifier judgement**: every illegal value (blank, a separator, a space, a hyphen, 129 characters) is
  ``rule:identifier.declared``'s finding, a refusal of ``generate arxml`` at both versions with no file written,
  and ``invalid-identifier`` for a change set;
* **the layout both importers keep** (``kept_package``): a source path exactly ``/{Root}/{package}/{name}``
  records ``package``, a whole-model build re-imports placed with every reference resolving, a foreign layout keeps
  none.

What it does **not** reach: placement in full (``test_package_placement_writer.py``) and case-only placement
(``test_package_placement_import.py``). The layout rule reads a path's shape only, so a foreign export naming a
top-level package like one of SWCraft's roots is read as SWCraft's layout.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, _write_manifest, make_empty_model_root, model_element_path, model_element_storage_name
from external_stubs_support import change, put
from model_contract_fixtures import load_contract
from operation_event_support import INTERFACE, SERVER, SERVER_FILE, load, mixed_event_model
from project_support import files_of
from reference_oracle import path_index, reference_problems
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.reconcile import ROOT_PACKAGE_OF_LAYER, home_package, kept_package
from swcraft_tools.arxml_import.report import interface_to_dict
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.component_exchange import import_ as component_import_module
from swcraft_tools.component_exchange import import_component
from swcraft_tools.editing.diagnosis import INVALID_IDENTIFIER, emitted_identifier_violations
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
    ArrayDataType,
    BaseDataType,
    Composition,
    CSInterface,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.validator import check_identifier_conformance
from swcraft_tools.writer import create_cs_interface, create_interface, generate_arxml, mutations, write_element
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import (
    composition_to_dict,
    data_type_to_dict,
    mapping_set_to_dict,
    swc_to_dict,
)
from test_component_import import _dump as placement_dump
from test_editing_validation import _staged
from test_package_placement_import import _base as placement_base
from test_package_placement_import import _export as placement_export
from test_package_placement_import import _exported as placement_exported
from test_package_placement_import import _fix_native_declarations as placement_fix_native_declarations
from test_package_placement_import import _move_to_package as placement_move_to_package
from test_package_placement_import import _read as placement_read
from test_package_placement_import import _sender_receiver as placement_sender_receiver
from test_package_placement_import import _source as placement_source
from test_package_placement_import import _target as placement_target
from test_package_placement_import import _value as placement_value

PACKAGE = "Legacy"

#: The data description layers' packages: the home a data type's or a mapping
#: set's ``package`` is a sub-package of.
BASE = "DataTypes/Base"
IMPLEMENTATION = "DataTypes/Implementation"
APPLICATION = "DataTypes/Application"
MAPPING_SETS = "DataTypes/MappingSets"

#: The fifteen kinds, with the class each is bound to, the root package it is
#: emitted into, and the smallest valid document of it -- written out rather
#: than read off the contract, since the contract is what is under test.
KINDS: dict[str, tuple[type, str, dict[str, Any]]] = {
    "kind:swc.swc": (SWComponent, "ComponentTypes", {"name": "Comp"}),
    "kind:compositions.composition": (Composition, "ComponentTypes", {"name": "Top", "title": "t"}),
    "kind:interfaces.sender_receiver": (
        SRInterface,
        "PortInterfaces",
        {"name": "ISr", "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": "uint8"}]},
    ),
    "kind:interfaces.client_server": (
        CSInterface,
        "PortInterfaces",
        {"name": "ICs", "kind": "client_server", "operations": [{"name": "Op"}]},
    ),
    "kind:base_types.base_type": (
        BaseDataType,
        BASE,
        {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
    ),
    "kind:implementation_types.value_type": (
        ValueDataType,
        IMPLEMENTATION,
        {"name": "u8", "kind": "value_type", "base": "u8_base"},
    ),
    "kind:implementation_types.reference_type": (
        ReferenceDataType,
        IMPLEMENTATION,
        {"name": "u8_ref", "kind": "reference_type", "base": "u8"},
    ),
    "kind:application_types.enum": (
        EnumDataType,
        APPLICATION,
        {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
    ),
    "kind:application_types.record": (
        RecordDataType,
        APPLICATION,
        {"name": "Pair", "kind": "record", "fields": [{"name": "x", "type": "u8"}]},
    ),
    "kind:application_types.array": (
        ArrayDataType,
        APPLICATION,
        {"name": "Row", "kind": "array", "element_type": "u8", "length": 4},
    ),
    "kind:application_types.application_type": (
        ApplicationDataType,
        APPLICATION,
        {"name": "Speed", "kind": "application_type"},
    ),
    "kind:implementation_types.implementation_record": (
        ImplementationRecordDataType,
        IMPLEMENTATION,
        {"name": "Frame", "kind": "implementation_record", "elements": [{"name": "x", "type": "u8"}]},
    ),
    "kind:implementation_types.implementation_array": (
        ImplementationArrayDataType,
        IMPLEMENTATION,
        {"name": "Buf", "kind": "implementation_array", "element_type": "u8", "length": 4, "element_name": "Item"},
    ),
    "kind:implementation_types.pointer": (
        PointerDataType,
        IMPLEMENTATION,
        {"name": "Ptr", "kind": "pointer", "target": "u8"},
    ),
    "kind:mapping_sets.mapping_set": (MappingSet, MAPPING_SETS, {"name": "Maps"}),
}

#: The four top-level kinds of the layers of the forms a type names by reference,
#: each with the layer package its ``package`` is a sub-package of. Kept apart from
#: :data:`KINDS` because their field's description is worded for a sub-package of
#: the layer's own package, and because :data:`KINDS`' minimal documents feed the
#: tests that build each kind by hand. What holds for both sets is asserted of all
#: nineteen.
ELEMENT_KINDS: dict[str, str] = {
    "kind:units.unit": "DataTypes/Units",
    "kind:compu_methods.texttable": "DataTypes/CompuMethods",
    "kind:compu_methods.linear": "DataTypes/CompuMethods",
    "kind:data_constraints.data_constr": "DataTypes/Constraints",
}

#: Every value the Identifier rule refuses, by what makes it illegal.
ILLEGAL: dict[str, str] = {
    "blank": "",
    "separator": "a/b",
    "space": "Platform Types",
    "hyphen": "platform-types",
    "too long": "P" * 129,
}


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _field_id(kind_id: str) -> str:
    return f"field:{kind_id.removeprefix('kind:')}.package"


def _dump(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# The contract and the schema
# ---------------------------------------------------------------------------


#: The fields appended after ``package``: an interface's ``is_service`` and the
#: trace link ``satisfies``. ``package`` stays last in every kind that existed
#: before them, which is what "last in its kind" asserts below.
_APPENDED_AFTER_PACKAGE = frozenset({"is_service", "satisfies"})


class TestTheContractDeclaresTheFifteenFields:
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_each_is_an_optional_mutable_string_last_in_its_kind(self, kind_id: str) -> None:
        field = [f for f in _kinds()[kind_id]["fields"] if f["name"] not in _APPENDED_AFTER_PACKAGE][-1]
        root = KINDS[kind_id][1]

        assert {key: value for key, value in field.items() if key != "description"} == {
            "id": _field_id(kind_id),
            "name": "package",
            "type": "string",
            "required": False,
            "default": None,
            "immutable": False,
        }
        description = field["description"]
        assert f"`/{root}`" in description
        assert "only in case" in description, "it says when the importer records it"
        assert "An author may set it" in description
        assert f"`/{root}/{{package}}/{{name}}`" in description, "it says the importers keep SWCraft's layout"
        assert "Identifier" in description

    def test_the_fifteen_are_every_package_field(self) -> None:
        """The fifteen and the four of :data:`ELEMENT_KINDS` are every ``package`` field."""
        declared = {field["id"] for kind in _kinds().values() for field in kind["fields"] if field["name"] == "package"}
        assert declared == {_field_id(kind_id) for kind_id in (*KINDS, *ELEMENT_KINDS)}

    @pytest.mark.parametrize("kind_id", sorted(ELEMENT_KINDS))
    def test_an_element_kinds_package_is_a_sub_package_of_its_layer_package(self, kind_id: str) -> None:
        (field,) = [field for field in _kinds()[kind_id]["fields"] if field["name"] == "package"]
        assert {key: value for key, value in field.items() if key != "description"} == {
            "id": _field_id(kind_id),
            "name": "package",
            "type": "string",
            "required": False,
            "default": None,
            "immutable": False,
        }
        assert f"`/{ELEMENT_KINDS[kind_id]}`" in field["description"]
        assert "Identifier" in field["description"]
        assert _kinds()[kind_id]["fields"][-1]["name"] == "package"

    def test_the_identifier_rule_applies_to_all_fifteen_by_addition(self) -> None:
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:identifier.declared"]
        assert {_field_id(kind_id) for kind_id in (*KINDS, *ELEMENT_KINDS)} <= set(rule["appliesTo"])
        # By addition: the six layers it named before are still named.
        assert {
            "layer:swc",
            "layer:compositions",
            "layer:interfaces",
            "layer:connectors",
            "layer:implementation_types",
            "layer:mapping_sets",
        } <= set(rule["appliesTo"])

    def test_no_rule_of_its_own(self) -> None:
        """No rule judges the field alone: only the Identifier rule and the name-space rule read it.

        `rule:name.name-space-collision` names all fifteen (a sub-package shares
        its root's name space); `rule:swc.external-with-ports` names the
        component's alone, since an external stub emits no component type.
        """
        mentioned = {
            rule["id"]
            for rule in load_contract()["rules"]
            if {_field_id(kind_id) for kind_id in (*KINDS, *ELEMENT_KINDS)} & set(rule["appliesTo"])
        }
        assert mentioned == {
            "rule:identifier.declared",
            "rule:name.name-space-collision",
            "rule:swc.external-with-ports",
        }
        stub_rule = next(rule for rule in load_contract()["rules"] if rule["id"] == "rule:swc.external-with-ports")
        assert {_field_id(kind_id) for kind_id in KINDS} & set(stub_rule["appliesTo"]) == {"field:swc.swc.package"}
        name_space = next(rule for rule in load_contract()["rules"] if rule["id"] == "rule:name.name-space-collision")
        assert {_field_id(kind_id) for kind_id in (*KINDS, *ELEMENT_KINDS)} <= set(name_space["appliesTo"])


class TestNoOtherKindHasIt:
    @pytest.mark.parametrize("cls", [ExternalInterface, ExternalDataType])
    def test_the_external_kinds_have_no_package(self, cls: type) -> None:
        assert "package" not in cls.model_fields

    def test_no_member_kind_has_one(self) -> None:
        members = [kind for kind in _kinds().values() if kind.get("memberOnly")]
        assert members
        assert not [kind["id"] for kind in members if any(field["name"] == "package" for field in kind["fields"])]

    def test_only_the_fifteen_top_level_kinds_have_one(self) -> None:
        carriers = {
            kind_id for kind_id, kind in _kinds().items() if any(field["name"] == "package" for field in kind["fields"])
        }
        assert carriers == set(KINDS) | set(ELEMENT_KINDS)

    @pytest.mark.parametrize("cls", [ExternalInterface, ExternalDataType])
    def test_a_stray_key_on_an_external_kind_is_refused(self, cls: type) -> None:
        document = {"name": "X", "kind": "external_interface" if cls is ExternalInterface else "external_type"}
        with pytest.raises(ValueError):
            cls.model_validate({**document, "external_ref": "/P/X", "package": PACKAGE})


class TestTheSchemaLoadsTheField:
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_it_defaults_to_none_and_loads_a_value(self, kind_id: str) -> None:
        cls, _root, minimal = KINDS[kind_id]
        assert cls.model_validate(minimal).package is None
        assert cls.model_validate({**minimal, "package": PACKAGE}).package == PACKAGE

    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_it_is_the_last_model_field(self, kind_id: str) -> None:
        fields = [name for name in KINDS[kind_id][0].model_fields if name not in _APPENDED_AFTER_PACKAGE]
        assert fields[-1] == "package"

    @pytest.mark.parametrize("value", sorted(ILLEGAL.values()))
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_any_string_loads(self, kind_id: str, value: str) -> None:
        """The value loads whatever it is, so a project holding a bad one stays readable."""
        cls, _root, minimal = KINDS[kind_id]
        assert cls.model_validate({**minimal, "package": value}).package == value


# ---------------------------------------------------------------------------
# The serializers
# ---------------------------------------------------------------------------


def _element(kind_id: str, package: str | None) -> Any:
    cls, _root, minimal = KINDS[kind_id]
    return cls.model_validate({**minimal, "package": package})


#: Every hand-written dump that writes one of the fifteen kinds, by the kinds it writes.
HAND_WRITTEN: dict[str, tuple[Any, list[str]]] = {
    "yaml_documents.swc_to_dict": (swc_to_dict, ["kind:swc.swc"]),
    "yaml_documents.composition_to_dict": (composition_to_dict, ["kind:compositions.composition"]),
    "yaml_documents.data_type_to_dict": (
        data_type_to_dict,
        [kind_id for kind_id in KINDS if kind_id.startswith(("kind:implementation_types.", "kind:application_types."))],
    ),
    "yaml_documents.mapping_set_to_dict": (mapping_set_to_dict, ["kind:mapping_sets.mapping_set"]),
    "report.interface_to_dict": (
        interface_to_dict,
        ["kind:interfaces.sender_receiver", "kind:interfaces.client_server"],
    ),
}

_DUMP_CASES = [(dump, kind_id) for dump, (_serializer, kinds) in HAND_WRITTEN.items() for kind_id in kinds]


class TestTheDerivedSerializer:
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_the_key_is_last_when_set_and_absent_when_none(self, kind_id: str) -> None:
        written = element_to_dict(_element(kind_id, PACKAGE))
        assert list(written)[-1] == "package"
        assert written["package"] == PACKAGE
        assert "package" not in element_to_dict(_element(kind_id, None))

    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_write_element_preserves_it(self, kind_id: str, tmp_path: Path) -> None:
        """``writer/elements.py``'s walk over ``model_fields`` writes the field with no edit."""
        element = _element(kind_id, PACKAGE)
        layer = kind_id.removeprefix("kind:").split(".")[0]
        # The layer's own directory: a base type's is `data_types/base`.
        relative = (
            "swc/group/" + f"{element.name}.yaml"
            if layer == "swc"
            else model_element_storage_name(element.name, layer=layer)
        )
        (tmp_path / relative).parent.mkdir(parents=True)
        path = Path(write_element(str(tmp_path), relative, element))
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["package"] == PACKAGE
        assert type(element).model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) == element


class TestEveryHandWrittenDump:
    @pytest.mark.parametrize(("dump", "kind_id"), _DUMP_CASES)
    def test_it_writes_the_key_last_when_set(self, dump: str, kind_id: str) -> None:
        serializer = HAND_WRITTEN[dump][0]
        written = serializer(_element(kind_id, PACKAGE))
        assert list(written)[-1] == "package"
        assert written["package"] == PACKAGE

    @pytest.mark.parametrize(("dump", "kind_id"), _DUMP_CASES)
    def test_it_omits_the_key_when_none(self, dump: str, kind_id: str) -> None:
        assert "package" not in HAND_WRITTEN[dump][0](_element(kind_id, None))

    @pytest.mark.parametrize(("dump", "kind_id"), _DUMP_CASES)
    def test_it_agrees_with_the_derived_serializer(self, dump: str, kind_id: str) -> None:
        """Key for key, so a file one path writes and the other rewrites does not move."""
        for package in (None, PACKAGE):
            element = _element(kind_id, package)
            assert _dump(HAND_WRITTEN[dump][0](element)) == _dump(element_to_dict(element)), (dump, kind_id)

    @pytest.mark.parametrize(("dump", "kind_id"), _DUMP_CASES)
    def test_the_element_reloads_equal(self, dump: str, kind_id: str) -> None:
        element = _element(kind_id, PACKAGE)
        assert type(element).model_validate(yaml.safe_load(_dump(HAND_WRITTEN[dump][0](element)))) == element


class TestTheCreatingMutationsInlineDicts:
    """``create_interface`` and ``create_cs_interface`` write a fresh interface, which has no ``package``."""

    def test_create_interface_writes_no_key(self, tmp_path: Path) -> None:
        path = create_interface(str(tmp_path), "ISpeed", [{"name": "v", "implementation_type": "uint8"}])
        assert "package" not in yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def test_create_cs_interface_writes_no_key(self, tmp_path: Path) -> None:
        path = create_cs_interface(str(tmp_path), "IService", [{"name": "Op"}])
        assert "package" not in yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The calling mutations
# ---------------------------------------------------------------------------


RAM0: dict[str, Any] = {
    "name": "RAM0",
    "kind": "memory",
    "type": "RAM",
    "size": 4096,
    "accessible": ["CORE0"],
    "threshold": 0.8,
}


def _with_other_runnable(model_root: Path) -> None:
    change(model_root, SERVER_FILE, lambda document: document["runnables"].append({"name": "Other", "events": []}))


def _with_a_budget(model_root: Path) -> None:
    put(model_root, "hardware/RAM0.yaml", RAM0)
    change(model_root, SERVER_FILE, lambda document: document.update({"memory": {"RAM0": 64}}))


#: Every mutation that rewrites an existing component through `swc_to_dict`
#: (`grep -n 'swc_to_dict(' writer/mutations.py`): ``(prepare, call)``.
REWRITING_MUTATIONS: dict[str, tuple[Any, Any]] = {
    "add_port": (
        lambda _root: None,
        lambda root: mutations.add_port(root, SERVER, "pSecond", "provide", INTERFACE),
    ),
    "add_runnable": (lambda _root: None, lambda root: mutations.add_runnable(root, SERVER, "Other")),
    "add_timing_event": (_with_other_runnable, lambda root: mutations.add_timing_event(root, SERVER, "Other", 0.02)),
    "add_init_event": (_with_other_runnable, lambda root: mutations.add_init_event(root, SERVER, "Other")),
    "set_swc_memory_budget": (_with_a_budget, lambda root: mutations.set_swc_memory_budget(root, SERVER, "RAM0", 128)),
    "unset_swc_memory_budget": (_with_a_budget, lambda root: mutations.unset_swc_memory_budget(root, SERVER, "RAM0")),
}


class TestEveryCallingMutationKeepsThePackage:
    """The writer rule: a preservation test per calling mutation."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_the_value_last(self, tmp_path: Path, mutation: str) -> None:
        model = mixed_event_model(tmp_path / "model")
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        change(model, SERVER_FILE, lambda document: document.update({"package": PACKAGE}))

        call(str(model))

        component = load(model, SERVER_FILE)
        assert component["package"] == PACKAGE
        assert [key for key in component if key not in _APPENDED_AFTER_PACKAGE][-1] == "package"


# ---------------------------------------------------------------------------
# The Identifier judgement: check-model, the writer, the editing protocol
# ---------------------------------------------------------------------------


#: One element of each kind ``examples/basic`` does not declare, typed only by
#: names it does: record and array members take the enum (a composite's member
#: may not be an implementation type), the implementation kinds take ``uint8``.
ADDED_ELEMENTS: dict[str, dict[str, Any]] = {
    "data_types/implementation/u8_ref.yaml": {"name": "u8_ref", "kind": "reference_type", "base": "uint8"},
    "data_types/application/Mode.yaml": {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
    "data_types/application/Pair.yaml": {"name": "Pair", "kind": "record", "fields": [{"name": "x", "type": "Mode"}]},
    "data_types/application/Row.yaml": {"name": "Row", "kind": "array", "element_type": "Mode", "length": 4},
    "data_types/application/Speed.yaml": {"name": "Speed", "kind": "application_type"},
    "data_types/implementation/Frame.yaml": {
        "name": "Frame",
        "kind": "implementation_record",
        "elements": [{"name": "x", "type": "uint8"}],
    },
    "data_types/implementation/Buf.yaml": {
        "name": "Buf",
        "kind": "implementation_array",
        "element_type": "uint8",
        "length": 4,
        "element_name": "Item",
    },
    "data_types/implementation/Ptr.yaml": {"name": "Ptr", "kind": "pointer", "target": "uint8"},
    "data_types/mapping_sets/Maps.yaml": {
        "name": "Maps",
        "maps": [{"application": "Speed", "implementation": "uint8"}],
    },
}

#: The file of each kind's element in the project :func:`_project` builds.
FILES: dict[str, str] = {
    "kind:swc.swc": "swc/example/ExampleSWC.yaml",
    "kind:compositions.composition": "compositions/Root.yaml",
    "kind:interfaces.sender_receiver": "interfaces/IExample.yaml",
    "kind:interfaces.client_server": "interfaces/IDiagnosticService.yaml",
    "kind:base_types.base_type": "data_types/base/uint8_base.yaml",
    "kind:implementation_types.value_type": "data_types/implementation/uint8.yaml",
    "kind:implementation_types.reference_type": "data_types/implementation/u8_ref.yaml",
    "kind:application_types.enum": "data_types/application/Mode.yaml",
    "kind:application_types.record": "data_types/application/Pair.yaml",
    "kind:application_types.array": "data_types/application/Row.yaml",
    "kind:application_types.application_type": "data_types/application/Speed.yaml",
    "kind:implementation_types.implementation_record": "data_types/implementation/Frame.yaml",
    "kind:implementation_types.implementation_array": "data_types/implementation/Buf.yaml",
    "kind:implementation_types.pointer": "data_types/implementation/Ptr.yaml",
    "kind:mapping_sets.mapping_set": "data_types/mapping_sets/Maps.yaml",
}

VERSIONS = [str(version) for version in ArxmlVersion]


def _project(tmp_path: Path) -> Path:
    """``examples/basic`` with one element of each of the fifteen kinds; returns the project root."""
    project = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    for relative, document in ADDED_ELEMENTS.items():
        put(project / "model", relative, document)
    return project


def _set_package(project: Path, kind_id: str, value: str) -> tuple[str, str]:
    """Declare *value* as the ``package`` of *kind_id*'s element; returns ``(layer, element name)``."""
    relative = FILES[kind_id]
    change(project / "model", relative, lambda document: document.update({"package": value}))
    name = yaml.safe_load((project / "model" / relative).read_text(encoding="utf-8"))["name"]
    # The layer the kind is declared in, never the first path segment: a base
    # type's file lies in `data_types/base/`, its layer is `base_types`.
    return kind_id.removeprefix("kind:").split(".")[0], name


def _package_findings(model_root: Path) -> list[Any]:
    return [
        record
        for record in check_identifier_conformance(str(model_root)).records
        if any(step.value == "package" for subject in record.subjects for step in subject.path)
    ]


class TestTheProjectIsCleanWithoutAValue:
    def test_check_model_reports_no_identifier_finding(self, tmp_path: Path) -> None:
        assert check_identifier_conformance(str(_project(tmp_path) / "model")).records == ()

    @pytest.mark.parametrize("version", VERSIONS)
    def test_generate_arxml_succeeds(self, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(_project(tmp_path) / "model"), str(output), version=version)
        assert output.is_file()

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_legal_value_places_every_element_in_its_sub_package(self, tmp_path: Path, version: str) -> None:
        """A legal ``package`` moves each of the fifteen elements from ``/{root}/{name}`` to ``/{root}/Legacy/{name}``.

        Every reference follows it."""
        project = _project(tmp_path)
        placed = {}
        for kind_id, (_cls, root, _document) in KINDS.items():
            _layer, name = _set_package(project, kind_id, PACKAGE)
            placed[root, name] = kind_id
        assert check_identifier_conformance(str(project / "model")).records == ()
        output = tmp_path / "after.arxml"
        generate_arxml(str(project / "model"), str(output), version=version)
        index = path_index(output)
        assert [
            kind_id
            for (root, name), kind_id in placed.items()
            if f"/{root}/{PACKAGE}/{name}" not in index or f"/{root}/{name}" in index
        ] == []
        assert reference_problems(output) == []


class TestCheckModelReportsAnIllegalValue:
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_on_every_kind(self, kind_id: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        layer, name = _set_package(project, kind_id, ILLEGAL["space"])

        (finding,) = _package_findings(project / "model")
        assert finding.rule == "identifier.declared"
        (subject,) = finding.subjects
        assert (subject.layer, subject.element) == (layer, name)
        assert [step.value for step in subject.path] == ["package"]
        assert f"package '{ILLEGAL['space']}'" in finding.message
        assert "is not a legal AUTOSAR Identifier" in finding.message

    @pytest.mark.parametrize("what", sorted(ILLEGAL))
    def test_every_illegal_value(self, what: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _set_package(project, "kind:swc.swc", ILLEGAL[what])

        (finding,) = check_identifier_conformance(str(project / "model")).records
        assert finding.rule == "identifier.declared"
        assert f"SWC 'ExampleSWC' package '{ILLEGAL[what]}'" in finding.message

    def test_a_package_named_like_its_root_is_legal(self, tmp_path: Path) -> None:
        """A package named like its root, ``/DataTypes/DataTypes/Flag``, breaks no identifier rule."""
        project = _project(tmp_path)
        _set_package(project, "kind:implementation_types.value_type", "DataTypes")
        assert check_identifier_conformance(str(project / "model")).records == ()


class TestTheWriterRefusesAnIllegalValue:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_on_every_kind_with_no_file_written(self, kind_id: str, version: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _set_package(project, kind_id, ILLEGAL["hyphen"])
        output_dir = tmp_path / "fresh-output"

        with pytest.raises(ValueError, match="Non-conforming SHORT-NAME") as caught:
            generate_arxml(str(project / "model"), str(output_dir / "model.arxml"), version=version)
        assert f"package: '{ILLEGAL['hyphen']}'" in str(caught.value)
        assert not output_dir.exists()

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("what", sorted(ILLEGAL))
    def test_every_illegal_value(self, what: str, version: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _set_package(project, "kind:compositions.composition", ILLEGAL[what])
        output_dir = tmp_path / "fresh-output"

        with pytest.raises(ValueError, match="Composition 'Root' package"):
            generate_arxml(str(project / "model"), str(output_dir / "model.arxml"), version=version)
        assert not output_dir.exists()


def _approve(proposal: Any) -> Any:
    from swcraft_tools.editing.authorization import ApprovalDecision

    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _update_package(project: Path, layer: str, element: str, value: str) -> dict[str, Any]:
    from swcraft_tools.editing.engine import handle_request

    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {
            "operation": "change_set",
            "baseline": token,
            "intent": "place the element in a sub-package",
            "operations": [
                {
                    "operation": "update",
                    "address": {"layer": layer, "element": element, "path": [{"field": "package"}]},
                    "value": value,
                }
            ],
        },
        project=str(project),
        review=_approve,
    )


#: One element of each layer the fifteen kinds live in, by ``(layer, element)``.
EDITED: dict[str, tuple[str, str]] = {
    "swc": ("swc", "ExampleSWC"),
    "compositions": ("compositions", "Root"),
    "interfaces": ("interfaces", "IExample"),
    "implementation_types": ("implementation_types", "uint8"),
    "mapping_sets": ("mapping_sets", "Maps"),
}


class TestTheEditingProtocol:
    @pytest.mark.parametrize("layer", sorted(EDITED))
    def test_a_legal_value_is_committed(self, layer: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        response = _update_package(project, *EDITED[layer], PACKAGE)
        assert response["response"] != "error", response
        name = EDITED[layer][1]
        (relative,) = [
            path
            for kind_id, path in FILES.items()
            if kind_id.removeprefix("kind:").split(".")[0] == layer and path.endswith(f"/{name}.yaml")
        ]
        assert yaml.safe_load((project / "model" / relative).read_text(encoding="utf-8"))["package"] == PACKAGE

    @pytest.mark.parametrize("layer", sorted(EDITED))
    def test_an_illegal_value_is_invalid_identifier_with_nothing_written(self, layer: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        before = files_of(project / "model")

        response = _update_package(project, *EDITED[layer], ILLEGAL["space"])

        assert response["response"] == "error", response
        assert response["error"] == INVALID_IDENTIFIER, response
        assert files_of(project / "model") == before

    @pytest.mark.parametrize("what", sorted(ILLEGAL))
    def test_every_illegal_value(self, what: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        before = files_of(project / "model")

        response = _update_package(project, "swc", "ExampleSWC", ILLEGAL[what])

        assert response["response"] == "error", response
        assert response["error"] == INVALID_IDENTIFIER, response
        assert files_of(project / "model") == before


class TestThePreMaterializationDiagnosis:
    """``emitted_identifier_violations`` reads the field off every staged element, as ``check-model`` does."""

    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_it_reports_the_value_where_check_model_does(self, kind_id: str, tmp_path: Path) -> None:
        project = _project(tmp_path)
        layer, name = _set_package(project, kind_id, ILLEGAL["space"])

        violations = emitted_identifier_violations(_staged(project))

        (violation,) = violations
        assert violation.error == INVALID_IDENTIFIER
        (address,) = violation.addresses
        assert (address.layer, address.element) == (layer, name)
        assert [step.to_json() for step in address.path] == [{"field": "package"}]
        assert f"package name '{ILLEGAL['space']}'" in violation.message

    def test_an_absent_value_is_no_violation(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        assert emitted_identifier_violations(_staged(project)) == ()


# ---------------------------------------------------------------------------
# `import model` keeps a sub-package in SWCraft's own layout
# ---------------------------------------------------------------------------


def _reimported(tmp_path: Path, arxml: Path, name: str = "reimported") -> tuple[Path, Any]:
    """``import model`` of *arxml* into a fresh project; returns the model root and the report."""
    project = tmp_path / name
    make_empty_model_root(project)
    report = run_import(str(arxml), str(_write_manifest(project)))
    return project / "model", report


def _write_source(tmp_path: Path, packages: dict[str, list[str]]) -> Path:
    """A hand-built AUTOSAR 4.3.0 file declaring each list of elements in the package its key names."""
    source = tmp_path / "source.arxml"
    source.write_text(placement_export(packages), encoding="utf-8", newline="\n")
    return source


def _packages_by_layer(model_root: Path) -> dict[tuple[str, str], str | None]:
    """Every emitted element's ``package``, keyed ``(layer, name)``; external stubs have none and are absent."""
    root = str(model_root)
    layers: dict[str, list[Any]] = {
        "base_types": read_layer(root, "base_types"),
        "implementation_types": read_data_types(root),
        "application_types": read_layer(root, "application_types"),
        "interfaces": read_interfaces(root),
        "swc": read_swc_model(root),
        "compositions": read_compositions(root),
        "mapping_sets": read_mapping_sets(root),
    }
    return {
        (layer, element.name): element.package
        for layer, elements in layers.items()
        for element in elements
        if hasattr(element, "package")
    }


class TestKeptPackageIsOneRuleForBothImporters:
    """``/{Root}/{package}/{name}`` names ``package``; any other path names none."""

    @pytest.mark.parametrize(
        ("root", "name", "path", "expected"),
        [
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/Legacy/u8", "Legacy"),
            (MAPPING_SETS, "Maps", "/DataTypes/MappingSets/Legacy/Maps", "Legacy"),
            ("PortInterfaces", "ISr", "/PortInterfaces/Legacy/ISr", "Legacy"),
            ("ComponentTypes", "Comp", "/ComponentTypes/Legacy/Comp", "Legacy"),
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/u8", None),
            # An earlier release's layout, /DataTypes/{package}/{name}: read by tag, its package not recovered.
            (IMPLEMENTATION, "u8", "/DataTypes/Legacy/u8", None),
            (IMPLEMENTATION, "u8", "/DataTypes/u8", None),
            (IMPLEMENTATION, "u8", "/DataTypes/Base/Legacy/u8", None),
            (IMPLEMENTATION, "u8", "/Acme/Legacy/u8", None),
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/A/B/u8", None),
            (IMPLEMENTATION, "u8", "/PortInterfaces/Legacy/u8", None),
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/Legacy/u16", None),
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/Bad-Segment/u8", None),
            (IMPLEMENTATION, "u8", "/DataTypes/Implementation/Platform Types/u8", None),
            (IMPLEMENTATION, "u8", "DataTypes/Implementation/Legacy/u8", None),
        ],
    )
    def test_only_the_swcraft_layout_names_a_package(
        self, root: str, name: str, path: str, expected: str | None
    ) -> None:
        assert kept_package(root, name, path) == expected

    def test_import_component_reads_the_same_function(self) -> None:
        assert component_import_module.kept_package is kept_package
        assert not hasattr(component_import_module, "_kept_package")

    def test_each_layer_is_read_against_the_home_the_writer_emits_it_into(self) -> None:
        assert ROOT_PACKAGE_OF_LAYER == {
            "mapping_sets": MAPPING_SETS,
            "interfaces": "PortInterfaces",
            "swc": "ComponentTypes",
            "compositions": "ComponentTypes",
        }

    @pytest.mark.parametrize("kind_id", sorted(KINDS))
    def test_a_data_types_home_is_its_kinds_layer_package(self, kind_id: str) -> None:
        """A data type's home depends on its kind, so it is asked of the element, not the layer."""
        cls, home, document = KINDS[kind_id]
        layer = kind_id.removeprefix("kind:").split(".")[0]
        assert home_package(layer, cls.model_validate(document)) == home


class TestImportModelKeepsAHandSetPackage:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_whole_model_build_reimported_keeps_every_package_and_reference(
        self, tmp_path: Path, version: str
    ) -> None:
        """Each of the fifteen kinds, placed by hand with no case-only twin, comes back placed.

        The re-import records ``package`` on every one and prints no placement
        line; its own build puts each element at ``/{root}/Legacy/{name}`` with
        every reference resolving.
        """
        project = _project(tmp_path)
        placed: dict[tuple[str, str], str] = {}
        for kind_id, (_cls, root, _document) in KINDS.items():
            layer, name = _set_package(project, kind_id, PACKAGE)
            placed[layer, name] = root
        first = tmp_path / "first.arxml"
        generate_arxml(str(project / "model"), str(first), version=version)

        model_root, report = _reimported(tmp_path, first)

        packages = _packages_by_layer(model_root)
        assert {key: packages.get(key) for key in placed} == dict.fromkeys(placed, PACKAGE)
        assert report.package_placement_lines == []
        second = tmp_path / "second.arxml"
        generate_arxml(str(model_root), str(second), version=version)
        index = path_index(second)
        assert [
            f"/{root}/{name}" for (_layer, name), root in placed.items() if f"/{root}/{PACKAGE}/{name}" not in index
        ] == []
        assert reference_problems(second) == []

    def test_a_reference_into_a_kept_package_resolves_to_the_kept_element(self, tmp_path: Path) -> None:
        model_root, report = _reimported(
            tmp_path,
            _write_source(
                tmp_path,
                {
                    "/DataTypes/Base": [placement_base("u8_base")],
                    "/DataTypes/Implementation/Legacy": [placement_value("u8", "/DataTypes/Base/u8_base")],
                    "/PortInterfaces/Ifaces": [
                        placement_sender_receiver("ISr", "v", "/DataTypes/Implementation/Legacy/u8")
                    ],
                },
            ),
        )

        packages = _packages_by_layer(model_root)
        assert packages[("implementation_types", "u8")] == "Legacy"
        assert packages[("interfaces", "ISr")] == "Ifaces"
        assert packages[("base_types", "u8_base")] is None
        (interface,) = read_interfaces(str(model_root))
        assert [element.implementation_type for element in interface.data_elements] == ["u8"]
        assert report.package_placement_lines == []


class TestAForeignLayoutKeepsNone:
    def test_no_path_outside_the_swcraft_layout_records_a_package(self, tmp_path: Path) -> None:
        """A foreign root, a nested sub-package, and the other roots' layout for a data type: none kept."""
        model_root, report = _reimported(
            tmp_path,
            _write_source(
                tmp_path,
                {
                    "/Acme/Legacy": [placement_base("a_base")],
                    "/DataTypes/Deep/Er": [placement_base("b_base")],
                    "/PortInterfaces/Legacy": [placement_base("c_base")],
                    "/ComponentTypes/Legacy": [placement_value("d_value", "/Acme/Legacy/a_base")],
                },
            ),
        )

        packages = _packages_by_layer(model_root)
        layers = {
            "a_base": "base_types",
            "b_base": "base_types",
            "c_base": "base_types",
            "d_value": "implementation_types",
        }
        assert {name: packages[(layer, name)] for name, layer in layers.items()} == dict.fromkeys(layers)
        assert report.package_placement_lines == []


class TestACaseOnlyPairStillPlaces:
    def test_two_foreign_packages_place_as_before(self, tmp_path: Path) -> None:
        model_root, report = _reimported(
            tmp_path,
            _write_source(
                tmp_path,
                {
                    "/Acme/Foundation/Types": [placement_base("SInt8")],
                    "/Acme/Platform/PlatformTypes": [placement_base("sint8")],
                },
            ),
        )

        packages = _packages_by_layer(model_root)
        assert (packages[("base_types", "SInt8")], packages[("base_types", "sint8")]) == (None, "PlatformTypes")
        assert report.package_placement_lines == [
            "data type 'sint8' (/Acme/Platform/PlatformTypes/sint8) placed in /DataTypes/Base/PlatformTypes"
        ]

    def test_a_pair_in_the_root_still_places_the_one_that_moves(self, tmp_path: Path) -> None:
        model_root, report = _reimported(
            tmp_path,
            _write_source(
                tmp_path, {"/DataTypes/Base": [placement_base("zeta")], "/Acme/Alpha": [placement_base("Zeta")]}
            ),
        )

        packages = _packages_by_layer(model_root)
        assert (packages[("base_types", "zeta")], packages[("base_types", "Zeta")]) == (None, "Alpha")
        assert report.package_placement_lines == ["data type 'Zeta' (/Acme/Alpha/Zeta) placed in /DataTypes/Base/Alpha"]

    def test_a_kept_member_leaves_the_root_so_its_twin_stays_there_with_no_line(self, tmp_path: Path) -> None:
        """The kept ``Speed`` is no root member any more: ``speed`` has no case-only twin in the root."""
        model_root, report = _reimported(
            tmp_path,
            _write_source(
                tmp_path,
                {"/Acme/Types": [placement_base("speed")], "/DataTypes/Base/Legacy": [placement_base("Speed")]},
            ),
        )

        packages = _packages_by_layer(model_root)
        assert (packages[("base_types", "speed")], packages[("base_types", "Speed")]) == (None, "Legacy")
        assert report.package_placement_lines == []


class TestImportComponentIsUnchanged:
    def test_a_created_element_in_the_layout_keeps_its_package_and_a_foreign_one_none(self, tmp_path: Path) -> None:
        source = placement_source(tmp_path, "kept")
        placement_fix_native_declarations(source)
        placement_dump(
            model_element_path(source, "Speed_T", kind="value_type"),
            {"name": "Speed_T", "kind": "value_type", "base": "f32_base", "package": PACKAGE},
        )
        kept = placement_exported(tmp_path, source, "kept")
        foreign = placement_exported(tmp_path, source, "foreign")
        placement_move_to_package(foreign, "Speed_T", f"{IMPLEMENTATION}/{PACKAGE}", "AppTypes")

        kept_root = placement_target(tmp_path, "kept_target")
        kept_report = import_component(str(kept), str(kept_root))
        foreign_root = placement_target(tmp_path, "foreign_target")
        import_component(str(foreign), str(foreign_root))

        assert placement_read(kept_root, model_element_storage_name("Speed_T", kind="value_type"))["package"] == PACKAGE
        assert kept_report.package_placement_lines == []
        assert "package" not in placement_read(foreign_root, model_element_storage_name("Speed_T", kind="value_type"))
