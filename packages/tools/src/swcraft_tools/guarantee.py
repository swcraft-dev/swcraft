"""An explicit check of a value that another part of the code guarantees.

:func:`required` takes a value typed ``T | None`` that a rule held elsewhere (a
schema validator, a precondition) keeps from being ``None``, and returns it typed
``T`` or raises :class:`BrokenGuarantee` naming that rule.
:func:`required_instance` does the same where the guarantee is the value's class.

A broken guarantee is a defect in SWCraft, never a refusal of the user's input: a
caller does not catch it to report a model problem. The message is the caller's
*guarantee* sentence alone, never a path or a model value.

**What it does not reach:** presence and class. A guarantee on a value's form
-- a range, a shape, a member's type -- is not checked here.
"""

from __future__ import annotations

from typing import TypeVar

__all__ = ["BrokenGuarantee", "required", "required_instance"]

T = TypeVar("T")


class BrokenGuarantee(RuntimeError):
    """A guarantee held elsewhere did not hold at its read: a defect, never a user refusal."""


def required(value: T | None, guarantee: str) -> T:
    """Return *value*, or raise :class:`BrokenGuarantee` with *guarantee* when it is ``None``.

    *guarantee* is a short sentence naming the rule that holds the value, e.g.
    ``"map_ports sets every kept port's interface"``. Only ``None`` is refused: an
    empty string, zero or an empty container is a present value and is returned.
    """
    if value is None:
        raise BrokenGuarantee(guarantee)
    return value


def required_instance(value: object, kind: type[T] | tuple[type[T], ...], guarantee: str) -> T:
    """Return *value* as a *kind*, or raise :class:`BrokenGuarantee` with *guarantee* when it is not one.

    *kind* is a class or, as :func:`isinstance` takes it, a tuple of classes; the
    result is then typed as their join.
    """
    if not isinstance(value, kind):
        raise BrokenGuarantee(guarantee)
    return value
