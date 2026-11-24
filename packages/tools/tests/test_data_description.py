"""The resolved view of a model's data description (`swcraft_tools.data_description`).

The view resolves an element by a **name the caller read** and reads no field of the element it
answers: one accessor per layer, each taking only a name -- ``application(name)`` in ``application_types``,
``implementation(name)`` in ``implementation_types``, ``base(name)`` in ``base_types`` -- no accessor that takes
a data element or any other argument, and no table of which kinds a role admits (those stay in the ``validator/``
package).

The module is imported inside each test body, so a checkout without it fails as a test failure and
not at collection.

**Not reached:** which accessor a consumer asks for which field; a consumer asking the wrong one
resolves nothing, which the consumers' own suites observe (``test_application_types_layer.py`` among them).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from conftest import model_element_path
from emission_scan import constructs
from swcraft_tools.reader import read_data_types, read_layer
from swcraft_tools.schema import BaseDataType, EnumDataType, RecordDataType, ValueDataType

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools"
MODULE_SOURCE = PACKAGE_ROOT / "data_description.py"

#: One element of every family the three accessors name, with plainly synthetic names.
_BASE = {"name": "probe_u8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"}
_VALUE = {"name": "ProbeValue", "kind": "value_type", "base": "probe_u8"}
_ENUM = {"name": "ProbeMode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}]}
_RECORD = {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "mode", "type": "ProbeMode"}]}


def _write(model_root: Path, file_stem: str, document: dict[str, Any]) -> None:
    path = model_element_path(model_root, file_stem, kind=document["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    for document in (_BASE, _VALUE, _ENUM, _RECORD):
        _write(tmp_path, str(document["name"]), document)
    return tmp_path


# The accessors


class TestTheAccessors:
    def test_each_accessor_answers_the_declared_element_of_a_name(self, model_root: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))

        assert isinstance(view.base("probe_u8"), BaseDataType)
        assert isinstance(view.implementation("ProbeValue"), ValueDataType)
        assert isinstance(view.application("ProbeMode"), EnumDataType)
        assert isinstance(view.application("ProbeRecord"), RecordDataType)

    @pytest.mark.parametrize("name", ["ProbeMode", "ProbeRecord"])
    def test_an_application_type_is_answered_by_application_alone(self, model_root: Path, name: str) -> None:
        """An application type is an element of ``application_types``, answered by ``application`` alone."""
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))

        assert view.application(name) is not None
        assert view.application(name).name == name
        assert view.implementation(name) is None
        assert view.base(name) is None

    def test_an_implementation_type_is_answered_by_implementation_alone(self, model_root: Path) -> None:
        """No accessor filters by kind: the view holds no admitted-kind table; it resolves by layer."""
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))

        assert isinstance(view.implementation("ProbeValue"), ValueDataType)
        assert view.application("ProbeValue") is None
        assert view.base("ProbeValue") is None

    def test_a_base_type_is_answered_by_base_alone(self, model_root: Path) -> None:
        """A base type is an element of ``base_types``, which only ``base`` resolves in."""
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))

        assert isinstance(view.base("probe_u8"), BaseDataType)
        assert view.application("probe_u8") is None
        assert view.implementation("probe_u8") is None

    @pytest.mark.parametrize("name", ["Absent", "probe_U8", "", None])
    def test_a_name_no_element_declares_is_none_in_every_accessor(self, model_root: Path, name: str | None) -> None:
        """An absent name answers nothing and judges nothing; a name differing only in case is another name."""
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))

        assert (view.application(name), view.implementation(name), view.base(name)) == (None, None, None)

    def test_an_empty_or_absent_layer_is_an_empty_view(self, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(tmp_path))

        assert view.data_types == ()
        assert view.application("probe_u8") is None


# The elements, and a name declared twice


class TestTheElements:
    def test_data_types_is_both_layers_merged_by_name_each_in_its_readers_order(self, model_root: Path) -> None:
        """``data_types`` is the three layers merged, sorted by name."""
        from swcraft_tools.data_description import read_data_description

        view = read_data_description(str(model_root))
        bases = read_layer(str(model_root), "base_types")
        applications = read_layer(str(model_root), "application_types")
        implementations = read_data_types(str(model_root))

        assert list(view.data_types) == sorted(implementations + bases + applications, key=lambda dt: dt.name)
        assert [dt for dt in view.data_types if isinstance(dt, BaseDataType)] == bases
        assert [dt.name for dt in applications] == ["ProbeMode", "ProbeRecord"]
        assert [dt.name for dt in implementations] == ["ProbeValue"]

    def test_a_name_declared_twice_in_one_layer_resolves_to_its_first_declaration_and_both_are_elements(
        self,
    ) -> None:
        """``data-type.duplicate-name`` owns the repeat; a check judges one element, the first."""
        from swcraft_tools.data_description import DataDescription

        first = BaseDataType.model_validate(_BASE)
        second = BaseDataType.model_validate({**_BASE, "size": 16})
        view = DataDescription([first, second])

        assert view.data_types == (first, second)
        assert view.base("probe_u8") is first
        assert view.application("probe_u8") is None
        assert view.implementation("probe_u8") is None

    def test_one_name_in_two_layers_is_two_elements_each_answered_by_its_layer(self) -> None:
        """A base type and a value type of one name: ``base`` answers the one, ``implementation`` the other."""
        from swcraft_tools.data_description import DataDescription

        base = BaseDataType.model_validate({**_BASE, "name": "ProbeWord"})
        value = ValueDataType.model_validate({**_VALUE, "name": "ProbeWord", "base": "ProbeWord"})
        for view in (DataDescription([base, value]), DataDescription.last_declared([value, base])):
            assert view.base("ProbeWord") is base
            assert view.implementation("ProbeWord") is value
            assert view.application("ProbeWord") is None

    def test_an_application_type_and_an_implementation_type_of_one_name_are_each_answered_by_its_layer(
        self,
    ) -> None:
        """Identity is (layer, name): an enumeration and a value type of one name are two elements."""
        from swcraft_tools.data_description import DataDescription

        enum = EnumDataType.model_validate({**_ENUM, "name": "ProbeShared"})
        value = ValueDataType.model_validate({**_VALUE, "name": "ProbeShared"})
        for view in (
            DataDescription([enum, value]),
            DataDescription([value, enum]),
            DataDescription.last_declared([enum, value]),
            DataDescription.last_declared([value, enum]),
        ):
            assert view.application("ProbeShared") is enum
            assert view.implementation("ProbeShared") is value
            assert view.base("ProbeShared") is None
            assert set(map(id, view.data_types)) == {id(enum), id(value)}

    def test_an_application_type_and_an_implementation_type_of_one_name_load_side_by_side(
        self, model_root: Path
    ) -> None:
        """The same pair read from a model root: each file in its layer's directory, each answered by its accessor."""
        from swcraft_tools.data_description import read_data_description

        _write(model_root, "ProbeShared", {**_ENUM, "name": "ProbeShared"})
        _write(model_root, "ProbeShared", {**_VALUE, "name": "ProbeShared"})

        view = read_data_description(str(model_root))

        assert (model_root / "data_types" / "application" / "ProbeShared.yaml").is_file()
        assert (model_root / "data_types" / "implementation" / "ProbeShared.yaml").is_file()
        assert isinstance(view.application("ProbeShared"), EnumDataType)
        assert isinstance(view.implementation("ProbeShared"), ValueDataType)
        assert [dt.name for dt in view.data_types].count("ProbeShared") == 2

    def test_a_name_of_the_other_layer_is_not_found(self) -> None:
        """A name absent from the asked layer and present in the other answers ``None``, never the other's element."""
        from swcraft_tools.data_description import DataDescription

        enum = EnumDataType.model_validate(_ENUM)
        value = ValueDataType.model_validate(_VALUE)
        for view in (DataDescription([enum, value]), DataDescription.last_declared([enum, value])):
            assert view.implementation("ProbeMode") is None
            assert view.application("ProbeValue") is None
            assert view.application("ProbeMode") is enum
            assert view.implementation("ProbeValue") is value

    def test_last_declared_resolves_a_name_repeated_in_one_layer_to_its_last_declaration(self) -> None:
        """The rule of a caller that indexed the layers with a dict of its own, kept per layer."""
        from swcraft_tools.data_description import DataDescription

        first = BaseDataType.model_validate(_BASE)
        second = BaseDataType.model_validate({**_BASE, "size": 16})
        view = DataDescription.last_declared([first, second])

        assert view.data_types == (first, second)
        assert view.base("probe_u8") is second

    def test_last_declared_answers_each_element_from_the_accessor_of_its_layer_alone(self) -> None:
        """A caller's own ``{name: element}`` index handed over as its values: each layer answers its own."""
        from swcraft_tools.data_description import DataDescription

        base = BaseDataType.model_validate(_BASE)
        value = ValueDataType.model_validate(_VALUE)
        enum = EnumDataType.model_validate(_ENUM)
        index = {"probe_u8": base, "ProbeValue": value, "ProbeMode": enum}
        view = DataDescription.last_declared(index.values())

        assert (view.application("ProbeMode"), view.implementation("ProbeMode"), view.base("ProbeMode")) == (
            enum,
            None,
            None,
        )
        assert (view.application("ProbeValue"), view.implementation("ProbeValue"), view.base("ProbeValue")) == (
            None,
            value,
            None,
        )
        assert (view.application("probe_u8"), view.implementation("probe_u8"), view.base("probe_u8")) == (
            None,
            None,
            base,
        )
        assert view.data_types == (base, value, enum)


# What the module is: what it imports, what it reads, what it offers


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_SOURCE.read_text(encoding="utf-8"), filename=str(MODULE_SOURCE))


class TestWhatTheModuleIs:
    def test_it_imports_schema_and_reader_and_nothing_else_of_the_package(self) -> None:
        imported = {
            node.module
            for node in ast.walk(_module_tree())
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools")
        } | {
            alias.name
            for node in ast.walk(_module_tree())
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("swcraft_tools")
        }

        assert imported == {"swcraft_tools.schema", "swcraft_tools.reader"}

    def test_the_only_field_it_reads_off_an_element_is_its_name(self) -> None:
        """Every attribute read on something other than ``self``/``cls``/``view`` is ``.name`` or the index's own."""
        own = {"self", "cls", "view"}
        read = {
            node.attr
            for node in ast.walk(_module_tree())
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and not (isinstance(node.value, ast.Name) and node.value.id in own)
        }

        # `get`, `setdefault`, `file` and `in_order` are the per-layer index's own; `append` a local list's.
        assert read - {"get", "setdefault", "append", "file", "in_order"} == {"name"}

    def test_each_accessor_takes_a_name_and_nothing_else(self) -> None:
        from swcraft_tools.data_description import DataDescription

        for accessor in (
            DataDescription.application,
            DataDescription.implementation,
            DataDescription.base,
            DataDescription.unit,
            DataDescription.compu_method,
            DataDescription.data_constr,
        ):
            assert list(inspect.signature(accessor).parameters) == ["self", "name"], accessor.__name__

    def test_its_public_surface_is_the_three_accessors_the_elements_and_the_two_constructors(self) -> None:
        """No accessor that takes a data element or an argument, and no admitted-kind table."""
        from swcraft_tools import data_description
        from swcraft_tools.data_description import DataDescription

        public = {name for name in vars(DataDescription) if not name.startswith("_")}
        module_public = {name for name in vars(data_description) if not name.startswith("_")} - {
            "annotations",
            "Iterable",
            "Mapping",
            # The typing names of the per-layer index.
            "Generic",
            "Protocol",
            "TypeVar",
            "DataType",
            "BaseDataType",
            "ApplicationDataType",
            "ArrayDataType",
            "EnumDataType",
            "RecordDataType",
            "merge",
            "read_data_types",
            "read_layer",
            # Imported so that an element of no data type class is refused by the view.
            "ExternalDataType",
            "ImplementationArrayDataType",
            "ImplementationRecordDataType",
            "PointerDataType",
            "ReferenceDataType",
            "ValueDataType",
            # Imported so that the view files each element in its layer.
            "CompuMethod",
            "DataConstr",
            "Linear",
            "TextTable",
            "Unit",
            "DataDescriptionElement",
        }

        assert public == {
            "application",
            "implementation",
            "base",
            "data_types",
            "last_declared",
            "unit",
            "compu_method",
            "data_constr",
            "units",
            "compu_methods",
            "data_constraints",
            "elements",
        }
        assert module_public == {"DataDescription", "read_data_description", "APPLICATION_DATA_TYPE_CLASSES"}

    def test_the_application_classes_are_the_four_top_level_kinds_of_the_application_types_layer(self) -> None:
        """``APPLICATION_DATA_TYPE_CLASSES`` is the classes of the contract's ``application_types`` kinds."""
        from swcraft_tools.data_description import APPLICATION_DATA_TYPE_CLASSES
        from swcraft_tools.model_registry import production_registry

        layer = production_registry().layer("application_types")

        assert set(APPLICATION_DATA_TYPE_CLASSES) == {kind.model for kind in layer.top_level_kinds}
        assert len(APPLICATION_DATA_TYPE_CLASSES) == 4


# The validator on the view


class TestTheValidatorReadsTheDataDescriptionThroughTheView:
    def test_the_validator_names_no_reader_wrapper_of_the_data_types_layer(self) -> None:
        """Every type lookup of the validator package goes through the view; the wrappers are not imported."""
        body = [node for path in validator_package.paths() for node in ast.parse(path.read_text(encoding="utf-8")).body]
        tree = ast.Module(body=body, type_ignores=[])
        imported = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.reader"
            for alias in node.names
        }
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert {"read_data_types", "find_data_type_file"}.isdisjoint(imported | called)
        assert "read_data_description" in called

    def test_a_base_type_and_an_enumeration_of_one_name_are_two_layers_elements(self, tmp_path: Path) -> None:
        """A base type and an enumeration of one name are two layers' elements, not a repeat: nothing is reported."""
        from swcraft_tools.validator import check_data_type_references

        _write(tmp_path, "probe_u8", _BASE)
        _write(tmp_path, "probe_u8", {**_ENUM, "name": "probe_u8"})
        interface = {
            "name": "IProbe",
            "kind": "sender_receiver",
            "data_elements": [{"name": "level", "application_type": "probe_u8"}],
        }
        path = tmp_path / "interfaces" / "IProbe.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(interface, sort_keys=False), encoding="utf-8", newline="\n")

        rules = [record.rule for record in check_data_type_references(str(tmp_path)).records]

        assert rules == []

    @pytest.mark.parametrize(
        ("first_kind", "expected"),
        [("enum", []), ("record", ["interface.init-shape-mismatch"])],
        ids=["the-enumeration-first", "the-record-first"],
    )
    def test_a_check_over_a_name_repeated_in_one_layer_judges_its_first_declaration(
        self, tmp_path: Path, first_kind: str, expected: list[str]
    ) -> None:
        """A validator check judges the view's first declaration of a name repeated in one layer.

        ``data-type.duplicate-name`` owns the repeat itself; this pins only which of the two the role
        check judges, and is moot for a model that passes ``check-model``.
        """
        from swcraft_tools.validator import check_init_conformance

        enum = {"name": "ProbeTwice", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]}
        record = {"name": "ProbeTwice", "kind": "record", "fields": [{"name": "mode", "type": "ProbeMode"}]}
        first, second = (enum, record) if first_kind == "enum" else (record, enum)
        _write(tmp_path, "ProbeMode", _ENUM)
        _write(tmp_path, "a_first", first)
        _write(tmp_path, "b_second", second)
        interface = {
            "name": "IProbe",
            "kind": "sender_receiver",
            "data_elements": [{"name": "level", "application_type": "ProbeTwice", "init": "OFF"}],
        }
        path = tmp_path / "interfaces" / "IProbe.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(interface, sort_keys=False), encoding="utf-8", newline="\n")

        rules = [record.rule for record in check_init_conformance(str(tmp_path)).records]

        assert rules == expected


# The writer, the model writers, the importers and the component exchange on the view

#: The consumers on the view, each with the reader wrapper it still calls. ``find_data_type_file``
#: answers a FILE PATH (``create_data_type``'s duplicate message names it), which the view does not
#: hold, so it stays a storage lookup.
_CONSUMERS_ON_THE_VIEW: dict[str, frozenset[str]] = {
    "writer/arxml_generation.py": frozenset(),
    "writer/mutations.py": frozenset({"find_data_type_file"}),
    "arxml_import/orchestrator.py": frozenset(),
    "arxml_import/pipeline.py": frozenset(),
    "arxml_import/type_mapping.py": frozenset(),
    "arxml_import/type_mappers.py": frozenset(),
    "component_exchange/closure.py": frozenset(),
    "component_exchange/import_.py": frozenset(),
    "component_exchange/export.py": frozenset(),
}


def _named_and_called(source: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.reader"
        for alias in node.names
    }
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return imported, called


def _callers_of(source: Path, callee: str) -> set[str]:
    """Return the construct of *source* each call of *callee*, by bare name or as an attribute, is evaluated in.

    ``""`` stands for module level (:func:`emission_scan.constructs`).
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return {
        construct
        for node, construct in constructs(tree).items()
        if isinstance(node, ast.Call) and callee in (getattr(node.func, "id", None), getattr(node.func, "attr", None))
    }


class TestTheOtherConsumersReadTheDataDescriptionThroughTheView:
    @pytest.mark.parametrize("module", sorted(_CONSUMERS_ON_THE_VIEW))
    def test_no_consumer_reads_the_layer_through_a_reader_wrapper(self, module: str) -> None:
        """``read_data_types`` is called by no consumer but the view itself; ``find_data_type_file`` by one, named.

        What it does not reach: a wrapper reached under another name, or through
        ``reader.`` as an attribute — none of these modules imports the module whole.
        """
        imported, called = _named_and_called(PACKAGE_ROOT / module)

        assert {"read_data_types", "find_data_type_file"} & (imported | called) == _CONSUMERS_ON_THE_VIEW[module]

    def test_the_reader_wrapper_of_the_layer_is_called_by_the_view_alone(self) -> None:
        """Across the package, every ``read_data_types`` call is made by ``read_data_description``, however many."""
        callers = {
            f"{path.relative_to(PACKAGE_ROOT).as_posix()}::{qualname}"
            for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix())
            for qualname in _callers_of(path, "read_data_types")
        }

        assert callers == {"data_description.py::read_data_description"}

    @pytest.mark.parametrize(
        ("function", "parameter"),
        [
            ("interface_compatibility", "data_types"),
            ("port_init_records", "type_map"),
            ("mapped_pointer", "data_types"),
            ("composite_map_difference", "data_types"),
            ("implementation_end", "data_types"),
            ("reference_chain_end", "type_map"),
            ("external_type_chain", "type_map"),
            ("resolve_type", "data_types"),
        ],
    )
    def test_the_shared_validator_functions_take_the_view_alone(self, function: str, parameter: str) -> None:
        """Every caller holds a view now, so the functions the writer and the importers share take nothing else."""
        defining = validator_package.defining_module(function)
        annotation = inspect.signature(getattr(defining, function)).parameters[parameter].annotation

        assert annotation == "DataDescription"
        assert not validator_package.defines("_as_data_description")
        assert not validator_package.binding_modules("_as_data_description")


# Which layer a class answers, and a class of no layer


def _data_type_kinds() -> list[tuple[str, type]]:
    """``(layer, class)`` of every top-level kind whose class is in the ``DataType`` union, per the contract."""
    from typing import get_args

    from swcraft_tools.model_registry import production_registry
    from swcraft_tools.schema import DataType

    members = set(get_args(get_args(DataType)[0]))
    registry = production_registry()
    return [
        (name, kind.model)
        for name in registry.layer_names
        for kind in registry.layer(name).top_level_kinds
        if kind.model in members
    ]


class TestEveryDataTypeClassAnswersTheLayerOfItsKind:
    """``validator.data_type_layer`` and the view's own classification agree with the contract, and refuse a stranger.

    Guards a defect: both filed every class that is neither a base nor an application type in
    ``implementation_types`` without a word.
    """

    def test_every_member_of_the_union_is_one_layers_kind(self) -> None:
        from typing import get_args

        from swcraft_tools.schema import DataType

        kinds = _data_type_kinds()

        assert sorted(cls.__name__ for _, cls in kinds) == sorted(
            cls.__name__ for cls in get_args(get_args(DataType)[0])
        )
        assert len({layer for layer, _ in kinds}) == 3

    @pytest.mark.parametrize(("layer", "cls"), _data_type_kinds(), ids=lambda value: getattr(value, "__name__", value))
    def test_data_type_layer_answers_the_layer_whose_kind_the_class_is(self, layer: str, cls: type) -> None:
        from swcraft_tools.validator import data_type_layer

        assert data_type_layer(cls.model_construct(name="ProbeAny")) == layer

    @pytest.mark.parametrize(("layer", "cls"), _data_type_kinds(), ids=lambda value: getattr(value, "__name__", value))
    def test_the_view_resolves_the_class_in_the_accessor_of_its_layer(self, layer: str, cls: type) -> None:
        from swcraft_tools.data_description import DataDescription
        from swcraft_tools.validator import data_type_layer

        element = cls.model_construct(name="ProbeAny")
        view = DataDescription([element])
        answered = {
            accessor: getattr(view, accessor)("ProbeAny") is element
            for accessor in ("application", "implementation", "base")
        }

        assert sum(answered.values()) == 1
        assert data_type_layer(element).startswith(next(accessor for accessor, hit in answered.items() if hit))

    def test_data_type_layer_refuses_an_element_of_no_data_description_class(self) -> None:
        from swcraft_tools.schema import SWComponent
        from swcraft_tools.validator import data_type_layer

        with pytest.raises(ValueError, match="^SWComponent is no data description element$"):
            data_type_layer(SWComponent.model_construct(name="ProbeAny"))

    def test_the_view_refuses_an_element_of_no_data_description_class(self) -> None:
        from swcraft_tools.data_description import DataDescription
        from swcraft_tools.schema import SWComponent

        with pytest.raises(ValueError, match="is no data description class$"):
            DataDescription([SWComponent.model_construct(name="ProbeAny")])
