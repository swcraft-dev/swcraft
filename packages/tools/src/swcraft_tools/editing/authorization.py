"""The two authorization gates and the approval binding.

Nothing here does I/O or prompts: it answers *is this covered*, and the host owns
*may it proceed*. The scheme is address-keyed.

**The two gates.** Gate 1 runs at step 0, before the model is fingerprinted, over
the ``{operation, address}`` list the request **states**; gate 2, the authority,
runs at step 5 over the delta the staged result **produced**. Both refuse with
``unauthorized-delta``. A scope authorizes what a client may *ask*, so gate 1's
refusal never depends on the model's current value.

**Both gates run only where an approved scope exists.** ``None`` is an unscoped
run; an empty list states that nothing is approved, and it covers nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from swcraft_tools.editing.address import (
    Address,
    AddressError,
    ScopeEntry,
    covers_any,
)
from swcraft_tools.editing.protocol import ChangeOperation, DeltaEntry, Violation
from swcraft_tools.schema import Partition

# `SAFETY_FIELDS` and `SAFETY_MEMBER_FIELDS` are published for the classifier's parity gate.
__all__ = [
    "ApprovalDecision",
    "ApprovalMismatchError",
    "HARDWARE_LAYER",
    "SAFETY_FIELDS",
    "SAFETY_MEMBER_FIELDS",
    "UNAUTHORIZED_DELTA",
    "gate_delta",
    "gate_requested_operations",
    "parse_approved_scope",
]

#: The one ``unauthorized``-class code. Both gates use it.
UNAUTHORIZED_DELTA = "unauthorized-delta"

#: The one safety layer classified per element rather than per entry. A registry
#: layer name; it coincides with the snapshot attribute that holds the layer.
HARDWARE_LAYER = "hardware"

#: The scalar hardware fields whose ``update`` can weaken a check: the trigger only;
#: :func:`~swcraft_tools.authorization.classify_safety_act` decides the direction.
#: Clearing the optional ``asil_capability`` arrives as an ``update`` to ``None``.
SAFETY_FIELDS: frozenset[str] = frozenset({"threshold", "size", "enforcement", "asil_capability"})

#: The list-shaped safety fields. A widening is a ``create`` at the member's address
#: and a list's only ``update`` is a reordering, so in :data:`SAFETY_FIELDS` these
#: would classify reorderings and miss every widening.
SAFETY_MEMBER_FIELDS: frozenset[str] = frozenset({"accessible"})

#: ``(collection field, argument field)`` for each declared argument reached through
#: a member: ``ports/<port>/measures/<element>``, whose last step is a mapping key.
SAFETY_ARGUMENT_KEYS: frozenset[tuple[str, str]] = frozenset({("ports", "measures")})

#: The declared arguments that are a scalar on the element. The direction lives in
#: the value (``""`` -> a text weakens), so every ``update`` triggers.
SAFETY_ARGUMENT_FIELDS: frozenset[str] = frozenset({"coexistence"})

#: The fields on which an element declares a level, read on ``requirements``,
#: ``interfaces`` and ``swc``. Lowering or removing one makes a rule stop refusing
#: with no finding in exchange. A component is not exempt: rules reading the
#: effective level (``safety.consumer-above-element``, ``safety.task-mixed-asil``)
#: simply stop refusing.
SAFETY_ALLOCATION_FIELDS: frozenset[str] = frozenset({"asil"})

#: The one field whose member removal is a weakening: dropping a ``satisfies`` link
#: withdraws the allocation that gave a component its level. Every other removal
#: inside an element narrows something.
SAFETY_UNLINK_FIELDS: frozenset[str] = frozenset({"satisfies"})

#: The member collections whose members carry ``satisfies`` (``ports/<p>/satisfies/<m>``).
LINKING_MEMBERS: frozenset[str] = frozenset({"ports", "runnables"})

#: An unlink's path depths: ``satisfies/<m>`` and ``<owner>/<o>/satisfies/<m>``.
OWN_UNLINK_DEPTH = 2
MEMBER_UNLINK_DEPTH = 4

#: The argument fields split by the shape of their undeclared state, derived from the
#: sets above so a new argument cannot reach one and miss the other. :data:`UNDECLARED`
#: is that state: ``""`` for a scalar, ``None`` for a mapping created key by key.
_SCALAR_ARGUMENTS: tuple[str, ...] = tuple(sorted(SAFETY_ARGUMENT_FIELDS))
_MAPPING_ARGUMENTS: tuple[str, ...] = tuple(sorted({argument for _, argument in SAFETY_ARGUMENT_KEYS}))

#: Which layer declares which argument; the walk keys on the layer, because a
#: ``coexistence`` string inside a ``swc`` value is data (a ``measures`` key may be
#: named so), and keying on the field name alone would ask about a port as a task.
#: The ``hardware`` entry is read by no walk (a partition's argument is decided by
#: comparing two models); it mirrors the contract's placement of ``coexistence``.
SCALAR_ARGUMENTS_BY_LAYER: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"tasks": _SCALAR_ARGUMENTS, "hardware": _SCALAR_ARGUMENTS}
)
MAPPING_ARGUMENTS_BY_LAYER: Mapping[str, tuple[str, ...]] = MappingProxyType({"swc": _MAPPING_ARGUMENTS})

UNDECLARED: Mapping[str, Any] = MappingProxyType(
    {**{field: "" for field in _SCALAR_ARGUMENTS}, **{field: None for field in _MAPPING_ARGUMENTS}}
)

#: The synthetic call names the classifier dispatches on; no protocol request names a tool.
UPDATE_RESOURCE = "update_resource"
DELETE_ELEMENT = "delete_element"

#: A partition's ``trust`` when it declares none, read off the schema.
DEFAULT_TRUST: str = Partition.model_fields["trust"].default


# ---------------------------------------------------------------------------
# The approved scope
# ---------------------------------------------------------------------------


def parse_approved_scope(entries: Sequence[Mapping[str, Any]] | None) -> tuple[ScopeEntry, ...] | None:
    """The parsed wire form of an approved scope, keeping ``None`` distinct from ``[]``.

    Returns:
        ``None`` for an unscoped run, otherwise the parsed entries — ``()`` for
        an empty list, which is the read-only scope and covers nothing.

    Raises:
        AddressError: the scope is not a list (a scalar included), or an entry
            is not ``{operation, address}``.
    """
    if entries is None:
        return None
    if not isinstance(entries, list | tuple):
        raise AddressError(f"an approved scope must be a list of entries, not {type(entries).__name__}")
    return tuple(ScopeEntry.from_json(entry) for entry in entries)


# ---------------------------------------------------------------------------
# Gate 1 — step 0, over what the request states
# ---------------------------------------------------------------------------


def gate_requested_operations(
    operations: Sequence[ChangeOperation],
    scope: Sequence[ScopeEntry] | None,
) -> Violation | None:
    """Gate 1: whether every operation the request **states** is covered.

    Returns:
        ``None`` when every operation is covered, and always for an unscoped run.
        Otherwise one ``unauthorized-delta`` violation naming every uncovered
        operation by its index: the user declines the change as a whole.
    """
    if scope is None:
        return None
    uncovered = [
        (index, operation)
        for index, operation in enumerate(operations)
        if not covers_any(scope, operation.operation, operation.address)
    ]
    if not uncovered:
        return None
    return Violation(
        error=UNAUTHORIZED_DELTA,
        message=(
            "the approved scope does not cover "
            + ", ".join(
                f"operation {index} ({_render(operation.operation, operation.address)})"
                for index, operation in uncovered
            )
            + f"; the scope carries {len(scope)} entr{'y' if len(scope) == 1 else 'ies'}"
        ),
        addresses=tuple(operation.address for _, operation in uncovered),
    )


# ---------------------------------------------------------------------------
# Gate 2 — step 5, the authority
# ---------------------------------------------------------------------------


def gate_delta(delta: Sequence[DeltaEntry], scope: Sequence[ScopeEntry] | None) -> Violation | None:
    """Gate 2: whether every entry of the **produced** delta is covered.

    The delta is what the model became, which may be wider or narrower than the request.

    Returns:
        ``None`` when every entry is covered. Otherwise one
        ``unauthorized-delta`` violation naming every uncovered entry: the
        commit applies the reviewed delta whole or not at all. A scope entry no
        delta entry matches is never an error: doing less than allowed is safe.
    """
    if scope is None:
        return None
    uncovered = [entry for entry in delta if not covers_any(scope, entry.operation, entry.address)]
    if not uncovered:
        return None
    return Violation(
        error=UNAUTHORIZED_DELTA,
        message=(
            f"the approved scope does not cover {len(uncovered)} of {len(delta)} delta entr"
            f"{'y' if len(delta) == 1 else 'ies'}: "
            + ", ".join(_render(entry.operation, entry.address) for entry in uncovered)
            + ". There is no partial approval: the commit boundary applies the reviewed delta whole or not at all"
        ),
        addresses=tuple(entry.address for entry in uncovered),
    )


def _render(operation: str, address: Address) -> str:
    """One operation at one address, for a message. Never parsed by a client."""
    rendered = address.layer
    if address.element is not None:
        rendered += f"/{address.element}"
    for step in address.path:
        rendered += "." + str(step.to_json())
    return f"{operation} {rendered}"


# ---------------------------------------------------------------------------
# The approval binding
# ---------------------------------------------------------------------------


class ApprovalMismatchError(RuntimeError):
    """An approval decision was offered for a change set it is not bound to.

    A fault, not a rejection: no protocol operation approves anything, so only a
    host replaying a decision over another change set or baseline raises it.
    """


@dataclass(frozen=True)
class ApprovalDecision:
    """A human's yes or no over one reviewed delta, bound to where it was taken.

    An approval never carries over: it binds one change-set id and one baseline
    token, so a stale change set is re-authored and reviewed again, never re-based.

    ``approved`` fails closed (the host yields ``False`` for an empty line or EOF).
    ``safety_approved`` approves a weakened check inside the delta; it defaults to
    ``False`` so a host that never renders the safety verdict cannot let a
    weakening through. ``channel`` (``elicit``, ``panel`` or ``None``) is recorded,
    never interpreted.
    """

    change_set_id: str
    baseline: str
    approved: bool
    safety_approved: bool = False
    channel: str | None = None

    def binds_to(self, change_set_id: str, baseline: str) -> bool:
        """Whether this decision was taken over exactly this change set at this baseline."""
        return self.change_set_id == change_set_id and self.baseline == baseline

    def require_binding(self, change_set_id: str, baseline: str) -> None:
        """The guard that raises unless :meth:`binds_to` holds.

        Raises:
            ApprovalMismatchError: naming both halves of the mismatch.
        """
        if not self.binds_to(change_set_id, baseline):
            raise ApprovalMismatchError(
                f"an approval taken over change set {self.change_set_id!r} at baseline {self.baseline!r} "
                f"cannot be replayed onto {change_set_id!r} at {baseline!r}: an approval binds to one "
                "change-set id and one baseline token"
            )
