"""Where a validator finding *is*, expressed in model terms and nothing else.

The validator and ``resource_analysis`` report findings as prose, and a message is not a contract, so a
consumer that has to act on a finding reads its **subjects**: the model places it is about. A subject is a
layer, optionally an element identity within it, and optionally a path of steps into that element — the same
shape as an editing-protocol address, but not that type: ``editing/`` depends on the validator, so this
module imports nothing from it.

A **leaf**: standard library only, so anything that produces findings may depend on it. Subjects are
additive and optional; a finding that has no useful subject carries none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "KEY",
    "MEMBER",
    "FIELD",
    "Subject",
    "SubjectStep",
    "field_step",
    "key_step",
    "member_step",
]

#: The three step kinds an editing-protocol address path admits. There is no ordinal step: an index denotes a
#: different thing the moment anything is inserted before it.
FIELD = "field"
MEMBER = "member"
KEY = "key"


@dataclass(frozen=True)
class SubjectStep:
    """One step into an element: a field, a collection member, or a map key.

    ``value`` names the step: a field name, a map key, or a member *identity* — a plain name (``"pOut"``), a
    composite object or the member's whole value. It is not constrained here.
    """

    kind: str
    value: Any

    def to_json(self) -> dict[str, Any]:
        """This step as one JSON object: ``{"field": …}``, ``{"member": …}`` or ``{"key": …}``.

        The value is spelled as written, not canonicalised; it is the spelling ``swcraft check-model --format
        json`` prints and equals the editing protocol's step ``to_json``.
        """
        return {self.kind: self.value}


def field_step(name: str) -> SubjectStep:
    """A declared field of the owning element."""
    return SubjectStep(FIELD, name)


def member_step(identity: Any) -> SubjectStep:
    """One member of a nested collection, named by its identity."""
    return SubjectStep(MEMBER, identity)


def key_step(key: Any) -> SubjectStep:
    """One entry of a map field, named by its key.

    In a finding it may also name a key no field declares
    (``model.unknown-key``, contract ``12.4.0``): the key of an element file the
    loader dropped, as the file spells it. That is the one case where a key
    step follows a member or an element rather than a map field.
    """
    return SubjectStep(KEY, key)


@dataclass(frozen=True)
class Subject:
    """One model place a finding is about.

    Args:
        layer: the model layer (``swc``, ``interfaces``, ``tasks``, …).
        element: the identity of the element within it — ``name`` everywhere,
            ``id`` for ``requirements`` — or ``None`` when the finding is about
            the layer as a whole.
        path: the steps into that element, outermost first. Empty when the
            finding is about the element itself.
    """

    layer: str
    element: str | None = None
    path: tuple[SubjectStep, ...] = ()

    def at(self, *steps: SubjectStep) -> Subject:
        """This subject, extended by *steps*."""
        return Subject(layer=self.layer, element=self.element, path=(*self.path, *steps))

    def to_json(self) -> dict[str, Any]:
        """This subject as ``{"layer", "element"?, "path"?}``.

        ``element`` is spelled only when it is not ``None`` and ``path`` only when it is non-empty, exactly as
        the editing protocol's ``Address.to_json`` spells an address; this is the subject spelling of ``swcraft
        check-model --format json``.
        """
        result: dict[str, Any] = {"layer": self.layer}
        if self.element is not None:
            result["element"] = self.element
        if self.path:
            result["path"] = [step.to_json() for step in self.path]
        return result
