"""The generic element writer: one element file per call.

:func:`write_element` and :func:`remove_element` write or remove **one element file** each, and
:func:`edited_text` computes what an ``update`` of a stored element writes instead of the whole dump.

This module imports nothing from ``swcraft_tools.editing``: the caller composes the model-root-relative *target*,
and every segment of it is validated before any filesystem access, so a write cannot leave ``model_root``.

**Serialization is derived, not restated.** :func:`element_to_dict` walks the element's ``model_fields`` in
declaration order; the fields omitted at a default the derived rule does not cover are declared in the contract
(``fieldDecl.omitAtDefault``).

**A line edit is proved, and its failure is a whole write.** :func:`edited_text` reads the edited text back and
compares it strictly with the element; a refusal, a mismatch or any exception falls back to dumping the whole
file. The read-back proves **values only**: it sees no comment, no undeclared key and no layout.

**Emitted ``SHORT-NAME``s are refused before anything is written**, by ``mutations.py``'s guards; an element class
neither :data:`_NAME_GUARDS` nor :data:`_NO_EMITTED_SHORT_NAME` classifies is a ``TypeError``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from swcraft_tools.model_registry import current_registry
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CallReturnedEvent,
    Composition,
    Connector,
    Core,
    CSInterface,
    DataConstr,
    DataDescriptionElement,
    DataEvent,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Linear,
    MappingSet,
    Memory,
    OperationEvent,
    Partition,
    PointerDataType,
    Processor,
    RecordDataType,
    ReferenceDataType,
    Requirement,
    Sequence,
    SRInterface,
    SWComponent,
    Task,
    TextTable,
    Unit,
    ValueDataType,
)
from swcraft_tools.undeclared_keys import undeclared_keys
from swcraft_tools.validator import derived_event_names

# The generated ``SHORT-NAME`` forms, taken from the emitter that owns them rather
# than restated. ``write_element`` writes a whole component's event list in one act,
# so an event name is checked in its final, counter-resolved shape
# (``validator.derived_event_names``), not as ``{prefix}{runnable}``.
from swcraft_tools.writer.arxml_generation import (
    BUDGET_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    emits_swc_timing,
    event_element,
)
from swcraft_tools.writer.mutations import (
    audit,
    check_call_returned_event_identifier,
    check_data_event_identifier,
    check_data_type_identifiers,
    check_generated_identifier,
    check_interface_init_constants,
    check_operation_event_identifier,
    check_port_init_values_writable,
    check_reserved_data_type_name,
    check_reserved_swc_name,
    check_runnable_access_names,
    check_variable_init_values_writable,
    require_identifier,
)
from swcraft_tools.yaml_load import load_yaml_document
from swcraft_tools.yaml_text_edit import FormRefused, edit_element_text, same_value

# ---------------------------------------------------------------------------
# The two refusals a caller can act on
# ---------------------------------------------------------------------------


class TargetExistsError(ValueError):
    """``create=True`` and a file is already at the composed target.

    Typed because a caller that located the target for an identity (the editing protocol) answers it with a code.
    The protocol allocates a create's file over a directory listing, so a well-formed request meets this only when a
    file appears between that listing and the write. ``target`` is the model-root-relative path, the only form a
    caller may pass on: an absolute path into a private staging tree never crosses a protocol boundary.
    """

    def __init__(self, target: str, message: str) -> None:
        super().__init__(message)
        self.target = target


class TargetMissingError(ValueError):
    """:func:`remove_element` was asked for a target that holds no file.

    A well-formed protocol request meets this only when the file disappears between the lookup and the removal.
    """

    def __init__(self, target: str, message: str) -> None:
        super().__init__(message)
        self.target = target


# ---------------------------------------------------------------------------
# The target path
# ---------------------------------------------------------------------------

#: Characters no segment of a target may contain. ``/`` is the separator the
#: registry joins with, so it is split on rather than rejected.
_ILLEGAL_SEGMENT_CHARS = ("\\", "\x00")


def _resolve_target(model_root: str, target: str) -> Path:
    """``model_root`` + a composed relative *target* -> the file to write.

    Runs before any filesystem access, so a refusal touched nothing. Every segment must be a single legal path
    segment, restating ``swcraft_tools.model_registry.segment_refusal`` because a writer may not trust its caller.

    Raises:
        ValueError: *target* is empty, absolute, contains a backslash or NUL, or
            has a segment that is empty, ``.``, or contains ``..``.
    """
    if not isinstance(target, str):
        raise ValueError(f"target must be a string, not {type(target).__name__}")
    if not target or not target.strip():
        raise ValueError("target must not be empty")
    if any(char in target for char in _ILLEGAL_SEGMENT_CHARS):
        raise ValueError(f"Invalid target '{target}': must not contain '\\' or NUL")
    if target.startswith("/"):
        raise ValueError(f"Invalid target '{target}': must be relative to model_root")
    for segment in target.split("/"):
        if not segment or not segment.strip():
            raise ValueError(f"Invalid target '{target}': it has an empty path segment")
        if segment == "." or ".." in segment:
            raise ValueError(f"Invalid target '{target}': segment '{segment}' is not a single path segment")

    root = Path(model_root)
    path = root.joinpath(target)
    # Lexical, not ``resolve()``d: the segment guard above already makes escape
    # impossible, and this is the assertion of it that costs no filesystem call.
    if root not in path.parents:
        raise ValueError(f"Invalid target '{target}': it would write outside {model_root}")
    return path


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _is_omitted_at_default(model: type[BaseModel], field_name: str, field_info: Any) -> bool:
    """Whether *field* disappears from the file when it still holds its default.

    The DERIVED rule drops an optional field still holding ``None``, ``""`` or ``{}`` -- not every falsy default:
    ``Task.runnables`` (default ``[]``) is written. The DECLARED exception, ``fieldDecl.omitAtDefault``, omits a field
    whose default lies outside that rule (e.g. ``field:swc.swc.external`` at ``False``). The registry is read per call,
    so a contract under ``use_registry`` is honoured and a class no contract binds takes the derived rule.
    """
    if field_info.is_required():
        return False
    kind = current_registry().kind_of_model(model)
    declared = None if kind is None else kind.get_field(field_info.alias or field_name)
    if declared is not None and declared.omit_at_default:
        return True
    default = field_info.default
    if default is None:
        return True
    return (isinstance(default, str) and default == "") or (isinstance(default, dict) and not default)


def _dump_value(value: Any) -> Any:
    """A YAML-serialisable form of one field value, models included."""
    if isinstance(value, BaseModel):
        return element_to_dict(value)
    if isinstance(value, list):
        return [_dump_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump_value(item) for key, item in value.items()}
    return value


def element_to_dict(element: BaseModel) -> dict[str, Any]:
    """The YAML dict the model files are written in, for any model element.

    Fields come out in ``schema.py`` declaration order, under their alias where one is declared
    (``Connector.from_`` is written as ``from``), and a field still holding an omittable default is left out, so
    existing YAML round-trips byte-identically.
    """
    model = type(element)
    data: dict[str, Any] = {}
    for name, field_info in model.model_fields.items():
        value = getattr(element, name)
        if _is_omitted_at_default(model, name, field_info) and value == field_info.default:
            continue
        data[field_info.alias or name] = _dump_value(value)
    return data


# ---------------------------------------------------------------------------
# The emitted-SHORT-NAME guards
# ---------------------------------------------------------------------------


def _check_swc_names(model_root: str, swc: SWComponent) -> None:
    """Every ``SHORT-NAME`` writing one component file emits.

    The declared name, its generated component-side forms, one per port, variable, exclusive area and runnable, one
    per event in the **final** shape the retroactive counter gives it, and one per entry of every runnable's access
    lists (through the shared :func:`~swcraft_tools.writer.mutations.check_runnable_access_names`, so this path and
    ``add_runnable`` cannot disagree). A variable's or port's ``init`` the dump could not write is refused too.

    A stated behaviour or event name is written as it is, so it is checked as a declared name, and the generated form
    it replaces is not checked.
    """

    require_identifier(swc.name, "SWC name")
    stated_behaviour = swc.behavior_name
    # A stub emits no element, so its generated forms are not charged (as in
    # `check-model`): otherwise the protocol would be stricter than the gate.
    # A stated behaviour name replaces the derived behaviour form.
    generated = ((IMPLEMENTATION_SUFFIX, "SWC-IMPLEMENTATION"),) + (
        ((INTERNAL_BEHAVIOR_SUFFIX, "SWC-INTERNAL-BEHAVIOR"),) if stated_behaviour is None else ()
    )
    for suffix, element in () if swc.external else generated:
        check_generated_identifier(
            swc.name, f"{swc.name}{suffix}", len(suffix), f"SWC '{swc.name}' generated {element} name"
        )
    if stated_behaviour is not None:
        require_identifier(stated_behaviour, f"SWC '{swc.name}' stated SWC-INTERNAL-BEHAVIOR name")
    check_reserved_swc_name(model_root, swc.name, external=swc.external, budgeted=emits_swc_timing(swc))

    # A stub's port emits no SHORT-NAME, but its name is the last segment of
    # every connector end on it, so it is checked too.
    for port in swc.ports:
        require_identifier(port.name, f"SWC '{swc.name}' port name")
    # A variable's name is its `VARIABLE-DATA-PROTOTYPE`'s SHORT-NAME; that it does
    # not collide with a runnable is `check-model`'s (`name.name-space-collision`).
    for variable in swc.variables:
        require_identifier(variable.name, f"SWC '{swc.name}' variable name")
    # An area's name is its `EXCLUSIVE-AREA`'s SHORT-NAME (collisions are
    # `check-model`'s); a `can_enter` or `runs_inside` entry is a reference.
    for area in swc.exclusive_areas:
        require_identifier(area.name, f"SWC '{swc.name}' exclusive area name")
    for runnable in swc.runnables:
        require_identifier(runnable.name, f"SWC '{swc.name}' runnable name")
        # `{runnable}_Budget` is emitted only for a runnable stating `wcet`.
        if runnable.wcet is not None:
            check_generated_identifier(
                runnable.name,
                f"{runnable.name}{BUDGET_SUFFIX}",
                len(BUDGET_SUFFIX),
                f"SWC '{swc.name}' runnable '{runnable.name}' generated EXECUTION-TIME-CONSTRAINT name",
            )

    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    for (runnable, event), derived in zip(pairs, derived_event_names(swc), strict=True):
        stated = event.name
        if stated is not None:
            require_identifier(
                stated, f"SWC '{swc.name}' runnable '{runnable.name}' stated {event_element(event)} name"
            )
            continue
        if isinstance(event, DataEvent):
            # `DRT_{port}_{element}`: the pair's joint budget, the overhead read off
            # the final, collision-resolved name.
            check_data_event_identifier(swc.name, runnable.name, event.port, event.element, derived)
            continue
        if isinstance(event, OperationEvent):
            # `OIT_{port}_{operation}`: the pair's joint budget, as above.
            check_operation_event_identifier(swc.name, runnable.name, event.port, event.operation, derived)
            continue
        if isinstance(event, CallReturnedEvent):
            # `ASRT_{port}_{operation}`: the pair's joint budget, never the
            # single-name budget the fall-through below charges.
            check_call_returned_event_identifier(swc.name, runnable.name, event.port, event.operation, derived)
            continue
        # The writer's own element for the kind, one arm per kind: the
        # derivation above already refused any kind it was not taught.
        check_generated_identifier(
            runnable.name,
            derived,
            len(derived) - len(runnable.name),
            f"SWC '{swc.name}' runnable '{runnable.name}' generated {event_element(event)} name",
        )

    for runnable in swc.runnables:
        check_runnable_access_names(swc.name, runnable)

    # Not a name: an `init` the dump could not write, refused before the dump.
    check_port_init_values_writable(swc)
    check_variable_init_values_writable(swc)


def _check_sr_interface_names(model_root: str, iface: SRInterface) -> None:
    """The interface name, every data element name, and the generated ``{Interface}_{Element}_Init`` form.

    That form's budget belongs to a *pair* of declared names. The shared
    :func:`~swcraft_tools.writer.mutations.check_interface_init_constants` keeps this path and ``create_interface``
    in agreement; it takes the **elements** because it also refuses an ``init`` ``yaml.safe_dump`` cannot write,
    which must be refused before the dump on both write paths.
    """
    require_identifier(iface.name, "Interface name")
    for element in iface.data_elements:
        require_identifier(element.name, f"Interface '{iface.name}' data element name")
    check_interface_init_constants(model_root, iface.name, iface.data_elements)


def _check_cs_interface_names(model_root: str, iface: CSInterface) -> None:
    """The interface name, every operation name and every argument name.

    A client-server interface derives no init constant, but its name is a sibling of every constant in
    ``PortInterfaces``, so it owes the reverse half of that collision: the same guard, with no data elements.
    """
    require_identifier(iface.name, "Interface name")
    for operation in iface.operations:
        require_identifier(operation.name, f"Interface '{iface.name}' operation name")
        for argument in operation.arguments:
            require_identifier(argument.name, f"Interface '{iface.name}' operation '{operation.name}' argument name")
    check_interface_init_constants(model_root, iface.name, [])


def _check_connector_names(model_root: str, connector: Connector) -> None:
    """The connector name, which becomes an ``ASSEMBLY-SW-CONNECTOR`` ``SHORT-NAME``.

    The endpoints are ``*-REF`` paths into names other files declare, so they are
    checked where they are declared, not here.
    """
    del model_root
    require_identifier(connector.name, "Connector name")


def _check_composition_names(model_root: str, composition: Composition) -> None:
    """Every ``SHORT-NAME`` writing one composition file emits.

    The composition, one per boundary port, one per member and one per delegation. A member's ``type`` and a
    delegation's references are ``*-REF`` paths checked where they are declared. No generated form lengthens any
    of the four.
    """
    del model_root
    require_identifier(composition.name, "Composition name")
    for port in composition.ports:
        require_identifier(port.name, f"Composition '{composition.name}' port name")
    for member in composition.members:
        require_identifier(member.name, f"Composition '{composition.name}' member name")
    for delegation in composition.delegations:
        require_identifier(delegation.name, f"Composition '{composition.name}' delegation name")


def _check_data_type_names(model_root: str, dt: Any) -> None:
    """The declared name, the kind-scoped generated forms and every member name."""
    require_identifier(dt.name, "Data type name")
    check_data_type_identifiers(dt)
    check_reserved_data_type_name(model_root, dt)


def _check_mapping_set_names(model_root: str, mapping_set: MappingSet) -> None:
    """The set's own name, which becomes a ``DATA-TYPE-MAPPING-SET`` ``SHORT-NAME``.

    No generated form lengthens it; a map's ``application`` and ``implementation`` are ``*-REF`` paths checked where
    they are declared.
    """
    del model_root
    require_identifier(mapping_set.name, "Mapping set name")


#: The word a name finding opens with for each data description element class, keyed by the
#: exact class as :data:`_NAME_GUARDS` is.
_DATA_DESCRIPTION_ELEMENT_LABELS: dict[type[BaseModel], str] = {
    Unit: "Unit",
    TextTable: "Compu method",
    Linear: "Compu method",
    DataConstr: "Data constraint",
}


def _check_data_description_element_names(model_root: str, element: DataDescriptionElement) -> None:
    """A unit's, a compu method's or a data constraint's own name, and a table's entry names.

    Each element is a ``UNIT``, a ``COMPU-METHOD`` or a ``DATA-CONSTR`` whose ``SHORT-NAME`` is its ``name``. A
    ``texttable``'s entry name is written as its scale's ``SHORT-LABEL`` and ``VT``, exactly as an enumeration
    literal's, and is checked as one. A ``linear``'s ``unit`` is a ``*-REF`` checked where it is declared.
    """
    del model_root
    label = _DATA_DESCRIPTION_ELEMENT_LABELS.get(type(element))
    if label is None:
        raise TypeError(f"no name guard labels the element kind {type(element).__name__}")
    require_identifier(element.name, f"{label} name")
    if isinstance(element, TextTable):
        for entry in element.entries:
            require_identifier(entry.name, f"Compu method '{element.name}' entry name")


#: One guard per element kind that writes an AUTOSAR ``SHORT-NAME``, keyed by the
#: exact Pydantic class so a discriminated union's members are separate rows.
_NAME_GUARDS: dict[type[BaseModel], Callable[[str, Any], None]] = {
    SWComponent: _check_swc_names,
    Composition: _check_composition_names,
    SRInterface: _check_sr_interface_names,
    CSInterface: _check_cs_interface_names,
    Connector: _check_connector_names,
    BaseDataType: _check_data_type_names,
    ValueDataType: _check_data_type_names,
    ReferenceDataType: _check_data_type_names,
    EnumDataType: _check_data_type_names,
    RecordDataType: _check_data_type_names,
    ArrayDataType: _check_data_type_names,
    # The application type is an ordinary data type to this table.
    ApplicationDataType: _check_data_type_names,
    MappingSet: _check_mapping_set_names,
    # Each is an IMPLEMENTATION-DATA-TYPE; `check_data_type_identifiers` checks the
    # record's elements and the array's sub-element. None generates a form of its name.
    ImplementationRecordDataType: _check_data_type_names,
    ImplementationArrayDataType: _check_data_type_names,
    PointerDataType: _check_data_type_names,
    # The data description layers: each element a SHORT-NAME of its own, no generated form.
    Unit: _check_data_description_element_names,
    TextTable: _check_data_description_element_names,
    Linear: _check_data_description_element_names,
    DataConstr: _check_data_description_element_names,
}

#: The element kinds that emit no ``SHORT-NAME`` at all (the scope
#: ``validator.check_identifier_conformance`` states): requirements, tasks,
#: sequences and hardware are never emitted into ARXML, and applying an AUTOSAR
#: constraint to them would invent a rule the standard does not ask for.
#: ``layer:hardware`` is ``arxmlBound: false`` in the contract; that flag and this
#: row move together. A stub is emitted as no element (a reference carries its
#: ``external_ref`` verbatim); its name is still checked by ``check-model``.
_NO_EMITTED_SHORT_NAME: tuple[type[BaseModel], ...] = (
    Requirement,
    Task,
    Sequence,
    Processor,
    Core,
    Memory,
    Partition,
    ExternalInterface,
    ExternalDataType,
)


def _check_emitted_short_names(model_root: str, element: BaseModel) -> None:
    """The name policy for *element*'s kind, run, or the kind refused outright.

    Fail-closed on purpose: an element class in neither table is a ``TypeError`` rather than an unchecked write.
    """
    guard = _NAME_GUARDS.get(type(element))
    if guard is not None:
        guard(model_root, element)
        return
    if isinstance(element, _NO_EMITTED_SHORT_NAME):
        return
    raise TypeError(
        f"write_element has no emitted-SHORT-NAME policy for {type(element).__name__}; "
        "classify it in _NAME_GUARDS or _NO_EMITTED_SHORT_NAME before writing it"
    )


# ---------------------------------------------------------------------------
# The two operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditedText:
    """What an update or a rename's create writes: the edited *text*, or ``None`` and the *reason* to write whole.

    ``in_writer_form`` says of a file with no text that it already stands exactly as the writer writes the element,
    so a whole write loses nothing and there is nothing to warn of; the audit's ``form`` still says ``whole``.

    ``dropped_keys`` is filled on a fallback alone: the undeclared keys of the text that would have been edited, in
    document order, which the whole write then drops. Raw model text, unbounded: the review bounds what it shows.
    """

    text: str | None
    reason: str | None
    in_writer_form: bool = False
    dropped_keys: tuple[str, ...] = ()


#: The reason of an edit whose result does not read back as the element.
READ_BACK_MISMATCH = "the edited file did not read back as the model's value"

#: The reason of an exception :func:`edited_text` did not expect, filled with the
#: class name alone: an exception's message may hold model text, and a reason
#: reaches the review.
UNEXPECTED_EXCEPTION = "the line edit stopped on an unexpected {}"


def _dump_element(data: dict[str, Any]) -> str:
    """The one statement of an element file's dump: the whole write's text, and ``written``."""
    # `allow_unicode` is deliberately left at its default (False): non-ASCII text
    # comes back as `\uXXXX` escapes, the intended output. One dialect for both
    # write paths (`mutations.py`'s dumps match it), so an element written either
    # way has the same bytes; and the escaped form reads back to the identical
    # string -- except a lone surrogate, which the reader refuses. Turning it on is
    # a repo-wide decision covering every dump site and the byte-identity fixtures.
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


def stands_in_writer_form(text: str, element: BaseModel) -> bool:
    """Whether *text* is exactly the file the writer writes for *element*.

    Such a file holds no comment, no undeclared key and the writer's layout, so writing it whole loses nothing.
    Staging asks it of a removed element's file before the removal.
    """
    return text == _dump_element(element_to_dict(element))


def undeclared_keys_of(text: str | None, element: BaseModel) -> tuple[str, ...]:
    """The keys of *text*, a file of *element*'s kind, that no field declares, in document order; raises nothing.

    What a whole write of that file drops beyond comments and layout, judged by
    :func:`~swcraft_tools.undeclared_keys.undeclared_keys`, the predicate ``check-model`` reports through. Every
    occurrence is a result. **What it does not reach:** a text that is ``None``, does not parse as YAML, or belongs to
    a class no layer binds gives ``()``. Only the parse failure is caught: the predicate raises nothing by contract,
    so a defect in it fails here as loudly as in ``check-model`` instead of reading as "no dropped keys".
    """

    if text is None:
        return ()
    kind = current_registry().kind_of_model(type(element))
    if kind is None:
        return ()
    try:
        document = load_yaml_document(text)
    except yaml.YAMLError:
        return ()
    return tuple(key.key for key in undeclared_keys(text, document, kind.layer))


def _read_back_holds(text: str, element: BaseModel, new: dict[str, Any]) -> bool:
    """Whether *text*, read and validated as *element*'s class, is *new*, *element*'s dictionary, type-strict."""
    read_back = type(element).model_validate(load_yaml_document(text))
    return same_value(element_to_dict(read_back), new)


def edited_text(
    model_root: str,
    target: str,
    element: BaseModel,
    prior: Callable[[], BaseModel],
    *,
    source_text: str | None = None,
) -> EditedText:
    """The text that turns the stored file at *target* into *element*, changing only the lines it needs.

    Writes nothing, and raises nothing but a defect of :func:`undeclared_keys_of`'s predicate. A refused form, a
    result that does not read back as *element*, and any other exception give no text and a reason, and
    :func:`write_element` then dumps the file whole, so a defect of the line edit cannot stop an edit. The edit is
    proved in its values alone. A fallback names the undeclared keys its whole write drops, so the review can say so
    before the approval.

    ``except Exception`` is deliberate and the only such site: the read-back already stands between a defect and the
    file, and ``test_change_set_text_oracle.py`` asserts no such reason occurs over its corpus.

    Args:
        model_root: root of the ``model/`` directory.
        target: the model-root-relative path of the stored file.
        element: the validated element to hold.
        prior: builds the element the file holds, a callable so that its validation runs inside this guard.
        source_text: the text to edit in place of *target*'s, which is then not read. A rename's create passes the
            removed element's text here, so the only line the edit changes is the identity's.
    """
    text = source_text
    in_writer_form = False
    try:
        if text is None:
            text = _resolve_target(model_root, target).read_bytes().decode("utf-8")
        new = element_to_dict(element)
        old = element_to_dict(prior())
        written = _dump_element(new)
        try:
            changed = edit_element_text(text, old, new, written)
        except FormRefused as refused:
            reason = str(refused)
            # Dumped only here: an update that edits never pays for the old side's dump.
            in_writer_form = text in (written, _dump_element(old))
        else:
            if _read_back_holds(changed, element, new):
                return EditedText(changed, None)
            reason = READ_BACK_MISMATCH
    except Exception as unexpected:  # noqa: BLE001 -- fallback: see the docstring
        reason = UNEXPECTED_EXCEPTION.format(type(unexpected).__name__)
        in_writer_form = False
    # Asked once, outside the guard: a defect of the predicate fails loudly (`undeclared_keys_of`).
    return EditedText(None, reason, in_writer_form, undeclared_keys_of(text, element))


def write_element(
    model_root: str,
    target: str,
    element: BaseModel,
    auditor: Any = None,
    *,
    create: bool = False,
    edited: EditedText | None = None,
) -> str:
    """One element file written at *model_root*/*target*.

    The file is re-serialized whole from *element* unless *edited* holds a text, which lands in its place; the guards
    run either way.

    Args:
        target: the model-root-relative path the caller placed the element at, e.g. ``swc/sensors/Foo.yaml``, a
            collision-suffixed one (``swc/sensors/Foo~1.yaml``), or the file a re-written element was found in.
        element: the already-validated Pydantic element (a dict is not accepted).
        auditor: one entry per call; ``args`` carries the *target* and the element's ``layer`` and ``identity``.
        create: when true the target must not already exist.
        edited: what :func:`edited_text` computed; passed, ``args`` gain ``form`` (``"lines"`` or ``"whole"``), and
            ``reason`` on ``"whole"``.

    Returns:
        Absolute path string of the written file.

    Raises:
        TargetExistsError: *create* is true and a file is already at *target*.
        ValueError: *target* is not a composition of single path segments, or an emitted ``SHORT-NAME`` is not a
            legal AUTOSAR ``Identifier`` or is one the ARXML writer reserves. Nothing is written.
        TypeError: *element* is not a Pydantic model, or is of a class no name policy classifies.
    """
    if not isinstance(element, BaseModel):
        raise TypeError(f"element must be a schema model instance, not {type(element).__name__}")

    path = _resolve_target(model_root, target)
    _check_emitted_short_names(model_root, element)

    if create and path.exists():
        # The model-root-relative target, never the absolute path: an absolute path
        # into a private staging tree must not cross a protocol boundary.
        raise TargetExistsError(target, f"An element file already exists at '{target}'")

    data = element_to_dict(element)

    # The layer directory is materialized only as a side effect of writing an
    # element, never as a requestable act.
    path.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"`: model files are committed, fingerprinted and byte-compared
    # ("an untouched element is reproduced byte for byte"); a Windows default would
    # translate every `\n` to `\r\n` and move the baseline for no semantic change.
    # It translates nothing, so an edited text's CRLF lands exactly.
    text = _dump_element(data) if edited is None or edited.text is None else edited.text
    path.write_text(text, encoding="utf-8", newline="\n")

    args = _writer_args(target, _address_of(element))
    if edited is not None:
        args["form"] = "whole" if edited.text is None else "lines"
        if edited.reason is not None:
            args["reason"] = edited.reason
    audit(auditor, "write_element", args, str(path))
    return str(path)


def _address_of(element: BaseModel) -> tuple[str, str] | None:
    """``(layer, identity)`` of *element*, from the kernel registry's kind of its class, or ``None``."""
    registry = current_registry()
    kind = registry.kind_of_model(type(element))
    if kind is None:
        return None
    identity = getattr(element, registry.layer(kind.layer).identity_field, None)
    return (kind.layer, identity) if isinstance(identity, str) else None


def _writer_args(target: str, address: tuple[str, str] | None) -> dict[str, str]:
    """A writer entry's ``args``: the target, and the address beside it when one is known."""
    if address is None:
        return {"target": target}
    layer, identity = address
    return {"target": target, "layer": layer, "identity": identity}


def remove_element(
    model_root: str,
    target: str,
    auditor: Any = None,
    *,
    address: tuple[str, str] | None = None,
) -> str:
    """The element file at *model_root*/*target*, removed; the directory is left in place, even when now empty.

    Args:
        target: the model-root-relative path of the file the element is in.
        auditor: one audit entry, shaped like :func:`write_element`'s.
        address: ``(layer, identity)`` of the element the file holds, recorded beside the target. Omitted, the entry
            records the target alone.

    Returns:
        Absolute path string of the removed file.

    Raises:
        TargetMissingError: no file exists at *target*.
        ValueError: *target* is not a composition of single path segments.
    """
    path = _resolve_target(model_root, target)
    if not path.is_file():
        raise TargetMissingError(target, f"No element file to remove at '{target}'")

    path.unlink()

    audit(auditor, "remove_element", _writer_args(target, address), str(path))
    return str(path)
