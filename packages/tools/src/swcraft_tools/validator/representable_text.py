"""The rule that every free text the ARXML writer writes is representable in XML 1.0.

Checks ``rule:text.xml-unrepresentable-character`` (:func:`check_xml_representable_text`) and
holds the sentence it reports and the data description texts it judges, both shared with the
ARXML writer's refusal.
"""

from __future__ import annotations

from collections.abc import Sequence

from swcraft_tools.data_description import APPLICATION_DATA_TYPE_CLASSES, DataDescription
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Composition,
    CSInterface,
    EnumDataType,
    ImplementationRecordDataType,
    Interface,
    RecordDataType,
    SRInterface,
    SWComponent,
    TextTable,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.xml_text import unrepresentable_text_sentence, xml_unrepresentable_character

# ---------------------------------------------------------------------------
# check_xml_representable_text
# ---------------------------------------------------------------------------


def xml_unrepresentable_text_message(owner: str, field: str, character: str) -> str:
    """The one sentence ``rule:text.xml-unrepresentable-character`` reports, and the writer refuses with.

    *owner* names the element (``"SWC 'A' port 'p'"``), *field* the free text (``"description"``),
    *character* the first character
    :func:`~swcraft_tools.xml_text.xml_unrepresentable_character` found. The sentence names the
    element and the field, never where the element is stored.
    """
    return unrepresentable_text_sentence(f"{owner} `{field}`", character)


def check_xml_representable_text(model_root: ModelInput) -> ConsistencyResult:
    """``rule:text.xml-unrepresentable-character``: one finding per free text XML 1.0 cannot carry.

    The texts are every ``description`` of every kind (those no written document carries
    included), a composition's ``title``, a base type's ``native_declaration`` and ``encoding``, an
    application type's ``unit`` (written exactly, into ``DISPLAY-NAME``) and a unit's
    ``display_name``. The predicate is the writer's own, so ``check-model`` reports every text
    ``generate arxml`` refuses; both releases are XML 1.0, so the version is not read.

    What it does **not** reach: a requirement's or a sequence's ``title`` and a value type's
    ``unit``, none of which the ARXML writer writes; the diagram writer and the needs export.
    """
    view = as_view(model_root)
    records: list[ConsistencyIssue] = []
    records.extend(_component_text_records(view.swcs))
    records.extend(_interface_text_records(view.interfaces))
    for connector in view.connectors:
        _judge_text(
            records,
            f"Connector '{connector.name}'",
            "description",
            connector.description,
            Subject("connectors", connector.name),
        )

    records.extend(_data_description_text_records(view.data_description))
    for mapping_set in view.mapping_sets:
        _judge_text(
            records,
            f"Mapping set '{mapping_set.name}'",
            "description",
            mapping_set.description,
            Subject("mapping_sets", mapping_set.name),
        )

    records.extend(_composition_text_records(view.compositions))
    for requirement in view.requirements:
        _judge_text(
            records,
            f"Requirement '{requirement.id}'",
            "description",
            requirement.description,
            Subject("requirements", requirement.id),
        )

    for task in view.tasks:
        _judge_text(records, f"Task '{task.name}'", "description", task.description, Subject("tasks", task.name))

    for hardware in view.hardware:
        _judge_text(
            records,
            f"Hardware {hardware.kind} '{hardware.name}'",
            "description",
            hardware.description,
            Subject("hardware", hardware.name),
        )

    return consistency_result(records)


def _judge_text(records: list[ConsistencyIssue], owner: str, field: str, text: str, element: Subject) -> None:
    """Append the finding on *element*'s *field* to *records* when *text* holds a character XML 1.0 cannot carry."""
    character = xml_unrepresentable_character(text)
    if character is not None:
        records.append(
            ConsistencyIssue(
                rule="text.xml-unrepresentable-character",
                message=xml_unrepresentable_text_message(owner, field, character),
                subjects=(element.at(field_step(field)),),
            )
        )


def _member_subject(element: Subject, collection: str, name: str) -> Subject:
    """The member *name* of *element*'s *collection*."""
    return element.at(field_step(collection), member_step(name))


def _component_text_records(swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """Every component's texts, and its ports', runnables', variables' and exclusive areas'."""
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        at = Subject("swc", swc.name)
        _judge_text(records, f"SWC '{swc.name}'", "description", swc.description, at)
        for port in swc.ports:
            _judge_text(
                records,
                f"SWC '{swc.name}' port '{port.name}'",
                "description",
                port.description,
                _member_subject(at, "ports", port.name),
            )
        for runnable in swc.runnables:
            _judge_text(
                records,
                f"SWC '{swc.name}' runnable '{runnable.name}'",
                "description",
                runnable.description,
                _member_subject(at, "runnables", runnable.name),
            )
        for variable in swc.variables:
            _judge_text(
                records,
                f"SWC '{swc.name}' variable '{variable.name}'",
                "description",
                variable.description,
                _member_subject(at, "variables", variable.name),
            )
        for area in swc.exclusive_areas:
            _judge_text(
                records,
                f"SWC '{swc.name}' exclusive area '{area.name}'",
                "description",
                area.description,
                _member_subject(at, "exclusive_areas", area.name),
            )
    return records


def _interface_text_records(interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """Every interface's texts, and its data elements', operations' and arguments'."""
    records: list[ConsistencyIssue] = []
    for interface in interfaces:
        at = Subject("interfaces", interface.name)
        _judge_text(records, f"Interface '{interface.name}'", "description", interface.description, at)
        if isinstance(interface, SRInterface):
            for element in interface.data_elements:
                _judge_text(
                    records,
                    f"Interface '{interface.name}' data element '{element.name}'",
                    "description",
                    element.description,
                    _member_subject(at, "data_elements", element.name),
                )
        elif isinstance(interface, CSInterface):
            for operation in interface.operations:
                operation_at = _member_subject(at, "operations", operation.name)
                _judge_text(
                    records,
                    f"Interface '{interface.name}' operation '{operation.name}'",
                    "description",
                    operation.description,
                    operation_at,
                )
                for argument in operation.arguments:
                    _judge_text(
                        records,
                        f"Interface '{interface.name}' operation '{operation.name}' argument '{argument.name}'",
                        "description",
                        argument.description,
                        _member_subject(operation_at, "arguments", argument.name),
                    )
    return records


def _data_description_text_records(description: DataDescription) -> list[ConsistencyIssue]:
    """Every data type's texts, then every unit's, compu method's and data constraint's."""
    records: list[ConsistencyIssue] = []
    for data_type in description.data_types:
        at = Subject(
            "base_types"
            if isinstance(data_type, BaseDataType)
            else "application_types"
            if isinstance(data_type, APPLICATION_DATA_TYPE_CLASSES)
            else "implementation_types",
            data_type.name,
        )
        _judge_text(records, f"Data type '{data_type.name}'", "description", data_type.description, at)
        if isinstance(data_type, BaseDataType):
            _judge_text(
                records, f"Base type '{data_type.name}'", "native_declaration", data_type.native_declaration, at
            )
            _judge_text(records, f"Base type '{data_type.name}'", "encoding", data_type.encoding, at)
        elif isinstance(data_type, EnumDataType):
            for literal in data_type.literals or ():
                _judge_text(
                    records,
                    f"Enum '{data_type.name}' literal '{literal.name}'",
                    "description",
                    literal.description,
                    _member_subject(at, "literals", literal.name),
                )
        elif isinstance(data_type, RecordDataType):
            for record_field in data_type.fields:
                _judge_text(
                    records,
                    f"Record '{data_type.name}' field '{record_field.name}'",
                    "description",
                    record_field.description,
                    _member_subject(at, "fields", record_field.name),
                )
        elif isinstance(data_type, ImplementationRecordDataType):
            for implementation_element in data_type.elements:
                _judge_text(
                    records,
                    f"Implementation record '{data_type.name}' element '{implementation_element.name}'",
                    "description",
                    implementation_element.description,
                    _member_subject(at, "elements", implementation_element.name),
                )
        elif isinstance(data_type, ApplicationDataType):
            _judge_text(records, f"Application type '{data_type.name}'", "unit", data_type.unit, at)

    # Every declared element is judged, referenced or not.
    for owner, field_name, text, at in data_description_element_texts(description):
        _judge_text(records, owner, field_name, text, at)
    return records


def _composition_text_records(compositions: Sequence[Composition]) -> list[ConsistencyIssue]:
    """Every composition's texts, and its ports', members' and delegations'."""
    records: list[ConsistencyIssue] = []
    for composition in compositions:
        at = Subject("compositions", composition.name)
        _judge_text(records, f"Composition '{composition.name}'", "title", composition.title, at)
        _judge_text(records, f"Composition '{composition.name}'", "description", composition.description, at)
        for composition_port in composition.ports:
            _judge_text(
                records,
                f"Composition '{composition.name}' port '{composition_port.name}'",
                "description",
                composition_port.description,
                _member_subject(at, "ports", composition_port.name),
            )
        for composition_member in composition.members:
            _judge_text(
                records,
                f"Composition '{composition.name}' member '{composition_member.name}'",
                "description",
                composition_member.description,
                _member_subject(at, "members", composition_member.name),
            )
        for delegation in composition.delegations:
            _judge_text(
                records,
                f"Composition '{composition.name}' delegation '{delegation.name}'",
                "description",
                delegation.description,
                _member_subject(at, "delegations", delegation.name),
            )
    return records


def data_description_element_texts(description: DataDescription) -> tuple[tuple[str, str, str, Subject], ...]:
    """Every free text of every declared unit, compu method and data constraint, as ``(owner, field, text, subject)``.

    A unit's ``display_name``, each element's ``description`` and a table entry's ``description``,
    for every declared element, referenced or not, in the view's order; a table entry's subject
    carries its member step. A table entry's ``name`` is an identifier
    (``rule:identifier.declared``) and is not among them.
    """
    texts: list[tuple[str, str, str, Subject]] = []
    for unit in description.units:
        at = Subject("units", unit.name)
        texts.append((f"Unit '{unit.name}'", "display_name", unit.display_name, at))
        texts.append((f"Unit '{unit.name}'", "description", unit.description, at))
    for compu_method in description.compu_methods:
        at = Subject("compu_methods", compu_method.name)
        texts.append((f"Compu method '{compu_method.name}'", "description", compu_method.description, at))
        if isinstance(compu_method, TextTable):
            texts.extend(
                (
                    f"Compu method '{compu_method.name}' entry '{entry.name}'",
                    "description",
                    entry.description,
                    at.at(field_step("entries"), member_step(entry.name)),
                )
                for entry in compu_method.entries
            )
    for data_constr in description.data_constraints:
        texts.append(
            (
                f"Data constraint '{data_constr.name}'",
                "description",
                data_constr.description,
                Subject("data_constraints", data_constr.name),
            )
        )
    return tuple(texts)
