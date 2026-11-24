"""Whether an incoming definition is the target's: the conflict comparison of ``import component``.

Nothing here raises or reads the target project.

The comparison runs on an ARXML-representable projection, because the component ARXML cannot carry
every SWCraft field: a value type's ``unit`` (Table 5.18 gives it no place), authored port order (the
writer emits every P-port before the R-ports) and the component's ``memory`` budget. Those fields are
normalized out of BOTH sides, so a target definition differing only there is identical. A **created**
definition can never carry them.

What the writer does write is compared as it stands, except a **shared** definition's descriptions,
which are projected out. The runnable's ``reads``/``writes``/``calls`` are compared only for a marked
export or ``--replace-access-lists``; otherwise nulled on BOTH sides. A data element's ``init`` is
projected out **one-directionally**: an incoming ``None`` is ignored, an incoming value the target
contradicts is a conflict (:func:`incoming_init_disagreements`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.schema import (
    CSInterface,
    DataDescriptionElement,
    DataType,
    DataTypeMap,
    EnumDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    PortComSpec,
    RecordDataType,
    Runnable,
    SRInterface,
    SWComponent,
    TextTable,
    ValueDataType,
)
from swcraft_tools.validator import application_type_pairings
from swcraft_tools.writer.arxml_generation import (
    COM_SPEC_SILENT_VALUES as _COM_SPEC_SILENT_VALUES,
)
from swcraft_tools.writer.arxml_generation import (
    com_spec_value as _writer_com_spec_value,
)

from .capabilities import Decision, project_runnable, project_swc


def _projected_runnable(runnable: Runnable, decisions: Mapping[str, Decision] | None = None) -> Runnable:
    """One runnable, normalized for the divergence comparison.

    Every capability family's projection is the registry's (:func:`.capabilities.project_runnable`),
    by *decisions*. ``None`` compares every family but the access lists, which are nulled on both sides.
    """
    own: dict[str, Any] = {
        # `symbol` and the stated names are compared as they stand. LIMIT: a target stating a
        # `symbol` or a `name` equal to the derived one reads back from its own export with none,
        # so an unchanged round trip reports a difference.
        # Trace links: an export has no place for a link, so a linked runnable would otherwise
        # diverge against its own unchanged export.
        "satisfies": [],
    }
    # The budget (`wcet` and its type) is compared: the export carries it in `SWC-TIMING`. A file
    # runnable stating none has already been given the target's pair (`carry_target_costs`).
    return project_runnable(runnable, decisions, own)


def projected_swc(
    swc: SWComponent,
    decisions: Mapping[str, Decision] | None = None,
    variable_mappings: frozenset[str] = frozenset(),
) -> SWComponent:
    """The component, normalized for the divergence comparison.

    Every capability family's projection is the registry's (:func:`.capabilities.project_swc`), by
    *decisions*, ``None`` meaning the default :func:`_projected_runnable` names.

    ``variable_mappings``: when the variables are not compared, the target's ``data_type_mappings``
    entries an ``--update`` would carry for the left-out variables alone are dropped on both sides
    (:func:`.capabilities.carried_variable_mappings`); otherwise an unchanged file would diverge on
    that entry alone.

    ``behavior_name``, ``handle_termination_and_restart`` (an incoming export stating none has
    already been given the target's value) and a timing event's ``disabled_modes`` are compared as
    they stand. A port's ``com_specs`` go through :func:`_projected_com_specs` on both sides, so the
    spellings of one ARXML compare equal.
    """
    return swc.model_copy(
        update={
            # `description` is compared (the writer writes it as `DESC`) once an empty incoming
            # text has been given the target's (`carry_target_descriptions`).
            # `memory`, `asil` and `satisfies` appear in no export (AUTOSAR Classic has no safety
            # level attribute, requirements are never exported); comparing them would make every
            # round trip look divergent and leave `--update` as the only exit.
            "memory": {},
            "asil": None,
            "satisfies": [],
            # Package placement is model data an export's path only mirrors.
            "package": None,
            # Stable provide-first sort, matching the writer's port order (`_ordered_ports()`):
            # authored order is not recoverable from the file, and every component round trip's
            # equality depends on this order.
            "ports": [
                # `measures` and `observed_interface` (a hint on an external stub's ports, and a
                # stub is never exported) appear in no export; the port's trace links neither.
                p.model_copy(
                    update={
                        "measures": {},
                        "observed_interface": None,
                        "com_specs": _projected_com_specs(p.com_specs),
                        "satisfies": [],
                    }
                )
                for p in sorted(swc.ports, key=lambda p: p.direction != "provide")
            ],
            "runnables": [_projected_runnable(r, decisions) for r in swc.runnables],
            "data_type_mappings": [name for name in swc.data_type_mappings if name not in variable_mappings],
            **project_swc(swc, decisions),
        }
    )


def projected_interface(iface: Interface) -> Interface:
    """Return *iface* without the fields a shared interface's comparison ignores, for an equality check."""
    # `description` and `package`: a shared definition is reused and never replaced, so a
    # documentation or placement difference breaks no consumer (the differing text is listed by
    # `untaken_description_lines`). `is_service`, `sw_impl_policy` and `sw_calibration_access` are
    # compared: they change what every consumer gets. `satisfies`: an export carries no link, and
    # an interface divergence has no `--update` exit.
    update: dict[str, Any] = {"description": "", "package": None, "satisfies": []}
    if isinstance(iface, SRInterface):
        # `init` is blanked on BOTH sides: an export carries a value only where its producer
        # declared one, so an incoming None may face an authored target value. This is the
        # structural half; `incoming_init_disagreements` is the other half, over the unprojected
        # pair, and keeps an incoming value that IS there from being erased.
        # `asil`: an export carries no level, and an interface divergence has no `--update` exit,
        # so the first project to rate a signal would lose `import component` for every component
        # sharing the interface. An incoming None here is always an absence.
        update["data_elements"] = [
            element.model_copy(update={"init": None, "asil": None, "description": ""})
            for element in iface.data_elements
        ]
    elif isinstance(iface, CSInterface):
        update["operations"] = [
            operation.model_copy(
                update={
                    "description": "",
                    "arguments": [argument.model_copy(update={"description": ""}) for argument in operation.arguments],
                }
            )
            for operation in iface.operations
        ]
    return iface.model_copy(update=update)


def _init_word(value: object | None) -> str:
    """One side's ``init`` in a conflict line: the value, or ``none`` (the YAML field is absent).

    Rendered by :func:`~swcraft_tools.rendering.render_model_scalar`, not ``repr``: a hand-authored
    ``init: 0x<4000 f>`` loads but raises inside its own ``repr``, which would fail while formatting
    the refusal."""
    return "none" if value is None else render_model_scalar(value)


def incoming_init_disagreements(existing: Interface, incoming: Interface) -> list[str]:
    """Every data element whose declared incoming ``init`` the target contradicts, one line each.

    An incoming ``None`` is ignored. Call only after the projections compared equal. **Does not
    reach:** a port's COM-SPEC ``init`` (:func:`_projected_com_specs`); a *created* interface.
    """
    if not (isinstance(existing, SRInterface) and isinstance(incoming, SRInterface)):
        return []
    return [
        f"data element '{arriving.name}' arrives with init {_init_word(arriving.init)} "
        f"while the target declares {_init_word(current.init)}"
        for current, arriving in zip(existing.data_elements, incoming.data_elements, strict=True)
        if arriving.init is not None and arriving.init != current.init
    ]


#: Why an incoming ``init`` is compared while a missing one is not, appended to the conflict line.
INIT_ASYMMETRY_NOTE = (
    "an init value the incoming export does not declare is ignored -- an export carries one only "
    "where its producer declared it, so its absence says nothing about this project's value -- but "
    "one it does declare was read from the file, and a shared definition has no per-import override"
)


def projected_data_type(dt: DataType) -> DataType:
    """Return *dt* without the fields a shared data type's comparison ignores (descriptions, package, unit)."""
    update: dict[str, Any] = {"description": "", "package": None}
    if isinstance(dt, ValueDataType):
        # `unit` alone: Table 5.18 gives it no place. The bounds are written as `DATA-CONSTR`.
        update.update(unit="")
    elif isinstance(dt, EnumDataType) and dt.literals is not None:  # or a texttable, by name
        update["literals"] = [literal.model_copy(update={"description": ""}) for literal in dt.literals]
    elif isinstance(dt, RecordDataType):
        update["fields"] = [record_field.model_copy(update={"description": ""}) for record_field in dt.fields]
    elif isinstance(dt, ImplementationRecordDataType):
        update["elements"] = [element.model_copy(update={"description": ""}) for element in dt.elements]
    # A base type's `native_declaration` is compared as declared: the writer emits it exactly and
    # derives none, so a value equal to the type's own name is a declaration, not a default.
    return dt.model_copy(update=update)


#: The note on a divergent shared **data type** or **interface**: a shared definition ignores every
#: description because it is reused and never replaced, not because the ARXML cannot carry one.
SHARED_DEFINITION_NOTE = (
    "the comparison already ignores every description of a shared definition, which is reused and "
    "never replaced (a differing text is listed, not taken), and the fields the exported ARXML does "
    "not carry: a value type's unit, data element init values the "
    "incoming export does not declare and declared safety integrity levels -- this is a real, "
    "representable difference"
)


#: The note on a divergent shared **mapping set**, the one asymmetric shared-definition comparison.
CONTRADICTING_SET_NOTE = (
    "a component export carries the set narrowed to its own closure, so a map the export does "
    "not carry is never a difference and a map the target's set lacks extends it; what is refused "
    "is a map that would pair one application type with two implementation types"
)


def compare_carried_set(existing: MappingSet, incoming: MappingSet) -> tuple[list[DataTypeMap], list[str]]:
    """The maps *incoming* adds to the target's set of that name, and the maps it contradicts.

    A map the export does not carry is no difference; a map of an application type the target's set
    does not mention is **added**; one pairing it with a **different** implementation type is a
    contradiction, with no override, ``--update`` included.

    Returns:
        ``(added, contradictions)``: the added maps in the incoming set's order, and one sentence per
        contradicting map. Both empty is a silent reuse.
    """
    stored = {entry.application: entry.implementation for entry in existing.maps}
    added: list[DataTypeMap] = []
    contradictions: list[str] = []
    for entry in incoming.maps:
        if entry.application not in stored:
            added.append(entry)
        elif stored[entry.application] != entry.implementation:
            contradictions.append(
                f"it maps '{entry.application}' onto '{entry.implementation}' where the target's set maps it "
                f"onto '{stored[entry.application]}'"
            )
    return added, contradictions


def double_mappings_after_import(
    importing: SWComponent,
    existing_importing: SWComponent | None,
    target_swcs: Mapping[str, SWComponent],
    sets_before: Mapping[str, MappingSet],
    sets_after: Mapping[str, MappingSet],
) -> list[str]:
    """Every application type a component would pair with two implementation types after this import.

    Decided over the union of each component's sets **as they will stand after the whole import**.
    One sentence per double-mapped type; two sets pairing a type with the **same** implementation type
    agree.

    What this does not reach: a double mapping that **stood before** the import identically is not
    refused; ``check-model`` reports it (``rule:data-type.application-type-double-mapped``,
    ``[constr_1004]``'s *exactly one* half) and ``generate arxml`` refuses it. A target component naming
    no set this import creates or extends reads the same union before and after. A composite's members
    are not mapped one by one. An extension mapping a type the target left **unmapped**
    (``rule:data-type.application-type-unmapped``) changes that component's ARXML; the ``EXTENDED``
    block names every other component naming the extended set.
    """
    readings: list[tuple[str, Sequence[str], Sequence[str]]] = [
        (name, target_swcs[name].data_type_mappings, target_swcs[name].data_type_mappings)
        for name in sorted(target_swcs)
        if name != importing.name
    ]
    readings.append(
        (
            importing.name,
            existing_importing.data_type_mappings if existing_importing is not None else [],
            importing.data_type_mappings,
        )
    )
    lines: list[str] = []
    for name, named_before, named_after in readings:
        before = application_type_pairings(named_before, sets_before)
        after = application_type_pairings(named_after, sets_after)
        for application, onto in after.items():
            if len(onto) < 2 or set(before.get(application, {})) == set(onto):
                continue
            routes = " and ".join(
                f"onto '{implementation}' through set{'s' if len(through) > 1 else ''} "
                + ", ".join(f"'{set_name}'" for set_name in through)
                for implementation, through in onto.items()
            )
            lines.append(f"component '{name}' would map '{application}' {routes}")
    return lines


def com_spec_silent_spelling(entry: PortComSpec) -> dict[str, bool | float | str]:
    """The attributes *entry* states at the writer's silent value, by field name, with the value as stated.

    Read through the writer's own :func:`~swcraft_tools.writer.arxml_generation.com_spec_value` and
    :data:`~swcraft_tools.writer.arxml_generation.COM_SPEC_SILENT_VALUES`, so the writer, the importer
    and this comparison share one notion of *silent*. ``queue_length`` has no silent value.
    """
    spelling: dict[str, bool | float | str] = {}
    for tag, attribute in _COM_SPEC_SILENT_VALUES.items():
        value = _writer_com_spec_value(entry, tag)
        if value is not None and value == attribute.silent:
            spelling[attribute.field] = value
    return spelling


def _projected_com_specs(entries: Sequence[PortComSpec]) -> list[PortComSpec]:
    """A port's ``com_specs`` as the ARXML holds them, for the divergence comparison.

    An attribute stated at the writer's silent value reads as unstated, an entry left with only its
    ``data_element`` is dropped, and the entries are ordered by ``data_element``. **Does not reach:**
    ``queue_length`` and a port's own ``init`` are compared as they stand; an inapplicable attribute is
    ``check-model``'s finding (``rule:swc.port.com-spec-inapplicable``); the YAML spelling is kept only
    by :func:`keep_target_com_spec_spelling` on ``--update``.
    """
    projected: list[PortComSpec] = []
    for entry in entries:
        stated = entry.model_copy(update=dict.fromkeys(com_spec_silent_spelling(entry)))
        if stated != PortComSpec(data_element=entry.data_element):
            projected.append(stated)
    return sorted(projected, key=lambda entry: entry.data_element)


def projected_element(element: DataDescriptionElement) -> DataDescriptionElement:
    """A unit, compu method or data constraint as a shared definition compares it.

    Its ``description`` (and a texttable entry's) and its ``package`` are the target's, for
    :func:`projected_data_type`'s reason; every other field is written by the writer and compared.
    """
    update: dict[str, Any] = {"description": "", "package": None}
    if isinstance(element, TextTable):
        update["entries"] = [entry.model_copy(update={"description": ""}) for entry in element.entries]
    return element.model_copy(update=update)
