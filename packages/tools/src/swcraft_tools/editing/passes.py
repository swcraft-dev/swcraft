"""The detection passes of a change set, and the constructors of every protocol violation.

===== ======================================= =========================================================
Pass  What it checks                          Codes
===== ======================================= =========================================================
A     The request against the protocol shapes ``schema-violation``
B     Each address and payload, in order      ``unknown-field`` ``immutable-field``
                                              ``identity-conflict`` ``schema-violation``
C     The operations pass B could not apply   ``partial-change-set`` ``broken-reference``
D     The assembled, materialized result      ``invalid-identifier`` ``broken-reference``
                                              ``consistency-violation`` ``schema-violation``
===== ======================================= =========================================================

**A** checks every request shape: the envelope fail-fast, then every malformed
change-set operation in one round trip. **B** applies the operations in memory, in
order, recording every refusal. **C** answers ``partial-change-set`` (never a
partial commit or a silent reordering) when a *later* operation would have
supplied the missing target, and ``broken-reference`` otherwise. **D** waives
nothing: a change set is accepted only when the whole staged result is valid, and
it may repair a pre-existing violation. D's ``broken-reference`` is a dangling
reference in the result, addressed at the referrers; C's is an address resolving
against nothing.

The other constructors code conditions outside the passes (a ``read`` miss, a writer
refusal, an ambiguous baseline, the seam's arguments) with the code a pass gives the same condition.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    FieldStep,
    KeyStep,
    MemberStep,
    Miss,
    MissReason,
    attribute_for,
    identity_key,
    is_address_prefix,
    member_identity_of,
)
from swcraft_tools.editing.apply import (
    IDENTITY_CONFLICT,
    UNKNOWN_FIELD,
    ApplyOutcome,
    InapplicableOperation,
    apply_operations,
)
from swcraft_tools.editing.diagnosis import (
    CONSISTENCY_VIOLATION,
    UndiagnosedInconsistency,
    diagnose,
)
from swcraft_tools.editing.protocol import (
    OVER_BUDGET,
    ChangeOperation,
    ChangeSetRequest,
    CreateOperation,
    DiscoverRequest,
    EnvelopeError,
    ErrorResult,
    Hint,
    Measured,
    ProtocolError,
    Request,
    UpdateOperation,
    Violation,
    parse_change_operation,
    parse_change_set_envelope,
    parse_find_request,
    parse_read_request,
    request_operation,
)
from swcraft_tools.editing.resolve import Role, reference_candidates
from swcraft_tools.model_state import single_read
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import ConsistencyIssue, ConsistencyResult, check_cross_layer_consistency

__all__ = [
    "BROKEN_REFERENCE",
    "MISS_CODES",
    "OVER_BUDGET",
    "PARTIAL_CHANGE_SET",
    "SCHEMA_VIOLATION",
    "ambiguous_baseline_violations",
    "ambiguous_member_violations",
    "malformed_scope_violation",
    "miss_violation",
    "over_budget_violation",
    "pass_a",
    "pass_b",
    "pass_c",
    "pass_d",
    "pass_d_findings",
    "placement_violation",
    "reserved_name_violation",
    "retired_storage_violation",
    "summary_not_derivable_violation",
    "target_occupied_violation",
    "target_vanished_violation",
    "undeclared_layer_violation",
    "unopenable_project_violation",
    "unparseable_element_violation",
    "unreadable_model_root_violation",
]

#: The one code pass A can produce.
SCHEMA_VIOLATION = "schema-violation"

# `OVER_BUDGET` (re-exported, no `#:` comment: one with no assignment after it would
# document the next statement) is the read budget's refusal, raised by no pass. It is
# imported, not respelled, because `Violation.__post_init__` keys `hints`/`measured` on it.

#: The two codes pass C can produce, and the whole of its vocabulary.
PARTIAL_CHANGE_SET = "partial-change-set"
BROKEN_REFERENCE = "broken-reference"


def pass_a(request: Any) -> Request:
    """Pass A: *request* checked against the protocol shapes, and its parsed form.

    Args:
        request: the request exactly as it arrived — plain JSON data, never a
            file path and never raw YAML.

    Returns:
        The parsed ``discover``, ``read``, ``find`` or
        :class:`~swcraft_tools.editing.protocol.ChangeSetRequest` form.

    Raises:
        ProtocolError: carrying a ``schema-violation`` with no ``addresses``: the
            value that would have been one is what the pass refused.
    """
    try:
        operation = request_operation(request)
    except EnvelopeError as exc:
        raise _refusal(exc) from exc

    if operation == "change_set":
        return _pass_a_change_set(request)

    parsers = {"discover": DiscoverRequest.from_json, "find": parse_find_request}
    parser = parsers.get(operation, parse_read_request)
    try:
        return parser(request)
    except EnvelopeError as exc:
        raise _refusal(exc) from exc


def _pass_a_change_set(request: Any) -> ChangeSetRequest:
    """The change-set envelope fail-fast, then every operation in it.

    The operations are walked, and their violations collected, only once
    ``operations`` is known to be a non-empty array.
    """
    try:
        baseline, intent, raw_operations, complete = parse_change_set_envelope(request)
    except EnvelopeError as exc:
        raise _refusal(exc) from exc

    operations = []
    violations: list[Violation] = []
    for position, raw in enumerate(raw_operations):
        try:
            operations.append(parse_change_operation(raw, position))
        except EnvelopeError as exc:
            violations.append(Violation(error=SCHEMA_VIOLATION, message=str(exc)))
    if violations:
        raise ProtocolError(ErrorResult(primary=violations[0], additional=tuple(violations[1:])))
    return ChangeSetRequest(baseline=baseline, intent=intent, operations=tuple(operations), complete=complete)


def pass_b(
    operations: Sequence[ChangeOperation],
    snapshot: ModelSnapshot,
    *,
    registry: reg.Registry | None = None,
) -> ApplyOutcome:
    """Pass B: *operations* bound against the registry and applied to *snapshot*, in memory.

    Returns the :class:`~swcraft_tools.editing.apply.ApplyOutcome`: the staged tree,
    the pass-B violations in operation order, the operations pass C must classify,
    and the elements whose value moved. *snapshot* is never mutated, and nothing is
    raised: pass C's violations join the same result, so the engine builds it.
    """
    registry = reg.or_current(registry)
    return apply_operations(snapshot, operations, registry=registry)


def pass_c(
    operations: Sequence[ChangeOperation],
    inapplicable: Sequence[InapplicableOperation],
) -> tuple[Violation, ...]:
    """Pass C: every operation pass B could not apply, classified in operation order.

    Args:
        operations: the whole change set in the client's order; the question is
            about the operations *after* each miss.
        inapplicable: what pass B recorded, in operation order.

    Returns:
        One violation per inapplicable operation — ``partial-change-set`` where a
        later operation would have supplied the missing target,
        ``broken-reference`` where nothing would — every one, never only the first.
        Each carries the address the operation named (what the client must change);
        the absent prefix is named in the message.
    """
    violations: list[Violation] = []
    for miss in inapplicable:
        supplier = _later_supplier(operations, miss)
        if supplier is None:
            violations.append(
                Violation(
                    error=BROKEN_REFERENCE,
                    message=(
                        f"the address does not resolve: nothing in the model or in this change set supplies "
                        f"{_render(miss.missing)}. {miss.message}"
                    ),
                    addresses=(miss.address,),
                )
            )
            continue
        position, operation = supplier
        violations.append(
            Violation(
                error=PARTIAL_CHANGE_SET,
                message=(
                    f"this change set is not applicable as a whole: operation {miss.position} names "
                    f"{_render(miss.missing)}, which operation {position} ({operation.operation}) supplies "
                    "later in the same set. Nothing dangles — the target is here, only after the operation "
                    "that needs it — so this is a refusal about the order: never a partial commit, and never "
                    "a silent reordering, because reordering on your behalf would commit a sequence nobody "
                    "reviewed. Put the supplying operation first, or fold the change into its payload. "
                    "Whether the reordered set is *valid* is a separate question this pass has not asked: "
                    "the assembled result is validated after the operations apply, so a reordered "
                    "re-submission can still be refused for what it leaves the model looking like"
                ),
                addresses=(miss.address,),
            )
        )
    return tuple(violations)


def _later_supplier(
    operations: Sequence[ChangeOperation],
    miss: InapplicableOperation,
) -> tuple[int, ChangeOperation] | None:
    """The first operation after *miss* that would have supplied its target, or ``None``.

    A ``create`` at the target or a prefix of it, or an ``update`` at a **strict**
    prefix; an ``update`` at the target needs it there already, and a ``remove``
    supplies nothing.
    """
    for position in range(miss.position + 1, len(operations)):
        operation = operations[position]
        if isinstance(operation, CreateOperation) and is_address_prefix(operation.address, miss.missing):
            return position, operation
        if (
            isinstance(operation, UpdateOperation)
            and operation.address != miss.missing
            and is_address_prefix(operation.address, miss.missing)
        ):
            return position, operation
    return None


def pass_d(model_root: str, arxml_version: str | None = None) -> tuple[Violation, ...]:
    """Pass D: the materialized staged result validated, with which code and where.

    *model_root* is the **shadow's** model root; *arxml_version* ``None`` means
    4.2.2. Returns the violations in ``RULE_CODES`` order, empty when valid
    (warnings never refuse); raises :class:`UndiagnosedInconsistency` for a rule
    ``RULE_CODES`` does not name.
    """
    return pass_d_findings(model_root, arxml_version)[0]


def pass_d_findings(
    model_root: str, arxml_version: str | None = None
) -> tuple[tuple[Violation, ...], tuple[ConsistencyIssue, ...]]:
    """:func:`pass_d`'s violations, and the error findings they were translated from.

    The findings decide ``complete: true`` (the server completes a change set only
    when every error finding is one it can repair); one validation run answers both.
    Both are empty for a valid result.

    One :func:`~swcraft_tools.model_state.single_read` block covers the whole
    function, so each staged layer is read once; nothing writes the shadow tree
    meanwhile, so its memo holds only the staged result. **What this does not
    reach:** the repair's re-read of four staged layers, the baseline snapshot,
    ``materialize`` and the commit, which run with no block open.
    """
    with single_read():
        consistency = check_cross_layer_consistency(model_root, arxml_version)
        if consistency.consistent:
            return (), ()

        violations = diagnose(consistency, _nearest_names(model_root))
        if not violations:
            raise UndiagnosedInconsistency(
                "check_cross_layer_consistency refused the staged result and none of its findings carries a rule the "
                "editing protocol maps to a code. That is an engine/validator disagreement — not a client mistake — so "
                "it is raised rather than answered with a protocol code; `diagnosis.RULE_CODES` is held complete by "
                "`packages/tools/tests/test_validator_rule_coverage.py`. The findings, by rule and model place: "
                f"{_unmapped_findings(consistency)}. The validator's own issues, as a diagnostic and never as "
                f"contract: {consistency.issues}"
            )
        return violations, consistency.records


def _nearest_names(model_root: str) -> Callable[[str, str, tuple[Role, ...]], str]:
    """``diagnose``'s ``suggest``: the nearest names to a dangling target, over the staged model.

    The staged snapshot is loaded on the first broken reference only.
    """
    loaded: list[Any] = []

    def suggest(group: str, target: str, roles: tuple[Role, ...]) -> str:
        if not loaded:
            loaded.append(load_snapshot(str(model_root)))
        return reference_candidates(loaded[0], group, target, reg.or_current(None), roles)

    return suggest


def ambiguous_baseline_violations(
    snapshot: ModelSnapshot,
    *,
    registry: reg.Registry | None = None,
) -> tuple[Violation, ...]:
    """Every identity the **baseline** holds more than once, as ``consistency-violation``.

    Asked before pass B binds anything: an address is ``{layer, element}``, so where
    two files declare the same identity every operation over it is a guess (the
    last-wins index once made a ``remove`` a no-op success). No change set can repair
    an unaddressable duplicate and pass D refuses every outcome holding it, so this
    only names the reason earlier, at the ambiguous identity. The code is pass D's
    for its ``name.duplicate-*`` rules: one condition, one code.
    """
    registry = reg.or_current(registry)
    violations: list[Violation] = []
    # By layer name off `ModelSnapshot.state`, not the adapter attribute: a layer
    # with no `snapshotAttributes` row has no attribute, and its duplicates would go unreported.
    state = snapshot.state
    for layer in registry.layers:
        counts: Counter[str] = Counter()
        for element in state[layer.name] if layer.name in state else ():
            identity = getattr(element, layer.identity_field, None)
            if isinstance(identity, str):
                counts[identity] += 1
        for identity, count in counts.items():
            if count < 2:
                continue
            violations.append(
                Violation(
                    error=CONSISTENCY_VIOLATION,
                    message=(
                        f"the baseline holds {count} elements of layer '{layer.name}' with the identity "
                        f"'{identity}', so that address names more than one of them and no operation over it can "
                        "say which it moved. Resolve the duplicate in the model before editing through the "
                        "protocol: an address is {layer, element} and has no way to pick between them"
                    ),
                    addresses=(Address(layer.name, identity),),
                )
            )
    return tuple(violations)


def ambiguous_member_violations(
    snapshot: ModelSnapshot,
    operations: Sequence[ChangeOperation],
    *,
    registry: reg.Registry | None = None,
) -> tuple[Violation, ...]:
    """:func:`ambiguous_baseline_violations` one level down: an ambiguous **member**.

    The schema does not enforce member-identity uniqueness for the ``field``
    collections, so a file with two ports named ``pOut`` parses, and a member step
    naming one would silently pick the first.

    Unlike the element check, the scope is the elements the operations reach into
    (every operation carrying a ``path``): a duplicate member corrupts nothing
    outside its element. A ``create`` or ``remove`` at a bare ``{layer, element}``
    is left alone on purpose — removing the holding element is the only change set
    pass D can accept for such a model. ``whole_value`` collections are exempt
    (their members are counted and may repeat) and a ``map`` needs no check (keys
    are unique). The code is ``consistency-violation``, as for the element check.
    """
    registry = reg.or_current(registry)
    violations: list[Violation] = []
    seen: set[tuple[str, str]] = set()
    # By layer name off the state, for `ambiguous_baseline_violations`' reason.
    state = snapshot.state
    for operation in operations:
        address = operation.address
        if not address.path or address.element is None:
            continue
        if (address.layer, address.element) in seen:
            continue
        seen.add((address.layer, address.element))
        try:
            layer = registry.layer(address.layer)
        except KeyError:
            continue  # an undeclared layer is pass B's `unknown-field`, not this
        element = next(
            (
                candidate
                for candidate in (state[layer.name] if layer.name in state else ())
                if getattr(candidate, layer.identity_field, None) == address.element
            ),
            None,
        )
        kind = None if element is None else registry.kind_of_model(type(element))
        if kind is None:
            continue  # an absent element is pass C's miss; the walk needs a value
        violations.extend(_duplicate_members(element, kind, Address(address.layer, address.element), registry))
    return tuple(violations)


def _duplicate_members(
    value: Any,
    kind: reg.ElementKindDescriptor,
    at: Address,
    registry: reg.Registry,
) -> list[Violation]:
    """Every uniquely identified collection member *value* holds more than once, at any depth.

    Driven by the registry's field list rather than a table of collections, so a
    collection two levels down (``operation.arguments``) is reached; the recursion
    follows the parsed value and terminates on its finite nesting.
    """
    violations: list[Violation] = []
    for descriptor in kind.fields:
        if descriptor.placement:
            continue  # not a model field: it lives beside the element, and holds one value
        attribute = attribute_for(type(value), descriptor.name)
        if attribute is None:  # pragma: no cover - the registry derives from model_fields
            continue
        held = getattr(value, attribute)
        field_at = Address(at.layer, at.element, (*at.path, FieldStep(descriptor.name)))
        collection = descriptor.collection
        if collection is None:
            nested = registry.kind_of_model(type(held))
            if nested is not None:  # a nested object (`connector.from`), not a collection
                violations.extend(_duplicate_members(held, nested, field_at, registry))
            continue
        if collection.kind == "map":
            # Keys are unique by construction; only the entries' interiors can hide one.
            for key, entry in (held or {}).items():
                nested = registry.kind_of_model(type(entry))
                if nested is not None:
                    entry_at = Address(at.layer, at.element, (*field_at.path, KeyStep(str(key))))
                    violations.extend(_duplicate_members(entry, nested, entry_at, registry))
            continue
        members = held if isinstance(held, list) else []
        if collection.member_identity != "whole_value":
            violations.extend(_duplicate_identities(members, collection, field_at))
        for member in members:
            nested = registry.kind_of_model(type(member))
            if nested is None:
                continue
            identity = member_identity_of(member, collection)
            member_at = Address(at.layer, at.element, (*field_at.path, MemberStep(identity)))
            violations.extend(_duplicate_members(member, nested, member_at, registry))
    return violations


def _duplicate_identities(
    members: Sequence[Any],
    collection: reg.CollectionDescriptor,
    field_at: Address,
) -> list[Violation]:
    """One violation per identity a uniquely identified collection carries twice."""
    counts: Counter[Any] = Counter()
    first: dict[Any, Any] = {}
    for member in members:
        identity = member_identity_of(member, collection)
        key = identity_key(identity)
        counts[key] += 1
        first.setdefault(key, identity)
    violations: list[Violation] = []
    for key, count in counts.items():
        if count < 2:
            continue
        identity = first[key]
        violations.append(
            Violation(
                error=CONSISTENCY_VIOLATION,
                message=(
                    f"the baseline holds {count} members of this collection carrying the identity "
                    f"{identity!r}, so a member step naming it cannot say which one an operation means. "
                    "Resolve the duplicate in the model before editing inside this element: a member is "
                    "addressed by its declared identity and there is no positional step"
                ),
                addresses=(Address(field_at.layer, field_at.element, (*field_at.path, MemberStep(identity))),),
            )
        )
    return violations


def unopenable_project_violation() -> Violation:
    """The project this request was dispatched against could not be opened.

    No manifest, an unreadable or unparseable one, one that is not a
    ``swcraft.yaml``, or one declaring a refused layout. ``consistency-violation``:
    the request is well formed and the client did not author the project, so
    ``schema-violation`` would be wrong. There is no address (the subject is the
    project), and the message names no location: a client is not owed a path.
    """
    return Violation(
        error=CONSISTENCY_VIOLATION,
        message=(
            "the project this request was dispatched against could not be opened: there is no swcraft.yaml "
            "manifest where one was expected, or the manifest cannot be read, is not valid YAML, is not a "
            "swcraft manifest, or declares a layout that is refused. The request itself is well formed — what "
            "has to be repaired is the project, outside the protocol"
        ),
    )


def unreadable_model_root_violation() -> Violation:
    """The project opened and its ``model_root`` is not a readable directory.

    Same code, reasoning and absence of an address as
    :func:`unopenable_project_violation`; the message names no path.
    """
    return Violation(
        error=CONSISTENCY_VIOLATION,
        message=(
            "the project's model root is not a readable directory: it does not exist, is not a directory, or "
            "cannot be listed. The request itself is well formed — a project whose manifest declares a model "
            "root that is not there has to be repaired outside the protocol"
        ),
    )


def retired_storage_violation(message: str) -> Violation:
    """A model that keeps element files in a directory the contract retired.

    Answered like :func:`unreadable_model_root_violation`, with no element address:
    the layout is at fault. *message* is the kernel's refusal
    (``model_state.RetiredStorageError``), model-root-relative, so it travels as is.
    """
    return Violation(error=CONSISTENCY_VIOLATION, message=message)


def malformed_scope_violation(message: str) -> Violation:
    """An ``approved_scope`` that is not a list of ``$defs/scopeEntry`` objects.

    Pass A's code, no addresses. Fail-closed: the whole change set is refused.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message)


def reserved_name_violation(layer: str, element: str, message: str) -> Violation:
    """A writer's reserved-name refusal, as the code pass D would have given it.

    ``consistency-violation``, addressed at the element whose declared name
    produces the collision.
    """
    return Violation(error=CONSISTENCY_VIOLATION, message=message, addresses=(Address(layer, element),))


def summary_not_derivable_violation(address: Address, message: str) -> Violation:
    """A ``summary`` the loaded contract does not let this server derive.

    ``schema-violation``, addressed at the address the client sent.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def unknown_filter_field_violation(address: Address, message: str) -> Violation:
    """A ``where`` clause naming a field nothing at its address declares.

    ``unknown-field``, as for an undeclared address step: the fix is to read
    ``discover``. The address is the one sent, unchanged: a filter clause is not a
    path step. See :class:`~swcraft_tools.editing.read.UnknownFilterField`.
    """
    return Violation(error=UNKNOWN_FIELD, message=message, addresses=(address,))


def filter_not_applicable_violation(address: Address, message: str) -> Violation:
    """A ``where`` this address cannot be filtered by.

    ``schema-violation``: the request is well formed and the combination has no
    answer — the address names one thing, or the clause names a collection, which is
    filtered at its own address. See :class:`~swcraft_tools.editing.read.FilterNotApplicable`.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def page_not_applicable_violation(address: Address, message: str) -> Violation:
    """A ``limit`` at an address with no set to window.

    ``schema-violation``; a constructor separate from the filter's so a client that
    sent a page and no filter is not told its filter did not apply. See
    :class:`~swcraft_tools.editing.read.PageNotApplicable`.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def unknown_member_collection_violation(address: Address, message: str) -> Violation:
    """A ``members`` naming a collection no kind of the layer declares.

    ``unknown-field``, as for an undeclared ``where`` field. The address is the one
    sent: ``members`` is not a path step. See
    :class:`~swcraft_tools.editing.read.UnknownMemberCollection`.
    """
    return Violation(error=UNKNOWN_FIELD, message=message, addresses=(address,))


def members_not_applicable_violation(address: Address, message: str) -> Violation:
    """A ``members`` this address or this field cannot answer.

    ``schema-violation``: the address is not a layer address, or the named field is
    not a collection. A constructor of its own so the message names its own key. See
    :class:`~swcraft_tools.editing.read.MembersNotApplicable`.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def unknown_derived_value_violation(address: Address, message: str) -> Violation:
    """A ``derived`` name the computed-value vocabulary does not carry.

    ``unknown-field``: a word outside ``discover``'s ``reads.derived``. See
    :class:`~swcraft_tools.editing.read.UnknownDerivedValue`.
    """
    return Violation(error=UNKNOWN_FIELD, message=message, addresses=(address,))


def derived_not_applicable_violation(address: Address, message: str) -> Violation:
    """A ``derived`` name at a set its vocabulary entry does not apply at.

    ``schema-violation``: the combination has no answer. See
    :class:`~swcraft_tools.editing.read.DerivedNotApplicable`.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def join_path_outside_row_violation(address: Address, message: str) -> Violation:
    """A ``join`` condition's path outside the declared row it reads.

    ``schema-violation``. See :class:`~swcraft_tools.editing.read.JoinPathOutsideRow`.
    """
    return Violation(error=SCHEMA_VIOLATION, message=message, addresses=(address,))


def page_cursor_violation(address: Address, message: str) -> Violation:
    """An ``after`` naming an identity the selected set does not carry.

    ``broken-reference``, the code of every content miss: the cursor came from an
    earlier page, and its absence means the model moved. Answering with a different
    window silently is what an offset page would do. The address is the one sent.
    See :class:`~swcraft_tools.editing.read.PageCursorNotFound`.
    """
    return Violation(error=BROKEN_REFERENCE, message=message, addresses=(address,))


def placement_violation(layer: str, element: str, field: str | None, message: str) -> Violation:
    """A value the registry cannot compose into a path component, as ``schema-violation``.

    *field* is ``None`` when the offending value is the identity, and the address
    then carries no path.
    """
    path = () if field is None else (FieldStep(field),)
    return Violation(
        error=SCHEMA_VIOLATION,
        message=message,
        addresses=(Address(layer, element, path),),
    )


def target_occupied_violation(layer: str, element: str, message: str) -> Violation:
    """A ``create`` whose plain file name a file declaring that same identity holds.

    The materialization-time twin of pass B's ``identity-conflict``, with the same
    code, remedy and ``{layer, element}`` address. A file name held by *another*
    element is no refusal: the create takes a collision suffix.
    """
    return Violation(error=IDENTITY_CONFLICT, message=message, addresses=(Address(layer, element),))


def unparseable_element_violation(address: Address | None) -> Violation:
    """A model file that does not parse, as ``consistency-violation``.

    The reader's own error names the file's absolute path, so it must never leave
    the seam (and an uncaught exception would also skip the change set's lifecycle
    record). The client did not write the file and its request is correct, so this
    is a model defect, not a ``schema-violation``.

    *address* is ``{layer, element}`` from the file's storage target, or ``None``
    for a file no layer accounts for. The message is composed from the address
    alone, never from the reader's text.
    """
    where = (
        "a model file"
        if address is None
        else f"the '{address.layer}' element '{address.element}'"  # the storage target's own identity
    )
    return Violation(
        error=CONSISTENCY_VIOLATION,
        message=(
            f"the model cannot be read: {where} is malformed YAML, or carries a value its schema rejects. The "
            "request itself is well formed — what is inconsistent is the model on disk, and it has to be repaired "
            "outside the protocol before this layer can be read or edited"
        ),
        addresses=() if address is None else (address,),
    )


def target_vanished_violation(layer: str, element: str, message: str) -> Violation:
    """A re-written or removed element no file of its layer declares, as ``consistency-violation``.

    :func:`target_occupied_violation`'s mirror and a model defect the client cannot
    fix inside the change set, reported at the element.
    """
    return Violation(error=CONSISTENCY_VIOLATION, message=message, addresses=(Address(layer, element),))


#: Every :data:`~swcraft_tools.editing.address.MissReason`, mapped onto the code
#: that answers it — the split passes B and C make, stated once so ``read`` answers
#: a miss as a change set does: the registry's vocabulary is ``unknown-field``, the
#: model's content ``broken-reference``.
MISS_CODES: dict[MissReason, str] = {
    "no_such_layer": UNKNOWN_FIELD,
    "no_such_field": UNKNOWN_FIELD,
    # A step the registry declares no way to take — a member step above a
    # non-list, a field step below a scalar. Vocabulary, not content.
    "not_traversable": UNKNOWN_FIELD,
    # A declared field whose value the snapshot does not carry: an engine condition,
    # mapped rather than left to raise a KeyError out of this table.
    "placement_unavailable": UNKNOWN_FIELD,
    "no_such_element": BROKEN_REFERENCE,
    "no_such_member": BROKEN_REFERENCE,
    "no_such_key": BROKEN_REFERENCE,
}


def miss_violation(miss: Miss) -> Violation:
    """One :class:`~swcraft_tools.editing.address.Miss` as the protocol's rejection.

    The address is the one the client sent; the failed step is named in the
    message, which is not contract.
    """
    return Violation(error=MISS_CODES[miss.reason], message=miss.detail, addresses=(miss.address,))


def over_budget_violation(
    address: Address | None,
    message: str,
    hints: tuple[Hint, ...],
    measured: Measured,
) -> Violation:
    """A read whose response would exceed the server's budget.

    For ``read`` and ``discover`` only. *address* is ``None`` for ``discover``, and
    ``addresses`` is then omitted. *hints* (the narrower requests that would fit)
    may be empty when nothing narrower exists.
    """
    return Violation(
        error=OVER_BUDGET,
        message=message,
        addresses=() if address is None else (address,),
        hints=hints,
        measured=measured,
    )


def undeclared_layer_violation(layer: str) -> Violation:
    """``discover`` narrowed to a layer the registry does not declare.

    The same ``unknown-field`` a ``read`` at that layer gets: ``$defs/layerName`` is
    a pattern, so the layer is well formed on the wire and refused semantically.
    """
    return Violation(
        error=UNKNOWN_FIELD,
        message=f"no layer '{layer}' is declared; `discover` with no layer answers with every layer there is",
        addresses=(Address(layer),),
    )


def _render(address: Address) -> str:
    """One address as prose, for a message. Never a storage path and never contract."""
    rendered = f"layer '{address.layer}'"
    if address.element is not None:
        rendered += f" element '{address.element}'"
    for step in address.path:
        # `render_model_scalar` and not `!r`: a member step carries a value, and every
        # message printing a model value goes through the one policy.
        rendered += " -> " + ", ".join(f"{key}={render_model_scalar(value)}" for key, value in step.to_json().items())
    return rendered


def _refusal(exc: EnvelopeError) -> ProtocolError:
    """One :class:`EnvelopeError` as the protocol's rejection; its message is not contract."""
    return ProtocolError.single(SCHEMA_VIOLATION, str(exc))


def _unmapped_findings(consistency: ConsistencyResult) -> str:
    """The findings of a disagreement :func:`pass_d` raises, by rule, layer and element.

    Read from ``records``, the error half :func:`pass_d` judged. Never a storage
    path: an MCP host returns an uncaught exception's text to the client, and a
    location here would name the staged copy.
    """
    if not consistency.records:
        return "none — the validator refused with no finding at all"
    return "; ".join(
        f"{record.rule} at "
        + (
            ", ".join(
                f"'{subject.element}' in the {subject.layer} layer"
                if subject.element is not None
                else f"the {subject.layer} layer"
                for subject in record.subjects
            )
            or "no model place"
        )
        for record in consistency.records
    )
