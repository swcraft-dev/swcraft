"""The names-free checks' sentinel model: a specimen's model rewritten with sentinels.

Shared by the passport's names-free check and the benchmark runner's privacy test
(``test_agent_task_run_private.py``); kept out of ``conftest.py``, which holds fixtures only.

The rewrite walks every element with the model contract's registry: each identity and each value a declared
reference, relation or member reference names gains :data:`NAME_SENTINEL` as a prefix, so a name and everything
pointing at it move together. :data:`STORED_NAME_FIELDS` and :data:`MEMBER_NAME_FIELDS` add the name fields the
contract declares neither as identity nor as reference. Every free text (:data:`TEXT_FIELDS`) becomes
:data:`TEXT_SENTINEL`, and every kind declaring a ``description`` gains one. An ``instance`` path
(:data:`INSTANCE_PATH_FIELDS`) has each segment prefixed; an element-form member (:data:`ELEMENT_FORM_FIELDS`,
``swc:Controller``) keeps its token and has its name prefixed.

The completeness check has two halves: :func:`unsentinelled_fields` names every field of the loaded model holding a
value without the sentinel, independently of the walk's plan; :func:`unresolved_references` counts what resolves to
nothing before and after, so a name moved without its target fails although it carries the sentinel.

What this does not reach: a name inside a free text; a member reference, which :func:`unresolved_references` does
not resolve; and a later contract's name-holding, path-valued or element-form field that is neither identity nor
reference (extend the field lists above by hand).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, TypeAdapter
from swcraft_tools.model_registry import KindFacts, ModelRegistry, current_registry
from swcraft_tools.model_state import load_model_state, scan_layer
from swcraft_tools.scheduling_identity import PATH_SEPARATOR, identities_from
from swcraft_tools.trace import LINKABLE_LAYERS, is_element_form, parse_element_reference
from swcraft_tools.yaml_load import load_yaml_document

#: The sentinels a names-free check plants (:func:`rewrite_with_sentinels`): one in every name, one
#: in every text.
NAME_SENTINEL = "ZqSentinelName"
TEXT_SENTINEL = "ZqSentinelText"

#: The fields, by name and on every kind that declares them, that store a name the contract declares
#: neither as an identity nor as a reference.
STORED_NAME_FIELDS = frozenset({"symbol", "behavior_name", "package"})

#: The fields that spell a member's name without the contract declaring a reference: each names a port,
#: a composition member or an operation the walk renames, so it is renamed with them.
MEMBER_NAME_FIELDS = frozenset(
    {
        "field:connectors.connector_endpoint.port",
        "field:compositions.delegation.port",
        "field:compositions.delegation.member",
        "field:compositions.delegation.member_port",
        "field:swc.operation_event.port",
        "field:swc.operation_event.operation",
        "field:swc.disabled_mode.port",
    }
)

#: The free-text fields, replaced by a text sentinel wherever an element or a member holds one.
TEXT_FIELDS = ("title", "description", "note")

#: The fields holding an ``instance`` path, :data:`~swcraft_tools.scheduling_identity.PATH_SEPARATOR`-joined
#: composition member names from the root composition down (the contract states the form in the
#: fields' descriptions alone): each segment is prefixed, never the path as one string.
INSTANCE_PATH_FIELDS = frozenset({"field:tasks.task_runnable_ref.instance", "field:sequences.sequence_step.instance"})

#: The fields whose members may be in the element form ``<token>:<name>`` (the contract states the form
#: in the field's description alone): the name after the first ``:`` is prefixed, never the token.
ELEMENT_FORM_FIELDS = frozenset({"field:requirements.requirement.derived_from"})


class SentinelIncomplete(AssertionError):
    """The rewrite left a name without the sentinel; ``fields`` names each field, never a value."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__(f"the sentinel rewrite missed {len(fields)} field(s): {', '.join(fields)}")


def _prefixed(value: Any) -> Any:
    """*value*'s names prefixed with the name sentinel: a string, each string of a list, each key of a map."""
    if isinstance(value, str):
        return f"{NAME_SENTINEL}{value}"
    if isinstance(value, list):
        return [_prefixed(item) for item in value]
    if isinstance(value, dict):
        return {_prefixed(key): item for key, item in value.items()}
    return value


def _prefixed_path(value: Any) -> Any:
    """An instance path's segments each prefixed with the name sentinel: ``outer/inner`` as two names."""
    if isinstance(value, str):
        return PATH_SEPARATOR.join(_prefixed(segment) for segment in value.split(PATH_SEPARATOR))
    return value


def _prefixed_element_forms(value: Any) -> Any:
    """A list's members prefixed, an element-form one in its name alone: ``swc:Foo`` as ``swc:<sentinel>Foo``."""
    if isinstance(value, list):
        return [
            f"{item.partition(':')[0]}:{_prefixed(item.partition(':')[2])}"
            if isinstance(item, str) and is_element_form(item)
            else _prefixed(item)
            for item in value
        ]
    return _prefixed(value)


def _is_instance_path(kind: KindFacts, key: str) -> bool:
    facts = kind.get_field(key)
    return facts is not None and facts.id in INSTANCE_PATH_FIELDS


def _is_element_form_field(kind: KindFacts, key: str) -> bool:
    facts = kind.get_field(key)
    return facts is not None and facts.id in ELEMENT_FORM_FIELDS


def _member_reference_targets(registry: ModelRegistry) -> frozenset[str]:
    """The collections a member reference finds its member in: a whole-value one's items are names."""
    return frozenset(target.collection_id for edge in registry.member_references() for target in edge.collections)


def names_of(kind: KindFacts, registry: ModelRegistry) -> frozenset[str]:
    """The fields of *kind* whose value the walk prefixes, member identities apart (the walk's plan).

    A field declared as a reference, a relation or a member reference; one of
    :data:`STORED_NAME_FIELDS` or :data:`MEMBER_NAME_FIELDS`; and a whole-value collection a member
    reference targets (its items are the members' names).
    """
    targets = _member_reference_targets(registry)
    return frozenset(
        facts.name
        for facts in kind.fields
        if facts.reference is not None
        or facts.relation is not None
        or facts.member_reference is not None
        or facts.name in STORED_NAME_FIELDS
        or facts.id in MEMBER_NAME_FIELDS
        or (
            facts.collection is not None
            and facts.collection.member_identity == "whole_value"
            and facts.collection.collection_id in targets
        )
    )


def _member_identity(kind: KindFacts, field: str) -> frozenset[str]:
    """The fields a member of *kind*'s collection *field* is identified by (none for a whole value)."""
    facts = kind.get_field(field)
    collection = None if facts is None else facts.collection
    if collection is None:
        return frozenset()
    if collection.identity_field is not None:
        return frozenset({collection.identity_field})
    return frozenset(collection.identity_fields)


def _aliases(model: BaseModel) -> dict[str, str]:
    """*model*'s attribute names by the key the model file spells each under."""
    return {(info.alias or name): name for name, info in type(model).model_fields.items()}


def _children(value: Any, raw: Any) -> Iterator[tuple[BaseModel, dict[str, Any]]]:
    """The members under one field: the validated member beside its file form, for each one."""
    if isinstance(value, BaseModel) and isinstance(raw, dict):
        yield value, raw
    elif isinstance(value, list) and isinstance(raw, list):
        for item, raw_item in zip(value, raw, strict=True):
            if isinstance(item, BaseModel) and isinstance(raw_item, dict):
                yield item, raw_item
    elif isinstance(value, dict) and isinstance(raw, dict):
        for key, item in value.items():
            if isinstance(item, BaseModel) and isinstance(raw.get(key), dict):
                yield item, raw[key]


def _rewrite(
    model: BaseModel, raw: dict[str, Any], identity: frozenset[str], registry: ModelRegistry, counter: list[int]
) -> None:
    """*raw*, the file form of *model*, rewritten in place: its names prefixed, its texts replaced."""
    kind = registry.kind_of_model(type(model))
    assert kind is not None, f"no kind declares {type(model).__name__}"
    named = names_of(kind, registry) | identity
    aliases = _aliases(model)
    for key in list(raw):
        value = getattr(model, aliases[key]) if key in aliases else None
        for member, raw_member in _children(value, raw[key]):
            _rewrite(member, raw_member, _member_identity(kind, key), registry, counter)
        if key in named and raw[key] is not None:
            if _is_instance_path(kind, key):
                raw[key] = _prefixed_path(raw[key])
            elif _is_element_form_field(kind, key):
                raw[key] = _prefixed_element_forms(raw[key])
            else:
                raw[key] = _prefixed(raw[key])
    for key in TEXT_FIELDS:
        if key in raw or (key == "description" and kind.get_field(key) is not None):
            counter[0] += 1
            raw[key] = f"{TEXT_SENTINEL} {counter[0]}"


def rewrite_with_sentinels(model_root: Path, *, registry: ModelRegistry | None = None) -> None:
    """The model under *model_root* rewritten in place with the sentinels, each file's name prefixed as its identity is.

    The walk is the module docstring's. Each file is parsed through the product's YAML reader and
    validated by its layer's element type, so every value is classified by the kind the registry binds
    it to, never by its spelling.

    Raises:
        FileExistsError: a renamed file's name is already taken (nothing is overwritten).
        SentinelIncomplete: the completeness check found a field the walk left without the sentinel
            (:func:`unsentinelled_fields`), or one whose count of references resolving to nothing moved
            (:func:`unresolved_references`, named ``<field> (unresolved)``), so the fixture fails instead
            of leaving a name out of the names-free check or a reference dangling.
    """
    registry = registry or current_registry()
    counter = [0]
    unresolved_before = unresolved_references(model_root, registry=registry)
    for layer in registry.layer_names:
        facts = registry.layer(layer)
        adapter = TypeAdapter(facts.element_annotation)
        scan = scan_layer(model_root, layer, registry=registry)
        for path in sorted((entry.path for entry in scan.files), key=lambda path: path.as_posix()):
            raw = copy.deepcopy(load_yaml_document(path.read_text(encoding="utf-8")))
            element = adapter.validate_python(raw)
            _rewrite(element, raw, frozenset({facts.identity_field}), registry, counter)
            # The file name follows the identity: a collision's ``~N`` suffix is kept, and an existing
            # file is never overwritten.
            renamed = path.with_name(f"{NAME_SENTINEL}{path.name}")
            if renamed.exists():
                raise FileExistsError(
                    f"the sentinel rewrite would overwrite {renamed.relative_to(model_root).as_posix()}"
                )
            path.unlink()
            renamed.write_text(
                yaml.safe_dump(raw, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
            )
    missed = unsentinelled_fields(model_root, registry=registry)
    unresolved_after = unresolved_references(model_root, registry=registry)
    missed += [
        f"{field} (unresolved)"
        for field in sorted(set(unresolved_before) | set(unresolved_after))
        if unresolved_after.get(field, 0) != unresolved_before.get(field, 0)
    ]
    if missed:
        raise SentinelIncomplete(sorted(missed))


def _kind_and_field(field_id: str) -> tuple[str, str]:
    """``field:layer.kind.field`` as ``(kind:layer.kind, field)``."""
    owner, _, name = field_id.removeprefix("field:").rpartition(".")
    return f"kind:{owner}", name


def required_names(registry: ModelRegistry) -> dict[str, frozenset[str]]:
    """The fields that must carry the name sentinel, by kind id, read off the registry's flat lists.

    Independent of :func:`names_of`: each layer's identity field on its top-level kinds, each
    collection's member identity on its member kinds, every reference, relation and member reference by
    its field id, and the two lists of undeclared name fields.
    """
    required: dict[str, set[str]] = {}
    for layer in registry.layer_names:
        facts = registry.layer(layer)
        for kind in facts.top_level_kinds:
            required.setdefault(kind.id, set()).add(facts.identity_field)
        for kind in facts.kinds:
            required.setdefault(kind.id, set()).update(f.name for f in kind.fields if f.name in STORED_NAME_FIELDS)
    for collection in registry.collections():
        identity = {collection.identity_field} if collection.identity_field else set(collection.identity_fields)
        for member in collection.member_kinds:
            required.setdefault(f"kind:{collection.layer}.{member}", set()).update(identity)
    edges = [*registry.references(), *registry.relations(), *registry.member_references()]
    for field_id in [edge.field_id for edge in edges] + sorted(MEMBER_NAME_FIELDS):
        kind_id, name = _kind_and_field(field_id)
        required.setdefault(kind_id, set()).add(name)
    return {kind_id: frozenset(names) for kind_id, names in required.items()}


def _names_in(value: Any) -> Iterator[str]:
    """The names one field holds: the string, a list's strings, a map's keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        yield from (item for item in value if isinstance(item, str))
    elif isinstance(value, dict):
        yield from (key for key in value if isinstance(key, str))


def _offenders(model: BaseModel, required: dict[str, frozenset[str]], registry: ModelRegistry) -> Iterator[str]:
    kind = registry.kind_of_model(type(model))
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        key = info.alias or name
        if kind is not None and key in required.get(kind.id, frozenset()):
            names = list(_names_in(value))
            if _is_instance_path(kind, key):
                names = [segment for text in names for segment in text.split(PATH_SEPARATOR)]
            if _is_element_form_field(kind, key):
                names = [text.partition(":")[2] if is_element_form(text) else text for text in names]
            if any(not text.startswith(NAME_SENTINEL) for text in names):
                yield f"{kind.id.removeprefix('kind:')}.{key}"
        children = value if isinstance(value, list) else list(value.values()) if isinstance(value, dict) else [value]
        for child in children:
            if isinstance(child, BaseModel):
                yield from _offenders(child, required, registry)


def unsentinelled_fields(model_root: Path, *, registry: ModelRegistry | None = None) -> list[str]:
    """The completeness check: each field of the loaded model at *model_root* holding a name without the sentinel.

    Each entry is a field, ``layer.kind.field``, named once; the list is empty when every identity,
    member identity, reference and listed name field carries :data:`NAME_SENTINEL`. A value is never
    named.
    """
    registry = registry or current_registry()
    required = required_names(registry)
    state = load_model_state(model_root, registry=registry)
    return sorted(
        {
            field
            for layer in registry.layer_names
            for element in state[layer]
            for field in _offenders(element, required, registry)
        }
    )


def _nodes(model: BaseModel, registry: ModelRegistry) -> Iterator[tuple[str, BaseModel]]:
    """*model* and every member under it, each with its kind id (a node no kind declares is passed over)."""
    kind = registry.kind_of_model(type(model))
    if kind is not None:
        yield kind.id, model
    for name in type(model).model_fields:
        value = getattr(model, name)
        children = value if isinstance(value, list) else list(value.values()) if isinstance(value, dict) else [value]
        for child in children:
            if isinstance(child, BaseModel):
                yield from _nodes(child, registry)


def _field_value(node: BaseModel, key: str) -> Any:
    return getattr(node, _aliases(node).get(key, key), None)


def unresolved_references(model_root: Path, *, registry: ModelRegistry | None = None) -> dict[str, int]:
    """The second half of the completeness check: per field, how many names resolve to nothing.

    Every declared reference is looked up among its target layer's identities, every relation's keys
    among its target kind's field, every instance path is walked by the product's resolver
    (``instance-unknown``/``instance-not-a-component`` count as unresolved) and every element-form
    member is read by the product's element-reference reader (an unknown token or name counts). The
    counts are compared before and after the rewrite, since a model may hold a dangling name by design;
    keys are ``layer.kind.field``, never a value. Not reached: member references, and every instance
    path when the composition walk is undefined (no root composition, several, or a cycle).
    """
    registry = registry or current_registry()
    state = load_model_state(model_root, registry=registry)
    nodes = [
        (layer, kind_id, node)
        for layer in registry.layer_names
        for element in state[layer]
        for kind_id, node in _nodes(element, registry)
    ]
    unresolved: dict[str, int] = {}

    def count(field_id: str, missing: int) -> None:
        if missing:
            key = field_id.removeprefix("field:")
            unresolved[key] = unresolved.get(key, 0) + missing

    for edge in registry.references():
        targets = {getattr(element, edge.target_identity_field) for element in state[edge.target_layer]}
        source = f"kind:{edge.source_layer}.{edge.source_kind}"
        count(
            edge.field_id,
            sum(
                name not in targets
                for _, kind_id, node in nodes
                if kind_id == source
                for name in _names_in(_field_value(node, edge.source_field))
            ),
        )
    for edge in registry.relations():
        target = f"kind:{edge.target_layer}.{edge.target_kind}"
        keys = {_field_value(node, edge.target_field) for layer, kind_id, node in nodes if kind_id == target}
        source = f"kind:{edge.source_layer}.{edge.source_kind}"
        count(
            edge.field_id,
            sum(
                key not in keys
                for _, kind_id, node in nodes
                if kind_id == source and _field_value(node, edge.source_field) is not None
                for key in edge.resolver(node)
            ),
        )
    identities = identities_from(state["compositions"])
    for field_id in sorted(INSTANCE_PATH_FIELDS):
        kind_id, key = _kind_and_field(field_id)
        paths = [_field_value(node, key) for _, node_kind, node in nodes if node_kind == kind_id]
        count(
            field_id,
            sum(
                identities.resolve(None, path).refusal in ("instance-unknown", "instance-not-a-component")
                for path in paths
                if isinstance(path, str) and identities.defined
            ),
        )
    linkable = {
        layer: {str(getattr(element, registry.layer(layer).identity_field)) for element in state[layer]}
        for layer in LINKABLE_LAYERS.values()
    }
    for field_id in sorted(ELEMENT_FORM_FIELDS):
        kind_id, key = _kind_and_field(field_id)
        references = [
            parse_element_reference(text)
            for _, node_kind, node in nodes
            if node_kind == kind_id
            for text in _names_in(_field_value(node, key))
            if is_element_form(text)
        ]
        count(
            field_id, sum(reference is None or reference[1] not in linkable[reference[0]] for reference in references)
        )
    return dict(sorted(unresolved.items()))
