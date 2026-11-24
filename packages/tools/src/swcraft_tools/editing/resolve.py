"""Names resolved across the model: ``find``, the search instead of the map.

At most :data:`CANDIDATES_PER_NAME` per name and
:data:`~swcraft_tools.editing.protocol.FIND_CANDIDATES_TOTAL` in all. A name is
compared with every element identity and the members of :data:`MEMBER_COLLECTIONS`
in four tiers, best first: ``exact``; ``case``; ``word`` (every word of the name
equals or starts a **distinct** word of the candidate, never a substring); and
``near``, within :data:`NEAR_RATIO`. Within a tier an element precedes a member,
then the shorter name, then the alphabetical one.

:func:`find_text` matches a phrase's words by the ``word`` rule against each
requirement's ``id``, ``title`` and ``description``, in ``id`` order. :func:`nearest`
answers the nearest names for a ``broken-reference`` or an unresolved address,
restricted to where the reference points, counting the change set's own ``create``
operations.

**What it does not reach:** a requirement is found by name only by its ``id``;
collections outside :data:`MEMBER_COLLECTIONS` are not searched by ``find``;
``near`` compares spellings, never meanings. For the nearest names, an element or
member the same change set removes is still offered; what a ``create`` states
below an element's own collections is not searched; a non-name member step or an
address not starting with a field gets no sentence; ambiguous owners widen the
search; the :data:`UNSUGGESTED_GROUPS` get none.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.protocol import CANDIDATE_TIERS, CANDIDATES_PER_NAME, FIND_TEXT_MAX, candidates_per_name
from swcraft_tools.trace import parse_element_reference, parse_link

__all__ = [
    "CANDIDATES_PER_NAME",
    "MEMBER_COLLECTIONS",
    "NEAR_RATIO",
    "REFERENCE_POOLS",
    "ROLE_FIELD_LAYERS",
    "ROLE_LAYERS",
    "UNSUGGESTED_GROUPS",
    "Candidate",
    "Role",
    "address_candidates",
    "append_sentence",
    "created_names",
    "find_text",
    "nearest",
    "reference_candidates",
    "resolve_names",
]

#: The least :class:`difflib.SequenceMatcher` ratio a ``near`` candidate has.
NEAR_RATIO = 0.75

#: The member collections searched, per layer: ``(field, member noun)``.
MEMBER_COLLECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "swc": (("ports", "port"), ("runnables", "runnable")),
    "interfaces": (("data_elements", "data element"), ("operations", "operation")),
    "compositions": (("ports", "port"), ("members", "member"), ("delegations", "delegation")),
}

#: The member collections a ``broken-reference`` repair searches and ``find`` does not,
#: searched only when a pool of :data:`REFERENCE_POOLS` names them.
REPAIR_COLLECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "swc": (("variables", "variable"), ("exclusive_areas", "exclusive area")),
}

_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
#: The most characters of an identity line; a longer one is cut with "…".
_IDENTITY_CHARS = 72


@dataclass(frozen=True)
class Candidate:
    """One place a name may mean: its layer, its address and its identity line."""

    layer: str
    element: str
    name: str
    #: ``None`` for an element; ``(field, noun)`` for a member of one of its collections.
    collection: tuple[str, str] | None
    identity: str

    def address(self) -> dict[str, Any]:
        address: dict[str, Any] = {"layer": self.layer, "element": self.element}
        if self.collection is not None:
            address["path"] = [{"field": self.collection[0]}, {"member": self.name}]
        return address

    def to_json(self, tier: str) -> dict[str, Any]:
        return {"address": self.address(), "tier": tier, "identity": self.identity}


def _words(text: str) -> list[str]:
    return [word.lower() for word in _WORDS.findall(text)]


def _words_match(query_words: Sequence[str], name_words: Sequence[str]) -> bool:
    """Whether every query word equals, or is a prefix of, a **distinct** name word.

    A matching, not a greedy pass in the query's order, which could pair ``br``
    with the one word a later ``brake`` needed. A query with no word matches nothing.
    """
    return _word_pairing(query_words, name_words) is not None


def _word_pairing(query_words: Sequence[str], name_words: Sequence[str]) -> list[str] | None:
    """The name words paired with the query words, in the query's order; or ``None``.

    The name words each query word may take are nested or disjoint (prefixes),
    so pairing the query words with the fewest fits first, each with the first
    free word, is a maximum matching: no augmenting-path search, no recursion a
    thousand-word query could exhaust, and a pass linear in the pairs.
    """
    if not query_words or len(query_words) > len(name_words):
        return None
    fits = [[index for index, word in enumerate(name_words) if word.startswith(query)] for query in query_words]
    taken: set[int] = set()
    paired: dict[int, int] = {}
    for query in sorted(range(len(query_words)), key=lambda query: (len(fits[query]), query)):
        index = next((index for index in fits[query] if index not in taken), None)
        if index is None:
            return None
        taken.add(index)
        paired[query] = index
    return [name_words[paired[query]] for query in range(len(query_words))]


def _scored_tier(query: str, name: str) -> tuple[str, float] | None:
    """The best tier at which *query* names *name* and, in ``near``, its ratio (else ``0.0``); or ``None``."""
    if query == name:
        return ("exact", 0.0)
    if query.lower() == name.lower():
        return ("case", 0.0)
    if _words_match(_words(query), _words(name)):
        return ("word", 0.0)
    matcher = difflib.SequenceMatcher(None, query.lower(), name.lower())
    if matcher.real_quick_ratio() >= NEAR_RATIO and matcher.quick_ratio() >= NEAR_RATIO:
        ratio = matcher.ratio()
        if ratio >= NEAR_RATIO:
            return ("near", ratio)
    return None


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _IDENTITY_CHARS else text[: _IDENTITY_CHARS - 1] + "…"


def _element_identity(layer: str, name: str, element: Any) -> str:
    facts: list[str] = []
    for attribute in ("type", "kind"):
        value = getattr(element, attribute, None)
        if isinstance(value, str) and value:
            facts.append(value)
            break
    for field, noun in MEMBER_COLLECTIONS.get(layer, ()):
        members = getattr(element, field, None)
        if members:
            facts.append(f"{len(members)} {noun}{'' if len(members) == 1 else 's'}")
    line = f"{layer} {name}"
    if facts:
        line += f" ({', '.join(facts)})"
    return _with_text(line, element)


def _with_text(line: str, element: Any) -> str:
    """*line* with the element's title (else its description) after a dash, when it has one, cut at 72 characters."""
    text = getattr(element, "title", "") or getattr(element, "description", "")
    if isinstance(text, str) and text.strip():
        line += f" — {text}"
    return _short(line)


def _requirement_identity(identity: str, element: Any) -> str:
    """A text search's identity line: the id and the title (else the description), cut at 72 characters.

    No other field is named, the path-shaped ``document`` least of all.
    """
    return _with_text(identity, element)


def _member_identity(layer: str, element: str, noun: str, member: Any) -> str:
    facts = [
        str(value)
        for value in (
            getattr(member, attribute, None) for attribute in ("direction", "interface", "type", "composition")
        )
        if isinstance(value, str) and value
    ]
    line = f"{noun} {member.name} of {layer} {element}"
    if facts:
        line += f" ({', '.join(facts)})"
    return _short(line)


def _layer_elements(snapshot: Any, layer: str, registry: reg.Registry) -> Iterator[tuple[str, Any]]:
    """The elements of *layer* with their identities, read by layer name out of ``snapshot.state``.

    By name, never off an adapter attribute: a layer with no
    ``snapshotAttributes`` row has none, so ``read`` would list elements a
    search by attribute could not find. A layer the state does not carry holds nothing.
    """
    descriptor = registry.layer(layer)
    state = snapshot.state
    for element in state[descriptor.name] if descriptor.name in state else ():
        identity = getattr(element, descriptor.identity_field, None)
        if isinstance(identity, str):
            yield identity, element


def _pool(
    snapshot: Any,
    registry: reg.Registry,
    *,
    layers: Iterable[str] | None = None,
    members: Iterable[tuple[str, str]] | None = None,
    owner: str | None = None,
    kind: str | None = None,
) -> Iterator[Candidate]:
    """Every candidate of the searched layers (``layers``) and member collections (``members``).

    Both ``None`` search everything; naming either searches only what it names.
    ``owner`` narrows member collections to one element's; ``kind`` narrows the
    elements to that ``kind`` (a hardware core, partition or memory).
    """
    everything = layers is None and members is None
    element_layers = set(registry.layer_names if everything else (layers or ()))
    member_fields = (
        {(layer, field) for layer, fields in MEMBER_COLLECTIONS.items() for field, _ in fields}
        if everything
        else set(members or ())
    )
    for layer in registry.layer_names:
        searched = [
            (field, noun)
            for field, noun in (*MEMBER_COLLECTIONS.get(layer, ()), *REPAIR_COLLECTIONS.get(layer, ()))
            if (layer, field) in member_fields
        ]
        if layer not in element_layers and not searched:
            continue
        for name, element in _layer_elements(snapshot, layer, registry):
            if kind is not None and getattr(element, "kind", None) != kind:
                continue
            if layer in element_layers:
                yield Candidate(layer, name, name, None, _element_identity(layer, name, element))
            if owner is not None and name != owner:
                continue
            for field, noun in searched:
                for member in getattr(element, field, None) or ():
                    member_name = getattr(member, "name", None)
                    if isinstance(member_name, str):
                        yield Candidate(
                            layer, name, member_name, (field, noun), _member_identity(layer, name, noun, member)
                        )


def _ranked(query: str, pool: Iterable[Candidate]) -> list[tuple[str, Candidate]]:
    scored = []
    for candidate in pool:
        found = _scored_tier(query, candidate.name)
        if found is not None:
            # Within `near`, the closer spelling first; every other tier is a match as such.
            tier, closeness = found
            scored.append((tier, -closeness, candidate))
    scored.sort(
        key=lambda hit: (
            CANDIDATE_TIERS.index(hit[0]),
            hit[1],
            hit[2].collection is not None,
            len(hit[2].name),
            hit[2].name,
            hit[2].layer,
            hit[2].element,
        )
    )
    return [(tier, candidate) for tier, _, candidate in scored]


def resolve_names(snapshot: Any, names: Sequence[str], registry: reg.Registry) -> list[dict[str, Any]]:
    """One entry per name, in order: its candidates and how many more matched.

    Up to five candidates a name, sixteen in all (:func:`candidates_per_name`).
    """
    pool = list(_pool(snapshot, registry))
    per_name = candidates_per_name(len(names))
    answers = []
    for name in names:
        hits = _ranked(name, pool)
        entry: dict[str, Any] = {"name": name}
        if hits:
            entry["candidates"] = [candidate.to_json(tier) for tier, candidate in hits[:per_name]]
            if len(hits) > per_name:
                entry["more"] = len(hits) - per_name
        else:
            entry["candidates"] = "none"
        answers.append(entry)
    return answers


def find_text(snapshot: Any, text: str, registry: reg.Registry) -> tuple[list[dict[str, Any]] | str, int | None]:
    """The requirements whose ``id``, ``title`` and ``description`` hold every word of *text*.

    ``(candidates, more)``: at most :data:`FIND_TEXT_MAX` candidates in ``id``
    order — each the requirement's address, its identity line and the matched
    words of its text — or ``"none"``, and how many more matched (``None`` when
    none did). A stub is searched like any requirement. A word repeated in the
    query must be repeated in the text. An ``id`` two files declare answers twice.
    """
    query = _words(text)
    hits = []
    for identity, element in _layer_elements(snapshot, "requirements", registry):
        words = _words(identity)
        for field in ("title", "description"):
            value = getattr(element, field, None)
            if isinstance(value, str):
                words.extend(_words(value))
        paired = _word_pairing(query, words)
        if paired is not None:
            hits.append((identity, element, paired))
    if not hits:
        return "none", None
    hits.sort(key=lambda hit: hit[0])
    candidates = [
        {
            "address": {"layer": "requirements", "element": identity},
            "identity": _requirement_identity(identity, element),
            "words": paired,
        }
        for identity, element, paired in hits[:FIND_TEXT_MAX]
    ]
    return candidates, (len(hits) - FIND_TEXT_MAX if len(hits) > FIND_TEXT_MAX else None)


def nearest(
    snapshot: Any,
    target: str,
    registry: reg.Registry,
    *,
    layers: Iterable[str] | None = None,
    members: Iterable[tuple[str, str]] | None = None,
    owner: str | None = None,
    kind: str | None = None,
    extra: Iterable[Candidate] = (),
) -> list[str]:
    """The names nearest to *target* in the pool named, best first, at most five; never *target* itself.

    *extra* joins the pool: names a change set's own ``create`` states that the
    snapshot does not hold yet.
    """
    pool = [*_pool(snapshot, registry, layers=layers, members=members, owner=owner, kind=kind), *extra]
    hits = _ranked(target, pool)
    seen: dict[str, None] = {}
    for _, candidate in hits:
        if candidate.name != target:
            seen.setdefault(candidate.name, None)
    return list(seen)[:CANDIDATES_PER_NAME]


def created_names(
    operations: Iterable[Any],
) -> tuple[frozenset[tuple[str, str]], dict[tuple[str, str, str], tuple[str, ...]]]:
    """What a change set's ``create`` operations state that the baseline snapshot does not hold yet.

    ``(elements, members)``: every ``(layer, element)`` a whole-element create
    states, and per ``(layer, element, field)`` the member names a create states
    there — in a created element's value or created at their own address.
    """
    elements: set[tuple[str, str]] = set()
    members: dict[tuple[str, str, str], list[str]] = {}
    for operation in operations:
        if getattr(operation, "operation", None) != "create":
            continue
        address = operation.address
        if address.element is None:
            continue
        steps = list(address.path)
        if not steps:
            elements.add((address.layer, address.element))
            if isinstance(operation.value, Mapping):
                for field, value in operation.value.items():
                    if isinstance(value, list):
                        named = [item.get("name") for item in value if isinstance(item, Mapping)]
                        members.setdefault((address.layer, address.element, field), []).extend(
                            name for name in named if isinstance(name, str)
                        )
        elif len(steps) == 2 and isinstance(getattr(steps[1], "member", None), str):
            field = getattr(steps[0], "field", None)
            if isinstance(field, str):
                members.setdefault((address.layer, address.element, field), []).append(steps[1].member)
    return frozenset(elements), {key: tuple(names) for key, names in members.items()}


def append_sentence(message: str, sentence: str) -> str:
    """*message* with the nearest-names *sentence* after it.

    One space after a message that already closes (``.``, ``;``, ``:``), a full
    stop and a space otherwise; *message* unchanged when *sentence* is empty.
    """
    if not sentence:
        return message
    closed = message.rstrip()
    separator = " " if closed.endswith((".", ";", ":")) else ". "
    return f"{closed}{separator}{sentence}"


def address_candidates(
    snapshot: Any,
    address: Any,
    registry: reg.Registry,
    *,
    operations: Iterable[Any] = (),
) -> str:
    """A sentence naming the nearest names for an address that does not resolve, or ``""``.

    The element step first, then the ``field``/``member`` steps in order as deep
    as the address goes: the first member its collection does not hold is
    searched in that collection alone. ``""`` when nothing is near, when a
    member identity on the way is not a name, when the address has no element or
    does not start with a field step, or when a member on the way is one the
    change set creates.

    *operations* are the change set's (none for a read); what their ``create``
    operations state counts as present, so a created element is never named
    missing and created names join the candidates. **What it does not reach:**
    an element or member a ``remove`` of the same set takes away is still
    offered, and an ``update`` that renames nothing is not read.
    """
    if address.layer not in registry.layer_names or address.element is None:
        return ""
    operations = tuple(operations)
    created_elements, created_members = created_names(operations)
    elements = dict(_layer_elements(snapshot, address.layer, registry))
    created = (address.layer, address.element) in created_elements
    if address.element not in elements and not created:
        extra = [
            Candidate(layer, element, element, None, "")
            for layer, element in sorted(created_elements)
            if layer == address.layer
        ]
        names = nearest(snapshot, address.element, registry, layers=(address.layer,), extra=extra)
        return _sentence(address.layer, address.element, names) if names else ""
    steps = list(address.path)
    if len(steps) < 2 or getattr(steps[0], "field", None) is None:
        return ""
    field, identity = steps[0].field, getattr(steps[1], "member", None)
    if not isinstance(identity, str):
        return ""
    stated = created_members.get((address.layer, address.element, field), ())
    cursor = elements.get(address.element)
    members = list(getattr(cursor, field, None) or ())
    held = [getattr(member, "name", None) for member in members]
    held.extend(stated)
    if identity not in held:
        nouns = dict(MEMBER_COLLECTIONS.get(address.layer, ()))
        if field in nouns:
            extra = [Candidate(address.layer, address.element, name, (field, nouns[field]), "") for name in stated]
            names = nearest(
                snapshot, identity, registry, members=((address.layer, field),), owner=address.element, extra=extra
            )
        else:
            names = _nearest_among(identity, held)
        return _sentence(f"{address.element}.{field}", identity, names) if names else ""
    # The member is there: walk on through the steps below it, each collection
    # searched alone. A member the baseline does not hold (one the set creates)
    # ends the walk, and so does a collection a create of the set reaches into.
    where = f"{address.element}.{field}"
    depth = 2
    while len(steps) >= depth + 2 and getattr(steps[depth], "field", None) is not None:
        cursor = next((member for member in members if getattr(member, "name", None) == identity), None)
        field, identity = steps[depth].field, getattr(steps[depth + 1], "member", None)
        if cursor is None or not isinstance(identity, str) or _created_under(operations, address, steps[: depth + 1]):
            return ""
        where = f"{where}.{cursor.name}.{field}"
        members = list(getattr(cursor, field, None) or ())
        held = [getattr(member, "name", None) for member in members]
        if identity not in held:
            names = _nearest_among(identity, held)
            return _sentence(where, identity, names) if names else ""
        depth += 2
    return ""


def _nearest_among(target: str, held: Iterable[Any]) -> list[str]:
    """The names of *held* within :data:`NEAR_RATIO` of *target*, closest first, at most five.

    Spelling alone, for a collection the resolver's tiers do not index.
    """
    names = [name for name in dict.fromkeys(held) if isinstance(name, str)]
    return difflib.get_close_matches(target, names, n=CANDIDATES_PER_NAME, cutoff=NEAR_RATIO)


def _created_under(operations: Sequence[Any], address: Any, prefix: Sequence[Any]) -> bool:
    """Whether a ``create`` of the set addresses something inside *prefix* of *address*'s element."""
    return any(
        getattr(operation, "operation", None) == "create"
        and operation.address.layer == address.layer
        and operation.address.element == address.element
        and list(operation.address.path)[: len(prefix)] == list(prefix)
        for operation in operations
    )


def _sentence(where: str, target: str, names: Sequence[str]) -> str:
    if not names:
        return f"{where} holds no '{target}' and nothing near it."
    return f"{where} holds no '{target}'; nearest: {', '.join(names)}."


#: A ``broken-reference`` group (``diagnosis.RULE_CODES``; a rule with no group by
#: its rule id) -> where its dangling name should have pointed: ``(layers, member
#: collections)``. Every such group is here or in :data:`UNSUGGESTED_GROUPS`; a group
#: in neither is searched across everything the resolver reaches.
REFERENCE_POOLS: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    "memory": (("hardware",), ()),
    "core": (("hardware",), ()),
    "partition": (("hardware",), ()),
    "interface": (("interfaces",), ()),
    "requirement": (("requirements",), ()),
    "trace.derived-from-unknown-target": (("requirements",), ()),
    "mapping-set": (("mapping_sets",), ()),
    "data-type": (("application_types", "implementation_types", "base_types"), ()),
    "connector-swc": (("swc",), ()),
    "composition-member-type": (("swc",), ()),
    "task-mapping-swc": (("swc",), ()),
    "sequence-step-swc": (("swc",), ()),
    "connector-composition": (("compositions",), ()),
    "composition-member-composition": (("compositions",), ()),
    "sequence-task": (("tasks",), ()),
    "connector-port": ((), (("swc", "ports"), ("compositions", "ports"))),
    "composition-delegation-port": ((), (("swc", "ports"), ("compositions", "ports"))),
    "composition-delegation-member": ((), (("compositions", "members"),)),
    "task-mapping-runnable": ((), (("swc", "runnables"),)),
    "sequence-step-runnable": ((), (("swc", "runnables"),)),
    "access-port": ((), (("swc", "ports"),)),
    "operation-event-port": ((), (("swc", "ports"),)),
    "access-element": ((), (("interfaces", "data_elements"),)),
    "access-operation": ((), (("interfaces", "operations"),)),
    "operation-event-operation": ((), (("interfaces", "operations"),)),
    "data-event-port": ((), (("swc", "ports"),)),
    "data-event-element": ((), (("interfaces", "data_elements"),)),
    "access-variable": ((), (("swc", "variables"),)),
    "access-area": ((), (("swc", "exclusive_areas"),)),
}

#: The reference roles of the ``data-type`` group, by rule: the one layer each
#: resolves its name in. The group shares one repair but not one namespace: a
#: value type's ``base`` resolves among the base types alone, so a group-wide
#: pool would offer types the field cannot take.
ROLE_LAYERS: dict[str, tuple[str, ...]] = {
    "data-type.value-type-undeclared-base": ("base_types",),
    "data-type.reference-type-undeclared-base": ("implementation_types",),
    "data-type.record-field-undeclared-type": ("application_types",),
    "data-type.array-undeclared-element-type": ("application_types",),
    "data-type.implementation-element-undeclared-type": ("implementation_types",),
    "data-type.implementation-array-undeclared-element-type": ("implementation_types",),
    "data-type.pointer-undeclared-target": ("implementation_types",),
    "mapping-set.unknown-application": ("application_types",),
    "mapping-set.unknown-implementation": ("implementation_types",),
}

#: The three prototype rules (a data element's, an argument's, a variable's type), whose layer
#: is the one of the field that is set: ``rule -> {field -> layer}``.
ROLE_FIELD_LAYERS: dict[str, dict[str, tuple[str, ...]]] = {
    rule: {"application_type": ("application_types",), "implementation_type": ("implementation_types",)}
    for rule in (
        "interface.data-element-undeclared-type",
        "interface.argument-undeclared-type",
        "swc.variable.undeclared-type",
    )
}


#: One finding a dangling name stands for: its rule, the last field step of its
#: subject (the field that names the target) and the element that subject sits
#: on (``diagnosis.diagnose`` builds it).
Role = tuple[str, str | None, str | None]


def _role_layers(roles: Iterable[Role]) -> tuple[str, ...] | None:
    """The layers the *roles* of one dangling name resolve in, in first-named order.

    ``None`` when there is no role or one of them has no layer of its own here,
    and the group's pool stands.
    """
    layers: dict[str, None] = {}
    roles = tuple(roles)
    for rule, field, _ in roles:
        found = ROLE_LAYERS.get(rule) or ROLE_FIELD_LAYERS.get(rule, {}).get(field or "")
        if not found:
            return None
        layers.update(dict.fromkeys(found))
    return tuple(layers) or None


#: The hardware groups, narrowed to the elements of their ``kind``: a missing
#: core is never "nearest" to a partition or a memory.
_POOL_KINDS = frozenset({"memory", "core", "partition"})

#: The ``broken-reference`` groups that get no nearest names: what is missing is
#: not a name. A sequence step not mapped to the sequence's task names a
#: component and a runnable that exist; the task's entry is what is absent. A
#: call-returned event's asynchronous calls are nested, pair-identified entries
#: no pool row expresses.
UNSUGGESTED_GROUPS = frozenset({"sequence-step-mapping", "call-returned-event-call"})


#: The owner-less member groups a delegation names, looked up on the finding's own
#: composition: its outer ``port`` among that composition's ports, its ``member`` among its members.
_OWN_COMPOSITION_POOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "composition-delegation-port": (("compositions", "ports"),),
    "composition-delegation-member": (("compositions", "members"),),
}

#: The port groups whose ``Owner.port`` target names a TYPE — a component or a
#: composition, which may share a name across the two layers — and the layer
#: whose elements refer: a connector's endpoint, a delegation's inner end.
_TYPED_PORT_REFERRERS: dict[str, str] = {
    "connector-port": "connectors",
    "composition-delegation-port": "compositions",
}

#: The field that declares an endpoint's or a member's type -> the layer it names.
_TYPE_FIELD_LAYERS: tuple[tuple[str, str], ...] = (("swc", "swc"), ("type", "swc"), ("composition", "compositions"))


def _declared_type_ports(
    snapshot: Any, registry: reg.Registry, group: str, owner: str, port: str, roles: Iterable[Role]
) -> tuple[tuple[str, str], ...]:
    """The port collections the referring ends' **declared type** names *owner* in, or ``()``.

    The referrers are the elements the *roles* name: a connector's ``from`` and
    ``to``, a composition's delegations through the member each names; an end
    counts when it names *owner* and the dangling *port*. ``()`` (the group's
    two-layer pool stands) when no referrer or end matches.
    """
    named = {element for _, _, element in roles if element}
    layers: dict[str, None] = {}
    for identity, element in _layer_elements(snapshot, _TYPED_PORT_REFERRERS[group], registry):
        if identity not in named:
            continue
        if group == "connector-port":
            ends = [
                (end, getattr(end, "port", None))
                for end in (getattr(element, "from_", None), getattr(element, "to", None))
            ]
        else:
            by_name = {getattr(member, "name", None): member for member in getattr(element, "members", None) or ()}
            ends = [
                (by_name.get(each.member), each.member_port) for each in getattr(element, "delegations", None) or ()
            ]
        for typed, end_port in ends:
            if typed is None or end_port != port:
                continue
            layers.update({layer: None for field, layer in _TYPE_FIELD_LAYERS if getattr(typed, field, None) == owner})
    return tuple((layer, "ports") for layer in layers)


def reference_candidates(
    snapshot: Any,
    group: str,
    target: str,
    registry: reg.Registry,
    roles: Iterable[Role] = (),
) -> str:
    """The sentence naming the nearest names to a dangling *target* of a ``broken-reference`` *group*.

    A member group's ``Owner.name`` target is searched in that owner's
    collection alone (a ``task: `` prefix set aside); a typed owner only in the
    layer the referring end declares it in (:func:`_declared_type_ports`). A
    target with no owner part (a delegation's outer ``port`` or ``member``)
    resolves on the composition the *roles* name, or on every composition when
    they name several. An element group's target is taken whole (a requirement
    id may hold a ``.``) less a version (``REQ@2``); a ``derived_from`` member
    in the element form (``swc:Name``) is searched in the layer its token names.
    *roles* — ``(rule, field, element)`` of each finding — narrow an element
    group to the layers those roles resolve in, so the sentence never says a
    layer the referring field does not read holds no such name.
    """
    if group in UNSUGGESTED_GROUPS:
        return ""
    roles = tuple(roles)
    layers, members = REFERENCE_POOLS.get(group, (None, None))
    if members:
        dotted = target.split(": ", 1)[-1] if ": " in target else target
        owner, _, name = dotted.rpartition(".")
        if not name:
            return ""
        if not owner and group in _OWN_COMPOSITION_POOLS:
            members = _OWN_COMPOSITION_POOLS[group]
            owners = {element for _, _, element in roles if element}
            owner = owners.pop() if len(owners) == 1 else ""
        elif owner and group in _TYPED_PORT_REFERRERS:
            # `Owner` may be a component or a composition: the referring endpoint says which.
            members = _declared_type_ports(snapshot, registry, group, owner, name, roles) or members
        names = nearest(snapshot, name, registry, members=members, owner=owner or None)
        if owner:
            where = " or ".join(f"{owner}.{field}" for field in dict.fromkeys(field for _, field in members))
            return _sentence(where, name, names)
        where = " or ".join(f"{layer}.{field}" for layer, field in members)
        return f"nearest in {where}: {', '.join(names)}." if names else ""
    layers = _role_layers(roles) or layers
    kind = group if group in _POOL_KINDS else None
    reference = parse_element_reference(target) if group == "trace.derived-from-unknown-target" else None
    if reference is not None:
        layers, name = (reference[0],), reference[1]
    else:
        name = parse_link(target).id if layers == ("requirements",) else target
    if not name:
        return ""
    names = nearest(snapshot, name, registry, layers=layers, kind=kind)
    where = " or ".join(layers or ()) or "the model"
    if kind is not None:
        where = f"{where} ({kind})"
    return _sentence(where, name, names)
