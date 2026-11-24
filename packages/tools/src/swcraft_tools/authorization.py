"""The always-on hardware-budget safety classifier, and the layer field vocabulary.

The address-keyed mutation gates live in :mod:`swcraft_tools.editing.authorization`. What this
module holds:

* :func:`_layer_fields` -- the per-layer field vocabulary, under each field's *serialization alias*
  (``Connector.from_`` is ``from``; ``from_`` is rejected), read off ``model_fields``
  (:func:`_layer_vocabulary`), with :func:`_layer_names`, :func:`_layer_identity_fields` and
  :func:`_layer_models`: projections of the kernel registry that take a ``registry``. The vocabulary
  excludes the identity field (``name``, or ``id`` for ``requirements``): an identity change is a
  removal plus an addition.
* :data:`REFUSAL_MARKER` / :class:`Refusal` -- the refusal vocabulary the hosts build refusals in.
* :func:`classify_safety_act`, :func:`classify_argument_act`, :func:`classify_allocation_act` and
  :func:`classify_trust_act`, with :class:`SafetyVerdict` / :class:`SafetyChange` -- the always-on
  safety guard: a weakening needs its own explicit confirmation however the mutation arrived.
  The weakening directions: **raising** a ``threshold``, **enlarging** a memory's ``size``, ``strict`` ->
  ``advisory``, **admitting a core** to a memory's ``accessible``, and **raising** a core's or a
  memory's ``asil_capability`` or clearing one below ``D``; their inverses are not. A verdict
  carries every weakening change a call would make.

:func:`classify_safety_act` dispatches on ``update_resource`` and ``delete_element``, call names
:mod:`swcraft_tools.editing.safety_guard` synthesises from a change set's addresses, not MCP tools;
:data:`DELETE_ELEMENT_LAYERS` is its ``element_type`` vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, get_args

from pydantic import BaseModel

from swcraft_tools.model_registry import (
    LayerFacts,
    ModelRegistry,
    current_registry,
)
from swcraft_tools.safety import ASIL_LEVELS, asil_rank
from swcraft_tools.schema import Core, Hardware, Memory, Partition

# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def _layer_key(facts: LayerFacts) -> str:
    """The name these projections key a layer by.

    The ``ModelSnapshot`` attribute (``swcs`` for the contract's ``swc``), or the layer's own
    contract name when the binding gives it no snapshot attribute.
    """
    return facts.snapshot_attribute or facts.name


def _ordered_layers(registry: ModelRegistry) -> tuple[LayerFacts, ...]:
    """Every layer, in ``ModelSnapshot`` field order first and contract order after.

    The order is the registry's ``adapter_order``, not the contract's ``displayOrder``; a layer with
    no snapshot attribute follows in contract order, keyed by its own name.
    """
    adapter = registry.adapter_order
    trailing = tuple(name for name in registry.layer_names if name not in adapter)
    return tuple(registry.layer(name) for name in (*adapter, *trailing))


def _model_classes(annotation: Any) -> tuple[type[BaseModel], ...]:
    """Unwrap ``Annotated[A | B, Field(discriminator=...)]`` into its members."""
    if hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    members = [arg for arg in get_args(annotation) if isinstance(arg, type) and issubclass(arg, BaseModel)]
    return tuple(members) if members else (annotation,)


def _layer_vocabulary(annotation: Any, identity_field: str) -> frozenset[str]:
    """Every member class's top-level field aliases, minus the identity field."""
    aliases: set[str] = set()
    for model_class in _model_classes(annotation):
        for field_name, field_info in model_class.model_fields.items():
            aliases.add(field_info.alias or field_name)
    return frozenset(aliases - {identity_field})


def _layer_models(registry: ModelRegistry | None = None) -> dict[str, Any]:
    """Layer key -> the annotation an element of that layer parses as.

    A single class for a layer with one top-level kind, and the discriminated union
    :func:`_model_classes` unwraps for one with several.

    Args:
        registry: the kernel registry to describe; ``None`` resolves the current one at call time.
    """
    registry = registry or current_registry()
    return {_layer_key(facts): facts.element_annotation for facts in _ordered_layers(registry)}


def _layer_identity_fields(registry: ModelRegistry | None = None) -> Mapping[str, str]:
    """Layer key -> the field ``differ`` indexes that layer by. Never part of the vocabulary."""
    registry = registry or current_registry()
    return MappingProxyType({_layer_key(facts): facts.identity_field for facts in _ordered_layers(registry)})


def _layer_names(registry: ModelRegistry | None = None) -> tuple[str, ...]:
    """Every layer key, in ``ModelSnapshot`` field order."""
    registry = registry or current_registry()
    return tuple(_layer_key(facts) for facts in _ordered_layers(registry))


def _layer_fields(registry: ModelRegistry | None = None) -> Mapping[str, frozenset[str]]:
    """Layer key -> the field names reportable for it, under their serialization aliases.

    The aliases are read off the bound classes' ``model_fields``: the contract declares field ids,
    and the differ reports ``model_dump(by_alias=True)`` names.
    """
    registry = registry or current_registry()
    return MappingProxyType(
        {
            _layer_key(facts): _layer_vocabulary(facts.element_annotation, facts.identity_field)
            for facts in _ordered_layers(registry)
        }
    )


# ---------------------------------------------------------------------------
# The safety classifier's element-type vocabulary
# ---------------------------------------------------------------------------

#: :func:`classify_safety_act`'s ``element_type`` -> the layer it lives in; an unsupported type is
#: refused rather than mapped to nothing.
#:
#: Declared, deliberately not derived from the kernel registry: it is the closed set of element types
#: the always-on guard classifies a ``delete_element`` for, and no structural rule over the contract
#: yields it (the ``hardware`` layer declares no kind named ``hardware``, and a "top-level kind name"
#: rule would admit ``system``, ``task`` and ``sequence``). Deriving it would silently widen a safety
#: vocabulary. ``composition`` is listed so the guard answers its deletion (an empty verdict: a
#: composition holds no budget and no ``accessible`` list) instead of failing closed on it. A
#: ``requirement`` row's answer is the empty verdict too: removing a levelled one is a weakening that
#: :func:`classify_allocation_act` reports from the delta.
DELETE_ELEMENT_LAYERS: Mapping[str, str] = MappingProxyType(
    {
        "swc": "swcs",
        "requirement": "requirements",
        "connector": "connectors",
        "hardware": "hardware",
        "composition": "compositions",
    }
)


# ---------------------------------------------------------------------------
# The refusal vocabulary
# ---------------------------------------------------------------------------

#: The marker every refusal message starts with, wherever it is built: the agent system prompt's
#: "a ``REFUSED:`` result is final, report it and do not retry" rule keys on this one token.
REFUSAL_MARKER = "REFUSED: "


@dataclass(frozen=True)
class Refusal:
    """One reason a call was refused, in the shape every host renders.

    ``target`` is a display string (``"<layer>:<name>"``) and not a grammar: literal ``:`` is a legal
    element-name character, so nothing parses it back. It is empty when the refusal is about the call
    rather than an element.
    """

    code: str
    tool: str
    target: str
    message: str

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Safety acts (always on, independent of the scope)
# ---------------------------------------------------------------------------


class SafetyClassificationError(ValueError):
    """A call naming a hardware element could not be classified against the model.

    Raised when the named element does not exist, when its name is ambiguous
    (``hardware.duplicate-name``), or when an argument's shape makes the comparison impossible. Never
    answered with "not weakening": a guard that cannot read the current values has nothing to compare
    against.
    """


class SafetyChangeKind(StrEnum):
    """The twelve acts the safety guard asks about: eleven weaken a check, one widens a permission.

    Twelve kinds in **nine** directions: the sixth direction, declaring an argument, has four kinds
    (:data:`DECLARE_MEASURE`, :data:`DECLARE_COEXISTENCE`, :data:`DECLARE_PARTITION_COEXISTENCE` and
    :data:`DECLARE_PARTITION_PROTECTION`). New kinds are appended, so no member's position moves.

    Every kind but one meets the weakening definition: the edit makes a rule stop refusing and
    creates no finding in exchange. :data:`GRANT_PARTITION_TRUST` widens what the partition may
    write; no check refuses it (pass D already refuses a trusted partition below the processor's
    highest level), so it is asked about where it is legal, in the same safety question.

    The member names are not a wire format: the prompt renders :meth:`SafetyChange.describe`, and the
    audit log records the verdict's decision rather than its kinds.
    """

    #: ``strict`` -> ``advisory``: breaches become report-only ``info`` lines
    #: and a missing consumer budget starts counting as zero.
    WEAKEN_ENFORCEMENT = "weaken_enforcement"
    #: A higher ``threshold``. ``limit = size x threshold``, so this *raises* the limit.
    RAISE_THRESHOLD = "raise_threshold"
    #: A larger memory ``size``, which raises the limit the same way.
    ENLARGE_POOL = "enlarge_pool"
    #: A core admitted to a memory's ``accessible``. Narrowing that list is *not* one: it can only
    #: make ``hardware.unreachable-memory`` refuse more.
    WIDEN_ACCESS = "widen_access"
    #: The core or the memory, and with it every check it performed, goes away.
    DELETE_RESOURCE = "delete_resource"
    #: A ``measures`` entry declared on a port, which makes ``safety.consumer-above-element`` stop
    #: refusing that element. The direction lives in the operation (a ``create`` at the entry's key),
    #: so removing one narrows.
    DECLARE_MEASURE = "declare_measure"
    #: The same sixth direction on the task: a ``coexistence`` argument written where there was none,
    #: which makes ``safety.task-mixed-asil`` stop refusing. The direction lives in the value
    #: (``""`` -> text), as ``strict`` -> ``advisory`` does.
    DECLARE_COEXISTENCE = "declare_coexistence"
    #: What a bearer declares, lowered or removed: a requirement's, a component's own or a data
    #: element's ``asil``, a ``satisfies`` member on a component, one of its ports or one of its
    #: runnables, the ``require`` port through which a rated element was consumed under a declared
    #: measure, or the bearer removed whole (the levelled requirement, the rated data element, the
    #: interface that declared one, the component whose effective level is a real ASIL or that carried
    #: a measured ``require`` port, the task whose ``coexistence`` argument answered a genuine spread of
    #: levels). Raising any of them is not one; :func:`classify_allocation_act` states the limit that
    #: costs and what the whole-element arm does not reach.
    LOWER_ALLOCATION = "lower_allocation"
    #: A core's or a memory's ``asil_capability`` raised, or cleared where it stood below ``D``, which
    #: makes ``safety.partition-on-unqualified-core`` or ``safety.memory-below-component`` stop refusing
    #: the partitions or components placed on the element, and, for a memory,
    #: ``safety.partition-memory-below-level`` stop refusing a partition whose components budget it.
    #: Not :data:`RAISE_THRESHOLD`'s direction: a level is not a budget. Decided by outcome over the
    #: set of levels the element refuses, so ``A -> None`` is one and ``D -> None`` is not; declaring a
    #: capability where none stood, and lowering one, refuse more and are not.
    RAISE_CAPABILITY = "raise_capability"
    #: A partition's ``coexistence`` argument declared where there was none (``""`` -> a text, a
    #: partition created carrying one, or a standing text going live because the partition became
    #: mixed), which makes ``safety.component-below-partition`` stop refusing the partition's lower
    #: components. A kind of its own so the prompt names a partition and its rule. Decided by the
    #: editing guard's comparison of two models, never by an address.
    DECLARE_PARTITION_COEXISTENCE = "declare_partition_coexistence"
    #: The one kind that is not a weakening: a partition's ``trust`` raised to ``trusted`` (from
    #: ``non_trusted`` or ``trusted_with_protection``, or a partition created ``trusted``). A trusted
    #: OS-Application is unrestricted for memory (``[SWS_Os_00207]``,
    #: ``[SWS_Os_00355]``, ``[SWS_Os_00356]`` protect private data from
    #: NON-trusted OS-Applications only), so the edit widens what the partition
    #: may write. No check refuses it, so the act is asked about where it is legal. Decided by the
    #: editing guard's comparison of two models.
    GRANT_PARTITION_TRUST = "grant_partition_trust"
    #: A ``trusted`` partition declared ``trusted_with_protection`` or ``non_trusted``, which makes
    #: ``safety.trusted-partition-below-processor-level`` and ``safety.trusted-partition-unrated`` stop
    #: refusing it, while nothing in the model checks that the OS configuration confines it. The guard
    #: asks; it never refuses.
    DECLARE_PARTITION_PROTECTION = "declare_partition_protection"


#: Why the guard asks about each kind, in the words the approval prompt shows.
_WEAKENING_REASONS: Mapping[SafetyChangeKind, str] = MappingProxyType(
    {
        SafetyChangeKind.WEAKEN_ENFORCEMENT: (
            "an advisory core or memory reports instead of failing, and an undeclared consumer budget counts as zero"
        ),
        SafetyChangeKind.RAISE_THRESHOLD: (
            "limit = size x threshold, so a higher threshold raises the limit and more consumption passes"
        ),
        SafetyChangeKind.ENLARGE_POOL: (
            "limit = size x threshold, so a larger memory raises the limit and more consumption passes"
        ),
        SafetyChangeKind.WIDEN_ACCESS: (
            "a core this memory refused before may use it now, so hardware.unreachable-memory "
            "stops refusing the tasks that charge it a non-zero budget"
        ),
        SafetyChangeKind.DELETE_RESOURCE: "every check this core or memory performed stops being performed",
        SafetyChangeKind.DECLARE_MEASURE: (
            "a declared measure answers safety.consumer-above-element for that data element, so the "
            "check stops refusing a component that consumes it from a lower-rated source"
        ),
        SafetyChangeKind.DECLARE_COEXISTENCE: (
            "a declared coexistence argument answers safety.task-mixed-asil, so the check stops "
            "refusing a task whose components carry different safety integrity levels"
        ),
        SafetyChangeKind.LOWER_ALLOCATION: (
            "lowering or removing what this bearer declares — the level it declares or allocates, the port "
            "through which a rated element was consumed, or the argument that answered a task's or a "
            "partition's mixed levels — makes the check stop refusing, and creates no finding in exchange"
        ),
        SafetyChangeKind.RAISE_CAPABILITY: (
            "a core or a memory declared able to host a higher level — or no longer bounded at all — stops "
            "safety.partition-on-unqualified-core, safety.memory-below-component or "
            "safety.partition-memory-below-level refusing the partitions or components placed on it"
        ),
        SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE: (
            "a declared coexistence argument answers safety.component-below-partition, so the check stops "
            "refusing a partition that runs components rated below its own level"
        ),
        SafetyChangeKind.GRANT_PARTITION_TRUST: (
            "the edit widens what the partition may write: a trusted partition is unrestricted for memory, so "
            "every partition beside it depends on it for its own memory's integrity, and no check refuses the act "
            "where it is legal"
        ),
        SafetyChangeKind.DECLARE_PARTITION_PROTECTION: (
            "a declared confinement answers safety.trusted-partition-below-processor-level and "
            "safety.trusted-partition-unrated, so the checks stop refusing a trusted partition beside a higher "
            "level, and nothing in the model verifies that the operating system confines it"
        ),
    }
)


#: The fields whose WITHDRAWAL is the weakening, where every other field reaching
#: :func:`classify_allocation_act` is a level compared against a level. The caller has already
#: decided the condition (the removed ``satisfies`` member named a levelled requirement and the
#: component's allocated maximum dropped, the port was a ``require`` port carrying a map, the task's
#: runnables spanned two levels, a rated consumer's port left a rated source for an external interface
#: stub), so this records the movement rather than re-deriving it.
_WITHDRAWN_WHOLE: frozenset[str] = frozenset({"satisfies", "measures", "coexistence", "interface"})


def _admitted_members(old: Any, new: Any) -> list[str]:
    """The members *new* adds to *old*, in *new*'s order.

    Tolerates a non-list on either side rather than raising, because it runs inside
    :meth:`SafetyChange.describe`, which renders a prompt and must not fail in front of a human.
    """
    before = list(old) if isinstance(old, list) else []
    after = list(new) if isinstance(new, list) else []
    return [str(member) for member in after if member not in before]


@dataclass(frozen=True)
class SafetyChange:
    """One weakening a call would make, with the values it moves between.

    ``old`` and ``new`` are read from the session's *current* model, never from the call's arguments
    alone. For :data:`SafetyChangeKind.DELETE_RESOURCE` ``field`` is empty, ``old`` is a rendering of
    the whole element and ``new`` is ``None``. For :data:`SafetyChangeKind.WIDEN_ACCESS` both are the
    memory's whole ``accessible`` lists, so such an instance is **not hashable**, and :meth:`describe`
    renders the cores the change admits. :attr:`resource` is the element's name.
    """

    kind: SafetyChangeKind
    resource: str
    field: str
    old: Any
    new: Any

    def describe(self) -> str:
        """A stable one-line rendering for the approval prompt."""
        reason = _WEAKENING_REASONS[self.kind]
        if self.kind is SafetyChangeKind.DELETE_RESOURCE:
            return f"delete hardware element '{self.resource}' ({self.old}) — {reason}"
        if self.kind is SafetyChangeKind.WIDEN_ACCESS:
            # The admitted cores, not both lists: two nearly identical lists hide the difference.
            admitted = ", ".join(_admitted_members(self.old, self.new))
            return f"hardware element '{self.resource}': {self.field} admits {admitted} — {reason}"
        if self.kind is SafetyChangeKind.DECLARE_MEASURE:
            return f"port measure '{self.resource}': {self.field} declares {self.new!r} — {reason}"
        if self.kind is SafetyChangeKind.DECLARE_COEXISTENCE:
            return f"task '{self.resource}': {self.field} declares {self.new!r} — {reason}"
        if self.kind is SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE:
            return f"partition '{self.resource}': {self.field} declares {self.new!r} — {reason}"
        if self.kind in (SafetyChangeKind.GRANT_PARTITION_TRUST, SafetyChangeKind.DECLARE_PARTITION_PROTECTION):
            return f"partition '{self.resource}': {self.field} {self.old} -> {self.new} — {reason}"
        if self.kind is SafetyChangeKind.LOWER_ALLOCATION:
            # `None` reads as `none`: the reader is looking at YAML, where the key is absent.
            spelled = "none" if self.new is None else self.new
            was = "none" if self.old is None else self.old
            return f"'{self.resource}': {self.field} {was} -> {spelled} — {reason}"
        if self.kind is SafetyChangeKind.RAISE_CAPABILITY:
            # A cleared capability reads `none`, for LOWER_ALLOCATION's reason.
            was = "none" if self.old is None else self.old
            spelled = "none" if self.new is None else self.new
            return f"hardware element '{self.resource}': {self.field} {was} -> {spelled} — {reason}"
        return f"hardware element '{self.resource}': {self.field} {self.old} -> {self.new} — {reason}"


@dataclass(frozen=True)
class SafetyVerdict:
    """Every safety change one call would make. Empty ``changes`` = not a safety act.

    ``changes`` is a tuple, not one value: one call may weaken several fields at once, and the prompt must
    describe all of what the user is agreeing to.

    :attr:`notes` is what the guard says **beside** the weakenings. A ``remove``/``create`` pairing
    that is not unique is refused the rename exemption, and a note tells the reader why a change that
    may read as a rename is asked about (only where the ambiguous group bore on a reported owner).
    After those notes, one sentence per rated consumer port a surviving connection moved onto a stub
    names the witnessing connections that fit its bound, each with the port it now ends on, and counts
    the rest; a sentence that cannot fit even one witness is left over the bound for the prompt's
    elision. It says the consumer interface became uncheckable and never that the signal is the same;
    it reaches no unconnected port, no connection renamed and edited at once, and none whose provider
    end or consumer component changed. Empty for every classifier in this module. A host renders the
    notes **below** the list, never instead of it, and a note alone is never a question:
    :attr:`is_safety_act` reads :attr:`changes` alone.
    """

    tool: str
    changes: tuple[SafetyChange, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_safety_act(self) -> bool:
        """The one accessor for "did this call weaken anything"."""
        return bool(self.changes)


#: The order weakening changes are reported in, fixed here rather than taken from the argument
#: mapping, so one call always renders the same way. A new field is appended, so a change set a
#: shipped prompt rendered renders the same way after.
_SAFETY_FIELD_ORDER = ("threshold", "size", "enforcement", "accessible", "asil_capability")


def _safety_name(args: Mapping[str, Any], tool_name: str) -> str:
    value = args.get("name")
    if not isinstance(value, str) or not value.strip():
        raise SafetyClassificationError(
            f"'{tool_name}' needs a non-empty string 'name' argument to be checked against the model, got {value!r}"
        )
    return value


def _find_hardware(hardware: Iterable[Hardware], name: str, tool_name: str) -> Hardware:
    matching = [element for element in hardware if element.name == name]
    if not matching:
        raise SafetyClassificationError(
            f"'{tool_name}' names hardware element '{name}', which does not exist in the model; "
            "its current limits cannot be read, so the call cannot be classified"
        )
    if len(matching) > 1:
        raise SafetyClassificationError(
            f"'{tool_name}' names hardware element '{name}', which is declared {len(matching)} times; "
            "which limits are current cannot be decided, so the call cannot be classified"
        )
    return matching[0]


def _render_hardware(element: Core | Memory) -> str:
    """The whole of a budgeted element, for the deletion prompt.

    A ``processor`` never reaches this: it carries no ``threshold`` and no ``enforcement``, so its
    deletion removes no check. A declared ``asil_capability`` is rendered last, because the deletion
    takes the declaration the two placement rules read with it.
    """
    figure = f"size={element.size}, " if isinstance(element, Memory) else ""
    capability = "" if element.asil_capability is None else f", asil_capability={element.asil_capability}"
    return f"kind={element.kind}, {figure}threshold={element.threshold}, enforcement={element.enforcement}{capability}"


def _new_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SafetyClassificationError(f"'threshold' must be a number to compare against the model, got {value!r}")
    return float(value)


def _new_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafetyClassificationError(f"'size' must be an int to compare against the model, got {value!r}")
    return value


def _new_enforcement(value: Any) -> str:
    if not isinstance(value, str):
        raise SafetyClassificationError(f"'enforcement' must be a string to compare against the model, got {value!r}")
    return value


def _new_accessible(value: Any) -> list[str]:
    """The whole staged ``accessible`` list; the classifier computes the difference against the model."""
    if not isinstance(value, list) or not all(isinstance(member, str) for member in value):
        raise SafetyClassificationError(
            f"'accessible' must be a list of core names to compare against the model, got {value!r}"
        )
    return value


def _new_capability(value: Any) -> str | None:
    """The staged ``asil_capability``: ``None`` for a clear, or one of the five levels.

    Anything else fails closed: a delta entry's ``after`` is not a loader-checked model element, and
    ranking an unknown string would answer "not weakening" for a typo.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value not in ASIL_LEVELS:
        raise SafetyClassificationError(
            f"'asil_capability' must be one of {', '.join(ASIL_LEVELS)} or none to compare against the model, "
            f"got {value!r}"
        )
    return value


def _refused(capability: str | None) -> frozenset[str]:
    """Every component level the two placement rules report under *capability*.

    A level strictly **above** the capability is reported (by :func:`asil_rank`); an element that
    declares no capability refuses the empty set. A move ``old -> new`` weakens exactly when
    ``_refused(new) < _refused(old)``: ``A -> C`` and ``A -> None`` weaken, ``None -> A`` and
    ``C -> A`` tighten, and ``D -> None`` is silent, because a ``D`` capability refuses nothing.
    """
    if capability is None:
        return frozenset()
    return frozenset(level for level in ASIL_LEVELS if asil_rank(level) > asil_rank(capability))


def _update_resource_changes(args: Mapping[str, Any], element: Hardware) -> tuple[SafetyChange, ...]:
    """Every weakening one ``update_resource`` call makes to *element*, in field order.

    For the four budget fields a ``None`` argument means "leave this field alone": they are required
    on the element and can never hold one. ``asil_capability`` is optional, so for it **presence
    decides**: a key absent from *args* is untouched, and a key present with ``None`` is a clear.
    """
    changes: list[SafetyChange] = []
    for field_name in _SAFETY_FIELD_ORDER:
        if field_name == "asil_capability":
            if field_name not in args:
                continue
            new_capability = _new_capability(args[field_name])
            # A `processor` declares no capability: nothing is placed on one.
            if isinstance(element, Core | Memory) and _refused(new_capability) < _refused(element.asil_capability):
                changes.append(
                    SafetyChange(
                        SafetyChangeKind.RAISE_CAPABILITY,
                        element.name,
                        "asil_capability",
                        element.asil_capability,
                        new_capability,
                    )
                )
            continue
        value = args.get(field_name)
        if value is None:
            continue
        if field_name == "threshold":
            new_threshold = _new_threshold(value)
            # A `processor` declares no threshold: there is no budget to raise.
            if isinstance(element, Core | Memory) and new_threshold > element.threshold:
                changes.append(
                    SafetyChange(
                        SafetyChangeKind.RAISE_THRESHOLD, element.name, "threshold", element.threshold, new_threshold
                    )
                )
        elif field_name == "size":
            new_size = _new_size(value)
            # A core has no memory to enlarge, and the writer rejects `size=`
            # on one outright — so such a call can change nothing at all.
            if isinstance(element, Memory) and new_size > element.size:
                changes.append(
                    SafetyChange(SafetyChangeKind.ENLARGE_POOL, element.name, "size", element.size, new_size)
                )
        elif field_name == "enforcement":
            new_enforcement = _new_enforcement(value)
            if isinstance(element, Core | Memory) and element.enforcement == "strict" and new_enforcement == "advisory":
                changes.append(
                    SafetyChange(
                        SafetyChangeKind.WEAKEN_ENFORCEMENT,
                        element.name,
                        "enforcement",
                        element.enforcement,
                        new_enforcement,
                    )
                )
        elif field_name == "accessible":
            new_accessible = _new_accessible(value)
            # Only an ADMISSION weakens: dropping a core, or reordering the same set, leaves
            # `hardware.unreachable-memory` refusing everything it refused.
            if isinstance(element, Memory) and _admitted_members(element.accessible, new_accessible):
                changes.append(
                    SafetyChange(
                        SafetyChangeKind.WIDEN_ACCESS,
                        element.name,
                        "accessible",
                        list(element.accessible),
                        new_accessible,
                    )
                )
        else:
            # Fail closed: a field added to `_SAFETY_FIELD_ORDER` without a branch of its own must not
            # pass as "not weakening".
            raise SafetyClassificationError(f"no classifier branch for safety field '{field_name}'")
    return tuple(changes)


#: Which kind a declared ``coexistence`` is, by the layer of its owner: the two kinds the contract
#: declares the field on (``field:tasks.task.coexistence`` and ``field:hardware.partition.coexistence``).
_COEXISTENCE_KINDS: Mapping[str, SafetyChangeKind] = MappingProxyType(
    {"tasks": SafetyChangeKind.DECLARE_COEXISTENCE, "hardware": SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE}
)


def classify_argument_act(element: str, field: str, old: Any, new: Any, *, layer: str = "tasks") -> SafetyVerdict:
    """The SIXTH weakening direction: a declared argument that answers a finding.

    *layer* says whose ``coexistence`` it is, a task's (``"tasks"``) or a partition's
    (``"hardware"``), and chooses between :data:`SafetyChangeKind.DECLARE_COEXISTENCE` and
    :data:`SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE`; ``measures`` reads no *layer*, because
    only a port declares one.

    - ``coexistence`` is the **value** shape: ``""`` -> a text is the weakening, because it makes
      ``safety.task-mixed-asil`` (for a partition, ``safety.component-below-partition``) stop
      refusing. Text -> ``""`` and text -> another text are not. A task **created** carrying a text
      arrives as ``""`` -> that text, so an argument does not escape the question by arriving inside
      its owner; a partition's arrives with ``""`` as *old* also when a standing text goes live
      because the partition became mixed.
    - ``measures`` is the **operation** shape and arrives already decided: only a ``create`` at an
      entry's key, or a port arriving with a non-empty map, reaches here, so *old* is ``None`` and
      every call is a weakening. The ``remove`` of the whole ``require`` port that carried one is
      :func:`classify_allocation_act`'s.

    Raises:
        SafetyClassificationError: for a field this function has no branch for, or a ``coexistence``
            of a *layer* that declares none. Never an empty verdict: an unknown field fails closed.
    """
    if field == "coexistence":
        kind = _COEXISTENCE_KINDS.get(layer)
        if kind is None:
            raise SafetyClassificationError(f"no classifier branch for a coexistence argument in layer '{layer}'")
        if isinstance(old, str) and isinstance(new, str) and not old and new:
            return SafetyVerdict(element, (SafetyChange(kind, element, field, old, new),))
        return SafetyVerdict(element)
    if field == "measures":
        return SafetyVerdict(element, (SafetyChange(SafetyChangeKind.DECLARE_MEASURE, element, field, old, new),))
    raise SafetyClassificationError(f"no classifier branch for declared-argument field '{field}'")


def classify_allocation_act(element: str, field: str, old: Any, new: Any) -> SafetyVerdict:
    """The SEVENTH: the level a BEARER declares, lowered or removed.

    Each shape makes a rule stop refusing and creates no error finding in exchange:

    - a requirement's or a data element's ``asil`` lowered or ``None``;
    - a ``satisfies`` member removed from a component, a port or a runnable: *field* ``satisfies``,
      *new* ``None``, *old* the member (``REQ_1@1``), *element* the site (``Ctl``,
      ``Ctl.ports[pIn]``, ``Ctl.runnables[Run10ms]``); a confirmation edit never arrives;
    - a component's own ``asil``, compared as its effective level (its declaration, else what its
      requirements allocate);
    - a bearer removed whole, *old* its level and *new* ``None`` (both ``None`` is no weakening);
    - ``measures`` and ``coexistence`` (:data:`_WITHDRAWN_WHOLE`), already decided by the caller;
    - a partition's ``asil`` lowered, cleared or removed whole, and an argued mixed partition removed,
      as ``coexistence``; *element* is the partition's name;
    - ``interface``: a rated consumer's ``require`` port re-pointed from an interface carrying a
      rated data element to an external interface stub, *old* and *new* the two interface names.

    Raising a level is never one. Not reached: raising a data element's level can close
    ``safety.consumer-above-element``, and a partition's ``safety.component-above-partition`` where
    the core and memories are unrated, with no question asked. A requirement left satisfied by
    nothing is a warning, so the classification does not depend on another allocation remaining.

    Raises:
        SafetyClassificationError: for a field this function has no branch for.
    """
    if field in _WITHDRAWN_WHOLE:
        return SafetyVerdict(element, (SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, element, field, old, new),))
    if field != "asil":
        raise SafetyClassificationError(f"no classifier branch for allocation field '{field}'")
    # `None` is BELOW every declared level: removing a level is the weakening. TWO `None`s are
    # nothing to withdraw (an unlevelled bearer removed); the differ emits no entry for an unchanged
    # value, so this arm is reached only through a whole-element removal.
    if old is None and new is None:
        return SafetyVerdict(element)
    if new is None:
        return SafetyVerdict(element, (SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, element, field, old, new),))
    if old is None:
        return SafetyVerdict(element)
    if asil_rank(str(new)) < asil_rank(str(old)):
        return SafetyVerdict(element, (SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, element, field, old, new),))
    return SafetyVerdict(element)


#: The three classes a partition's ``trust`` may hold (``field:hardware.partition.trust``), read off
#: the schema's own ``Literal``; :func:`classify_trust_act` fails closed on any other value.
_TRUST_CLASSES: tuple[str, ...] = get_args(Partition.model_fields["trust"].annotation)

#: The one class the OS leaves unrestricted for memory and the two trust rules read.
_TRUSTED: str = "trusted"


def classify_trust_act(element: str, old: Any, new: Any) -> SafetyVerdict:
    """The partition's ``trust`` moved across ``trusted``: the ninth direction and the sixth's fourth kind.

    Only a movement that crosses ``trusted`` is asked about:

    - *old* not ``trusted``, *new* ``trusted`` -- :data:`SafetyChangeKind.GRANT_PARTITION_TRUST`; a
      partition **created** ``trusted`` arrives as ``non_trusted`` -> ``trusted``;
    - *old* ``trusted``, *new* not -- :data:`SafetyChangeKind.DECLARE_PARTITION_PROTECTION`;
    - ``non_trusted`` <-> ``trusted_with_protection`` -- both confined for memory, neither read by a
      rule: an empty verdict.

    *element* is the partition's name. A **removed** partition never reaches here.

    Raises:
        SafetyClassificationError: *old* or *new* is not one of the three classes.
    """
    for value in (old, new):
        if value not in _TRUST_CLASSES:
            raise SafetyClassificationError(f"no classifier branch for the partition trust class {value!r}")
    if new == _TRUSTED and old != _TRUSTED:
        kind = SafetyChangeKind.GRANT_PARTITION_TRUST
    elif old == _TRUSTED and new != _TRUSTED:
        kind = SafetyChangeKind.DECLARE_PARTITION_PROTECTION
    else:
        return SafetyVerdict(element)
    return SafetyVerdict(element, (SafetyChange(kind, element, "trust", old, new),))


def classify_safety_act(
    tool_name: str,
    args: Mapping[str, Any],
    hardware: Iterable[Hardware],
) -> SafetyVerdict:
    """Every way *tool_name* would weaken a HARDWARE check, given the model.

    The hardware classifier alone: it dispatches on ``update_resource`` and ``delete_element`` (the
    classifier's vocabulary, not MCP tools) and returns only the six ``hardware`` kinds. *hardware* is
    the session model's current hardware layer, so the verdict's old values are the ones the user
    has. The guard is always on, independent of any authorization scope, and stateless; an empty
    :attr:`SafetyVerdict.changes` means "not a safety act".

    Only two call shapes weaken a check of these six: ``update_resource`` (raising ``threshold``,
    enlarging a memory's ``size``, ``strict`` -> ``advisory``, admitting a core to a memory's
    ``accessible``, raising an ``asil_capability`` or clearing one below ``D``) and ``delete_element``
    on a core or a memory. Everything else returns an empty verdict: adding a core or a memory,
    tightening a limit, narrowing ``accessible``, updating or deleting a ``processor`` or a
    ``partition`` (neither carries a budget; a partition's level, argument and trust class are the
    editing guard's comparison of two models), and every consumer budget. A lowered consumer budget is
    model content, visible in the reviewed delta and in ``swcraft resource-report``; the guard protects
    the check itself.

    What the capability arm does **not** reach: any re-point of a task's ``partition`` or of a
    partition's ``core``, and any move of a budget, can close a placement finding too, and none
    reaches this function, because none edits a core's or a memory's declaration.

    Raises:
        SafetyClassificationError: the named hardware element is missing or ambiguous, the name
            argument is unusable, ``delete_element`` carries no usable ``element_type``, an argument's
            type makes the comparison impossible, or an ``asil_capability`` is neither one of the five
            levels nor ``None``. Never answered with an empty verdict.
    """
    if tool_name == "update_resource":
        name = _safety_name(args, tool_name)
        element = _find_hardware(hardware, name, tool_name)
        return SafetyVerdict(tool_name, _update_resource_changes(args, element))

    if tool_name == "delete_element":
        element_type = args.get("element_type")
        if not isinstance(element_type, str) or element_type not in DELETE_ELEMENT_LAYERS:
            raise SafetyClassificationError(
                f"'delete_element' cannot be classified for element_type {element_type!r}; "
                f"supported: {', '.join(sorted(DELETE_ELEMENT_LAYERS))}"
            )
        if element_type != "hardware":
            # Deleting an swc/requirement/connector removes no BUDGET check; the classifiers beside
            # this one answer what such a deletion withdraws of a safety allocation.
            return SafetyVerdict(tool_name)
        name = _safety_name(args, tool_name)
        element = _find_hardware(hardware, name, tool_name)
        if not isinstance(element, Core | Memory):
            # A `processor` or a `partition` carries no budget; a partition's removal is the editing
            # guard's comparison's question.
            return SafetyVerdict(tool_name)
        return SafetyVerdict(
            tool_name,
            (
                SafetyChange(
                    SafetyChangeKind.DELETE_RESOURCE,
                    element.name,
                    "",
                    _render_hardware(element),
                    None,
                ),
            ),
        )

    return SafetyVerdict(tool_name)
