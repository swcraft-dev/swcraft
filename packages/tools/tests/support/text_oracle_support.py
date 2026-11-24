"""The change-set text oracles' own machinery: the corpus, the mutations, the restyles, the proofs.

A helper beside ``conftest.py``, imported by ``test_change_set_text_oracle.py`` alone. **Nothing here
reads a position of the edit or calls into** :mod:`swcraft_tools.yaml_text_edit`: the mutations come
from the writer's dictionaries through the schema, the foreign layouts from an emitter of this
module's own, and the lines a change may touch from the old text and the two dictionaries. That
is what makes the three oracles judgements of the edit rather than restatements of it. The
comparator :func:`canonical` is a second statement of the type-strict equality too.

What this does not reach: a layout :class:`Style` does not generate, and a mutation the schema
refuses (skipped, counted by the caller that passes :func:`mutations` a counter).
:func:`changed_lines` is deliberately coarse where the edit's contract is coarse: a flow collection,
a run of list items that are not one pair, and a mapping or list emptied or filled are marked whole,
so a loss inside them is not seen here. :func:`stray` reads an added line by its text alone, so it
says nothing of a line holding a flow bracket, which is ``_flow_text``'s and in no dump, and nothing
of a duplicate of a line the dump holds.
"""

from __future__ import annotations

import json
import re
import shutil
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

import pydantic
import yaml
from swcraft_tools.model_registry import production_registry
from swcraft_tools.model_state import load_layer, stored_element_files
from swcraft_tools.writer.elements import (
    READ_BACK_MISMATCH,
    UNEXPECTED_EXCEPTION,
    EditedText,
    _dump_element,
    edited_text,
    element_to_dict,
    write_element,
)
from swcraft_tools.yaml_load import compose_yaml_document, load_yaml_document

#: The mutation shapes, the names the delta-projection table of the test module uses.
SCALAR_ALTERED = "scalar altered"
KIND_CHANGED = "value changed in kind"
KEY_DROPPED = "optional key dropped"
KEY_ADDED = "omitted key added"
FIRST_DROPPED = "list's first dropped"
LAST_DROPPED = "list's last dropped"
LAST_DUPLICATED = "list's last duplicated, new identity"
REVERSED = "list reversed"
ENTRY_CHANGED = "map entry changed"
ENTRY_ADDED = "map entry added"
ENTRY_REMOVED = "map entry removed"
SEVERAL_CHANGES = "several changes at once"
SHAPES = (
    SCALAR_ALTERED,
    KIND_CHANGED,
    KEY_DROPPED,
    KEY_ADDED,
    FIRST_DROPPED,
    LAST_DROPPED,
    LAST_DUPLICATED,
    REVERSED,
    ENTRY_CHANGED,
    ENTRY_ADDED,
    ENTRY_REMOVED,
    SEVERAL_CHANGES,
)

_DROP = object()
_ABSENT = object()

#: How many groups of edits one element takes: each is a mutation of its own in all three oracles.
_SEVERAL_CHANGES_LIMIT = 4

#: The undeclared key the restyles plant; a closed class refuses it and then goes without.
PLANTED_KEY = "zz_undeclared"

#: The pair :data:`ALIAS_STYLE` plants at the end of a list item, the note written through the anchor.
PLANTED_ANCHOR = "zz_anchor"
PLANTED_NOTE = "zz_owner_note"

#: The fallbacks the oracles allow, reason to the test of the case that may take it. The reasons
#: are spelled here and not imported: a reworded refusal must fail, not follow.
_NESTED_ARRAY = "a list item does not stand on a line of its own"
_LIMIT = "the change touches too many places in the file"
_MATCH = "a list changed along too many items to match them"
_RECOMPOSE = "the change touches too many places for a file of this size"
_THROUGH_AN_ALIAS = "the file writes a value through an alias, an anchor or a tag"


def _holds_a_nested_list(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_holds_a_nested_list(item) for item in value.values())
    if isinstance(value, list):
        return any(isinstance(item, list) or _holds_a_nested_list(item) for item in value)
    return False


def _pairs_past_the_limit(old: Any, new: Any) -> bool:
    """Whether a list of the same length on both sides differs in more than 64 items."""
    if isinstance(old, dict) and isinstance(new, dict):
        return any(_pairs_past_the_limit(old[key], new[key]) for key in old.keys() & new.keys())
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return sum(canonical(a) != canonical(b) for a, b in zip(old, new, strict=True)) > 64
    return False


def _match_cost(old: Any, new: Any) -> int:
    """What the lists changed in a walk from *old* to *new* add up to, each ``min(n, m) * (n + pairs)``.

    Item by item where a list keeps its length: the edit's own pairing is what is under test.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        return sum(_match_cost(old[key], new[key]) for key in old.keys() & new.keys())
    if not (isinstance(old, list) and isinstance(new, list)):
        return 0
    shortest = min(len(old), len(new))
    head = next((at for at in range(shortest) if canonical(old[at]) != canonical(new[at])), shortest)
    left = shortest - head
    tail = next((at for at in range(left) if canonical(old[-1 - at]) != canonical(new[-1 - at])), left)
    old_items = [canonical(item) for item in old[head : len(old) - tail]]
    counts = Counter(canonical(item) for item in new[head : len(new) - tail])
    own = min(len(old_items), counts.total()) * (len(old_items) + sum(counts[item] for item in old_items))
    if len(old) != len(new):
        return own
    return own + sum(_match_cost(a, b) for a, b in zip(old, new, strict=True))


def _matched_past_the_budget(old: Any, new: Any) -> bool:
    """Whether a walk's list matches add up to more than the 32 million the edit's budget allows."""
    return _match_cost(old, new) > 32_000_000


def _changed_places(old: Any, new: Any) -> int:
    """How many values differ between *old* and *new*, list items pair by pair where a list keeps its length."""
    if isinstance(old, dict) and isinstance(new, dict):
        return sum(_changed_places(old.get(key), new.get(key)) for key in old.keys() | new.keys())
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return sum(_changed_places(a, b) for a, b in zip(old, new, strict=True))
    return int(canonical(old) != canonical(new))


def _recomposed_past_the_budget(old: Any, new: Any) -> bool:
    """Whether the changes times the size of the larger side pass the 16 million the edit allows.

    The sizes are the canonical texts' and not the file's and the writer's, which this judge does not
    hold: a close estimate, and a file of a size no corpus model reaches.
    """
    return _changed_places(old, new) * max(len(canonical(old)), len(canonical(new))) > 16_000_000


CLOSED_LIST: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], bool]] = {
    # The writer's own nested array, `- - 1`: the edit refuses it, and its text is the writer's.
    _NESTED_ARRAY: lambda old, new: _holds_a_nested_list(old) or _holds_a_nested_list(new),
    # A change of that size has no small diff to protect.
    _LIMIT: _pairs_past_the_limit,
    # The matcher's cost is refused before it is paid.
    _MATCH: _matched_past_the_budget,
    # 64 changes are cheap in a small file and not in a large one.
    _RECOMPOSE: _recomposed_past_the_budget,
}


def canonical(value: Any) -> str:
    """A string equal for two values only when they are, type-strict and at every depth.

    JSON keeps ``1``, ``1.0`` and ``true`` apart; the key order is a layout, not a value.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def writer_dict(model: pydantic.BaseModel) -> dict[str, Any]:
    """*model* as the writer's dictionary."""
    return element_to_dict(model)


def validated_dict(model_class: type[pydantic.BaseModel], text: str) -> dict[str, Any]:
    """What *text* holds, validated as *model_class*, as the writer's dictionary."""
    return element_to_dict(model_class.model_validate(load_yaml_document(text)))


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    """One element of a corpus: where it lies, what it holds."""

    layer: str
    identity: str
    target: str
    model: pydantic.BaseModel

    @property
    def kind(self) -> tuple[str, str]:
        return self.layer, type(self.model).__name__


def stored_elements(model_root: Path) -> list[Stored]:
    """Every element under *model_root*, with its model-root-relative file, in a stable order."""
    registry = production_registry()
    found: list[Stored] = []
    for layer in registry.layer_names:
        field_name = registry.layer(layer).identity_field
        files = stored_element_files(model_root, layer, registry=registry)
        for model in load_layer(model_root, layer, registry=registry):
            identity = getattr(model, field_name)
            target = files[identity][0].path.relative_to(model_root).as_posix()
            found.append(Stored(layer, identity, target, model))
    return found


def one_of_each_kind(elements: Sequence[Stored]) -> list[Stored]:
    """The element of each ``(layer, class)`` that gives the most mutations, ties to the first."""
    best: dict[tuple[str, str], tuple[int, Stored]] = {}
    for element in elements:
        size = len(canonical(element_to_dict(element.model)))
        if element.kind not in best or size > best[element.kind][0]:
            best[element.kind] = (size, element)
    return [chosen for _size, chosen in best.values()]


# ---------------------------------------------------------------------------
# The mutations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutation:
    """One derived change: its shape, where it acts, and the validated element it gives."""

    shape: str
    path: tuple[str | int, ...]
    model: pydantic.BaseModel


def _replaced(value: Any, path: Sequence[str | int], transform: Callable[[Any], Any]) -> Any:
    """*value* with *transform* applied at *path*, sharing what it does not change."""
    if not path:
        return transform(value)
    head, rest = path[0], path[1:]
    copy: Any = dict(value) if isinstance(value, dict) else list(value)
    inner = _replaced(value[head], rest, transform)
    if inner is _DROP:
        del copy[head]
    else:
        copy[head] = inner
    return copy


def _altered(value: Any) -> Any:
    """A different value of the same type; ``_ABSENT`` for one with no such thing."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value * 1.5 + 0.5
    if isinstance(value, str):
        return f"{value}x"
    return _ABSENT


def _renewed(item: Any) -> Any:
    """A copy of *item* with a new identity: its first string changed, or itself altered."""
    if isinstance(item, dict):
        for key, value in item.items():
            if isinstance(value, str):
                return {**item, key: f"{value}_dup"}
    fresh = _altered(item)
    return item if fresh is _ABSENT else fresh


def _samples(annotation: Any, depth: int = 0) -> list[Any]:
    """Values worth trying for a field of *annotation*; the schema decides which one holds."""
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if depth > 4:
        return []
    if origin is typing.Annotated:
        return _samples(arguments[0], depth)
    if origin is typing.Union or origin is types.UnionType:
        return [sample for argument in arguments if argument is not type(None) for sample in _samples(argument, depth)]
    if origin is typing.Literal:
        return list(arguments)
    if origin is list:
        return [[sample] for sample in _samples(arguments[0], depth + 1)[:3]]
    if origin is dict:
        return [{"Added": sample} for sample in _samples(arguments[1], depth + 1)[:3]]
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return _model_samples(annotation, depth)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [member.value for member in annotation]
    if annotation is bool:
        return [True, False]
    if annotation is int:
        return [7]
    if annotation is float:
        return [0.5]
    if annotation is str:
        return ["Added", "1ms"]
    return ["Added", 7, 0.5, True]


def _model_samples(model_class: type[pydantic.BaseModel], depth: int) -> list[dict[str, Any]]:
    built: dict[str, Any] = {}
    for name, info in model_class.model_fields.items():
        if info.is_required():
            samples = _samples(info.annotation, depth + 1)
            if not samples:
                return []
            built[info.alias or name] = samples[0]
    return [built]


def _edits_of_model(model: pydantic.BaseModel, data: dict[str, Any], path: tuple[Any, ...]) -> Iterator[Any]:
    for name, info in type(model).model_fields.items():
        key = info.alias or name
        if key in data:
            yield from _edits_of_value(getattr(model, name), data[key], (*path, key), SCALAR_ALTERED)
            yield KEY_DROPPED, (*path, key), lambda _value: _DROP, None
        else:
            for sample in _samples(info.annotation):
                yield KEY_ADDED, path, (lambda holder, key=key, sample=sample: {**holder, key: sample}), (path, key)


def _edits_of_value(value: Any, data: Any, path: tuple[Any, ...], scalar_shape: str) -> Iterator[Any]:
    if isinstance(value, pydantic.BaseModel):
        yield from _edits_of_model(value, data, path)
    elif isinstance(value, dict):
        yield from _edits_of_map(value, data, path)
    elif isinstance(value, list):
        yield from _edits_of_list(value, data, path)
    else:
        fresh = _altered(data)
        if fresh is not _ABSENT:
            yield scalar_shape, path, (lambda _value, fresh=fresh: fresh), None
        yield KIND_CHANGED, path, lambda old: [old], None


def _edits_of_map(value: dict[Any, Any], data: dict[Any, Any], path: tuple[Any, ...]) -> Iterator[Any]:
    for key in value:
        yield from _edits_of_value(value[key], data[key], (*path, key), ENTRY_CHANGED)
        yield ENTRY_REMOVED, (*path, key), lambda _value: _DROP, None
    if data:
        last = list(data)[-1]
        fresh = _altered(data[last])
        added = data[last] if fresh is _ABSENT else fresh
        yield ENTRY_ADDED, path, (lambda holder, last=last, added=added: {**holder, f"{last}Added": added}), None


def _edits_of_list(value: list[Any], data: list[Any], path: tuple[Any, ...]) -> Iterator[Any]:
    for index, item in enumerate(value):
        yield from _edits_of_value(item, data[index], (*path, index), SCALAR_ALTERED)
    if data:
        yield FIRST_DROPPED, path, lambda items: items[1:], None
        yield LAST_DROPPED, path, lambda items: items[:-1], None
        yield LAST_DUPLICATED, path, lambda items: [*items, _renewed(items[-1])], None
        yield REVERSED, path, lambda items: items[::-1], None
        yield KIND_CHANGED, path, lambda items: items[0], None


def mutations(
    model: pydantic.BaseModel, *, one_of_each_shape: bool = False, refused: Counter[str] | None = None
) -> Iterator[Mutation]:
    """Every change derived from *model*'s writer dictionary that its own class validates.

    A shape the schema refuses is skipped. Of an omitted field's samples the first one that
    validates stands for the field. The element's identity field is mutated like any other: the
    edit knows no identities. With *one_of_each_shape* the first of each shape stands for it,
    without validating the rest: on a large element that validation is the cost. *refused*
    counts what the schema turned away, by shape.
    """
    base = element_to_dict(model)
    reference = canonical(base)
    settled: set[Any] = set()
    for shape, path, transform, slot in _edits_of_model(model, base, ()):
        if (slot is not None and slot in settled) or (one_of_each_shape and shape in settled):
            continue
        data = _replaced(base, path, transform)
        try:
            changed = type(model).model_validate(data)
        except (pydantic.ValidationError, ValueError, TypeError):
            if refused is not None:
                refused[shape] += 1
            continue
        if canonical(element_to_dict(changed)) == reference:
            continue
        if slot is not None:
            settled.add(slot)
        settled.add(shape)
        yield Mutation(shape, path, changed)


#: Each transform of a group applies in the order `_edits_of_model` gave it: that walk is
#: post-order, so an edit inside a list or a key comes before the edit of the list or the key.
_Edit = tuple[str, tuple[Any, ...], Callable[[Any], Any], Any]


def _is_inside(path: tuple[Any, ...], holder: tuple[Any, ...]) -> bool:
    return path[: len(holder)] == holder and len(path) > len(holder)


def _groups_of_edits(edits: Sequence[_Edit]) -> Iterator[list[_Edit]]:
    """Groups of edits one change set could hold, a candidate for each reach the edit must handle."""
    plain = [edit for edit in edits if edit[0] != KIND_CHANGED]
    fields = [edit for edit in plain if edit[0] in (SCALAR_ALTERED, ENTRY_CHANGED)]
    # Far apart in the file: the last one's text must not move the first one's place.
    if len(plain) >= 3:
        yield [plain[0], plain[len(plain) // 2], plain[-1]]
    # Two fields of one list item.
    for first, second in zip(fields, fields[1:], strict=False):
        if len(first[1]) >= 3 and first[1][:-1] == second[1][:-1] and isinstance(first[1][-2], int):
            yield [first, second]
    # A deletion before a pair: the pair's place in the file and in the dump differ.
    for dropped in (edit for edit in plain if edit[0] == FIRST_DROPPED):
        holder = dropped[1]
        for inner in fields:
            if _is_inside(inner[1], holder) and inner[1][len(holder)] >= 2:
                yield [inner, dropped]
                break
    # A key removed beside a list walked elsewhere in the element.
    for removed in (edit for edit in plain if edit[0] == KEY_DROPPED):
        for changed in (edit for edit in plain if edit[0] == LAST_DUPLICATED):
            if not _is_inside(changed[1], removed[1]) and changed[1] != removed[1]:
                yield [removed, changed]
                break


def several_changes(model: pydantic.BaseModel) -> list[Mutation]:
    """Up to :data:`_SEVERAL_CHANGES_LIMIT` groups of edits at different paths that validate as one."""
    base = element_to_dict(model)
    reference = canonical(base)
    found: list[Mutation] = []
    for group in _groups_of_edits(list(_edits_of_model(model, base, ()))):
        data = base
        for _shape, path, transform, _slot in group:
            data = _replaced(data, path, transform)
        try:
            changed = type(model).model_validate(data)
        except (pydantic.ValidationError, ValueError, TypeError):
            continue
        if canonical(element_to_dict(changed)) != reference:
            found.append(Mutation(SEVERAL_CHANGES, group[0][1], changed))
            if len(found) == _SEVERAL_CHANGES_LIMIT:
                break
    return found


def every_mutation(element: Stored) -> list[Mutation]:
    """The selector of a full walk."""
    return [*mutations(element.model), *several_changes(element.model)]


def each_shape_once(element: Stored) -> list[Mutation]:
    """The selector of a sample, one mutation of each shape, and the groups of edits."""
    return [*mutations(element.model, one_of_each_shape=True), *several_changes(element.model)]


def longest_list_changed(element: Stored) -> list[Mutation]:
    """The sample of a large element's longest top-level list: reversed, and a field of each item paired.

    The second is the one that passes the limit: more than 64 items pair off, each its own change.
    Reversed, the items go out and come in as one run. The first of the shapes never meets either,
    because it stops at the element's first list, however short.
    """
    data = element_to_dict(element.model)
    name = max((key for key in data if isinstance(data[key], list)), key=lambda key: len(data[key]))
    items = data[name]
    reversed_items = type(element.model).model_validate({**data, name: items[::-1]})
    paired = type(element.model).model_validate({**data, name: [_renewed(item) for item in items]})
    return [Mutation(REVERSED, (name,), reversed_items), Mutation(SCALAR_ALTERED, (name,), paired)]


# ---------------------------------------------------------------------------
# The ledger, and oracles 1 and 2
# ---------------------------------------------------------------------------


@dataclass
class Ledger:
    """What one run met: the count of each reason, and every failure as a sentence."""

    mutations: int = 0
    lines: int = 0
    shapes: Counter[str] = field(default_factory=Counter)
    fallbacks: Counter[str] = field(default_factory=Counter)
    skipped_plants: Counter[str] = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)

    def judge(
        self, element: Stored, mutation: Mutation, outcome: EditedText, oracle: str, old: dict[str, Any]
    ) -> str | None:
        """Count *outcome*; the edited text when there is one, a failure for any unlisted fallback."""
        self.mutations += 1
        self.shapes[mutation.shape] += 1
        if outcome.text is not None:
            self.lines += 1
            return outcome.text
        reason = outcome.reason or ""
        self.fallbacks[reason] += 1
        allowed = CLOSED_LIST.get(reason)
        unexpected = reason == READ_BACK_MISMATCH or reason.startswith(UNEXPECTED_EXCEPTION.split("{", 1)[0])
        if unexpected or allowed is None or not allowed(old, element_to_dict(mutation.model)):
            self.fail(element, mutation, oracle, f"fell back: {reason}")
        return None

    def fail(self, element: Stored, mutation: Mutation, oracle: str, sentence: str) -> None:
        where = "/".join(str(step) for step in mutation.path)
        self.failures.append(f"{oracle} {element.layer}/{element.identity} [{mutation.shape} at {where}]: {sentence}")


def _prior(element: Stored) -> Callable[[], pydantic.BaseModel]:
    return lambda: element.model


def _workspace(root: Path, name: str, element: Stored, text: bytes | None) -> str:
    """A model root under *root* holding *element*'s file, with *text* or the writer's own."""
    model_root = root / name
    path = model_root / element.target
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        write_element(str(model_root), element.target, element.model)
    else:
        path.write_bytes(text)
    return str(model_root)


def run_parity_and_identity(
    source_root: Path, work: Path, elements: Sequence[Stored], chosen: Callable[[Stored], Sequence[Mutation]]
) -> Ledger:
    """Oracle 1 over the files as stored, oracle 2 over each file in the writer's form.

    The reference of both is the real ``write_element``, called with no ``edited``, into a root of
    its own. A mutation the writer's own guards refuse is not one a commit could land, and is
    skipped.
    """
    ledger = Ledger()
    for position, element in enumerate(elements):
        root = work / str(position)
        stored = (source_root / element.target).read_bytes()
        as_stored = _workspace(root, "stored", element, stored)
        writer_form = _workspace(root, "form", element, None)
        reference_root = root / "reference"
        reference_root.mkdir()
        old = element_to_dict(element.model)
        model_class = type(element.model)
        prior = _prior(element)
        for mutation in chosen(element):
            try:
                write_element(str(reference_root), element.target, mutation.model)
            except ValueError:
                continue
            whole = (reference_root / element.target).read_bytes().decode("utf-8")
            text = ledger.judge(
                element, mutation, edited_text(as_stored, element.target, mutation.model, prior), "oracle 1", old
            )
            if text is not None and canonical(validated_dict(model_class, text)) != canonical(
                validated_dict(model_class, whole)
            ):
                ledger.fail(element, mutation, "oracle 1", "the edited file holds another value than the whole write")
            text = ledger.judge(
                element, mutation, edited_text(writer_form, element.target, mutation.model, prior), "oracle 2", old
            )
            if text is not None and text != whole:
                ledger.fail(element, mutation, "oracle 2", "the edited bytes differ from the whole write")
        shutil.rmtree(root)
    return ledger


# ---------------------------------------------------------------------------
# The foreign layouts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    """One foreign layout; each field is one restyle."""

    name: str
    reversed_keys: bool = False
    flow_lists: bool = False
    multi_line_flow: bool = False
    indent: int = 2
    indented_lists: bool = False
    single_quoted: bool = False
    block_scalar: str = ""
    multi_line_plain: bool = False
    value_on_next_line: bool = False
    wide_dash: bool = False
    crlf: bool = False
    plants_keys: bool = True
    blank_lines: bool = False
    trailing_comments: bool = False
    #: An item ends in an anchor and an alias of it, in place of the planted key.
    alias_in_items: bool = False


STYLES = (
    Style("reversed key order", reversed_keys=True),
    Style("flow lists", flow_lists=True),
    Style("multi-line flow lists", flow_lists=True, multi_line_flow=True),
    Style("indented lists", indented_lists=True),
    Style("4-space", indent=4, indented_lists=True),
    Style("single-quoted strings", single_quoted=True),
    Style("literal block scalars", block_scalar="|"),
    Style("folded block scalars", block_scalar=">-"),
    Style("multi-line plain scalars", multi_line_plain=True),
    Style("value on the next line", value_on_next_line=True),
    Style("wide dash", wide_dash=True),
    Style("CRLF", crlf=True),
    Style("CRLF, 4-space, single-quoted", crlf=True, indent=4, indented_lists=True, single_quoted=True),
    Style("comments alone", plants_keys=False),
    Style("blank lines", blank_lines=True),
    Style("blank lines after block scalars", block_scalar="|", blank_lines=True),
    Style("trailing comments", trailing_comments=True),
)


def comments_only(style: Style) -> Style:
    """*style* planting comments and no undeclared key: the form a file takes on its way through pass D.

    Pass D refuses every change set while any file holds an undeclared key, so a restyle that
    reaches the engine plants comments alone; the key is planted where survival is decided, in
    ``edited_text`` and ``edit_element_text``, with no pass D.
    """
    return replace(style, plants_keys=False)


#: Not one of :data:`STYLES`: the fallback it leads to is no entry of the closed list, because it is
#: this layout that earns it. :func:`run_alias_stability` holds it to that fallback.
ALIAS_STYLE = Style("an anchor and an alias among the planted keys", alias_in_items=True)


def _one_line(value: Any) -> str:
    """A scalar in one line: JSON for what YAML's plain form cannot hold, YAML's own dump otherwise."""
    if isinstance(value, str) and ("\n" in value or not value.isascii()):
        return json.dumps(value)
    dumped = yaml.safe_dump(value, width=2**31 - 1, default_flow_style=True)
    return dumped.removesuffix("\n...\n").removesuffix("\n")


def _is_plain_prose(value: str) -> bool:
    """Whether *value* stands as a plain multi-line or a folded scalar: words, single-spaced."""
    return re.fullmatch(r"[A-Za-z0-9][\w.,()/-]*( [\w(][\w.,()/-]*)+", value) is not None


def _is_literal_safe(value: str) -> bool:
    """Whether a literal block holds *value* exactly: no edge whitespace, no blank line, no tab."""
    return bool(value) and value == value.strip() and "\t" not in value and "" not in value.split("\n")


class _Emitter:
    """The test's own YAML emitter, sharing nothing with the writer or the edit."""

    def __init__(self, style: Style, plant_top: bool, plant_items: bool) -> None:
        self.style = style
        self.plant_top = plant_top
        self.plant_items = plant_items
        self.anchors = 0

    def _keys(self, mapping: Mapping[str, Any]) -> list[str]:
        keys = list(mapping)
        return keys[::-1] if self.style.reversed_keys else keys

    def _flow(self, value: Any) -> str:
        if isinstance(value, dict):
            return "{" + ", ".join(f"{_one_line(key)}: {self._flow(value[key])}" for key in value) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(self._flow(item) for item in value) + "]"
        return _one_line(value)

    def _scalar_lines(self, head: str, value: Any, column: int) -> list[str]:
        style = self.style
        pad = " " * (column + style.indent)
        separator = ": " if head.endswith(":") is False else " "
        if isinstance(value, str) and value and value.isascii():
            if style.block_scalar == "|" and _is_literal_safe(value):
                return [f"{head} |-", *[f"{pad}{line}" for line in value.split("\n")]]
            if style.block_scalar == "|" and value.endswith("\n") and _is_literal_safe(value[:-1]):
                return [f"{head} |", *[f"{pad}{line}" for line in value[:-1].split("\n")]]
            if style.block_scalar == ">-" and _is_plain_prose(value):
                return [f"{head} >-", f"{pad}{value}"]
            if style.multi_line_plain and _is_plain_prose(value):
                first, rest = value.split(" ", 1)
                return [f"{head} {first}", f"{pad}{rest}"]
            if style.single_quoted and "\n" not in value:
                quoted = value.replace("'", "''")
                return self._placed(head, f"'{quoted}'", pad, separator)
        return self._placed(head, _one_line(value), pad, separator)

    def _placed(self, head: str, text: str, pad: str, separator: str) -> list[str]:
        if self.style.value_on_next_line and head.endswith(":"):
            return [head, f"{pad}{text}"]
        return [f"{head}{separator if not head.endswith(':') else ' '}{text}"]

    def mapping(self, mapping: Mapping[str, Any], column: int, in_item: bool = False, top: bool = False) -> list[str]:
        lines: list[str] = []
        pad = " " * column
        if top and self.plant_top:
            lines.append(f"{pad}{PLANTED_KEY}: kept")
        for key in self._keys(mapping):
            lines.extend(self._entry(f"{pad}{_one_line(key)}:", mapping[key], column))
        if in_item and self.plant_items:
            # Last, so a declared key keeps the dash line, where a key's removal is its own form.
            lines.extend(self._item_tail(pad))
        return lines

    def _item_tail(self, pad: str) -> list[str]:
        if not self.style.alias_in_items:
            return [f"{pad}{PLANTED_KEY}: kept"]
        self.anchors += 1
        return [f"{pad}{PLANTED_ANCHOR}: &a{self.anchors} kept", f"{pad}{PLANTED_NOTE}: *a{self.anchors}"]

    def _entry(self, head: str, value: Any, column: int) -> list[str]:
        style = self.style
        if isinstance(value, dict) and value:
            return [head, *self.mapping(value, column + style.indent)]
        if isinstance(value, list) and value:
            if style.flow_lists:
                return self._flow_list(head, value, column)
            return [head, *self.sequence(value, column + (style.indent if style.indented_lists else 0))]
        if isinstance(value, dict | list):
            return [f"{head} {self._flow(value)}"]
        return self._scalar_lines(head, value, column)

    def _flow_list(self, head: str, value: list[Any], column: int) -> list[str]:
        if not self.style.multi_line_flow:
            return [f"{head} {self._flow(value)}"]
        pad = " " * (column + self.style.indent)
        body = [f"{pad}{self._flow(item)}," for item in value]
        return [f"{head} [", *body, f"{pad}]"]

    def sequence(self, items: Sequence[Any], column: int) -> list[str]:
        dash = "-   " if self.style.wide_dash else "- "
        pad = " " * column
        inner = column + len(dash)
        lines: list[str] = []
        for item in items:
            if isinstance(item, dict) and item:
                block = self.mapping(item, inner, in_item=True)
            elif isinstance(item, list) and item:
                block = self.sequence(item, inner)
            elif isinstance(item, dict | list):
                block = [f"{' ' * inner}{self._flow(item)}"]
            else:
                block = self._scalar_item(item, inner)
            lines.extend([f"{pad}{dash}{block[0][inner:]}", *block[1:]])
        return lines

    def _scalar_item(self, item: Any, column: int) -> list[str]:
        pad = " " * column
        if isinstance(item, str) and item and item.isascii() and "\n" not in item:
            if self.style.single_quoted:
                return [f"{pad}'{item.replace(chr(39), chr(39) * 2)}'"]
            if self.style.block_scalar == ">-" and _is_plain_prose(item):
                return [f"{pad}>-", f"{pad}{' ' * self.style.indent}{item}"]
        return [f"{pad}{_one_line(item)}"]


def _inside_a_scalar(text: str) -> set[int]:
    """The lines a comment would land inside: all but the first of each multi-line scalar's."""
    inside: set[int] = set()

    def visit(node: yaml.Node) -> None:
        if isinstance(node, yaml.ScalarNode):
            inside.update(range(node.start_mark.line + 1, _last_line(node, text.split("\n")) + 1))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                visit(item)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                visit(key)
                visit(value)

    visit(compose_yaml_document(text))
    return inside


def _with_comments(text: str, style: Style) -> str:
    """*text* with a numbered comment on every free line, the top and the end included.

    With the style's flags a line of one space and a blank line stand above each comment too, and each line
    that closes its own value carries a comment of its own.
    """
    inside = _inside_a_scalar(text)
    lines = text.split("\n")[:-1]
    planted: list[str] = []
    for number, line in enumerate(lines):
        if number not in inside:
            if style.blank_lines:
                planted += (" ", "")
            planted.append(f"# planted {number}")
        closes = number not in inside and number + 1 not in inside
        planted.append(f"{line}  # trailing {number}" if style.trailing_comments and closes else line)
    planted.append("# planted at the end")
    return "\n".join(planted) + "\n"


def restyled(old: dict[str, Any], style: Style, model_class: type[pydantic.BaseModel], ledger: Ledger) -> str:
    """*old* in *style*, every free line commented, the undeclared key planted wherever it validates.

    Each plant is tried against the class: a closed class refuses the key, and the layout then goes
    without it, counted in the ledger. The result is held to *old*: a layout that changes a value is
    a defect of this emitter, not of the edit.
    """
    reference = canonical(old)
    plants = ((True, True), (True, False), (False, True), (False, False)) if style.plants_keys else ((False, False),)
    for plant_top, plant_items in plants:
        text = "\n".join(_Emitter(style, plant_top, plant_items).mapping(old, 0, top=True)) + "\n"
        try:
            if canonical(validated_dict(model_class, text)) == reference:
                if style.plants_keys and not (plant_top and plant_items):
                    ledger.skipped_plants[model_class.__name__] += 1
                break
        except pydantic.ValidationError:
            continue
    else:
        raise AssertionError(f"the restyle {style.name!r} changed a value of a {model_class.__name__}")
    commented = _with_comments(text, style)
    return commented.replace("\n", "\r\n") if style.crlf else commented


# ---------------------------------------------------------------------------
# Oracle 3
# ---------------------------------------------------------------------------


def _last_line(node: yaml.Node, lines: Sequence[str]) -> int:
    """The last line holding *node*'s own text, from the marks and the lines alone."""
    if isinstance(node, yaml.ScalarNode):
        line = node.end_mark.line
        if node.style in ("|", ">"):
            if node.end_mark.column == 0:
                line -= 1
            while line > node.start_mark.line and not lines[line].strip():
                line -= 1
        return line
    if node.flow_style or not node.value:
        return node.end_mark.line
    tail = node.value[-1]
    return _last_line(tail[1] if isinstance(tail, tuple) else tail, lines)


def _mark(marks: set[int], first: int, last: int) -> None:
    marks.update(range(first, last + 1))


def _is_block(node: yaml.Node, kind: type) -> bool:
    return isinstance(node, kind) and not node.flow_style and bool(node.value)


def _mark_value(marks: set[int], lines: Sequence[str], node: yaml.Node, old: Any, new: Any, on_dash: bool) -> None:
    if _is_block(node, yaml.MappingNode) and isinstance(old, dict) and isinstance(new, dict) and new:
        _mark_mapping(marks, lines, node, old, new, on_dash)
    elif _is_block(node, yaml.SequenceNode) and isinstance(old, list) and isinstance(new, list) and new:
        _mark_sequence(marks, lines, node, old, new)
    else:
        _mark(marks, node.start_mark.line, _last_line(node, lines))


def _mark_mapping(
    marks: set[int],
    lines: Sequence[str],
    node: yaml.MappingNode,
    old: dict[str, Any],
    new: dict[str, Any],
    on_dash: bool,
) -> None:
    pairs = [pair for pair in node.value if isinstance(pair[0], yaml.ScalarNode)]
    held = {pair[0].value: index for index, pair in enumerate(pairs)}
    for name in sorted(new.keys() - old.keys()):
        if on_dash and name not in held:
            # A key that lands before the first one moves that key off the dash line.
            marks.add(node.start_mark.line)
    for name, index in held.items():
        if name not in old and name not in new:
            continue
        before, after = old.get(name, _ABSENT), new.get(name, _ABSENT)
        if before is not _ABSENT and after is not _ABSENT and canonical(before) == canonical(after):
            continue
        key, value = pairs[index]
        if before is _ABSENT or after is _ABSENT or not before or not after:
            _mark(marks, key.start_mark.line, _last_line(value, lines))
            if on_dash and index == 0 and len(pairs) > 1:
                marks.add(pairs[1][0].start_mark.line)
        elif _is_block(value, yaml.MappingNode) or _is_block(value, yaml.SequenceNode):
            if type(before) is type(after):
                _mark_value(marks, lines, value, before, after, False)
                continue
            _mark(marks, key.start_mark.line, _last_line(value, lines))
        else:
            _mark(marks, key.start_mark.line, _last_line(value, lines))


def _mark_sequence(
    marks: set[int], lines: Sequence[str], node: yaml.SequenceNode, old: list[Any], new: list[Any]
) -> None:
    head = 0
    while head < min(len(old), len(new)) and canonical(old[head]) == canonical(new[head]):
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and canonical(old[-1 - tail]) == canonical(new[-1 - tail]):
        tail += 1
    removed = range(head, len(old) - tail)
    if len(removed) == 1 and len(new) - tail - head == 1:
        _mark_value(marks, lines, node.value[head], old[head], new[head], True)
        marks.add(node.value[head].start_mark.line)
        return
    for index in removed:
        item = node.value[index]
        _mark(marks, item.start_mark.line, _last_line(item, lines))


def changed_lines(text: str, old: dict[str, Any], new: dict[str, Any]) -> set[int]:
    """The lines of *text* the change from *old* to *new* may touch, by this module's own reading.

    Computed from the composed old text and the dictionaries: a changed key's lines through its
    value's last line, one item when one item changed, every item of a longer run, and the whole
    of a flow collection. Whatever lies outside these must survive.
    """
    marks: set[int] = set()
    _mark_mapping(marks, text.split("\n"), compose_yaml_document(text), old, new, False)
    return marks


def survives(text: str, changed: str, marks: set[int]) -> str | None:
    """The first line of *text* outside *marks* that *changed* lost or moved out of order, or None."""
    remaining = iter(changed.split("\n"))
    for number, line in enumerate(text.split("\n")):
        if number not in marks and not any(line == other for other in remaining):
            return line
    return None


#: What :func:`stray` strips from an added line: the indentation and an item's dash.
_LINE_HEAD = re.compile(r"\s*(?:-\s+)*")


def stray(text: str, changed: str, written: str) -> str | None:
    """The first line *changed* holds more often than *text* that the writer's dump does not explain.

    The bound on what an edit may add: a value's text is the writer's, so an added line stands in
    *written*, reindented. A comment planted twice, a planted key copied or a gap put back does not.
    A comment of the old text stays allowed on a line whose value changed.
    """
    added = Counter(changed.split("\n")) - Counter(text.split("\n"))
    for line in added:
        bare = _LINE_HEAD.sub("", line.rstrip("\r"), count=1)
        before_comment, mark, comment = bare.partition("  # ")
        if mark and f"{mark}{comment}" in text:
            bare = before_comment
        if "[" in bare or "{" in bare:
            continue
        if (bare and bare not in written) or (not bare and "\n\n" not in written):
            return line
    return None


def _pairs_torn_apart(value: Any) -> bool:
    """Whether a mapping under *value* holds one of the planted pair without the other."""
    if isinstance(value, dict):
        if (PLANTED_ANCHOR in value) is not (PLANTED_NOTE in value):
            return True
        return any(_pairs_torn_apart(item) for item in value.values())
    return isinstance(value, list) and any(_pairs_torn_apart(item) for item in value)


def run_alias_stability(
    work: Path, elements: Sequence[Stored], chosen: Callable[[Stored], Sequence[Mutation]]
) -> Ledger:
    """Oracle 3 over :data:`ALIAS_STYLE`: an edit keeps each item's pair together, or the file ends whole.

    The read-back cannot tell: the pair is undeclared, so a note moved to the next item validates.
    A fallback is the fixed indirect sentence and nothing else, and it is counted in the ledger, so
    the caller can see the layout met both ends. An element of a closed class goes without the pair.
    """
    ledger = Ledger()
    for position, element in enumerate(elements):
        root = work / str(position)
        old = element_to_dict(element.model)
        model_class = type(element.model)
        text = restyled(old, ALIAS_STYLE, model_class, ledger)
        model_root = _workspace(root, "restyled", element, text.encode("utf-8"))
        for mutation in chosen(element):
            outcome = edited_text(model_root, element.target, mutation.model, _prior(element))
            oracle = f"oracle 3, {ALIAS_STYLE.name}"
            if outcome.text is None and outcome.reason == _THROUGH_AN_ALIAS:
                ledger.mutations += 1
                ledger.fallbacks[_THROUGH_AN_ALIAS] += 1
                continue
            changed = ledger.judge(element, mutation, outcome, oracle, old)
            if changed is None:
                continue
            if _pairs_torn_apart(load_yaml_document(changed)):
                ledger.fail(element, mutation, oracle, "a planted key moved to another mapping")
            lost = survives(text, changed, changed_lines(text, old, element_to_dict(mutation.model)))
            if lost is not None:
                ledger.fail(element, mutation, oracle, f"a line outside the change is gone: {lost!r}")
        shutil.rmtree(root)
    return ledger


def _reference_values(root: Path, element: Stored, selected: Sequence[Mutation]) -> dict[int, str]:
    """What the real whole write of each of *selected* validates into; a refused one is left out."""
    reference_root = root / "reference"
    reference_root.mkdir(parents=True)
    values: dict[int, str] = {}
    for index, mutation in enumerate(selected):
        try:
            write_element(str(reference_root), element.target, mutation.model)
        except ValueError:
            continue
        whole = (reference_root / element.target).read_bytes().decode("utf-8")
        values[index] = canonical(validated_dict(type(element.model), whole))
    return values


def run_stability(
    work: Path,
    elements: Sequence[Stored],
    chosen: Callable[[Stored], Sequence[Mutation]],
    styles: Sequence[Style] = STYLES,
) -> Ledger:
    """Oracle 3: each element in each style, each mutation, every line outside the change kept.

    And nothing else: no added line the writer's dump does not explain, and the value the whole
    write gives, so a layout is held to more than the read-back inside the code under test.
    """
    ledger = Ledger()
    for position, element in enumerate(elements):
        root = work / str(position)
        old = element_to_dict(element.model)
        model_class = type(element.model)
        selected = chosen(element)
        prior = _prior(element)
        references = _reference_values(root, element, selected)
        for style in styles:
            text = restyled(old, style, model_class, ledger)
            model_root = _workspace(root, "restyled", element, text.encode("utf-8"))
            for index, mutation in enumerate(selected):
                outcome = edited_text(model_root, element.target, mutation.model, prior)
                oracle = f"oracle 3, {style.name}"
                changed = ledger.judge(element, mutation, outcome, oracle, old)
                if changed is None:
                    continue
                new = element_to_dict(mutation.model)
                lost = survives(text, changed, changed_lines(text, old, new))
                if lost is not None:
                    ledger.fail(element, mutation, oracle, f"a line outside the change is gone: {lost!r}")
                extra = stray(text, changed, _dump_element(new))
                if extra is not None:
                    ledger.fail(element, mutation, oracle, f"a line nothing explains was added: {extra!r}")
                reference = references.get(index)
                if reference is not None and canonical(validated_dict(model_class, changed)) != reference:
                    ledger.fail(element, mutation, oracle, "the edited file holds another value than the whole write")
                if style.crlf and changed.count("\n") != changed.count("\r\n"):
                    ledger.fail(element, mutation, oracle, "the edit left a bare line feed in a CRLF file")
        shutil.rmtree(root)
    return ledger
