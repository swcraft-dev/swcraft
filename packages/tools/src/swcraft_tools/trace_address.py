"""The trace link search's target address, its order, its errors and its exact integer spelling.

A stdlib leaf beneath :mod:`swcraft_tools.trace_search` and :mod:`swcraft_tools.trace_records`, so
neither imports the other for these names.
"""

from __future__ import annotations

from dataclasses import dataclass


class TraceError(Exception):
    """Every refusal and failure of the search: ``swcraft experiment trace`` prints its sentence and exits 1.

    Each sentence names a key, a requirement, a document or a rule -- never a
    path, which the caller owns.
    """


class TraceExperimentError(TraceError, ValueError):
    """A configuration, a name it uses, an ``output`` or an ambiguous identity the search refuses."""


#: The group kinds, in the order ties between groups are broken.
GROUP_KIND_ORDER: tuple[str, ...] = (
    "swc",
    "composition",
    "interface",
    "task",
    "partition",
    "core",
    "processor",
    "memory",
)

#: A component's members, in the order ties between its rows are broken.
MEMBER_RANK = {"swc": 0, "port": 1, "runnable": 2}


@dataclass(frozen=True)
class TargetAddress:
    """A target's structured address: its kind, its owner (the component of a port or a runnable) and its name."""

    kind: str
    owner: str | None
    name: str

    @property
    def group(self) -> GroupKey:
        """The group the target belongs to: its owner's for a port or a runnable, its own otherwise."""
        if self.owner is not None:
            return GroupKey("swc", self.owner)
        return GroupKey(self.kind, self.name)

    def to_json(self) -> dict[str, str | None]:
        return {"kind": self.kind, "owner": self.owner, "name": self.name}


@dataclass(frozen=True)
class GroupKey:
    """A target group: a component (with its ports and runnables), or one element of another kind."""

    kind: str
    name: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name}


def group_order_key(group: GroupKey) -> tuple[int, str, str]:
    """The group order: kind order, then the name by ``(casefold, exact)``."""
    return (GROUP_KIND_ORDER.index(group.kind), group.name.casefold(), group.name)


def address_order_key(address: TargetAddress) -> tuple[int, str, str, int, str, str]:
    """The target address order: the group's order, then the owner, its ports and its runnables, then the name."""
    group = address.group
    return (*group_order_key(group), MEMBER_RANK.get(address.kind, 0), address.name.casefold(), address.name)


def id_order_key(identifier: str) -> tuple[str, str]:
    """The requirement id order, used for every tie between requirements: ``(casefold, exact)``."""
    return (identifier.casefold(), identifier)


#: An integer of fewer bits than this has fewer decimal digits than the smallest limit
#: ``sys.set_int_max_str_digits()`` accepts (640), so ``str()`` converts it under any limit.
SAFE_INTEGER_BITS = 2000
#: The digits converted per step of :func:`exact_decimal`, below that smallest limit.
_DIGITS_PER_STEP = 600


def exact_decimal(value: int) -> str:
    """*value*'s exact decimal spelling at any width, never changing the process-wide digit limit.

    A link's version may be wider than ``int()``'s default limit; this converts in chunks below the
    smallest limit CPython accepts.
    """
    if value.bit_length() < SAFE_INTEGER_BITS:
        return str(value)
    sign, value = ("-", -value) if value < 0 else ("", value)
    step = 10**_DIGITS_PER_STEP
    chunks: list[int] = []
    while value:
        value, chunk = divmod(value, step)
        chunks.append(chunk)
    head, *rest = reversed(chunks)
    return sign + exact_decimal(head) + "".join(f"{chunk:0{_DIGITS_PER_STEP}d}" for chunk in rest)
