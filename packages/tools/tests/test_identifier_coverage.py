"""The completeness guard for the AUTOSAR ``Identifier`` rule.

Every list here is derived from the module that owns it and then proved against behaviour:

* **From ``schema.py``**: every class carrying a ``name``/``id`` field is in :data:`_IN_SCOPE`,
  with a case showing ``check_identifier_conformance()`` reports it, or in :data:`_OUT_OF_SCOPE`
  with a reason.
* **From the goldens**: the out-of-scope claim is measured: one element of every out-of-scope
  class added to ``examples/basic``'s model leaves the ARXML byte-identical to
  ``basic-4.2.2.arxml``.
* **From ``arxml_generation.py``**: every module-level string constant is a ``SHORT-NAME`` affix
  (proved by an overflowing model), an affix subsumed by a longer one, a fixed ``SHORT-NAME``
  (proved legal) or not a ``SHORT-NAME`` at all; a constant added or retired fails here.
* **From ``writer/mutations.py``**: every public mutation is name-writing, with a case proving it
  refuses a non-conforming name and leaves the tree untouched, or not, with a reason.
* **From ``writer/elements.py`` and the registry**: every public function of ``elements.py`` and
  every top-level element kind the registry declares is classified the same way; the kind list
  comes from ``editing/registry.py``, not from ``elements.py``'s own tables.

The MCP half lives in ``packages/mcp/tests/test_protocol_change_set.py``, whose conftest carries
the isolation fixture.
"""

from __future__ import annotations

import ast
import inspect
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from conftest import GOLDEN_MODEL_ROOTS, make_empty_model_root, model_element_path, read_golden, write_model_element
from pydantic import BaseModel
from swcraft_tools import schema
from swcraft_tools.capability_markers import (
    ACCESS_POINTS,
    ASYNCHRONOUS_CALLS,
    DATA_EVENTS,
    DATA_TYPES,
    EXCLUSIVE_AREAS,
    IMPLICIT_ACCESS,
    INTER_RUNNABLE_VARIABLES,
    OPERATION_EVENTS,
    OWN_MARKERS,
    SDG_GID,
)
from swcraft_tools.editing.registry import build_registry
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, check_identifier
from swcraft_tools.reader import (
    read_layer,
    read_requirements,
    read_sequences,
    read_tasks,
)
from swcraft_tools.validator import check_identifier_conformance
from swcraft_tools.writer import arxml_generation, elements, mutations
from swcraft_tools.writer.arxml_generation import (
    BUDGET_SUFFIX,
    CALL_POINT_PREFIX,
    COMPOSITION_SWC_TAG,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    DATA_EVENT_PREFIX,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_SUFFIX,
    INIT_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    OPERATION_EVENT_PREFIX,
    RANGE_SUFFIX,
    READ_ACCESS_PREFIX,
    READ_VARIABLE_PREFIX,
    RESULT_POINT_PREFIX,
    RETURNS_EVENT_PREFIX,
    TIMING_EVENT_PREFIX,
    TIMING_SUFFIX,
    UNIT_SUFFIX,
    WRITE_ACCESS_PREFIX,
    WRITE_VARIABLE_PREFIX,
    access_point_name,
    generate_arxml,
    init_constant_name,
)

#: The suffix of the ``{name}_impl`` twin the writer does not generate; this module spells
#: names that end in it, which are ordinary names.
IMPL_SUFFIX = "_impl"

#: The suffix of the application view the writer does not generate for a composite member;
#: :data:`_RETIRED_AFFIXES` exercises the form from the other side.
APP_TYPE_SUFFIX = "_AppView"

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


def _swc(model_root: Path, swc: dict) -> None:
    _write_yaml(model_root / "swc" / "group" / f"{swc['name']}.yaml", swc)


def _runnable_with(model_root: Path, **lists: list[dict]) -> None:
    """Build one component with one runnable holding *lists* (stated names)."""
    _swc(model_root, {"name": "A", "type": "application", "runnables": [{"name": "Run", **lists}]})


def _issues(model_root: Path) -> list[str]:
    return check_identifier_conformance(str(model_root)).issues


# Derivation 1: every name-bearing schema class


def _name_bearing_schema_classes() -> dict[str, type[BaseModel]]:
    """Return every ``schema.py`` model carrying a ``name`` or an ``id`` field.

    Read off ``model_fields``, not the source: an inherited or aliased field is still a name.
    """
    return {
        name: cls
        for name, cls in vars(schema).items()
        if inspect.isclass(cls)
        and issubclass(cls, BaseModel)
        and cls.__module__ == schema.__name__
        and ({"name", "id"} & set(cls.model_fields))
    }


class Covered:
    """An in-scope class, with the model that proves the check reaches it."""

    def __init__(self, build: Callable[[Path], None], subject: str) -> None:
        self.build = build
        self.subject = subject


def _sr_interface(model_root: Path, iface: dict) -> None:
    _write_yaml(model_root / "interfaces" / f"{iface['name']}.yaml", iface)


def _data_type(model_root: Path, dt: dict) -> None:
    _write_yaml(model_element_path(model_root, dt["name"], kind=dt["kind"]), dt)


def _composition(model_root: Path, composition: dict) -> None:
    _write_yaml(model_root / "compositions" / f"{composition['name']}.yaml", composition)


def _mapping_set(model_root: Path, mapping_set: dict) -> None:
    _write_yaml(model_element_path(model_root, mapping_set["name"], layer="mapping_sets"), mapping_set)


def _element(model_root: Path, element: dict, *, layer: str) -> None:
    """Write one element of *layer*: a unit, a compu method or a data constraint."""
    _write_yaml(model_element_path(model_root, element["name"], layer=layer), element)


#: The ARXML-bound classes, each with a model root holding exactly one non-conforming name of that
#: class; a class the check does not really report fails ``test_each_in_scope_class_is_really_reported``.
_IN_SCOPE: dict[str, Covered] = {
    "SWComponent": Covered(
        lambda root: _swc(root, {"name": "My SWC", "type": "application"}),
        "SWC 'My SWC'",
    ),
    "Port": Covered(
        lambda root: _swc(
            root,
            {
                "name": "A",
                "type": "application",
                "ports": [{"name": "my port", "direction": "provide", "interface": "IExample"}],
            },
        ),
        "port 'my port'",
    ),
    "Runnable": Covered(
        lambda root: _swc(root, {"name": "A", "type": "application", "runnables": [{"name": "9run"}]}),
        "runnable '9run'",
    ),
    "Composition": Covered(
        lambda root: _composition(root, {"name": "My Composition", "title": "T"}),
        "Composition 'My Composition'",
    ),
    "CompositionPort": Covered(
        lambda root: _composition(
            root,
            {
                "name": "Root",
                "title": "T",
                "ports": [{"name": "my port", "direction": "provide", "interface": "IExample"}],
            },
        ),
        "port 'my port'",
    ),
    "CompositionMember": Covered(
        lambda root: _composition(
            root, {"name": "Root", "title": "T", "members": [{"name": "my member", "type": "A"}]}
        ),
        "member 'my member'",
    ),
    "Delegation": Covered(
        lambda root: _composition(
            root,
            {
                "name": "Root",
                "title": "T",
                "delegations": [{"name": "my delegation", "port": "p", "member": "m", "member_port": "q"}],
            },
        ),
        "delegation 'my delegation'",
    ),
    "Connector": Covered(
        lambda root: _write_yaml(
            root / "connectors" / "c.yaml",
            {"name": "My Connector", "from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}},
        ),
        "Connector 'My Connector'",
    ),
    "SRInterface": Covered(
        lambda root: _sr_interface(
            root,
            {
                "name": "I-Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "float32"}],
            },
        ),
        "Interface 'I-Sensor'",
    ),
    "DataElement": Covered(
        lambda root: _sr_interface(
            root,
            {
                "name": "ISensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "my value", "implementation_type": "float32"}],
            },
        ),
        "data element 'my value'",
    ),
    "CSInterface": Covered(
        lambda root: _sr_interface(
            root, {"name": "I-Ctl", "kind": "client_server", "operations": [{"name": "Do", "arguments": []}]}
        ),
        "Interface 'I-Ctl'",
    ),
    "CSOperation": Covered(
        lambda root: _sr_interface(
            root, {"name": "ICtl", "kind": "client_server", "operations": [{"name": "do it", "arguments": []}]}
        ),
        "operation 'do it'",
    ),
    "CSArgument": Covered(
        lambda root: _sr_interface(
            root,
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
        ),
        "argument 'in.value'",
    ),
    "BaseDataType": Covered(
        lambda root: _data_type(root, {**FLOAT32, "name": "float 32"}),
        "Data type 'float 32'",
    ),
    "ValueDataType": Covered(
        lambda root: _data_type(root, {"name": "my alias", "kind": "value_type", "base": "float32"}),
        "Data type 'my alias'",
    ),
    "ReferenceDataType": Covered(
        lambda root: _data_type(root, {"name": "my reference", "kind": "reference_type", "base": "float32"}),
        "Data type 'my reference'",
    ),
    "EnumDataType": Covered(
        lambda root: _data_type(root, {"name": "Gear T", "kind": "enum", "literals": [{"name": "P", "value": 0}]}),
        "Data type 'Gear T'",
    ),
    "EnumLiteral": Covered(
        lambda root: _data_type(root, {"name": "Gear_T", "kind": "enum", "literals": [{"name": "1st", "value": 0}]}),
        "Enum 'Gear_T' literal '1st'",
    ),
    "RecordDataType": Covered(
        lambda root: _data_type(root, {"name": "Pose T", "kind": "record", "fields": [{"name": "x", "type": "f"}]}),
        "Data type 'Pose T'",
    ),
    "RecordField": Covered(
        lambda root: _data_type(
            root, {"name": "Pose_T", "kind": "record", "fields": [{"name": "x pos", "type": "float32"}]}
        ),
        "Record 'Pose_T' field 'x pos'",
    ),
    "ArrayDataType": Covered(
        lambda root: _data_type(root, {"name": "Samples T", "kind": "array", "element_type": "float32", "length": 4}),
        "Data type 'Samples T'",
    ),
    # The application type is reported by the data-type loop; the mapping set is its
    # ARXML-bound layer's own `SHORT-NAME`.
    "ApplicationDataType": Covered(
        lambda root: _data_type(root, {"name": "Speed T", "kind": "application_type", "unit": "mps"}),
        "Data type 'Speed T'",
    ),
    "MappingSet": Covered(
        lambda root: _mapping_set(root, {"name": "My Set", "maps": []}),
        "Mapping set 'My Set'",
    ),
    # A stub emits no `SHORT-NAME`, but its name names its file and is held to the rule the
    # importer derives it by, so it is in scope.
    "ExternalInterface": Covered(
        lambda root: _sr_interface(
            root,
            {
                "name": "I-Foreign",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IForeign",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        ),
        "Interface 'I-Foreign'",
    ),
    "ExternalDataType": Covered(
        lambda root: _data_type(
            root,
            {
                "name": "Foreign T",
                "kind": "external_type",
                "external_ref": "/Foreign/ImplementationDataTypes/ForeignT",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        ),
        "Data type 'Foreign T'",
    ),
    # The implementation record's member name is an IMPLEMENTATION-DATA-TYPE-ELEMENT's
    # SHORT-NAME. (The array's `element_name` is a field, witnessed by
    # `test_impl_composites_validator.py`.)
    "ImplementationRecordDataType": Covered(
        lambda root: _data_type(
            root, {"name": "Frame T", "kind": "implementation_record", "elements": [{"name": "a", "type": "f"}]}
        ),
        "Data type 'Frame T'",
    ),
    "ImplementationElement": Covered(
        lambda root: _data_type(
            root,
            {"name": "Frame_T", "kind": "implementation_record", "elements": [{"name": "my element", "type": "f"}]},
        ),
        "Implementation record 'Frame_T' element 'my element'",
    ),
    "ImplementationArrayDataType": Covered(
        lambda root: _data_type(
            root,
            {
                "name": "Samples Impl",
                "kind": "implementation_array",
                "element_type": "f",
                "length": 2,
                "element_name": "Element",
            },
        ),
        "Data type 'Samples Impl'",
    ),
    "PointerDataType": Covered(
        lambda root: _data_type(root, {"name": "my pointer", "kind": "pointer", "target": "f"}),
        "Data type 'my pointer'",
    ),
    # A texttable's entry name is emitted as its scale's SHORT-LABEL and VT, an enum literal's way.
    "Unit": Covered(lambda root: _element(root, {"name": "km h"}, layer="units"), "Unit 'km h'"),
    "TextTable": Covered(
        lambda root: _element(
            root,
            {"name": "Gear Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]},
            layer="compu_methods",
        ),
        "Compu method 'Gear Table'",
    ),
    "TableEntry": Covered(
        lambda root: _element(
            root,
            {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "my entry", "value": 0}]},
            layer="compu_methods",
        ),
        "Compu method 'Gear_Table' entry 'my entry'",
    ),
    "Linear": Covered(
        lambda root: _element(root, {"name": "Speed Compu", "kind": "linear", "unit": "U"}, layer="compu_methods"),
        "Compu method 'Speed Compu'",
    ),
    # An event's and an access entry's stated `name` is its SHORT-NAME as written.
    "TimingEvent": Covered(
        lambda root: _runnable_with(root, events=[{"type": "timing", "period": 0.01, "name": "my event"}]),
        "timing event name",
    ),
    "InitEvent": Covered(
        lambda root: _runnable_with(root, events=[{"type": "init", "name": "1st"}]),
        "init event name",
    ),
    "OperationEvent": Covered(
        lambda root: _runnable_with(
            root, events=[{"type": "operation", "port": "pServe", "operation": "Get", "name": "on get"}]
        ),
        "operation event name",
    ),
    "DataEvent": Covered(
        lambda root: _runnable_with(
            root, events=[{"type": "data", "port": "rIn", "element": "value", "name": "on value"}]
        ),
        "data-received event name",
    ),
    "CallReturnedEvent": Covered(
        lambda root: _runnable_with(
            root, events=[{"type": "call_returned", "port": "rIn", "operation": "Op", "name": "on done"}]
        ),
        "call-returned event name",
    ),
    "DataReadRef": Covered(
        lambda root: _runnable_with(root, reads=[{"port": "rIn", "element": "value", "name": "read value"}]),
        "reads entry 'rIn'.'value' name",
    ),
    "DataWriteRef": Covered(
        lambda root: _runnable_with(root, writes=[{"port": "pOut", "element": "value", "name": "write-value"}]),
        "writes entry 'pOut'.'value' name",
    ),
    "OperationCallRef": Covered(
        lambda root: _runnable_with(root, calls=[{"port": "rCall", "operation": "Get", "name": "9call"}]),
        "calls entry 'rCall'.'Get' name",
    ),
    "DataConstr": Covered(
        lambda root: _element(root, {"name": "Speed Limits"}, layer="data_constraints"),
        "Data constraint 'Speed Limits'",
    ),
    # A variable's name is its VARIABLE-DATA-PROTOTYPE's SHORT-NAME, and a variable access's
    # stated `name` its VARIABLE-ACCESS's.
    "Variable": Covered(
        lambda root: _swc(
            root,
            {
                "name": "A",
                "type": "application",
                "variables": [{"name": "my variable", "access": "implicit", "implementation_type": "f"}],
            },
        ),
        "SWC 'A' variable 'my variable'",
    ),
    "VariableReadRef": Covered(
        lambda root: _runnable_with(root, reads_variables=[{"variable": "Shared", "name": "read shared"}]),
        "reads_variables entry 'Shared' name",
    ),
    "VariableWriteRef": Covered(
        lambda root: _runnable_with(root, writes_variables=[{"variable": "Shared", "name": "1st write"}]),
        "writes_variables entry 'Shared' name",
    ),
    # An exclusive area's two entry kinds name an area and carry no name, so neither is name-bearing.
    "ExclusiveArea": Covered(
        lambda root: _swc(
            root,
            {"name": "A", "type": "application", "exclusive_areas": [{"name": "my area"}]},
        ),
        "SWC 'A' exclusive area 'my area'",
    ),
}

#: The classes left out, with the reason: none is ever emitted as an AUTOSAR ``SHORT-NAME``, so
#: constraining it would invent a rule the standard does not ask for.
#: :class:`TestTheOutOfScopeTableIsMeasuredAgainstAGolden` measures that claim.
_OUT_OF_SCOPE: dict[str, str] = {
    "Requirement": "an RST/Sphinx-Needs id; the requirements layer reaches no ARXML element",
    "Task": "the scheduling layer; SWCraft emits no AUTOSAR OS configuration",
    "Sequence": (
        "names the sequence's diagram files, and it keeps its own rule "
        "(schema._SEQUENCE_NAME_RE), which allows a leading underscore where AUTOSAR does not"
    ),
    "Processor": "the hardware layer; SWCraft emits no ECU Resource Template",
    "Core": "the hardware layer; a scheduling and budget fact, never an ARXML element",
    "Memory": "the hardware layer; reported by resource-report, never generated",
    "Partition": "the hardware layer; the scheduling partition a task runs in, never an ARXML element",
}


class TestEveryNameBearingClassIsClassified:
    def test_the_classification_is_exactly_the_derived_set(self) -> None:
        derived = set(_name_bearing_schema_classes())
        classified = set(_IN_SCOPE) | set(_OUT_OF_SCOPE)

        assert derived == classified, (
            "schema.py and the identifier classification disagree; "
            f"unclassified: {sorted(derived - classified)}, stale: {sorted(classified - derived)}"
        )

    def test_the_two_tables_are_disjoint(self) -> None:
        assert not (set(_IN_SCOPE) & set(_OUT_OF_SCOPE))

    def test_the_counts_match_the_phase_scope(self) -> None:
        """53 name-bearing classes, 46 of them ARXML-bound and 7 out of scope.

        Written out so a scope change has to be typed: a name-bearing class added to or
        removed from ``schema.py``, or moved between the tables, moves these figures.
        """
        assert len(_name_bearing_schema_classes()) == 53
        assert len(_IN_SCOPE) == 46
        assert len(_OUT_OF_SCOPE) == 7

    @pytest.mark.parametrize("class_name", sorted(_IN_SCOPE))
    def test_each_in_scope_class_is_really_reported(self, class_name: str, tmp_path: Path) -> None:
        covered = _IN_SCOPE[class_name]
        covered.build(tmp_path)

        issues = _issues(tmp_path)

        assert len(issues) == 1, issues
        assert covered.subject in issues[0], issues[0]

    @pytest.mark.parametrize("class_name", sorted(_OUT_OF_SCOPE))
    def test_each_out_of_scope_class_states_a_reason(self, class_name: str) -> None:
        assert _OUT_OF_SCOPE[class_name].strip()


# Derivation 2: the out-of-scope claim, measured against a golden

#: One element per out-of-scope class, each named so the rule would refuse it. ``Sequence``
#: gets a leading underscore, not a space, because its own name rule refuses the space.
_OUT_OF_SCOPE_ELEMENTS: dict[str, tuple[str, str, dict]] = {
    "Requirement": (
        "requirements",
        "REQ OUT OF SCOPE",
        {
            "id": "REQ OUT OF SCOPE",
            "title": "Out of scope",
            "description": "Never an ARXML SHORT-NAME.",
            "version": 1,
            "source": "architecture",
        },
    ),
    "Task": (
        "tasks",
        "Out Of Scope Task",
        # `partition` names one of the golden model's `hardware/` elements: this element
        # exists to be loaded, so it has to validate.
        {"name": "Out Of Scope Task", "period": 0.02, "priority": 3, "partition": "PARTITION0"},
    ),
    "Sequence": (
        "sequences",
        "_out_of_scope_sequence",
        {
            "name": "_out_of_scope_sequence",
            "task": "Out Of Scope Task",
            "steps": [{"swc": "ExampleSWC", "runnable": "ExampleRun"}],
        },
    ),
    "Processor": ("hardware", "Out Of Scope CPU", {"name": "Out Of Scope CPU", "kind": "processor"}),
    "Core": (
        "hardware",
        "Out Of Scope Core",
        {"name": "Out Of Scope Core", "kind": "core", "threshold": 0.7},
    ),
    "Memory": (
        "hardware",
        "Out Of Scope RAM",
        {
            "name": "Out Of Scope RAM",
            "kind": "memory",
            "type": "RAM",
            "size": 4096,
            "accessible": ["CORE0"],
            "threshold": 0.8,
        },
    ),
    "Partition": (
        "hardware",
        "Out Of Scope Partition",
        {"name": "Out Of Scope Partition", "kind": "partition", "core": "CORE0"},
    ),
}


@pytest.fixture
def basic_model_with_out_of_scope_elements(tmp_path: Path) -> Path:
    """Return ``examples/basic``'s model plus one non-conforming out-of-scope element per class."""
    model_root = tmp_path / "model"
    shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
    for subdir, file_stem, data in _OUT_OF_SCOPE_ELEMENTS.values():
        _write_yaml(model_root / subdir / f"{file_stem}.yaml", data)
    return model_root


class TestTheOutOfScopeTableIsMeasuredAgainstAGolden:
    """ "Never emitted as a ``SHORT-NAME``" is settled against the byte-for-byte golden.

    The elements are added rather than read from the committed models, which do not hold every class.
    """

    def test_the_table_covers_every_out_of_scope_class(self) -> None:
        assert set(_OUT_OF_SCOPE_ELEMENTS) == set(_OUT_OF_SCOPE)

    @pytest.mark.parametrize("class_name", sorted(_OUT_OF_SCOPE_ELEMENTS))
    def test_each_added_name_really_is_one_the_rule_would_refuse(self, class_name: str) -> None:
        """Non-vacuity: the added names really are ones the rule would refuse."""
        _, _, data = _OUT_OF_SCOPE_ELEMENTS[class_name]
        assert check_identifier(data.get("name") or data["id"]) is not None

    def test_the_readers_really_load_them(self, basic_model_with_out_of_scope_elements: Path) -> None:
        """Non-vacuity: the readers really load the added files."""
        root = str(basic_model_with_out_of_scope_elements)
        loaded = (
            {requirement.id for requirement in read_requirements(root)}
            | {task.name for task in read_tasks(root)}
            | {sequence.name for sequence in read_sequences(root)}
            | {element.name for element in read_layer(root, "hardware")}
        )

        expected = {name for _, name, _ in _OUT_OF_SCOPE_ELEMENTS.values()}
        assert expected <= loaded

    def test_none_of_them_reaches_the_generated_arxml(
        self, basic_model_with_out_of_scope_elements: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out" / "model.arxml"

        generate_arxml(str(basic_model_with_out_of_scope_elements), str(output))

        assert output.read_text(encoding="utf-8") == read_golden("basic-4.2.2")

    def test_none_of_them_is_reported_by_the_check(self, basic_model_with_out_of_scope_elements: Path) -> None:
        assert _issues(basic_model_with_out_of_scope_elements) == []


# Derivation 3: every generated name form the writer defines

_ARXML_GENERATION_PY = Path(inspect.getsourcefile(arxml_generation) or "")


def _module_level_string_constants() -> dict[str, str]:
    """Return every ``NAME = "literal"`` (or f-string) at ``arxml_generation.py``'s top level.

    An AST scan, not ``vars(module)``, which also holds the constants the module imports. An
    f-string's value is read from the imported module.
    """
    tree = ast.parse(_ARXML_GENERATION_PY.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.JoinedStr):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = getattr(arxml_generation, target.id)
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _long_primitive(root: Path, suffix: str) -> str:
    name = "P" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _data_type(root, {**FLOAT32, "name": name})
    return f"{name}{suffix}"


def _long_alias(root: Path, suffix: str) -> str:
    name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _data_type(root, FLOAT32)
    _data_type(root, {"name": name, "kind": "value_type", "base": "float32"})
    return f"{name}{suffix}"


def _long_enum(root: Path, suffix: str) -> str:
    name = "E" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _data_type(root, {"name": name, "kind": "enum", "literals": [{"name": "P", "value": 0}]})
    return f"{name}{suffix}"


#: The field whose declaration makes the writer emit each application-type form: these forms are
#: reserved for a declared field, not a kind, so the builder must declare it to overflow anything.
_APPLICATION_FORM_DECLARATION: dict[str, dict] = {
    UNIT_SUFFIX: {"unit": "mps"},
    COMPU_SUFFIX: {"conversion": {"factor": 2.0, "offset": 1.0}},
    RANGE_SUFFIX: {"range": {"min": 0.0, "max": 100.0}},
}


def _long_application_type(root: Path, suffix: str) -> str:
    name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _data_type(root, {"name": name, "kind": "application_type", **_APPLICATION_FORM_DECLARATION[suffix]})
    return f"{name}{suffix}"


def _long_swc(root: Path, suffix: str) -> str:
    name = "S" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _swc(root, {"name": name, "type": "application"})
    return f"{name}{suffix}"


def _long_runnable(root: Path, prefix: str, event: dict) -> str:
    name = "R" * (MAX_IDENTIFIER_LENGTH - len(prefix) + 1)
    _swc(root, {"name": "A", "type": "application", "runnables": [{"name": name, "events": [event]}]})
    return f"{prefix}{name}"


def _long_variable_access(root: Path, prefix: str, list_name: str) -> str:
    """Overflow ``RV_{variable}`` or ``WV_{variable}`` with a legal 126-character variable.

    The variable is the only declared name in the form, so its budget is 125.
    """
    name = "V" * (MAX_IDENTIFIER_LENGTH - len(prefix) + 1)
    _swc(
        root,
        {
            "name": "A",
            "type": "application",
            "variables": [{"name": name, "access": "explicit", "implementation_type": "uint8", "init": 0}],
            "runnables": [{"name": "Run", list_name: [{"variable": name}]}],
        },
    )
    return f"{prefix}{name}"


def _long_budgeted_runnable(root: Path, suffix: str) -> str:
    """Build a runnable stating ``wcet``, one character too long for ``{runnable}_Budget``."""
    name = "R" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _swc(
        root,
        {"name": "A", "type": "application", "runnables": [{"name": name, "wcet": "400us", "wcet_type": "gross"}]},
    )
    return f"{name}{suffix}"


def _long_budgeted_swc(root: Path, suffix: str) -> str:
    """Build a component emitting ``{swc}_Timing``, one character too long for it."""
    name = "S" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
    _swc(
        root,
        {"name": name, "type": "application", "runnables": [{"name": "Run", "wcet": "400us", "wcet_type": "gross"}]},
    )
    return f"{name}{suffix}"


def _long_interface_pair(root: Path, suffix: str) -> str:
    """Overflow ``{Interface}_{Element}{suffix}``, the joint budget, with two names legal on their own.

    The pair overflows one character past ``MAX_IDENTIFIER_LENGTH - len(suffix) - 1``.
    """
    budget = MAX_IDENTIFIER_LENGTH - len(suffix) - 1
    interface_name = "I" * (budget // 2 + 1)
    element_name = "E" * (budget - budget // 2)
    _write_yaml(
        root / "interfaces" / f"{interface_name}.yaml",
        {
            "name": interface_name,
            "kind": "sender_receiver",
            "data_elements": [{"name": element_name, "implementation_type": "float32"}],
        },
    )
    return init_constant_name(interface_name, element_name)


def _long_access_pair(root: Path, prefix: str, list_name: str) -> str:
    """Overflow ``{prefix}{port}_{member}`` with a port and a member legal on their own.

    The pair overflows one character past ``MAX_IDENTIFIER_LENGTH - len(prefix) - 1``. A helper of
    its own because the two names live in different layers: the port on the component, the member
    on the interface.
    """
    budget = MAX_IDENTIFIER_LENGTH - len(prefix) - 1
    port_name = "P" * ((budget + 1) // 2)
    member_name = "M" * (budget + 1 - len(port_name))
    interface_name = "IPair"
    if list_name == "calls":
        interface = {
            "name": interface_name,
            "kind": "client_server",
            "operations": [{"name": member_name, "arguments": []}],
        }
        direction, entry = "require", {"port": port_name, "operation": member_name}
    else:
        interface = {
            "name": interface_name,
            "kind": "sender_receiver",
            "data_elements": [{"name": member_name, "implementation_type": "float32"}],
        }
        direction = "provide" if list_name == "writes" else "require"
        entry = {"port": port_name, "element": member_name}
    _write_yaml(root / "interfaces" / f"{interface_name}.yaml", interface)
    _swc(
        root,
        {
            "name": "A",
            "type": "application",
            "ports": [{"name": port_name, "direction": direction, "interface": interface_name}],
            "runnables": [{"name": "Run", list_name: [entry]}],
        },
    )
    return access_point_name(prefix, port_name, member_name)


def _long_operation_event_pair(root: Path) -> str:
    """Overflow ``OIT_{port}_{operation}`` by its pair: the two legal names get 123 characters."""
    budget = MAX_IDENTIFIER_LENGTH - len(OPERATION_EVENT_PREFIX) - 1
    port_name = "P" * ((budget + 1) // 2)
    operation_name = "O" * (budget + 1 - len(port_name))
    _write_yaml(
        root / "interfaces" / "IServe.yaml",
        {"name": "IServe", "kind": "client_server", "operations": [{"name": operation_name, "arguments": []}]},
    )
    _swc(
        root,
        {
            "name": "A",
            "type": "application",
            "ports": [{"name": port_name, "direction": "provide", "interface": "IServe"}],
            "runnables": [
                {"name": "Run", "events": [{"type": "operation", "port": port_name, "operation": operation_name}]}
            ],
        },
    )
    return f"{OPERATION_EVENT_PREFIX}{port_name}_{operation_name}"


def _long_data_event_pair(root: Path) -> str:
    """Overflow ``DRT_{port}_{element}`` by its pair: the two legal names get 123 characters."""
    budget = MAX_IDENTIFIER_LENGTH - len(DATA_EVENT_PREFIX) - 1
    port_name = "R" * ((budget + 1) // 2)
    element_name = "E" * (budget + 1 - len(port_name))
    write_model_element(root, {**FLOAT32, "name": "float32_base"})
    write_model_element(root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
    _write_yaml(
        root / "interfaces" / "IArrive.yaml",
        {
            "name": "IArrive",
            "kind": "sender_receiver",
            "data_elements": [{"name": element_name, "implementation_type": "float32"}],
        },
    )
    _swc(
        root,
        {
            "name": "A",
            "type": "application",
            "ports": [{"name": port_name, "direction": "require", "interface": "IArrive"}],
            "runnables": [{"name": "Run", "events": [{"type": "data", "port": port_name, "element": element_name}]}],
        },
    )
    return f"{DATA_EVENT_PREFIX}{port_name}_{element_name}"


def _long_async_pair(root: Path, prefix: str, callback: bool) -> str:
    """Overflow ``Result_{port}_{operation}`` (polled call) or ``ASRT_{port}_{operation}`` (*callback*).

    The port and the operation are legal and declared, so the one finding is the generated form's.
    """
    budget = MAX_IDENTIFIER_LENGTH - len(prefix) - 1
    port_name = "R" * ((budget + 1) // 2)
    operation_name = "O" * (budget + 1 - len(port_name))
    _write_yaml(
        root / "interfaces" / "IStore.yaml",
        {"name": "IStore", "kind": "client_server", "operations": [{"name": operation_name, "arguments": []}]},
    )
    call = {"port": port_name, "operation": operation_name, "asynchronous": True}
    runnables: list[dict[str, object]] = [{"name": "Run", "calls": [call if callback else {**call, "poll": True}]}]
    if callback:
        runnables.append(
            {"name": "OnDone", "events": [{"type": "call_returned", "port": port_name, "operation": operation_name}]}
        )
    _swc(
        root,
        {
            "name": "A",
            "type": "application",
            "ports": [{"name": port_name, "direction": "require", "interface": "IStore"}],
            "runnables": runnables,
        },
    )
    return f"{prefix}{port_name}_{operation_name}"


def _marker_document(base: Path, list_name: str = "reads") -> str:
    """Generate a component-export document carrying one access point and every capability marker.

    The port is always ``rIn`` and the member always ``value``, so a caller can spell the derived name.
    """
    model_root = make_empty_model_root(base)
    # The data element names a value type over a base type.
    write_model_element(model_root, {**FLOAT32, "name": "float32_base"})
    write_model_element(model_root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
    if list_name == "calls":
        interface = {"name": "IMark", "kind": "client_server", "operations": [{"name": "value", "arguments": []}]}
        entry = {"port": "rIn", "operation": "value"}
    else:
        interface = {
            "name": "IMark",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        }
        entry = {"port": "rIn", "element": "value"}
    _write_yaml(model_root / "interfaces" / "IMark.yaml", interface)
    _swc(
        model_root,
        {
            "name": "Marked",
            "type": "application",
            "ports": [
                {
                    "name": "rIn",
                    "direction": "provide" if list_name == "writes" else "require",
                    "interface": "IMark",
                }
            ],
            "runnables": [{"name": "Run", list_name: [entry]}],
            "handle_termination_and_restart": "no_support",
        },
    )
    output = base / "model.arxml"
    arxml_generation.generate_component_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


#: Constant name -> a model whose declared names all conform and whose form emitted through that
#: affix does not; the builder returns the emitted string the report must name. Joint-budget
#: affixes overflow a pair of declared names. An affix added to the writer lands in no bucket and
#: fails ``test_every_writer_string_constant_is_classified``.
_SHORT_NAME_AFFIXES: dict[str, Callable[[Path], str]] = {
    "COMPU_METHOD_SUFFIX": lambda root: _long_enum(root, COMPU_METHOD_SUFFIX),
    "IMPLEMENTATION_SUFFIX": lambda root: _long_swc(root, IMPLEMENTATION_SUFFIX),
    "INTERNAL_BEHAVIOR_SUFFIX": lambda root: _long_swc(root, INTERNAL_BEHAVIOR_SUFFIX),
    "TIMING_EVENT_PREFIX": lambda root: _long_runnable(root, TIMING_EVENT_PREFIX, {"type": "timing", "period": 0.01}),
    "INIT_EVENT_PREFIX": lambda root: _long_runnable(root, INIT_EVENT_PREFIX, {"type": "init"}),
    "INIT_CONSTANT_SUFFIX": lambda root: _long_interface_pair(root, INIT_CONSTANT_SUFFIX),
    "READ_ACCESS_PREFIX": lambda root: _long_access_pair(root, READ_ACCESS_PREFIX, "reads"),
    "WRITE_ACCESS_PREFIX": lambda root: _long_access_pair(root, WRITE_ACCESS_PREFIX, "writes"),
    "CALL_POINT_PREFIX": lambda root: _long_access_pair(root, CALL_POINT_PREFIX, "calls"),
    "OPERATION_EVENT_PREFIX": _long_operation_event_pair,
    "DATA_EVENT_PREFIX": _long_data_event_pair,
    "RESULT_POINT_PREFIX": lambda root: _long_async_pair(root, RESULT_POINT_PREFIX, callback=False),
    "RETURNS_EVENT_PREFIX": lambda root: _long_async_pair(root, RETURNS_EVENT_PREFIX, callback=True),
    # The two variable access forms, each a single-name budget of 125.
    "READ_VARIABLE_PREFIX": lambda root: _long_variable_access(root, READ_VARIABLE_PREFIX, "reads_variables"),
    "WRITE_VARIABLE_PREFIX": lambda root: _long_variable_access(root, WRITE_VARIABLE_PREFIX, "writes_variables"),
    # The application type's three suffixes: single-name budgets of 123, 122 and 122.
    "UNIT_SUFFIX": lambda root: _long_application_type(root, UNIT_SUFFIX),
    "COMPU_SUFFIX": lambda root: _long_application_type(root, COMPU_SUFFIX),
    "RANGE_SUFFIX": lambda root: _long_application_type(root, RANGE_SUFFIX),
    # Charged only where the runnable states `wcet`.
    "BUDGET_SUFFIX": lambda root: _long_budgeted_runnable(root, BUDGET_SUFFIX),
}

#: Affixes whose overflow is reported under another, longer affix's emitted name: ``{swc}_Timing``
#: is emitted only beside ``{swc}_Implementation``, so ``check-model`` charges the longer form only.
#: The value names the reporting affix.
_SUBSUMED_AFFIXES: dict[str, tuple[Callable[[Path], str], str]] = {
    "TIMING_SUFFIX": (lambda root: _long_budgeted_swc(root, TIMING_SUFFIX), IMPLEMENTATION_SUFFIX),
}

#: The ``SHORT-NAME``s the writer owns outright: none, since compositions and mapping sets are
#: declared elements. Kept as a classification target; its emptiness is asserted below.
_FIXED_SHORT_NAMES: tuple[str, ...] = ()

#: The writer's other string constants, with why each never becomes a ``SHORT-NAME``. Each is
#: also proved no legal Identifier, so a new name form cannot be parked here by mistake.
_NOT_A_SHORT_NAME: dict[str, str] = {
    "APPLICATION_SWC_TAG": "an element tag and a reference DEST, never a SHORT-NAME",
    "COMPOSITION_SWC_TAG": "the composition half of the same pair -- an element tag and a reference DEST",
    "_CONFORMING_VALUE": "the message of a presence check that raises, never written to the document",
}

#: The marker texts emitted as an attribute value or a non-name element's text, never as a
#: ``SHORT-NAME``. Some are legal Identifiers, so the proof is behavioural: a document carrying the
#: marker carries the value and no ``SHORT-NAME`` equal to it. The values live in
#: ``capability_markers``, not the writer, so this table is no part of the classification. Each
#: entry is ``(value, what it is)``; the keys are test ids only, kept so
#: ``scripts/exchange_output_oracle.py`` compares each case with itself.
_SDG_MARKER_VALUES: dict[str, tuple[str, str]] = {
    "ACCESS_MARKER_GID": (SDG_GID, "the GID attribute of SWCraft's own SDG on a SWC-INTERNAL-BEHAVIOR"),
    "ACCESS_MARKER_SD": (ACCESS_POINTS.sd_gid, "the GID attribute of the SD inside it"),
    "ACCESS_MARKER_VALUE": (
        ACCESS_POINTS.text,
        "that SD's text -- a capability token, never a name and never a version",
    ),
    "DATA_TYPES_MARKER_SD": (
        DATA_TYPES.sd_gid,
        "the GID attribute of the data-types SD a component export adds to that group",
    ),
    "DATA_TYPES_MARKER_VALUE": (
        DATA_TYPES.text,
        "that SD's text -- the shape the exporter states, never a name and never a version",
    ),
    "OPERATION_EVENTS_MARKER_SD": (
        OPERATION_EVENTS.sd_gid,
        "the GID attribute of the operation-events SD a component export adds to that group",
    ),
    "OPERATION_EVENTS_MARKER_VALUE": (
        OPERATION_EVENTS.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
    "INTER_RUNNABLE_VARIABLES_MARKER_SD": (
        INTER_RUNNABLE_VARIABLES.sd_gid,
        "the GID attribute of the inter-runnable variables SD a component export adds to that group",
    ),
    "INTER_RUNNABLE_VARIABLES_MARKER_VALUE": (
        INTER_RUNNABLE_VARIABLES.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
    "EXCLUSIVE_AREAS_MARKER_SD": (
        EXCLUSIVE_AREAS.sd_gid,
        "the GID attribute of the exclusive areas SD a component export adds to that group",
    ),
    "EXCLUSIVE_AREAS_MARKER_VALUE": (
        EXCLUSIVE_AREAS.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
    "DATA_EVENTS_MARKER_SD": (
        DATA_EVENTS.sd_gid,
        "the GID attribute of the data-received events SD a component export adds to that group",
    ),
    "DATA_EVENTS_MARKER_VALUE": (
        DATA_EVENTS.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
    "IMPLICIT_ACCESS_MARKER_SD": (
        IMPLICIT_ACCESS.sd_gid,
        "the GID attribute of the implicit access SD a component export adds to that group",
    ),
    "IMPLICIT_ACCESS_MARKER_VALUE": (
        IMPLICIT_ACCESS.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
    "ASYNCHRONOUS_CALLS_MARKER_SD": (
        ASYNCHRONOUS_CALLS.sd_gid,
        "the GID attribute of the asynchronous calls SD a component export adds to that group",
    ),
    "ASYNCHRONOUS_CALLS_MARKER_VALUE": (
        ASYNCHRONOUS_CALLS.text,
        "that SD's text -- what the exporter states, never a name and never a version",
    ),
}


#: Affixes whose form the writer does not generate, exercised from the other side: a model
#: whose name with the affix would overflow is reported for nothing.
_RETIRED_AFFIXES: dict[str, Callable[[Path], str]] = {
    "IMPL_SUFFIX": lambda root: _long_primitive(root, IMPL_SUFFIX),
    "APP_TYPE_SUFFIX": lambda root: _long_alias(root, APP_TYPE_SUFFIX),
}


class TestEveryGeneratedFormIsAccountedFor:
    def test_every_writer_string_constant_is_classified(self) -> None:
        derived = set(_module_level_string_constants())
        classified = (
            set(_SHORT_NAME_AFFIXES) | set(_SUBSUMED_AFFIXES) | set(_FIXED_SHORT_NAMES) | set(_NOT_A_SHORT_NAME)
        )

        assert derived == classified, (
            "arxml_generation.py defines a string constant this guard has no opinion about; "
            f"unclassified: {sorted(derived - classified)}, stale: {sorted(classified - derived)}"
        )

    def test_the_scan_found_the_constants_it_is_supposed_to(self) -> None:
        """A scan that silently matched nothing would pass the test above too."""
        constants = _module_level_string_constants()
        assert "IMPL_SUFFIX" not in constants  # deleted with the twin
        # Retired constants, asserted here: the classification above would also pass if the scan
        # stopped seeing them.
        assert "APP_TYPE_SUFFIX" not in constants
        assert "DATA_TYPE_MAPPING_SET_NAME" not in constants
        assert constants["COMPOSITION_SWC_TAG"] == COMPOSITION_SWC_TAG
        assert "SERVER_QUEUE_LENGTH" not in constants  # an int, and private
        # The capability markers live in `capability_markers`, not the writer.
        assert not [name for name in constants if "MARKER" in name]

    @pytest.mark.parametrize("constant_name", sorted(_SHORT_NAME_AFFIXES))
    def test_each_affix_overflows_into_a_reported_issue(self, constant_name: str, tmp_path: Path) -> None:
        emitted = _SHORT_NAME_AFFIXES[constant_name](tmp_path)
        assert check_identifier(emitted) is not None

        issues = _issues(tmp_path)

        assert any(emitted in issue for issue in issues), (emitted, issues)

    @pytest.mark.parametrize("constant_name", sorted(_SUBSUMED_AFFIXES))
    def test_each_subsumed_affix_overflows_into_the_longer_affix_s_issue(
        self, constant_name: str, tmp_path: Path
    ) -> None:
        """The subsumed affix's overflow is reported, under the longer form's name."""
        builder, reporting_suffix = _SUBSUMED_AFFIXES[constant_name]
        emitted = builder(tmp_path)
        suffix = _module_level_string_constants()[constant_name]
        reporting = f"{emitted[: -len(suffix)]}{reporting_suffix}"
        assert check_identifier(emitted) is not None
        assert len(reporting_suffix) > len(suffix), "the reporting form must be the longer one"

        issues = _issues(tmp_path)

        assert any(reporting in issue for issue in issues), (reporting, issues)
        assert not any(f"'{emitted}'" in issue for issue in issues), (emitted, issues)

    @pytest.mark.parametrize("constant_name", sorted(_RETIRED_AFFIXES))
    def test_a_retired_affix_is_no_longer_reported(self, constant_name: str, tmp_path: Path) -> None:
        """A retired form, still no legal Identifier, is reported for nothing: nothing emits it."""
        emitted = _RETIRED_AFFIXES[constant_name](tmp_path)
        assert check_identifier(emitted) is not None

        issues = _issues(tmp_path)

        assert not any(emitted in issue for issue in issues), (emitted, issues)
        assert issues == [], (constant_name, "the declaring kind has no generated form to overflow")

    def test_every_fixed_short_name_is_itself_a_legal_identifier(self) -> None:
        """Each fixed ``SHORT-NAME`` is a legal Identifier.

        A loop, not a parametrization: over the empty bucket a parametrized case is a skip, and
        this way the guard is plainly vacuous while the writer owns none.
        """
        constants = _module_level_string_constants()
        for constant_name in _FIXED_SHORT_NAMES:
            value = constants[constant_name]
            assert check_identifier(value) is None, value

    @pytest.mark.parametrize("constant_name", sorted(_NOT_A_SHORT_NAME))
    def test_each_non_short_name_constant_could_not_be_one(self, constant_name: str) -> None:
        value = _module_level_string_constants()[constant_name]
        assert _NOT_A_SHORT_NAME[constant_name].strip()
        assert check_identifier(value) is not None, (
            f"'{constant_name}' is a legal Identifier, so parking it in _NOT_A_SHORT_NAME "
            "no longer proves anything — classify it as an affix or a fixed SHORT-NAME instead."
        )

    def test_the_writer_owns_no_fixed_short_name(self) -> None:
        """The writer mints no ``SHORT-NAME`` without reading the model.

        Asserted from both sides: an empty bucket alone is satisfied by moving the entry elsewhere.
        """
        assert _FIXED_SHORT_NAMES == ()
        assert "DATA_TYPE_MAPPING_SET_NAME" not in _module_level_string_constants()

    @pytest.mark.parametrize("label", sorted(_SDG_MARKER_VALUES))
    def test_each_marker_value_is_emitted_but_never_as_a_short_name(self, label: str, tmp_path: Path) -> None:
        """Each marker value is in a document carrying the marker, and no ``SHORT-NAME`` equals it."""
        value, what = _SDG_MARKER_VALUES[label]
        assert what.strip()
        document = _marker_document(tmp_path)
        assert value in document, value
        assert f"<SHORT-NAME>{value}</SHORT-NAME>" not in document

    def test_the_marker_values_are_the_seventeen_the_writer_owns(self) -> None:
        """The table is the group's ``GID`` plus every marker's ``GID`` and value, each byte-pinned.

        A marker appended to ``OWN_MARKERS`` fails here until it is classified.
        """
        assert len(_SDG_MARKER_VALUES) == 17
        assert {value for value, _ in _SDG_MARKER_VALUES.values()} == {SDG_GID} | {
            text for marker in OWN_MARKERS for text in (marker.sd_gid, marker.text)
        }
        assert (SDG_GID, ACCESS_POINTS.sd_gid, ACCESS_POINTS.text) == ("SWCRAFT", "ACCESS-POINTS", "explicit")
        assert (DATA_TYPES.sd_gid, DATA_TYPES.text) == (
            "DATA-TYPES",
            "base and implementation types declared, no invented twin",
        )
        assert (OPERATION_EVENTS.sd_gid, OPERATION_EVENTS.text) == (
            "OPERATION-EVENTS",
            "operation events and concurrency declared",
        )
        assert (INTER_RUNNABLE_VARIABLES.sd_gid, INTER_RUNNABLE_VARIABLES.text) == (
            "INTER-RUNNABLE-VARIABLES",
            "inter-runnable variables and local variable accesses declared",
        )
        assert (EXCLUSIVE_AREAS.sd_gid, EXCLUSIVE_AREAS.text) == (
            "EXCLUSIVE-AREAS",
            "exclusive areas and exclusive area references declared",
        )
        assert (DATA_EVENTS.sd_gid, DATA_EVENTS.text) == ("DATA-EVENTS", "data-received events declared")
        assert (IMPLICIT_ACCESS.sd_gid, IMPLICIT_ACCESS.text) == (
            "IMPLICIT-ACCESS",
            "implicit reads and writes declared",
        )
        assert (ASYNCHRONOUS_CALLS.sd_gid, ASYNCHRONOUS_CALLS.text) == (
            "ASYNCHRONOUS-CALLS",
            "asynchronous calls, result points and call-returned events declared",
        )


# Derivation 4: the write path, from mutations.py's own public surface

#: Legal on its own (126 characters), illegal behind either event prefix (``IT_`` is the shorter):
#: the case ``add_runnable`` cannot predict, so the ``add_*_event`` mutations guard it themselves.
_LONG_RUNNABLE = "R" * (MAX_IDENTIFIER_LENGTH - 2)


def _refusing_create_swc(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_swc(str(root), "My SWC", "sensors")


def _refusing_add_port(root: Path) -> Callable[[], object]:
    mutations.create_swc(str(root), "A", "sensors")
    return lambda: mutations.add_port(str(root), "A", "my port", "provide", "ISensor")


def _refusing_add_runnable(root: Path) -> Callable[[], object]:
    mutations.create_swc(str(root), "A", "sensors")
    return lambda: mutations.add_runnable(str(root), "A", "my run")


def _refusing_add_timing_event(root: Path) -> Callable[[], object]:
    mutations.create_swc(str(root), "A", "sensors")
    mutations.add_runnable(str(root), "A", _LONG_RUNNABLE)
    return lambda: mutations.add_timing_event(str(root), "A", _LONG_RUNNABLE, 0.01)


def _refusing_add_init_event(root: Path) -> Callable[[], object]:
    mutations.create_swc(str(root), "A", "sensors")
    mutations.add_runnable(str(root), "A", _LONG_RUNNABLE)
    return lambda: mutations.add_init_event(str(root), "A", _LONG_RUNNABLE)


def _refusing_create_interface(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_interface(str(root), "I-Sensor", [{"name": "v", "implementation_type": "float32"}])


def _refusing_create_cs_interface(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_cs_interface(str(root), "I-Ctl", [{"name": "Do", "arguments": []}])


def _refusing_create_data_type(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_data_type(str(root), "float 32", "base_type", size=32, encoding="IEEE754")


def _refusing_create_connector(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_connector(str(root), "My Connector", "A", "pOut", "B", "rIn")


def _refusing_create_composition(root: Path) -> Callable[[], object]:
    return lambda: mutations.create_composition(str(root), "My Composition", "A composition")


def _refusing_create_mapping_set(root: Path) -> Callable[[], object]:
    """A mapping set's name is a ``DATA-TYPE-MAPPING-SET`` ``SHORT-NAME``, so its writer is name-writing."""
    return lambda: mutations.create_mapping_set(str(root), "My Mapping Set")


#: One refusal case per public mutation that writes an AUTOSAR ``SHORT-NAME``. Each entry does its
#: setup and returns the call, so "wrote nothing" is measured after the setup.
_NAME_WRITING_MUTATIONS: dict[str, Callable[[Path], Callable[[], object]]] = {
    "create_swc": _refusing_create_swc,
    "add_port": _refusing_add_port,
    "add_runnable": _refusing_add_runnable,
    "add_timing_event": _refusing_add_timing_event,
    "add_init_event": _refusing_add_init_event,
    "create_interface": _refusing_create_interface,
    "create_cs_interface": _refusing_create_cs_interface,
    "create_data_type": _refusing_create_data_type,
    "create_connector": _refusing_create_connector,
    "create_composition": _refusing_create_composition,
    "create_mapping_set": _refusing_create_mapping_set,
}

#: The public mutations that write no ``SHORT-NAME``, with the reason.
_NOT_NAME_WRITING_MUTATIONS: dict[str, str] = {
    "create_requirement": "a requirements-layer id — out of scope by decision 2 (RST, never ARXML)",
    "add_trace_link": "appends an existing SWC name to an existing requirement; writes no new name",
    "create_task": "the scheduling layer; SWCraft emits no AUTOSAR OS configuration",
    "create_sequence": "a sequence name, which names its diagram files and keeps schema.py's own, different rule",
    "create_hardware_element": "the hardware layer; SWCraft emits no ECU Resource Template",
    "update_resource": "edits an existing hardware element's budget; writes no name",
    "map_runnable_to_task": "references existing SWC/runnable names; writes no new name",
    "set_task_wcet": "a scheduling number on an existing task",
    "set_task_partition": "re-points an existing task to an existing partition; writes no new name",
    "set_task_priority": "a scheduling number on an existing task",
    "set_swc_memory_budget": "a byte budget keyed by an existing memory-pool name",
    "unset_swc_memory_budget": "removes a byte budget; writes no name",
    "find_runnable_ref": "a lookup, not a mutation — it writes nothing at all",
    # Public because the importers and the editing engine import them; none writes.
    "audit": "appends the audit record of a write already made; writes no model name",
    "check_path_segment": "a guard the mutations call; raises on a path separator, writes nothing",
    "require_identifier": "a guard the mutations call; raises on an illegal name, writes nothing",
    "check_generated_identifier": "a guard the mutations call; raises on an illegal generated name, writes nothing",
    "check_operation_event_identifier": "a guard the mutations call; raises on an illegal OIT_ name, writes nothing",
    "check_data_event_identifier": "a guard the mutations call; raises on an illegal DRT_ name, writes nothing",
    "check_call_returned_event_identifier": "a guard; raises on an illegal ASRT_ name, writes nothing",
    "check_data_type_identifiers": "a guard create_data_type calls; raises on an illegal name, writes nothing",
    "check_reserved_swc_name": "a guard the mutations call; raises on a reserved SWC name, writes nothing",
    "check_reserved_data_type_name": "a guard the mutations call; raises on a reserved data type name, writes nothing",
    "check_port_init_values_writable": "a guard the mutations call; raises on an unwritable port init, writes nothing",
    "check_variable_init_values_writable": "a guard; raises on an unwritable variable init, writes nothing",
    "check_interface_init_constants": "a guard the mutations call; raises on an illegal init constant, writes nothing",
    "check_runnable_access_names": "a guard the mutations call; raises on an illegal access point name, writes nothing",
}


def _public_functions(module: object) -> set[str]:
    """Return every module-level public function *module* defines (an AST scan, not ``vars``)."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    return {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def _public_mutations() -> set[str]:
    """Return every module-level public function ``writer/mutations.py`` defines."""
    return _public_functions(mutations)


class TestEveryPublicMutationIsClassified:
    """Every public mutation of ``writer/mutations.py`` is classified, guarding the write path.

    The write-path cases in ``test_reserved_name_policies.py`` are hand-listed and cannot notice an
    added mutation.
    """

    def test_the_classification_is_exactly_the_derived_set(self) -> None:
        derived = _public_mutations()
        classified = set(_NAME_WRITING_MUTATIONS) | set(_NOT_NAME_WRITING_MUTATIONS)

        assert derived == classified, (
            "writer/mutations.py exposes a public function this guard has no opinion about; "
            f"unclassified: {sorted(derived - classified)}, stale: {sorted(classified - derived)}"
        )

    def test_the_two_tables_are_disjoint(self) -> None:
        assert not (set(_NAME_WRITING_MUTATIONS) & set(_NOT_NAME_WRITING_MUTATIONS))

    @pytest.mark.parametrize("mutation", sorted(_NAME_WRITING_MUTATIONS))
    def test_each_name_writing_mutation_refuses_and_writes_nothing(self, mutation: str, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir(parents=True)
        call = _NAME_WRITING_MUTATIONS[mutation](root)
        before = sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix())

        with pytest.raises(ValueError) as excinfo:
            call()

        assert (
            sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix()) == before
        ), "a refused mutation must leave the model tree exactly as it found it"
        assert "legal AUTOSAR Identifier" in str(excinfo.value), str(excinfo.value)

    @pytest.mark.parametrize("mutation", sorted(_NOT_NAME_WRITING_MUTATIONS))
    def test_each_non_name_writing_mutation_states_a_reason(self, mutation: str) -> None:
        assert _NOT_NAME_WRITING_MUTATIONS[mutation].strip()


# Derivation 5: the generic element writer, from the registry's own kind list

#: What ``writer/elements.py`` exposes, classified as ``mutations.py``'s functions are.
_NAME_WRITING_ELEMENT_WRITERS: dict[str, str] = {
    "write_element": "writes a whole element file, including every nested and generated SHORT-NAME",
}

_NOT_NAME_WRITING_ELEMENT_WRITERS: dict[str, str] = {
    "remove_element": "deletes one file; it writes no name at all, and it accepts no element",
    "edited_text": "computes a text and writes nothing; write_element's guards run on what it computed",
    # Public because editing/staging.py calls them.
    "stands_in_writer_form": "compares a text with the writer's form of an element; reads and writes no file",
    "undeclared_keys_of": "lists the keys of a text no field declares; reads and writes no file",
    "element_to_dict": "builds the dict write_element dumps; reads and writes no file",
}


#: One non-conforming element per top-level element kind the registry declares, as
#: ``(target, element)``; every target is a legal path, so what is refused is the name.
_NAME_WRITING_ELEMENT_KINDS: dict[str, tuple[str, BaseModel]] = {
    "swc": ("swc/g/My SWC.yaml", schema.SWComponent.model_validate({"name": "My SWC"})),
    "sender_receiver": (
        "interfaces/I-Sensor.yaml",
        schema.SRInterface.model_validate(
            {
                "name": "I-Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "uint8"}],
            }
        ),
    ),
    "client_server": (
        "interfaces/I-Ctl.yaml",
        schema.CSInterface.model_validate({"name": "I-Ctl", "kind": "client_server", "operations": [{"name": "Do"}]}),
    ),
    "composition": (
        "compositions/My Composition.yaml",
        schema.Composition.model_validate({"name": "My Composition", "title": "T"}),
    ),
    "connector": (
        "connectors/My Connector.yaml",
        schema.Connector.model_validate(
            {
                "name": "My Connector",
                "from": {"swc": "A", "port": "pOut"},
                "to": {"swc": "B", "port": "rIn"},
            }
        ),
    ),
    "base_type": (
        "data_types/float 32.yaml",
        schema.BaseDataType.model_validate(
            {"name": "float 32", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        ),
    ),
    "value_type": (
        "data_types/my alias.yaml",
        schema.ValueDataType.model_validate({"name": "my alias", "kind": "value_type", "base": "uint8"}),
    ),
    "reference_type": (
        "data_types/my reference.yaml",
        schema.ReferenceDataType.model_validate({"name": "my reference", "kind": "reference_type", "base": "my alias"}),
    ),
    "enum": (
        "data_types/my enum.yaml",
        schema.EnumDataType.model_validate(
            {"name": "my enum", "kind": "enum", "literals": [{"name": "Off", "value": 0}]}
        ),
    ),
    "record": (
        "data_types/my record.yaml",
        schema.RecordDataType.model_validate(
            {"name": "my record", "kind": "record", "fields": [{"name": "x", "type": "uint8"}]}
        ),
    ),
    "array": (
        "data_types/my array.yaml",
        schema.ArrayDataType.model_validate(
            {"name": "my array", "kind": "array", "element_type": "uint8", "length": 4}
        ),
    ),
    "application_type": (
        "data_types/my application type.yaml",
        schema.ApplicationDataType.model_validate(
            {"name": "my application type", "kind": "application_type", "unit": "mps"}
        ),
    ),
    "mapping_set": (
        "mapping_sets/my set.yaml",
        schema.MappingSet.model_validate({"name": "my set", "maps": []}),
    ),
    "implementation_record": (
        "data_types/my record impl.yaml",
        schema.ImplementationRecordDataType.model_validate(
            {"name": "my record impl", "kind": "implementation_record", "elements": [{"name": "a", "type": "uint8"}]}
        ),
    ),
    "implementation_array": (
        "data_types/my array impl.yaml",
        schema.ImplementationArrayDataType.model_validate(
            {
                "name": "my array impl",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 4,
                "element_name": "Element",
            }
        ),
    ),
    "pointer": (
        "data_types/my pointer.yaml",
        schema.PointerDataType.model_validate({"name": "my pointer", "kind": "pointer", "target": "uint8"}),
    ),
    # A texttable's entry names are its scales' SHORT-LABELs.
    "unit": ("data_types/units/km h.yaml", schema.Unit.model_validate({"name": "km h"})),
    "texttable": (
        "data_types/compu_methods/Gear Table.yaml",
        schema.TextTable.model_validate(
            {"name": "Gear Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}
        ),
    ),
    "linear": (
        "data_types/compu_methods/Speed Compu.yaml",
        schema.Linear.model_validate({"name": "Speed Compu", "kind": "linear", "unit": "U"}),
    ),
    "data_constr": (
        "data_types/constraints/Speed Limits.yaml",
        schema.DataConstr.model_validate({"name": "Speed Limits"}),
    ),
}

#: The kinds ``write_element`` writes without an ``Identifier`` check, with the reason: the scope
#: ``check_identifier_conformance`` states, per element kind.
_NOT_NAME_WRITING_ELEMENT_KINDS: dict[str, str] = {
    "requirement": "a requirements-layer id — out of scope by decision 2 (RST, never ARXML)",
    "task": "the scheduling layer; SWCraft emits no AUTOSAR OS configuration",
    "sequence": "a sequence name, which names its diagram files and keeps schema.py's own, different rule",
    "processor": "the hardware layer; SWCraft emits no ECU Resource Template",
    "core": "the hardware layer; a scheduling and budget fact, never an ARXML element",
    "memory": "the hardware layer; reported by resource-report, never generated",
    "partition": "the hardware layer; the scheduling partition a task runs in, never an ARXML element",
    # A stub is written into no ARXML: a reference to it carries its `external_ref`, so its
    # `name` is never a `SHORT-NAME`. `check-model` still checks it (pass D), never `write_element`.
    "external_interface": "an external interface stub; no element is emitted, its reference is the source's path",
    "external_type": "an external data type stub; no element is emitted, its reference is the source's path",
}


def _registry_top_level_kinds() -> set[str]:
    """Return every top-level element kind the registry declares.

    Read from ``editing/registry.py``, not ``elements.py``'s own tables. Member kinds are out:
    never written on their own, their names are checked in the owner's case
    (``packages/tools/tests/test_writer_elements.py``).
    """
    return {kind.kind for layer in build_registry().layers for kind in layer.top_level_kinds}


class TestTheGenericElementWriterIsClassified:
    """``writer/elements.py`` is classified: one call emits a whole element's names, generated ones included."""

    def test_every_public_element_writer_is_classified(self) -> None:
        derived = _public_functions(elements)
        classified = set(_NAME_WRITING_ELEMENT_WRITERS) | set(_NOT_NAME_WRITING_ELEMENT_WRITERS)

        assert derived == classified, (
            "writer/elements.py exposes a public function this guard has no opinion about; "
            f"unclassified: {sorted(derived - classified)}, stale: {sorted(classified - derived)}"
        )

    def test_every_registry_element_kind_is_classified(self) -> None:
        derived = _registry_top_level_kinds()
        classified = set(_NAME_WRITING_ELEMENT_KINDS) | set(_NOT_NAME_WRITING_ELEMENT_KINDS)

        assert derived == classified, (
            "the registry declares a top-level element kind write_element has no opinion about; "
            f"unclassified: {sorted(derived - classified)}, stale: {sorted(classified - derived)}"
        )

    def test_the_two_kind_tables_are_disjoint(self) -> None:
        assert not (set(_NAME_WRITING_ELEMENT_KINDS) & set(_NOT_NAME_WRITING_ELEMENT_KINDS))

    @pytest.mark.parametrize("kind", sorted(_NAME_WRITING_ELEMENT_KINDS))
    def test_each_name_writing_kind_is_refused_and_writes_nothing(self, kind: str, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir(parents=True)
        target, element = _NAME_WRITING_ELEMENT_KINDS[kind]
        before = sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix())

        with pytest.raises(ValueError) as excinfo:
            elements.write_element(str(root), target, element)

        assert (
            sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix()) == before
        ), "a refused write must leave the model tree exactly as it found it"
        assert "legal AUTOSAR Identifier" in str(excinfo.value), str(excinfo.value)

    @pytest.mark.parametrize("kind", sorted(_NOT_NAME_WRITING_ELEMENT_KINDS))
    def test_each_non_name_writing_kind_states_a_reason(self, kind: str) -> None:
        assert _NOT_NAME_WRITING_ELEMENT_KINDS[kind].strip()

    def test_every_classified_kind_is_one_the_writer_dispatches_on(self) -> None:
        """Each classified kind is one the writer dispatches on, and the other way round.

        Both halves are checked against the registry's ``kind -> model`` map.
        """
        registry = build_registry()
        for kind in _NAME_WRITING_ELEMENT_KINDS:
            assert registry.element_kind(kind).model in elements._NAME_GUARDS, kind
        for kind in _NOT_NAME_WRITING_ELEMENT_KINDS:
            assert issubclass(registry.element_kind(kind).model, elements._NO_EMITTED_SHORT_NAME), kind
