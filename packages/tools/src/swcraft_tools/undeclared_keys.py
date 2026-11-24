"""Which keys of an element file no field of its kind declares.

A **pure predicate** of the kernel: given a file's text, the document
:func:`~swcraft_tools.yaml_load.load_yaml_document` constructs from it and its
layer, :func:`undeclared_keys` returns every key the contract declares no field
for, with the kind whose mapping holds it and a locator down to that mapping. The
only message text it holds is :func:`unknown_key_clauses`.

The declared key set is :attr:`~swcraft_tools.model_registry.KindFacts.file_fields`,
the property the editor schemas are rendered from. The walk descends into a
declared key whose field names an ``element_kind`` or a list collection with
``member_kinds``. A key that is not a string (libyaml loads YAML 1.1, so ``yes:``
arrives as ``True``) is reported by its scalar's text from the source nodes; an
empty key gives an empty ``key``. The closed kinds (the data type kinds and the
stubs) refuse a stray key when the model is read; every other kind loads with the
key dropped and ``check-model`` reports it through this predicate.

:func:`undeclared_manifest_keys` walks the manifest with the same key spelling over
``project.manifest_keys()``; its results carry the kind ``manifest`` or
``manifest.<key>``.

What this does not reach:

- a key inside a ``map[string, X]`` field (``memory``, ``measures``) or a ``value``
  field (``init``), or anything in a list of scalars;
- a value of a shape the loader refuses, which gives no result;
- a string key written twice in one mapping, which is one result; a merged key
  (``<<``) is reported once, at the mapping it is merged into.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from swcraft_tools.model_registry import CollectionFacts, KindFacts, ModelRegistry, current_registry
from swcraft_tools.yaml_load import compose_yaml_document

#: One step of a locator: ``("field", name)`` into a declared field's value, or
#: ``("item", index)`` into one item of a list.
LocatorStep = tuple[str, str | int]


@dataclass(frozen=True)
class UndeclaredKey:
    """One key no field of its kind declares.

    ``kind`` is the :attr:`~swcraft_tools.model_registry.KindFacts.kind` whose
    mapping holds the key, ``key`` the key's text (empty for an empty key) and
    ``locator`` the steps from the document's top mapping down to that mapping
    (empty for a key at the top).
    """

    kind: str
    key: str
    locator: tuple[LocatorStep, ...]


class _NonStringKey(Exception):
    """A constructed key that is not a string: the walk starts again over the source nodes."""


@dataclass(frozen=True)
class _Reading:
    """How the walk reads one representation of the document: constructed values or source nodes."""

    mapping_items: Callable[[Any], list[tuple[str, Any]] | None]
    sequence_items: Callable[[Any], list[Any] | None]
    scalar: Callable[[Any], Any]


def _constructed_items(value: Any) -> list[tuple[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    items: list[tuple[str, Any]] = []
    for key, item in value.items():
        if not isinstance(key, str):
            raise _NonStringKey
        items.append((key, item))
    return items


def _constructed_sequence(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _node_items(text: str) -> Callable[[Any], list[tuple[str, Any]] | None]:
    def items(node: Any) -> list[tuple[str, Any]] | None:
        if not isinstance(node, yaml.MappingNode):
            return None
        return _flattened(text, node)

    return items


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _merge_sources(value: yaml.Node) -> list[yaml.MappingNode] | None:
    """The mappings a ``<<`` value merges, the winning one first, or ``None`` for a value the loader refuses."""
    if isinstance(value, yaml.MappingNode):
        return [value]
    if isinstance(value, yaml.SequenceNode) and all(isinstance(item, yaml.MappingNode) for item in value.value):
        return list(value.value)
    return None


def _flattened(text: str, node: yaml.MappingNode) -> list[tuple[str, Any]]:
    """*node*'s pairs with every ``<<`` resolved as the loader resolves it (``flatten_mapping``, then construction).

    The merged pairs come first. A key merged more than once, or merged and stated
    again, is one pair at the place it was first merged, carrying the value
    construction gives it; the mapping's own keys are otherwise kept occurrence by
    occurrence. A ``<<`` whose value the loader refuses stays a key named ``<<``.
    """
    merged: dict[str, Any] = {}
    own: list[tuple[str, Any]] = []
    for key, value in node.value:
        sources = _merge_sources(value) if key.tag == _MERGE_TAG else None
        if sources is None:
            own.append((_key_text(text, key), value))
            continue
        for source in reversed(sources):
            for name, item in _flattened(text, source):
                merged[name] = item
    kept: list[tuple[str, Any]] = []
    for name, item in own:
        if name in merged:
            merged[name] = item
        else:
            kept.append((name, item))
    return [*merged.items(), *kept]


def _key_text(text: str, key: yaml.Node) -> str:
    """A key node's text as the user wrote it: a scalar's value, or the source span of any other node."""
    if isinstance(key, yaml.ScalarNode):
        return str(key.value)
    return text[key.start_mark.index : key.end_mark.index]


def _node_sequence(node: Any) -> list[Any] | None:
    return list(node.value) if isinstance(node, yaml.SequenceNode) else None


def _node_scalar(node: Any) -> Any:
    return node.value if isinstance(node, yaml.ScalarNode) else None


_CONSTRUCTED = _Reading(_constructed_items, _constructed_sequence, lambda value: value)


def _node_reading(text: str) -> _Reading:
    return _Reading(_node_items(text), _node_sequence, _node_scalar)


def _single_enum(kind: KindFacts, field_name: str | None) -> str | None:
    """The one value *kind* pins *field_name* to, or ``None`` when it pins none or several."""
    if field_name is None:
        return None
    field = kind.get_field(field_name)
    if field is None or len(field.enum) != 1:
        return None
    return str(field.enum[0])


def _choose(kinds: Iterable[KindFacts], field_name: str | None, value: Any) -> KindFacts | None:
    """The one of *kinds* whose discriminator *field_name* is pinned to *value*, or ``None``."""
    if not isinstance(value, str):
        return None
    matches = [kind for kind in kinds if _single_enum(kind, field_name) == value]
    return matches[0] if len(matches) == 1 else None


def _member_kind(
    collection: CollectionFacts, item_items: list[tuple[str, Any]], reading: _Reading, registry: ModelRegistry
) -> KindFacts | None:
    members = [registry.kind(name) for name in collection.member_kinds]
    if len(members) == 1:
        return members[0]
    field_name = collection.discriminator_field
    value = next((reading.scalar(item) for key, item in item_items if key == field_name), None)
    return _choose(members, field_name, value)


def _walk_kind(
    value: Any,
    kind: KindFacts,
    locator: tuple[LocatorStep, ...],
    reading: _Reading,
    registry: ModelRegistry,
) -> Iterator[UndeclaredKey]:
    items = reading.mapping_items(value)
    if items is None:
        return
    declared = {field.name: field for field in kind.file_fields}
    for key, item in items:
        field = declared.get(key)
        if field is None:
            yield UndeclaredKey(kind=kind.kind, key=key, locator=locator)
            continue
        if field.element_kind is not None:
            yield from _walk_kind(
                item, registry.kind(field.element_kind), (*locator, ("field", key)), reading, registry
            )
            continue
        collection = field.collection
        if collection is None or collection.collection_kind != "list" or not collection.member_kinds:
            continue
        for index, member in enumerate(reading.sequence_items(item) or ()):
            member_items = reading.mapping_items(member)
            if member_items is None:
                continue
            member_kind = _member_kind(collection, member_items, reading, registry)
            if member_kind is None:
                continue
            yield from _walk_kind(member, member_kind, (*locator, ("field", key), ("item", index)), reading, registry)


def _top_kind(value: Any, top_level: tuple[KindFacts, ...], reading: _Reading) -> KindFacts | None:
    if len(top_level) == 1:
        return top_level[0]
    items = reading.mapping_items(value)
    if items is None:
        return None
    discriminators = {kind.discriminator_field for kind in top_level}
    if len(discriminators) != 1:
        return None
    (field_name,) = discriminators
    found = next((reading.scalar(item) for key, item in items if key == field_name), None)
    return _choose(top_level, field_name, found)


def _collect(
    text: str, document: Any, walk: Callable[[Any, _Reading], Iterator[UndeclaredKey]]
) -> tuple[UndeclaredKey, ...]:
    """Run *walk* over the constructed *document*, or over *text*'s source nodes when a key is no string."""
    try:
        return tuple(walk(document, _CONSTRUCTED))
    except _NonStringKey:
        pass
    root = compose_yaml_document(text)
    if root is None:
        return ()
    return tuple(walk(root, _node_reading(text)))


def undeclared_keys(
    text: str, document: Any, layer: str, *, registry: ModelRegistry | None = None
) -> tuple[UndeclaredKey, ...]:
    """Every key of one element file of *layer* that no field of its kind declares, in document order.

    *document* is what :func:`~swcraft_tools.yaml_load.load_yaml_document`
    constructs from *text*. A document of a shape the loader refuses gives no
    result and raises nothing.
    """
    resolved = current_registry() if registry is None else registry
    top_level = resolved.layer(layer).top_level_kinds

    def walk(value: Any, reading: _Reading) -> Iterator[UndeclaredKey]:
        kind = _top_kind(value, top_level, reading)
        if kind is None:
            return iter(())
        return _walk_kind(value, kind, (), reading, resolved)

    return _collect(text, document, walk)


#: The :attr:`UndeclaredKey.kind` of a key at the manifest's top; a nested mapping's is ``manifest.<key>``.
MANIFEST_KIND = "manifest"


def undeclared_manifest_keys(text: str, document: Any, keys: Mapping[str, frozenset[str]]) -> tuple[UndeclaredKey, ...]:
    """Every key of a manifest that it does not declare, in document order.

    *keys* is ``project.manifest_keys()``: under ``""`` the top-level keys, under a
    declared key whose value is itself a mapping of settings (``outputs``) that
    mapping's keys. A top-level key outside ``keys[""]`` is one result of kind
    :data:`MANIFEST_KIND` with an empty locator; a key inside a nested mapping
    outside its set is one result of kind ``manifest.<key>`` with the locator
    ``(("field", <key>),)``. A non-mapping manifest, or a non-mapping value under a
    nested key, gives no result.
    """
    top = keys.get("", frozenset())

    def walk(value: Any, reading: _Reading) -> Iterator[UndeclaredKey]:
        items = reading.mapping_items(value)
        for key, item in items or ():
            if key not in top:
                yield UndeclaredKey(kind=MANIFEST_KIND, key=key, locator=())
                continue
            nested = keys.get(key)
            if nested is None:
                continue
            for inner, _ in reading.mapping_items(item) or ():
                if inner not in nested:
                    yield UndeclaredKey(kind=f"{MANIFEST_KIND}.{key}", key=inner, locator=(("field", key),))

    return _collect(text, document, walk)


def nearest_declared_key(key: str, declared: Iterable[str], held: Iterable[str]) -> str | None:
    """The declared key *key* most likely misspells, or None: the hint both unknown-key rules give.

    ``difflib``'s best guess at a ratio of 0.8 among *declared* less the keys the
    mapping already *held* -- a key it holds is never the one a stray key was meant
    to be. An empty *key* gets None.
    """
    if not key:
        return None
    candidates = sorted(set(declared) - set(held))
    nearest = difflib.get_close_matches(key, candidates, n=1, cutoff=0.8)
    return nearest[0] if nearest else None


def held_keys(document: Any, locator: Sequence[tuple[str, str | int]]) -> set[str]:
    """The string keys of the mapping *locator* names in the constructed *document*; empty where it names none."""
    value = document
    for step, name in locator:
        if step == "field" and isinstance(value, dict):
            value = value.get(name)
        elif step == "item" and isinstance(value, list) and isinstance(name, int) and name < len(value):
            value = value[name]
        else:
            return set()
    return {key for key in value if isinstance(key, str)} if isinstance(value, dict) else set()


def unknown_key_clauses(key: str, declared: Iterable[str], held: Iterable[str]) -> tuple[str, str]:
    """``(carried, hint)``: the two message clauses both unknown-key rules spell alike.

    *carried* is ``the key '<key>'``, or ``an empty key``; *hint* is
    ``; the nearest declared key is '<k>'`` by :func:`nearest_declared_key`, or
    empty without one.
    """
    nearest = nearest_declared_key(key, declared, held)
    carried = f"the key '{key}'" if key else "an empty key"
    return carried, (f"; the nearest declared key is '{nearest}'" if nearest else "")
