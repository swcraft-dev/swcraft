"""ARXML generation: builds an :class:`ArxmlDocument` from model_root and saves it.

Owns ``build_workspace()`` (the single-argument seam ``writer/__init__.py`` exports), ``generate_arxml()`` and its
CLI shim (``swcraft-generate-arxml``), and the helpers other modules of the package import. The AUTOSAR version
enters at ``generate_arxml()``.

Some generated artifacts (``{enum}_CompuMethod``, ``TMT_``/``IT_`` event names, provide-first port order, the
default com-specs, ...) are kept because readers depend on them;
``packages/tools/tests/fixtures/arxml_golden/README.md`` lists each with its consumer. Not written: a
``{base}_impl`` twin (a base type is its ``SW-BASE-TYPE`` alone, and every implementation type is one the model
declares) and ``PORT-API-OPTIONS`` (the model states no port API option, and an absent one means both features off).
``SYMBOL`` mirrors the runnable's name only where the runnable states no ``symbol``.

**No emitter branches on the version, and none should without a schema reason.** For the element subset SWCraft
emits, 4.3.0 output differs from 4.2.2 output only in the ``xsi:schemaLocation`` filename token, so the version is
consumed by ``ArxmlDocument``'s root attributes alone.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple

from lxml import etree

from swcraft_tools import validator
from swcraft_tools.arxml_version import (
    DEFAULT_ARXML_VERSION,
    HANDLE_TERMINATION_AND_RESTART_LITERALS,
    ROOT_PACKAGE_NAMES,
    ArxmlVersion,
)
from swcraft_tools.capability_markers import ACCESS_POINTS, COMPONENT_EXPORT_MARKERS, SDG_GID, Marker
from swcraft_tools.data_description import DataDescription, read_data_description
from swcraft_tools.execution_budget import parse_budget
from swcraft_tools.guarantee import required, required_instance
from swcraft_tools.identifier import IDENTIFIER_PATTERN, MAX_IDENTIFIER_LENGTH, check_identifier
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_interfaces,
    read_mapping_sets,
    read_swc_model,
)
from swcraft_tools.schema import (
    ACCESS_LIST_NEEDS,
    INTERFACE_KIND_PROSE,
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CallReturnedEvent,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    ConversionLimits,
    CSArgument,
    CSInterface,
    DataConstr,
    DataDescriptionElement,
    DataElement,
    DataEvent,
    DataType,
    Delegation,
    EnumDataType,
    EnumLiteral,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    Linear,
    LinearLimits,
    MappingSet,
    OperationCallRef,
    OperationEvent,
    PointerDataType,
    Port,
    PortComSpec,
    RecordDataType,
    ReferenceDataType,
    Runnable,
    SRInterface,
    SWComponent,
    TableEntry,
    TextTable,
    TimingEvent,
    Unit,
    ValueDataType,
    Variable,
)
from swcraft_tools.validator import (
    SEVERITY_ERROR,
    STUB_MEMBER_LISTS,
    NotResolved,
    access_point_collision_repair,
    application_type_double_mappings,
    async_call_records,
    check_operation_events,
    components_without_termination_handling,
    composite_map_difference,
    conversion_unit_records,
    data_description_element_records,
    data_description_element_texts,
    data_description_package_path,
    data_event_records,
    data_type_layer,
    data_type_references,
    delegation_context,
    derived_event_names,
    disabled_mode_records,
    duplicate_event_names,
    emitted_access_point_names,
    exclusive_area_records,
    implementation_table,
    implicit_read_records,
    inter_runnable_variable_records,
    interface_compatibility,
    is_unwritable_integer,
    mapped_pointer,
    name_space_collisions,
    port_com_spec_records,
    port_init_records,
    queued_read_records,
    queued_receiver_records,
    result_point_sites,
    runnable_event_collisions,
    runnable_symbol_records,
    service_composition_port_records,
    shared_access_point_names,
    stated_access_point_names,
    stated_literals,
    stub_member_list,
    stub_member_miss,
    stub_member_names,
    termination_handling_message,
    variable_access_entries,
    variable_init_records,
    xml_unrepresentable_text_message,
)
from swcraft_tools.validator.compatibility import is_queued
from swcraft_tools.validator.data_types import (
    COMPOSITE_MAP_REPAIR,
    MAP_APPLICATION_KINDS,
    MAP_MAY_APPLICATION_KINDS,
    MAP_MAY_APPLICATION_REPAIR,
    MAP_MAY_IMPLEMENTATION_KINDS,
    MAP_MAY_IMPLEMENTATION_REPAIR,
    variable_application_types,
)
from swcraft_tools.validator.description_elements import chain_text
from swcraft_tools.validator.init_values import external_type_chain, first_elements, reference_chain_end
from swcraft_tools.writer.arxml_xml import (
    ArxmlDocument,
    ArxmlPackage,
    PackagePlacement,
    ShortNameScope,
    format_float,
    format_init_number,
    named_element,
    reference_element,
    set_description,
    set_long_name,
    sub_element,
)
from swcraft_tools.writer.composition_structure import (
    check_composition_hierarchy,
    check_external_stubs,
    compositions_by_name,
    member_type_ref,
    root_composition,
)
from swcraft_tools.xml_text import xml_unrepresentable_character

if TYPE_CHECKING:
    from swcraft_tools.validator import StatedTable

# The writer generates twenty ``SHORT-NAME`` forms from declared names. Each one
# lengthens a declared name, so each is charged against ``MAX_IDENTIFIER_LENGTH``
# where it is emitted; the forms built from two declared names share a joint budget.

#: Suffix of the ``COMPU-METHOD`` generated for each enum.
#: ``arxml_import/type_mapping.py``'s enum reduction keys on this exact name, so
#: it is a contract, not a formatting choice.
COMPU_METHOD_SUFFIX = "_CompuMethod"

#: The ``UNIT``, ``COMPU-METHOD`` and ``DATA-CONSTR`` an
#: ``APPLICATION-PRIMITIVE-DATA-TYPE`` of category ``VALUE`` derives from its
#: own ``SHORT-NAME``. Each lengthens one declared name, so each has a budget of
#: its own: 123 for ``_Unit``, 122 for the other two.
#:
#: ``{name}_CompuMethod`` is the enum's form and a different reservation, so an
#: application type's method is ``{name}_Compu`` and the two can never be one
#: name.
UNIT_SUFFIX = "_Unit"
COMPU_SUFFIX = "_Compu"
RANGE_SUFFIX = "_Range"

#: The generated forms reserved per declared field rather than per kind, as
#: ``(suffix, emitted element, the kind that emits it, the fields any one of
#: which makes the writer emit it)``: the presence rule
#: :func:`generated_data_type_forms` evaluates. A form is declared when any of
#: its fields is set; a guard stricter than that would refuse names generation
#: accepts. One ``_Range`` suffix serves the application type's ``range`` and
#: the value type's ``value_min`` / ``value_max`` (its ``INTERNAL-CONSTRS``), so
#: one rule (``rule:data-type.range-name-collision``) reserves both.
#:
#: Every guard reads this table rather than restating it, except
#: ``validator.py``, for the reason :data:`RESERVED_DATA_TYPE_RULES` states;
#: ``packages/tools/tests/test_validator_identifier.py`` pins that restatement.
RESERVED_DATA_TYPE_FORM_RULES: tuple[tuple[str, str, type[DataType], tuple[str, ...]], ...] = (
    (UNIT_SUFFIX, "UNIT", ApplicationDataType, ("unit",)),
    (COMPU_SUFFIX, "COMPU-METHOD", ApplicationDataType, ("conversion",)),
    (RANGE_SUFFIX, "DATA-CONSTR", ApplicationDataType, ("range",)),
    (RANGE_SUFFIX, "DATA-CONSTR", ValueDataType, ("value_min", "value_max")),
)


def generated_application_forms(application_type: ApplicationDataType) -> frozenset[str]:
    """Return which suffixes *application_type* generates: :func:`generated_data_type_forms`' application half.

    Call :func:`generated_data_type_forms` instead. A blank ``unit`` is no unit; a ``conversion: {}`` is a
    conversion.
    """
    forms: set[str] = set()
    if application_type.unit.strip():
        forms.add(UNIT_SUFFIX)
    if application_type.conversion is not None:
        forms.add(COMPU_SUFFIX)
    if application_type.range is not None:
        forms.add(RANGE_SUFFIX)
    return frozenset(forms)


def generated_data_type_forms(dt: DataType) -> frozenset[str]:
    """Return which of :data:`RESERVED_DATA_TYPE_FORM_RULES`' suffixes *dt* generates.

    An application type's forms, and a value type's ``{name}_Range`` when a ``value_min`` or ``value_max`` is
    declared; every other kind generates none. Both emitters and every reserved-name guard ask this function.
    ``validator.py``'s ``generated_data_type_forms`` restates it, pinned by
    ``packages/tools/tests/test_validator_identifier.py``.
    """
    if isinstance(dt, ApplicationDataType):
        return generated_application_forms(dt)
    if isinstance(dt, ValueDataType) and (dt.value_min is not None or dt.value_max is not None):
        return frozenset({RANGE_SUFFIX})
    return frozenset()


#: Suffix of the ``SWC-INTERNAL-BEHAVIOR`` generated for each component, nested
#: in its ``INTERNAL-BEHAVIORS``; ``arxml_reader.py``'s traversal keys on it. It
#: is written where the component states no ``behavior_name``
#: (:func:`_behavior_name`).
INTERNAL_BEHAVIOR_SUFFIX = "_InternalBehavior"

#: Suffix of the ``SWC-IMPLEMENTATION`` generated for each component, a
#: package-level sibling emitted right after it.
IMPLEMENTATION_SUFFIX = "_Implementation"

#: Suffix of the ``SWC-TIMING`` emitted after a component's
#: ``SWC-IMPLEMENTATION`` when a runnable declares a ``wcet``
#: (:func:`emits_swc_timing`). It is not charged by ``rule:identifier.generated``:
#: ``{swc}_Implementation`` is charged on every non-external component and is
#: longer, so it always fails first.
TIMING_SUFFIX = "_Timing"

#: Suffix of the ``EXECUTION-TIME-CONSTRAINT`` emitted in that ``SWC-TIMING``'s
#: ``TIMING-REQUIREMENTS`` for each runnable declaring a ``wcet``; charged by
#: ``rule:identifier.generated`` there. It reserves no package-level name: the
#: ``SWC-TIMING`` holds nothing else, and runnable names are already unique
#: ignoring case in their behaviour.
BUDGET_SUFFIX = "_Budget"

#: ``SHORT-NAME`` prefix of a runnable's ``TIMING-EVENT``.
TIMING_EVENT_PREFIX = "TMT_"

#: ``SHORT-NAME`` prefix of a runnable's ``INIT-EVENT``.
INIT_EVENT_PREFIX = "IT_"

#: ``SHORT-NAME`` prefix of a runnable's ``OPERATION-INVOKED-EVENT``,
#: ``OIT_{port}_{operation}``. Derived from the event's pair rather than the
#: runnable's name, so one event inserted among many renumbers no other; the
#: port and operation names get 123 characters between them, before any suffix
#: ``_unique_event_names`` adds.
OPERATION_EVENT_PREFIX = "OIT_"

#: ``SHORT-NAME`` prefix of a runnable's ``DATA-RECEIVED-EVENT``,
#: ``DRT_{port}_{element}``, derived from the pair as ``OIT_`` is: the port and
#: element names get 123 characters between them, before any suffix
#: ``_unique_event_names`` adds. ``validator.GENERATED_DATA_EVENT_PREFIX``
#: restates it.
DATA_EVENT_PREFIX = "DRT_"

#: Suffix of the ``CONSTANT-SPECIFICATION`` generated for each sender-receiver
#: data element that declares an ``init``: ``{Interface}_{Element}_Init``, built
#: from two declared names.
#:
#: Not ``SR_{Interface}_{Element}_Init``: a sender-receiver interface is often
#: itself named ``SR_…`` and the prefix would read ``SR_SR_…``.
INIT_CONSTANT_SUFFIX = "_Init"

#: ``SHORT-NAME`` prefix of the ``VARIABLE-ACCESS`` a runnable's ``reads`` entry
#: is emitted as, ``R_{port}_{element}``, whatever the receive form: ``by`` is
#: no part of the entry's identity, so only the container differs.
#:
#: The three prefixes are SWCraft's derivation rule; a source file may add a
#: de-duplication suffix of its own to a call point, which SWCraft does not
#: reproduce.
READ_ACCESS_PREFIX = "R_"

#: ``SHORT-NAME`` prefix of the ``VARIABLE-ACCESS`` a runnable's ``writes`` entry
#: is emitted as, ``W_{port}_{element}``.
WRITE_ACCESS_PREFIX = "W_"

#: ``SHORT-NAME`` prefix of the ``SYNCHRONOUS-SERVER-CALL-POINT`` a runnable's
#: ``calls`` entry is emitted as, ``Call_{port}_{operation}``.
CALL_POINT_PREFIX = "Call_"

#: ``SHORT-NAME`` prefix of the ``VARIABLE-ACCESS`` a ``reads_variables`` entry
#: stating no ``name`` is emitted as in ``READ-LOCAL-VARIABLES``,
#: ``RV_{variable}``, budget 125. Its second character ``V`` keeps a derived
#: ``RV_`` name from equalling a derived ``R_`` one in the runnable's name space.
READ_VARIABLE_PREFIX = "RV_"

#: ``SHORT-NAME`` prefix of the ``VARIABLE-ACCESS`` a ``writes_variables`` entry
#: is emitted as in ``WRITTEN-LOCAL-VARIABLES``, ``WV_{variable}``, budget 125.
WRITE_VARIABLE_PREFIX = "WV_"

#: ``SHORT-NAME`` prefix of the ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` an
#: asynchronous call's poll or a ``call_returned`` event is emitted as when it
#: states no ``result``: ``Result_{port}_{operation}``, 120 characters between
#: the two names. Its third character ``s`` keeps it from equalling a derived
#: ``R_`` name. ``validator.GENERATED_RESULT_POINT_PREFIX`` restates it.
RESULT_POINT_PREFIX = "Result_"

#: ``SHORT-NAME`` prefix of the ``ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT`` a
#: ``call_returned`` event stating no ``name`` is emitted as,
#: ``ASRT_{port}_{operation}``, derived from the pair as ``OIT_`` is: 122
#: characters between the two names, before any suffix ``_unique_event_names``
#: adds. ``validator.GENERATED_RETURNS_EVENT_PREFIX`` restates it.
RETURNS_EVENT_PREFIX = "ASRT_"

#: ``TIMEOUT`` of every ``SYNCHRONOUS-SERVER-CALL-POINT``. The schema makes it
#: optional; it is emitted anyway so the call point states its timeout, and an
#: RTE generator reads ``0`` as "no timeout". ``format_float`` prints it as ``0``.
_CALL_POINT_TIMEOUT = 0.0

#: The six access containers of a ``RUNNABLE-ENTITY``, in the order both
#: licensed schemas (4.2.2 and 4.3.0) require: a container after ``SYMBOL``, or
#: ``DATA-SEND-POINTS`` before ``DATA-RECEIVE-POINT-BY-ARGUMENTS``, is refused.
#: The two implicit containers hold ``by: implicit`` entries. A container is
#: emitted only when it has an entry, so a runnable that declares nothing emits
#: no byte for it.
_ACCESS_CONTAINER_ORDER = (
    "DATA-READ-ACCESSS",
    "DATA-RECEIVE-POINT-BY-ARGUMENTS",
    "DATA-RECEIVE-POINT-BY-VALUES",
    "DATA-SEND-POINTS",
    "DATA-WRITE-ACCESSS",
    "SERVER-CALL-POINTS",
)

#: The container each read form is emitted in: absent (by argument), ``value``
#: and ``implicit`` -- the ``VariableAccess`` in the role ``dataReadAccess``
#: (``[constr_2002]``, SWCT R4.2.2 ``:25318``, R4.3.0 ``:28436``), the RTE's
#: ``Rte_IRead``.
_READ_CONTAINERS: dict[str | None, str] = {
    None: "DATA-RECEIVE-POINT-BY-ARGUMENTS",
    "value": "DATA-RECEIVE-POINT-BY-VALUES",
    "implicit": "DATA-READ-ACCESSS",
}

#: ``QUEUE-LENGTH`` of the default ``SERVER-COM-SPEC``. The model has no field
#: for it, so this is the only value.
SERVER_QUEUE_LENGTH = 1


class ComSpecAttribute(NamedTuple):
    """One multiplicity-1 sender/receiver com-spec attribute a ``port_com_spec`` field carries.

    ``field`` is the ``PortComSpec`` field, ``silent`` the value the writer
    states when the model is silent (the no-mechanism value of an attribute the
    standard gives no default), and ``literals`` the XSD literal of each model
    value of an enumeration, ``None`` for a boolean or a number.
    """

    field: str
    silent: bool | float | str
    literals: Mapping[str, str] | None = None


#: The six ``port_com_spec`` attributes with a silent value, keyed by XSD tag,
#: in the order a non-queued receiver com-spec holds them (XSD 4.2.2 and 4.3.0
#: alike). One table for both directions: :func:`_emit_com_specs` writes each
#: ``silent`` value where the model is silent, and the importer records a source
#: value only where it differs (``arxml_import.merge.com_spec_values``), so the
#: two cannot disagree about what silent means. A queued receiver's
#: ``QUEUE-LENGTH`` is not here: it is multiplicity 1 with no default, so a
#: model that needs one states it.
COM_SPEC_SILENT_VALUES: dict[str, ComSpecAttribute] = {
    "HANDLE-OUT-OF-RANGE": ComSpecAttribute(
        "handle_out_of_range",
        "none",
        {"none": "NONE", "default": "DEFAULT", "ignore": "IGNORE", "saturate": "SATURATE"},
    ),
    "USES-END-TO-END-PROTECTION": ComSpecAttribute("uses_end_to_end_protection", False),
    "ALIVE-TIMEOUT": ComSpecAttribute("alive_timeout", 0.0),
    "ENABLE-UPDATE": ComSpecAttribute("enable_update", False),
    "HANDLE-NEVER-RECEIVED": ComSpecAttribute("handle_never_received", False),
    "HANDLE-TIMEOUT-TYPE": ComSpecAttribute("handle_timeout_type", "none", {"none": "NONE", "replace": "REPLACE"}),
}

#: A data element's ``sw_impl_policy`` -> its ``SW-IMPL-POLICY`` literal
#: (``SwImplPolicyEnum``, XSD 4.2.2 and 4.3.0). The importer's
#: ``arxml_import.type_mapping._SW_IMPL_POLICY_VALUES`` is this table inverted.
#: ``measurementPoint`` has no row: the model does not carry it.
SW_IMPL_POLICY_LITERALS: dict[str, str] = {"standard": "STANDARD", "queued": "QUEUED"}

#: A data element's ``sw_calibration_access`` ->
#: its ``SW-CALIBRATION-ACCESS`` literal (``SwCalibrationAccessEnum``, Table 5.48
#: of SWCT R4.2.2, 5.49 of R4.3.0). The importer's
#: ``arxml_import.type_mapping._SW_CALIBRATION_ACCESS_VALUES`` is this table
#: inverted.
SW_CALIBRATION_ACCESS_LITERALS: dict[str, str] = {
    "not_accessible": "NOT-ACCESSIBLE",
    "read_only": "READ-ONLY",
    "read_write": "READ-WRITE",
}

#: The only component element this writer emits (:func:`_check_swc_type` rejects
#: every other ``SWComponent.type``). It is both the element tag and the ``DEST``
#: a composition's ``SW-COMPONENT-PROTOTYPE/TYPE-TREF`` carries.
APPLICATION_SWC_TAG = "APPLICATION-SW-COMPONENT-TYPE"

#: The element tag every declared composition is emitted as, which is also the
#: ``DEST`` a ``SW-COMPONENT-PROTOTYPE/TYPE-TREF`` naming one carries — the
#: composition-side companion of :data:`APPLICATION_SWC_TAG`.
COMPOSITION_SWC_TAG = "COMPOSITION-SW-COMPONENT-TYPE"

#: The element tag each interface kind is emitted as, which is also the ``DEST``
#: a ``PROVIDED-``/``REQUIRED-INTERFACE-TREF`` pointing at it must carry — the
#: interface-side companion of :data:`_TYPE_REF_DEST`.
_INTERFACE_REF_DEST: dict[type, str] = {
    SRInterface: "SENDER-RECEIVER-INTERFACE",
    CSInterface: "CLIENT-SERVER-INTERFACE",
}

#: The element tag each declared data type kind is emitted as, which is also the
#: ``DEST`` any ``TYPE-TREF`` pointing at it must carry. Every implementation
#: kind is an ``IMPLEMENTATION-DATA-TYPE`` (the record, array and pointer of
#: their own category). A base type has no row: AUTOSAR types a data prototype
#: by an ``AutosarDataType`` and a ``SwBaseType`` is not one, so the lookup
#: answers ``None`` and every caller turns that into a refusal. The key set is
#: the set :func:`resolve_type_ref` resolves.
_TYPE_REF_DEST: dict[type, str] = {
    ValueDataType: "IMPLEMENTATION-DATA-TYPE",
    ReferenceDataType: "IMPLEMENTATION-DATA-TYPE",
    ImplementationRecordDataType: "IMPLEMENTATION-DATA-TYPE",
    ImplementationArrayDataType: "IMPLEMENTATION-DATA-TYPE",
    PointerDataType: "IMPLEMENTATION-DATA-TYPE",
    ApplicationDataType: "APPLICATION-PRIMITIVE-DATA-TYPE",
    EnumDataType: "APPLICATION-PRIMITIVE-DATA-TYPE",
    RecordDataType: "APPLICATION-RECORD-DATA-TYPE",
    ArrayDataType: "APPLICATION-ARRAY-DATA-TYPE",
}

#: The composite-member twin of :data:`_TYPE_REF_DEST`. An
#: ``APPLICATION-RECORD-ELEMENT``'s and an array ``ELEMENT``'s ``TYPE-TREF`` may
#: only name an ``APPLICATION-*`` type (both licensed schemas enumerate the
#: allowed ``DEST`` values), so the four rows are the four kinds of the
#: ``application_types`` layer. An implementation type's name is an undeclared
#: name of that layer, refused by :func:`_record_field_ref` and
#: :func:`_array_element_ref` before anything is emitted; a base type has no
#: row, for the reason :data:`_TYPE_REF_DEST` gives.
_APPLICATION_TYPE_REF_DEST: dict[type, str] = {
    ApplicationDataType: "APPLICATION-PRIMITIVE-DATA-TYPE",
    EnumDataType: "APPLICATION-PRIMITIVE-DATA-TYPE",
    RecordDataType: "APPLICATION-RECORD-DATA-TYPE",
    ArrayDataType: "APPLICATION-ARRAY-DATA-TYPE",
}

#: The declared kinds a composite member may name, as the
#: ``isinstance`` tuple :func:`_resolve_application_type_ref` dispatches on. It is
#: :data:`_APPLICATION_TYPE_REF_DEST`'s key set, written beside it so the two
#: cannot answer differently about one kind.
_APPLICATION_MEMBER_KINDS = (ApplicationDataType, EnumDataType, RecordDataType, ArrayDataType)

#: The ``CATEGORY`` each application kind is emitted with, and so the one a
#: composite member naming it carries (:func:`_application_member_category`).
#: Keyed like :data:`_APPLICATION_TYPE_REF_DEST`. The four type emitters and the
#: two member emitters read this one table, so a member's category cannot drift
#: from its type's: ``[constr_1152]`` (SWCT R4.2.2 ``:11938``, R4.3.0
#: ``:12545``) requires an ``ApplicationArrayElement``'s category to be
#: *"identical to the value of category of the AutosarDataType referenced"*.
#: For a record element no such constraint was found; Table 5.7
#: (``[constr_1006]``) defines ``VALUE`` as a single value and ``STRUCTURE`` as
#: one holding further elements, so the same rule is applied to both members.
_APPLICATION_CATEGORY: dict[type, str] = {
    ApplicationDataType: "VALUE",
    EnumDataType: "VALUE",
    RecordDataType: "STRUCTURE",
    ArrayDataType: "ARRAY",
}

#: Every data-type ``SHORT-NAME`` the writer generates, as
#: ``(suffix, emitted element, the kinds a base must have)`` — the single
#: statement of what :func:`_check_data_type_names` enforces below.
#:
#: The kind scope is the load-bearing part: the writer emits ``{x}_CompuMethod``
#: only for an enum ``x``, so a guard that ignores the base's kind refuses names
#: generation accepts. Every write-path guard reads this table
#: (``writer/mutations.py``'s ``check_reserved_data_type_name``,
#: ``component_exchange/name_policies.py``'s ``reserved_name_violations``).
#: ``validator.py`` restates the constants because it must stay importable
#: without ``writer``; ``packages/tools/tests/test_validator_name_uniqueness.py``
#: pins the two together.
#:
#: Forms reserved per declared field rather than per kind are
#: :data:`RESERVED_DATA_TYPE_FORM_RULES`, evaluated by
#: :func:`generated_data_type_forms`.
RESERVED_DATA_TYPE_RULES: tuple[tuple[str, str, tuple[type[DataType], ...]], ...] = (
    (COMPU_METHOD_SUFFIX, "COMPU-METHOD", (EnumDataType,)),
)


def _check_identifiers(candidates: Iterable[tuple[str, str]], context: str) -> None:
    """Refuse every candidate ``SHORT-NAME`` that is not a legal AUTOSAR ``Identifier``.

    The fail-closed twin of ``validator.check_identifier_conformance()``; both read :mod:`swcraft_tools.identifier`.
    Every offender of one call is batched into one ``ValueError``.

    Args:
        candidates: ``(subject, name)`` pairs, where *name* is the string actually emitted (a generated form in its
            final shape) and *subject* says where it comes from.
        context: the layer directory the message names, relative to the model root and never where the model is
            stored, e.g. ``"swc/"``.

    Raises:
        ValueError: naming every offending candidate, its reason, and the rule.
    """
    offenders = []
    for subject, name in candidates:
        reason = check_identifier(name)
        if reason is not None:
            offenders.append(f"{subject}: '{name}' {reason}")
    if offenders:
        raise ValueError(
            f"Non-conforming SHORT-NAME(s) in {context}: "
            + "; ".join(offenders)
            + f". Every emitted SHORT-NAME must be a legal AUTOSAR Identifier (matching "
            f"{IDENTIFIER_PATTERN}, at most {MAX_IDENTIFIER_LENGTH} characters); "
            "'swcraft check-model' reports every offender with a suggested name."
        )


def _package_candidate(owner: str, package: str | None) -> list[tuple[str, str]]:
    """Return the one ``(subject, name)`` candidate an element's declared ``package`` adds, or none.

    The sub-package is a ``SHORT-NAME`` of its own, checked by :func:`_check_identifiers` beside the element's name
    (``rule:identifier.declared``).
    """
    return [] if package is None else [(f"{owner} package", package)]


def _referenced_type(name: str, type_registry: DataDescription, *, implementation: bool) -> DataType | None:
    """Return the declaration a reference to *name* names, asked of the layer its role targets.

    An implementation role (a reference type's ``base``, an implementation record's or array's element, a pointer's
    target, a map's implementation side, an ``implementation_type``) names an implementation type; an application
    role an application type.
    """
    if implementation:
        return type_registry.implementation(name)
    return type_registry.application(name)


def _prototype_type(prototype: DataElement | CSArgument | Variable) -> tuple[str, bool]:
    """Return the type a data element, an argument or a variable states, as ``(name, application)``.

    ``application_type`` when it is set, else ``implementation_type`` (the schema
    admits exactly one): the writer's reading of ``validator.prototype_type_ref``.
    """
    if prototype.application_type is not None:
        return prototype.application_type, True
    return required(prototype.implementation_type, "exactly_one_type_field admits one type"), False


def resolve_type_ref(name: str, type_registry: DataDescription, *, implementation: bool) -> str | None:
    """Resolve a declared data type name to the absolute path it is emitted at.

    A base type resolves to None: a ``SW-BASE-TYPE`` is not an ``AutosarDataType``, so no ``TYPE-TREF`` may name
    one. An external type stub resolves to its ``external_ref`` verbatim.

    Args:
        implementation: *name* fills an implementation role, so it is asked of the implementation side alone; the
            application side otherwise.

    Returns:
        The absolute AUTOSAR path for the named type, or None for an undeclared name or a base type.
    """
    dt = _referenced_type(name, type_registry, implementation=implementation)
    if isinstance(dt, ExternalDataType):
        return dt.external_ref
    if dt is not None and type(dt) in _TYPE_REF_DEST:
        return _placed_path(_home_of(dt), name)
    return None


def _type_ref_dest(name: str, type_registry: DataDescription, *, implementation: bool) -> str | None:
    """Return the ``DEST`` a ``TYPE-TREF`` pointing at *name* must carry.

    Returns:
        The referent's element tag, or None for an undeclared name; for an external type stub, its own
        ``external_dest``, verbatim.
    """
    dt = _referenced_type(name, type_registry, implementation=implementation)
    if isinstance(dt, ExternalDataType):
        return dt.external_dest
    return _TYPE_REF_DEST.get(type(dt))


def _resolve_application_type_ref(name: str, type_registry: DataDescription) -> str | None:
    """Resolve *name* to the path a **composite member** must reference it by.

    AUTOSAR allows only an ``APPLICATION-*`` ``DEST`` on an ``APPLICATION-RECORD-ELEMENT``'s and an array
    ``ELEMENT``'s ``TYPE-TREF``, so a member resolves in the ``application_types`` layer alone.

    Returns:
        The absolute AUTOSAR path, or None for an undeclared name, a base type or an implementation type.
    """
    if isinstance(type_registry.application(name), _APPLICATION_MEMBER_KINDS):
        return _placed_path(_HOME.APPLICATION_TYPES, name)
    return None


def _application_type_ref_dest(name: str, type_registry: DataDescription) -> str | None:
    """Return the ``DEST`` a composite member's ``TYPE-TREF`` to *name* must carry."""
    return _APPLICATION_TYPE_REF_DEST.get(type(type_registry.application(name)))


def _application_member_category(name: str, type_registry: DataDescription) -> str:
    """Return the ``CATEGORY`` of a composite member typed by *name*: its type's category.

    ``VALUE`` for an ``application_type`` or an ``enum``, ``STRUCTURE`` for a ``record``, ``ARRAY`` for an ``array``
    (``[constr_1152]``).

    Raises:
        ValueError: if *name* is not a declared application kind (a last line of defence).
    """
    category = _APPLICATION_CATEGORY.get(type(type_registry.application(name)))
    if category is None:
        raise ValueError(f"Data type '{name}' is not declared")
    return category


def _check_data_type_names(data_types: list[DataType]) -> None:
    """Reject duplicate declared names and collisions with auto-generated ones.

    The identifier check runs first: a name that is not a legal AUTOSAR
    ``Identifier`` produces schema-invalid ARXML whatever else is true of it,
    and a collision between two illegal names would name the wrong problem. The
    declared name is checked, and so is every generated form the declaration
    mints (:data:`RESERVED_DATA_TYPE_RULES` and
    :func:`generated_data_type_forms`), so no guard here is stricter than the
    emitters.

    Raises:
        ValueError: on a declared or generated name that is not a legal AUTOSAR
            ``Identifier``; on a duplicate declared name in one layer; on a
            declared ``{x}_CompuMethod`` / ``{x}_Unit`` / ``{x}_Compu`` /
            ``{x}_Range`` name of the owner's package that collides with the
            element generated for a type ``{x}`` that mints that form.
    """
    # A generated form is only checked when the declared name itself conforms:
    # otherwise one bad name is refused twice over, once as itself and once as
    # its generated form.
    identifier_candidates: list[tuple[str, str]] = []
    for dt in data_types:
        if not isinstance(dt, ExternalDataType):  # a stub is emitted nowhere and has no `package`
            identifier_candidates.extend(_package_candidate(f"Data type '{dt.name}'", dt.package))
        if check_identifier(dt.name) is not None:
            identifier_candidates.append(("Data type name", dt.name))
            continue
        for suffix, what, base_kinds in RESERVED_DATA_TYPE_RULES:
            if generates_kind_form(dt, base_kinds):
                identifier_candidates.append((f"Data type '{dt.name}' generated {what} name", f"{dt.name}{suffix}"))
        minted = generated_data_type_forms(dt)
        for suffix, what, kind, _fields in RESERVED_DATA_TYPE_FORM_RULES:
            if isinstance(dt, kind) and suffix in minted:
                identifier_candidates.append((f"Data type '{dt.name}' generated {what} name", f"{dt.name}{suffix}"))
    _check_identifiers(identifier_candidates, "data_types/")

    # Duplicates are per layer: types of two layers may share a name. The
    # refusal names each name's layer, as the validator's records do.

    _dt_keys = [(data_type_layer(dt), dt.name) for dt in data_types]
    _dt_dups = sorted({key for key in _dt_keys if _dt_keys.count(key) > 1}, key=lambda key: (key[1], key[0]))
    if _dt_dups:
        listed = ", ".join(f"'{name}' ({layer})" for layer, name in _dt_dups)
        raise ValueError(f"Duplicate data type names: {listed}")

    # An external type stub is left out of the collision checks: nothing is
    # emitted for it, so it occupies no SHORT-NAME. A generated form lies in its
    # owner's package, so only a declared type of that package (`_home_of`, the
    # validator's answer too) collides with it.
    _emitting_types = [dt for dt in data_types if not isinstance(dt, ExternalDataType)]
    _enum_homes = {
        dt.name: _home_of(dt) for dt in data_types if generates_kind_form(dt, (EnumDataType,))
    }  # an enumeration naming a texttable generates no form
    _compu_method_collisions = [
        dt.name
        for dt in _emitting_types
        if dt.name.endswith(COMPU_METHOD_SUFFIX)
        and _enum_homes.get(dt.name[: -len(COMPU_METHOD_SUFFIX)]) == _home_of(dt)
    ]
    if _compu_method_collisions:
        raise ValueError(
            f"Data type name(s) {sorted(_compu_method_collisions)} collide with auto-generated "
            "'{base}_CompuMethod' COMPU-METHOD names. Rename the conflicting types."
        )

    # The field-scoped forms: an application type with no `unit` mints no
    # "{x}_Unit", so a declared type of that name collides with nothing.
    _form_elements = dict.fromkeys((suffix, what) for suffix, what, _kind, _fields in RESERVED_DATA_TYPE_FORM_RULES)
    _minted_stems: dict[str, set[tuple[PackagePlacement.Home, str]]] = {
        suffix: set() for suffix, _what in _form_elements
    }
    for dt in data_types:
        for suffix in generated_data_type_forms(dt):
            _minted_stems[suffix].add((_home_of(dt), dt.name))
    for suffix, what in _form_elements:
        collisions = [
            dt.name
            for dt in _emitting_types
            if dt.name.endswith(suffix) and (_home_of(dt), dt.name[: -len(suffix)]) in _minted_stems[suffix]
        ]
        if collisions:
            raise ValueError(
                f"Data type name(s) {sorted(collisions)} collide with auto-generated "
                f"'{{base}}{suffix}' {what} names. Rename the conflicting types."
            )


def init_constant_name(interface_name: str, element_name: str) -> str:
    """Return the ``SHORT-NAME`` of one data element's init constant.

    Not injective: interface ``A_B`` with element ``C`` and interface ``A`` with element ``B_C`` both give
    ``A_B_C_Init`` (``rule:interface.init-constant-name-collision``).
    """
    return f"{interface_name}_{element_name}{INIT_CONSTANT_SUFFIX}"


#: How many characters :func:`init_constant_name` adds to the two declared names
#: it joins: the separating ``_`` and :data:`INIT_CONSTANT_SUFFIX`. The budget is
#: joint: the two names share ``MAX_IDENTIFIER_LENGTH - INIT_CONSTANT_ADDED_LENGTH``
#: = 122 characters.
INIT_CONSTANT_ADDED_LENGTH = len(INIT_CONSTANT_SUFFIX) + 1


def access_point_name(prefix: str, port: str, member: str) -> str:
    """Return the ``SHORT-NAME`` of one runnable access point.

    *prefix* is one of :data:`READ_ACCESS_PREFIX`, :data:`WRITE_ACCESS_PREFIX` and :data:`CALL_POINT_PREFIX`. Not
    injective: port ``a_b`` with element ``c`` and port ``a`` with element ``b_c`` both give ``R_a_b_c``
    (``rule:swc.access.name-collision``).
    """
    return f"{prefix}{port}_{member}"


#: Which of a runnable's three access lists each form is derived from; every
#: guard that must agree about these names takes the prefix from this table.
ACCESS_LIST_PREFIXES: dict[str, str] = {
    "reads": READ_ACCESS_PREFIX,
    "writes": WRITE_ACCESS_PREFIX,
    "calls": CALL_POINT_PREFIX,
}

#: Which of a runnable's two variable access lists each variable access form is
#: derived from, the writer's table that
#: ``validator.GENERATED_VARIABLE_ACCESS_PREFIXES`` restates.
VARIABLE_ACCESS_LIST_PREFIXES: dict[str, str] = {
    "reads_variables": READ_VARIABLE_PREFIX,
    "writes_variables": WRITE_VARIABLE_PREFIX,
}


def access_point_added_length(prefix: str) -> int:
    """Return how many characters :func:`access_point_name` adds to the two names it joins.

    The prefix and the separating ``_``: three for ``R_``/``W_`` and six for
    ``Call_``. The two declared names share
    ``MAX_IDENTIFIER_LENGTH - access_point_added_length(prefix)`` characters,
    125 and 122.
    """
    return len(prefix) + 1


def _init_constant_pairs(interfaces: list[Interface]) -> list[tuple[str, str]]:
    """Return every ``(interface, data element)`` pair the writer derives a constant for.

    Unconditionally, whether or not the element carries an ``init``: a
    conditional reservation would mean that adding the first initial value to
    one interface starts failing generation on a file nobody touched.
    """
    return [
        (iface.name, element.name)
        for iface in interfaces
        if isinstance(iface, SRInterface)
        for element in iface.data_elements
    ]


def _check_init_constant_names(interfaces: list[Interface]) -> None:
    """Reject the init constant form's overflows and its two collisions.

    Runs after :func:`_check_interface_names`' identifier check, so every declared name here already conforms.
    Neither collision is renamed or numbered.

    * The joint budget: ``{Interface}_{Element}_Init`` leaves the two names 122 characters between them; the message
      names both and suggests shortening the longer (on a tie, the data element).
    * Constant ↔ interface: the constants are ``PortInterfaces`` siblings of the interfaces.
    * Constant ↔ constant: the derivation is not injective (:func:`init_constant_name`).

    Raises:
        ValueError: on a pair whose derived constant is not a legal AUTOSAR ``Identifier``, on a constant colliding
            with a declared interface name, or on two pairs deriving one constant.
    """
    pairs = _init_constant_pairs(interfaces)
    budget = MAX_IDENTIFIER_LENGTH - INIT_CONSTANT_ADDED_LENGTH

    overflows = []
    for interface_name, element_name in pairs:
        emitted = init_constant_name(interface_name, element_name)
        reason = check_identifier(emitted)
        if reason is None:
            continue
        longer = interface_name if len(interface_name) > len(element_name) else element_name
        allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
        overflows.append(
            f"interface '{interface_name}' data element '{element_name}' generates "
            f"'{emitted}' ({len(emitted)} characters), which {reason} — '{interface_name}' and "
            f"'{element_name}' get {budget} characters between them, so shorten '{longer}' to at "
            f"most {allowance} characters"
        )
    if overflows:
        raise ValueError(
            f"Generated CONSTANT-SPECIFICATION name(s) in interfaces/ are not legal "
            f"AUTOSAR Identifiers: {'; '.join(overflows)}. Rename the conflicting names."
        )

    # An external interface stub emits no `SHORT-NAME`, so it collides with
    # nothing in `PortInterfaces`.
    declared = {iface.name for iface in interfaces if not isinstance(iface, ExternalInterface)}
    owners: dict[str, list[tuple[str, str]]] = {}
    for pair in pairs:
        owners.setdefault(init_constant_name(*pair), []).append(pair)

    against_interfaces = [
        f"'{emitted}' (auto-generated for interface '{pair[0]}' data element '{pair[1]}')"
        for emitted, claimants in sorted(owners.items())
        if emitted in declared
        for pair in claimants[:1]
    ]
    if against_interfaces:
        raise ValueError(
            f"Interface name(s) in interfaces/ collide with auto-generated "
            f"'{{interface}}_{{element}}{INIT_CONSTANT_SUFFIX}' CONSTANT-SPECIFICATION names: "
            f"{', '.join(against_interfaces)}. Rename the conflicting interfaces."
        )

    shared = [
        f"'{emitted}' is derived from "
        + " and ".join(f"interface '{pair[0]}' data element '{pair[1]}'" for pair in claimants)
        for emitted, claimants in sorted(owners.items())
        if len(claimants) > 1
    ]
    if shared:
        raise ValueError(
            f"Two or more data elements in interfaces/ derive one "
            f"CONSTANT-SPECIFICATION SHORT-NAME: {'; '.join(shared)}. An interface name and a data "
            "element name may both contain '_', so the derivation is not injective; rename one of "
            "the conflicting names."
        )


def runnable_access_entries(runnable: Runnable) -> list[tuple[str, str, str, str]]:
    """Return ``(list name, port, member field, member)`` for every entry *runnable* declares.

    The lists are read as named attributes rather than through ``getattr``: the
    coverage oracle binds a field read to the attribute it names.
    """
    entries = [("reads", read.port, "element", read.element) for read in runnable.reads]
    entries += [("writes", write.port, "element", write.element) for write in runnable.writes]
    entries += [("calls", call.port, "operation", call.operation) for call in runnable.calls]
    return entries


def _check_access_point_names(swcs: list[SWComponent]) -> None:
    """Reject the access-point and variable-access forms' overflows and collisions.

    Runs after :func:`_check_component_identifiers`, so every declared port and runnable name here already conforms.
    Every declared entry is checked, whether or not it resolves.

    * The joint budgets: ``R_``/``W_`` leave a port and member name 125 characters between them, ``Call_`` 122. The
      message names both and suggests shortening the longer (on a tie, the member).
    * Access point ↔ access point: the derivation is not injective (:func:`access_point_name`), and every access
      point of a runnable is a ``SHORT-NAME`` sibling of every other. A clash is reported, never renamed.

    Raises:
        ValueError: on an entry whose derived access point is not a legal AUTOSAR ``Identifier``, or on two entries
            of one runnable deriving one.
    """
    # The names compared are the ones generation writes: a stated `name`, else
    # the derived form. A stated name is checked as a declared name; only a
    # derived one is charged a joint budget here.

    overflows: list[str] = []
    shared: list[str] = []
    any_stated = False

    def pair_overflow(subject: str, port_name: str, member: str, emitted: str, prefix: str, tail: str = "") -> None:
        """Record the joint budget's line for a pair form *emitted* from *port_name* and *member* when it is illegal."""
        reason = check_identifier(emitted)
        if reason is None:
            return
        budget = MAX_IDENTIFIER_LENGTH - access_point_added_length(prefix)
        longer = port_name if len(port_name) > len(member) else member
        allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
        overflows.append(
            f"{subject} generates '{emitted}' ({len(emitted)} characters), which {reason} — "
            f"'{port_name}' and '{member}' get {budget} characters between them, so shorten "
            f"'{longer}' to at most {allowance} characters{tail}"
        )

    for swc in swcs:
        for runnable in swc.runnables:
            for (list_name, port_name, _member_field, member), emitted, stated in zip(
                runnable_access_entries(runnable),
                emitted_access_point_names(runnable),
                stated_access_point_names(runnable),
                strict=True,
            ):
                prefix = ACCESS_LIST_PREFIXES[list_name]
                if stated is not None:
                    continue  # written as it is; checked as a declared name by `_check_component_identifiers`
                if check_identifier(port_name) is not None or check_identifier(member) is not None:
                    continue  # already refused as a declared name, or refused by _access_point_plan
                pair_overflow(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{port_name}'.'{member}'",
                    port_name,
                    member,
                    emitted,
                    prefix,
                )
            # `RV_`/`WV_`, a single-name form (budget 125), charged only where no
            # name is stated and the variable name conforms: an illegal one is
            # already refused as a declared name (one root cause, one offender).
            for list_name, variable, stated in variable_access_entries(runnable):
                if stated is not None or check_identifier(variable) is not None:
                    continue
                prefix = VARIABLE_ACCESS_LIST_PREFIXES[list_name]
                emitted = f"{prefix}{variable}"
                reason = check_identifier(emitted)
                if reason is None:
                    continue
                overflows.append(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{variable}' generates "
                    f"'{emitted}' ({len(emitted)} characters), which {reason} — '{variable}' gets "
                    f"{MAX_IDENTIFIER_LENGTH - len(prefix)} characters, so shorten it to at most "
                    f"{MAX_IDENTIFIER_LENGTH - len(prefix)} characters"
                )
            # `Result_{port}_{operation}` for a result point stating no `result`:
            # a joint budget of 120, charged only when both names conform.
            for words, port_name, operation, stated, _path, stated_field in result_point_sites(swc.name, runnable):
                if stated is not None or check_identifier(port_name) is not None:
                    continue
                if check_identifier(operation) is not None:
                    continue
                pair_overflow(
                    words,
                    port_name,
                    operation,
                    _result_point_name(port_name, operation, None),
                    RESULT_POINT_PREFIX,
                    f", or state {stated_field}",
                )
            for emitted, claimants, written in shared_access_point_names(runnable):
                shared.append(
                    f"SWC '{swc.name}' runnable '{runnable.name}': '{emitted}' is "
                    f"{'written for' if written else 'derived from'} {claimants}"
                )
                any_stated = any_stated or written

    if overflows:
        raise ValueError(
            f"Generated access point SHORT-NAME(s) in swc/ are not legal AUTOSAR "
            f"Identifiers: {'; '.join(overflows)}. Rename the conflicting names, or state the entry's name."
        )
    if shared:
        raise ValueError(
            f"Two or more access entries of one runnable in swc/ are written with one access "
            f"point SHORT-NAME: {'; '.join(shared)}. {access_point_collision_repair(any_stated)}"
        )


def _duplicate_names(names: Iterable[str]) -> list[str]:
    """Return the names appearing more than once, sorted: the shared shape of every check below."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return sorted(duplicates)


def _check_interface_names(interfaces: list[Interface]) -> None:
    """Reject duplicate interface names, and duplicates *inside* an interface.

    A duplicate data element, operation or argument within one interface breaks ``DATA-ELEMENT-REF`` /
    ``OPERATION-REF``, which resolve to whichever the reader reaches first. Every nested name is also
    identifier-checked; the generated init-constant form is :func:`_check_init_constant_names`'.

    Raises:
        ValueError: on an interface, data element, operation or argument name that is not a legal AUTOSAR
            ``Identifier``; on a duplicate declared interface name; on a duplicate data element, operation or
            argument name within one interface; on a generated init-constant name that is not a legal ``Identifier``
            or collides with an interface or with another constant.
    """

    identifier_candidates: list[tuple[str, str]] = []
    for iface in interfaces:
        identifier_candidates.append(("Interface name", iface.name))
        if isinstance(iface, (SRInterface, CSInterface)):  # a stub is emitted nowhere and has no `package`
            identifier_candidates.extend(_package_candidate(f"Interface '{iface.name}'", iface.package))
        if isinstance(iface, SRInterface):
            identifier_candidates.extend(
                (f"Interface '{iface.name}' data element name", element.name) for element in iface.data_elements
            )
        elif isinstance(iface, ExternalInterface):
            # A listed name is written verbatim as the last segment of every
            # member path through the stub (`_member_path`).
            identifier_candidates.extend(
                (f"External interface stub '{iface.name}' {noun} name", member)
                for member_field, (_list, noun, _dest) in STUB_MEMBER_LISTS.items()
                for member in stub_member_list(iface, member_field)
            )
        elif isinstance(iface, CSInterface):
            for operation in iface.operations:
                identifier_candidates.append((f"Interface '{iface.name}' operation name", operation.name))
                identifier_candidates.extend(
                    (f"Interface '{iface.name}' operation '{operation.name}' argument name", argument.name)
                    for argument in operation.arguments
                )
    _check_identifiers(identifier_candidates, "interfaces/")

    duplicates = _duplicate_names(iface.name for iface in interfaces)
    if duplicates:
        raise ValueError(f"Duplicate interface names in interfaces/: {sorted(duplicates)}")

    for iface in interfaces:
        if isinstance(iface, SRInterface):
            element_duplicates = _duplicate_names(element.name for element in iface.data_elements)
            if element_duplicates:
                raise ValueError(
                    f"Interface '{iface.name}' in interfaces/ has duplicate data element "
                    f"name(s) {element_duplicates}; data element names must be unique within an interface."
                )
        elif isinstance(iface, CSInterface):
            operation_duplicates = _duplicate_names(operation.name for operation in iface.operations)
            if operation_duplicates:
                raise ValueError(
                    f"Interface '{iface.name}' in interfaces/ has duplicate operation "
                    f"name(s) {operation_duplicates}; operation names must be unique within an interface."
                )
            for operation in iface.operations:
                argument_duplicates = _duplicate_names(argument.name for argument in operation.arguments)
                if argument_duplicates:
                    raise ValueError(
                        f"Interface '{iface.name}' operation '{operation.name}' in "
                        f"interfaces/ has duplicate argument name(s) {argument_duplicates}; "
                        "argument names must be unique within an operation."
                    )

    # Last, so that a duplicate declared name is reported as the duplicate it is
    # rather than as a constant two identical pairs happen to share.
    _check_init_constant_names(interfaces)


def _collect_component_name_candidates(swc: SWComponent, identifier_candidates: list[tuple[str, str]]) -> None:
    """Append to *identifier_candidates* every declared and generated name of *swc* the identifier check covers.

    The component's package, its own name and the generated forms of it, its
    stated behaviour name, the names of its ports, runnables, variables and
    exclusive areas, the stated names of its access points and result points, and
    the ``{runnable}_Budget`` of a runnable that states a ``wcet``. The names of
    its events are :func:`_collect_event_name_candidates`'.

    Args:
        swc: the component read.
        identifier_candidates: the ``(subject, name)`` pairs collected so far,
            appended to in the order the pairs are reported.
    """
    stated_behaviour = swc.behavior_name
    identifier_candidates.extend(_package_candidate(f"SWC '{swc.name}'", swc.package))
    if check_identifier(swc.name) is not None:
        identifier_candidates.append(("SWC name", swc.name))
    elif not swc.external:
        # An external stub emits neither form, so checking them would refuse a
        # name generation accepts (a foreign 120-character SHORT-NAME is legal).
        # A stated behaviour name replaces the derived behaviour form.
        generated = ((IMPLEMENTATION_SUFFIX, "SWC-IMPLEMENTATION"),) + (
            ((INTERNAL_BEHAVIOR_SUFFIX, "SWC-INTERNAL-BEHAVIOR"),) if stated_behaviour is None else ()
        )
        identifier_candidates.extend(
            (f"SWC '{swc.name}' generated {element} name", f"{swc.name}{suffix}") for suffix, element in generated
        )
    # A stated SHORT-NAME is written as it is, so it is checked as a declared name.
    if stated_behaviour is not None:
        identifier_candidates.append((f"SWC '{swc.name}' stated SWC-INTERNAL-BEHAVIOR name", stated_behaviour))
    for runnable in swc.runnables:
        identifier_candidates.extend(
            (
                f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{port_name}'.'{member}' stated name",
                stated,
            )
            for (list_name, port_name, _member_field, member), stated in zip(
                runnable_access_entries(runnable), stated_access_point_names(runnable), strict=True
            )
            if stated is not None
        )
        identifier_candidates.extend(
            (f"{words} stated name", stated)
            for words, _port, _operation, stated, _path, _field in result_point_sites(swc.name, runnable)
            if stated is not None
        )
    # A stub's port emits no SHORT-NAME, but its name is the last segment of
    # every connector end written on it, so it is checked all the same.
    port_subject = "port name (a reference path segment)" if swc.external else "port name"
    identifier_candidates.extend((f"SWC '{swc.name}' {port_subject}", port.name) for port in swc.ports)
    identifier_candidates.extend((f"SWC '{swc.name}' runnable name", runnable.name) for runnable in swc.runnables)
    identifier_candidates.extend((f"SWC '{swc.name}' variable name", variable.name) for variable in swc.variables)
    # An area entry is a reference and mints no SHORT-NAME of its own.
    identifier_candidates.extend((f"SWC '{swc.name}' exclusive area name", area.name) for area in swc.exclusive_areas)
    for runnable in swc.runnables:
        identifier_candidates.extend(
            (f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{variable}' stated name", stated)
            for list_name, variable, stated in variable_access_entries(runnable)
            if stated is not None
        )
    # `{runnable}_Budget` is charged only where a runnable states `wcet`, so a
    # 122-128-character runnable is legal without a budget. A runnable whose
    # own name is refused is one root cause, reported once.
    identifier_candidates.extend(
        (
            f"SWC '{swc.name}' runnable '{runnable.name}' generated EXECUTION-TIME-CONSTRAINT name",
            f"{runnable.name}{BUDGET_SUFFIX}",
        )
        for runnable in swc.runnables
        if runnable.wcet is not None and not swc.external and check_identifier(runnable.name) is None
    )


def _collect_event_name_candidates(
    swc: SWComponent,
    identifier_candidates: list[tuple[str, str]],
    event_overflows: list[str],
    overflowing_elements: dict[str, None],
) -> None:
    """Append to the accumulators every event name of *swc* the identifier check covers.

    A stated event name is a declared name. A derived name is a candidate when the
    names it derives from conform; an overflowing name from an operation, data or
    call-returned event's own pair is not a candidate but an entry of
    *event_overflows*, with the joint budget and the name to shorten.

    Args:
        swc: the component read.
        identifier_candidates: the ``(subject, name)`` pairs collected so far.
        event_overflows: the sentences of the pair-derived event names that
            overflow.
        overflowing_elements: the kinds of those events, in first-seen order.
    """
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    for (runnable, event), emitted in zip(pairs, derived_event_names(swc), strict=True):
        stated = event.name
        if stated is not None:
            # Written as it is, so checked as a declared name; the derived
            # form it replaces is not written and not checked.
            identifier_candidates.append(
                (f"SWC '{swc.name}' runnable '{runnable.name}' stated {event_element(event)} name", stated)
            )
            continue
        if isinstance(event, OperationEvent | DataEvent | CallReturnedEvent):
            # `OIT_`, `DRT_` and `ASRT_` names are the pair's, not the
            # runnable's: checked only when both names conform, with the
            # overhead read off the final name, since the retroactive counter
            # lengthens both of two pairs deriving one base.
            if isinstance(event, OperationEvent):
                port, member, words = event.port, event.operation, "operation event"
            elif isinstance(event, CallReturnedEvent):
                port, member, words = event.port, event.operation, "call-returned event"
            else:
                port, member, words = event.port, event.element, "data-received event"
            if check_identifier(port) is not None or check_identifier(member) is not None:
                continue  # the declared name is the one to fix; one root cause, one offender
            reason = check_identifier(emitted)
            if reason is None:
                continue
            budget = MAX_IDENTIFIER_LENGTH - (len(emitted) - len(port) - len(member))
            longer = port if len(port) > len(member) else member
            allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
            overflowing_elements.setdefault(event_element(event), None)
            event_overflows.append(
                f"SWC '{swc.name}' runnable '{runnable.name}' {words} '{port}'.'{member}' "
                f"generates '{emitted}' ({len(emitted)} characters), which {reason} — '{port}' and "
                f"'{member}' get {budget} characters between them, so shorten '{longer}' to at "
                f"most {allowance} characters"
            )
            continue
        if check_identifier(runnable.name) is not None:
            continue  # already refused as a runnable name; one root cause, one offender
        identifier_candidates.append(
            (f"SWC '{swc.name}' runnable '{runnable.name}' generated {event_element(event)} name", emitted)
        )


def _check_component_identifiers(swcs: list[SWComponent]) -> None:
    """Refuse a declared or generated component-side name that is not a legal AUTOSAR ``Identifier``.

    Every offender is reported in one batched message; an overflowing event name
    from an event's own pair is its own refusal, after the batched one.

    Args:
        swcs: every component declared under ``model_root/swc/``.

    Raises:
        ValueError: naming the offenders, or the overflowing event names.
    """
    identifier_candidates: list[tuple[str, str]] = []
    event_overflows: list[str] = []
    # The pair-derived event elements that overflow, in first-seen order: the
    # refusal names the kinds it lists.
    overflowing_elements: dict[str, None] = {}
    for swc in swcs:
        _collect_component_name_candidates(swc, identifier_candidates)
        _collect_event_name_candidates(swc, identifier_candidates, event_overflows, overflowing_elements)
    _check_identifiers(identifier_candidates, "swc/")
    if event_overflows:
        raise ValueError(
            f"Generated {' and '.join(overflowing_elements)} name(s) in swc/ are not legal AUTOSAR "
            f"Identifiers: {'; '.join(event_overflows)}. Rename the conflicting names, or state the event's name."
        )


def _check_component_names(swcs: list[SWComponent]) -> None:
    """Reject duplicate SWC/port/runnable names and collisions with generated names.

    ``{base}_InternalBehavior`` is not reserved: it is nested in its own component. Event names are judged as
    written: a stated name as a declared one, a derived one in its final form only, because
    :func:`_unique_event_names`' counter is retroactive.

    Raises:
        ValueError: on a declared or generated name that is not a legal AUTOSAR ``Identifier`` (a pair-derived event
            overflow is its own refusal, naming the joint budget and the name to shorten); on a duplicate SWC name,
            or a duplicate port or runnable name within a component; on a runnable name an event is also written
            with, ignoring case; on two events written with one name; on a name colliding with another component's
            ``SWC-IMPLEMENTATION`` or ``SWC-TIMING``; or on the shapes :func:`_check_access_point_names` refuses.
    """

    _check_component_identifiers(swcs)

    names = [swc.name for swc in swcs]
    duplicates = _duplicate_names(names)
    if duplicates:
        raise ValueError(f"Duplicate SWC names in swc/: {sorted(duplicates)}")

    for swc in swcs:
        port_duplicates = _duplicate_names(port.name for port in swc.ports)
        if port_duplicates:
            raise ValueError(
                f"SWC '{swc.name}' in swc/ has duplicate port name(s) "
                f"{port_duplicates}; port names must be unique within a component "
                "(a provide port and a require port of one name are siblings in PORTS)."
            )
        runnable_duplicates = _duplicate_names(runnable.name for runnable in swc.runnables)
        if runnable_duplicates:
            raise ValueError(
                f"SWC '{swc.name}' in swc/ has duplicate runnable name(s) "
                f"{runnable_duplicates}; runnable names must be unique within a component."
            )

    # `rule:name.runnable-event-collision`: a runnable and an event are siblings
    # in one SWC-INTERNAL-BEHAVIOR, but `RUNNABLES` and `EVENTS` have separate
    # `ShortNameScope`s, so neither sees the other.
    event_collisions = [collision.message for swc in swcs for collision in runnable_event_collisions(swc)]
    if event_collisions:
        raise ValueError(
            f"Runnable name(s) in swc/ collide with event SHORT-NAMEs as they are written: "
            f"{'; '.join(event_collisions)}"
        )

    # `rule:name.duplicate-event`: only a stated name can produce it. The
    # `ShortNameScope` over `EVENTS` is the backstop.
    event_duplicates = [duplicate.text for swc in swcs for duplicate in duplicate_event_names(swc)]
    if event_duplicates:
        raise ValueError(f"Event SHORT-NAME(s) in swc/ written more than once: {'; '.join(event_duplicates)}")

    # The base must be a component that actually emits the SWC-IMPLEMENTATION:
    # an external stub emits none, so `Stub_Implementation` is a free name.
    emitting = {swc.name for swc in swcs if not swc.external}
    implementation_collisions = [
        name
        for name in names
        if name.endswith(IMPLEMENTATION_SUFFIX) and name[: -len(IMPLEMENTATION_SUFFIX)] in emitting
    ]
    if implementation_collisions:
        raise ValueError(
            f"SWC name(s) {sorted(implementation_collisions)} collide with auto-generated "
            "'{base}_Implementation' SWC-IMPLEMENTATION names. Rename the conflicting components."
        )

    # `rule:name.swc-timing-collision`: the base must be a component that
    # actually emits the SWC-TIMING (`emits_swc_timing`), so
    # `X_Timing` beside a stub `X`, or beside an `X` no runnable of which
    # declares a `wcet`, is a free name.
    timing_bases = {swc.name for swc in swcs if emits_swc_timing(swc)}
    timing_collisions = [
        name for name in names if name.endswith(TIMING_SUFFIX) and name[: -len(TIMING_SUFFIX)] in timing_bases
    ]
    if timing_collisions:
        raise ValueError(
            f"SWC name(s) {sorted(timing_collisions)} collide with auto-generated "
            "'{base}_Timing' SWC-TIMING names. Rename the conflicting components."
        )

    # Last, so a duplicate declared name is reported as the duplicate it is
    # rather than as an access point two identical entries share.
    _check_access_point_names(swcs)


def composite_dependencies(dt: DataType) -> list[str]:
    """Return the names this composite data type directly references.

    The record field types, or the single array element type; empty for every
    other kind. Names of non-composites are returned too: the caller
    (:func:`topological_sort_data_types`) draws no edge for them.
    """
    if isinstance(dt, RecordDataType):
        return [f.type for f in dt.fields]
    if isinstance(dt, ArrayDataType):
        return [dt.element_type]
    return []


def topological_sort_data_types(
    types: Sequence[DataType],
    dependencies_of: Callable[[DataType], list[str]],
    label: str,
) -> list[DataType]:
    """Topologically sort one batch of data types so dependencies precede dependents.

    Ties are broken by input order. A dependency outside *types* draws no edge.

    Args:
        dependencies_of: the names one element depends on.
        label: the kind the cycle refusal names (``"Composite"``, ``"Implementation"``).

    Raises:
        ValueError: if the batch's dependency graph contains a cycle.
    """
    names = {dt.name for dt in types}
    by_name = {dt.name: dt for dt in types}
    # dependency name -> list of dependent names (edges: dependency -> dependent)
    dependents: dict[str, list[str]] = {name: [] for name in names}
    in_degree: dict[str, int] = {name: 0 for name in names}

    for dt in types:
        for dep_name in dependencies_of(dt):
            if dep_name not in names:
                continue
            # A self-reference never reaches zero in-degree, so it is reported
            # as a cycle below.
            dependents[dep_name].append(dt.name)
            in_degree[dt.name] += 1

    # Deterministic queue: process zero-in-degree nodes in original declaration order.
    queue = [dt.name for dt in types if in_degree[dt.name] == 0]
    sorted_names: list[str] = []
    while queue:
        name = queue.pop(0)
        sorted_names.append(name)
        for dependent_name in dependents[name]:
            in_degree[dependent_name] -= 1
            if in_degree[dependent_name] == 0:
                queue.append(dependent_name)

    if len(sorted_names) != len(types):
        remaining = [dt.name for dt in types if dt.name not in sorted_names]
        cycle_desc = " -> ".join(sorted(remaining) + [sorted(remaining)[0]])
        raise ValueError(f"{label} data type dependency cycle: {cycle_desc}")

    return [by_name[name] for name in sorted_names]


def _check_enum_literals(dt: EnumDataType) -> None:
    """Reject an enum whose literals collide by name or by value.

    A literal is a ``SHORT-NAME`` of its own, so it is identifier-checked too;
    this is the writer's only guard over an enum's members. Nothing is written
    before the whole document is built, so refusing at emit time still leaves
    no partial file.

    Raises:
        ValueError: if a literal name is not a legal AUTOSAR ``Identifier``; if
            two literals share a name, or two literals share a value.
    """
    literals = required(dt.literals, "_emit_enum_type checks stated literals alone")
    _check_identifiers(
        (("Enum literal name", literal.name) for literal in literals),
        f"enum '{dt.name}'",
    )

    _lit_names = [lit.name for lit in literals]
    _lit_name_dups = {n for n in _lit_names if _lit_names.count(n) > 1}
    if _lit_name_dups:
        raise ValueError(
            f"Enum '{dt.name}' has duplicate literal name(s) {sorted(_lit_name_dups)}; "
            "literal names must be unique within an enum."
        )
    _lit_values = [lit.value for lit in literals]
    _lit_value_dups = {v for v in _lit_values if _lit_values.count(v) > 1}
    if _lit_value_dups:
        raise ValueError(
            f"Enum '{dt.name}' has duplicate literal value(s) {sorted(_lit_value_dups)}; "
            "literal values must be unique within an enum."
        )


def _check_record_fields(dt: RecordDataType) -> None:
    """Reject a record whose fields collide by name.

    A field is an ``APPLICATION-RECORD-ELEMENT`` ``SHORT-NAME``, so it is
    identifier-checked too; this is the only writer guard that sees it.

    Raises:
        ValueError: if a field name is not a legal AUTOSAR ``Identifier``; if two
            fields share the same name.
    """
    _check_identifiers(
        (("Record field name", field.name) for field in dt.fields),
        f"record '{dt.name}'",
    )

    _field_names = [f.name for f in dt.fields]
    _field_name_dups = {n for n in _field_names if _field_names.count(n) > 1}
    if _field_name_dups:
        raise ValueError(
            f"Record '{dt.name}' has duplicate field name(s) {sorted(_field_name_dups)}; "
            "field names must be unique within a record."
        )


def _record_field_ref(
    dt: RecordDataType,
    field: Any,
    type_registry: DataDescription,
) -> str:
    """Resolve one record field's type to its path, or raise.

    Called by :func:`_emit_record_type` for the raise alone, before it creates any element; the path is discarded
    because :func:`_emit_type_reference` must take path and ``DEST`` from one lookup.

    Raises:
        ValueError: if the field's type is not declared in the ``application_types`` layer (an implementation type's
            name included).
    """
    ref = _resolve_application_type_ref(field.type, type_registry)
    if ref is None:
        raise ValueError(
            f"Data type '{field.type}' referenced by record '{dt.name}' field "
            f"'{field.name}' is not declared in data_types/application/"
        )
    return ref


def _array_element_ref(dt: ArrayDataType, type_registry: DataDescription) -> str:
    """Resolve an array's element type to its path, or raise.

    Called for its raise alone, like :func:`_record_field_ref`.

    Raises:
        ValueError: if the element type is not declared in the ``application_types`` layer.
    """
    ref = _resolve_application_type_ref(dt.element_type, type_registry)
    if ref is None:
        raise ValueError(
            f"Data type '{dt.element_type}' referenced by array '{dt.name}' element type is not declared in "
            "data_types/application/"
        )
    return ref


def _interface_type_ref(type_name: str | None, type_registry: DataDescription, *, application: bool) -> str:
    """Resolve an interface member's declared type to its path, or raise.

    Args:
        application: the member names an application type, asked of that layer; an implementation type otherwise.

    Raises:
        ValueError: if *type_name* is not declared in the layer its field names.
    """
    ref = None if type_name is None else resolve_type_ref(type_name, type_registry, implementation=not application)
    if ref is None:
        layer = "data_types/application/" if application else "data_types/implementation/"
        raise ValueError(f"Data type '{type_name}' not declared in {layer}")
    return ref


# ---------------------------------------------------------------------------
# Data type emitters, reached from ``_build_document()``.
# ---------------------------------------------------------------------------


def _check_base_type_native_declarations(data_types: list[DataType]) -> None:
    """Refuse every base type that declares no ``native_declaration``, naming each, before anything is emitted.

    ``NATIVE-DECLARATION`` is never derived: falling back to the type's name would silently change the C name on a
    rename. A blank value is refused like an absent one.

    Raises:
        ValueError: naming every base type with an absent or blank declaration.
    """
    missing = sorted(dt.name for dt in data_types if isinstance(dt, BaseDataType) and not dt.native_declaration.strip())
    if missing:
        raise ValueError(
            f"Base type(s) {missing} in data_types/base/ declare no native_declaration: the C type name a "
            "SW-BASE-TYPE's NATIVE-DECLARATION carries is never derived. Declare one on each "
            "('swcraft check-model' reports the same model)."
        )


def _refuse_unrepresentable_text(owner: str, field: str, text: str) -> None:
    """Refuse a free text XML 1.0 cannot carry with ``check-model``'s own sentence, before it is written.

    ``DESC`` and ``LONG-NAME`` texts are refused by ``arxml_xml``'s paragraph builder instead.

    Raises:
        ValueError: ``rule:text.xml-unrepresentable-character: <message>``.
    """
    character = xml_unrepresentable_character(text)
    if character is None:
        return

    raise ValueError(
        f"rule:text.xml-unrepresentable-character: {xml_unrepresentable_text_message(owner, field, character)}"
    )


def _emit_sw_base_type(package: ArxmlPackage, dt: BaseDataType) -> etree._Element:
    """Emit a base type's ``SW-BASE-TYPE`` into *package* — the whole of what a base type emits.

    ``NATIVE-DECLARATION`` is emitted exactly as declared and never derived. ``CATEGORY`` is always
    ``FIXED_LENGTH``: the model has no field for a base type's own category.

    Raises:
        ValueError: if *dt* declares no native declaration, or a blank one.
    """
    if not dt.native_declaration.strip():
        raise ValueError(f"SW-BASE-TYPE {dt.name!r} has no declared NATIVE-DECLARATION, and one is never derived")
    _refuse_unrepresentable_text(f"Base type '{dt.name}'", "native_declaration", dt.native_declaration)
    _refuse_unrepresentable_text(f"Base type '{dt.name}'", "encoding", dt.encoding)
    node = package.create_element("SW-BASE-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", "FIXED_LENGTH")
    sub_element(node, "BASE-TYPE-SIZE", dt.size)
    sub_element(node, "BASE-TYPE-ENCODING", dt.encoding)
    sub_element(node, "NATIVE-DECLARATION", dt.native_declaration)
    return node


def _emit_implementation_data_type(
    package: ArxmlPackage,
    name: str,
    base_type_name: str,
    *,
    data_constr: str | None = None,
    compu_method: str | None = None,
) -> etree._Element:
    """Emit an ``IMPLEMENTATION-DATA-TYPE`` of category ``VALUE`` named *name* over *base_type_name*.

    The shape a ``value_type`` emits: exactly one ``BASE-TYPE-REF``, to the
    ``SW-BASE-TYPE`` of its declared base (``[constr_1009]`` gives ``baseType``
    multiplicity ``1`` under ``VALUE``), and ``SW-CALIBRATION-ACCESS``. A
    *compu_method* (the ``texttable`` a value type's ``compu_method_ref`` names)
    adds a ``COMPU-METHOD-REF`` and a *data_constr* a ``DATA-CONSTR-REF``, in
    the order ``group SW-DATA-DEF-PROPS-CONTENT`` (XSD 4.2.2 ``:59278``, 4.3.0
    ``:68514``) gives as one ``xsd:sequence``: ``BASE-TYPE-REF``
    (``:59321`` / ``:68557``), ``SW-CALIBRATION-ACCESS`` (``:59340`` / ``:68576``),
    ``COMPU-METHOD-REF`` (``:59375`` / ``:68611``) and ``DATA-CONSTR-REF``
    (``:59388`` / ``:68624``), the same at both versions.

    Args:
        name: the element's ``SHORT-NAME``.
        base_type_name: the declared base type whose ``SW-BASE-TYPE`` is referenced.
        data_constr: the ``SHORT-NAME`` of the type's own ``DATA-CONSTR``, or ``None``.
        compu_method: the name of the ``compu_methods`` element the type names, or ``None``.
    """
    node = package.create_element("IMPLEMENTATION-DATA-TYPE", name)
    sub_element(node, "CATEGORY", "VALUE")
    props = sub_element(node, "SW-DATA-DEF-PROPS")
    variants = sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS")
    conditional = sub_element(variants, "SW-DATA-DEF-PROPS-CONDITIONAL")
    reference_element(
        conditional,
        "BASE-TYPE-REF",
        _placed_path(_HOME.BASE_TYPES, base_type_name),
        dest="SW-BASE-TYPE",
    )
    sub_element(conditional, "SW-CALIBRATION-ACCESS", "NOT-ACCESSIBLE")
    if compu_method is not None:
        reference_element(
            conditional, "COMPU-METHOD-REF", _placed_path(_HOME.COMPU_METHODS, compu_method), dest="COMPU-METHOD"
        )
    if data_constr is not None:
        reference_element(
            conditional, "DATA-CONSTR-REF", _placed_path(_HOME.IMPLEMENTATION_TYPES, data_constr), dest="DATA-CONSTR"
        )
    return node


def _emit_value_type(
    package: ArxmlPackage,
    dt: ValueDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a value type as an ``IMPLEMENTATION-DATA-TYPE`` of category ``VALUE`` over its base type.

    The base and the description are validated before anything is emitted. A declared ``value_min`` or ``value_max``
    is written as the type's own ``{name}_Range`` ``DATA-CONSTR`` of ``INTERNAL-CONSTRS``, emitted just before the
    type and referenced by a ``DATA-CONSTR-REF`` after ``SW-CALIBRATION-ACCESS``, the place
    ``SW-DATA-DEF-PROPS-CONTENT``'s sequence gives it (XSD 4.2.2 and 4.3.0 alike). Table 5.18 admits ``dataConstr``
    ``0..1`` under ``VALUE``, made binding by ``[constr_1009]``; ``INTERNAL-CONSTRS`` and not ``PHYS-CONSTRS``,
    because ``[TPS_SWCT_01288]`` has internal constraints interpreted only with respect to implementation data
    types.

    ``unit`` has no place: Table 5.18's ``unit`` row is empty for every category, so it is not emitted. A
    ``compu_method_ref`` is written as a ``COMPU-METHOD-REF`` to the ``texttable`` it names.

    Raises:
        ValueError: if the base is not declared in the ``base_types`` layer — the writer's half of
            ``rule:data-type.value-type-undeclared-base``.
    """
    base = type_registry.base(dt.base)
    if base is None:
        raise ValueError(f"Value type '{dt.name}' base type '{dt.base}' is not declared in data_types/base/.")
    # Before the first element: `set_description` below would refuse the same
    # text only after the constraint and the type are in the package.
    _refuse_unrepresentable_text(f"Data type '{dt.name}'", "description", dt.description)
    ranged = RANGE_SUFFIX in generated_data_type_forms(dt)
    if ranged:
        _emit_data_constr(package, f"{dt.name}{RANGE_SUFFIX}", "INTERNAL-CONSTRS", dt.value_min, dt.value_max)
    node = _emit_implementation_data_type(
        package,
        dt.name,
        dt.base,
        data_constr=f"{dt.name}{RANGE_SUFFIX}" if ranged else None,
        compu_method=dt.compu_method_ref,
    )
    set_description(node, dt.description)
    return node


def _emit_implementation_type_reference(
    parent: etree._Element,
    name: str,
    type_registry: DataDescription,
    *,
    compu_method: str | None = None,
) -> etree._Element:
    """Append ``SW-DATA-DEF-PROPS`` carrying one ``IMPLEMENTATION-DATA-TYPE-REF`` to *name*.

    The path and ``DEST`` are :func:`resolve_type_ref`'s and :func:`_type_ref_dest`'s, so an external type stub is
    written as its own ``external_ref`` and ``external_dest``, verbatim. A *compu_method* is written as a
    ``COMPU-METHOD-REF`` before the target: ``group SW-DATA-DEF-PROPS-CONTENT`` orders ``COMPU-METHOD-REF`` (XSD
    4.2.2 ``:59375``, 4.3.0 ``:68611``) before ``IMPLEMENTATION-DATA-TYPE-REF`` (``:59413`` / ``:68649``) at both
    versions.

    Returns:
        The created ``SW-DATA-DEF-PROPS`` element.
    """
    props = sub_element(parent, "SW-DATA-DEF-PROPS")
    variants = sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS")
    conditional = sub_element(variants, "SW-DATA-DEF-PROPS-CONDITIONAL")
    if compu_method is not None:
        reference_element(
            conditional, "COMPU-METHOD-REF", _placed_path(_HOME.COMPU_METHODS, compu_method), dest="COMPU-METHOD"
        )
    reference_element(
        conditional,
        "IMPLEMENTATION-DATA-TYPE-REF",
        resolve_type_ref(name, type_registry, implementation=True),
        dest=_type_ref_dest(name, type_registry, implementation=True),
    )
    return props


def _emit_reference_type(
    package: ArxmlPackage,
    dt: ReferenceDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a reference type as an ``IMPLEMENTATION-DATA-TYPE`` of category ``TYPE_REFERENCE``.

    Exactly one ``IMPLEMENTATION-DATA-TYPE-REF``, to the implementation type the model declares as its ``base``
    (``[constr_1009]`` gives ``implementationDataType`` multiplicity ``1`` under ``TYPE_REFERENCE``), and no
    ``BASE-TYPE-REF``; a ``compu_method_ref`` adds one ``COMPU-METHOD-REF`` before the reference.
    """
    node = package.create_element("IMPLEMENTATION-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", "TYPE_REFERENCE")
    _emit_implementation_type_reference(node, dt.base, type_registry, compu_method=dt.compu_method_ref)
    return node


def _emit_implementation_record(
    package: ArxmlPackage,
    dt: ImplementationRecordDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit an implementation record as an ``IMPLEMENTATION-DATA-TYPE`` of category ``STRUCTURE``.

    One ``IMPLEMENTATION-DATA-TYPE-ELEMENT`` per declared element, in order,
    under ``SUB-ELEMENTS``: ``SHORT-NAME``, ``CATEGORY`` ``TYPE_REFERENCE`` and
    the reference to its type, all at depth one. The type itself carries no
    ``SW-DATA-DEF-PROPS``: the model declares nothing for it to hold.
    :func:`_emit_implementation_types` has validated every element first.
    """
    node = package.create_element("IMPLEMENTATION-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", "STRUCTURE")
    members = sub_element(node, "SUB-ELEMENTS")
    for implementation_element in dt.elements:
        member = named_element(members, "IMPLEMENTATION-DATA-TYPE-ELEMENT", implementation_element.name)
        set_description(member, implementation_element.description)
        sub_element(member, "CATEGORY", "TYPE_REFERENCE")
        _emit_implementation_type_reference(member, implementation_element.type, type_registry)
    return node


def _emit_implementation_array(
    package: ArxmlPackage,
    dt: ImplementationArrayDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit an implementation array as an ``IMPLEMENTATION-DATA-TYPE`` of category ``ARRAY``.

    Exactly one sub-element, named ``element_name`` (required by the model, never
    invented), of category ``TYPE_REFERENCE``, carrying ``ARRAY-SIZE`` and
    ``ARRAY-SIZE-SEMANTICS`` ``FIXED-SIZE`` on the sub-element, before its
    ``SW-DATA-DEF-PROPS``: the one place the schema gives them. No
    ``ARRAY-SIZE-HANDLING``: the model declares fixed-size arrays only.
    """
    node = package.create_element("IMPLEMENTATION-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", "ARRAY")
    member = named_element(sub_element(node, "SUB-ELEMENTS"), "IMPLEMENTATION-DATA-TYPE-ELEMENT", dt.element_name)
    sub_element(member, "CATEGORY", "TYPE_REFERENCE")
    sub_element(member, "ARRAY-SIZE", dt.length)
    sub_element(member, "ARRAY-SIZE-SEMANTICS", "FIXED-SIZE")
    _emit_implementation_type_reference(member, dt.element_type, type_registry)
    return node


def _emit_pointer(
    package: ArxmlPackage,
    dt: PointerDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a pointer as an ``IMPLEMENTATION-DATA-TYPE`` of category ``DATA_REFERENCE``.

    ``SW-POINTER-TARGET-PROPS`` in the type's one conditional, holding
    ``TARGET-CATEGORY`` ``TYPE_REFERENCE`` (which ``[constr_1177]`` admits) and
    the target's own ``SW-DATA-DEF-PROPS`` with the one reference. A pointer to
    a pointer names another declared pointer, never an inline pointer-to-pointer
    (``[constr_1254]``'s option 1).
    """
    node = package.create_element("IMPLEMENTATION-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", "DATA_REFERENCE")
    props = sub_element(node, "SW-DATA-DEF-PROPS")
    conditional = sub_element(sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL")
    target = sub_element(conditional, "SW-POINTER-TARGET-PROPS")
    sub_element(target, "TARGET-CATEGORY", "TYPE_REFERENCE")
    _emit_implementation_type_reference(target, dt.target, type_registry)
    return node


#: The kinds the one dependency-ordered implementation pass emits, each with its
#: emitter: these four can depend on each other.
_IMPLEMENTATION_PASS_EMITTERS: dict[type, Callable[[ArxmlPackage, Any, DataDescription], etree._Element]] = {
    ReferenceDataType: _emit_reference_type,
    ImplementationRecordDataType: _emit_implementation_record,
    ImplementationArrayDataType: _emit_implementation_array,
    PointerDataType: _emit_pointer,
}


def _implementation_role_refusal(subject: str, name: str, type_registry: DataDescription) -> str | None:
    """Return the writer's sentence for an implementation role naming nothing, or ``None``.

    The role of an implementation record's element, an implementation array's
    element and a pointer's target (``rule:data-type.*-undeclared-*``). The
    ``implementation_types`` layer holds implementation kinds alone, so a
    resolved name is always of an admitted kind.
    """
    if type_registry.implementation(name) is None:
        return f"{subject} '{name}' is not declared in data_types/implementation/."
    return None


def _check_implementation_pass(batch: list[DataType], type_registry: DataDescription) -> None:
    """Refuse every member of the implementation pass whose names or references ``check-model`` reports.

    Run over the whole batch before its first element is created, so a refusal
    leaves no output; in reader order, each kind's checks in the validator's
    order: a reference type's base; an implementation record's element names
    (identifier, then duplicate), then each element's type; an implementation
    array's ``element_name``, then its element type; a pointer's target. Raises
    on the first offender.

    Raises:
        ValueError: naming the offender and the rule's repair.
    """
    for dt in batch:
        refusal: str | None = None
        if isinstance(dt, ReferenceDataType):
            # Every kind the `implementation_types` layer holds is one a reference
            # type's base may name, so a resolved base needs no kind test.
            if type_registry.implementation(dt.base) is None:
                refusal = f"Reference type '{dt.name}' base '{dt.base}' is not declared in data_types/implementation/."
        elif isinstance(dt, ImplementationRecordDataType):
            _check_identifiers(
                (("Implementation record element name", element.name) for element in dt.elements),
                f"implementation record '{dt.name}'",
            )
            names = [element.name for element in dt.elements]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                refusal = (
                    f"Implementation record '{dt.name}' has duplicate element name(s) {duplicates}; element names "
                    "must be unique within an implementation record."
                )
            for element in dt.elements:
                refusal = refusal or _implementation_role_refusal(
                    f"Implementation record '{dt.name}' element '{element.name}' type", element.type, type_registry
                )
        elif isinstance(dt, ImplementationArrayDataType):
            _check_identifiers(
                (("Implementation array element_name", dt.element_name),), f"implementation array '{dt.name}'"
            )
            refusal = _implementation_role_refusal(
                f"Implementation array '{dt.name}' element_type", dt.element_type, type_registry
            )
        elif isinstance(dt, PointerDataType):
            refusal = _implementation_role_refusal(f"Pointer '{dt.name}' target", dt.target, type_registry)
        if refusal is not None:
            raise ValueError(refusal)


def _emit_implementation_types(
    package: ArxmlPackage,
    data_types: list[DataType],
    type_registry: DataDescription,
) -> list[etree._Element]:
    """Emit the implementation reference, record, array and pointer types in **one** dependency order.

    The four kinds depend on each other, so they are one batch sorted by :func:`topological_sort_data_types`, ties
    broken by reader order; every name and reference is validated first.

    Returns:
        Every emitted element, dependencies first.

    Raises:
        ValueError: if a reference type's base, a record element's type, an array's element type or a pointer's
            target is undeclared in the ``implementation_types`` layer (an external type stub is admitted in every
            role); on a duplicate element name or a name that is not a legal AUTOSAR ``Identifier``; or on a cycle
            (``rule:data-type.cycle``).
    """
    batch = [dt for dt in data_types if type(dt) in _IMPLEMENTATION_PASS_EMITTERS]
    _check_implementation_pass(batch, type_registry)
    # An edge only where a reference names another member of the batch: a value
    # type is emitted by the pass before, and a stub by no pass.

    ordered = topological_sort_data_types(batch, data_type_references, "Implementation")
    return [_IMPLEMENTATION_PASS_EMITTERS[type(dt)](package, dt, type_registry) for dt in ordered]


def _emit_type_reference(
    parent: etree._Element,
    tag: str,
    name: str,
    type_registry: DataDescription,
    *,
    application: bool = False,
) -> etree._Element:
    """Append a ``TYPE-TREF``-shaped reference to a declared data type.

    Path and ``DEST`` come from the same registry entry. The caller validates *name* first.

    Args:
        application: *name* names an application type; otherwise an implementation type.
    """
    if application:
        dest = _application_type_ref_dest(name, type_registry)
        path = _resolve_application_type_ref(name, type_registry)
    else:
        dest = _type_ref_dest(name, type_registry, implementation=True)
        path = resolve_type_ref(name, type_registry, implementation=True)
    if dest is None:
        raise ValueError(f"Data type '{name}' is not declared")
    return reference_element(parent, tag, path, dest=dest)


def _emit_compu_method(package: ArxmlPackage, dt: EnumDataType) -> etree._Element:
    """Emit an enum's ``{name}_CompuMethod`` ``COMPU-METHOD`` into *package*.

    ``arxml_import/type_mapping.py``'s enum reduction keys on this exact name. Each literal is one closed
    single-point ``COMPU-SCALE`` whose ``DESC`` is the literal's ``description``; the method holds no description of
    its own.
    """
    return _emit_text_table(package, f"{dt.name}{COMPU_METHOD_SUFFIX}", dt.literals or ())


def _emit_text_table(package: ArxmlPackage, name: str, members: Iterable[EnumLiteral | TableEntry]) -> etree._Element:
    """Emit a ``TEXTTABLE`` ``COMPU-METHOD`` named *name*: one closed single-point scale per member.

    The one shape both an enumeration's ``{name}_CompuMethod``
    (:func:`_emit_compu_method`) and a declared ``texttable`` element
    (:func:`_emit_texttable_element`) are written in: ``CATEGORY``,
    ``COMPU-INTERNAL-TO-PHYS`` alone, and per member a ``COMPU-SCALE`` whose
    ``SHORT-LABEL`` and ``COMPU-CONST/VT`` are its name, whose ``DESC`` is its
    ``description`` and whose two closed limits are its value. The method's own
    ``DESC`` is its caller's: a declared element sets it afterwards, at the
    ``Identifiable`` position before ``CATEGORY``; the enum's form has none.
    """
    node = package.create_element("COMPU-METHOD", name)
    sub_element(node, "CATEGORY", "TEXTTABLE")
    scales = sub_element(sub_element(node, "COMPU-INTERNAL-TO-PHYS"), "COMPU-SCALES")
    for member in members:
        scale = sub_element(scales, "COMPU-SCALE")
        sub_element(scale, "SHORT-LABEL", member.name)
        set_description(scale, member.description)
        sub_element(scale, "LOWER-LIMIT", member.value).set("INTERVAL-TYPE", "CLOSED")
        sub_element(scale, "UPPER-LIMIT", member.value).set("INTERVAL-TYPE", "CLOSED")
        sub_element(sub_element(scale, "COMPU-CONST"), "VT", member.name)
    return node


def _emit_enum_type(package: ArxmlPackage, dt: EnumDataType) -> tuple[etree._Element, ...]:
    """Emit an enum as its ``TEXTTABLE`` ``COMPU-METHOD`` plus a ``VALUE`` application primitive.

    The compu method precedes the type that references it; the literals and every text are validated before anything
    is emitted. An enumeration naming a texttable states no ``literals``: it generates no ``{name}_CompuMethod``,
    and its ``COMPU-METHOD-REF`` names the declared ``texttable`` element.

    ``TEXTTABLE`` is the method's category and never the type's: the table of categories an application data type
    may take (``[constr_1007]``, table 5.8 at R4.2.2 and R4.3.0) does not list it, and ``[constr_1634]`` (R22-11)
    pairs a ``TEXTTABLE`` method with ``VALUE``. Neither licensed schema constrains the value, so no XSD run checks
    it.

    Returns:
        The ``(COMPU-METHOD, APPLICATION-PRIMITIVE-DATA-TYPE)`` pair, or the type alone for an enumeration naming a
        texttable.

    Raises:
        ValueError: if two literals share a name, two literals share a value, or a ``description`` holds a character
            XML 1.0 cannot carry.
    """
    _refuse_unrepresentable_text(f"Data type '{dt.name}'", "description", dt.description)
    if dt.literals is None:
        method_path = _placed_path(
            _HOME.COMPU_METHODS, required(dt.compu_method_ref, "literal_forms_must_be_exactly_one admits one form")
        )
        emitted: tuple[etree._Element, ...] = ()
    else:
        _check_enum_literals(dt)
        for literal in dt.literals:
            _refuse_unrepresentable_text(
                f"Enum '{dt.name}' literal '{literal.name}'", "description", literal.description
            )
        method_path = _placed_path(_HOME.APPLICATION_TYPES, f"{dt.name}{COMPU_METHOD_SUFFIX}")
        emitted = (_emit_compu_method(package, dt),)
    node = package.create_element("APPLICATION-PRIMITIVE-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", _APPLICATION_CATEGORY[EnumDataType])
    props = sub_element(node, "SW-DATA-DEF-PROPS")
    variants = sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS")
    conditional = sub_element(variants, "SW-DATA-DEF-PROPS-CONDITIONAL")
    reference_element(conditional, "COMPU-METHOD-REF", method_path, dest="COMPU-METHOD")
    return (*emitted, node)


def _emit_record_type(
    package: ArxmlPackage,
    dt: RecordDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a record as an ``APPLICATION-RECORD-DATA-TYPE``.

    Every field's type is resolved before the first element is created, so a
    record with one dangling field type leaves nothing behind in *package*.
    Unlike the array and enum types, a record carries no ``SW-DATA-DEF-PROPS``.

    Raises:
        ValueError: if two fields share a name, or a field's type is not declared.
    """
    _check_record_fields(dt)
    for field in dt.fields:
        _record_field_ref(dt, field, type_registry)
    node = package.create_element("APPLICATION-RECORD-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", _APPLICATION_CATEGORY[RecordDataType])
    elements = sub_element(node, "ELEMENTS")
    for field in dt.fields:
        element = named_element(elements, "APPLICATION-RECORD-ELEMENT", field.name)
        set_description(element, field.description)
        sub_element(element, "CATEGORY", _application_member_category(field.type, type_registry))
        _emit_type_reference(element, "TYPE-TREF", field.type, type_registry, application=True)
    return node


def _emit_array_type(
    package: ArxmlPackage,
    dt: ArrayDataType,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit an array as an ``APPLICATION-ARRAY-DATA-TYPE``.

    The single ``ELEMENT`` is always named ``Element``, and the type carries an
    empty ``SW-DATA-DEF-PROPS-VARIANTS``; both are kept because the goldens
    contain them.

    Raises:
        ValueError: if the element type is not declared.
    """
    _array_element_ref(dt, type_registry)
    node = package.create_element("APPLICATION-ARRAY-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", _APPLICATION_CATEGORY[ArrayDataType])
    sub_element(sub_element(node, "SW-DATA-DEF-PROPS"), "SW-DATA-DEF-PROPS-VARIANTS")
    element = named_element(node, "ELEMENT", "Element")
    sub_element(element, "CATEGORY", _application_member_category(dt.element_type, type_registry))
    _emit_type_reference(element, "TYPE-TREF", dt.element_type, type_registry, application=True)
    sub_element(element, "ARRAY-SIZE-SEMANTICS", "FIXED-SIZE")
    sub_element(element, "MAX-NUMBER-OF-ELEMENTS", dt.length)
    return node


def _emit_unit(package: ArxmlPackage, dt: ApplicationDataType) -> etree._Element:
    """Emit an application type's ``{name}_Unit`` ``UNIT`` into *package*.

    The free-text ``unit`` goes into ``DISPLAY-NAME`` exactly as held; ``FACTOR-SI-TO-UNIT``, ``OFFSET-SI-TO-UNIT``
    and ``PHYSICAL-DIMENSION-REF`` are not written. What this does not reach: the compatibility rule compares units
    stripped, so ``"m"`` and ``" m "`` are one unit to it and two display names in the file.
    """
    _refuse_unrepresentable_text(f"Application type '{dt.name}'", "unit", dt.unit)
    node = package.create_element("UNIT", f"{dt.name}{UNIT_SUFFIX}")
    sub_element(node, "DISPLAY-NAME", dt.unit)
    return node


def _emit_application_compu_method(package: ArxmlPackage, dt: ApplicationDataType) -> etree._Element:
    """Emit an application type's ``{name}_Compu`` ``COMPU-METHOD`` into *package*.

    The numerator is ``[offset, factor]`` and the denominator ``[1]``, lowest power first (:func:`_emit_linear`); a
    swapped order is still XSD-valid, so no schema check reaches it.

    The scale carries the conversion's own ``limits`` only: ``[TPS_SWCT_01548]`` (SWCT R4.2.2 ``:16830-16833``,
    R4.3.0 ``:18289-18292``) reads a scale's limits as the limiting values *before* the conversion is applied, not a
    copy of the type's physical ``range`` (the ``DATA-CONSTR``'s).

    ``UNIT-REF`` is second in ``group COMPU-METHOD``, after ``DISPLAY-FORMAT`` and before ``COMPU-INTERNAL-TO-PHYS``
    (XSD 4.2.2 ``:12834``, 4.3.0 ``:14300``). ``[constr_1175]`` (SWCT R4.2.2 ``:16954``, R4.3.0 ``:18398``): a compu
    method *"shall refer to a unit unless the CompuMethod's category is one of TEXTTABLE, BITFIELD_TEXTTABLE, or
    IDENTICAL"*, and every declared conversion (``conversion: {}`` included) is ``LINEAR``.

    Raises:
        ValueError: if *dt* states no unit by either form (a blank ``unit`` is none).
    """
    unit_path = _stated_form_path(dt, generated_data_type_forms(dt), UNIT_SUFFIX, _HOME.UNITS, dt.unit_ref)
    if unit_path is None:
        raise ValueError(
            "rule:data-type.conversion-without-unit: "
            + "; ".join(record.message for record in conversion_unit_records(DataDescription([dt])))
        )
    conversion = required(dt.conversion, "generated_data_type_forms emits a stated conversion alone")
    return _emit_linear(
        package, f"{dt.name}{COMPU_SUFFIX}", unit_path, conversion.factor, conversion.offset, conversion.limits
    )


def _emit_linear(
    package: ArxmlPackage,
    name: str,
    unit_path: str,
    factor: int | float,
    offset: int | float,
    limits: LinearLimits | ConversionLimits | None,
) -> etree._Element:
    """Emit a ``LINEAR`` ``COMPU-METHOD`` named *name*: ``UNIT-REF`` and one scale ``[offset, factor] / [1]``.

    The one shape an application type's ``{name}_Compu`` and a declared ``linear`` element are written in; no scale
    label, and the method's ``DESC`` is its caller's. Each declared bound of *limits* is written exactly as the
    scale's ``LOWER-LIMIT`` / ``UPPER-LIMIT`` with its ``INTERVAL-TYPE``: ``group COMPU-SCALE`` orders the limits
    after ``SHORT-LABEL``, ``SYMBOL``, ``DESC`` and ``MASK`` and before the scale's contents (XSD 4.2.2 ``:12990``,
    ``:12996``; 4.3.0 ``:14456``, ``:14462``). An absent bound is not written.
    """
    node = package.create_element("COMPU-METHOD", name)
    sub_element(node, "CATEGORY", "LINEAR")
    reference_element(node, "UNIT-REF", unit_path, dest="UNIT")
    scales = sub_element(sub_element(node, "COMPU-INTERNAL-TO-PHYS"), "COMPU-SCALES")
    scale = sub_element(scales, "COMPU-SCALE")
    if limits is not None:
        for tag, bound in (("LOWER-LIMIT", limits.lower), ("UPPER-LIMIT", limits.upper)):
            if bound is not None:
                sub_element(scale, tag, bound.value).set("INTERVAL-TYPE", bound.interval.upper())
    coeffs = sub_element(scale, "COMPU-RATIONAL-COEFFS")
    numerator = sub_element(coeffs, "COMPU-NUMERATOR")
    # Lowest power first: the constant term, then the coefficient of `internal`.
    sub_element(numerator, "V", offset)
    sub_element(numerator, "V", factor)
    sub_element(sub_element(coeffs, "COMPU-DENOMINATOR"), "V", 1)
    return node


def _emit_data_constr(
    package: ArxmlPackage,
    name: str,
    constrs_tag: str,
    lower: int | float | None,
    upper: int | float | None,
    *,
    level: int | None = None,
) -> etree._Element:
    """Emit a ``{name}_Range`` ``DATA-CONSTR`` into *package*: one rule, one *constrs_tag*.

    * ``PHYS-CONSTRS`` for an application type's ``range``: its bounds are physical;
    * ``INTERNAL-CONSTRS`` for a value type's ``value_min`` / ``value_max``: ``[TPS_SWCT_01288]`` interprets
      internal constraints only with respect to implementation data types, and physical ones only with respect to
      application data types.

    Each bound is emitted only when declared, written exactly, with ``INTERVAL-TYPE="CLOSED"``. ``CONSTR-LEVEL`` is
    written only for a declared ``data_constr`` element's *level*, first in the rule: ``group DATA-CONSTR-RULE``
    orders ``CONSTR-LEVEL`` (XSD 4.2.2 ``:14693``, 4.3.0 ``:16382``) before ``PHYS-CONSTRS`` (``:14703`` /
    ``:16392``). A ``range: {}`` emits a constraint that constrains nothing, on purpose: ``range``'s presence is the
    declaration.
    """
    node = package.create_element("DATA-CONSTR", name)
    rule = sub_element(sub_element(node, "DATA-CONSTR-RULES"), "DATA-CONSTR-RULE")
    if level is not None:
        sub_element(rule, "CONSTR-LEVEL", level)
    constrs = sub_element(rule, constrs_tag)
    if lower is not None:
        sub_element(constrs, "LOWER-LIMIT", lower).set("INTERVAL-TYPE", "CLOSED")
    if upper is not None:
        sub_element(constrs, "UPPER-LIMIT", upper).set("INTERVAL-TYPE", "CLOSED")
    return node


def _stated_form_path(
    dt: ApplicationDataType, forms: Collection[str], suffix: str, home: PackagePlacement.Home, ref: str | None
) -> str | None:
    """Return the path an application type's reference to one of its three properties names, or ``None``.

    The type's own private form ``{name}{suffix}`` where it states the property
    inline (*suffix* among its generated *forms*), the declared element *ref* names
    in *home* where it names one, else none. One decision, so the type's
    ``UNIT-REF`` and its ``{name}_Compu``'s cannot disagree.
    """
    if suffix in forms:
        return _placed_path(_HOME.APPLICATION_TYPES, f"{dt.name}{suffix}")
    return _placed_path(home, ref) if ref is not None else None


def _emit_application_type(package: ArxmlPackage, dt: ApplicationDataType) -> etree._Element:
    """Emit one declared ``application_type`` and the elements its fields derive.

    An ``APPLICATION-PRIMITIVE-DATA-TYPE`` of category ``VALUE``, preceded by the ``UNIT``, ``COMPU-METHOD`` and
    ``DATA-CONSTR`` it references, each emitted only when its field is declared inline
    (:func:`generated_data_type_forms`); a property stated by reference (``unit_ref``, ``compu_method_ref``,
    ``data_constr_ref``) writes its reference to the named element instead. A type that declares none carries no
    ``SW-DATA-DEF-PROPS``.

    Inside ``SW-DATA-DEF-PROPS-CONDITIONAL`` the references are written ``COMPU-METHOD-REF``, ``DATA-CONSTR-REF``,
    ``UNIT-REF``, the order ``group SW-DATA-DEF-PROPS-CONTENT`` gives at 4.2.2 and 4.3.0 alike.

    Returns:
        The created ``APPLICATION-PRIMITIVE-DATA-TYPE``; the derived elements are siblings already appended before
        it.

    Raises:
        ValueError: if the ``description`` or the ``unit`` holds a character XML 1.0 cannot carry, judged before the
            first element.
    """
    _refuse_unrepresentable_text(f"Data type '{dt.name}'", "description", dt.description)
    _refuse_unrepresentable_text(f"Application type '{dt.name}'", "unit", dt.unit)
    forms = generated_data_type_forms(dt)
    if UNIT_SUFFIX in forms:
        _emit_unit(package, dt)
    if COMPU_SUFFIX in forms:
        _emit_application_compu_method(package, dt)
    if RANGE_SUFFIX in forms:
        value_range = required(dt.range, "generated_data_type_forms emits a stated range alone")
        _emit_data_constr(package, f"{dt.name}{RANGE_SUFFIX}", "PHYS-CONSTRS", value_range.min, value_range.max)

    node = package.create_element("APPLICATION-PRIMITIVE-DATA-TYPE", dt.name)
    set_description(node, dt.description)
    sub_element(node, "CATEGORY", _APPLICATION_CATEGORY[ApplicationDataType])
    # Each property's reference, in the schema's order: the private form where the
    # type states it inline, the declared element where it names one, else none.
    references = [
        (tag, dest, path)
        for tag, dest, path in (
            (
                "COMPU-METHOD-REF",
                "COMPU-METHOD",
                _stated_form_path(dt, forms, COMPU_SUFFIX, _HOME.COMPU_METHODS, dt.compu_method_ref),
            ),
            (
                "DATA-CONSTR-REF",
                "DATA-CONSTR",
                _stated_form_path(dt, forms, RANGE_SUFFIX, _HOME.DATA_CONSTRAINTS, dt.data_constr_ref),
            ),
            ("UNIT-REF", "UNIT", _stated_form_path(dt, forms, UNIT_SUFFIX, _HOME.UNITS, dt.unit_ref)),
        )
        if path is not None
    ]
    if not references:
        return node
    conditional = sub_element(
        sub_element(sub_element(node, "SW-DATA-DEF-PROPS"), "SW-DATA-DEF-PROPS-VARIANTS"),
        "SW-DATA-DEF-PROPS-CONDITIONAL",
    )
    for tag, dest, path in references:
        reference_element(conditional, tag, path, dest=dest)
    return node


def _emit_mapping_set(
    package: ArxmlPackage,
    mapping_set: MappingSet,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit one declared ``mapping_set`` as a ``DATA-TYPE-MAPPING-SET``.

    One ``DATA-TYPE-MAP`` per entry, in model order, each ``DEST`` from the registry lookup; an empty
    ``DATA-TYPE-MAPS`` is not written, and ``MODE-REQUEST-TYPE-MAPS`` never is.

    A map may pair a record or an array with its implementation counterpart; it is refused where ``check-model``
    reports it: :func:`~swcraft_tools.validator.mapped_pointer` (``[constr_1066]``, R4.3.0, normative: an
    application data type is never mapped to a ``DATA_REFERENCE``; R22-11's sub-element extension read as the later
    release it is) and :func:`~swcraft_tools.validator.composite_map_difference`, SWCraft's positional check,
    partial coverage of ``[TPS_SWCT_01191]`` and never a claim of conformance. What that check does not judge
    (member names, scalar representability, swapped members of one shape, an unresolved or stub member) is generated
    as declared.

    Raises:
        ValueError: if either side is undeclared or of a kind that side may not name
            (``rule:mapping-set.unknown-application`` / ``-unknown-implementation``), if the map reaches a pointer
            (``rule:mapping-set.pointer-mapped``) or pairs a composite the positional check refuses
            (``rule:mapping-set.incompatible-composite-map``).
    """

    # Both sides are resolved before the first element is created, so a set with
    # one dangling map leaves nothing behind. The kind tuples, repair sentences
    # and predicates are the validator's own, so `generate arxml` and
    # `check-model` cannot disagree. One cause is one refusal, in the
    # validator's order: a map reaching a pointer is not also judged by shape.
    for entry in mapping_set.maps:
        application = type_registry.application(entry.application)
        if not isinstance(application, MAP_MAY_APPLICATION_KINDS):
            raise ValueError(
                f"Mapping set '{mapping_set.name}' maps '{entry.application}', which is "
                + (f"declared as a '{application.kind}'" if application is not None else "not declared")
                + f"; {MAP_MAY_APPLICATION_REPAIR}"
            )
        implementation = type_registry.implementation(entry.implementation)
        if not isinstance(implementation, MAP_MAY_IMPLEMENTATION_KINDS):
            raise ValueError(
                f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto '{entry.implementation}', "
                + (
                    f"which is declared as a '{implementation.kind}'"
                    if implementation is not None
                    else "which is not declared"
                )
                + f"; {MAP_MAY_IMPLEMENTATION_REPAIR}"
            )
        pointer = mapped_pointer(entry.implementation, type_registry)
        if pointer is not None:
            raise ValueError(
                f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto '{entry.implementation}', which "
                f"reaches the pointer '{pointer[-1]}' ({chain_text(pointer)}); an application type is never mapped "
                "to a pointer — directly, through a reference type or inside an implementation record or array "
                "([constr_1066]; 'swcraft check-model' reports the same model)."
            )
        difference = composite_map_difference(entry.application, entry.implementation, type_registry)
        if difference is not None:
            raise ValueError(
                f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto '{entry.implementation}', which "
                f"SWCraft's positional check of a composite map refuses: {difference}; {COMPOSITE_MAP_REPAIR} "
                "('swcraft check-model' reports the same model)"
            )
    node = package.create_element("DATA-TYPE-MAPPING-SET", mapping_set.name)
    set_description(node, mapping_set.description)
    if not mapping_set.maps:
        return node
    maps = sub_element(node, "DATA-TYPE-MAPS")
    for entry in mapping_set.maps:
        item = sub_element(maps, "DATA-TYPE-MAP")
        reference_element(
            item,
            "APPLICATION-DATA-TYPE-REF",
            _placed_path(_HOME.APPLICATION_TYPES, entry.application),
            dest=_application_type_ref_dest(entry.application, type_registry),
        )
        reference_element(
            item,
            "IMPLEMENTATION-DATA-TYPE-REF",
            _placed_path(_HOME.IMPLEMENTATION_TYPES, entry.implementation),
            dest=_type_ref_dest(entry.implementation, type_registry, implementation=True),
        )
    return node


def _emit_mapping_sets(
    package: ArxmlPackage,
    mapping_sets: list[MappingSet],
    type_registry: DataDescription,
) -> None:
    """Emit every declared mapping set into *package*, in reader order.

    Last in ``DataTypes``, in its own layer package after every type a map may
    name, so each reference points backwards.
    """
    for mapping_set in mapping_sets:
        _emit_mapping_set(package, mapping_set, type_registry)


def _emit_composite_types(
    package: ArxmlPackage,
    composites: Sequence[DataType],
    type_registry: DataDescription,
) -> list[etree._Element]:
    """Emit every enum/record/array in dependency order.

    A composite follows every other composite it references; the order is an output-order contract the goldens pin.

    Returns:
        Every emitted element, in emission order (an enum contributes two).

    Raises:
        ValueError: if the composites' dependency graph contains a cycle, or any referenced type is undeclared.
    """
    emitted: list[etree._Element] = []
    for dt in topological_sort_data_types(composites, composite_dependencies, "Composite"):
        if isinstance(dt, EnumDataType):
            emitted.extend(_emit_enum_type(package, dt))
        elif isinstance(dt, RecordDataType):
            emitted.append(_emit_record_type(package, dt, type_registry))
        elif isinstance(dt, ArrayDataType):
            emitted.append(_emit_array_type(package, dt, type_registry))
    return emitted


# ---------------------------------------------------------------------------
# Port-interface emitters
# ---------------------------------------------------------------------------


def _init_value_refusals(
    value: object,
    type_name: str,
    type_registry: DataDescription,
    path: str,
    resolving: tuple[str, ...] = (),
    *,
    application: bool,
) -> list[str]:
    """Return every reason this writer cannot spell *value* as *type_name*, recursively.

    The fail-closed twin of ``validator.check_init_conformance()``'s walk, restating ``check-model``'s refusals. It
    dispatches on the declared type, never on the value's shape, and abstains wherever the resolution it needs is
    missing (:func:`_init_type_resolves` lists the cases). Two refusals are the emitter's own:

    * a ``str`` scalar under a value type (or a reference chain ending in one) that states no table:
      ``interface.init-string-scalar``;
    * an ``int`` this interpreter cannot convert to its decimal string: ``interface.init-unwritable-integer``.

    Args:
        path: where this value sits inside the element's ``init``, for the message.
        resolving: the type names already being resolved on this branch.
        application: the type and its member types are looked up on the application side (else the implementation
            side).

    Returns:
        One sentence per refusal, in walk order; empty when the value is writable.
    """
    declared = _referenced_type(type_name, type_registry, implementation=not application)
    if declared is None or type_name in resolving:
        return []
    resolving = (*resolving, type_name)

    if isinstance(declared, ReferenceDataType):
        # A reference type declares no range of its own, so the value is judged
        # at the bottom of the chain: a value type, or an implementation record,
        # array or pointer whose shape the value then takes. A reference type
        # stating a table, itself or along its chain, takes an entry's name.
        table = implementation_table(declared, type_registry)
        if table is not None:
            return _init_table_refusals(value, type_name, table, path)
        bottom = reference_chain_end(declared, type_registry)
        if not isinstance(
            bottom, (ValueDataType, ImplementationRecordDataType, ImplementationArrayDataType, PointerDataType)
        ):
            return []
        return _init_value_refusals(value, bottom.name, type_registry, path, resolving, application=False)

    if isinstance(declared, PointerDataType):
        # A pointer takes no value: an address is the RTE's to supply, and no
        # value specification this writer emits can spell one.
        return [
            f"{path} is given a value, but '{type_name}' is a pointer, which takes no init value "
            "(interface.init-shape-mismatch)"
        ]

    if isinstance(declared, ImplementationRecordDataType):
        # The application record's shape over the elements.
        return _init_map_refusals(
            value,
            type_name,
            "implementation record",
            "elements",
            {element.name: element.type for element in declared.elements},
            _init_member_refusals,
            type_registry,
            path,
            resolving,
        )

    if isinstance(declared, ImplementationArrayDataType):
        return _init_list_refusals(
            value,
            type_name,
            "implementation array",
            declared.length,
            declared.element_type,
            _init_member_refusals,
            type_registry,
            path,
            resolving,
        )

    if isinstance(declared, ApplicationDataType):
        # The value type's three refusals, in its order. The `range` bounds are
        # not judged here (`interface.init-out-of-range` is
        # `check_init_conformance`'s): this walk refuses only what the emitter
        # cannot spell.
        if isinstance(value, (list, dict)):
            return [f"{path} is {'a list' if isinstance(value, list) else 'a map'}, but '{type_name}' takes a scalar"]
        if isinstance(value, str):
            return [
                f"{path} is {value!r}, a string, but '{type_name}' is no enumeration, so its values are numbers "
                f"(interface.init-string-scalar)"
            ]
        return _unwritable_integer_refusal(value, path)

    if isinstance(declared, ValueDataType):
        table = implementation_table(declared, type_registry)
        if table is not None:
            return _init_table_refusals(value, type_name, table, path)
        if not isinstance(type_registry.base(declared.base), BaseDataType):
            return []
        if isinstance(value, (list, dict)):
            return [f"{path} is {'a list' if isinstance(value, list) else 'a map'}, but '{type_name}' takes a scalar"]
        if isinstance(value, str):
            return [
                f"{path} is {value!r}, a string, but '{type_name}' references no table, so its values are numbers "
                f"(interface.init-string-scalar)"
            ]
        return _unwritable_integer_refusal(value, path)

    if isinstance(declared, EnumDataType):
        # Its own literals, or the entries of the texttable it names; an
        # unresolved table is `rule:data-type.unknown-compu-method`'s, refused first.
        stated = stated_literals(declared, type_registry)
        if isinstance(stated, NotResolved):
            return []
        names = [name for name, _value in stated]
        if isinstance(value, str) and value in names:
            return []
        return [f"{path} names no literal of enum '{type_name}'; it declares {names}"]

    if isinstance(declared, RecordDataType):
        return _init_map_refusals(
            value,
            type_name,
            "record",
            "fields",
            {field.name: field.type for field in declared.fields},
            _init_application_member_refusals,
            type_registry,
            path,
            resolving,
        )

    if isinstance(declared, ArrayDataType):
        return _init_list_refusals(
            value,
            type_name,
            "array",
            declared.length,
            declared.element_type,
            _init_application_member_refusals,
            type_registry,
            path,
            resolving,
        )

    # Only an external type stub reaches here (a base type resolves in
    # `base_types` alone). It abstains rather than raising: a walk that cannot
    # say anything about a value reports nothing about it.
    return []


def _init_table_refusals(value: object, type_name: str, stated: StatedTable, path: str) -> list[str]:
    """Return the refusals for a value under a type that states a table: it must name an entry, as under an enumeration.

    ``validator._init_table_violations``' refusals, restated: an integer too wide
    to write first, a composite, then anything that names no entry (a number
    among them, which SWCraft reads as no literal).
    """
    names = [entry.name for entry in stated.table.entries]
    unwritable = _unwritable_integer_refusal(value, path)
    if unwritable:
        return unwritable
    if isinstance(value, (list, dict)):
        return [
            f"{path} is {'a list' if isinstance(value, list) else 'a map'}, but '{type_name}' references the table "
            f"'{stated.compu_method_ref}' and takes the name of one of its entries (interface.init-shape-mismatch)"
        ]
    if isinstance(value, str) and value in names:
        return []
    return [
        f"{path} names no entry of the table '{stated.compu_method_ref}' that '{stated.type_name}' references; "
        f"it declares {names} (interface.init-unknown-enum-literal)"
    ]


def _unwritable_integer_refusal(value: object, path: str) -> list[str]:
    """Return ``interface.init-unwritable-integer``'s refusal: an ``int`` this interpreter cannot write in decimal.

    The sentence names the value's bit length, never its digits. Empty for every other value (a ``bool`` included),
    and for every value when ``sys.get_int_max_str_digits() == 0``.
    """

    if not isinstance(value, int) or not is_unwritable_integer(value):
        return []
    return [
        f"{path} is an integer of {value.bit_length()} bits, above this interpreter's "
        f"{sys.get_int_max_str_digits()}-digit limit for writing an integer "
        f"(interface.init-unwritable-integer)"
    ]


def _article(noun: str) -> str:
    """Return ``a record``, ``an array`` or ``an implementation record``."""
    return f"{'an' if noun[0] in 'aeiou' else 'a'} {noun}"


#: How one composite's member is walked: :func:`_init_value_refusals` for an
#: application record's or array's, :func:`_init_member_refusals` for an
#: implementation record's or array's.
_InitMemberRefusals = Callable[[object, str, DataDescription, str, tuple[str, ...]], list[str]]


def _init_map_refusals(
    value: object,
    type_name: str,
    noun: str,
    collection: str,
    members: dict[str, str],
    walk: _InitMemberRefusals,
    type_registry: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[str]:
    """Check a map keyed by exactly the declared *members*, then each member's value, walked by *walk*.

    The record shape for both record kinds (*noun* ``record`` or
    ``implementation record``, *collection* ``fields`` or ``elements``) —
    ``validator._init_map_violations``' two rules, in the writer's words.
    """
    if not isinstance(value, dict):
        return [f"{path} is not a map, but '{type_name}' is {_article(noun)}"]
    missing = sorted(set(members) - set(value))
    undeclared = sorted(set(value) - set(members))
    refusals = []
    if missing or undeclared:
        refusals.append(
            f"{path} does not name the {collection} {noun} '{type_name}' declares: "
            f"missing {missing}, undeclared {undeclared}"
        )
    for name, member_type in members.items():
        if name in value:
            refusals.extend(walk(value[name], member_type, type_registry, f"{path}.{name}", resolving))
    return refusals


def _init_list_refusals(
    value: object,
    type_name: str,
    noun: str,
    length: int,
    element_type: str,
    walk: _InitMemberRefusals,
    type_registry: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[str]:
    """Check a list of exactly the declared *length*, then each member's value, walked by *walk*.

    The array shape for both array kinds (*noun* ``array`` or ``implementation
    array``) — ``validator._init_list_violations``' two rules, in the writer's
    words.
    """
    if not isinstance(value, list):
        return [f"{path} is not a list, but '{type_name}' is {_article(noun)}"]
    refusals = []
    if len(value) != length:
        refusals.append(f"{path} has {len(value)} member(s), but {noun} '{type_name}' declares length {length}")
    for index, member in enumerate(value):
        refusals.extend(walk(member, element_type, type_registry, f"{path}[{index}]", resolving))
    return refusals


def _init_member_refusals(
    value: object,
    member_type: str,
    type_registry: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[str]:
    """Check one member of an implementation record's or array's value against the member's type.

    The writer's half of ``validator._init_member_violations``: a member typed by
    an external data type stub, directly or through a reference chain, shapes no
    value, so the value is refused under ``interface.init-on-external-type``.
    The chain is the validator's own ``external_type_chain``. Every other
    member is walked by :func:`_init_value_refusals`.
    """

    chain = external_type_chain(member_type, type_registry, application=False)
    if chain is not None:
        return [
            f"{path} sits where the type "
            + chain_text(chain)
            + f" ends at the external type stub '{chain[-1]}', whose content is outside the project, so no value "
            "can be shaped for it (interface.init-on-external-type)"
        ]
    return _init_value_refusals(value, member_type, type_registry, path, resolving, application=False)


def _init_application_member_refusals(
    value: object,
    member_type: str,
    type_registry: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[str]:
    """Check one member of a record's or an application array's value against the member's application type.

    The writer's half of ``validator._init_application_member_violations``: a
    record field and an array element name an application type, so the walk
    descends on that side.
    """
    return _init_value_refusals(value, member_type, type_registry, path, resolving, application=True)


def _init_type_resolves(
    type_name: str | None,
    type_registry: DataDescription,
    resolving: tuple[str, ...] = (),
    *,
    application: bool,
) -> bool:
    """Return whether a value declared as *type_name* can be walked at all.

    The emission-side twin of :func:`_init_value_refusals`' abstention, on the same conditions (an undeclared type,
    a value type whose base is not a declared base type, a base type named directly, a type already being resolved).
    An implementation record or array resolves when every member does; a pointer and a stub member resolve. A cycle
    answers ``True`` and stops descending.

    Args:
        resolving: the type names already being resolved on this branch.
        application: the side *type_name* is asked on; the walk stays on it.
    """
    if type_name is None:
        return False
    declared = _referenced_type(type_name, type_registry, implementation=not application)
    if declared is None:
        return False
    if type_name in resolving:
        return True
    resolving = (*resolving, type_name)
    if isinstance(declared, BaseDataType):
        return False
    if isinstance(declared, ReferenceDataType):
        # A chain may end at a value type, an implementation composite or a
        # pointer; one ending at an external type stub resolves too, because the
        # value walk refuses it rather than abstaining.
        bottom = reference_chain_end(declared, type_registry)
        if isinstance(bottom, ExternalDataType):
            return True
        return bottom is not None and _init_type_resolves(bottom.name, type_registry, resolving, application=False)
    if isinstance(declared, ValueDataType):
        return isinstance(type_registry.base(declared.base), BaseDataType)
    if isinstance(declared, RecordDataType):
        return all(
            _init_type_resolves(field.type, type_registry, resolving, application=True) for field in declared.fields
        )
    if isinstance(declared, ArrayDataType):
        return _init_type_resolves(declared.element_type, type_registry, resolving, application=True)
    if isinstance(declared, ImplementationRecordDataType):
        return all(
            _init_type_resolves(element.type, type_registry, resolving, application=False)
            for element in declared.elements
        )
    if isinstance(declared, ImplementationArrayDataType):
        return _init_type_resolves(declared.element_type, type_registry, resolving, application=False)
    # An application type, an enum, an external type stub (a member's, which the
    # value walk refuses) and a pointer (which the value walk refuses): each
    # resolves, so its value is judged rather than skipped without a word.
    return True


#: The precondition :func:`_emit_value_specification` reads its value under.
_CONFORMING_VALUE = "_init_value_refusals passed the value's shape"


def _emit_value_specification(
    parent: etree._Element,
    value: object,
    type_name: str,
    type_registry: DataDescription,
    label: str | None = None,
    *,
    application: bool,
    own: bool,
) -> etree._Element:
    """Emit one value specification for *value* under the declared *type_name*.

    The shape follows the declared type, recursively, never the value's own. Clauses are
    ``AUTOSAR_TPS_SoftwareComponentTemplate``'s, at the releases named:

    * a prototype's own value (*own*) of an application type is an ``APPLICATION-VALUE-SPECIFICATION``
      (``[constr_1221]``, R4.2.2 and R4.3.0: *"If a DataPrototype is typed by an ApplicationPrimitiveDataType its
      initValue shall be provided by an ApplicationValueSpecification"*), shaped by :func:`_emit_application_value`:
      one ``V`` and the type's own ``UNIT-REF`` where it states a unit, never an invented one (a stated
      nonconformance with ``SwValueCont.unit``'s multiplicity 1 that both XSDs admit); no ``SW-ARRAYSIZE``
      (``[constr_2051]``, R4.2.2 and R4.3.0, asks it of the compound categories only);
    * an application type's number as a member stays a ``NUMERICAL-VALUE-SPECIFICATION`` with no unit: no rule
      prescribes a member's form (``[constr_1271]``-``[constr_1274]``, ``[constr_4035]``, R4.2.2 and R4.3.0), and the
      value is read by its type either way (``[TPS_SWCT_01183]``, R4.2.2 and R4.3.0);
    * an enumeration leaf, at any level, is an ``APPLICATION-VALUE-SPECIFICATION`` holding one ``VT``, the literal's
      name, and no ``UNIT-REF``. As the prototype's own value that is ``[constr_1221]``'s second sentence, *"the
      value provided shall match to one of the applicable text values (vt, shortLabel, symbol)"*; as a member it is
      SWCraft's choice, matching R4.3.0's ``[TPS_SWCT_01695]`` (a value against the scales' symbolic names);
    * a value or reference type stating a table, on the type or along its chain, is a ``TEXT-VALUE-SPECIFICATION``
      naming the entry (``[constr_1225]``, R4.2.2 and R4.3.0); any other value, reference or base type is a
      ``NUMERICAL-VALUE-SPECIFICATION`` (``[constr_1385]``, R4.2.2 and R4.3.0: never the application form under an
      implementation type);
    * a record (application or implementation) is a ``RECORD-VALUE-SPECIFICATION`` with one member per field in
      declaration order, an array an ``ARRAY-VALUE-SPECIFICATION`` with one member per index (application types
      ``[constr_1223]``, ``[constr_1224]``; implementation types ``[constr_1272]``, ``[constr_1274]``; R4.2.2 and
      R4.3.0); a reference type takes the form of what its chain ends at.

    A ``bool`` is written ``1``/``0``. ``SHORT-LABEL`` is emitted only for a record member; no ``BINDING-TIME`` is
    written. :func:`_init_value_refusals` has already passed on this value.

    Args:
        label: the ``SHORT-LABEL`` to carry, for a record member only.
        own: whether the value is the prototype's own value or a member of a record or array value; it decides the
            leaf's form.
    """
    declared = _referenced_type(type_name, type_registry, implementation=not application)
    if declared is None:
        # The value has passed `_init_value_refusals`, so its type is declared.
        raise KeyError(type_name)

    if isinstance(declared, (ValueDataType, ReferenceDataType)):
        # A type stating a table, on itself or along its chain, is the
        # implementation type `[constr_1225]` speaks of: its value is a
        # TEXT-VALUE-SPECIFICATION naming the entry, the text the table's scale
        # carries as its VT, so it names exactly one.
        if implementation_table(declared, type_registry) is not None:
            node = sub_element(parent, "TEXT-VALUE-SPECIFICATION")
            if label is not None:
                sub_element(node, "SHORT-LABEL", label)
            sub_element(node, "VALUE", str(value))
            return node

    if isinstance(declared, ReferenceDataType):
        # An alias takes the shape of what its chain ends at; a chain ending at a
        # value type keeps the numerical form below.
        bottom = reference_chain_end(declared, type_registry)
        if isinstance(bottom, (ImplementationRecordDataType, ImplementationArrayDataType)):
            return _emit_value_specification(
                parent, value, bottom.name, type_registry, label=label, application=False, own=own
            )

    if isinstance(declared, (RecordDataType, ImplementationRecordDataType)):
        # An implementation record's value takes the application record's shape
        # over its elements: the form AUTOSAR gives a record value under a
        # STRUCTURE-typed prototype.
        members = declared.fields if isinstance(declared, RecordDataType) else declared.elements
        node = sub_element(parent, "RECORD-VALUE-SPECIFICATION")
        if label is not None:
            sub_element(node, "SHORT-LABEL", label)
        fields = sub_element(node, "FIELDS")
        record = required_instance(value, dict, _CONFORMING_VALUE)
        for member in members:
            _emit_value_specification(
                fields,
                record[member.name],
                member.type,
                type_registry,
                label=member.name,
                application=application,
                own=False,
            )
        return node

    if isinstance(declared, (ArrayDataType, ImplementationArrayDataType)):
        node = sub_element(parent, "ARRAY-VALUE-SPECIFICATION")
        if label is not None:
            sub_element(node, "SHORT-LABEL", label)
        elements = sub_element(node, "ELEMENTS")
        for member in required_instance(value, list, _CONFORMING_VALUE):
            _emit_value_specification(
                elements, member, declared.element_type, type_registry, application=application, own=False
            )
        return node

    # A scalar leaf. A `bool` is mapped to 1/0 here, where the declared type is known
    # (an enumeration leaf below reads no number).
    number = int(value) if isinstance(value, bool) else value

    if isinstance(declared, ApplicationDataType) and own:
        # A prototype's own value of an application primitive that is not an
        # enumeration (`[constr_1221]`), with its type's own unit.
        unit_path = _stated_form_path(
            declared, generated_data_type_forms(declared), UNIT_SUFFIX, _HOME.UNITS, declared.unit_ref
        )
        text = format_init_number(required_instance(number, (int, float), _CONFORMING_VALUE))
        return _emit_application_value(parent, label, "V", text, unit_path)

    if isinstance(declared, EnumDataType):
        # The literal's name as the one VT of an application value, at every
        # level, with no unit: an enumeration states none.
        stated = stated_literals(declared, type_registry)
        if isinstance(stated, NotResolved):
            # `_init_refusals` and `rule:data-type.unknown-compu-method` refuse an
            # unresolved table before the first element is emitted.
            raise TypeError(f"enum type '{type_name}' names no texttable to take its init value's literal from")
        if not any(name == value for name, _number in stated):
            # The precondition: `_init_value_refusals` has refused a name of no literal.
            raise KeyError(value)
        return _emit_application_value(parent, label, "VT", str(value))

    node = sub_element(parent, "NUMERICAL-VALUE-SPECIFICATION")
    if label is not None:
        sub_element(node, "SHORT-LABEL", label)
    sub_element(node, "VALUE", format_init_number(required_instance(number, (int, float), _CONFORMING_VALUE)))
    return node


def _emit_application_value(
    parent: etree._Element, label: str | None, leaf_tag: str, text: str, unit_path: str | None = None
) -> etree._Element:
    """Emit an ``APPLICATION-VALUE-SPECIFICATION``, the one place its shape is written.

    ``SHORT-LABEL`` first where *label* is given (a record member), ``CATEGORY``
    ``VALUE``, then ``SW-VALUE-CONT`` with, in the schema's order, the ``UNIT-REF``
    *unit_path* names where it is given and ``SW-VALUES-PHYS`` holding one
    *leaf_tag* -- ``V`` with a number's text, or ``VT`` with an enumeration
    literal's name. No ``SW-ARRAYSIZE``.
    """
    node = sub_element(parent, "APPLICATION-VALUE-SPECIFICATION")
    if label is not None:
        sub_element(node, "SHORT-LABEL", label)
    sub_element(node, "CATEGORY", "VALUE")
    content = sub_element(node, "SW-VALUE-CONT")
    if unit_path is not None:
        reference_element(content, "UNIT-REF", unit_path, dest="UNIT")
    sub_element(sub_element(content, "SW-VALUES-PHYS"), leaf_tag, text)
    return node


def _emit_init_constants(
    package: ArxmlPackage,
    iface: SRInterface,
    type_registry: DataDescription,
) -> list[etree._Element]:
    """Emit one ``CONSTANT-SPECIFICATION`` per data element carrying an ``init``.

    They land in ``PortInterfaces`` as siblings of the interfaces, emitted just before the interface. Every value is
    checked before the first element is created. An element whose declared type does not resolve is skipped
    (:func:`_init_type_resolves`); an ``init`` on an element typed by an external type stub, directly or through a
    reference chain, is refused (``rule:interface.init-on-external-type``).

    Returns:
        The created ``CONSTANT-SPECIFICATION`` elements, in data-element order.

    Raises:
        ValueError: if any declared ``init`` is a value this writer cannot spell, does not fit its declared type, or
            sits on an element whose type ends at an external type stub.
    """

    refusals: list[str] = []
    stubbed: set[str] = set()
    for element in iface.data_elements:
        if element.init is None:
            continue
        element_type, element_application = _prototype_type(element)
        chain = external_type_chain(element_type, type_registry, application=element_application)
        if chain is not None:
            stubbed.add(element.name)
            refusals.append(
                f"data element '{element.name}' declares an init, but its type "
                + chain_text(chain)
                + f" ends at the external type stub '{chain[-1]}', whose content is outside the project, "
                "so no value can be shaped for it (interface.init-on-external-type)"
            )

    valued = [
        element
        for element in iface.data_elements
        if element.init is not None
        and element.name not in stubbed
        and _init_type_resolves(_prototype_type(element)[0], type_registry, application=_prototype_type(element)[1])
    ]

    for element in valued:
        element_type, element_application = _prototype_type(element)
        refusals.extend(
            f"data element '{element.name}' {refusal}"
            for refusal in _init_value_refusals(
                element.init, element_type, type_registry, "init", application=element_application
            )
        )
    if refusals:
        raise ValueError(
            f"Interface '{iface.name}' declares init value(s) this writer cannot emit: " + "; ".join(refusals) + "."
        )

    emitted: list[etree._Element] = []
    for element in valued:
        node = package.create_element("CONSTANT-SPECIFICATION", init_constant_name(iface.name, element.name))
        element_type, element_application = _prototype_type(element)
        _emit_value_specification(
            sub_element(node, "VALUE-SPEC"),
            element.init,
            element_type,
            type_registry,
            application=element_application,
            own=True,
        )
        emitted.append(node)
    return emitted


def _emit_init_value_reference(parent: etree._Element, interface_name: str, data_element: Any) -> etree._Element | None:
    """Emit *data_element*'s ``INIT-VALUE`` reference under *parent*, or nothing.

    ``INIT-VALUE`` / ``CONSTANT-REFERENCE`` / ``CONSTANT-REF`` with ``DEST`` ``CONSTANT-SPECIFICATION``. The caller
    appends it last, the schema position at both versions: after ``TYPE-TREF`` on the prototype, after
    ``HANDLE-TIMEOUT-TYPE`` on a ``NONQUEUED-RECEIVER-COM-SPEC`` and after ``USES-END-TO-END-PROTECTION`` on a
    ``NONQUEUED-SENDER-COM-SPEC`` (4.3.0's ``TIMEOUT-SUBSTITUTION-VALUE`` is not emitted).
    """
    if data_element.init is None:
        return None
    node = sub_element(parent, "INIT-VALUE")
    reference_element(
        sub_element(node, "CONSTANT-REFERENCE"),
        "CONSTANT-REF",
        _placed_path(_HOME.PORT_INTERFACES, init_constant_name(interface_name, data_element.name)),
        dest="CONSTANT-SPECIFICATION",
    )
    return node


def _emit_sr_interface(
    package: ArxmlPackage,
    iface: SRInterface,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a sender-receiver interface as a ``SENDER-RECEIVER-INTERFACE``.

    Every data element's type is resolved before the first element is created. ``IS-SERVICE`` states ``is_service``:
    ``isService`` is multiplicity 1 on ``PortInterface`` (Table 3.18 of SWCT R4.2.2) with no default, so a silent
    model states ``false``. A ``VARIABLE-DATA-PROTOTYPE`` carries ``SW-DATA-DEF-PROPS`` only when its data element
    states a ``sw_calibration_access`` or ``sw_impl_policy``, and an ``INIT-VALUE`` reference after its
    ``TYPE-TREF`` when it carries an ``init``.

    Raises:
        ValueError: if a data element's type is not declared.
    """
    for data_element in iface.data_elements:
        element_type, element_application = _prototype_type(data_element)
        _interface_type_ref(element_type, type_registry, application=element_application)
    node = package.create_element("SENDER-RECEIVER-INTERFACE", iface.name)
    set_description(node, iface.description)
    sub_element(node, "IS-SERVICE", iface.is_service)
    elements = sub_element(node, "DATA-ELEMENTS")
    scope = ShortNameScope(elements, f"DATA-ELEMENTS of interface {iface.name!r}")
    for data_element in iface.data_elements:
        prototype = scope.create("VARIABLE-DATA-PROTOTYPE", data_element.name)
        set_description(prototype, data_element.description)
        _emit_data_element_props(prototype, data_element)
        element_type, element_application = _prototype_type(data_element)
        _emit_type_reference(prototype, "TYPE-TREF", element_type, type_registry, application=element_application)
        _emit_init_value_reference(prototype, iface.name, data_element)
    return node


def _emit_data_element_props(prototype: etree._Element, data_element: DataElement) -> etree._Element | None:
    """Emit a data element's ``SW-DATA-DEF-PROPS``, or nothing when it states neither value.

    A stated ``sw_calibration_access`` and then a stated ``sw_impl_policy`` in one
    ``SW-DATA-DEF-PROPS-CONDITIONAL``, the order ``SW-DATA-DEF-PROPS-CONTENT`` gives them (XSD 4.2.2
    ``:59340``/``:59437``, 4.3.0 ``:68576``/``:68673``), under the ``SW-DATA-DEF-PROPS`` the ``DATA-PROTOTYPE``
    group puts before ``TYPE-TREF`` (4.2.2 ``:14935``, 4.3.0 ``:16772``); the caller appends it after the ``DESC``.

    An absent value writes nothing, which the standard defines: ``notAccessible`` (``[TPS_SWCT_01559]``, SWCT R4.2.2
    ``:15610``, R4.3.0 ``:16757``) and ``standard`` (``[TPS_SWCT_02000]``, R4.2.2 ``:15287``, R4.3.0 ``:16416``). A
    stated one is written, the explicit default included. The combination ``[constr_1017]`` refuses is refused by
    the model's load.
    """
    return _emit_sw_data_def_props(prototype, data_element.sw_calibration_access, data_element.sw_impl_policy)


def _emit_sw_data_def_props(
    prototype: etree._Element, access: str | None, policy: str | None = None
) -> etree._Element | None:
    """Emit ``SW-DATA-DEF-PROPS`` with a stated calibration access and implementation policy, or nothing.

    The one place the block's shape is written, for a data element
    (:func:`_emit_data_element_props`) and a variable
    (:func:`_emit_inter_runnable_variables`). A variable has no
    ``sw_impl_policy`` field, so none is written: the standard's default,
    ``standard``, is the only value ``[constr_2038]`` / ``[constr_2039]`` admit
    for either role. An absent value writes nothing.
    """
    if access is None and policy is None:
        return None
    props = sub_element(prototype, "SW-DATA-DEF-PROPS")
    conditional = sub_element(sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL")
    if access is not None:
        sub_element(conditional, "SW-CALIBRATION-ACCESS", SW_CALIBRATION_ACCESS_LITERALS[access])
    if policy is not None:
        sub_element(conditional, "SW-IMPL-POLICY", SW_IMPL_POLICY_LITERALS[policy])
    return props


def _emit_cs_interface(
    package: ArxmlPackage,
    iface: CSInterface,
    type_registry: DataDescription,
) -> etree._Element:
    """Emit a client-server interface as a ``CLIENT-SERVER-INTERFACE``.

    Every argument's type is resolved before the first element is created, as in
    :func:`_emit_sr_interface`. An operation with no arguments emits no
    ``ARGUMENTS`` wrapper, and ``DIRECTION`` is the uppercased model value.
    ``IS-SERVICE`` states ``is_service``. Each ``ARGUMENT-DATA-PROTOTYPE``
    carries ``SW-CALIBRATION-ACCESS NOT-ACCESSIBLE`` and, after its
    ``DIRECTION``, ``SERVER-ARGUMENT-IMPL-POLICY USE-ARGUMENT-TYPE``, except
    where :func:`_argument_policy_omitted` says otherwise.

    Raises:
        ValueError: if an argument's type is not declared.
    """
    for operation in iface.operations:
        for argument in operation.arguments:
            argument_type, argument_application = _prototype_type(argument)
            _interface_type_ref(argument_type, type_registry, application=argument_application)
    node = package.create_element("CLIENT-SERVER-INTERFACE", iface.name)
    set_description(node, iface.description)
    sub_element(node, "IS-SERVICE", iface.is_service)
    operations = sub_element(node, "OPERATIONS")
    operation_scope = ShortNameScope(operations, f"OPERATIONS of interface {iface.name!r}")
    for operation in iface.operations:
        operation_node = operation_scope.create("CLIENT-SERVER-OPERATION", operation.name)
        set_description(operation_node, operation.description)
        if not operation.arguments:
            continue
        arguments = sub_element(operation_node, "ARGUMENTS")
        argument_scope = ShortNameScope(
            arguments, f"ARGUMENTS of operation {operation.name!r} in interface {iface.name!r}"
        )
        for argument in operation.arguments:
            prototype = argument_scope.create("ARGUMENT-DATA-PROTOTYPE", argument.name)
            set_description(prototype, argument.description)
            props = sub_element(prototype, "SW-DATA-DEF-PROPS")
            variants = sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS")
            conditional = sub_element(variants, "SW-DATA-DEF-PROPS-CONDITIONAL")
            sub_element(conditional, "SW-CALIBRATION-ACCESS", "NOT-ACCESSIBLE")
            argument_type, argument_application = _prototype_type(argument)
            _emit_type_reference(prototype, "TYPE-TREF", argument_type, type_registry, application=argument_application)
            sub_element(prototype, "DIRECTION", argument.direction.upper())
            if not _argument_policy_omitted(argument_type, type_registry, application=argument_application):
                sub_element(prototype, "SERVER-ARGUMENT-IMPL-POLICY", "USE-ARGUMENT-TYPE")
    return node


def _argument_policy_omitted(type_name: str, type_registry: DataDescription, *, application: bool) -> bool:
    """Return whether an argument typed *type_name* carries no ``SERVER-ARGUMENT-IMPL-POLICY``.

    True for an array-typed argument (:func:`argument_array_typed`) and for one whose type is, or resolves through a
    reference chain to, an external type stub: a stub's content is declared elsewhere, so the absent attribute means
    the standard's default for whatever the type is.
    """

    return (
        argument_array_typed(type_name, type_registry, application=application)
        or external_type_chain(type_name, type_registry, application=application) is not None
    )


def argument_array_typed(type_name: str, type_registry: DataDescription, *, application: bool) -> bool:
    """Return whether an argument typed *type_name* is array-typed, directly or at the end of its reference chain.

    Table 4.8 (SWCT R4.2.2 ``:4469``, R4.3.0 ``:4852``): an absent ``SERVER-ARGUMENT-IMPL-POLICY`` means
    ``useArgumentType`` for primitive arguments and structures and ``useArrayBaseType`` for arrays. Stating
    ``USE-ARGUMENT-TYPE`` on an array would state the opposite of the default, so the element is omitted there --
    rather than written as ``USE-ARRAY-BASE-TYPE``, which ``[constr_1297]`` (R4.2.2 ``:4618``) conditions on the
    mapped implementation type's category. An external type stub is not array-typed
    (:func:`_argument_policy_omitted` covers it).
    """
    declared: object | None = _referenced_type(type_name, type_registry, implementation=not application)
    if isinstance(declared, ReferenceDataType):
        declared = reference_chain_end(declared, type_registry)
    return isinstance(declared, ArrayDataType | ImplementationArrayDataType)


def _emit_interfaces(
    package: ArxmlPackage,
    interfaces: list[Interface],
    type_registry: DataDescription,
) -> list[etree._Element]:
    """Emit every port interface into *package*, in the order they were read.

    Interfaces have no ordering constraint among themselves: they reference data
    types only, and the ``DataTypes`` package is always emitted first.

    Returns:
        Every emitted element, in emission order.

    Raises:
        ValueError: if any referenced type is undeclared.
    """
    emitted: list[etree._Element] = []
    for iface in interfaces:
        if isinstance(iface, SRInterface):
            # Each interface's init constants immediately before it, so every
            # reference into them points backwards.
            emitted.extend(_emit_init_constants(package, iface, type_registry))
            emitted.append(_emit_sr_interface(package, iface, type_registry))
        elif isinstance(iface, CSInterface):
            emitted.append(_emit_cs_interface(package, iface, type_registry))
        # An external interface stub emits nothing: its target is declared
        # outside the project, and a port naming it carries the stub's own
        # reference pair (`_emit_port`).
    return emitted


# ---------------------------------------------------------------------------
# Software-component emitters
# ---------------------------------------------------------------------------


def _check_swc_type(swc: SWComponent) -> None:
    """Reject a component this writer has no element type for.

    ``APPLICATION-SW-COMPONENT-TYPE`` is the only component element it emits.

    Raises:
        ValueError: if ``swc.type`` is not ``application``.
    """
    if swc.type != "application":
        raise ValueError(
            f"SWC type '{swc.type}' is not supported — only 'application' SWCs are generated "
            "(APPLICATION-SW-COMPONENT-TYPE is the only component element the ARXML writer emits)"
        )


def _ordered_ports(swc: SWComponent) -> list[Any]:
    """Return *swc*'s ports provide-first, declaration order kept inside each group.

    The order is load-bearing: ``component_exchange/divergence.py`` applies a
    stable provide-first sort on the way back in to match it, so emitting
    declaration order would stop component round trips being Pydantic-equal.
    """
    return [p for p in swc.ports if p.direction == "provide"] + [p for p in swc.ports if p.direction != "provide"]


def _event_base_name(
    runnable: Runnable, event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent
) -> str:
    """Return the ``SHORT-NAME`` one event is derived to, before :func:`_unique_event_names` runs.

    ``TMT_``/``IT_`` are named for the runnable; an operation event ``OIT_{port}_{operation}``, a data-received
    event ``DRT_{port}_{element}`` and a call-returned event ``ASRT_{port}_{operation}``. Any other kind raises
    ``TypeError``. This is the **derived** form, written only where the event states no ``name``.
    """
    if isinstance(event, TimingEvent):
        return f"{TIMING_EVENT_PREFIX}{runnable.name}"
    if isinstance(event, InitEvent):
        return f"{INIT_EVENT_PREFIX}{runnable.name}"
    if isinstance(event, OperationEvent):
        return f"{OPERATION_EVENT_PREFIX}{event.port}_{event.operation}"
    if isinstance(event, DataEvent):
        return f"{DATA_EVENT_PREFIX}{event.port}_{event.element}"
    if isinstance(event, CallReturnedEvent):
        return f"{RETURNS_EVENT_PREFIX}{event.port}_{event.operation}"
    raise TypeError(f"an event kind the ARXML writer has not been taught: {type(event).__name__}")


def event_element(event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent) -> str:
    """Return the element one event is emitted as: one explicit arm per kind, and no ``else`` meaning ``INIT-EVENT``.

    The one source of each event element's name: the emission, the identifier
    guards here and ``writer/elements.py``'s pass-D guard all read it.
    """
    if isinstance(event, TimingEvent):
        return "TIMING-EVENT"
    if isinstance(event, InitEvent):
        return "INIT-EVENT"
    if isinstance(event, OperationEvent):
        return "OPERATION-INVOKED-EVENT"
    if isinstance(event, DataEvent):
        return "DATA-RECEIVED-EVENT"
    if isinstance(event, CallReturnedEvent):
        return "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT"
    raise TypeError(f"an event kind the ARXML writer has not been taught: {type(event).__name__}")


def _unique_event_names(base_names: list[str]) -> list[str]:
    """Resolve colliding event base names into pairwise distinct ones.

    The counter is **retroactive**: the first event to carry a name is renamed
    to ``{base}_0`` when a second one wants the same base, and the second
    becomes ``{base}_1``. Names that already end in ``{base}_{n}`` push the
    counter past ``n``. Both the rename and the new name skip an index already
    taken (else ``["A", "A_0", "A"]`` would give two ``A_0``); the index test is
    ``{base}_`` plus an all-digit remainder, so ``A_12x`` is not index 12.

    The MCP tools allow at most one timing and one init event per runnable, but
    ``generate_arxml()`` reads hand-edited YAML and applies no such guard.

    Returns:
        The final names, in the same order and of the same length. They are
        pairwise distinct.
    """
    names: list[str] = []
    taken: set[str] = set()

    def _free(base: str, start: int) -> str:
        index = start
        while f"{base}_{index}" in taken:
            index += 1
        return f"{base}_{index}"

    for base in base_names:
        prefix = f"{base}_"
        highest = 0
        has_index = False
        collision: int | None = None
        for position, existing in enumerate(names):
            if existing == base:
                collision = position
            elif existing.startswith(prefix) and existing[len(prefix) :].isdigit():
                has_index = True
                highest = max(highest, int(existing[len(prefix) :]))
        if collision is not None:
            taken.discard(names[collision])
            names[collision] = _free(base, 0)
            taken.add(names[collision])
        name = _free(base, highest + 1) if (has_index or collision is not None) else base
        names.append(name)
        taken.add(name)
    return names


#: The attributes of :data:`COM_SPEC_SILENT_VALUES` a ``NONQUEUED-SENDER-COM-SPEC``
#: holds (``group SENDER-COM-SPEC``, XSD 4.2.2 ``:55999``/``:56017``, 4.3.0
#: ``:64748``/``:64766``), in the table's order, which is the XSD's. The other four
#: are a non-queued receiver's alone. They are also the two a queued sender and a
#: queued receiver write: ``group RECEIVER-COM-SPEC`` holds the same two.
_SENDER_SILENT_TAGS = ("HANDLE-OUT-OF-RANGE", "USES-END-TO-END-PROTECTION")


def com_spec_value(entry: PortComSpec | None, tag: str) -> bool | float | str | None:
    """Return the value *entry* states for the com-spec attribute *tag*, or ``None`` when it is silent.

    One named attribute read per tag, never ``getattr`` over
    :data:`COM_SPEC_SILENT_VALUES`' field names: the coverage field oracle binds
    a read to the attribute it names and abstains on a computed one.
    """
    if entry is None:
        return None
    stated: dict[str, bool | float | str | None] = {
        "HANDLE-OUT-OF-RANGE": entry.handle_out_of_range,
        "USES-END-TO-END-PROTECTION": entry.uses_end_to_end_protection,
        "ALIVE-TIMEOUT": entry.alive_timeout,
        "ENABLE-UPDATE": entry.enable_update,
        "HANDLE-NEVER-RECEIVED": entry.handle_never_received,
        "HANDLE-TIMEOUT-TYPE": entry.handle_timeout_type,
    }
    return stated[tag]


def _emit_silent_com_spec_attribute(spec: etree._Element, tag: str, entry: PortComSpec | None = None) -> etree._Element:
    """Append *tag* to *spec* with the value *entry* states, else its :data:`COM_SPEC_SILENT_VALUES` silent value.

    A stated silent value writes the same bytes as silence. An enumeration is written as its XSD literal (``none``
    -> ``NONE``); a boolean and a number through ``format_value``.
    """
    attribute = COM_SPEC_SILENT_VALUES[tag]
    value = com_spec_value(entry, tag)
    if value is None:
        value = attribute.silent
    if attribute.literals is not None:
        value = attribute.literals[str(value)]
    return sub_element(spec, tag, value)


def _port_com_spec_entries(port: Any) -> dict[str, PortComSpec]:
    """Return a component port's ``com_specs`` by data element; empty for a composition port, which carries none.

    A composition port states no com-spec value and writes the silent values.
    The first entry per element wins; a second is
    ``rule:swc.port.com_specs.no-duplicate-entry``'s, refused at load.
    """
    if not isinstance(port, Port):
        return {}
    entries: dict[str, PortComSpec] = {}
    for entry in port.com_specs:
        entries.setdefault(entry.data_element, entry)
    return entries


def _member_path(iface: Interface, member: str) -> str:
    """Return the path of *iface*'s member *member*: under the placed interface, or under a stub's ``external_ref``.

    A member of an **external interface stub** is written
    ``{external_ref}/{member}``, both halves verbatim: the member rules have
    resolved the name against the stub's list before anything is emitted. Every
    site that references a member composes its path here.
    """
    if isinstance(iface, ExternalInterface):
        return f"{iface.external_ref}/{member}"
    return f"{_placed_path(_HOME.PORT_INTERFACES, iface.name)}/{member}"


def _emit_com_specs(
    port_node: etree._Element, port: Any, iface: Interface, type_registry: DataDescription | None = None
) -> etree._Element | None:
    """Emit a port's com-specs, or nothing when none is owed.

    One com-spec per interface member, in declaration order; the body follows from the port direction and the
    interface kind. A composition's require port whose every element is queued gets no wrapper at all. SWCT below is
    ``AUTOSAR_TPS_SoftwareComponentTemplate``; every XSD group named is in both ``AUTOSAR_4-2-2.xsd`` and
    ``AUTOSAR_4-3-0.xsd``.

    **A non-queued com-spec ends with its ``INIT-VALUE``** (XSD groups ``NONQUEUED-SENDER-COM-SPEC`` and
    ``NONQUEUED-RECEIVER-COM-SPEC``; 4.3.0's ``TIMEOUT-SUBSTITUTION-VALUE`` follows and is not written). A port
    entry's ``init`` is written in place (:func:`_emit_value_specification`): a com-spec's value supersedes the data
    prototype's (SWCT R4.2.2 and R4.3.0 ``[TPS_SWCT_01182]``), and a constant reference and the value in place are one
    value (``ConstantReference``, SWCT R4.2.2 Table 5.123, R4.3.0 Table 5.130), so no constant name is invented.
    Otherwise the element's own reference is repeated (:func:`_emit_init_value_reference`), or nothing.

    **The sender-receiver attributes are** :data:`COM_SPEC_SILENT_VALUES`' silent values in the XSD's order (groups
    ``SENDER-COM-SPEC``, ``RECEIVER-COM-SPEC``, ``NONQUEUED-RECEIVER-COM-SPEC``), replaced attribute by attribute by
    the port's ``com_specs`` entry. Each is multiplicity 1 with no default (SWCT R4.2.2 Tables 4.60, 4.61, 4.67;
    R4.3.0 Tables 4.62, 4.63, 4.69, except ``ReceiverComSpec.usesEndToEndProtection``, ``0..1`` there); ``none`` is
    *"A range check is not required"* (R4.2.2 Table 4.71, R4.3.0 Table 4.73) and *"no replacement shall take place"*
    (R4.2.2 Table 4.63, R4.3.0 Table 4.65). **No ``FILTER`` is written**: ``filter`` is ``0..1`` (R4.2.2 Table 4.61,
    R4.3.0 Table 4.63), and ``[constr_1044]`` (SWCT R4.2.2 and R4.3.0) allows a data filter only on *"values with an
    integer base type"*.

    **A queued element takes the queued kinds** (SWCT R4.2.2 and R4.3.0 ``[TPS_SWCT_01219]`` and
    ``[constr_1129]``-``[constr_1132]``): ``QUEUED-SENDER-COM-SPEC`` with the two sender attributes (its own XSD group
    is empty), ``QUEUED-RECEIVER-COM-SPEC`` with the same two and then ``QUEUE-LENGTH`` (``queueLength`` is
    multiplicity 1 with no default, SWCT R4.2.2 Table 4.62, R4.3.0 Table 4.64). Neither has an ``INIT-VALUE``. A queued
    element received with no ``queue_length`` gets no com-spec; :func:`_check_queued_receivers` refuses that on a
    component port, so only a composition's port reaches it.

    **A port on an external interface stub** gets one com-spec per listed member, by the stub's ``external_dest``:
    server/client for a client-server stub, non-queued sender/receiver with no ``INIT-VALUE`` for a sender-receiver
    one; any other ``DEST`` and an empty list write none. A stub port never gets a queued or a mode-switch com-spec.
    """
    provided = port.direction == "provide"
    specs = sub_element(port_node, "PROVIDED-COM-SPECS" if provided else "REQUIRED-COM-SPECS")
    if isinstance(iface, SRInterface):
        entries = _port_com_spec_entries(port)
        for data_element in iface.data_elements:
            entry = entries.get(data_element.name)
            queued = data_element.sw_impl_policy == "queued"
            queue_length = None if entry is None else entry.queue_length
            if queued and not provided and queue_length is None:
                continue
            path = _member_path(iface, data_element.name)
            if queued:
                spec = sub_element(specs, "QUEUED-SENDER-COM-SPEC" if provided else "QUEUED-RECEIVER-COM-SPEC")
            else:
                spec = sub_element(specs, "NONQUEUED-SENDER-COM-SPEC" if provided else "NONQUEUED-RECEIVER-COM-SPEC")
            reference_element(spec, "DATA-ELEMENT-REF", path, dest="VARIABLE-DATA-PROTOTYPE")
            for tag in _SENDER_SILENT_TAGS if provided or queued else COM_SPEC_SILENT_VALUES:
                _emit_silent_com_spec_attribute(spec, tag, entry)
            if queued:
                if queue_length is not None:
                    sub_element(spec, "QUEUE-LENGTH", queue_length)
                continue
            if entry is not None and entry.init is not None:
                # `_emit_swc` refuses a port init with no declared types before
                # anything is emitted, and a composition's port carries no
                # `com_specs`, so this holds by construction.
                assert type_registry is not None
                element_type, element_application = _prototype_type(data_element)
                _emit_value_specification(
                    sub_element(spec, "INIT-VALUE"),
                    entry.init,
                    element_type,
                    type_registry,
                    application=element_application,
                    own=True,
                )
            else:
                _emit_init_value_reference(spec, iface.name, data_element)
    elif isinstance(iface, CSInterface):
        for operation in iface.operations:
            _emit_operation_com_spec(specs, provided, _member_path(iface, operation.name))
    elif isinstance(iface, ExternalInterface):
        # Branch on the DEST, never on which list is non-empty:
        # `rule:interfaces.external_interface.member-kind` has refused a list the
        # DEST does not admit at load.
        if iface.external_dest == _INTERFACE_REF_DEST[CSInterface]:
            for operation_name in iface.operations:
                _emit_operation_com_spec(specs, provided, _member_path(iface, operation_name))
        elif iface.external_dest == _INTERFACE_REF_DEST[SRInterface]:
            entries = _port_com_spec_entries(port)
            for element_name in iface.data_elements:
                spec = sub_element(specs, "NONQUEUED-SENDER-COM-SPEC" if provided else "NONQUEUED-RECEIVER-COM-SPEC")
                reference_element(
                    spec, "DATA-ELEMENT-REF", _member_path(iface, element_name), dest="VARIABLE-DATA-PROTOTYPE"
                )
                for tag in _SENDER_SILENT_TAGS if provided else COM_SPEC_SILENT_VALUES:
                    _emit_silent_com_spec_attribute(spec, tag, entries.get(element_name))
    if len(specs) == 0:
        port_node.remove(specs)
        return None
    return specs


def _emit_operation_com_spec(specs: etree._Element, provided: bool, path: str) -> etree._Element:
    """Append one ``SERVER-COM-SPEC`` (a provide port) or ``CLIENT-COM-SPEC`` (a require port) for *path*.

    A server com-spec states ``QUEUE-LENGTH`` :data:`SERVER_QUEUE_LENGTH`;
    a client com-spec holds its ``OPERATION-REF`` alone.
    """
    spec = sub_element(specs, "SERVER-COM-SPEC" if provided else "CLIENT-COM-SPEC")
    reference_element(spec, "OPERATION-REF", path, dest="CLIENT-SERVER-OPERATION")
    if provided:
        sub_element(spec, "QUEUE-LENGTH", SERVER_QUEUE_LENGTH)
    return spec


def _emit_port(
    scope: ShortNameScope, port: Any, iface: Interface, type_registry: DataDescription | None = None
) -> etree._Element:
    """Emit one ``P-PORT-PROTOTYPE``/``R-PORT-PROTOTYPE`` into *scope*'s ``PORTS``.

    The scope rather than the bare ``PORTS`` element is the parameter because a
    provide port and a require port of the same name are siblings there, and
    ``PORT-REF``/``TARGET-P-PORT-REF`` cannot tell them apart.

    A port on an **external interface stub** carries the stub's ``external_ref``
    as its ``…-INTERFACE-TREF`` text and its ``external_dest`` as the ``DEST``,
    verbatim: the source export's own reference.
    :func:`~swcraft_tools.writer.composition_structure.check_external_stubs`
    has refused a malformed pair before anything is emitted.

    *type_registry* shapes a component port's own ``init``; a composition's port
    carries no ``com_specs`` and passes none.
    """
    provided = port.direction == "provide"
    node = scope.create("P-PORT-PROTOTYPE" if provided else "R-PORT-PROTOTYPE", port.name)
    set_description(node, port.description)
    _emit_com_specs(node, port, iface, type_registry)
    if isinstance(iface, ExternalInterface):
        path, dest = iface.external_ref, iface.external_dest
    else:
        path, dest = _placed_path(_HOME.PORT_INTERFACES, iface.name), _INTERFACE_REF_DEST[type(iface)]
    reference_element(
        node,
        "PROVIDED-INTERFACE-TREF" if provided else "REQUIRED-INTERFACE-TREF",
        path,
        dest=dest,
    )
    return node


def _emit_events(
    behavior: etree._Element,
    swc: SWComponent,
    behavior_path: str,
    port_interfaces: dict[str, tuple[Any, Interface]],
) -> etree._Element | None:
    """Emit ``EVENTS`` for every runnable's declared events, or nothing.

    Events are interleaved per runnable, in declaration order. An event that states a ``name`` is written with it;
    every other takes its derived name, counted over **every** event's base name, so no derived name moves.

    After ``START-ON-EVENT-REF``, an ``OPERATION-INVOKED-EVENT`` carries its ``OPERATION-IREF``, a
    ``DATA-RECEIVED-EVENT`` its ``DATA-IREF`` (``REFERRABLE``, ``RTE-EVENT``, then ``DATA-RECEIVED-EVENT``, one
    order at 4.2.2 and 4.3.0), an ``ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT`` its ``EVENT-SOURCE-REF`` to the result
    point on the runnable it starts. A timing and a data-received event's ``disabled_modes`` precede
    ``START-ON-EVENT-REF``. No ``DESC``, ``CATEGORY``, ``ACTIVATION-REASON-REPRESENTATION-REF`` or
    ``VARIATION-POINT`` is written: the model has no field for them and the standard gives none a default. Mixed
    event kinds are emitted as written, not judged.

    Returns:
        The ``EVENTS`` element, or None when no runnable declares an event.
    """
    pending = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    if not pending:
        return None
    derived = _unique_event_names([_event_base_name(runnable, event) for runnable, event in pending])
    # The component's placed path, which an event's own port is named under --
    # the operation event's provide port and the data-received event's require port.
    component_path = _placed_path(_HOME.COMPONENT_TYPES, swc.name)
    events = sub_element(behavior, "EVENTS")
    scope = ShortNameScope(events, f"EVENTS of SWC {swc.name!r}")
    for (runnable, event), name in zip(pending, derived, strict=True):
        node = scope.create(event_element(event), name if event.name is None else event.name)
        if isinstance(event, TimingEvent | DataEvent):
            _emit_disabled_modes(node, swc, event)
        reference_element(node, "START-ON-EVENT-REF", f"{behavior_path}/{runnable.name}", dest="RUNNABLE-ENTITY")
        if isinstance(event, TimingEvent):
            sub_element(node, "PERIOD", format_float(event.period))
        elif isinstance(event, DataEvent):
            # `DATA-IREF`, an `R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF`: the
            # component's own require port, then the port's interface's data
            # element -- under a stub's `external_ref` for an interface stub.
            iref = sub_element(node, "DATA-IREF")
            reference_element(
                iref,
                "CONTEXT-R-PORT-REF",
                f"{component_path}/{event.port}",
                dest="R-PORT-PROTOTYPE",
            )
            reference_element(
                iref,
                "TARGET-DATA-ELEMENT-REF",
                _member_path(port_interfaces[event.port][1], event.element),
                dest="VARIABLE-DATA-PROTOTYPE",
            )
        elif isinstance(event, OperationEvent):
            iref = sub_element(node, "OPERATION-IREF")
            reference_element(
                iref,
                "CONTEXT-P-PORT-REF",
                f"{component_path}/{event.port}",
                dest="P-PORT-PROTOTYPE",
            )
            reference_element(
                iref,
                "TARGET-PROVIDED-OPERATION-REF",
                _member_path(port_interfaces[event.port][1], event.operation),
                dest="CLIENT-SERVER-OPERATION",
            )
        elif isinstance(event, CallReturnedEvent):
            # `EVENT-SOURCE-REF`, after `group RTE-EVENT`, names the result point
            # `_emit_result_points` writes on this same runnable.
            reference_element(
                node,
                "EVENT-SOURCE-REF",
                f"{behavior_path}/{runnable.name}/{_result_point_name(event.port, event.operation, event.result)}",
                dest="ASYNCHRONOUS-SERVER-CALL-RESULT-POINT",
            )
    return events


def _emit_disabled_modes(
    node: etree._Element, swc: SWComponent, event: TimingEvent | DataEvent
) -> etree._Element | None:
    """Emit an event's ``DISABLED-MODE-IREFS``, or nothing for an event with none.

    ``RTEEvent.disabledMode`` is an instance reference ``*`` (Table 7.8 of SWCT R4.2.2), and the ``RTE-EVENT`` group
    puts ``DISABLED-MODE-IREFS`` before ``START-ON-EVENT-REF`` (XSD 4.2.2 ``:52030``, 4.3.0 ``:59044``), so the
    caller appends it right after the event's ``SHORT-NAME``. One ``DISABLED-MODE-IREF`` per mode, in model order,
    holding the ``R-MODE-IN-ATOMIC-SWC-INSTANCE-REF`` group's three references in its order (4.2.2 ``:51822``, 4.3.0
    ``:58836``): ``CONTEXT-PORT-REF`` to the component's own ``R-PORT-PROTOTYPE``, then the two mode references
    **verbatim** (SWCraft models no mode-switch interface).
    """
    if not event.disabled_modes:
        return None
    irefs = sub_element(node, "DISABLED-MODE-IREFS")
    for mode in event.disabled_modes:
        iref = sub_element(irefs, "DISABLED-MODE-IREF")
        reference_element(
            iref,
            "CONTEXT-PORT-REF",
            f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{mode.port}",
            dest="R-PORT-PROTOTYPE",
        )
        reference_element(
            iref,
            "CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF",
            mode.mode_group_ref,
            dest="MODE-DECLARATION-GROUP-PROTOTYPE",
        )
        reference_element(iref, "TARGET-MODE-DECLARATION-REF", mode.mode_ref, dest="MODE-DECLARATION")
    return irefs


#: One resolved access point, in emission order: the container tag, the derived
#: ``SHORT-NAME``, the port it goes through, that port's interface and the
#: interface member it names.
_AccessPoint = tuple[str, str, Any, Interface, str]


def _interface_member_names(iface: Interface) -> frozenset[str]:
    """Return every member name *iface* declares: data elements or operations.

    The two attributes are read by name rather than through ``getattr``, for the
    reason ``validator.access_entries`` states: the coverage oracle binds a
    field read to the attribute it names and abstains on a name it cannot narrow.
    """
    if isinstance(iface, SRInterface):
        return frozenset(element.name for element in iface.data_elements)
    if isinstance(iface, CSInterface):
        return frozenset(operation.name for operation in iface.operations)
    # An external interface stub's members are asked through
    # `validator.stub_member_names` before this is reached; this arm fails closed.
    return frozenset()


def _access_point_plan(
    swc: SWComponent,
    runnable: Runnable,
    port_interfaces: dict[str, tuple[Any, Interface]],
) -> list[_AccessPoint]:
    """Resolve *runnable*'s three access lists into the points to emit, or raise.

    Each refusal is one of the ``rule:swc.access.*`` findings; every offender of one runnable is batched into one
    ``ValueError``.

    Each point takes its entry's stated ``name``, else :func:`access_point_name`'s derived form.

    Returns:
        Every point to emit, in the schema's container order (:data:`_ACCESS_CONTAINER_ORDER`), then each list's
        declaration order; empty when the runnable declares none.

    Raises:
        ValueError: if an entry names a port the component does not declare, a port of the wrong interface kind or
            the wrong direction, or a member the port's interface (or stub list) does not declare; or if two
            unstated entries of one runnable derive one ``SHORT-NAME``.
    """

    # `(list name, container tag, port, member field, member, stated name)` for
    # every entry, the lists read as named attributes for the coverage oracle.
    entries: list[tuple[str, str, str, str, str, str | None]] = []
    # The form picks the container: an implicit read is a `dataReadAccess` in
    # `DATA-READ-ACCESSS`, an implicit write a `dataWriteAccess` in
    # `DATA-WRITE-ACCESSS`; the element is the same `VARIABLE-ACCESS`.
    for read in runnable.reads:
        container = _READ_CONTAINERS[read.by]
        entries.append(("reads", container, read.port, "element", read.element, read.name))
    for write in runnable.writes:
        container = "DATA-WRITE-ACCESSS" if write.by == "implicit" else "DATA-SEND-POINTS"
        entries.append(("writes", container, write.port, "element", write.element, write.name))
    for call in runnable.calls:
        entries.append(("calls", "SERVER-CALL-POINTS", call.port, "operation", call.operation, call.name))
    if not entries:
        return []

    planned: list[_AccessPoint] = []
    refusals: list[str] = []
    for list_name, container, port_name, member_field, member, stated in entries:
        needed_kind, needed_direction = ACCESS_LIST_NEEDS[list_name]
        subject = f"runnable {runnable.name!r} {list_name} entry {port_name!r}.{member!r}"
        resolved = port_interfaces.get(port_name)
        if resolved is None:
            refusals.append(f"{subject} names port {port_name!r}, which the component does not declare")
            continue
        port, iface = resolved
        # The kind across an external interface stub is never compared. The
        # entry resolves against the stub's own member list, as
        # `check_runnable_access` does: an unlisted member is refused before the
        # direction, so each entry gets one refusal.
        if isinstance(iface, ExternalInterface):
            members = stub_member_names(iface, member_field)
            if member not in members:
                rule = "swc.access.unknown-operation" if member_field == "operation" else "swc.access.unknown-element"
                refusals.append(
                    f"{subject} goes through port {port_name!r}, but "
                    f"{stub_member_miss(iface, member_field, member)} ({rule})"
                )
                continue
        elif iface.kind != needed_kind:
            refusals.append(
                f"{subject} goes through port {port_name!r}, whose interface {iface.name!r} is "
                f"{INTERFACE_KIND_PROSE[iface.kind]}; `{list_name}` needs a "
                f"{INTERFACE_KIND_PROSE[needed_kind]} port"
            )
            continue
        else:
            members = _interface_member_names(iface)
        if port.direction != needed_direction:
            refusals.append(
                f"{subject} goes through port {port_name!r}, which is a {port.direction} port; "
                f"`{list_name}` needs a {needed_direction} port"
            )
            continue
        if member not in members:
            refusals.append(
                f"{subject} names {member_field} {member!r} of port {port_name!r}, which interface "
                f"{iface.name!r} does not declare"
            )
            continue
        derived = access_point_name(ACCESS_LIST_PREFIXES[list_name], port_name, member)
        planned.append((container, derived if stated is None else stated, port, iface, member))

    if refusals:
        raise ValueError(
            f"SWC {swc.name!r} declares runnable access entries that do not resolve: "
            f"{'; '.join(refusals)}. `swcraft check-model` reports the same offenders under "
            "the `swc.access.*` rules."
        )

    # A stated name sharing a SHORT-NAME is `_check_access_point_names`'
    # refusal; this one names the derived collision, and the runnable's
    # `ShortNameScope` in `_emit_access_points` is the backstop behind both.
    owners: dict[str, list[str]] = {}
    for (_container, name, port, iface, member), (list_name, *_rest, stated) in zip(planned, entries, strict=True):
        if stated is None:
            owners.setdefault(name, []).append(
                f"{list_name} port {port.name!r} {'operation' if list_name == 'calls' else 'element'} {member!r}"
            )
    shared = [
        f"{name!r} is derived from " + " and ".join(claimants)
        for name, claimants in sorted(owners.items())
        if len(claimants) > 1
    ]
    if shared:
        raise ValueError(
            f"Two or more access entries of SWC {swc.name!r} runnable {runnable.name!r} derive one "
            f"access point SHORT-NAME: {'; '.join(shared)}. A port name and a member name may both "
            "contain '_', so the derivation is not injective; rename one of the conflicting names."
        )

    order = {tag: rank for rank, tag in enumerate(_ACCESS_CONTAINER_ORDER)}
    return sorted(planned, key=lambda point: order[point[0]])


def _emit_variable_access(
    container: etree._Element,
    name: str,
    swc: SWComponent,
    port: Any,
    iface: Interface,
    element: str,
) -> etree._Element:
    """Emit one ``VARIABLE-ACCESS``, a read or a write.

    The element is identical in every data container; the container says which
    access it is, and the port's direction moves the ``DEST``. Its shape is the
    minimal one the schema admits: ``SHORT-NAME`` then
    ``ACCESSED-VARIABLE/AUTOSAR-VARIABLE-IREF`` holding exactly the two
    references, with no ``SCOPE`` and no ``ROOT-``/``CONTEXT-DATA-PROTOTYPE-REF``.
    The paths are the port's own and the one :func:`_emit_com_specs` writes, so
    ``arxml_import/swc_mapping.py``'s exact-path resolution reads the output back.
    """
    node = named_element(container, "VARIABLE-ACCESS", name)
    iref = sub_element(sub_element(node, "ACCESSED-VARIABLE"), "AUTOSAR-VARIABLE-IREF")
    reference_element(
        iref,
        "PORT-PROTOTYPE-REF",
        f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{port.name}",
        dest="P-PORT-PROTOTYPE" if port.direction == "provide" else "R-PORT-PROTOTYPE",
    )
    reference_element(
        iref,
        "TARGET-DATA-PROTOTYPE-REF",
        _member_path(iface, element),
        dest="VARIABLE-DATA-PROTOTYPE",
    )
    return node


def _emit_server_call_point(
    container: etree._Element,
    name: str,
    swc: SWComponent,
    port: Any,
    iface: Interface,
    call: OperationCallRef,
) -> etree._Element:
    """Emit one ``SYNCHRONOUS-`` or ``ASYNCHRONOUS-SERVER-CALL-POINT``.

    The synchronous point is written in one shape: ``SHORT-NAME``,
    ``OPERATION-IREF`` holding the port and the operation, and ``TIMEOUT``
    :data:`_CALL_POINT_TIMEOUT`. ``CALLED-FROM-WITHIN-EXCLUSIVE-AREA-REF`` is
    not written.

    **An asynchronous call** is the sibling element with the same
    ``OPERATION-IREF`` (``group SERVER-CALL-POINT`` holds it and ``TIMEOUT`` for
    both, ``group ASYNCHRONOUS-SERVER-CALL-POINT`` is an empty sequence, one
    order at 4.2.2 and 4.3.0), with ``TIMEOUT`` **only when the call states**
    ``timeout``, written exactly through ``format_float``. Never an invented
    ``0``: ``TIMEOUT``
    is ``minOccurs="0"`` in both XSDs (4-2-2.xsd ``:56707``, 4-3-0.xsd
    ``:65456``), so an absent one is valid at both versions and partial under
    ``[TPS_GST_00057]``, and **not complete** against the meta-model's
    multiplicity 1 -- what the source export states, and no entry of the
    input-vs-output oracle's allowed differences.
    """
    asynchronous = call.asynchronous
    node = named_element(
        container, "ASYNCHRONOUS-SERVER-CALL-POINT" if asynchronous else "SYNCHRONOUS-SERVER-CALL-POINT", name
    )
    iref = sub_element(node, "OPERATION-IREF")
    reference_element(
        iref,
        "CONTEXT-R-PORT-REF",
        f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{port.name}",
        dest="R-PORT-PROTOTYPE",
    )
    reference_element(
        iref,
        "TARGET-REQUIRED-OPERATION-REF",
        _member_path(iface, call.operation),
        dest="CLIENT-SERVER-OPERATION",
    )
    if not asynchronous:
        sub_element(node, "TIMEOUT", _CALL_POINT_TIMEOUT)
    elif call.timeout is not None:
        sub_element(node, "TIMEOUT", format_float(call.timeout))
    return node


def _emit_access_points(
    node: etree._Element,
    swc: SWComponent,
    runnable: Runnable,
    port_interfaces: dict[str, tuple[Any, Interface]],
    scope: ShortNameScope,
    local_reads: list[tuple[str, str]],
) -> None:
    """Emit *runnable*'s access containers into its ``RUNNABLE-ENTITY``.

    Called between ``CAN-BE-INVOKED-CONCURRENTLY`` and ``SYMBOL``, where both
    licensed schemas put the containers. Each container is created on its first
    entry and in the schema's order, so a runnable that declares no list emits
    none.

    **The ``SHORT-NAME`` scope is the runnable, not a container**: the container
    carries no path segment, so all containers share one name space. *scope* is
    built on the ``RUNNABLE-ENTITY`` by :func:`_emit_runnables`, so the ``RV_`` /
    ``WV_`` names and the port access names claim it together; it is the
    structural backstop behind :func:`_access_point_plan` and
    :func:`_check_access_point_names`.

    The runnable's ``READ-LOCAL-VARIABLES`` (*local_reads*) is written here too,
    because the schema puts it between the port containers and
    ``SERVER-CALL-POINTS`` (``group RUNNABLE-ENTITY``, one order at 4.2.2 and
    4.3.0).
    """
    planned = _access_point_plan(swc, runnable, port_interfaces)
    # The plan is in the schema's container order, the call points last: the port
    # containers, then `READ-LOCAL-VARIABLES`, then `SERVER-CALL-POINTS`.
    port_points = [entry for entry in planned if entry[0] != "SERVER-CALL-POINTS"]
    call_points = [entry for entry in planned if entry[0] == "SERVER-CALL-POINTS"]
    containers: dict[str, etree._Element] = {}
    for container_tag, name, port, iface, member in port_points:
        scope.claim(name)
        container = containers.get(container_tag)
        if container is None:
            container = sub_element(node, container_tag)
            containers[container_tag] = container
        _emit_variable_access(container, name, swc, port, iface, member)
    _emit_local_variable_accesses(node, "READ-LOCAL-VARIABLES", swc, local_reads, scope)
    if call_points:
        # One runnable holds one `(port, operation)` pair once (refused at load),
        # so the pair finds the call whose synchrony and timeout the point takes.
        calls = {(call.port, call.operation): call for call in runnable.calls}
        container = sub_element(node, "SERVER-CALL-POINTS")
        for _tag, name, port, iface, member in call_points:
            scope.claim(name)
            _emit_server_call_point(container, name, swc, port, iface, calls[(port.name, member)])


def _local_access_plan(swc: SWComponent, runnable: Runnable) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return *runnable*'s variable accesses as ``(SHORT-NAME, variable)``, reads and writes apart.

    Each entry takes its stated ``name``, else ``RV_{variable}`` /
    ``WV_{variable}`` (``validator.emitted_variable_access_names`` is the same
    rule), in each list's own order. Each list is read by its own attribute,
    not through ``validator.variable_access_entries``, so the coverage oracle
    sees this module read ``variable_read.variable`` and ``variable_write.variable``.

    Raises:
        ValueError: on an entry naming no variable of *swc*, a backstop for
            :func:`_check_inter_runnable_variables`
            (``rule:swc.variable-access.unknown-variable``; ``[constr_2026]``:
            the variable is owned by the runnable's own behaviour).
    """
    declared = {variable.name for variable in swc.variables}
    planned: dict[str, list[tuple[str, str]]] = {"reads_variables": [], "writes_variables": []}
    entries = [
        *(("reads_variables", read.variable, read.name) for read in runnable.reads_variables),
        *(("writes_variables", write.variable, write.name) for write in runnable.writes_variables),
    ]
    for list_name, variable, stated in entries:
        if variable not in declared:
            raise ValueError(
                f"SWC {swc.name!r} runnable {runnable.name!r} {list_name} entry names variable {variable!r}, "
                "which the component does not declare (rule:swc.variable-access.unknown-variable)"
            )
        derived = f"{VARIABLE_ACCESS_LIST_PREFIXES[list_name]}{variable}"
        planned[list_name].append((derived if stated is None else stated, variable))
    return planned["reads_variables"], planned["writes_variables"]


def _emit_local_variable_accesses(
    node: etree._Element,
    tag: str,
    swc: SWComponent,
    planned: list[tuple[str, str]],
    scope: ShortNameScope,
) -> None:
    """Emit ``READ-LOCAL-VARIABLES`` or ``WRITTEN-LOCAL-VARIABLES`` into a runnable, or nothing.

    One ``VARIABLE-ACCESS`` per entry, in *planned*'s order: ``SHORT-NAME``,
    then ``ACCESSED-VARIABLE`` holding one ``LOCAL-VARIABLE-REF``
    ``DEST="VARIABLE-DATA-PROTOTYPE"`` -- the form ``[TPS_SWCT_01521]`` requires
    for both roles -- naming the variable
    :func:`_emit_inter_runnable_variables` writes in the same behaviour
    (``[constr_2026]``). **No ``SCOPE`` is written**: the model has no field for
    it, and the element is optional at both versions. Each name is claimed in
    *scope*, the runnable's one name space.
    """
    if not planned:
        return
    container = sub_element(node, tag)
    behaviour_path = f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{_behavior_name(swc)}"
    for name, variable in planned:
        scope.claim(name)
        access = named_element(container, "VARIABLE-ACCESS", name)
        reference_element(
            sub_element(access, "ACCESSED-VARIABLE"),
            "LOCAL-VARIABLE-REF",
            f"{behaviour_path}/{variable}",
            dest="VARIABLE-DATA-PROTOTYPE",
        )


def _has_access_points(swc: SWComponent) -> bool:
    """Return whether any runnable of *swc* declares at least one access entry."""
    return any(runnable.reads or runnable.writes or runnable.calls for runnable in swc.runnables)


def _emit_marker(behavior: etree._Element, marker: Marker) -> None:
    """Add *marker*'s ``SD`` to *behavior*'s ``SWCRAFT`` group, opening the group when absent.

    The ``SD`` joins the group after every ``SD`` in it. The group's ``ADMIN-DATA`` sits immediately after the
    behaviour's ``SHORT-NAME`` (valid there at 4.2.2 and 4.3.0, refused after ``EVENTS``).
    """
    datum = sub_element(_swcraft_marker_group(behavior), "SD", marker.text)
    datum.set("GID", marker.sd_gid)


def _emit_data_type_mapping_refs(
    behavior: etree._Element,
    swc: SWComponent,
    mapping_set_names: frozenset[str],
) -> etree._Element | None:
    """Emit ``DATA-TYPE-MAPPING-REFS`` for the sets *swc* names, or nothing.

    Every name is resolved **before** the container is created, so an undeclared
    set leaves no half-written block behind. The order is the component's
    declaration order, not sorted: ``import model`` reads it back in source
    order, so a sorted one would not round-trip.

    Returns:
        The created ``DATA-TYPE-MAPPING-REFS``, or None when the component names
        no set.

    Raises:
        ValueError: naming every entry that resolves to no declared set;
            ``check-model`` reports the same model one issue at a time under
            ``rule:swc.unknown-mapping-set``.
    """
    if not swc.data_type_mappings:
        return None
    unknown = [name for name in swc.data_type_mappings if name not in mapping_set_names]
    if unknown:
        raise ValueError(
            f"SWC '{swc.name}' names mapping set(s) {sorted(set(unknown))}, which are not declared in "
            "data_types/mapping_sets/ ('swcraft check-model' reports the same model)."
        )
    refs = sub_element(behavior, "DATA-TYPE-MAPPING-REFS")
    for name in swc.data_type_mappings:
        reference_element(
            refs,
            "DATA-TYPE-MAPPING-REF",
            _placed_path(_HOME.MAPPING_SETS, name),
            dest="DATA-TYPE-MAPPING-SET",
        )
    return refs


def _swcraft_marker_group(behavior: etree._Element) -> etree._Element:
    """Return *behavior*'s ``<SDG GID="SWCRAFT">``, found where it stands and created only where it is missing.

    Each of the three levels is looked up before anything is created: ``ADMIN-DATA``
    goes immediately after the behaviour's ``SHORT-NAME`` (the position the schema
    admits for an ``IDENTIFIABLE``'s ``ADMIN-DATA``) and holds at most one ``SDGS``,
    so every capability marker shares one ``ADMIN-DATA``, ``SDGS`` and
    ``SWCRAFT`` group whichever runs first.
    """
    admin = next((child for child in behavior if etree.QName(child).localname == "ADMIN-DATA"), None)
    if admin is None:
        admin = sub_element(behavior, "ADMIN-DATA")
        behavior.insert(1, admin)  # moved from the end to the place after SHORT-NAME
    groups = next((child for child in admin if etree.QName(child).localname == "SDGS"), None)
    if groups is None:
        groups = sub_element(admin, "SDGS")
    group = next(
        (child for child in groups if etree.QName(child).localname == "SDG" and child.get("GID") == SDG_GID),
        None,
    )
    if group is None:
        group = sub_element(groups, "SDG")
        group.set("GID", SDG_GID)
    return group


def _emit_runnables(
    behavior: etree._Element,
    swc: SWComponent,
    port_interfaces: dict[str, tuple[Any, Interface]],
    behavior_path: str,
) -> etree._Element | None:
    """Emit ``RUNNABLES``, or nothing for a runnable-less component.

    ``MINIMUM-START-INTERVAL`` is written exactly, through ``format_float``.
    ``SYMBOL`` is the runnable's ``symbol`` when stated and its name otherwise:
    ``RunnableEntity.symbol`` is the ``CIdentifier`` naming the entry point,
    multiplicity 1 (Table 7.3 of SWCT R4.2.2, Table 7.4 of R4.3.0), and
    ``SYMBOL`` is ``AR:C-IDENTIFIER`` in ``group RUNNABLE-ENTITY`` (XSD 4.2.2
    ``:54774``, 4.3.0 ``:62730``); :func:`_check_runnable_symbols` has refused a
    stated one that is not a C identifier or that two runnables share.

    Child order, one order at 4.2.2 and 4.3.0: ``group EXECUTABLE-ENTITY``
    orders ``CAN-ENTER-…``, ``EXCLUSIVE-AREA-NESTING-ORDER-REFS``,
    ``MINIMUM-START-INTERVAL``, ``REENTRANCY-LEVEL``, ``RUNS-INSIDE-…`` and
    precedes ``group RUNNABLE-ENTITY``; then the result points
    (:func:`_emit_result_points`), ``CAN-BE-INVOKED-CONCURRENTLY``, the access
    containers (:func:`_emit_access_points`, resolved against
    *port_interfaces*), ``SYMBOL`` and ``WRITTEN-LOCAL-VARIABLES``.
    *behavior_path* is the path every reference is written under.
    """
    if not swc.runnables:
        return None
    runnables = sub_element(behavior, "RUNNABLES")
    scope = ShortNameScope(runnables, f"RUNNABLES of SWC {swc.name!r}")
    for runnable in swc.runnables:
        node = scope.create("RUNNABLE-ENTITY", runnable.name)
        set_description(node, runnable.description)
        can_enter, runs_inside = _exclusive_area_plan(swc, runnable)
        _emit_exclusive_area_refs(node, _CAN_ENTER_EXCLUSIVE_AREA_ROLE, can_enter, behavior_path)
        sub_element(node, "MINIMUM-START-INTERVAL", format_float(runnable.min_start_interval))
        _emit_exclusive_area_refs(node, _RUNS_INSIDE_EXCLUSIVE_AREA_ROLE, runs_inside, behavior_path)
        # One name space for the port access points, the variable accesses and
        # the result points, built once before every emitter.
        access_scope = ShortNameScope(node, f"RUNNABLE-ENTITY {runnable.name!r} of SWC {swc.name!r}")
        _emit_result_points(node, swc, runnable, behavior_path, access_scope)
        sub_element(node, "CAN-BE-INVOKED-CONCURRENTLY", runnable.concurrent)
        local_reads, local_writes = _local_access_plan(swc, runnable)
        _emit_access_points(node, swc, runnable, port_interfaces, access_scope, local_reads)
        sub_element(node, "SYMBOL", runnable.symbol if runnable.symbol is not None else runnable.name)
        # The schema puts `WRITTEN-LOCAL-VARIABLES` after `SYMBOL` and `WAIT-POINTS`.
        _emit_local_variable_accesses(node, "WRITTEN-LOCAL-VARIABLES", swc, local_writes, access_scope)
    return runnables


def _call_point_name(call: OperationCallRef) -> str:
    """Return the ``SHORT-NAME`` of *call*'s call point: its stated ``name``, else ``Call_{port}_{operation}``.

    :func:`_access_point_plan`'s rule for a ``calls`` entry, for a result point's
    reference.
    """
    if call.name is not None:
        return call.name
    return access_point_name(ACCESS_LIST_PREFIXES["calls"], call.port, call.operation)


def _result_point_name(port: str, operation: str, stated: str | None) -> str:
    """Return a result point's ``SHORT-NAME``: the stated ``result``, else ``Result_{port}_{operation}``.

    ``validator.emitted_result_point_names`` is the same rule; the name guards
    have judged both forms before anything is emitted.
    """
    return stated if stated is not None else f"{RESULT_POINT_PREFIX}{port}_{operation}"


def _result_point_plan(swc: SWComponent, runnable: Runnable, behavior_path: str) -> list[tuple[str, str]]:
    """Return *runnable*'s result points as ``(SHORT-NAME, the call point's path)``, polls first.

    One per ``poll: true`` call, in ``calls`` order, referencing the runnable's
    **own** call point (a poll: the calling runnable fetches the result itself,
    ``[constr_2006]``); then one per ``call_returned`` event, in event order,
    referencing the component's one asynchronous call point of the event's pair,
    on whichever runnable makes the call (``[TPS_SWCT_01346]``'s example). The
    order is ``validator.result_point_entries``'.

    Raises:
        ValueError: on an event whose pair is not called asynchronously by
            exactly one runnable of *swc*, a backstop for
            :func:`_check_access_forms`.
    """
    planned: list[tuple[str, str]] = []
    for call in runnable.calls:
        if call.poll:
            planned.append(
                (
                    _result_point_name(call.port, call.operation, call.result),
                    f"{behavior_path}/{runnable.name}/{_call_point_name(call)}",
                )
            )
    for event in runnable.events:
        if not isinstance(event, CallReturnedEvent):
            continue
        callers = [
            (caller, call)
            for caller in swc.runnables
            for call in caller.calls
            if call.asynchronous and (call.port, call.operation) == (event.port, event.operation)
        ]
        if len(callers) != 1:
            raise ValueError(
                f"SWC {swc.name!r} runnable {runnable.name!r} has a call_returned event naming "
                f"{event.port!r}.{event.operation!r}, which {len(callers)} runnables of the component call "
                "asynchronously; its result point references exactly one call point "
                "(rule:swc.call-returned-event.unknown-call / ambiguous-call)"
            )
        ((caller, call),) = callers
        planned.append(
            (
                _result_point_name(event.port, event.operation, event.result),
                f"{behavior_path}/{caller.name}/{_call_point_name(call)}",
            )
        )
    return planned


def _emit_result_points(
    node: etree._Element,
    swc: SWComponent,
    runnable: Runnable,
    behavior_path: str,
    scope: ShortNameScope,
) -> None:
    """Emit *runnable*'s ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS``, or nothing.

    One ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` per :func:`_result_point_plan`
    entry: ``SHORT-NAME``, then ``ASYNCHRONOUS-SERVER-CALL-POINT-REF``
    ``DEST="ASYNCHRONOUS-SERVER-CALL-POINT"`` (the one value of its subtypes enum)
    -- the element's order at 4.2.2 and 4.3.0, where 4.3.0's
    ``ABSTRACT-ACCESS-POINT`` group adds no element. No ``VARIATION-POINT``: the
    model has no field for it. Each name is claimed in *scope*.
    """
    planned = _result_point_plan(swc, runnable, behavior_path)
    if not planned:
        return
    wrapper = sub_element(node, "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS")
    for name, call_point_path in planned:
        scope.claim(name)
        point = named_element(wrapper, "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT", name)
        reference_element(
            point, "ASYNCHRONOUS-SERVER-CALL-POINT-REF", call_point_path, dest="ASYNCHRONOUS-SERVER-CALL-POINT"
        )


#: The ``(wrapper, reference)`` tags each of a runnable's two exclusive area lists
#: is written as: the roles ``canEnterExclusiveArea`` and
#: ``runsInsideExclusiveArea`` of ``ExecutableEntity`` (``[TPS_SWCT_01049]``).
_CAN_ENTER_EXCLUSIVE_AREA_ROLE: tuple[str, str] = ("CAN-ENTER-EXCLUSIVE-AREA-REFS", "CAN-ENTER-EXCLUSIVE-AREA-REF")
_RUNS_INSIDE_EXCLUSIVE_AREA_ROLE: tuple[str, str] = (
    "RUNS-INSIDE-EXCLUSIVE-AREA-REFS",
    "RUNS-INSIDE-EXCLUSIVE-AREA-REF",
)


def _exclusive_area_plan(swc: SWComponent, runnable: Runnable) -> tuple[list[str], list[str]]:
    """Return the areas *runnable* names, ``can_enter`` and ``runs_inside`` apart, in declared order.

    Each list is read by its own attribute for the coverage oracle
    (:func:`_local_access_plan`'s reason).

    Raises:
        ValueError: on an entry naming no area of *swc*, a backstop for
            :func:`_check_exclusive_areas` (the InternalBehavior's
            ``exclusiveArea`` *is local to the component*, its class table in
            both TPS editions).
    """
    declared = {area.name for area in swc.exclusive_areas}
    can_enter = [entry.area for entry in runnable.can_enter]
    runs_inside = [entry.area for entry in runnable.runs_inside]
    for list_name, areas in (("can_enter", can_enter), ("runs_inside", runs_inside)):
        for area in areas:
            if area not in declared:
                raise ValueError(
                    f"SWC {swc.name!r} runnable {runnable.name!r} {list_name} entry names exclusive area {area!r}, "
                    "which the component does not declare (rule:swc.exclusive-area.unknown-area)"
                )
    return can_enter, runs_inside


def _emit_exclusive_area_refs(
    node: etree._Element,
    role: tuple[str, str],
    areas: list[str],
    behavior_path: str,
) -> None:
    """Emit one runnable's ``CAN-ENTER-…`` or ``RUNS-INSIDE-EXCLUSIVE-AREA-REFS``, or nothing.

    One reference per entry, in *areas*' order, each ``DEST="EXCLUSIVE-AREA"``
    (``EXCLUSIVE-AREA--SUBTYPES-ENUM``'s one value at both versions) naming
    ``{behavior_path}/{area}``, the area :func:`_emit_exclusive_areas` writes in
    the same behaviour. An empty list writes no wrapper.
    """
    if not areas:
        return
    wrapper_tag, reference_tag = role
    wrapper = sub_element(node, wrapper_tag)
    for area in areas:
        reference_element(wrapper, reference_tag, f"{behavior_path}/{area}", dest="EXCLUSIVE-AREA")


def _emit_exclusive_areas(behavior: etree._Element, swc: SWComponent) -> None:
    """Emit the component's ``EXCLUSIVE-AREAS`` into *behavior*, or nothing.

    One ``EXCLUSIVE-AREA`` per declared area, in declared order, holding its ``SHORT-NAME`` and, when the
    description is non-empty, its ``DESC`` -- the class has no attribute of its own (``ExclusiveArea``'s table,
    ``[TPS_SWCT_01031]``). No ``VARIATION-POINT``, no ``EXCLUSIVE-AREA-NESTING-ORDERS`` and (4.3.0) no
    ``EXCLUSIVE-AREA-POLICYS`` is written: the model has no field for them, and their absence is the standard's
    default (``apiPrinciple`` *common*; the nesting orders *the default scenario*, ``[TPS_SWCT_01459]``). A
    collision across the behaviour's containers is :func:`_check_name_space_collisions`' refusal.
    """
    if not swc.exclusive_areas:
        return
    wrapper = sub_element(behavior, "EXCLUSIVE-AREAS")
    scope = ShortNameScope(wrapper, f"EXCLUSIVE-AREAS of SWC {swc.name!r}")
    for area in swc.exclusive_areas:
        node = scope.create("EXCLUSIVE-AREA", area.name)
        set_description(node, area.description)


def _emit_handle_termination_and_restart(behavior: etree._Element, swc: SWComponent) -> None:
    """Emit ``HANDLE-TERMINATION-AND-RESTART`` when the model sets it.

    The position is the schema's: after ``EVENTS`` and ``EXPLICIT-INTER-RUNNABLE-VARIABLES``, before
    ``IMPLICIT-INTER-RUNNABLE-VARIABLES``, ``PORT-API-OPTIONS`` (``AUTOSAR_4-2-2.xsd:60954``,
    ``AUTOSAR_4-3-0.xsd:70249``; not emitted) and ``RUNNABLES``. A component that sets nothing gets **no** element,
    never a synthesized ``NO-SUPPORT``; :func:`_check_termination_and_restart` refuses that model later.
    """
    if swc.handle_termination_and_restart is None:
        return
    sub_element(
        behavior,
        "HANDLE-TERMINATION-AND-RESTART",
        HANDLE_TERMINATION_AND_RESTART_LITERALS[swc.handle_termination_and_restart],
    )


#: The ``SWC-INTERNAL-BEHAVIOR`` wrapper each value of a variable's ``access``
#: is written in: the roles
#: ``explicitInterRunnableVariable`` and ``implicitInterRunnableVariable`` of
#: ``[TPS_SWCT_01052]``.
_VARIABLE_WRAPPERS: dict[str, str] = {
    "explicit": "EXPLICIT-INTER-RUNNABLE-VARIABLES",
    "implicit": "IMPLICIT-INTER-RUNNABLE-VARIABLES",
}


def _emit_inter_runnable_variables(
    behavior: etree._Element,
    swc: SWComponent,
    access: str,
    scope: ShortNameScope,
    type_registry: DataDescription | None,
) -> None:
    """Emit the variables of one role into *behavior*, or nothing.

    The wrapper is :data:`_VARIABLE_WRAPPERS`' for *access*, the variables in declaration order. Each
    ``VARIABLE-DATA-PROTOTYPE`` holds, in the schema's order:

    * ``SHORT-NAME``, and ``DESC`` when the ``description`` is non-empty;
    * ``SW-DATA-DEF-PROPS`` with ``SW-CALIBRATION-ACCESS`` when one is stated. No ``SW-IMPL-POLICY``: the model has
      none, and ``standard``, the standard's default, is the only value ``[constr_2038]`` / ``[constr_2039]`` admit;
    * ``TYPE-TREF`` with the ``DEST`` of the resolved type;
    * ``INIT-VALUE`` with the ``init`` **in place** (``[TPS_SWCT_01637]``), never a new ``CONSTANT-SPECIFICATION``.

    *scope* is the behaviour's one name space for the variables of both roles.
    """
    variables = [variable for variable in swc.variables if variable.access == access]
    if not variables:
        return
    if type_registry is None:
        # A backstop, never an assert (`python -O` strips one): `_emit_swc` refuses a
        # component with variables and no declared types before anything is emitted.
        raise ValueError(f"SWC {swc.name!r} declares inter-runnable variables, and the model declares no data type")
    wrapper = sub_element(behavior, _VARIABLE_WRAPPERS[access])
    for variable in variables:
        scope.claim(variable.name)
        prototype = named_element(wrapper, "VARIABLE-DATA-PROTOTYPE", variable.name)
        set_description(prototype, variable.description)
        _emit_sw_data_def_props(prototype, variable.sw_calibration_access)
        variable_type, application = _prototype_type(variable)
        if variable_type is None:
            # A backstop: `rule:swc.variable.type-form` refuses a variable with neither field at load.
            raise ValueError(f"SWC {swc.name!r} variable {variable.name!r} states no type")
        _emit_type_reference(prototype, "TYPE-TREF", variable_type, type_registry, application=application)
        if variable.init is not None:
            _emit_value_specification(
                sub_element(prototype, "INIT-VALUE"),
                variable.init,
                variable_type,
                type_registry,
                application=application,
                own=True,
            )


def _behavior_name(swc: SWComponent) -> str:
    """Return the ``SHORT-NAME`` *swc*'s ``SWC-INTERNAL-BEHAVIOR`` is written with.

    The stated ``behavior_name``, else ``{swc}_InternalBehavior``
    (``validator.emitted_behavior_name``'s rule). Every path to the behaviour
    asks it, so all follow a stated name together.
    """
    return f"{swc.name}{INTERNAL_BEHAVIOR_SUFFIX}" if swc.behavior_name is None else swc.behavior_name


def _emit_internal_behavior(
    swc_node: etree._Element,
    swc: SWComponent,
    port_interfaces: dict[str, tuple[Any, Interface]],
    mapping_set_names: frozenset[str],
    type_registry: DataDescription | None,
) -> etree._Element:
    """Emit the component's ``SWC-INTERNAL-BEHAVIOR`` nested inside *swc_node*.

    Every optional container is omitted when empty. The children, in the schema's order (one at 4.2.2 and 4.3.0):
    the capability marker (only when a runnable declares an access entry), ``DATA-TYPE-MAPPING-REFS``,
    ``EXCLUSIVE-AREAS``, ``EVENTS``, ``EXPLICIT-INTER-RUNNABLE-VARIABLES``, ``HANDLE-TERMINATION-AND-RESTART``,
    ``IMPLICIT-INTER-RUNNABLE-VARIABLES``, ``RUNNABLES``, and always last ``SUPPORTS-MULTIPLE-INSTANTIATION``.

    No ``PORT-API-OPTIONS`` is written. The model states no port API option, ``SwcInternalBehavior.portAPIOption``
    is ``0..*`` (SWCT R4.2.2 ``:23196``, R4.3.0 ``:25726``) and the container ``minOccurs="0"``
    (``AUTOSAR_4-2-2.xsd:61042``, ``AUTOSAR_4-3-0.xsd:70337``), and the RTE acts on a flag set to true only
    (``[SWS_Rte_02613]``, ``[SWS_Rte_03799]``, ``[SWS_Rte_07100]``, SWS RTE R22-11), so no option and an option with
    both features off have one effect.

    Raises:
        ValueError: if ``data_type_mappings`` names a set the model does not declare
            (``rule:swc.unknown-mapping-set``).
    """
    behavior = named_element(sub_element(swc_node, "INTERNAL-BEHAVIORS"), "SWC-INTERNAL-BEHAVIOR", _behavior_name(swc))
    if _has_access_points(swc):
        _emit_marker(behavior, ACCESS_POINTS)
    _emit_data_type_mapping_refs(behavior, swc, mapping_set_names)
    _emit_exclusive_areas(behavior, swc)
    behavior_path = f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{_behavior_name(swc)}"
    _emit_events(behavior, swc, behavior_path, port_interfaces)
    variable_scope = ShortNameScope(behavior, f"inter-runnable variables of SWC {swc.name!r}")
    _emit_inter_runnable_variables(behavior, swc, "explicit", variable_scope, type_registry)
    _emit_handle_termination_and_restart(behavior, swc)
    _emit_inter_runnable_variables(behavior, swc, "implicit", variable_scope, type_registry)
    _emit_runnables(behavior, swc, port_interfaces, behavior_path)
    sub_element(behavior, "SUPPORTS-MULTIPLE-INSTANTIATION", False)
    return behavior


def _emit_swc_implementation(package: ArxmlPackage, swc: SWComponent) -> etree._Element:
    """Emit the ``<swc>_Implementation`` package-level sibling.

    Emitted immediately after the component itself, not in a second pass at the
    end of the package. Its ``BEHAVIOR-REF`` follows a stated ``behavior_name``.
    """
    node = package.create_element("SWC-IMPLEMENTATION", f"{swc.name}{IMPLEMENTATION_SUFFIX}")
    reference_element(
        node,
        "BEHAVIOR-REF",
        f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{_behavior_name(swc)}",
        dest="SWC-INTERNAL-BEHAVIOR",
    )
    return node


def emits_swc_timing(swc: SWComponent) -> bool:
    """Return whether the writer emits ``{swc}_Timing`` for *swc*.

    A non-external component with at least one runnable declaring a ``wcet``. Every guard that reserves ``_Timing``
    asks it; ``validator.py``'s restatement is pinned to it by ``packages/tools/tests/test_swc_timing_writer.py``.
    """
    return not swc.external and any(runnable.wcet is not None for runnable in swc.runnables)


def _emit_swc_timing(package: ArxmlPackage, swc: SWComponent) -> etree._Element | None:
    """Emit ``{swc}_Timing``, the component's execution-time budgets.

    One ``SWC-TIMING`` per component that :func:`emits_swc_timing`; ``None`` and nothing emitted otherwise. In the
    XSD's order (identical at 4.2.2 and 4.3.0): the ``SHORT-NAME``, one ``TIMING-REQUIREMENTS`` holding one
    ``EXECUTION-TIME-CONSTRAINT`` ``{runnable}_Budget`` per budgeted runnable in declared order, then
    ``BEHAVIOR-REF``. A constraint holds ``EXECUTABLE-REF`` to the runnable, ``EXECUTION-TIME-TYPE`` (``GROSS`` |
    ``NET``) and ``MAXIMUM`` as ``CSE-CODE`` -- the unit's code of TPS GST R4.3.0 Table 4.92 (R4.2.2
    ``[TPS_GST_00354]``) -- then ``CSE-CODE-FACTOR``, the integer as written: no conversion.

    **What it does not emit**: ``DESC``, ``TIMING-DESCRIPTIONS``, ``TIMING-GUARANTEES``, ``COMPONENT-REF``,
    ``COMPONENT-IREF``, ``MINIMUM``, ``TIMING-CONDITION-REF`` and ``VARIATION-POINT`` -- the model holds none of
    them -- and a task's ``wcet``, which no ``EXECUTABLE-ENTITY`` subtype can name.
    """
    if not emits_swc_timing(swc):
        return None
    budgeted = [runnable for runnable in swc.runnables if runnable.wcet is not None]
    node = package.create_element("SWC-TIMING", f"{swc.name}{TIMING_SUFFIX}")
    behavior_path = f"{_placed_path(_HOME.COMPONENT_TYPES, swc.name)}/{_behavior_name(swc)}"
    requirements = sub_element(node, "TIMING-REQUIREMENTS")
    scope = ShortNameScope(requirements, f"TIMING-REQUIREMENTS of SWC-TIMING '{swc.name}{TIMING_SUFFIX}'")
    for runnable in budgeted:
        budget = parse_budget(runnable.wcet)
        constraint = scope.create("EXECUTION-TIME-CONSTRAINT", f"{runnable.name}{BUDGET_SUFFIX}")
        reference_element(constraint, "EXECUTABLE-REF", f"{behavior_path}/{runnable.name}", dest="RUNNABLE-ENTITY")
        # The pairing rule (`Runnable.wcet_type_is_paired`) makes a stated `wcet`
        # carry its `wcet_type`, so the type is read, never defaulted.
        sub_element(constraint, "EXECUTION-TIME-TYPE", str(runnable.wcet_type).upper())
        maximum = sub_element(constraint, "MAXIMUM")
        sub_element(maximum, "CSE-CODE", budget.cse_code)
        sub_element(maximum, "CSE-CODE-FACTOR", budget.factor)
    reference_element(node, "BEHAVIOR-REF", behavior_path, dest="SWC-INTERNAL-BEHAVIOR")
    return node


def _emit_swc(
    package: ArxmlPackage,
    swc: SWComponent,
    interface_registry: dict[str, Interface],
    mapping_set_names: frozenset[str] = frozenset(),
    type_registry: DataDescription | None = None,
) -> tuple[etree._Element, etree._Element]:
    """Emit one component plus its implementation sibling into *package*.

    The component type, every port's interface and every ``description`` are judged before the first element is
    created.

    Args:
        mapping_set_names: every declared mapping set. The empty default is **test-only** and fail-closed: a
            component that names a set is refused under it.
        type_registry: the types a port's ``init`` and a variable are shaped by; ``None`` is test-only, likewise.

    Returns:
        The ``(APPLICATION-SW-COMPONENT-TYPE, SWC-IMPLEMENTATION)`` pair, in emission order.

    Raises:
        ValueError: if the component is not an ``application`` SWC, if a port names an undeclared interface, if a
            runnable access entry does not resolve (:func:`_access_point_plan`), if a ``description`` holds a
            character XML 1.0 cannot carry, or if a port ``init`` or a variable needs a *type_registry* and none was
            given.
    """
    _check_swc_type(swc)
    if type_registry is None and any(entry.init is not None for port in swc.ports for entry in port.com_specs):
        raise ValueError(
            f"SWC '{swc.name}' states a port init value in com_specs, and no declared data types were given "
            "to shape it by (a writer defect: every production call passes them)"
        )
    _refuse_unrepresentable_text(f"SWC '{swc.name}'", "description", swc.description)
    for port in swc.ports:
        _refuse_unrepresentable_text(f"SWC '{swc.name}' port '{port.name}'", "description", port.description)
    for runnable in swc.runnables:
        _refuse_unrepresentable_text(
            f"SWC '{swc.name}' runnable '{runnable.name}'", "description", runnable.description
        )
    for variable in swc.variables:
        _refuse_unrepresentable_text(
            f"SWC '{swc.name}' variable '{variable.name}'", "description", variable.description
        )
    for area in swc.exclusive_areas:
        _refuse_unrepresentable_text(f"SWC '{swc.name}' exclusive area '{area.name}'", "description", area.description)
    if type_registry is None and swc.variables:
        raise ValueError(
            f"SWC '{swc.name}' declares inter-runnable variables, and no declared data types were given "
            "to type them by (a writer defect: every production call passes them)"
        )
    ports = _ordered_ports(swc)
    interfaces = [_port_interface(swc, port, interface_registry) for port in ports]
    port_interfaces: dict[str, tuple[Any, Interface]] = {}
    for port, iface in zip(ports, interfaces):
        # The first declaration wins, as in every sibling map; a duplicate port
        # name is `_check_component_names`' and `ShortNameScope`'s to refuse.
        port_interfaces.setdefault(port.name, (port, iface))
    # Resolve every runnable's entries before the first element is created, so a
    # refused component leaves nothing behind. The result is recomputed at the
    # point of emission (a few dict lookups), keeping the emitters self-contained.
    for runnable in swc.runnables:
        _access_point_plan(swc, runnable, port_interfaces)
        _local_access_plan(swc, runnable)
        _exclusive_area_plan(swc, runnable)

    node = package.create_element(APPLICATION_SWC_TAG, swc.name)
    set_description(node, swc.description)
    ports_node = sub_element(node, "PORTS")
    port_scope = ShortNameScope(ports_node, f"PORTS of SWC {swc.name!r}")
    for port, iface in zip(ports, interfaces):
        _emit_port(port_scope, port, iface, type_registry)
    _emit_internal_behavior(node, swc, port_interfaces, mapping_set_names, type_registry)
    return node, _emit_swc_implementation(package, swc)


def _port_interface(
    swc: SWComponent,
    port: Any,
    interface_registry: dict[str, Interface],
) -> Interface:
    """Resolve a port's declared interface, or raise.

    Raises:
        ValueError: if the interface is not declared in
            ``model_root/interfaces/``.
    """
    if port.interface is None:
        # `interface` is optional at load, for an external stub's port; a
        # declared component's port without one is `port.undeclared-interface`.
        raise ValueError(
            f"SWC '{swc.name}' port '{port.name}' declares no interface; a port of a component that is "
            "not an external stub must name one declared in interfaces/"
        )
    iface = interface_registry.get(port.interface)
    if iface is None:
        raise ValueError(
            f"Interface '{port.interface}' referenced by SWC '{swc.name}' port "
            f"'{port.name}' is not declared in interfaces/"
        )
    return iface


def _emit_swcs(
    package: ArxmlPackage,
    swcs: list[SWComponent],
    interface_registry: dict[str, Interface],
    mapping_set_names: frozenset[str] = frozenset(),
    type_registry: DataDescription | None = None,
) -> list[etree._Element]:
    """Emit every non-external component into *package*, in the order they were read.

    Each contributes the component type and its implementation, and its ``{swc}_Timing`` where a runnable declares a
    ``wcet``. **An external stub contributes none**: it is emitted only as a prototype's reference.

    Returns:
        Every emitted element, in emission order.

    Raises:
        ValueError: if a component is not an ``application`` SWC, or a port names an undeclared interface.
    """
    emitted: list[etree._Element] = []
    for swc in swcs:
        if swc.external:
            continue
        emitted.extend(_emit_swc(package, swc, interface_registry, mapping_set_names, type_registry))
        timing = _emit_swc_timing(package, swc)
        if timing is not None:
            emitted.append(timing)
    return emitted


# ---------------------------------------------------------------------------
# Composition and connector emitters
# ---------------------------------------------------------------------------


def _check_connector_names(connectors: list[Connector]) -> None:
    """Reject a connector name that is not a legal AUTOSAR ``Identifier``.

    Runs unconditionally, unlike :func:`_check_connectors`; duplicates are ``check_connector_consistency``'s.

    Raises:
        ValueError: naming every offending connector.
    """
    _check_identifiers(
        (("Connector name", connector.name) for connector in connectors),
        "connectors/",
    )


def _component_type_path(
    field: str,
    name: str,
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
    *,
    owner: str,
) -> tuple[str, str]:
    """Return the ``(path, DEST)`` a reference to the component type *name* carries.

    *field* (``type`` for a ``swc`` element, ``composition`` for a composition) is the only thing dispatched on. An
    **external stub** carries the source's own ``external_ref`` and ``external_dest`` verbatim.

    Args:
        owner: the composition the reference is emitted inside, for the message.

    Raises:
        ValueError: if the layer *field* names does not declare *name* (``rule:composition.member-unknown-type`` /
            ``rule:composition.member-unknown-composition``).
    """
    if field == "type":
        swc = swc_map.get(name)
        if swc is None:
            raise ValueError(f"Composition '{owner}' places component '{name}', which is not declared in swc/.")
        if swc.external:
            return swc.external_ref, swc.external_dest
        return _placed_path(_HOME.COMPONENT_TYPES, name), APPLICATION_SWC_TAG
    if name not in composition_index:
        raise ValueError(f"Composition '{owner}' places composition '{name}', which is not declared in compositions/.")
    return _placed_path(_HOME.COMPONENT_TYPES, name), COMPOSITION_SWC_TAG


def _declared_ports(
    field: str,
    name: str,
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
) -> dict[str, Any]:
    """Return the ports of the component type *name*, by name, in the layer *field* names.

    A ``Port`` and a ``CompositionPort`` are interchangeable here: callers read
    only ``direction``. An undeclared *name* answers an empty map; the caller
    reports it against the port it was looking for.
    """
    if field == "type":
        swc = swc_map.get(name)
        return {} if swc is None else {port.name: port for port in swc.ports}
    composition = composition_index.get(name)
    return {} if composition is None else {port.name: port for port in composition.ports}


def _member_of_type(
    composition: Composition,
    endpoint: ConnectorEndpoint,
    connector_name: str,
) -> CompositionMember:
    """Return the member of *composition* whose type is *endpoint*'s, or raise.

    Unambiguous because a type is placed at most once per composition. The fields are matched by meaning (``swc``
    against ``type``, ``composition`` against ``composition``), so an endpoint naming a composition never resolves
    to a component of the same name.

    Raises:
        ValueError: if no member of *composition* has that type
            (``rule:composition.connector-endpoint-not-a-member``).
    """
    wanted = _endpoint_type_ref(endpoint)
    for member in composition.members:
        if member_type_ref(member) == wanted:
            return member
    raise ValueError(
        f"Connector '{connector_name}' names type '{wanted[1]}', which is not a member of "
        f"composition '{composition.name}' in compositions/."
    )


def _endpoint_type_ref(endpoint: ConnectorEndpoint) -> tuple[str, str]:
    """Return ``(field, type name)`` of what a connector endpoint names.

    Keyed on the member's field names (``swc`` -> ``type``) so the answer
    compares directly with
    :func:`~swcraft_tools.writer.composition_structure.member_type_ref`'s.
    """
    if endpoint.swc is not None:
        return "type", endpoint.swc
    return "composition", str(endpoint.composition)


def _check_composition_names(compositions: list[Composition]) -> None:
    """Reject a composition name site that is not a legal AUTOSAR ``Identifier``.

    The layer emits **four** ``SHORT-NAME`` sites -- the composition, each
    boundary port, each member and each delegation -- none with a generated form.
    Sibling uniqueness is :class:`~swcraft_tools.writer.arxml_xml.ShortNameScope`'s,
    at the point of emission.

    Raises:
        ValueError: naming every offending name.
    """
    candidates: list[tuple[str, str]] = []
    for composition in compositions:
        candidates.append(("Composition name", composition.name))
        candidates.extend(_package_candidate(f"Composition '{composition.name}'", composition.package))
        for port in composition.ports:
            candidates.append((f"Composition '{composition.name}' port name", port.name))
        for member in composition.members:
            candidates.append((f"Composition '{composition.name}' member name", member.name))
        for delegation in composition.delegations:
            candidates.append((f"Composition '{composition.name}' delegation name", delegation.name))
    _check_identifiers(candidates, "compositions/")


def _check_mapping_set_names(mapping_sets: list[MappingSet]) -> None:
    """Reject an illegal or duplicated mapping set ``SHORT-NAME``.

    Every set is emitted into ``/DataTypes/MappingSets``; a set and a data type of one name are not siblings, so
    they do not collide.

    **What this does not reach**: whether every application type a component's ports speak is *paired* at all
    (:func:`_check_application_type_mappings`), and a name in another package.

    Raises:
        ValueError: naming every offending name and every duplicated one.
    """
    _check_identifiers(
        [
            candidate
            for mapping_set in mapping_sets
            for candidate in (
                ("Mapping set name", mapping_set.name),
                *_package_candidate(f"Mapping set '{mapping_set.name}'", mapping_set.package),
            )
        ],
        "data_types/mapping_sets/",
    )
    names = [mapping_set.name for mapping_set in mapping_sets]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ValueError(f"Duplicate mapping set names in data_types/mapping_sets/: {sorted(duplicates)}")


def _check_application_type_mappings(
    swcs: list[SWComponent],
    interfaces: list[Interface],
    data_types: list[DataType],
    mapping_sets: list[MappingSet],
) -> None:
    """Refuse a component whose ports or variables speak an application type or enumeration no named set pairs.

    The fail-closed twin of ``validator.check_application_type_mapping``, so ``generate arxml`` never writes a
    ``[constr_1004]``-violating file ``check-model`` refuses. It runs for a component export too.

    It abstains exactly where the validator's rule does: on a component naming an undeclared set
    (``rule:swc.unknown-mapping-set``), a port naming an undeclared interface, and a type name that resolves to
    nothing or to a kind that is not mapped. What is **not** reached: the abstention itself is written twice and
    pinned by no equality test.

    Raises:
        ValueError: naming every unmapped ``(component, type)`` pair.
    """
    # The must-be-mapped set is the validator's own: a map MAY name a record or
    # an array, but neither must be mapped.

    interface_index: dict[str | None, Interface] = {interface.name: interface for interface in interfaces}
    # The view over this function's own index, last declaration winning as the
    # index always did (`_build_document`'s comment says why that is unobservable).
    type_index = DataDescription.last_declared(data_types)
    set_index = {mapping_set.name: mapping_set for mapping_set in mapping_sets}

    offenders: list[str] = []
    for swc in swcs:
        if any(name not in set_index for name in swc.data_type_mappings):
            continue
        mapped = {entry.application for name in swc.data_type_mappings for entry in set_index[name].maps}
        unmapped: dict[str, None] = {}
        for port in swc.ports:
            interface = interface_index.get(port.interface)
            if interface is None:
                continue
            for type_name in _interface_member_application_types(interface):
                declared = _referenced_type(type_name, type_index, implementation=False)
                if not isinstance(declared, MAP_APPLICATION_KINDS):
                    continue
                if type_name not in mapped:
                    unmapped.setdefault(type_name, None)
        # A variable's application type after the ports', in the validator's order.
        for type_name in variable_application_types(swc):
            declared = _referenced_type(type_name, type_index, implementation=False)
            if isinstance(declared, MAP_APPLICATION_KINDS) and type_name not in mapped:
                unmapped.setdefault(type_name, None)
        offenders.extend(f"'{swc.name}' speaks '{type_name}'" for type_name in unmapped)

    if offenders:
        raise ValueError(
            "Component(s) speak a physical type no mapping set they name pairs with an implementation "
            "type: " + ", ".join(offenders) + " ('swcraft check-model' reports the same model)."
        )


def _check_application_type_double_mappings(swcs: list[SWComponent], mapping_sets: list[MappingSet]) -> None:
    """Refuse a component whose mapping sets pair one application type with two implementation types.

    The fail-closed twin of ``rule:data-type.application-type-double-mapped``: ``[constr_1004]`` allows exactly one
    implementation type per application type in the scope of one atomic component, and the XSD does not catch a
    union of sets that pairs a type two ways.

    Raises:
        ValueError: naming every double-mapped ``(component, type)`` pair.
    """

    set_index = {mapping_set.name: mapping_set for mapping_set in mapping_sets}
    offenders: list[str] = []
    for swc in swcs:
        for application, onto in application_type_double_mappings(swc.data_type_mappings, set_index).items():
            offenders.append(f"'{swc.name}' maps '{application}' onto " + " and ".join(f"'{name}'" for name in onto))
    if offenders:
        raise ValueError(
            "Component(s) whose mapping sets pair one application type with more than one implementation "
            "type, where [constr_1004] allows exactly one: "
            + ", ".join(offenders)
            + " ('swcraft check-model' reports the same model)."
        )


def _interface_member_application_types(interface: Interface) -> Iterator[str]:
    """Yield every application type a port speaking *interface* puts on the bus, in declaration order.

    ``validator._port_member_application_types``' twin: the module keeps its own
    reading of a field, and the two are pinned equal by
    ``packages/tools/tests/test_arxml_application_types_writer.py``. Only a
    prototype's ``application_type`` is a type a mapping set must pair.
    """
    if isinstance(interface, SRInterface):
        for element in interface.data_elements:
            if element.application_type is not None:
                yield element.application_type
    elif isinstance(interface, CSInterface):
        for operation in interface.operations:
            for argument in operation.arguments:
                if argument.application_type is not None:
                    yield argument.application_type


def _check_component_type_names(
    swcs: list[SWComponent],
    compositions: list[Composition],
) -> None:
    """Reject a composition name that another declaration already claims in ``/ComponentTypes``.

    ``APPLICATION-SW-COMPONENT-TYPE``, ``SWC-IMPLEMENTATION`` and ``COMPOSITION-SW-COMPONENT-TYPE`` are siblings in
    one ``AR-PACKAGE``, so the ``swc`` and ``compositions`` layers are **one** ``SHORT-NAME`` namespace. An
    ``external: true`` component emits nothing and still claims its name; its ``{name}_Implementation`` is **not**
    claimed.

    **What this does not reach**: a duplicate *within* one layer (:func:`_check_component_names`,
    ``check_composition_consistency``), and every sibling scope below the package.

    Raises:
        ValueError: naming every offending composition and what claims its name
            (``rule:name.duplicate-component-type``, ``rule:name.composition-implementation-collision`` and
            ``rule:name.composition-timing-collision``).
    """
    declared_components = {swc.name for swc in swcs}
    implementations = {f"{swc.name}{IMPLEMENTATION_SUFFIX}": swc.name for swc in swcs if not swc.external}
    timings = {f"{swc.name}{TIMING_SUFFIX}": swc.name for swc in swcs if emits_swc_timing(swc)}
    collisions: list[str] = []
    for composition in compositions:
        if composition.name in declared_components:
            collisions.append(f"composition '{composition.name}' and the component of that name in swc/")
        elif composition.name in implementations:
            collisions.append(
                f"composition '{composition.name}' and the SWC-IMPLEMENTATION generated for component "
                f"'{implementations[composition.name]}'"
            )
        elif composition.name in timings:
            collisions.append(
                f"composition '{composition.name}' and the SWC-TIMING generated for component "
                f"'{timings[composition.name]}'"
            )
    if collisions:
        raise ValueError(
            "Colliding /ComponentTypes SHORT-NAME(s) declared in compositions/: "
            + "; ".join(collisions)
            + ". A composition, a component and a component's generated SWC-IMPLEMENTATION and "
            "SWC-TIMING are siblings in one AR-PACKAGE and cannot share a name."
        )


def _check_connectors(model_root: str) -> None:
    """Run the connector consistency gate, or raise.

    Called only for a model that **has** connectors.

    Raises:
        ValueError: if any connector is inconsistent, listing every issue.
    """
    result = validator.check_connector_consistency(model_root)
    if not result.consistent:
        raise ValueError("Connector consistency check failed: " + "; ".join(result.issues))


def _check_operation_events(swcs: list[SWComponent], model_root: str) -> None:
    """Refuse every operation event ``check-model`` reports, before anything is emitted.

    The predicate is ``validator.check_operation_events`` itself: the resolution family, a ``(port, operation)``
    pair bound twice, and the partial ``[constr_2000]`` comparison of one runnable's operations. Asked only when a
    component declares an operation event.

    What it does **not** refuse, because ``check-model`` does not report it: mixed event kinds, a provided operation
    no runnable serves, the possible errors, the return value, ``errorHandling``, ``serverArgumentImplPolicy`` and
    ``PortDefinedArgumentValues`` of the operations one runnable serves, and a position the comparison does not
    judge.

    Raises:
        ValueError: listing every finding, in ``check-model``'s order and words.
    """
    if not any(
        isinstance(event, OperationEvent) for swc in swcs for runnable in swc.runnables for event in runnable.events
    ):
        return

    result = check_operation_events(model_root)
    if not result.consistent:
        raise ValueError(
            "Operation event check failed ('swcraft check-model' reports the same model): " + "; ".join(result.issues)
        )


def _refuse_rule_records(records: list[Any]) -> None:
    """Raise one ``ValueError`` naming every record as ``rule:<id>: <the validator's message>``, or do nothing."""
    if records:
        raise ValueError("; ".join(f"rule:{record.rule}: {record.message}" for record in records))


def _check_port_com_specs(
    swcs: list[SWComponent],
    interfaces: list[Interface],
    type_registry: DataDescription,
) -> None:
    """Refuse a ``com_specs`` entry ``check-model`` reports, before any emission.

    The writer's twin of ``rule:swc.port.com-spec-unknown-element``, ``rule:swc.port.com-spec-inapplicable`` and
    ``rule:swc.port.com-spec-on-stub``. A port's own ``init`` is judged by the ``rule:interface.init-*`` rules, then
    asked of :func:`_init_value_refusals`; *type_registry* holds the units, compu methods and data constraints
    beside the types, so an enumeration naming a texttable is judged against the table's entries.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each, or every port value
            :func:`_init_value_refusals` cannot spell.
    """

    rule_records = [*port_com_spec_records(swcs, interfaces), *port_init_records(swcs, interfaces, type_registry)]
    _refuse_rule_records(rule_records)
    declared = {iface.name: iface for iface in interfaces}
    refusals: list[str] = []
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            interface = declared.get(port.interface) if port.interface is not None else None
            if not isinstance(interface, SRInterface):
                continue
            elements = first_elements(interface)
            for entry in port.com_specs:
                element = elements.get(entry.data_element)
                if entry.init is None or element is None or is_queued(element):
                    continue
                element_type, element_application = _prototype_type(element)
                if not _init_type_resolves(element_type, type_registry, application=element_application):
                    continue
                refusals.extend(
                    f"SWC '{swc.name}' port '{port.name}' com_specs init value for data element "
                    f"'{entry.data_element}' {refusal}"
                    for refusal in _init_value_refusals(
                        entry.init, element_type, type_registry, "init", application=element_application
                    )
                )
    if refusals:
        raise ValueError("Port init value(s) this writer cannot emit: " + "; ".join(refusals) + ".")


def _check_inter_runnable_variables(
    swcs: list[SWComponent],
    type_registry: DataDescription,
    mapping_sets: list[MappingSet],
) -> None:
    """Refuse every inter-runnable variable finding ``check-model`` reports, before any emission.

    Reads the validator's own records, so ``generate arxml`` refuses exactly what the gate reports, in its words:

    * ``swc.variable.undeclared-type`` -- a type field naming no declared type;
    * ``swc.variable.pointer-type`` -- ``[constr_1296]``, along the reference chain and through the component's
      mapping sets;
    * ``swc.variable-access.unknown-variable`` -- ``[constr_2026]``, an access naming no variable of its own
      component;
    * the init rules on a variable's ``init``.

    **The warning is not a refusal.** ``swc.variable.read-without-init`` is in ``validator.WARNING_RULES``: a read
    of a variable with no ``init`` generates (``[TPS_SWCT_01522]`` leaves it undefined only if it comes before every
    write, which the model cannot say). Each ``init`` is then asked of :func:`_init_value_refusals`.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each, or every variable value
            :func:`_init_value_refusals` cannot spell.
    """

    records = [
        record
        for record in inter_runnable_variable_records(swcs, type_registry, mapping_sets)
        if record.severity == SEVERITY_ERROR
    ]
    _refuse_rule_records([*records, *variable_init_records(swcs, type_registry)])
    refusals: list[str] = []
    for swc in swcs:
        if swc.external:
            continue
        for variable in swc.variables:
            if variable.init is None:
                continue
            variable_type, application = _prototype_type(variable)
            if variable_type is None or not _init_type_resolves(variable_type, type_registry, application=application):
                continue
            refusals.extend(
                f"SWC '{swc.name}' variable '{variable.name}' init value {refusal}"
                for refusal in _init_value_refusals(
                    variable.init, variable_type, type_registry, "init", application=application
                )
            )
    if refusals:
        raise ValueError("Variable init value(s) this writer cannot emit: " + "; ".join(refusals) + ".")


def _check_data_events(swcs: list[SWComponent], interfaces: list[Interface]) -> None:
    """Refuse every data-received event finding ``check-model`` reports, before any emission.

    The twin of the ``rule:swc.data-event.*`` rules: an undeclared port (``[constr_2530]``), a provide port, a
    non-sender/receiver interface, an element the interface or stub does not declare.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(data_event_records(swcs, interfaces))


def _check_exclusive_areas(swcs: list[SWComponent]) -> None:
    """Refuse every exclusive area finding ``check-model`` reports, before any emission.

    The writer's twin of ``rule:swc.exclusive-area.unknown-area``: a ``can_enter`` or ``runs_inside`` entry naming
    no exclusive area of its own component.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(exclusive_area_records(swcs))


def _check_runnable_symbols(swcs: list[SWComponent]) -> None:
    """Refuse a runnable ``symbol`` ``check-model`` reports, before any emission.

    The writer's twin of ``rule:swc.runnable.symbol-not-c-identifier`` and ``rule:name.duplicate-runnable-symbol``.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(runnable_symbol_records(swcs))


def _check_queued_receivers(swcs: list[SWComponent], interfaces: list[Interface]) -> None:
    """Refuse a queued element received with no ``queue_length``, before any emission.

    The writer's twin of ``rule:swc.port.queued-receiver-without-queue-length``.
    ``QueuedReceiverComSpec.queueLength`` is multiplicity 1 with no default (Table 4.62 of SWCT R4.2.2, 4.64 of
    R4.3.0), so the writer can neither invent a length nor, on a component port, drop the element's com-spec
    (``[TPS_SWCT_01219]``).

    Raises:
        ValueError: listing every ``(port, element)`` finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(queued_receiver_records(swcs, interfaces))


def _check_disabled_modes(swcs: list[SWComponent], interfaces: list[Interface]) -> None:
    """Refuse a disabled mode it cannot write, before any emission.

    The writer's twin of ``rule:swc.timing_event.disabled-mode-port``: only a require port of the component typed by
    a mode-switch interface stub is writable.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(disabled_mode_records(swcs, interfaces))


def _check_queued_reads(swcs: list[SWComponent], interfaces: list[Interface]) -> None:
    """Refuse a ``by: value`` read of a queued element, before any emission.

    The writer's twin of ``rule:swc.access.queued-read-by-value`` (``[constr_1277]``, SWCT R4.2.2 ``:25448``, R4.3.0
    ``:28306``).

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(queued_read_records(swcs, interfaces))


def _check_service_composition_ports(compositions: list[Composition], interfaces: list[Interface]) -> None:
    """Refuse a composition port typed by a service interface, before any emission.

    The writer's twin of ``rule:composition.port-service-interface`` (``[constr_1174]``, SWCT R4.2.2 ``:4016``,
    R4.3.0 ``:4369``): a composition port on a service interface is the port ``[constr_1174]`` forbids.

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(service_composition_port_records(compositions, interfaces))


def _check_conversion_units(description: DataDescription) -> None:
    """Refuse an application type with a ``conversion`` and no ``unit``, before any emission.

    The writer's twin of ``rule:data-type.conversion-without-unit``. ``[constr_1175]`` holds at R4.2.2 and R4.3.0
    alike; every declared ``conversion`` is a ``LINEAR`` compu method, which must refer to a unit.

    Raises:
        ValueError: listing every offending type, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records(conversion_unit_records(description))


def _emit_component_prototypes(
    node: etree._Element,
    composition: Composition,
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
) -> etree._Element | None:
    """Emit one ``SW-COMPONENT-PROTOTYPE`` per declared member, in declaration order.

    **A type may be placed at most once here**: a connector endpoint names a type, and with two members of it the
    connector would be silently mis-wired. Placing one type in *several* compositions stays legal.

    Returns:
        The ``COMPONENTS`` element, or None when the composition has no members.

    Raises:
        ValueError: if a member's type is declared in neither layer, if one type is placed twice, or if two members
            share a ``SHORT-NAME`` (``rule:composition.member-unknown-type`` /
            ``rule:composition.member-unknown-composition``, ``rule:composition.member-type-repeated`` and
            ``rule:composition.duplicate-member``).
    """
    if not composition.members:
        return None
    placed: dict[tuple[str, str], list[str]] = {}
    for member in composition.members:
        placed.setdefault(member_type_ref(member), []).append(member.name)
    repeated = sorted((name, members) for (_field, name), members in placed.items() if len(members) > 1)
    if repeated:
        raise ValueError(
            f"Composition '{composition.name}' in compositions/ places "
            + "; ".join(f"type '{type_name}' as members {', '.join(names)}" for type_name, names in repeated)
            + "; a type may be placed at most once per composition, because a connector endpoint "
            "names the type and is resolved to its member."
        )
    components = sub_element(node, "COMPONENTS")
    scope = ShortNameScope(components, f"COMPONENTS of composition {composition.name!r}")
    for member in composition.members:
        field, type_name = member_type_ref(member)
        path, dest = _component_type_path(
            field,
            type_name,
            swc_map,
            composition_index,
            owner=composition.name,
        )
        prototype = scope.create("SW-COMPONENT-PROTOTYPE", member.name)
        set_description(prototype, member.description)
        reference_element(prototype, "TYPE-TREF", path, dest=dest)
    return components


def _emit_assembly_connector(
    scope: ShortNameScope,
    connector: Connector,
    owner: Composition,
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
) -> etree._Element:
    """Emit one ``ASSEMBLY-SW-CONNECTOR``.

    ``PROVIDER-IREF``/``REQUESTER-IREF`` carry **no** ``DEST``: they are plain
    wrappers, and only their children are references. The two children point
    into **different** base paths: ``CONTEXT-COMPONENT-REF`` at the member of the
    endpoint's type *inside owner*, ``TARGET-*-PORT-REF`` at the port of the
    *component type*; they differ whenever a member's name differs from its type's.

    ``from_`` is always the provide side and ``to`` the require side, enforced
    by :func:`_check_connectors` before this is reached.
    """
    node = scope.create("ASSEMBLY-SW-CONNECTOR", connector.name)
    set_description(node, connector.description)
    for wrapper_tag, endpoint, target_tag, target_dest in (
        ("PROVIDER-IREF", connector.from_, "TARGET-P-PORT-REF", "P-PORT-PROTOTYPE"),
        ("REQUESTER-IREF", connector.to, "TARGET-R-PORT-REF", "R-PORT-PROTOTYPE"),
    ):
        member = _member_of_type(owner, endpoint, connector.name)
        member_field, member_type = member_type_ref(member)
        type_path, _dest = _component_type_path(
            member_field,
            member_type,
            swc_map,
            composition_index,
            owner=owner.name,
        )
        wrapper = sub_element(node, wrapper_tag)
        reference_element(
            wrapper,
            "CONTEXT-COMPONENT-REF",
            f"{_placed_path(_HOME.COMPONENT_TYPES, owner.name)}/{member.name}",
            dest="SW-COMPONENT-PROTOTYPE",
        )
        reference_element(wrapper, target_tag, f"{type_path}/{endpoint.port}", dest=target_dest)
    return node


def _emit_delegation(
    scope: ShortNameScope,
    composition: Composition,
    delegation: Delegation,
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
    interface_registry: dict[str, Interface],
    type_registry: DataDescription,
) -> etree._Element:
    """Emit one ``DELEGATION-SW-CONNECTOR``.

    The shape is the two AUTOSAR XSDs', identical in every composition group: ``SHORT-NAME``; ``INNER-PORT-IREF``
    holding one ``P-``/``R-PORT-IN-COMPOSITION-INSTANCE-REF`` with ``CONTEXT-COMPONENT-REF`` (the **prototype**)
    then ``TARGET-{P,R}-PORT-REF`` (the **type's** port); ``OUTER-PORT-REF`` at the composition's own boundary port.
    The variant follows from the two ports' declared direction, which must agree.

    Raises:
        ValueError: if the delegation names no member of its composition, if either port is undeclared, if the two
            directions disagree, or if the interface the required side of the delegation asks for is not satisfied
            by the one the provided side offers (``rule:composition.delegation-unknown-member``,
            ``rule:composition.delegation-unknown-port``, ``rule:composition.delegation-port-direction-mismatch``
            and ``rule:composition.delegation-interface-mismatch``).
    """

    members = {member.name: member for member in composition.members}
    member = members.get(delegation.member)
    if member is None:
        raise ValueError(
            f"Delegation '{delegation.name}' of composition '{composition.name}' names member "
            f"'{delegation.member}', which that composition does not declare "
            f"(compositions/)."
        )

    outer_ports = {port.name: port for port in composition.ports}
    outer = outer_ports.get(delegation.port)
    if outer is None:
        raise ValueError(
            f"Delegation '{delegation.name}' of composition '{composition.name}' names port "
            f"'{delegation.port}', which that composition does not declare "
            f"(compositions/)."
        )

    member_field, type_name = member_type_ref(member)
    inner = _declared_ports(member_field, type_name, swc_map, composition_index).get(delegation.member_port)
    if inner is None:
        raise ValueError(
            f"Delegation '{delegation.name}' of composition '{composition.name}' names port "
            f"'{delegation.member_port}' on member '{member.name}' of type '{type_name}', "
            f"which that type does not declare (compositions/)."
        )

    if outer.direction != inner.direction:
        raise ValueError(
            f"Delegation '{delegation.name}' of composition '{composition.name}' joins "
            f"'{delegation.port}' ({outer.direction}) to '{member.name}.{delegation.member_port}' "
            f"({inner.direction}); a delegation's two ports must declare the same direction."
        )

    # The interface, after the direction, as the validator reports it: AUTOSAR
    # requires the inner and the outer port of a DELEGATION-SW-CONNECTOR to
    # reference compatible port interfaces, and the XSD cannot express it (a
    # CLIENT-SERVER boundary port delegated onto a SENDER-RECEIVER member port
    # validates at 4.2.2 and 4.3.0), so this refusal and
    # `check_composition_consistency`'s report are the whole gate.
    #
    # The INNER port's interface is the required side of both clauses --
    # `[constr_1070]`/`[constr_1080]` 1(a) for a `require` delegation, the
    # subset; clause 2(a) for a `provide` one, where ONE shared compatible member
    # suffices -- and the outer port's is the provided side. The clause is chosen
    # by the OUTER port's direction; the guard above has established that the two
    # ports agree. The same name on both sides needs no resolution.
    if outer.interface != inner.interface:
        incompatibility = interface_compatibility(
            inner.interface,
            outer.interface,
            # Asked of the validator so the reporting and the refusing half
            # choose the same clause for one pair of ports.
            context=delegation_context(outer.direction),
            interfaces=interface_registry,
            data_types=type_registry,
        )
        if incompatibility is not None:
            raise ValueError(
                f"Delegation '{delegation.name}' of composition '{composition.name}' joins "
                f"'{delegation.port}' ({outer.interface}) to '{member.name}.{delegation.member_port}' "
                f"({inner.interface}): {incompatibility.subject}."
            )

    provided = outer.direction == "provide"
    letter = "P" if provided else "R"
    port_dest = f"{letter}-PORT-PROTOTYPE"

    node = scope.create("DELEGATION-SW-CONNECTOR", delegation.name)
    set_description(node, delegation.description)
    inner_iref = sub_element(node, "INNER-PORT-IREF")
    instance = sub_element(inner_iref, f"{letter}-PORT-IN-COMPOSITION-INSTANCE-REF")
    reference_element(
        instance,
        "CONTEXT-COMPONENT-REF",
        f"{_placed_path(_HOME.COMPONENT_TYPES, composition.name)}/{member.name}",
        dest="SW-COMPONENT-PROTOTYPE",
    )
    type_path, _dest = _component_type_path(
        member_field,
        type_name,
        swc_map,
        composition_index,
        owner=composition.name,
    )
    reference_element(instance, f"TARGET-{letter}-PORT-REF", f"{type_path}/{delegation.member_port}", dest=port_dest)
    reference_element(
        node,
        "OUTER-PORT-REF",
        f"{_placed_path(_HOME.COMPONENT_TYPES, composition.name)}/{delegation.port}",
        dest=port_dest,
    )
    return node


def _emit_composition(
    package: ArxmlPackage,
    composition: Composition,
    connectors: list[Connector],
    swc_map: dict[str, SWComponent],
    composition_index: dict[str, Composition],
    interface_registry: dict[str, Interface],
    type_registry: DataDescription,
) -> etree._Element:
    """Emit one declared composition as a package element.

    Its children, in the schema's order: ``SHORT-NAME``, ``LONG-NAME`` (only for a ``title`` other than the
    ``name``), ``DESC``, ``PORTS`` (emitted even when empty), ``COMPONENTS`` and ``CONNECTORS`` (omitted when
    empty). Boundary ports are in declaration order; ``CONNECTORS`` holds the assembly connectors in read order,
    then the delegations in declaration order.

    Args:
        connectors: the assembly connectors drawn inside it, in read order.

    Raises:
        ValueError: if a boundary port names an undeclared interface, if a member or endpoint type is undeclared, or
            if a delegation does not resolve.
    """
    interfaces = [_composition_port_interface(composition, port, interface_registry) for port in composition.ports]

    node = package.create_element(COMPOSITION_SWC_TAG, composition.name)
    if composition.title != composition.name:
        set_long_name(node, composition.title)
    set_description(node, composition.description)
    ports_node = sub_element(node, "PORTS")
    port_scope = ShortNameScope(ports_node, f"PORTS of composition {composition.name!r}")
    for port, iface in zip(composition.ports, interfaces):
        _emit_port(port_scope, port, iface)

    _emit_component_prototypes(node, composition, swc_map, composition_index)

    if connectors or composition.delegations:
        connectors_node = sub_element(node, "CONNECTORS")
        scope = ShortNameScope(connectors_node, f"CONNECTORS of composition {composition.name!r}")
        for connector in connectors:
            _emit_assembly_connector(scope, connector, composition, swc_map, composition_index)
        for delegation in composition.delegations:
            _emit_delegation(
                scope,
                composition,
                delegation,
                swc_map,
                composition_index,
                interface_registry,
                type_registry,
            )
    return node


def _composition_port_interface(
    composition: Composition,
    port: CompositionPort,
    interface_registry: dict[str, Interface],
) -> Interface:
    """Resolve a boundary port's declared interface, or raise.

    Raises:
        ValueError: if the interface is not declared in
            ``model_root/interfaces/``.
    """
    iface = interface_registry.get(port.interface)
    if iface is None:
        raise ValueError(
            f"Interface '{port.interface}' referenced by composition '{composition.name}' port "
            f"'{port.name}' is not declared in interfaces/"
        )
    return iface


def _emit_compositions(
    package: ArxmlPackage,
    compositions: list[Composition],
    swcs: list[SWComponent],
    connectors: list[Connector],
    interface_registry: dict[str, Interface],
    model_root: str,
    type_registry: DataDescription,
) -> list[etree._Element]:
    """Emit every declared composition, in read order, or nothing at all.

    A composition is emitted whether or not any connector exists. Every connector is attributed to a composition
    (``connector.composition``, or the root when it is ``None``) before the first element is created.

    Args:
        type_registry: the types a delegation's compatibility judgement resolves against. **Required**: with an
            empty default the delegation gate would silently lose its type half.

    Returns:
        Every emitted ``COMPOSITION-SW-COMPONENT-TYPE``, in read order.

    Raises:
        ValueError: if the connector consistency check fails, if the hierarchy contains a cycle, if no composition
            declares ``root: true`` while one is needed, if a connector names a composition that is not declared, or
            for any resolution failure the emitters below document; nothing is written in any case.
    """
    if not compositions:
        if connectors:
            raise ValueError(
                "connectors/ declares connectors but compositions/ declares no composition to draw them inside."
            )
        return []

    if connectors:
        _check_connectors(model_root)

    composition_index = compositions_by_name(compositions)
    swc_map = {swc.name: swc for swc in swcs}

    # A cyclic hierarchy is emitted without recursing anywhere (one composition
    # per read-order slot and a closed loop of TYPE-TREFs), so nothing else would
    # notice it.
    check_composition_hierarchy(composition_index)

    # The root is only needed when a connector leaves `composition` unset;
    # `check-model`'s `rule:composition.no-root` holds it unconditionally.
    root: Composition | None = None
    by_composition: dict[str, list[Connector]] = {composition.name: [] for composition in compositions}
    for connector in connectors:
        if connector.composition is None:
            if root is None:
                root = root_composition(compositions)
            owner_name = root.name
        else:
            owner_name = connector.composition
            if owner_name not in composition_index:
                raise ValueError(
                    f"Connector '{connector.name}' names composition '{owner_name}', which is not "
                    f"declared in compositions/."
                )
        by_composition[owner_name].append(connector)

    return [
        _emit_composition(
            package,
            composition,
            by_composition[composition.name],
            swc_map,
            composition_index,
            interface_registry,
            type_registry,
        )
        for composition in compositions
    ]


#: Every data-type class :func:`_build_document` accounts for: the classes its
#: passes select, and the external type stub, which no pass emits by design (a
#: reference naming one carries the stub's own pair). Any other class is refused
#: (``TypeError``, fail-closed), and ``packages/tools/tests/test_impl_composites_arxml.py``
#: pins this set equal to the ``DataType`` union's members, so a new kind cannot
#: be left out of the document silently.
_DATA_TYPE_PASS_KINDS: frozenset[type] = frozenset(
    {
        BaseDataType,
        ValueDataType,
        *_IMPLEMENTATION_PASS_EMITTERS,
        ApplicationDataType,
        EnumDataType,
        RecordDataType,
        ArrayDataType,
        ExternalDataType,
    }
)


def _check_model_and_build_type_registry(
    model_root: str,
    version: ArxmlVersion,
    swcs: list[SWComponent],
    interfaces: list[Interface],
    connectors: list[Connector],
    description: DataDescription,
    compositions: list[Composition],
    mapping_sets: list[MappingSet],
) -> DataDescription:
    """Run every up-front guard over the model, in ``check-model``'s order, and return the type registry.

    The guards run before the first element is created, so a model one of them
    refuses never reaches an emitter. The returned registry is the data
    description in which a name declared twice resolves to its last declaration;
    it is built once, before the first guard that resolves a type through it, and
    the emitters resolve through it too.

    Returns:
        The data description with last-declaration-wins resolution.

    Raises:
        ValueError: on the first model one of the guards refuses.
    """
    data_types = list(description.data_types)

    # The units, compu methods and data constraints first: a reference naming no
    # element, or a compu method of the wrong category, would otherwise meet a
    # guard that resolves it.
    _check_data_description_elements(description)

    # Duplicate names, collisions with generated names and illegal identifiers
    # are rejected before the registries are built: a `{x.name: x}` dict
    # silently drops the duplicate. These guards are unconditional; an empty
    # layer makes each a no-op.
    _check_data_type_names(data_types)
    _check_data_description_element_names(description)
    _check_base_type_native_declarations(data_types)
    _check_interface_names(interfaces)
    _check_component_names(swcs)
    _check_connector_names(connectors)
    _check_composition_names(compositions)
    _check_component_type_names(swcs, compositions)
    _check_mapping_set_names(mapping_sets)
    _check_application_type_mappings(swcs, interfaces, data_types, mapping_sets)
    _check_application_type_double_mappings(swcs, mapping_sets)
    check_external_stubs(swcs, interfaces, data_types)
    # After the stub shapes (a stub declares no runnable) and the name guards (a
    # duplicated port or runnable name is refused first).
    _check_operation_events(swcs, model_root)
    # Last declaration wins here, where `read_data_description`'s view and
    # `validator.interface_index` are first-wins. That is unobservable only
    # because the name guards above refused a name declared twice; removing one
    # would let the writer and the validator judge different elements. The view
    # holds the units, compu methods and data constraints too, so a validator
    # predicate asked with it resolves a reference field as `check-model` does.
    type_registry = DataDescription.last_declared([*data_types, *description.elements])
    # The remaining twins run in `check_cross_layer_consistency`'s order, so of
    # two offenders the one `check-model` lists first is refused.
    _check_port_com_specs(swcs, interfaces, type_registry)
    _check_runnable_symbols(swcs)
    _check_queued_receivers(swcs, interfaces)
    _check_disabled_modes(swcs, interfaces)
    _check_queued_reads(swcs, interfaces)
    _check_service_composition_ports(compositions, interfaces)
    _check_conversion_units(description)
    _check_inter_runnable_variables(swcs, type_registry, mapping_sets)
    _check_exclusive_areas(swcs)
    _check_data_events(swcs, interfaces)
    # The asynchronous call's result count is judged at the version this run generates.
    _check_access_forms(swcs, interfaces, version)
    return type_registry


def _emit_data_type_layers(
    document: ArxmlDocument,
    description: DataDescription,
    data_types: list[DataType],
    mapping_sets: list[MappingSet],
    type_registry: DataDescription,
) -> None:
    """Emit every data type layer into *document*, each into its own package, in dependency order.

    The order: base types, units, compu methods and data constraints, value
    types, the implementation pass, application types (each preceded by its
    derived ``UNIT``/``COMPU-METHOD``/``DATA-CONSTR``), composites topologically
    sorted, and the declared mapping sets last.
    """
    # Each pass emits into its layer's package under /DataTypes, which the
    # document opens with its first element, so the packages appear in the
    # passes' order and a layer with no element emits none.
    #
    # Pass 1 — base types: one SW-BASE-TYPE each, and nothing else.
    for dt in data_types:
        if isinstance(dt, BaseDataType):
            _emit_sw_base_type(document.home(_HOME.BASE_TYPES), dt)

    # Pass 1a — units, compu methods and data constraints, each layer in its
    # reader's order, before every type that names one.
    _emit_data_description_elements(document, description)

    # Pass 2 — value types: an IMPLEMENTATION-DATA-TYPE of category VALUE over the base type.
    for dt in data_types:
        if isinstance(dt, ValueDataType):
            _emit_value_type(document.home(_HOME.IMPLEMENTATION_TYPES), dt, type_registry)

    # Pass 3 — the implementation pass: every reference type (TYPE_REFERENCE),
    # implementation record (STRUCTURE), implementation array (ARRAY) and pointer
    # (DATA_REFERENCE), in one dependency order, so every reference points back.
    _emit_implementation_types(document.home(_HOME.IMPLEMENTATION_TYPES), data_types, type_registry)

    # Pass 4 — the declared application types, each preceded by the UNIT, the
    # COMPU-METHOD and the DATA-CONSTR its own fields derive.
    for dt in data_types:
        if isinstance(dt, ApplicationDataType):
            _emit_application_type(document.home(_HOME.APPLICATION_TYPES), dt)

    # Pass 5 — composites (enum/record/array), in dependency order.
    composites = [dt for dt in data_types if isinstance(dt, (EnumDataType, RecordDataType, ArrayDataType))]
    _emit_composite_types(document.home(_HOME.APPLICATION_TYPES), composites, type_registry)

    # Pass 6 — the declared mapping sets, last in the package, after every type
    # a map may name.
    _emit_mapping_sets(document.home(_HOME.MAPPING_SETS), mapping_sets, type_registry)


def _build_document(model_root: str, version: ArxmlVersion | str = DEFAULT_ARXML_VERSION) -> ArxmlDocument:
    """Read every generated layer under *model_root* and emit the whole document.

    Nothing is written before this returns, so a failed build leaves no outputs. The order is DataTypes ->
    PortInterfaces -> ComponentTypes; a reference points forward in two places only: a ``SW-COMPONENT-PROTOTYPE``
    naming a composition emitted after it, and a reference from a root's own element into one of its sub-packages.

    Raises:
        ValueError: on a YAML file failing schema validation (naming its path); an unsupported *version*; an illegal
            or colliding ``SHORT-NAME``, declared or generated; an undeclared or wrongly kinded reference; a
            dependency cycle; a non-``application`` SWC; any model an up-front guard refuses (each the writer's half
            of a ``check-model`` rule); a composition or connector that does not resolve; a free text XML 1.0 cannot
            carry (``rule:text.xml-unrepresentable-character``); an element created outside its placement (a writer
            defect); two names of one AUTOSAR name space equal ignoring case (``rule:name.name-space-collision``,
            after emission, or during it for a package-level pair); and last a non-stub component without
            ``handle_termination_and_restart`` (``rule:swc.undeclared-termination-and-restart``).
        TypeError: if a declared data type kind is selected by no emission pass.
    """
    # Parsed before the first layer is read: a bad version is the caller's typo,
    # not the model's, and reading the whole model to reject it would report it
    # behind whatever model error happened to come first.
    version = ArxmlVersion.from_string(version)

    swcs = read_swc_model(model_root)
    interfaces = read_interfaces(model_root)
    connectors = read_connectors(model_root)
    description = read_data_description(model_root)
    data_types = list(description.data_types)
    compositions = read_compositions(model_root)
    mapping_sets = read_mapping_sets(model_root)

    elements = description.elements
    type_registry = _check_model_and_build_type_registry(
        model_root, version, swcs, interfaces, connectors, description, compositions, mapping_sets
    )
    interface_registry: dict[str, Interface] = {iface.name: iface for iface in interfaces}

    # Fail-closed: every declared kind is one some pass selects, or the external
    # type stub, which a reference carries as its own pair and no pass emits.
    unemitted = sorted({dt.kind for dt in data_types if type(dt) not in _DATA_TYPE_PASS_KINDS})
    if unemitted:
        raise TypeError(f"no data-type pass emits the kind(s) {unemitted}")
    # The reset in `finally` keeps the placement from leaking into a later caller
    # of an emitter when a pass refuses the model.
    placement = _package_placement(swcs, interfaces, data_types, compositions, mapping_sets, elements)
    document = ArxmlDocument(version, placement=placement)
    placement_token = _PLACEMENT.set(placement)
    try:
        _emit_data_type_layers(document, description, data_types, mapping_sets, type_registry)
        _emit_interfaces(document.port_interfaces, interfaces, type_registry)
        _emit_swcs(
            document.component_types,
            swcs,
            interface_registry,
            frozenset(ms.name for ms in mapping_sets),
            type_registry,
        )
        # The compositions come after every component type they place.
        _emit_compositions(
            document.component_types, compositions, swcs, connectors, interface_registry, model_root, type_registry
        )
    finally:
        _PLACEMENT.reset(placement_token)
    # The post-emission guards run before anything is written, in this order, so
    # a model that failed an earlier guard, an emitter or `_check_connectors`
    # keeps its first error. A package-level case-only pair never gets here:
    # `ArxmlPackage`'s case-folding claim refused it during emission.
    _check_package_containment(document)
    _check_name_space_collisions(swcs, interfaces, data_types, compositions, connectors, mapping_sets, elements)
    # The emitter writes nothing for `None`, so this is the one refusal of a
    # missing termination handling.
    _check_termination_and_restart(swcs)

    return document


def build_workspace(model_root: str) -> ArxmlDocument:
    """Read all SWCs, interfaces, connectors and data types from *model_root*.

    Returns a populated :class:`ArxmlDocument` at the default AUTOSAR version (another version enters at
    :func:`generate_arxml`); raises every ``ValueError`` :func:`_build_document` documents.
    """
    return _build_document(model_root)


def generate_arxml(
    model_root: str,
    output_path: str,
    *,
    version: ArxmlVersion | str = DEFAULT_ARXML_VERSION,
) -> str:
    """Generate ARXML from the YAML model at model_root and write it to output_path.

    Creates parent directories of output_path if they do not exist. Nothing is written or moved when the model is
    rejected: the whole document is built and validated in memory first. Then a file at output_path whose bytes
    differ is moved to ``<stem>_old<suffix>`` beside it, one kept, an identical one never
    (:meth:`ArxmlDocument.save` has the whole rule).

    Returns:
        The output path as a string, spelled for the platform: relative stays relative, separators come back native.

    Raises:
        ValueError: for an unsupported *version* (refused before the first layer is read), and for every model error
            :func:`_build_document` documents.
    """
    return _build_document(model_root, version).save(output_path, keep_previous=True)


def generate_arxml_cli() -> None:
    """Run the CLI entry point: swcraft-generate-arxml <model_root> <output_path> (keeps a ``_old`` file, silently)."""
    if len(sys.argv) < 3:
        print("Usage: swcraft-generate-arxml <model_root> <output_path>", file=sys.stderr)
        sys.exit(1)
    model_root = sys.argv[1]
    output_path = sys.argv[2]
    generate_arxml(model_root, output_path)


# ---------------------------------------------------------------------------
# The component export's capability markers
# ---------------------------------------------------------------------------


def generate_component_arxml(
    model_root: str,
    output_path: str,
    *,
    version: ArxmlVersion | str = DEFAULT_ARXML_VERSION,
) -> str:
    """Run :func:`generate_arxml` for a component export: the same document plus the capability markers.

    Every ``SWC-INTERNAL-BEHAVIOR`` gets each of ``capability_markers.COMPONENT_EXPORT_MARKERS``, in order
    (:func:`_emit_marker`). A marker is a statement about the **exporter**, never about the component; the
    whole-model ``generate arxml`` never emits them.

    Raises:
        ValueError: exactly what :func:`generate_arxml` raises.
    """
    document = _build_document(model_root, version)
    behaviors = [node for node in document.root.iter() if etree.QName(node).localname == "SWC-INTERNAL-BEHAVIOR"]
    for behavior in behaviors:
        for marker in COMPONENT_EXPORT_MARKERS:
            _emit_marker(behavior, marker)
    return document.save(output_path)


# ---------------------------------------------------------------------------
# Package placement
# ---------------------------------------------------------------------------

#: The home packages, as a reference names the one it points into: a data description
#: layer's package under ``/DataTypes`` or one of the two other roots. Its members are
#: not strings: a path is joined by :func:`_placed_path` alone.
_HOME = PackagePlacement.Home


def _home_of(element: DataType | MappingSet | DataDescriptionElement) -> PackagePlacement.Home:
    """Return the home of a data type, a mapping set or a data description element: its layer's package.

    Asked of :func:`~swcraft_tools.validator.data_description_package_path`, so
    the packages ``check-model`` judges and the writer emits into cannot differ.
    """

    return PackagePlacement.Home(data_description_package_path(element))


#: The package placement in force while :func:`_build_document` runs its passes.
#: A :class:`~contextvars.ContextVar` rather than a parameter, because the references
#: are built deep inside emitters that take no document, and rather than a module
#: global, so two builds in two contexts cannot see each other's table. The default,
#: the empty table, puts every element in its root (an emitter called directly).
_PLACEMENT: ContextVar[PackagePlacement] = ContextVar("swcraft_package_placement", default=PackagePlacement())


def _placed_path(home: PackagePlacement.Home, name: str) -> str:
    """Return the absolute path of the root-level element *name* of *home*, where the placement puts it.

    ``/{home}/{name}``, or ``/{home}/{package}/{name}`` for an element the model places in a sub-package; a
    generated form takes its owner's home and package. The one function that joins a root package and a name.
    """
    if not isinstance(home, PackagePlacement.Home):
        raise TypeError(f"_placed_path takes a PackagePlacement.Home, a package of {ROOT_PACKAGE_NAMES}, got {home!r}")
    package = _PLACEMENT.get().package_of(home.value, name)
    return f"/{home.value}/{name}" if package is None else f"/{home.value}/{package}/{name}"


def _package_placement(
    swcs: list[SWComponent],
    interfaces: list[Interface],
    data_types: list[DataType],
    compositions: list[Composition],
    mapping_sets: list[MappingSet],
    elements: Iterable[DataDescriptionElement],
) -> PackagePlacement:
    """Read the model's package placement into one table, before the first element is created.

    Every declared non-external root-level element enters under its home with its own ``package``, and every
    generated form it can produce with its owner's home and package, whether or not the emitter will create it; the
    exception is ``{swc}_Timing``, entered only where emitted. External stubs enter nothing. A declared element wins
    over a generated form of its name. Nothing is moved to avoid a collision.

    Raises:
        ValueError: when two declared elements, or two generated forms, of one home name one element in two places
            (a defect of the table: the up-front guards refuse such a name first).
    """
    entries: dict[tuple[str, str], str | None] = {}
    declared: set[tuple[str, str]] = set()

    def enter(home: PackagePlacement.Home, name: str, package: str | None, *, is_declared: bool = False) -> None:
        key = (home.value, name)
        if key in declared and not is_declared:
            return
        if key in entries and entries[key] != package and (is_declared == (key in declared)):
            raise ValueError(
                f"{name!r} is placed twice in {home.value}, in {entries[key]!r} and in {package!r}: "
                "the writer cannot emit one element in two packages"
            )
        entries[key] = package
        if is_declared:
            declared.add(key)

    for dt in data_types:
        if isinstance(dt, ExternalDataType):
            continue
        home = _home_of(dt)
        enter(home, dt.name, dt.package, is_declared=True)
        if generates_kind_form(dt, (EnumDataType,)):  # from `literals` alone
            enter(home, f"{dt.name}{COMPU_METHOD_SUFFIX}", dt.package)
        for suffix, _what, kind, _fields in RESERVED_DATA_TYPE_FORM_RULES:
            if isinstance(dt, kind):
                enter(home, f"{dt.name}{suffix}", dt.package)
    for element in elements:
        enter(_home_of(element), element.name, element.package, is_declared=True)
    for mapping_set in mapping_sets:
        enter(_home_of(mapping_set), mapping_set.name, mapping_set.package, is_declared=True)
    for iface in interfaces:
        if isinstance(iface, ExternalInterface):
            continue
        enter(_HOME.PORT_INTERFACES, iface.name, iface.package, is_declared=True)
        if isinstance(iface, SRInterface):
            for data_element in iface.data_elements:
                enter(_HOME.PORT_INTERFACES, init_constant_name(iface.name, data_element.name), iface.package)
    for swc in swcs:
        if swc.external:
            continue
        enter(_HOME.COMPONENT_TYPES, swc.name, swc.package, is_declared=True)
        enter(_HOME.COMPONENT_TYPES, f"{swc.name}{IMPLEMENTATION_SUFFIX}", swc.package)
        if emits_swc_timing(swc):  # scoped to emission
            enter(_HOME.COMPONENT_TYPES, f"{swc.name}{TIMING_SUFFIX}", swc.package)
    for composition in compositions:
        enter(_HOME.COMPONENT_TYPES, composition.name, composition.package, is_declared=True)
    return PackagePlacement(entries=entries, declared=frozenset(declared))


def _check_package_containment(document: ArxmlDocument, placement: PackagePlacement | None = None) -> None:
    """Prove, after emission, that the document holds what its placement table says, or raise.

    Containment, deliberately not equality (equality would restate every emitter's own condition):

    1. every element a root or an open layer package created is one of the table's entries under that package's key,
       in the package the entry names, and every sub-package it created is in the table's sub-package set for that
       key;
    2. every declared non-external root-level element was created.

    A failure is a writer defect, never a model one.

    Args:
        placement: the table to check against; the document's own when ``None``.

    Raises:
        ValueError: naming every element and sub-package outside the table, and every declared element that was not
            created.
    """
    placement = document.placement if placement is None else placement
    problems: list[str] = []
    created: set[tuple[str, str]] = set()
    for package in document.placed_packages():
        key = package.key
        stray = sorted(set(package.sub_package_claims) - placement.sub_packages[key])
        problems.extend(f"sub-package {key}/{name} is not in the placement table" for name in stray)
        for placed, name in package.element_claims:
            created.add((key, name))
            if not placement.knows(key, name):
                problems.append(f"{key} element {name!r} is not in the placement table")
            elif placement.package_of(key, name) != placed:
                problems.append(
                    f"{key} element {name!r} was created in {placed!r}, "
                    f"the table places it in {placement.package_of(key, name)!r}"
                )
    problems.extend(
        f"declared {root} element {name!r} was never created" for root, name in sorted(placement.declared - created)
    )
    if problems:
        raise ValueError("Package placement containment failed: " + "; ".join(problems))


def _check_name_space_collisions(
    swcs: list[SWComponent],
    interfaces: list[Interface],
    data_types: list[DataType],
    compositions: list[Composition],
    connectors: list[Connector],
    mapping_sets: list[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> None:
    """Refuse a model two of whose names share one AUTOSAR name space ignoring case.

    The writer's twin of ``rule:name.name-space-collision``. ``[constr_2508]``: a ``SHORT-NAME`` is unique ignoring
    case within the name space of its nearest ``Identifiable`` (``[TPS_GST_00018]``), and the nested per-container
    ``ShortNameScope`` objects compare exactly, one container at a time, so none of them sees a case-only pair or a
    name repeated across two containers of one owner.

    It runs after emission and before anything is written; a package-level case-only pair is refused earlier, during
    emission, by ``ArxmlPackage``.

    Raises:
        ValueError: naming every group, each in the predicate's one sentence.
    """

    collisions = name_space_collisions(
        swcs,
        interfaces,
        data_types,
        compositions,
        connectors,
        mapping_sets,
        elements,
    )
    if collisions:
        raise ValueError("rule:name.name-space-collision: " + "; ".join(collision.sentence for collision in collisions))


def _check_termination_and_restart(swcs: list[SWComponent]) -> None:
    """Refuse a model with a component that sets no termination handling.

    The writer's twin of ``rule:swc.undeclared-termination-and-restart``. ``handleTerminationAndRestart`` has
    multiplicity 1 at R4.2.2 and R4.3.0, the only versions SWCraft writes. It runs **last**, after emission and
    before anything is written, so an earlier error is reported first.

    Raises:
        ValueError: listing every offending component, each in the rule's one sentence.
    """

    missing = components_without_termination_handling(swcs)
    if missing:
        raise ValueError(
            "rule:swc.undeclared-termination-and-restart: "
            + "; ".join(termination_handling_message(swc.name) for swc in missing)
        )


# ---------------------------------------------------------------------------
# The data description elements
# ---------------------------------------------------------------------------


def generates_kind_form(dt: object, base_kinds: tuple[type, ...]) -> bool:
    """Return whether *dt* generates the form of a :data:`RESERVED_DATA_TYPE_RULES` row whose kinds are *base_kinds*.

    An element of one of the row's kinds generates it, except an enumeration that names a ``texttable`` by
    ``compu_method_ref``. ``validator.generates_compu_method`` restates it.
    """
    if not isinstance(dt, base_kinds):
        return False
    return not isinstance(dt, EnumDataType) or dt.literals is not None


def _check_data_description_elements(description: DataDescription) -> None:
    """Refuse a model breaking a rule of the unit, compu method or data constraint layers, before anything is written.

    Also refuses every free text of a declared element XML 1.0 cannot carry. What it does **not** reach: an
    element's names (:func:`_check_data_description_element_names`).

    Raises:
        ValueError: listing every offending record, ``rule:<id>: <message>`` each, or the first text XML 1.0 cannot
            carry.
    """

    _refuse_rule_records(data_description_element_records(description))
    for owner, field, text, _subject in data_description_element_texts(description):
        _refuse_unrepresentable_text(owner, field, text)


def _check_data_description_element_names(description: DataDescription) -> None:
    """Refuse a unit, compu method or data constraint whose emitted name is not a legal AUTOSAR ``Identifier``.

    The writer's twin of ``rule:identifier.declared`` over the three element
    layers, before anything is emitted: each element's ``name`` (its
    ``SHORT-NAME``), its ``package``, and a texttable entry's ``name`` (its
    scale's ``SHORT-LABEL`` and ``VT``, judged as an enumeration literal's is).
    No generated form lengthens any of these names. Duplicates are
    :func:`_check_data_description_elements`' refusals.

    Raises:
        ValueError: naming every offending candidate (:func:`_check_identifiers`).
    """
    candidates: list[tuple[str, str]] = []
    for unit in description.units:
        candidates.extend(_package_candidate(f"Unit '{unit.name}'", unit.package))
        candidates.append(("Unit name", unit.name))
    for compu_method in description.compu_methods:
        candidates.extend(_package_candidate(f"Compu method '{compu_method.name}'", compu_method.package))
        candidates.append(("Compu method name", compu_method.name))
        if isinstance(compu_method, TextTable):
            candidates.extend(
                (f"Compu method '{compu_method.name}' entry name", entry.name) for entry in compu_method.entries
            )
    for data_constr in description.data_constraints:
        candidates.extend(_package_candidate(f"Data constraint '{data_constr.name}'", data_constr.package))
        candidates.append(("Data constraint name", data_constr.name))
    _check_identifiers(candidates, "data_types/")


def _emit_unit_element(package: ArxmlPackage, unit: Unit) -> etree._Element:
    """Emit a declared ``unit`` element as a ``UNIT``.

    ``DISPLAY-NAME`` -- first in ``group UNIT`` (XSD 4.2.2 ``:66568``, 4.3.0 ``:76795``) -- is written exactly as
    held, only when not empty. ``FACTOR-SI-TO-UNIT``, ``OFFSET-SI-TO-UNIT`` and ``PHYSICAL-DIMENSION-REF`` are not
    written: the model holds no SI relation.
    """
    node = package.create_element("UNIT", unit.name)
    set_description(node, unit.description)
    if unit.display_name != "":
        sub_element(node, "DISPLAY-NAME", unit.display_name)
    return node


def _emit_texttable_element(package: ArxmlPackage, compu_method: TextTable) -> etree._Element:
    """Emit a declared ``texttable`` element as a ``TEXTTABLE`` ``COMPU-METHOD``.

    :func:`_emit_text_table`'s shape, the enumeration's own: ``CATEGORY``, one
    closed single-point ``COMPU-SCALE`` per entry, in declaration order, whose
    ``SHORT-LABEL`` and ``VT`` are the entry's ``name`` and whose ``DESC`` is the
    entry's ``description``; the method's own ``DESC`` from its ``description``.
    """
    node = _emit_text_table(package, compu_method.name, compu_method.entries)
    set_description(node, compu_method.description)
    return node


def _emit_linear_element(package: ArxmlPackage, compu_method: Linear) -> etree._Element:
    """Emit a declared ``linear`` element as a ``LINEAR`` ``COMPU-METHOD``.

    :func:`_emit_linear`'s shape: a ``UNIT-REF`` to the ``unit`` element it names (``[constr_1175]``, SWCT R4.2.2
    ``:16954``, R4.3.0 ``:18398``: a compu method that is not ``TEXTTABLE``, ``BITFIELD_TEXTTABLE`` or ``IDENTICAL``
    refers to a unit), and one ``COMPU-SCALE`` ``[offset, factor]`` over ``[1]``. No scale label: ``[constr_1375]``
    (SWCT R4.2.2 ``:17401`` with Table 5.74, R4.3.0 ``:18891`` with Table 5.78) makes ``CompuScale.shortLabel`` not
    applicable for ``LINEAR`` in both releases.

    **One direction, internal to physical, at both versions.** R4.3.0's Table 5.78 gives both direction cells of
    ``LINEAR`` ``D(1)``, *"either compuPhysToInternal or compuInternalToPhys shall be"* defined. R4.2.2's Table 5.74
    prints ``compuInternalToPhys`` ``D(1)`` with note (1) *"This applies if not already defined by
    compuPhysToInternal"*, and a bare ``D`` in the ``compuPhysToInternal`` cell; the text beside the table (SWCT
    R4.2.2 ``:16923-16929``) says a ``CompuMethod`` is usually defined for one conversion direction only, and that
    for a linear function *"this is sufficient because the inverse function can be derived quite easily"*. The
    writer reads R4.2.2 by that text and the note. **The stated limit**: the bare ``D`` in R4.2.2's
    ``compuPhysToInternal`` cell is not written, and no check here reads that cell; an XSD pass does not reach it
    either (both direction elements are optional in the schema).
    """
    node = _emit_linear(
        package,
        compu_method.name,
        _placed_path(_HOME.UNITS, compu_method.unit),
        compu_method.factor,
        compu_method.offset,
        compu_method.limits,
    )
    set_description(node, compu_method.description)
    return node


def _emit_data_constr_element(package: ArxmlPackage, data_constr: DataConstr) -> etree._Element:
    """Emit a declared ``data_constr`` element as a ``DATA-CONSTR``.

    :func:`_emit_data_constr`'s shape with ``PHYS-CONSTRS``; ``INTERNAL-CONSTRS`` is never written: the element
    holds physical limits only.
    """
    node = _emit_data_constr(
        package, data_constr.name, "PHYS-CONSTRS", data_constr.min, data_constr.max, level=data_constr.level
    )
    set_description(node, data_constr.description)
    return node


def _emit_data_description_elements(document: ArxmlDocument, description: DataDescription) -> None:
    """Emit every declared unit, compu method and data constraint into its layer's package.

    Into ``/DataTypes/Units``, ``/DataTypes/CompuMethods`` or
    ``/DataTypes/Constraints`` (then the element's own ``package``), each layer in
    its reader's order and the three in that order, so every type that names one
    references backwards. **An element no type references is written too**: it is
    declared, and :func:`_check_package_containment` requires every declared
    element emitted. A layer with no element opens no package.
    """
    for unit in description.units:
        _emit_unit_element(document.home(_HOME.UNITS), unit)
    for compu_method in description.compu_methods:
        # Fail-closed: a compu method kind no arm emits is a writer defect, never left out.
        if isinstance(compu_method, TextTable):
            _emit_texttable_element(document.home(_HOME.COMPU_METHODS), compu_method)
        elif isinstance(compu_method, Linear):
            _emit_linear_element(document.home(_HOME.COMPU_METHODS), compu_method)
        else:
            raise TypeError(f"no emitter writes the compu method kind {type(compu_method).__name__}")
    for data_constr in description.data_constraints:
        _emit_data_constr_element(document.home(_HOME.DATA_CONSTRAINTS), data_constr)


def _check_access_forms(swcs: list[SWComponent], interfaces: list[Interface], version: ArxmlVersion) -> None:
    """Refuse an implicit read or an asynchronous call ``check-model`` refuses, before emission.

    The calls are judged at the version this run **generates**, so ``--arxml-version`` decides whether an
    asynchronous call with no result point is refused (``[constr_2006]`` of R4.2.2) or not (R4.3.0).

    Raises:
        ValueError: listing every finding, ``rule:<id>: <message>`` each.
    """

    _refuse_rule_records([*implicit_read_records(swcs, interfaces), *async_call_records(swcs, version)])
