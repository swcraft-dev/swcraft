"""How a model-sourced value is rendered **into a message**, decided once.

CPython bounds ``int.__repr__`` (and ``str(int)``, and every ``f"{n}"``) by
``sys.get_int_max_str_digits()`` — 4 300 digits by default — and raises ``ValueError`` above it. It does
**not** bound ``int(s, 16)`` or ``int(s, 2)``. YAML resolves ``0x…`` through ``int(v, 16)``, so a hand-authored
``init: 0xffff…`` of 4 000 hexadecimal digits loads, passes Pydantic and reaches every consumer as an ``int``
that cannot be printed.

**The policy.** A site that *describes* a value to a human substitutes a description it can always produce; a
site that *serializes* a value keeps raising, because a YAML or JSON document carrying "too wide to render" in
place of the number would be a false model. So a message that prints a value read off a model element renders
it through :func:`render_model_scalar`; ``yaml.safe_dump``, ``json.dumps`` and the query literal are left
raising. Comparison keys are compared and hashed, never printed, so this policy does not reach them.

**What this module does not reach:**

* it does not bound the value at the model boundary: ``interfaces.data_element.init``, ``enum_literal.value``
  and ``core.hw_id`` carry no Pydantic validator. Only ``init`` is bounded, one layer further in, by
  ``rule:interface.init-unwritable-integer``, which the ARXML writer refuses; a model carrying such a value is
  loaded, reported and refused, never rejected at parse time;
* it does not make an oversized value usable: such a model still cannot be written back, exported or serialized;
* nothing gates a new message site: a renderer added later gets this behaviour only by calling this function. An
  ``ast`` scan keyed on the integer field names was rejected: it flagged 19 sites, one of them the defect and 18
  unrelated ``value`` locals, and without ``value`` it misses the defect;
* it rebuilds only ``list`` and ``dict``, the two container shapes an ``InitValue`` can take. Any other value
  whose ``repr`` raises ``ValueError`` (a ``tuple``, a ``set``, a dataclass) is re-raised unchanged.

A leaf module: its callers form no chain, so it imports nothing from ``swcraft_tools``. :func:`path_free`
renders an exception into a message with no filesystem location in it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["path_free", "render_model_scalar"]


def render_model_scalar(value: object) -> str:
    """*value* as a message may print it — ``repr``, or a description of it.

    Byte-identical to ``repr(value)`` for every printable value: ``repr`` is tried first and the substitution
    lives entirely inside the ``except ValueError``. A too-wide :class:`int` becomes a description naming its
    ``bit_length()``; a ``list`` or ``dict`` (an ``init`` is recursive) is re-spelled around its rendered
    members. Anything else whose ``repr`` raises ``ValueError`` is re-raised.
    """
    try:
        return repr(value)
    except ValueError:
        if isinstance(value, int):
            # `bit_length()` builds no decimal string, so this line cannot raise the exception it handles.
            return f"<an integer of {value.bit_length()} bits, too wide to render>"
        if isinstance(value, list):
            return "[" + ", ".join(render_model_scalar(item) for item in value) + "]"
        if isinstance(value, dict):
            pairs = ", ".join(f"{render_model_scalar(key)}: {render_model_scalar(item)}" for key, item in value.items())
            return "{" + pairs + "}"
        raise


def path_free(exc: BaseException, *model_roots: Path) -> str:
    """*exc*'s text with no filesystem location in it, for a message a host log or a client note carries.

    An :class:`OSError` keeps its errno and reason and names its file only when it lies under one of
    *model_roots*, by its path relative to that root; a file anywhere else is not named. Every other exception
    is its own text. What it does not reach: a location an exception's own message spells out, rather than
    carries as ``filename``.
    """
    if not isinstance(exc, OSError) or exc.strerror is None:
        return str(exc)
    reason = exc.strerror if exc.errno is None else f"[Errno {exc.errno}] {exc.strerror}"
    for root in model_roots:
        try:
            return f"{reason}: {Path(exc.filename).relative_to(root).as_posix()}"
        except (TypeError, ValueError):
            continue
    return reason
