"""Atomic YAML write operations for the model's YAML files (the writer pattern).

Every write operation is atomic: validate inputs, validate via Pydantic, modify
in memory, dump YAML, audit. All public functions accept model_root as a str
path and use pathlib internally.

No ``create_*`` knows where its layer is stored: every target goes through :func:`_target_path`, which asks
:func:`~swcraft_tools.model_state.free_storage_path` for the file: the plain name, or the nearest free ``~N`` when
another element's file holds it.

Every dump writes ``encoding="utf-8", newline="\\n"``. The ``add_*`` / ``set_*`` / ``update_*`` rewrites call
``write_text`` here; the creates write through :func:`_write_new_file`, whose one text ``open`` is
:func:`~swcraft_tools.exclusive_create.open_new`'s. ``test_platform_io.py`` reaches only a ``write_text``/``open``
call spelled in this module: a dump handed to a handle opened elsewhere is invisible to it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from swcraft_tools import exclusive_create
from swcraft_tools.data_description import read_data_description
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, check_identifier
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import declared_identity, free_storage_path, scan_layer
from swcraft_tools.reader import (
    find_connector_file,
    find_data_type_file,
    find_element_file,
    find_hardware_file,
    find_requirement_file,
    find_sequence_file,
    find_swc_file,
    find_task_file,
    read_hardware,
    read_interfaces,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.scheduling_identity import EntryTriple, identities_for, written_selector
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Composition,
    Connector,
    Core,
    CSInterface,
    CSOperation,
    DataElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    Hardware,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    InitEvent,
    MappingSet,
    Memory,
    Partition,
    Port,
    Processor,
    RecordDataType,
    ReferenceDataType,
    Requirement,
    Runnable,
    Sequence,
    SequenceStep,
    SRInterface,
    SWComponent,
    Task,
    TaskRunnableRef,
    TimingEvent,
    ValueDataType,
)
from swcraft_tools.trace import parse_link
from swcraft_tools.validator import (
    access_point_collision_repair,
    check_connector_compatibility,
    data_description_package_path,
    data_type_layer,
    emitted_access_point_names,
    is_unwritable_integer,
    result_point_sites,
    shared_access_point_names,
    stated_access_point_names,
    suggest_identifier,
    variable_access_entries,
)

# The SHORT-NAMEs the ARXML writer generates for itself, imported from the
# emitter that owns them so the write-time and generation-time guards cannot
# disagree; ``arxml_generation`` imports nothing from here.
from swcraft_tools.writer.arxml_generation import (
    ACCESS_LIST_PREFIXES,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_ADDED_LENGTH,
    INIT_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    RESERVED_DATA_TYPE_FORM_RULES,
    RESERVED_DATA_TYPE_RULES,
    RESULT_POINT_PREFIX,
    TIMING_EVENT_PREFIX,
    TIMING_SUFFIX,
    VARIABLE_ACCESS_LIST_PREFIXES,
    access_point_added_length,
    emits_swc_timing,
    generated_data_type_forms,
    generates_kind_form,
    init_constant_name,
    runnable_access_entries,
)
from swcraft_tools.writer.yaml_documents import (
    composition_to_dict,
    connector_to_dict,
    cs_operation_to_dict,
    data_element_to_dict,
    data_type_to_dict,
    hardware_to_dict,
    mapping_set_to_dict,
    placed,
    requirement_to_dict,
    runnable_to_dict,
    sequence_to_dict,
    swc_to_dict,
    task_to_dict,
)
from swcraft_tools.yaml_load import load_yaml_document


class ReservedNameError(ValueError):
    """A write the ARXML exporter's own rules refuse, carrying the element it is about.

    A :class:`ValueError` subclass, so every ``except ValueError`` around the
    writers still catches it; it adds the layer and the element, so the editing
    engine can classify the refusal without reading the message. The writer guards
    run during materialization, before pass D, so without it the
    ``consistency-violation`` the validator declares for these conditions would
    never be reached and a well-formed change set would die with an uncaught
    ``ValueError``. The messages stay prose.

    The class is wider than its name: :func:`_check_init_value_writable` raises it
    for an unwritable integer *value* (``interface.init-unwritable-integer``), which
    wants the same answer, ``consistency-violation`` at ``{layer, element}``. Read
    it as "a writer refusal pass D would have reported"; the message says which rule.
    """

    def __init__(self, layer: str, element: str, message: str) -> None:
        super().__init__(message)
        #: The registry layer name — ``swc``, ``data_types`` or ``interfaces``.
        self.layer = layer
        #: The declared name of the element the refusal is about.
        self.element = element


# ---------------------------------------------------------------------------
# Audit helper — keeps writer.py independent of auditor.py
# ---------------------------------------------------------------------------


def audit(auditor: Any, operation: str, args: dict, result: str) -> None:
    """Call auditor.log_write if auditor is provided."""
    if auditor is not None:
        auditor.log_write(operation, args, result)


def check_path_segment(value: str, param_name: str) -> None:
    """Raise ValueError if *value* contains path-traversal characters.

    Rejects any string that contains ``/``, ``\\``, or ``..`` to prevent
    callers from escaping the target subdirectory inside model_root.
    """
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Invalid {param_name} '{value}': must not contain path separators or '..'")


def _target_path(model_root: str, layer: str, identity: str, placement: Mapping[str, str] | None = None) -> Path:
    """Return the absolute file a ``create_*`` writes, decided from the registry at the write.

    The plain name when no file holds it case-insensitively, else the nearest free ``{identity}~{n}`` (the file's
    suffix only). Call it immediately before the write. *placement* is ``{"group": group}`` for ``swc``, else ``None``.

    Raises:
        ValueError: the plain file name is held by a file declaring *identity*,
            or a suffixed name would exceed the file-name bound
            (``model_state.StorageNameRefusal``).
    """
    free = free_storage_path(model_root, layer, identity, placement, registry=current_registry())
    target = Path(model_root) / free.path
    if free.occupied_by_self:
        raise ValueError(f"An element '{identity}' of layer '{layer}' already exists at {target}")
    return target


def _write_new_file(target_file: Path, text: str, *, layer: str, identity: str) -> None:
    """Write *text* to *target_file*, which must not exist yet: the one write of every ``create_*``.

    The exclusive create (:func:`~swcraft_tools.exclusive_create.open_new`) creates the file or fails, truncates
    nothing and follows no link; a failed write removes the file. What it does not reach: the guarantee is the
    filesystem's (NFS before version 3 is its documented weak point); nothing locks the directory; an interrupted
    create leaves a short file; and it does not refuse a second element of one identity.

    Raises:
        ValueError: a file was already at *target_file* that the listing did not name. Every other ``OSError``
            propagates.
    """
    try:
        # The module, not the name, as every `exclusive_create` name this module uses.
        created = exclusive_create.open_new(target_file)
    except FileExistsError as error:
        raise ValueError(
            f"Cannot create '{identity}' of layer '{layer}': the file '{target_file.name}' "
            f"already exists and the store's listing did not name it"
        ) from error
    # A wrap or a write that fails takes the file this call created away, so the next
    # attempt is not refused by a file it never wrote (the helper's one cleanup policy).
    with created as handle:
        handle.write(text)


def _unparseable_layer_file(model_root: str, layer: str) -> str | None:
    """Return the first file of *layer* that does not parse, model-root-relative, or ``None``.

    Called only after the scan raised: ``yaml``'s mark names ``<unicode string>``,
    so the file has to be found again to be named. ``None`` if the tree changed in
    between; the caller then words its message without a file name.
    """
    root = Path(model_root)
    for entry in scan_layer(model_root, layer, registry=current_registry()).files:
        try:
            load_yaml_document(entry.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return entry.path.relative_to(root).as_posix()
    return None  # pragma: no cover - the scan raised a moment ago


@contextmanager
def _readable_layer(model_root: str, layer: str) -> Iterator[None]:
    """Refuse an unreadable file of *layer* as a ``ValueError``, never as a raw ``yaml.YAMLError``.

    Wraps every ``create_*`` content check (and ``create_sequence``'s task lookup). What it does not reach: the
    ``add_*`` / ``set_*`` / ``unset_*`` mutations still surface ``yaml.YAMLError`` from an unparseable layer file.
    """
    try:
        yield
    except yaml.YAMLError as exc:
        named = _unparseable_layer_file(model_root, layer)
        unreadable = (
            f"{named} is not valid YAML"
            if named is not None
            else "a file of it is not valid YAML, and it could no longer be identified"
        )
        raise ValueError(f"cannot check the '{layer}' layer: {unreadable} ({exc})") from exc


def _require_int(value: Any, label: str) -> int:
    """Raise ValueError unless *value* is a real ``int``.

    The one int-ness guard of every numeric budget writer, ``set_*`` and
    ``create_*`` alike, so a create cannot accept what its update refuses.
    Pydantic's lax mode coerces ``400.0`` to ``400``, so this is the only check
    that refuses a float; ``bool`` is excluded explicitly as an ``int`` subclass.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an int, got {type(value).__name__}")
    return value


def _require_pool_name(pool: str) -> None:
    """Raise ValueError if *pool* is blank or whitespace-only."""
    if not pool or not pool.strip():
        raise ValueError("pool must not be empty")


def _require_declared_memory_pools(model_root: str, pools: Iterable[str]) -> None:
    """Raise ValueError unless every name in *pools* is a declared `kind: memory` element.

    Shared by :func:`create_swc` and :func:`set_swc_memory_budget`. The blank-name
    check runs first, so an empty name is reported as such rather than as an
    undeclared memory. *pools* may be any iterable of names (a ``dict`` contributes
    its keys); an empty one reads nothing. The refusal names the declared memories,
    sorted and de-duplicated as ``validator._declared`` gives them, because the
    repair (a spelling fix or a new element) needs them.
    """
    names = list(pools)
    for pool in names:
        _require_pool_name(pool)
    if not names:
        return
    declared = sorted({element.name for element in read_hardware(model_root) if isinstance(element, Memory)})
    for pool in names:
        if pool not in declared:
            raise ValueError(
                f"Memory pool '{pool}' is not declared as a 'kind: memory' element in "
                f"{model_root}/hardware — declared memories are {declared}"
            )


# ---------------------------------------------------------------------------
# Identifier conformance on write
# ---------------------------------------------------------------------------


def _suggest(name: str, max_length: int) -> str:
    """Return a conforming name to offer, worded as ``check-model`` words it.

    Borrows ``validator.suggest_identifier`` so a caller is never told two
    different things about one name.
    """

    return suggest_identifier(name, max_length=max_length)


def require_identifier(name: str, subject: str) -> None:
    """Reject a name that is not a legal AUTOSAR ``Identifier``.

    The write-path twin of the generation and ``check-model`` checks, so a create never succeeds into a state a later
    ``validate`` refuses. *name* is the name as it will be **emitted**; *subject* leads the message.

    Raises:
        ValueError: naming the offender, the reason and a conforming suggestion.
    """
    reason = check_identifier(name)
    if reason is None:
        return
    raise ValueError(
        f"{subject} '{name}' is not a legal AUTOSAR Identifier: {reason}. "
        f"Choose another name — for example '{_suggest(name, MAX_IDENTIFIER_LENGTH)}'."
    )


def check_generated_identifier(declared: str, emitted: str, added_length: int, subject: str) -> None:
    """Reject a *generated* ``SHORT-NAME`` the declared name does not predict.

    A legal declared name can produce an illegal emitted one: ``_InternalBehavior`` costs 17 characters, so a
    112-character component has a legal ``_Implementation`` and an illegal ``_InternalBehavior``.

    Args:
        declared: the name the caller passed, which the suggestion derives from.
        emitted: the generated ``SHORT-NAME``, in its final shape.
        added_length: how many characters the writer adds to *declared* (a count, since an event name's ``_0``/``_1``
            counter adds a prefix and a suffix).
        subject: what the generated name is, e.g. ``"SWC 'A' generated SWC-INTERNAL-BEHAVIOR name"``.

    Raises:
        ValueError: naming the emitted form, its length, the reason and a
            conforming suggestion for the declared name.
    """
    reason = check_identifier(emitted)
    if reason is None:
        return
    budget = MAX_IDENTIFIER_LENGTH - added_length
    raise ValueError(
        f"{subject} '{emitted}' ({len(emitted)} characters) is not a legal AUTOSAR Identifier: "
        f"{reason}. Choose a name of at most {budget} characters — for example "
        f"'{_suggest(declared, budget)}'."
    )


def _check_generated_pair_identifier(
    first: str, second: str, emitted: str, added_length: int, subject: str, *, stated_field: str | None = None
) -> None:
    """Check a form derived from **two** names: the twin of :func:`check_generated_identifier`.

    The two names share ``MAX_IDENTIFIER_LENGTH - added_length`` characters (122 for ``{Interface}_{Element}_Init``,
    125 for ``R_``/``W_``, 122 for ``Call_``). The message suggests shortening the longer one, on a tie the second
    (the member, referenced from nowhere else).

    Args:
        first: the interface (init guard) or the port (access guard).
        second: the member name.
        emitted: the generated ``SHORT-NAME``, in its final shape.
        added_length: the characters the writer adds to the two names together.
        subject: what the generated name is, for the message.
        stated_field: the field that can state the emitted name instead, offered as a repair; None when there is none.

    Raises:
        ValueError: naming both names, the emitted form, its length, the reason,
            the joint budget and a suggestion for the longer name.
    """
    reason = check_identifier(emitted)
    if reason is None:
        return
    budget = MAX_IDENTIFIER_LENGTH - added_length
    longer = first if len(first) > len(second) else second
    allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
    raise ValueError(
        f"{subject} '{emitted}' ({len(emitted)} characters) is not a legal AUTOSAR Identifier: "
        f"{reason}. '{first}' and '{second}' get {budget} characters between them; shorten "
        f"'{longer}' to at most {allowance} characters — for example '{_suggest(longer, allowance)}'."
        + (f" Or state {stated_field}: a stated name is written in place of the generated one." if stated_field else "")
    )


def _check_event_identifier(runnable_name: str, swc_name: str, event_type: str) -> None:
    """Reject an event ``SHORT-NAME`` ``{prefix}{runnable}`` a legal runnable name would generate.

    Charged without ``_unique_event_names()``'s counter, which this surface cannot trigger.

    Raises:
        ValueError: if the generated event name is not a legal ``Identifier``.
        TypeError: if *event_type* is not ``"timing"`` or ``"init"`` (the pair-named kinds have guards of their own)
            -- a programming error, never a model refusal.
    """
    if event_type == "timing":
        prefix, element = TIMING_EVENT_PREFIX, "TIMING-EVENT"
    elif event_type == "init":
        prefix, element = INIT_EVENT_PREFIX, "INIT-EVENT"
    elif event_type == "operation":
        raise TypeError(
            f"an event of type {event_type!r} is not named for its runnable: an operation event's name is "
            "derived from its port and operation (see check_operation_event_identifier)"
        )
    elif event_type == "data":
        raise TypeError(
            f"an event of type {event_type!r} is not named for its runnable: a data-received event's name is "
            "derived from its port and element (see check_data_event_identifier)"
        )
    elif event_type == "call_returned":
        raise TypeError(
            f"an event of type {event_type!r} is not named for its runnable: a call-returned event's name is "
            "derived from its port and operation (see check_call_returned_event_identifier)"
        )
    else:
        raise TypeError(f"an event kind the identifier guard has not been taught: {event_type!r}")
    check_generated_identifier(
        runnable_name,
        f"{prefix}{runnable_name}",
        len(prefix),
        f"SWC '{swc_name}' runnable '{runnable_name}' generated {element} name",
    )


def check_operation_event_identifier(
    swc_name: str, runnable_name: str, port: str, operation: str, emitted: str
) -> None:
    """Check ``OIT_{port}_{operation}``, the operation event's pair-named form.

    *emitted* is the **final** name, after ``_unique_event_names``' collision suffix. An illegal port or operation
    name is not charged here: one root cause, one offender.

    Raises:
        ValueError: naming the component, the runnable, the pair, the emitted form and the joint budget, if the
            emitted form is not a legal ``Identifier``.
    """
    _check_pair_event_identifier(
        swc_name, runnable_name, port, operation, emitted, "operation event", "OPERATION-INVOKED-EVENT"
    )


def _generated_data_type_forms(dt: DataType) -> tuple[tuple[str, str], ...]:
    """Return which field-scoped generated forms *dt* generates, whatever its kind.

    The predicate is :func:`~swcraft_tools.writer.arxml_generation.generated_data_type_forms`'s.

    Returns:
        ``(suffix, emitted element)`` pairs, in the writer's declared order.
    """
    minted = generated_data_type_forms(dt)
    return tuple(
        (suffix, what)
        for suffix, what, kind, _fields in RESERVED_DATA_TYPE_FORM_RULES
        if isinstance(dt, kind) and suffix in minted
    )


def check_data_type_identifiers(dt: DataType) -> None:
    """Run the identifier rule over everything ``create_data_type`` writes.

    Four groups a declared-name-only check would miss:

    * the kind-scoped generated forms of
      :data:`~swcraft_tools.writer.arxml_generation.RESERVED_DATA_TYPE_RULES`
      (``{x}_CompuMethod`` for an enum);
    * the field-scoped forms (:func:`_generated_data_type_forms`);
    * enum literal names, which become ``COMPU-SCALE``/``TEXT-TABLE`` entries;
    * record field names, which become ``APPLICATION-RECORD-ELEMENT``s.

    Called after the kind dispatch, because the rules are kind-scoped and the kind
    is only trustworthy once Pydantic has accepted the object.

    Raises:
        ValueError: on the first non-conforming generated or member name.
    """
    for suffix, what, base_kinds in RESERVED_DATA_TYPE_RULES:
        if generates_kind_form(dt, base_kinds):
            check_generated_identifier(
                dt.name,
                f"{dt.name}{suffix}",
                len(suffix),
                f"Data type '{dt.name}' generated {what} name",
            )
    for suffix, what in _generated_data_type_forms(dt):
        check_generated_identifier(
            dt.name,
            f"{dt.name}{suffix}",
            len(suffix),
            f"Data type '{dt.name}' generated {what} name",
        )
    if isinstance(dt, EnumDataType):
        # An enumeration naming a texttable states no literal.
        for literal in dt.literals or ():
            require_identifier(literal.name, f"Enum '{dt.name}' literal name")
    elif isinstance(dt, RecordDataType):
        for record_field in dt.fields:
            require_identifier(record_field.name, f"Record '{dt.name}' field name")
    elif isinstance(dt, ImplementationRecordDataType):
        # Each element is an IMPLEMENTATION-DATA-TYPE-ELEMENT named by its `name`.
        for implementation_element in dt.elements:
            require_identifier(implementation_element.name, f"Implementation record '{dt.name}' element name")
    elif isinstance(dt, ImplementationArrayDataType):
        # The array's one sub-element carries `element_name` as its SHORT-NAME.
        require_identifier(dt.element_name, f"Implementation array '{dt.name}' element name")


# ---------------------------------------------------------------------------
# create_swc
# ---------------------------------------------------------------------------


def check_reserved_swc_name(model_root: str, name: str, *, external: bool = False, budgeted: bool = False) -> None:
    """Reject an SWC name the ARXML writer generates for itself.

    Checked in both directions: ``A_Implementation`` beside an existing ``A``, and ``A`` beside an existing
    ``A_Implementation`` (``_InternalBehavior`` is nested in its component, so it is not reserved). A collision needs
    a component that EMITS the ``SWC-IMPLEMENTATION``, which an external stub never does: ``A_Implementation`` beside a
    stub ``A`` is free, and so is a stub ``A`` (*external*) beside ``A_Implementation``, as
    ``name.swc-implementation-collision`` judges it; a stub named ``A_Implementation`` beside an emitting ``A`` is
    still refused. ``{base}_Timing`` is reserved the same way for a component one of whose runnables declares a
    ``wcet``; the reverse direction is checked only when the caller says the component it writes is *budgeted*.

    Raises:
        ReservedNameError: if *name* would collide with a ``SWC-IMPLEMENTATION``
            or a ``SWC-TIMING`` in either direction.
    """
    if name.endswith(IMPLEMENTATION_SUFFIX):
        base = name[: -len(IMPLEMENTATION_SUFFIX)]
        if base and _emits_implementation(find_swc_file(model_root, base)):
            raise ReservedNameError(
                "swc",
                name,
                f"SWC name '{name}' collides with the auto-generated SWC-IMPLEMENTATION name for "
                f"'{base}'. Choose another name.",
            )
    twin = f"{name}{IMPLEMENTATION_SUFFIX}"
    if not external and find_swc_file(model_root, twin) is not None:
        raise ReservedNameError(
            "swc",
            name,
            f"SWC name '{name}' collides with the existing SWC '{twin}', which would then share a "
            "SHORT-NAME with this component's auto-generated SWC-IMPLEMENTATION. Choose another name.",
        )
    if name.endswith(TIMING_SUFFIX):
        base = name[: -len(TIMING_SUFFIX)]
        if base and _emits_timing(find_swc_file(model_root, base)):
            raise ReservedNameError(
                "swc",
                name,
                f"SWC name '{name}' collides with the auto-generated SWC-TIMING name for '{base}', one of whose "
                "runnables declares a wcet. Choose another name.",
            )
    timing_twin = f"{name}{TIMING_SUFFIX}"
    if budgeted and find_swc_file(model_root, timing_twin) is not None:
        raise ReservedNameError(
            "swc",
            name,
            f"SWC name '{name}' collides with the existing SWC '{timing_twin}', which would then share a "
            "SHORT-NAME with this component's auto-generated SWC-TIMING (one of its runnables declares a wcet). "
            "Choose another name.",
        )


def _emits_timing(swc_file: str | None) -> bool:
    """Return whether the component stored at *swc_file* emits a ``SWC-TIMING``.

    ``False`` for no file and for a file that does not load: no budget can be read
    off an unreadable file, and ``check-model`` reports that file first.
    """
    if swc_file is None:
        return False
    try:
        swc = SWComponent.model_validate(load_yaml_document(Path(swc_file).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError):  # ValidationError and UnicodeDecodeError are ValueErrors
        # An unreadable base lets `X_Timing` be created where a readable budgeted `X`
        # would refuse it; refusing would block a name on a file nothing can read. The
        # model is unbuildable until the file is fixed, and `check-model` then reports
        # the collision too.
        return False
    return emits_swc_timing(swc)


def _emits_implementation(swc_file: str | None) -> bool:
    """Return whether the component stored at *swc_file* emits a ``SWC-IMPLEMENTATION``.

    ``False`` for no file and for an external stub, read through ``SWComponent``
    so the flag is the one ``check-model`` loads. A file that does not load counts
    as emitting, so an unreadable base keeps its refusal.
    """
    if swc_file is None:
        return False
    try:
        swc = SWComponent.model_validate(load_yaml_document(Path(swc_file).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError):  # ValidationError and UnicodeDecodeError are ValueErrors
        return True
    return not swc.external


def check_reserved_data_type_name(model_root: str, dt: DataType) -> None:
    """Reject a data type name the ARXML writer generates for itself.

    The data-type twin of :func:`check_reserved_swc_name`: it refuses on write what
    ``arxml_generation._check_data_type_names()`` refuses at generation, so no call
    creates a file the next build fails on. Checked in both directions:
    ``Mode_CompuMethod`` beside an enum ``Mode``, and an enum ``Mode`` beside an
    existing ``Mode_CompuMethod``. Only inside the package the form lies in
    (``data_description_package_path``): a type of another data description package
    may carry the name. Kind scoping is
    :data:`~swcraft_tools.writer.arxml_generation.RESERVED_DATA_TYPE_RULES` and
    :func:`~swcraft_tools.writer.arxml_generation.generates_kind_form`, read rather
    than restated, so this guard cannot disagree with generation.

    Raises:
        ReservedNameError: if *dt*'s name collides with a generated ``SHORT-NAME``
            in either direction. No fixed name is reserved: a mapping set is a
            declared element of its own layer.
    """

    name = dt.name
    # A type stub holds no SHORT-NAME, so it is never the twin a generated name
    # collides with; and only a type of *dt*'s own package is a sibling of a form
    # either of them generates.
    package = data_description_package_path(dt)
    declared = {
        existing.name: existing
        for existing in read_data_description(model_root).data_types
        if not isinstance(existing, ExternalDataType) and data_description_package_path(existing) == package
    }
    for suffix, what, base_kinds in RESERVED_DATA_TYPE_RULES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if base and generates_kind_form(declared.get(base), base_kinds):
                raise ReservedNameError(
                    data_type_layer(dt),
                    name,
                    f"Data type name '{name}' collides with the {what} auto-generated for "
                    f"'{base}'. Choose another name.",
                )
        if not generates_kind_form(dt, base_kinds):
            continue
        twin = f"{name}{suffix}"
        if twin in declared:
            raise ReservedNameError(
                data_type_layer(dt),
                name,
                f"Data type name '{name}' collides with the existing data type '{twin}', which "
                f"would then share a SHORT-NAME with this type's auto-generated {what}. "
                "Choose another name.",
            )

    # The field-scoped forms, in both directions: the stem's reservation depends
    # on what that stem DECLARES, not on its kind, so each direction asks
    # `_generated_data_type_forms` of the type at the other end.
    for suffix, what in _generated_data_type_forms(dt):
        twin = f"{name}{suffix}"
        if twin in declared:
            raise ReservedNameError(
                data_type_layer(dt),
                name,
                f"Data type name '{name}' collides with the existing data type '{twin}', which "
                f"would then share a SHORT-NAME with this type's auto-generated {what}. "
                "Choose another name.",
            )
    for suffix, what in dict.fromkeys((suffix, what) for suffix, what, _kind, _fields in RESERVED_DATA_TYPE_FORM_RULES):
        if not name.endswith(suffix):
            continue
        base = name[: -len(suffix)]
        stem = declared.get(base)
        # Which kinds generate a form is `generated_data_type_forms`' to say, for
        # every kind: asked of whatever the stem declares, never of a list here.
        if base and stem is not None and suffix in generated_data_type_forms(stem):
            raise ReservedNameError(
                data_type_layer(dt),
                name,
                f"Data type name '{name}' collides with the {what} auto-generated for '{base}'. Choose another name.",
            )


def _check_init_value_writable(interface_name: str, element_name: str, init: object) -> None:
    """Refuse an ``init`` carrying an integer this interpreter cannot write out.

    A value above ``sys.get_int_max_str_digits()`` would make every dump of the interface die; the detection is
    :func:`~swcraft_tools.validator.is_unwritable_integer`, so the refusal and ``interface.init-unwritable-integer``
    agree (with the limit disabled nothing is refused). The message names the value by ``bit_length()``.

    Raises:
        ReservedNameError: with ``layer="interfaces"``, so the editing engine
            answers it with ``consistency-violation``.
    """
    unwritable = _unwritable_init_integer(init)
    if unwritable is None:
        return
    path, value = unwritable
    raise ReservedNameError(
        "interfaces",
        interface_name,
        f"Interface '{interface_name}' data element '{element_name}' init value at "
        f"'{path}' is {render_model_scalar(value)}: writing it into the model file means "
        f"converting it to its decimal string, which is above this interpreter's "
        f"{sys.get_int_max_str_digits()}-digit limit "
        "(interface.init-unwritable-integer). Choose a value it can write.",
    )


def _unwritable_init_integer(init: object) -> tuple[str, object] | None:
    """Return the first integer in *init* this interpreter cannot write out, with its place, or ``None``.

    Depth first, the place spelled ``init``, ``.field``, ``[index]``; shared by
    :func:`_check_init_value_writable` and :func:`check_port_init_values_writable`.
    """

    def _walk(value: object, path: str) -> tuple[str, object] | None:
        if isinstance(value, dict):
            for key, member in value.items():
                found = _walk(member, f"{path}.{key}")
                if found is not None:
                    return found
            return None
        if isinstance(value, list):
            for index, member in enumerate(value):
                found = _walk(member, f"{path}[{index}]")
                if found is not None:
                    return found
            return None
        return (path, value) if is_unwritable_integer(value) else None

    return _walk(init, "init")


def check_port_init_values_writable(swc: SWComponent) -> None:
    """Refuse a port's own ``init`` carrying an integer this interpreter cannot write out.

    :func:`_check_init_value_writable`'s twin for a ``com_specs`` entry, for the
    same reason; called by ``writer.elements``' component guard, before the dump.
    It does not reach the mutation path's ``swc_to_dict`` callers, which take no
    ``init``.

    Raises:
        ReservedNameError: with ``layer="swc"``, so the editing engine answers
            it with ``consistency-violation``.
    """
    for port in swc.ports:
        for entry in port.com_specs:
            if entry.init is None:
                continue
            unwritable = _unwritable_init_integer(entry.init)
            if unwritable is None:
                continue
            path, value = unwritable
            raise ReservedNameError(
                "swc",
                swc.name,
                f"SWC '{swc.name}' port '{port.name}' com_specs init value for data element "
                f"'{entry.data_element}' at '{path}' is {render_model_scalar(value)}: writing it into the model "
                f"file means converting it to its decimal string, which is above this interpreter's "
                f"{sys.get_int_max_str_digits()}-digit limit "
                "(interface.init-unwritable-integer). Choose a value it can write.",
            )


def check_variable_init_values_writable(swc: SWComponent) -> None:
    """Refuse a variable's ``init`` carrying an integer this interpreter cannot write out.

    :func:`check_port_init_values_writable`'s twin for the component's
    ``variables``, for the same reason; called by ``writer.elements``' component
    guard, before the dump. It does not reach the mutation path's ``swc_to_dict``
    callers, which take no ``init``.

    Raises:
        ReservedNameError: with ``layer="swc"``, so the editing engine answers
            it with ``consistency-violation``.
    """
    for variable in swc.variables:
        if variable.init is None:
            continue
        unwritable = _unwritable_init_integer(variable.init)
        if unwritable is None:
            continue
        path, value = unwritable
        raise ReservedNameError(
            "swc",
            swc.name,
            f"SWC '{swc.name}' variable '{variable.name}' init value at '{path}' is {render_model_scalar(value)}: "
            "writing it into the model file means converting it to its decimal string, which is above this "
            f"interpreter's {sys.get_int_max_str_digits()}-digit limit "
            "(interface.init-unwritable-integer). Choose a value it can write.",
        )


def check_interface_init_constants(model_root: str, name: str, elements: Iterable[DataElement]) -> None:
    """Run every guard one interface write owes its ``init`` fields.

    Three refusals about the name ``{I}_{E}_Init``, all unconditional (whether or not an element carries an ``init``):

    * the joint budget (:func:`_check_generated_pair_identifier`), a plain
      ``ValueError``, which the editing protocol answers as ``invalid-identifier``;
    * a collision with a declared interface name, in both directions;
    * two pairs deriving one constant: ``A_B``/``C`` and ``A``/``B_C`` both give
      ``A_B_C_Init``.

    A fourth, :func:`_check_init_value_writable`, runs last. An interface already stored under *name* is excluded.
    *elements* is empty for a client-server interface, which derives no constant but can still *be* named like one.

    Raises:
        ValueError: a derived constant is not a legal AUTOSAR ``Identifier``.
        ReservedNameError: a derived constant collides with a declared interface
            name, two pairs derive one constant, or an ``init`` is unwritable.
    """
    data_elements = list(elements)
    element_names = [element.name for element in data_elements]
    for element_name in element_names:
        _check_generated_pair_identifier(
            name,
            element_name,
            init_constant_name(name, element_name),
            INIT_CONSTANT_ADDED_LENGTH,
            f"Interface '{name}' data element '{element_name}' generated CONSTANT-SPECIFICATION name",
        )

    others = [existing for existing in read_interfaces(model_root) if existing.name != name]
    # A stub holds no SHORT-NAME, so no derived constant collides with it, and its
    # listed data elements are names with no `init`, so they derive none.
    declared = {existing.name for existing in others if not isinstance(existing, ExternalInterface)}
    existing_constants: dict[str, tuple[str, str]] = {}
    for existing in others:
        if isinstance(existing, ExternalInterface):
            continue
        for member in getattr(existing, "data_elements", ()):
            existing_constants.setdefault(init_constant_name(existing.name, member.name), (existing.name, member.name))

    for element_name in element_names:
        emitted = init_constant_name(name, element_name)
        if emitted in declared:
            raise ReservedNameError(
                "interfaces",
                name,
                f"Interface '{name}' data element '{element_name}' generates the "
                f"CONSTANT-SPECIFICATION SHORT-NAME '{emitted}', which collides with the existing "
                f"interface '{emitted}'. Choose another name.",
            )
        owner = existing_constants.get(emitted)
        if owner is not None:
            raise ReservedNameError(
                "interfaces",
                name,
                f"Interface '{name}' data element '{element_name}' generates the "
                f"CONSTANT-SPECIFICATION SHORT-NAME '{emitted}', which interface '{owner[0]}' data "
                f"element '{owner[1]}' already generates. An interface name and a data element name "
                "may both contain '_', so the derivation is not injective; choose another name.",
            )

    if name in existing_constants:
        owner = existing_constants[name]
        raise ReservedNameError(
            "interfaces",
            name,
            f"Interface name '{name}' collides with the CONSTANT-SPECIFICATION SHORT-NAME "
            f"auto-generated for interface '{owner[0]}' data element '{owner[1]}'. Choose another name.",
        )

    for element in data_elements:
        _check_init_value_writable(name, element.name, element.init)


def check_runnable_access_names(swc_name: str, runnable: Runnable) -> None:
    """Run every guard one runnable's access lists and result points owe their names.

    Two refusals, among ``RUNNABLE-ENTITY`` siblings:

    * the identifier rule: a stated name as a declared name; an unstated one
      charged as ``R_``/``W_`` (joint 125), ``Call_`` (122) or ``Result_`` (120)
      for its pair, or ``RV_``/``WV_`` (125) for a variable. A plain
      ``ValueError``, answered as ``invalid-identifier``;
    * two entries of one runnable written with one name, e.g. port ``a_b``
      reading ``c`` and port ``a`` reading ``b_c`` (``R_a_b_c``), compared as
      written. The scope is the runnable.

    No other file is read.

    Raises:
        ValueError: a stated or derived access point or result point is not a legal AUTOSAR ``Identifier``.
        ReservedNameError: two entries of *runnable* are written with one access point name (``layer="swc"``).
    """
    for (list_name, port_name, _member_field, member), emitted, stated in zip(
        runnable_access_entries(runnable),
        emitted_access_point_names(runnable),
        stated_access_point_names(runnable),
        strict=True,
    ):
        prefix = ACCESS_LIST_PREFIXES[list_name]
        if stated is not None:
            require_identifier(
                stated,
                f"SWC '{swc_name}' runnable '{runnable.name}' {list_name} entry '{port_name}'.'{member}' stated name",
            )
            continue
        if check_identifier(port_name) is not None or check_identifier(member) is not None:
            continue  # one root cause, one offender: the declared name is the one to fix
        element = "SYNCHRONOUS-SERVER-CALL-POINT" if list_name == "calls" else "VARIABLE-ACCESS"
        _check_generated_pair_identifier(
            port_name,
            member,
            emitted,
            access_point_added_length(prefix),
            f"SWC '{swc_name}' runnable '{runnable.name}' {list_name} entry generated {element} name",
            stated_field="the entry's name",
        )

    # A variable access is charged only when the variable name conforms: an
    # illegal declared variable is `_check_swc_names`' offender, and an
    # undeclared one pass D's.
    for list_name, variable, stated in variable_access_entries(runnable):
        subject = f"SWC '{swc_name}' runnable '{runnable.name}' {list_name} entry '{variable}'"
        if stated is not None:
            require_identifier(stated, f"{subject} stated name")
            continue
        if check_identifier(variable) is not None:
            continue  # one root cause, one offender
        prefix = VARIABLE_ACCESS_LIST_PREFIXES[list_name]
        check_generated_identifier(
            variable, f"{prefix}{variable}", len(prefix), f"{subject} generated VARIABLE-ACCESS name"
        )

    # The result points, charged only when both declared names conform, as
    # `check_identifier_conformance` reads them.
    for subject, port_name, operation, stated, _steps, stated_field in result_point_sites(swc_name, runnable):
        if stated is not None:
            require_identifier(stated, f"{subject} name")
            continue
        if check_identifier(port_name) is not None or check_identifier(operation) is not None:
            continue  # one root cause, one offender
        _check_generated_pair_identifier(
            port_name,
            operation,
            f"{RESULT_POINT_PREFIX}{port_name}_{operation}",
            access_point_added_length(RESULT_POINT_PREFIX),
            f"{subject} generated ASYNCHRONOUS-SERVER-CALL-RESULT-POINT name",
            stated_field=stated_field,
        )

    collisions = shared_access_point_names(runnable)
    if collisions:
        shared = [
            f"'{emitted}' is {'written for' if stated else 'derived from'} {claimants}"
            for emitted, claimants, stated in collisions
        ]
        raise ReservedNameError(
            "swc",
            swc_name,
            f"SWC '{swc_name}' runnable '{runnable.name}' declares access entries that are written with one "
            f"access point SHORT-NAME: {'; '.join(shared)}. "
            + access_point_collision_repair(any(stated for *_shared, stated in collisions)),
        )


def create_swc(
    model_root: str,
    name: str,
    group: str,
    swc_type: str = "application",
    auditor: Any = None,
    memory: dict[str, int] | None = None,
    handle_termination_and_restart: str | None = None,
) -> str:
    """Create a new SWC YAML file at model_root/swc/{group}/{name}.yaml.

    ``memory`` is the optional initial ``{pool: bytes}`` budget map; every key must name a declared ``kind: memory``
    element. ``handle_termination_and_restart`` is ``no_support``, ``can_be_terminated`` or
    ``can_be_terminated_and_restarted``; left ``None`` the file states no value, which
    ``rule:swc.undeclared-termination-and-restart`` reports and the ARXML writer refuses: this API never chooses it.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, is not a legal AUTOSAR ``Identifier`` (in itself or in its generated
            ``_Implementation``/``_InternalBehavior`` form), an SWC with that name already exists, the name is one
            the ARXML writer reserves, or a ``memory`` key is blank, is not an ``int`` budget, or is not a declared
            ``kind: memory`` element (or the map fails ``schema.SWComponent`` validation, which also refuses a
            ``handle_termination_and_restart`` outside the three values).
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guards — reject separators and '..' in name or group
    check_path_segment(name, "name")
    if not group or not group.strip():
        raise ValueError("group must not be empty")
    check_path_segment(group, "group")

    # Identifier conformance before every other name rule: an illegal name
    # produces schema-invalid ARXML whatever else is true of it, and the two
    # generated component-side forms are checked with it because the declared
    # name does not predict them (_InternalBehavior costs 17 characters).
    require_identifier(name, "SWC name")
    for suffix, element in (
        (IMPLEMENTATION_SUFFIX, "SWC-IMPLEMENTATION"),
        (INTERNAL_BEHAVIOR_SUFFIX, "SWC-INTERNAL-BEHAVIOR"),
    ):
        check_generated_identifier(name, f"{name}{suffix}", len(suffix), f"SWC '{name}' generated {element} name")

    # Duplicate check — content-based: scan YAML name fields
    with _readable_layer(model_root, "swc"):
        existing = find_swc_file(model_root, name)
    if existing is not None:
        raise ValueError(f"SWC '{name}' already exists at {existing}")

    check_reserved_swc_name(model_root, name)

    budgets = dict(memory) if memory else {}
    for pool, size_bytes in budgets.items():
        _require_int(size_bytes, f"memory budget for pool '{pool}'")
    _require_declared_memory_pools(model_root, budgets)

    # Validate via Pydantic (catches invalid swc_type, negative budgets etc.)
    swc = SWComponent(
        name=name,
        type=swc_type,  # type: ignore[arg-type]  # pydantic: str -> Literal
        memory=budgets,
        handle_termination_and_restart=handle_termination_and_restart,  # type: ignore[arg-type]  # pydantic: str -> Literal
    )

    # A file name another element holds takes a collision suffix; the identity
    # was refused by content above.
    target_file = _target_path(model_root, "swc", name, {"group": group})
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(swc_to_dict(swc), default_flow_style=False, sort_keys=False),
        layer="swc",
        identity=name,
    )

    args: dict[str, Any] = {"name": name, "group": group, "swc_type": swc_type}
    if budgets:
        args["memory"] = budgets
    if handle_termination_and_restart is not None:
        args["handle_termination_and_restart"] = handle_termination_and_restart
    audit(auditor, "create_swc", args, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# add_port
# ---------------------------------------------------------------------------


def add_port(
    model_root: str,
    swc_name: str,
    port_name: str,
    direction: str,
    interface: str,
    auditor: Any = None,
) -> str:
    """Add a port to an existing SWC.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if SWC not found, direction is invalid, the port name is not
            a legal AUTOSAR ``Identifier``, or the port name is duplicate.
    """
    # Validate direction early — before touching the file
    if direction not in ("provide", "require"):
        raise ValueError(f"Invalid direction '{direction}': must be 'provide' or 'require'")

    require_identifier(port_name, f"SWC '{swc_name}' port name")

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    data = load_yaml_document(path.read_text(encoding="utf-8"))
    swc = SWComponent.model_validate(data)

    if any(p.name == port_name for p in swc.ports):
        raise ValueError(f"Port '{port_name}' already exists on SWC '{swc_name}'")

    new_port = Port(name=port_name, direction=direction, interface=interface)  # type: ignore[arg-type]  # pydantic: str -> Literal
    swc.ports.append(new_port)

    updated_data = swc_to_dict(swc)
    path.write_text(
        yaml.safe_dump(updated_data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "add_port",
        {"swc_name": swc_name, "port_name": port_name, "direction": direction, "interface": interface},
        swc_file,
    )
    return swc_file


# ---------------------------------------------------------------------------
# add_runnable
# ---------------------------------------------------------------------------


def add_runnable(
    model_root: str,
    swc_name: str,
    runnable_name: str,
    min_start_interval: float = 0.0,
    auditor: Any = None,
    reads: Iterable[Mapping[str, Any]] | None = None,
    writes: Iterable[Mapping[str, Any]] | None = None,
    calls: Iterable[Mapping[str, Any]] | None = None,
) -> str:
    """Add a runnable to an existing SWC.

    ``reads``, ``writes`` and ``calls`` are the runnable's access lists in the YAML's own vocabulary
    (``{port, element[, by]}``, ``{port, element}``, ``{port, operation}``), validated as the loader validates a file:
    a blank name or a pair listed twice is refused; a ``by`` on a write or a call is DROPPED, not refused.
    Cross-element rules are ``check-model``'s; an omitted list writes no key.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if SWC not found, the runnable name is not a legal AUTOSAR
            ``Identifier``, the runnable name is duplicate, an access list
            fails the model's own validation, or an entry's derived access-point name
            overflows its joint budget (see :func:`check_runnable_access_names`).
        ReservedNameError: two entries of the new runnable are written with one
            access point ``SHORT-NAME``.
    """
    require_identifier(runnable_name, f"SWC '{swc_name}' runnable name")

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    data = load_yaml_document(path.read_text(encoding="utf-8"))
    swc = SWComponent.model_validate(data)

    if any(r.name == runnable_name for r in swc.runnables):
        raise ValueError(f"Runnable '{runnable_name}' already exists on SWC '{swc_name}'")

    new_runnable = Runnable.model_validate(
        {
            "name": runnable_name,
            "min_start_interval": min_start_interval,
            "reads": list(reads or []),
            "writes": list(writes or []),
            "calls": list(calls or []),
        }
    )
    # Refused before the dump: a name `generate arxml` refuses would otherwise be
    # written here and fail two steps later.
    check_runnable_access_names(swc_name, new_runnable)
    swc.runnables.append(new_runnable)

    updated_data = swc_to_dict(swc)
    path.write_text(
        yaml.safe_dump(updated_data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    # the audit record carries the lists as written — the serializer's own
    # dicts, so `by` appears only where it was set
    written = runnable_to_dict(new_runnable)
    audit(
        auditor,
        "add_runnable",
        {
            "swc_name": swc_name,
            "runnable_name": runnable_name,
            "min_start_interval": min_start_interval,
            "reads": written.get("reads", []),
            "writes": written.get("writes", []),
            "calls": written.get("calls", []),
        },
        swc_file,
    )
    return swc_file


# ---------------------------------------------------------------------------
# add_timing_event
# ---------------------------------------------------------------------------


def add_timing_event(
    model_root: str,
    swc_name: str,
    runnable_name: str,
    period: float,
    auditor: Any = None,
) -> str:
    """Add a timing event to an existing runnable on an SWC.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if period <= 0, SWC not found, runnable not found, or the
            generated ``TMT_{runnable}`` event name is not a legal AUTOSAR
            ``Identifier`` (see :func:`_check_event_identifier`).
    """
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    data = load_yaml_document(path.read_text(encoding="utf-8"))
    swc = SWComponent.model_validate(data)

    runnable = next((r for r in swc.runnables if r.name == runnable_name), None)
    if runnable is None:
        raise ValueError(f"Runnable '{runnable_name}' not found on SWC '{swc_name}'")

    if any(e.type == "timing" for e in runnable.events):
        raise ValueError(f"Timing event already exists on runnable '{runnable_name}' of SWC '{swc_name}'")

    _check_event_identifier(runnable_name, swc_name, "timing")

    runnable.events.append(TimingEvent(type="timing", period=period))

    updated_data = swc_to_dict(swc)
    path.write_text(
        yaml.safe_dump(updated_data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "add_timing_event",
        {"swc_name": swc_name, "runnable_name": runnable_name, "period": period},
        swc_file,
    )
    return swc_file


# ---------------------------------------------------------------------------
# add_init_event
# ---------------------------------------------------------------------------


def add_init_event(
    model_root: str,
    swc_name: str,
    runnable_name: str,
    auditor: Any = None,
) -> str:
    """Add an init event to an existing runnable on an SWC.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if SWC not found, runnable not found, or the generated
            ``IT_{runnable}`` event name is not a legal AUTOSAR ``Identifier``
            (see :func:`_check_event_identifier`).
    """
    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    data = load_yaml_document(path.read_text(encoding="utf-8"))
    swc = SWComponent.model_validate(data)

    runnable = next((r for r in swc.runnables if r.name == runnable_name), None)
    if runnable is None:
        raise ValueError(f"Runnable '{runnable_name}' not found on SWC '{swc_name}'")

    if any(e.type == "init" for e in runnable.events):
        raise ValueError(f"Init event already exists on runnable '{runnable_name}' of SWC '{swc_name}'")

    _check_event_identifier(runnable_name, swc_name, "init")

    runnable.events.append(InitEvent(type="init"))

    updated_data = swc_to_dict(swc)
    path.write_text(
        yaml.safe_dump(updated_data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "add_init_event",
        {"swc_name": swc_name, "runnable_name": runnable_name},
        swc_file,
    )
    return swc_file


# ---------------------------------------------------------------------------
# create_interface
# ---------------------------------------------------------------------------


def create_interface(
    model_root: str,
    name: str,
    data_elements: list[dict[str, Any]],
    auditor: Any = None,
    description: str = "",
    is_service: bool = False,
) -> str:
    """Create a sender-receiver interface YAML at model_root/interfaces/{name}.yaml.

    Args:
        data_elements: dicts with a 'name' key and one of 'application_type' and 'implementation_type', and
            optionally 'init', 'asil', 'description', 'sw_impl_policy' and 'sw_calibration_access', validated by
            ``schema.DataElement`` and written when present. Whether an ``init`` conforms to the declared type is
            ``check-model``'s question, as is a type naming no declared data type.
        description: omitted from the YAML when empty.
        is_service: AUTOSAR's ``isService``, written last and only when ``True``.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, contains path-traversal chars, the interface name or any data element name
            is not a legal AUTOSAR ``Identifier``, an interface with that name already exists, or a file of the
            layer does not parse (:func:`_readable_layer`).
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard — reject any name that would escape interfaces/
    check_path_segment(name, "interface name")

    require_identifier(name, "Interface name")

    # Duplicate check — content-based, over both interface kinds of the layer.
    with _readable_layer(model_root, "interfaces"):
        existing = find_element_file(model_root, "interfaces", name)
    if existing is not None:
        raise ValueError(f"Interface '{name}' already exists at {existing}")

    # A hand-written element rebuild: every declared field must be named here,
    # or it is erased.
    elements = [
        DataElement(
            name=e["name"],
            # Both type fields are carried; the schema admits one.
            application_type=e.get("application_type"),
            implementation_type=e.get("implementation_type"),
            init=e.get("init"),
            asil=e.get("asil"),
            description=e.get("description", ""),
            sw_impl_policy=e.get("sw_impl_policy"),
            sw_calibration_access=e.get("sw_calibration_access"),
        )
        for e in data_elements
    ]
    iface = SRInterface(
        name=name, kind="sender_receiver", data_elements=elements, description=description, is_service=is_service
    )

    # The data elements are checked with the interface: this one call writes them
    # all, so a legal interface name carrying an illegal element name has to be
    # refused here rather than at the next build.
    for element in iface.data_elements:
        require_identifier(element.name, f"Interface '{name}' data element name")

    # The init constants, checked after every declared name conforms so one bad
    # name is not refused twice, and the writability of their values, before the
    # `yaml.safe_dump` below meets one it cannot represent.
    check_interface_init_constants(model_root, name, iface.data_elements)

    target_file = _target_path(model_root, "interfaces", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "name": iface.name,
        "kind": iface.kind,
        # Hand-written, mirroring the importer's `_data_element_to_dict` key for
        # key: a `DataElement` field missing from either is erased on the way to
        # the file. An absent `init` is omitted, so such a file round-trips.
        "data_elements": [data_element_to_dict(el) for el in iface.data_elements],
    }
    if iface.description:
        data["description"] = iface.description
    # Key for key with the importer's `interface_to_dict`.
    placed(data, iface.package)
    # After `package`, in `model_fields` order, and only when `True`
    # (`omitAtDefault`), so a file without it round-trips byte-identically.
    if iface.is_service:
        data["is_service"] = True
    _write_new_file(
        target_file,
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        layer="interfaces",
        identity=name,
    )

    audit(auditor, "create_interface", {"name": name, "data_elements": data_elements}, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# create_cs_interface
# ---------------------------------------------------------------------------


def create_cs_interface(
    model_root: str,
    name: str,
    operations: list[dict],
    auditor: Any = None,
    description: str = "",
    is_service: bool = False,
) -> str:
    """Create a client-server interface YAML at model_root/interfaces/{name}.yaml.

    Args:
        operations: list of operation dicts, each with 'name' key and optional
            'arguments' list (each argument dict has 'name', 'direction' and one of
            'application_type' and 'implementation_type').
        description: omitted from the YAML when empty.
        is_service: AUTOSAR's ``isService``, written last and only when ``True``.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, contains path-traversal chars, the interface name or any operation/argument
            name is not a legal AUTOSAR ``Identifier``, an interface with that name already exists, or a file of
            the layer does not parse (:func:`_readable_layer`).
        ValidationError: if operations list is empty or any field value is invalid.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard — reject any name that would escape interfaces/
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid interface name '{name}': must not contain path separators or '..'")

    require_identifier(name, "Interface name")

    # Duplicate check — content-based, over both interface kinds of the layer.
    with _readable_layer(model_root, "interfaces"):
        existing = find_element_file(model_root, "interfaces", name)
    if existing is not None:
        raise ValueError(f"Interface '{name}' already exists at {existing}")

    # A TypeError from iterating a non-list would escape a caller that catches
    # (ValueError, ValidationError).
    if not isinstance(operations, list):
        raise ValueError(f"operations must be a list, got {type(operations).__name__}")

    # Build Pydantic objects via model_validate — Pydantic raises ValidationError on
    # invalid or missing fields (e.g. missing name/type keys, wrong direction value)
    cs_ops = [CSOperation.model_validate(op_dict) for op_dict in operations]
    iface = CSInterface(
        name=name, kind="client_server", operations=cs_ops, description=description, is_service=is_service
    )

    # Operations *and* their arguments: both are SHORT-NAMEs this one call
    # writes, and an argument is the deepest one the client-server shape carries.
    for operation in iface.operations:
        require_identifier(operation.name, f"Interface '{name}' operation name")
        for argument in operation.arguments:
            require_identifier(argument.name, f"Interface '{name}' operation '{operation.name}' argument name")

    # No client-server name derives an init constant, but every interface name is
    # a sibling of every constant in `PortInterfaces`, so this side owes the
    # reverse half of the collision — the writer refuses it either way, and a
    # guard weaker than the writer is what lets an unbuildable project be written.
    check_interface_init_constants(model_root, name, [])

    target_file = _target_path(model_root, "interfaces", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "name": iface.name,
        "kind": iface.kind,
        "operations": [cs_operation_to_dict(op) for op in iface.operations],
    }
    if iface.description:
        data["description"] = iface.description
    # Key for key with the importer's `interface_to_dict`.
    placed(data, iface.package)
    # After `package`, in `model_fields` order, and only when `True`
    # (`omitAtDefault`), so a file without it round-trips byte-identically.
    if iface.is_service:
        data["is_service"] = True
    _write_new_file(
        target_file,
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        layer="interfaces",
        identity=name,
    )

    audit(auditor, "create_cs_interface", {"name": name, "operations": operations}, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# create_requirement
# ---------------------------------------------------------------------------


def create_requirement(
    model_root: str,
    req_id: str,
    title: str,
    description: str,
    req_type: str = "req",
    auditor: Any = None,
    *,
    version: int = 1,
    source: str = "architecture",
) -> str:
    """Create a new requirement YAML file at model_root/requirements/{req_id}.yaml.

    ``version`` and ``source`` are keyword-only: a requirement authored here is
    ``architecture`` at version ``1`` unless the caller says otherwise. The
    requirement names no element; an element names it, through
    :func:`add_trace_link` or its own ``satisfies``.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if req_id is empty or a requirement with that id already exists.
    """
    if not req_id or not req_id.strip():
        raise ValueError("req_id must not be empty")

    # Path-traversal guard
    check_path_segment(req_id, "req_id")

    # Duplicate check — content-based: scan YAML id fields
    with _readable_layer(model_root, "requirements"):
        existing = find_requirement_file(model_root, req_id)
    if existing is not None:
        raise ValueError(f"Requirement '{req_id}' already exists at {existing}")

    # Validate via Pydantic (catches invalid req_type etc.)
    req = Requirement(
        id=req_id,
        title=title,
        description=description,
        req_type=req_type,  # type: ignore[arg-type]  # pydantic: str -> Literal
        version=version,
        source=source,  # type: ignore[arg-type]  # pydantic: str -> Literal
    )

    # The id was refused by content above; the file is chosen at the write.
    target_file = _target_path(model_root, "requirements", req_id)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(requirement_to_dict(req), default_flow_style=False, sort_keys=False),
        layer="requirements",
        identity=req_id,
    )

    audit(
        auditor,
        "create_requirement",
        {"req_id": req_id, "title": title, "req_type": req_type, "version": version, "source": source},
        str(target_file),
    )
    return str(target_file)


# ---------------------------------------------------------------------------
# add_trace_link
# ---------------------------------------------------------------------------


def add_trace_link(
    model_root: str,
    req_id: str,
    swc_name: str,
    auditor: Any = None,
) -> str:
    """Link a component to a requirement it satisfies (idempotent by id).

    Appends ``<req_id>@<version>`` (the requirement's current ``version``) to the
    component's own ``satisfies`` and rewrites the component file through
    :func:`~swcraft_tools.writer.yaml_documents.swc_to_dict`. Nothing is added when
    a member already names ``req_id``, whatever its version: a stale ``@N`` is left
    for ``check-model`` to report, never confirmed in silence, and the audit
    record's ``link`` is that retained member.

    Returns:
        Absolute path string of the updated component file.

    Raises:
        ValueError: if no requirement declares ``req_id``, if two requirement
            files declare it (there is no one version to write; the model is
            ``name.duplicate-requirement``'s finding), if the component is
            not found, or if the requirement's version is too wide to write
            as decimal digits.
    """
    declaring = [
        entry for entry in scan_layer(model_root, "requirements").files if declared_identity(entry.path, "id") == req_id
    ]
    if not declaring:
        raise ValueError(f"Requirement '{req_id}' not found in {model_root}")
    if len(declaring) > 1:
        raise ValueError(
            f"Requirement '{req_id}' is declared by {len(declaring)} files; a link names one requirement by its id"
        )
    req = Requirement.model_validate(load_yaml_document(Path(declaring[0].path).read_text(encoding="utf-8")))

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")
    path = Path(swc_file)
    swc = SWComponent.model_validate(load_yaml_document(path.read_text(encoding="utf-8")))

    # The member naming the id, whatever its version, is the link the file
    # holds, and the one the audit records: a retained stale `@N` is not
    # audited as a confirmation at the current version.
    link = next((member for member in swc.satisfies if parse_link(member).id == req_id), None)
    if link is None:
        try:
            link = f"{req.id}@{req.version}"
        except ValueError:
            # A hexadecimal `version: 0x…` loads as an integer above
            # `sys.get_int_max_str_digits()`: no decimal link can name it.
            raise ValueError(
                f"Requirement '{req_id}' has a version too wide to write as decimal digits; a link cannot name it"
            ) from None
        swc.satisfies.append(link)

    path.write_text(
        yaml.safe_dump(swc_to_dict(swc), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )

    audit(auditor, "add_trace_link", {"req_id": req_id, "swc_name": swc_name, "link": link}, swc_file)
    return swc_file


# ---------------------------------------------------------------------------
# create_composition
# ---------------------------------------------------------------------------


def create_composition(
    model_root: str,
    name: str,
    title: str,
    description: str = "",
    root: bool = False,
    auditor: Any = None,
) -> str:
    """Create a new composition YAML file at model_root/compositions/{name}.yaml.

    The composition is created **empty**: boundary ports, members and delegations
    are edits to an existing element, made through
    :func:`~swcraft_tools.writer.elements.write_element`. *root* is written only
    when ``True`` (``omitAtDefault``); a connector naming no composition belongs to
    the root. Nothing here refuses a second root: ``rule:composition.multiple-roots``
    is ``check-model``'s, over the whole layer.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, contains path-traversal chars, is not a
            legal AUTOSAR ``Identifier``, a composition with that name already
            exists, or a file of the layer does not parse.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard
    check_path_segment(name, "name")

    # A composition name becomes a COMPOSITION-SW-COMPONENT-TYPE SHORT-NAME
    # directly, so it is held to the same rule every other emitted name is.
    require_identifier(name, "Composition name")

    # Duplicate check — content-based.
    with _readable_layer(model_root, "compositions"):
        existing = find_element_file(model_root, "compositions", name)
    if existing is not None:
        raise ValueError(f"Composition '{name}' already exists at {existing}")

    # Validate via Pydantic
    elem = Composition(name=name, title=title, description=description, root=root)

    target_file = _target_path(model_root, "compositions", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(composition_to_dict(elem), default_flow_style=False, sort_keys=False),
        layer="compositions",
        identity=name,
    )

    args: dict[str, Any] = {"name": name, "title": title}
    if root:
        args["root"] = True
    audit(auditor, "create_composition", args, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# create_connector
# ---------------------------------------------------------------------------


def create_connector(
    model_root: str,
    name: str,
    from_swc: str,
    from_port: str,
    to_swc: str,
    to_port: str,
    description: str = "",
    auditor: Any = None,
) -> str:
    """Create a persisted SWC connector at model_root/connectors/{name}.yaml.

    The connector is validated before it is written:
    - from_swc.from_port must be an existing provide port
    - to_swc.to_port must be an existing require port
    - the ``to`` port's interface must be **satisfied by** the ``from`` port's
      (``validator.check_connector_compatibility``, the condition ``check-model``
      and the ARXML writer apply), which abstains where either interface is not declared

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, contains path-traversal chars, is not a legal
            AUTOSAR ``Identifier``, or the connector already exists or endpoint
            validation fails.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard
    check_path_segment(name, "name")

    # A connector name becomes an ASSEMBLY-SW-CONNECTOR SHORT-NAME directly.
    require_identifier(name, "Connector name")

    # Duplicate check — content-based: scan YAML name fields
    with _readable_layer(model_root, "connectors"):
        existing = find_connector_file(model_root, name)
    if existing is not None:
        raise ValueError(f"Connector '{name}' already exists at {existing}")

    connector = Connector(
        name=name,
        **{  # type: ignore[arg-type]  # pydantic: the raw `from`/`to` dicts -> ConnectorEndpoint (`from` is a keyword)
            "from": {"swc": from_swc, "port": from_port},
            "to": {"swc": to_swc, "port": to_port},
        },
        description=description,
    )

    result = check_connector_compatibility(model_root, connector)
    if not result.compatible:
        raise ValueError(result.reason)

    # The name was refused by content above; the file is chosen at the write.
    target_file = _target_path(model_root, "connectors", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(
            connector_to_dict(connector),
            default_flow_style=False,
            sort_keys=False,
        ),
        layer="connectors",
        identity=name,
    )

    audit(
        auditor,
        "create_connector",
        {
            "name": name,
            "from_swc": from_swc,
            "from_port": from_port,
            "to_swc": to_swc,
            "to_port": to_port,
        },
        str(target_file),
    )
    return str(target_file)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def create_task(
    model_root: str,
    name: str,
    period: float,
    priority: int,
    partition: str,
    description: str = "",
    auditor: Any = None,
    wcet: str | None = None,
) -> str:
    """Create a persisted OS task at model_root/tasks/{name}.yaml.

    ``wcet`` is the optional budget as written with its unit (``"1200us"``). ``partition`` names a ``partition``
    declared under ``hardware/``, required with no default; whether it resolves is ``check-model``'s question.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, contains path-traversal chars, a task
            with the same name already exists, or ``priority`` is not an
            ``int`` (``400.0`` included).
        ValidationError: if period/priority/partition/wcet fail Pydantic validation
            (``partition`` is refused when it is blank or not a string).
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard
    check_path_segment(name, "name")

    # Duplicate check — content-based: scan YAML name fields
    with _readable_layer(model_root, "tasks"):
        existing = find_task_file(model_root, name)
    if existing is not None:
        raise ValueError(f"Task '{name}' already exists at {existing}")

    _require_int(priority, "priority")

    task = Task(
        name=name,
        period=period,
        priority=priority,
        partition=partition,
        description=description,
        wcet=wcet,
    )

    # The name was refused by content above; the file is chosen at the write.
    target_file = _target_path(model_root, "tasks", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(task_to_dict(task), default_flow_style=False, sort_keys=False),
        layer="tasks",
        identity=name,
    )

    audit_args: dict = {"name": name, "period": period, "priority": priority, "partition": partition}
    if wcet is not None:
        audit_args["wcet"] = wcet
    audit(auditor, "create_task", audit_args, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# map_runnable_to_task
# ---------------------------------------------------------------------------


def map_runnable_to_task(
    model_root: str,
    task_name: str,
    swc: str,
    runnable: str,
    auditor: Any = None,
) -> str:
    """Append a ``{swc, runnable}`` entry to an existing task's ordered runnable list.

    The entry is validated before it is written:
    - task_name must reference an existing task
    - swc must reference an existing SWC
    - runnable must reference an existing runnable on that SWC
    - the triple ``(swc, None, runnable)`` must not already be written in this
      task or any other task

    **What the guard does not reach:** it compares entries as written and never resolves an occurrence, so it accepts
    an ``swc`` entry for a type placed more than once below the root (``task.instance-ambiguous``) and one naming the
    same occurrence an existing ``instance`` entry names; ``check-model`` reports both.

    Returns:
        Absolute path string of the updated task file.

    Raises:
        ValueError: if any of the above validations fail.
    """
    task_file = find_task_file(model_root, task_name)
    if task_file is None:
        raise ValueError(f"Task '{task_name}' not found in {model_root}")

    swc_file = find_swc_file(model_root, swc)
    if swc_file is None:
        raise ValueError(f"SWC '{swc}' not found in {model_root}")

    swc_data = load_yaml_document(Path(swc_file).read_text(encoding="utf-8"))
    swc_obj = SWComponent.model_validate(swc_data)
    if not any(r.name == runnable for r in swc_obj.runnables):
        raise ValueError(f"Runnable '{runnable}' not found on SWC '{swc}'")

    # The written triple, checked across all tasks, this one included. The
    # comparison is syntactic over ``(swc, None, runnable)``: an ``instance``
    # entry naming the same occurrence is a different triple, judged by
    # ``check-model``'s resolved rules.
    for other_task in read_tasks(model_root):
        for ref in other_task.runnables:
            if (ref.swc, ref.instance, ref.runnable) == (swc, None, runnable):
                raise ValueError(f"Runnable '{swc}.{runnable}' is already mapped to task '{other_task.name}'")

    path = Path(task_file)
    data = load_yaml_document(path.read_text(encoding="utf-8"))
    task = Task.model_validate(data)
    task.runnables.append(TaskRunnableRef(swc=swc, runnable=runnable))

    path.write_text(
        yaml.safe_dump(task_to_dict(task), default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "map_runnable_to_task",
        {"task_name": task_name, "swc": swc, "runnable": runnable},
        str(task_file),
    )
    return str(task_file)


# ---------------------------------------------------------------------------
# create_sequence
# ---------------------------------------------------------------------------


def create_sequence(
    model_root: str,
    name: str,
    task: str,
    steps: list[dict],
    title: str = "",
    auditor: Any = None,
) -> str:
    """Create a persisted execution-sequence scenario at model_root/sequences/{name}.yaml.

    ``steps`` is a list of plain dicts (``{"swc"|"instance": ..., "runnable": ..., "note": ...}``). Refused: an empty,
    path-escaping or taken name; an unknown task; a step whose ``(swc, instance, runnable)`` triple is not written in
    the task's ``runnables`` or does not resolve to a real SWC and runnable; and steps out of the task's order (a
    subset is allowed; no reordering, no repeats).

    **What the guard does not reach:** a step matches the task entry written with the same selector, so a step
    written ``instance: A`` is refused against a task entry written ``swc: <type>`` for the same single occurrence,
    which ``check-model`` accepts; and an ``instance`` step under an undefined composition walk (no root, several, a
    cycle the root reaches) is refused here, where ``check-model`` leaves it to the composition rules.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if any of the above validations fail.
        ValidationError: if a step dict fails SequenceStep validation, or the assembled Sequence fails Pydantic
            validation (e.g. one ``(swc, instance, runnable)`` triple twice within steps).
    """
    # Normalize first, before any reference or order validation.
    step_objs = [SequenceStep.model_validate(s) for s in steps]

    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard
    check_path_segment(name, "name")

    # Duplicate check — content-based: scan YAML name fields
    with _readable_layer(model_root, "sequences"):
        existing = find_sequence_file(model_root, name)
    if existing is not None:
        raise ValueError(f"Sequence '{name}' already exists at {existing}")

    with _readable_layer(model_root, "tasks"):
        task_file = find_task_file(model_root, task)
    if task_file is None:
        raise ValueError(f"Task '{task}' not found in {model_root}")
    task_data = load_yaml_document(Path(task_file).read_text(encoding="utf-8"))
    task_obj = Task.model_validate(task_data)

    swcs = read_swc_model(model_root)
    swc_map = {swc_.name: swc_ for swc_ in swcs}

    # Position of each {swc, instance, runnable} triple in the task's own
    # runnable order. The guard is syntactic: a step matches the
    # task entry that writes the same selector, as ``check-model``'s resolved
    # rules judge the rest.
    task_entry_positions: dict[EntryTriple, int] = {}
    for idx, ref in enumerate(task_obj.runnables):
        task_entry_positions[(ref.swc, ref.instance, ref.runnable)] = idx

    identities = None
    last_position = -1
    for step in step_objs:
        triple = (step.swc, step.instance, step.runnable)
        written = f"{written_selector(step.swc, step.instance)}.{step.runnable}"

        if triple not in task_entry_positions:
            raise ValueError(f"Step '{written}' is not mapped to task '{task}'")

        # Resolve the SWC and runnable against the real model independently:
        # a hand-edited task.yaml can list a mapping whose SWC
        # or runnable no longer exists, even though the triple is nominally
        # present in task.runnables (checked above). An ``instance`` step's
        # component is the type its path resolves to, through the one resolver.
        component = step.swc
        if component is None:
            if identities is None:
                identities = identities_for(model_root)
            component = identities.type_of(None, step.instance)
            if component is None:
                reason = (
                    "which resolves to no component"
                    if identities.defined
                    else f"which is not resolved: {identities.undefined_reason}"
                )
                raise ValueError(f"Sequence step '{written}' names instance '{step.instance}', {reason}")
        find_runnable_ref(swc_map, task, component, step.runnable)

        position = task_entry_positions[triple]
        if position < last_position:
            raise ValueError(
                f"Step order for sequence '{name}' violates task '{task}' "
                f"runnable order: '{written}' appears out "
                "of order"
            )
        last_position = position

    sequence = Sequence(name=name, task=task, title=title, steps=step_objs)

    # The name was refused by content above; the file is chosen at the write.
    target_file = _target_path(model_root, "sequences", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(sequence_to_dict(sequence), default_flow_style=False, sort_keys=False),
        layer="sequences",
        identity=name,
    )

    audit(
        auditor,
        "create_sequence",
        {"name": name, "task": task, "steps": steps, "title": title},
        str(target_file),
    )
    return str(target_file)


# ---------------------------------------------------------------------------
# create_data_type
# ---------------------------------------------------------------------------


def _check_new_data_type_name(model_root: str, name: str, layer: str) -> None:
    """Refuse a data type name that cannot be created in *layer*.

    Refuses an empty name, one that would escape its layer directory, one that is not a legal
    AUTOSAR ``Identifier``, and one a data type of that layer already carries. The duplicate check
    is by content (the YAML ``name`` fields), in the layer of the kind: a base type is an element
    of ``base_types``, an application kind of ``application_types`` and every other kind of
    ``implementation_types``, and each may share its name with a type of another layer.

    Raises:
        ValueError: the name is empty, escapes its layer directory, is not a legal ``Identifier``,
            or a type of *layer* already carries it; or a file of *layer* cannot be read.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path traversal guard — reject any name that would escape its layer directory
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid data type name '{name}': must not contain path separators or '..'")

    require_identifier(name, "Data type name")

    with _readable_layer(model_root, layer):
        existing = (  # the wrapper stays until it is renamed: test_data_description's _CONSUMERS_ON_THE_VIEW
            find_data_type_file(model_root, name)
            if layer == "implementation_types"
            else find_element_file(model_root, layer, name)
        )
    if existing is not None:
        raise ValueError(f"DataType '{name}' already exists at {existing}")


def _base_data_type_arm(
    name: str, kind: str, size: int | None, encoding: str | None, native_declaration: str, description: str
) -> tuple[DataType, dict]:
    """Build the ``base_type`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: ``size`` or ``encoding`` is missing, or the type's model refuses a stated value.
    """
    if size is None:
        raise ValueError("'size' is required for base_type data types")
    if encoding is None:
        raise ValueError("'encoding' is required for base_type data types")
    dt = BaseDataType(
        name=name,
        kind="base_type",
        size=size,
        encoding=encoding,
        native_declaration=native_declaration,
        description=description,
    )
    return dt, {"name": name, "kind": kind, "size": dt.size, "encoding": dt.encoding}


def _value_data_type_arm(
    name: str,
    kind: str,
    base: str | None,
    unit: str,
    value_min: int | float | None,
    value_max: int | float | None,
    compu_method_ref: str | None,
    description: str,
) -> tuple[DataType, dict]:
    """Build the ``value_type`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: ``base`` is missing, or the type's model refuses a stated value.
    """
    if base is None:
        raise ValueError("'base' is required for value_type data types")
    dt = ValueDataType(
        name=name,
        kind="value_type",
        base=base,
        unit=unit,
        value_min=value_min,
        value_max=value_max,
        compu_method_ref=compu_method_ref,
        description=description,
    )
    return dt, {"name": name, "kind": kind, "base": dt.base}


def _reference_data_type_arm(
    name: str, kind: str, base: str | None, compu_method_ref: str | None, description: str
) -> tuple[DataType, dict]:
    """Build the ``reference_type`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: ``base`` is missing, or the type's model refuses a stated value.
    """
    if base is None:
        raise ValueError("'base' is required for reference_type data types")
    dt = ReferenceDataType(
        name=name,
        kind="reference_type",
        base=base,
        compu_method_ref=compu_method_ref,
        description=description,
    )
    return dt, {"name": name, "kind": kind, "base": dt.base}


def _enum_data_type_arm(
    name: str, kind: str, literals: list[dict] | None, compu_method_ref: str | None, description: str
) -> tuple[DataType, dict]:
    """Build the ``enum`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: neither ``literals`` nor ``compu_method_ref`` (a texttable) is given, or the type's model refuses a
            stated value.
    """
    if literals is None and compu_method_ref is None:
        raise ValueError("'literals' or 'compu_method_ref' is required for enum data types")
    dt = EnumDataType(
        name=name,
        kind="enum",
        literals=literals,  # type: ignore[arg-type]  # pydantic: a caller's raw dict -> the field's model
        compu_method_ref=compu_method_ref,
        description=description,
    )
    return dt, {"name": name, "kind": kind, "literals_count": len(dt.literals or ())}


def _record_data_type_arm(name: str, kind: str, fields: list[dict] | None, description: str) -> tuple[DataType, dict]:
    """Build the ``record`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: ``fields`` is missing, or the type's model refuses a stated value.
    """
    if fields is None:
        raise ValueError("'fields' is required for record data types")
    dt = RecordDataType(
        name=name,
        kind="record",
        fields=fields,  # type: ignore[arg-type]  # pydantic: a caller's raw dict -> the field's model
        description=description,
    )
    return dt, {"name": name, "kind": kind, "fields_count": len(dt.fields)}


def _array_data_type_arm(
    name: str, kind: str, element_type: str | None, length: int | None, description: str
) -> tuple[DataType, dict]:
    """Build the ``array`` kind: its model and the audit arguments of its creation.

    Raises:
        ValueError: ``element_type`` or ``length`` is missing, or the type's model refuses a stated value.
    """
    if element_type is None:
        raise ValueError("'element_type' is required for array data types")
    if length is None:
        raise ValueError("'length' is required for array data types")
    dt = ArrayDataType(
        name=name,
        kind="array",
        element_type=element_type,
        length=length,
        description=description,
    )
    return dt, {"name": name, "kind": kind, "element_type": dt.element_type, "length": dt.length}


def create_data_type(
    model_root: str,
    name: str,
    kind: str,
    *,
    # base_type
    size: int | None = None,
    encoding: str | None = None,
    native_declaration: str = "",
    # value_type and reference_type (a reference type takes `base` alone)
    base: str | None = None,
    unit: str = "",
    value_min: int | float | None = None,
    value_max: int | float | None = None,
    # application_type (`unit` above is shared with value_type; an application
    # type names no `base` at all, which is what keeps it out of every cycle)
    conversion: dict | None = None,
    range: dict | None = None,  # the declared field's own name; nothing here calls the builtin
    # enum
    literals: list[dict] | None = None,
    # record
    fields: list[dict] | None = None,
    # array
    element_type: str | None = None,
    length: int | None = None,
    # common
    description: str = "",
    auditor: Any = None,
    # Appended after `auditor`, as new writer parameters are:
    # an application type's three references, and an enumeration's, a value
    # type's and a reference type's compu method.
    unit_ref: str | None = None,
    compu_method_ref: str | None = None,
    data_constr_ref: str | None = None,
) -> str:
    """Create a new data type YAML file in its layer's directory: data_types/implementation/, application/ or base/.

    *kind* is one of ``base_type``, ``value_type``, ``reference_type``, ``application_type``, ``enum``,
    ``record`` or ``array``. Each kind reads its own parameters and ignores the rest. An
    ``application_type`` is the physical view of a signal: its optional ``unit``, linear ``conversion``
    and ``range``, each written only when declared. It names no ``base``.

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty, a data type with that name already exists,
            required fields for the given kind are missing/invalid, the name is
            one the ARXML writer generates for itself (see
            :func:`check_reserved_data_type_name`), or the declared name, one of
            its generated forms or one of its enum literal / record field names
            is not a legal AUTOSAR ``Identifier`` (see
            :func:`check_data_type_identifiers`).
    """
    layer = (
        "base_types"
        if kind == "base_type"
        else "application_types"
        if kind in ("application_type", "enum", "record", "array")
        else "implementation_types"
    )
    _check_new_data_type_name(model_root, name, layer)

    # Dispatch to the kind's arm; Pydantic raises on invalid input
    dt: DataType
    audit_args: dict
    if kind == "base_type":
        dt, audit_args = _base_data_type_arm(name, kind, size, encoding, native_declaration, description)
    elif kind == "value_type":
        dt, audit_args = _value_data_type_arm(
            name, kind, base, unit, value_min, value_max, compu_method_ref, description
        )
    elif kind == "reference_type":
        dt, audit_args = _reference_data_type_arm(name, kind, base, compu_method_ref, description)
    elif kind == "application_type":
        # Nothing is required beyond the name: an application primitive that
        # declares none of the three is what a source export carrying no physics
        # imports as, and each of the three is written only when declared
        # (:func:`~swcraft_tools.writer.yaml_documents.data_type_to_dict`).
        dt = ApplicationDataType(
            name=name,
            kind="application_type",
            unit=unit,
            conversion=conversion,  # type: ignore[arg-type]  # pydantic: a caller's raw dict -> the field's model
            range=range,  # type: ignore[arg-type]  # pydantic: a caller's raw dict -> the field's model
            unit_ref=unit_ref,
            compu_method_ref=compu_method_ref,
            data_constr_ref=data_constr_ref,
            description=description,
        )
        audit_args = {"name": name, "kind": kind, "unit": dt.unit}
    elif kind == "enum":
        dt, audit_args = _enum_data_type_arm(name, kind, literals, compu_method_ref, description)
    elif kind == "record":
        dt, audit_args = _record_data_type_arm(name, kind, fields, description)
    elif kind == "array":
        dt, audit_args = _array_data_type_arm(name, kind, element_type, length, description)
    else:
        raise ValueError(
            f"Unknown data type kind '{kind}': must be one of base_type, value_type, reference_type, "
            "application_type, enum, record, array"
        )

    # Both run after the dispatch, because their rules are kind-scoped and the
    # kind is only trustworthy once Pydantic has accepted the object. The
    # identifier check goes first, like the writer's: an illegal name produces
    # schema-invalid ARXML whatever else is true of it.
    check_data_type_identifiers(dt)
    check_reserved_data_type_name(model_root, dt)

    # The name was refused by content above; a file name another type holds
    # takes a collision suffix.
    target_file = _target_path(model_root, layer, name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(
            data_type_to_dict(dt),
            default_flow_style=False,
            sort_keys=False,
        ),
        layer=layer,
        identity=name,
    )

    audit(auditor, "create_data_type", audit_args, str(target_file))
    return str(target_file)


# ---------------------------------------------------------------------------
# create_mapping_set
# ---------------------------------------------------------------------------


def create_mapping_set(
    model_root: str,
    name: str,
    maps: list[dict] | None = None,
    description: str = "",
    auditor: Any = None,
) -> str:
    """Create a new mapping set YAML file at model_root/data_types/mapping_sets/{name}.yaml.

    Refuses what a single call can see: an empty name, a path-escaping one, a name
    no AUTOSAR ``Identifier`` admits, a name the layer already carries, and
    (through Pydantic) a pair with a blank side or one application type mapped
    twice in one set. Cross-file rules stay ``check-model``'s
    (``mapping-set.unknown-application``, ``.unknown-implementation``,
    ``data-type.application-type-unmapped``), so a set whose pairs name nothing is
    written. No generated form lengthens a set's ``SHORT-NAME``, and a set is
    emitted into ``/DataTypes/MappingSets``, so it may share a data type's name.
    An empty ``maps`` is written as an empty list (no ``omitAtDefault``).

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: on any of the refusals above, or if a file of the layer does
            not parse.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard
    check_path_segment(name, "name")

    # A set's name becomes a DATA-TYPE-MAPPING-SET SHORT-NAME directly, so it is
    # held to the same rule every other emitted name is.
    require_identifier(name, "Mapping set name")

    # Duplicate check — content-based: the identity is what a file DECLARES, not
    # what its name spells.
    with _readable_layer(model_root, "mapping_sets"):
        existing = find_element_file(model_root, "mapping_sets", name)
    if existing is not None:
        raise ValueError(f"Mapping set '{name}' already exists at {existing}")

    mapping_set = MappingSet(name=name, maps=maps or [], description=description)  # type: ignore[arg-type]  # pydantic: a caller's raw dict -> the field's model

    # The name was refused by content above; a file name another element holds
    # takes a collision suffix.
    target_file = _target_path(model_root, "mapping_sets", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    _write_new_file(
        target_file,
        yaml.safe_dump(mapping_set_to_dict(mapping_set), default_flow_style=False, sort_keys=False),
        layer="mapping_sets",
        identity=name,
    )

    audit(
        auditor,
        "create_mapping_set",
        {"name": name, "maps_count": len(mapping_set.maps)},
        str(target_file),
    )
    return str(target_file)


# ---------------------------------------------------------------------------
# create_hardware_element
# ---------------------------------------------------------------------------

#: The four hardware kinds :func:`create_hardware_element` dispatches on, in the
#: order its unknown-kind message names them.
_HARDWARE_KINDS: tuple[str, ...] = ("processor", "core", "memory", "partition")


#: Which hardware kinds carry each optional field of
#: :func:`create_hardware_element`, in the order :func:`_refuse_foreign_fields`
#: checks them, and the kinds' order its message names them in. Not derived from
#: the schema classes: the message is a sentence about the model, and
#: ``threshold`` is carried by two kinds, which a per-class derivation could not
#: say.
_HARDWARE_FIELD_OWNER: dict[str, tuple[str, ...]] = {
    "threshold": ("core", "memory"),
    "enforcement": ("core", "memory"),
    "hw_id": ("core",),
    "memory_type": ("memory",),
    "size": ("memory",),
    "accessible": ("memory",),
    "core": ("partition",),
    "asil": ("partition",),
    "trust": ("partition",),
    "coexistence": ("partition",),
}


def _refuse_foreign_fields(kind: str, **fields: Any) -> None:
    """Raise ValueError for the first non-``None`` field *kind* does not carry.

    The message names the kinds that do carry the field, since a field shared by
    two kinds has no single kind to suggest. Which kind carries which field is
    :data:`_HARDWARE_FIELD_OWNER`'s; a field the caller omits reads as ``None``.
    """
    for field_name, owners in _HARDWARE_FIELD_OWNER.items():
        if fields.get(field_name) is None or kind in owners:
            continue
        raise ValueError(f"'{field_name}' is not valid for a {kind} (it is a {' and '.join(owners)} field)")


def create_hardware_element(
    model_root: str,
    name: str,
    kind: str,
    *,
    # core and memory
    threshold: float | None = None,
    enforcement: str | None = None,
    # core
    hw_id: int | None = None,
    # memory
    memory_type: str | None = None,
    size: int | None = None,
    accessible: list[str] | None = None,
    # common
    description: str = "",
    auditor: Any = None,
    # partition: keyword-only, and appended after `auditor` all the same
    core: str | None = None,
    asil: str | None = None,
    coexistence: str | None = None,
    # partition, appended, never inserted
    trust: str | None = None,
) -> str:
    """Create a hardware element YAML file at model_root/hardware/{name}.yaml.

    Dispatches by ``kind``: ``processor`` takes a description only, ``core`` requires ``threshold``, ``memory``
    requires ``memory_type`` (the ``type`` field), ``size``, ``accessible`` and ``threshold``, and ``partition``
    requires ``core`` (the core it is on) and takes ``asil``, ``coexistence`` and ``trust`` (omitted means
    ``non_trusted``). Another kind's argument is **rejected, not ignored**. Not checked here, because they span files:
    exactly one ``processor``, and every ``accessible`` name and a partition's ``core`` resolving to a declared core
    (``check-model``'s ``hardware.*`` rules).

    Returns:
        Absolute path string of the created file.

    Raises:
        ValueError: if name is empty or contains path-traversal characters, a
            hardware element with that name already exists, the kind is unknown,
            a kind-specific field is missing or is not an ``int``, or another
            kind's field was passed.
        ValidationError: if any field fails Pydantic validation.
    """
    if not name or not name.strip():
        raise ValueError("name must not be empty")

    # Path-traversal guard — reject any name that would escape hardware/
    check_path_segment(name, "name")

    # Duplicate check — content-based: scan YAML name fields. One identity
    # namespace spans all four kinds, which is why this is not per-kind: a
    # `core` and a `memory` both named RAM0 could not both be referenced, and
    # `hardware.duplicate-name` reports the pair whichever kinds they are.
    with _readable_layer(model_root, "hardware"):
        existing = find_hardware_file(model_root, name)
    if existing is not None:
        raise ValueError(f"Hardware element '{name}' already exists at {existing}")

    if kind not in _HARDWARE_KINDS:
        raise ValueError(f"Unknown hardware kind '{kind}': must be one of {', '.join(_HARDWARE_KINDS)}")
    _refuse_foreign_fields(
        kind,
        threshold=threshold,
        enforcement=enforcement,
        hw_id=hw_id,
        memory_type=memory_type,
        size=size,
        accessible=accessible,
        core=core,
        asil=asil,
        trust=trust,
        coexistence=coexistence,
    )

    element: Hardware
    if kind == "processor":
        element = Processor(name=name, kind="processor", description=description)
    elif kind == "core":
        if threshold is None:
            raise ValueError("'threshold' is required for a core")
        if hw_id is not None:
            _require_int(hw_id, "hw_id")
        element = Core(
            name=name,
            kind="core",
            threshold=threshold,
            **({"enforcement": enforcement} if enforcement is not None else {}),  # type: ignore[arg-type]  # pydantic: str -> Literal
            hw_id=hw_id,
            description=description,
        )
    elif kind == "memory":
        if memory_type is None:
            raise ValueError("'memory_type' is required for a memory")
        if size is None:
            raise ValueError("'size' is required for a memory")
        if accessible is None:
            raise ValueError("'accessible' is required for a memory")
        if threshold is None:
            raise ValueError("'threshold' is required for a memory")
        _require_int(size, "size")
        element = Memory(
            name=name,
            kind="memory",
            type=memory_type,  # type: ignore[arg-type]  # pydantic: str -> Literal
            size=size,
            accessible=accessible,
            threshold=threshold,
            **({"enforcement": enforcement} if enforcement is not None else {}),  # type: ignore[arg-type]  # pydantic: str -> Literal
            description=description,
        )
    else:  # partition
        if core is None:
            raise ValueError("'core' is required for a partition")
        element = Partition(
            name=name,
            kind="partition",
            core=core,
            asil=asil,  # type: ignore[arg-type]  # pydantic: str -> Literal
            **({"trust": trust} if trust is not None else {}),  # type: ignore[arg-type]  # pydantic: str -> Literal
            coexistence=coexistence if coexistence is not None else "",
            description=description,
        )

    # The name was refused by content above; the file is chosen at the write.
    target_file = _target_path(model_root, "hardware", name)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    _write_new_file(
        target_file,
        yaml.safe_dump(
            hardware_to_dict(element),
            default_flow_style=False,
            sort_keys=False,
        ),
        layer="hardware",
        identity=name,
    )

    audit_args: dict = {"name": name, "kind": kind}
    if kind in _HARDWARE_FIELD_OWNER["threshold"]:
        audit_args["threshold"] = threshold
    if kind == "partition":
        audit_args["core"] = core
        if asil is not None:
            audit_args["asil"] = asil
        if trust is not None:
            audit_args["trust"] = trust
        if coexistence:
            audit_args["coexistence"] = coexistence
    if kind == "memory":
        audit_args["type"] = memory_type
        audit_args["size"] = size
        audit_args["accessible"] = accessible
    audit(auditor, "create_hardware_element", audit_args, str(target_file))
    return str(target_file)


def update_resource(
    model_root: str,
    name: str,
    *,
    threshold: float | None = None,
    enforcement: str | None = None,
    size: int | None = None,
    description: str | None = None,
    auditor: Any = None,
) -> str:
    """Update the *limit* fields of an existing hardware element in place.

    Only ``threshold``, ``enforcement``, ``size`` and ``description`` change. A ``processor`` and a ``partition``
    carry no budget, so only ``description`` applies to them; the others are refused rather than ignored. An argument
    left ``None`` is unchanged; ``description=""`` clears it.

    Returns:
        Absolute path string of the updated file.

    Raises:
        ValueError: if the element does not exist, no field was given to update,
            a field was passed that the element's kind does not carry, ``size``
            is not an ``int``, or the result fails ``schema`` validation.
    """
    if threshold is None and enforcement is None and size is None and description is None:
        raise ValueError("update_resource needs at least one of threshold, enforcement, size, description")

    element_file = find_hardware_file(model_root, name)
    if element_file is None:
        raise ValueError(f"Hardware element '{name}' not found in {model_root}")

    # read_hardware re-validates and names the offending file on failure, so a
    # corrupted sibling is reported the same way every other reader reports it.
    element = next(e for e in read_hardware(model_root) if e.name == name)

    if size is not None and not isinstance(element, Memory):
        raise ValueError(f"'size' is not valid for {element.kind} '{name}' (only a memory declares one)")
    if isinstance(element, Processor | Partition):
        for field_name, value in (("threshold", threshold), ("enforcement", enforcement)):
            if value is not None:
                raise ValueError(
                    f"'{field_name}' is not valid for {element.kind} '{name}' (a {element.kind} carries no budget)"
                )
    if size is not None:
        # The guard `create_hardware_element` applies, so both entry points agree;
        # after the kind-mismatch check, so `size=` on a core reports the mismatch.
        _require_int(size, "size")

    overrides: dict[str, Any] = {}
    if threshold is not None:
        overrides["threshold"] = threshold
    if enforcement is not None:
        overrides["enforcement"] = enforcement
    if size is not None:
        overrides["size"] = size
    if description is not None:
        overrides["description"] = description

    merged = {**hardware_to_dict(element), **overrides}
    updated: Hardware = type(element).model_validate(merged)

    path = Path(element_file)
    path.write_text(
        yaml.safe_dump(
            hardware_to_dict(updated),
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    audit(auditor, "update_resource", {"name": name, **overrides}, element_file)
    return element_file


# ---------------------------------------------------------------------------
# Budget setters
# ---------------------------------------------------------------------------


def set_task_wcet(
    model_root: str,
    task_name: str,
    wcet: str,
    auditor: Any = None,
) -> str:
    """Set (or overwrite) the execution-time budget of an existing task; ``wcet`` carries its unit (``"1200us"``).

    Returns:
        Absolute path string of the updated task file.

    Raises:
        ValueError: if the task does not exist, or the resulting task fails ``schema.Task`` validation.
    """
    task_file = _require_task_file(model_root, task_name)
    return _rewrite_task_field(task_file, task_name, "wcet", wcet, "set_task_wcet", auditor)


def set_task_partition(
    model_root: str,
    task_name: str,
    partition: str,
    auditor: Any = None,
) -> str:
    """Move an existing task to another partition, named as a declared ``kind: partition`` ``hardware`` element.

    :func:`create_task` has no such guard; ``check-model`` refuses both states.

    Returns:
        Absolute path string of the updated task file.

    Raises:
        ValueError: if the task does not exist, ``partition`` does not name a declared ``kind: partition`` element
            (the message names the declared ones), or the resulting task fails ``schema.Task`` validation.
    """
    # Resolved before the declared-partition guard so an unknown task keeps
    # reporting as an unknown task, whichever partition it was being moved to.
    task_file = _require_task_file(model_root, task_name)

    declared = sorted({element.name for element in read_hardware(model_root) if isinstance(element, Partition)})
    if partition not in declared:
        raise ValueError(
            f"'{partition}' is not declared as a 'kind: partition' element in {model_root}/hardware — "
            f"declared partitions are {declared}"
        )

    return _rewrite_task_field(task_file, task_name, "partition", partition, "set_task_partition", auditor)


def set_task_priority(
    model_root: str,
    task_name: str,
    priority: int,
    auditor: Any = None,
) -> str:
    """Set an existing task's scheduling priority (higher number = more urgent); no rate-monotonic guard.

    Returns:
        Absolute path string of the updated task file.

    Raises:
        ValueError: if the task does not exist, ``priority`` is not an ``int`` (``5.0`` included), or the resulting
            task fails ``schema.Task`` validation.
    """
    _require_int(priority, "priority")
    task_file = _require_task_file(model_root, task_name)
    return _rewrite_task_field(task_file, task_name, "priority", priority, "set_task_priority", auditor)


def set_swc_memory_budget(
    model_root: str,
    swc_name: str,
    pool: str,
    size_bytes: int,
    auditor: Any = None,
) -> str:
    """Set (or overwrite) one memory-pool budget on an existing SWC.

    ``pool`` must name a declared ``kind: memory`` element
    (:func:`_require_declared_memory_pools`, shared with :func:`create_swc`). The
    SWC's other pools are preserved. The non-negativity rule stays
    ``SWComponent.memory_budgets_must_be_named_and_non_negative``'s; only the
    int-ness guard is explicit, since Pydantic's lax mode would accept ``2048.0``.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if the SWC does not exist, the pool is blank or not a
            declared memory, ``size_bytes`` is not an ``int``, or the
            resulting SWC fails ``schema.SWComponent`` validation (a
            ``pydantic.ValidationError``, itself a ``ValueError``).
    """
    _require_int(size_bytes, "size_bytes")
    _require_declared_memory_pools(model_root, [pool])

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    swc = SWComponent.model_validate(load_yaml_document(path.read_text(encoding="utf-8")))
    swc = SWComponent.model_validate({**swc_to_dict(swc), "memory": {**swc.memory, pool: size_bytes}})

    path.write_text(
        yaml.safe_dump(swc_to_dict(swc), default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "set_swc_memory_budget",
        {"swc_name": swc_name, "pool": pool, "size_bytes": size_bytes},
        swc_file,
    )
    return swc_file


def unset_swc_memory_budget(
    model_root: str,
    swc_name: str,
    pool: str,
    auditor: Any = None,
) -> str:
    """Remove one memory-pool budget from an existing SWC, the inverse of :func:`set_swc_memory_budget`.

    Unlike its setter it does not require the pool to be a declared memory, so a
    budget left behind by a deleted memory can still be removed. Removing a budget
    the SWC does not carry is an error, so a mistyped pool cannot read as success.

    Returns:
        Absolute path string of the updated SWC file.

    Raises:
        ValueError: if the SWC does not exist, the pool is blank, or the SWC
            declares no budget for that pool.
    """
    _require_pool_name(pool)

    swc_file = find_swc_file(model_root, swc_name)
    if swc_file is None:
        raise ValueError(f"SWC '{swc_name}' not found in {model_root}")

    path = Path(swc_file)
    swc = SWComponent.model_validate(load_yaml_document(path.read_text(encoding="utf-8")))
    if pool not in swc.memory:
        raise ValueError(f"SWC '{swc_name}' declares no memory budget for pool '{pool}'")

    remaining = {name: size for name, size in swc.memory.items() if name != pool}
    swc = SWComponent.model_validate({**swc_to_dict(swc), "memory": remaining})

    # `swc_to_dict` omits an empty `memory` mapping, so removing the last pool
    # leaves no `memory: {}` behind.
    path.write_text(
        yaml.safe_dump(swc_to_dict(swc), default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(
        auditor,
        "unset_swc_memory_budget",
        {"swc_name": swc_name, "pool": pool},
        swc_file,
    )
    return swc_file


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_task_file(model_root: str, task_name: str) -> str:
    """Resolve *task_name* to its YAML file, or raise ValueError."""
    task_file = find_task_file(model_root, task_name)
    if task_file is None:
        raise ValueError(f"Task '{task_name}' not found in {model_root}")
    return task_file


def _rewrite_task_field(
    task_file: str,
    task_name: str,
    field: str,
    value: Any,
    operation: str,
    auditor: Any,
) -> str:
    """Re-validate the task at *task_file* with *field* replaced, write, audit.

    The shared body of :func:`set_task_wcet`, :func:`set_task_partition` and
    :func:`set_task_priority`. The round trip through ``Task.model_validate`` is
    load-bearing: Pydantic v2 does not validate on assignment. It takes the
    resolved *task_file* so each caller orders its guards and the existence check
    as its messages need.
    """
    path = Path(task_file)
    task = Task.model_validate(load_yaml_document(path.read_text(encoding="utf-8")))
    task = Task.model_validate({**task_to_dict(task), field: value})

    path.write_text(
        yaml.safe_dump(task_to_dict(task), default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )

    audit(auditor, operation, {"task_name": task_name, field: value}, task_file)
    return task_file


def find_runnable_ref(swc_map: dict[str, SWComponent], owner_name: str, swc_name: str, runnable_name: str) -> Runnable:
    """Resolve a sequence step's runnable by SWC and runnable name, naming the task it is mapped to."""
    swc = swc_map.get(swc_name)
    if swc is None:
        raise ValueError(f"Task '{owner_name}' references unknown SWC '{swc_name}'")
    for runnable in swc.runnables:
        if runnable.name == runnable_name:
            return runnable
    raise ValueError(f"Task '{owner_name}' references unknown runnable '{runnable_name}' on SWC '{swc_name}'")


# ---------------------------------------------------------------------------
# The data-received event's pair guard.
# ---------------------------------------------------------------------------


def check_data_event_identifier(swc_name: str, runnable_name: str, port: str, element: str, emitted: str) -> None:
    """Check ``DRT_{port}_{element}``, the data-received event's pair-named form.

    As :func:`check_operation_event_identifier`: *emitted* is the **final** name, and an illegal port or element name
    is not charged here.

    Raises:
        ValueError: naming the component, the runnable, the pair, the emitted form and the joint budget, if the
            emitted form is not a legal ``Identifier``.
    """
    _check_pair_event_identifier(
        swc_name, runnable_name, port, element, emitted, "data-received event", "DATA-RECEIVED-EVENT"
    )


# ---------------------------------------------------------------------------
# The call-returned event's pair guard.
# ---------------------------------------------------------------------------


def check_call_returned_event_identifier(
    swc_name: str, runnable_name: str, port: str, operation: str, emitted: str
) -> None:
    """Check ``ASRT_{port}_{operation}``, the call-returned event's pair-named form.

    As :func:`check_operation_event_identifier`: *emitted* is the **final** name, and an illegal port or operation
    name is not charged here.

    Raises:
        ValueError: naming the component, the runnable, the pair, the emitted form and the joint budget, if the
            emitted form is not a legal ``Identifier``.
    """
    _check_pair_event_identifier(
        swc_name,
        runnable_name,
        port,
        operation,
        emitted,
        "call-returned event",
        "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT",
    )


def _check_pair_event_identifier(
    swc_name: str, runnable_name: str, port: str, member: str, emitted: str, words: str, element: str
) -> None:
    """Check *emitted* against *port* and *member* jointly: the one body of the three pair-named event guards.

    The overhead is read off the **final** name, so a collision suffix is charged
    to the pair; a port or member name that is itself illegal is not charged here
    -- one root cause, one offender. *words* names the event kind in the message,
    *element* the ARXML element it is the ``SHORT-NAME`` of.
    """
    if check_identifier(port) is not None or check_identifier(member) is not None:
        return
    _check_generated_pair_identifier(
        port,
        member,
        emitted,
        len(emitted) - len(port) - len(member),
        f"SWC '{swc_name}' runnable '{runnable_name}' {words} '{port}'.'{member}' generated {element} name",
    )
