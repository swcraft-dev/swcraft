"""The primitives of editing a YAML file's text in place, so that only the lines a change needs move.

A leaf: it imports nothing from :mod:`swcraft_tools` but the YAML reader and names no layer. Every
position comes from the node tree :func:`~swcraft_tools.yaml_load.compose_yaml_document` composes,
whose marks count characters of the text.

**The edit** (:func:`edit_element_text`). A value's text is always the writer's own, taken from
``written`` at the same path, except a non-empty flow collection (:func:`_flow_text`). List items are
matched by value: a wrong pairing costs diff size, never a value. **The result is not proved here:**
the caller reads it back, and a comment or an undeclared key is what that read-back cannot see.

**Refused anywhere in the file:** the explicit key form (``? key``), a block collection holding an
alias among its own children where :func:`last_end` reads it, the characters of
:func:`_refuse_characters`, everything :func:`composed_root` refuses (a byte-order mark at the start,
a document marker past the first line, a text that cannot be composed, an empty one), line ends not
uniformly LF or CRLF, and a root that is no block mapping.

**Refused on the path of a change only** (a mapping or value it walks through or replaces, an item
of a list it walks): an alias, an anchor, a tag, a merge key, a key given twice, a key written
through an alias, and a walked list that does not hold as many items as the old side, each behind
its own dash. One nested inside a replaced region or elsewhere in the file passes.

**Refused for the one change:** a removal that would leave a list item empty, an indentation that
cannot hold the new text, a change that moves no byte, more changes than :data:`_EDIT_LIMIT`,
changes times the larger of the text and ``written`` past :data:`_RECOMPOSE_BUDGET`, and list
matches costing more than :data:`_MATCH_BUDGET` across one call. The budgets bound the matching and
the recompositions only; composing, walking and the read-back are paid whatever they say.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import chain
from typing import Any, Literal, TypeGuard

import yaml

from swcraft_tools.yaml_load import compose_yaml_document

#: What marks a value as not written where it stands: an anchor, an alias, a tag.
_INDIRECT_MARKS = ("&", "*", "!")

#: More changes than this to one file are refused: each recomposes the text, and a
#: change of that size has no small diff to protect.
_EDIT_LIMIT = 64

_TOO_MANY_PLACES = "the change touches too many places in the file"

#: What the recompositions of one edit may cost: the changes times the larger of the text and
#: ``written``, since each change composes the text anew. About 2 s of composing.
_RECOMPOSE_BUDGET = 16_000_000

#: What the list matches of one walk may cost, each costing ``min(n, m) * (n + pairs)``: ``n`` and
#: ``m`` the two lengths past the common ends, ``pairs`` the sum over the values of their count on
#: one side by their count on the other -- the matcher's own cost shape. It is paid before the matcher
#: runs, since slowness is nothing an ``except`` catches. Under 2 s at worst.
_MATCH_BUDGET = 32_000_000

_INDIRECT = "the file writes a value through an alias, an anchor or a tag"
_MERGE_TAG = "tag:yaml.org,2002:merge"
#: Says what the walk met, under a warning that says the file cannot be edited line by line.
_NO_LINE = "the line edit found no line to change"

#: A flow collection stays on one line: past any line a file holds, so nothing wraps.
_FLOW_WIDTH = 2**31 - 1

#: What precedes an item on its line: the dash and spacing, and nothing else.
_DASH_LINE = re.compile(r" *- *")

#: A place in a dictionary or a composed tree: a mapping's key, or a list's index.
_Path = tuple[str | int, ...]


class FormRefused(Exception):
    """The text, or the part of it to edit, is in a form this editing does not handle; the message says which."""


@dataclass(frozen=True)
class TextEdit:
    """The text between ``start`` and ``end`` replaced by ``text``."""

    start: int
    end: int
    text: str


def line_start(text: str, index: int) -> int:
    """The index where the line holding *index* starts."""
    return text.rfind("\n", 0, index) + 1


def line_end(text: str, index: int) -> int:
    """The index just past the line's ``\\n`` (the text's length on a last line without one)."""
    found = text.find("\n", index)
    return len(text) if found < 0 else found + 1


def find_key(mapping: yaml.MappingNode, name: str) -> tuple[yaml.Node, yaml.Node] | None:
    """The first pair of *mapping* whose key is the scalar *name*, as ``(key, value)`` nodes."""
    for key, value in mapping.value:
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            return key, value
    return None


def _refuse_displaced(collection: yaml.CollectionNode) -> None:
    """Refuse a block *collection* with a child that stands before what precedes it: that child is an alias.

    Every child is read, not the last alone: two aliases written in their anchors' order look in
    place beside each other, and only the anchor between them tells.
    """
    floor = collection.start_mark.index
    children = chain.from_iterable(collection.value) if isinstance(collection, yaml.MappingNode) else collection.value
    for child in children:
        if child.start_mark.index < floor:
            raise FormRefused(_INDIRECT)
        floor = max(floor, child.end_mark.index)


def last_end(node: yaml.Node) -> int:
    """The index just past *node*'s own text: its last descendant's end, for a block collection.

    A block scalar's end is already a line start, so a caller never takes
    :func:`line_end` of it: that would consume the next line.

    Raises:
        FormRefused: when a collection under *node* holds an alias, whose marks are
            the anchor's, including a sequence that is its own only item. *node*
            itself is the caller's to check (:func:`_guarded_end`).
    """
    return _last_descendant(node).end_mark.index


def _last_descendant(node: yaml.Node) -> yaml.Node:
    """The node whose text closes *node*'s, under the guards :func:`last_end` states."""
    # Without it a sequence that is its own only item (an alias to itself) would loop forever.
    seen: set[int] = set()
    while isinstance(node, yaml.CollectionNode) and not node.flow_style and node.value:
        if id(node) in seen:
            raise FormRefused(_INDIRECT)
        seen.add(id(node))
        _refuse_displaced(node)
        last = node.value[-1]
        node = last[1] if isinstance(node, yaml.MappingNode) else last
    return node


def is_indirect(text: str, after: int, value: yaml.Node) -> bool:
    """Whether *value* is an alias, is anchored or is tagged; *after* is where its key, or what precedes it, ends.

    An edit of either would change more than the value at hand.
    """
    # Between the key and its value: the colon, spacing and comments -- whose text is no token.
    between = re.sub(r"#[^\n]*", "", text[after : value.start_mark.index])
    return (
        value.start_mark.index < after
        or text[value.start_mark.index : value.start_mark.index + 1] in _INDIRECT_MARKS
        or any(mark in between for mark in _INDIRECT_MARKS)
    )


def _refuse_indirect(text: str, after: int, value: yaml.Node) -> None:
    """Refuse what :func:`is_indirect` finds."""
    if is_indirect(text, after, value):
        raise FormRefused(_INDIRECT)


def composed_root(text: str) -> yaml.Node:
    """*text* composed, or :class:`FormRefused` with a fixed sentence when it is no text to edit in place.

    Refused: a carriage return, a leading byte-order mark, a marker past the very
    first line, a text that cannot be composed, an empty one.
    """
    if "\r" in text:
        raise FormRefused("the file has carriage-return line ends")
    if text.startswith("\ufeff"):
        raise FormRefused("the file starts with a byte-order mark")
    # A marker anywhere but the very first line: a second document or a document end.
    if any(found.start() > 0 for found in re.finditer(r"^(?:\.\.\.|---)", text, re.MULTILINE)):
        raise FormRefused("the file holds a document marker")
    try:
        root = compose_yaml_document(text)
    except yaml.YAMLError as exc:
        raise FormRefused("the file cannot be read as YAML") from exc
    if root is None:
        raise FormRefused("the file is empty")
    return root


def _refuse_characters(text: str) -> None:
    """Refuse what this edit's line tests cannot read.

    U+0085, U+2028 and U+2029 are line breaks to libyaml and not to
    :func:`line_start`. A U+FEFF is no whitespace to the line tests, so a line
    holding one could be misread.
    """
    if "\ufeff" in text:
        raise FormRefused("the file holds a byte-order mark")
    if any(character in text for character in "\x85\u2028\u2029"):
        raise FormRefused("the file holds a character YAML reads as a line break")


def _nodes(root: yaml.Node) -> Iterator[yaml.Node]:
    """Every node under *root*, keys left out, each once: an alias can point back at its own collection."""
    seen: set[int] = set()
    waiting = [root]
    while waiting:
        node = waiting.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        if isinstance(node, yaml.MappingNode):
            waiting.extend(value for _, value in node.value)
        elif isinstance(node, yaml.SequenceNode):
            waiting.extend(node.value)


def _refuse_explicit_keys(text: str, root: yaml.Node) -> None:
    """Refuse a ``? key`` anywhere in a block mapping: such a key can hold a value marked past a comment."""
    for node in _nodes(root):
        if isinstance(node, yaml.MappingNode) and not node.flow_style:
            for key, _ in node.value:
                after = key.end_mark.index
                if not text[after : line_end(text, after)].lstrip(" \t").startswith(":"):
                    raise FormRefused("the file writes a key in the explicit form")


def _refuse_mapping(mapping: yaml.MappingNode) -> None:
    """Refuse a mapping a change walks through: a key given twice, merged in from elsewhere, or an alias.

    A key written through an alias is marked at its anchor, and a line edit is placed by a key's marks.
    A value's alias is left to the value's change.
    """
    names: set[str] = set()
    floor = mapping.start_mark.index
    for key, value in mapping.value:
        if key.start_mark.index < floor:
            raise FormRefused(_INDIRECT)
        floor = max(floor, key.end_mark.index, value.end_mark.index)
        if key.tag == _MERGE_TAG:
            raise FormRefused("the file uses a merge key")
        if isinstance(key, yaml.ScalarNode):
            if key.value in names:
                raise FormRefused("the file gives a key twice")
            names.add(key.value)


def _is_block_mapping(node: yaml.Node) -> TypeGuard[yaml.MappingNode]:
    return isinstance(node, yaml.MappingNode) and not node.flow_style and bool(node.value)


def _is_block_list(node: yaml.Node) -> TypeGuard[yaml.SequenceNode]:
    return isinstance(node, yaml.SequenceNode) and not node.flow_style and bool(node.value)


def same_value(old: Any, new: Any) -> bool:
    """Equality that tells ``1``, ``1.0`` and ``True`` apart, at every depth."""
    walk = _Walk()
    return walk.form(old) == walk.form(new)


def _in_its_span(node: yaml.Node, new: Any) -> bool:
    """Whether *new* replaces *node*'s span: a scalar by a scalar, a non-empty flow collection by a collection."""
    collection = isinstance(new, dict | list)
    if isinstance(node, yaml.ScalarNode):
        return not collection
    if not isinstance(node, yaml.CollectionNode):
        raise TypeError(f"a composed node is a scalar or a collection, not {type(node).__name__}")
    return bool(node.flow_style and node.value and collection)


@dataclass(frozen=True)
class _Change:
    """One key of the mapping at ``path`` to replace, add or remove; ``written_path`` is that mapping in ``written``."""

    kind: Literal["replace", "add", "remove"]
    name: str
    path: _Path
    written_path: _Path
    #: For an add: the keys before this one in the writer's order.
    before: tuple[str, ...] = ()
    #: For a replace: the new value.
    value: Any = None


@dataclass(frozen=True)
class _ItemChange:
    """Item ``index`` of the list at ``path`` replaced within its span by ``written_index`` of ``written``'s list."""

    path: _Path
    written_path: _Path
    index: int
    written_index: int
    value: Any


@dataclass(frozen=True)
class _ItemsChange:
    """Items ``start`` to ``stop`` of the list at ``path`` out, ``written_start`` to ``written_stop`` in."""

    path: _Path
    written_path: _Path
    start: int
    stop: int
    written_start: int
    written_stop: int


_AnyChange = _Change | _ItemChange | _ItemsChange


def _scalar_key(value: Any) -> tuple[str, str]:
    """*value* as two strings: equal for two scalars only when type and value are, ``0.0`` and ``-0.0`` apart.

    Strings, never a number: Python's hash of a number is public arithmetic, so a file could hold
    values that collide before either budget is read, where a string's hash is seeded per process.
    That covers this module's own keys alone (the YAML reader's, pydantic's and the model's
    dictionaries hash their own), and a fixed ``PYTHONHASHSEED`` gives the seed away. An integer is
    spelled in hexadecimal because ``repr`` refuses one past Python's limit on decimal digits, and
    ``0x`` followed by any number of digits is valid YAML.
    """
    return type(value).__name__, hex(value) if isinstance(value, int) else repr(value)


@dataclass
class _Walk:
    """What one walk keeps: one number per distinct value, and what its list matches have cost.

    A collection is remembered by ``id()``, valid only while the two sides are alive and unchanged,
    so an instance serves one :func:`edit_element_text` call. Every key of ``_numbers`` is two
    strings, a collection's too: its members' numbers are predictable, and a hash over them could be
    solved for.
    """

    spent: int = 0
    _numbers: dict[tuple[str, str], int] = field(default_factory=dict)
    _collections: dict[int, int] = field(default_factory=dict)

    def form(self, value: Any) -> int:
        """*value*'s number: equal for two values only when they are, type-strict and key order aside."""
        if not isinstance(value, dict | list):
            return self._number(_scalar_key(value))
        known = self._collections.get(id(value))
        if known is None:
            if isinstance(value, dict):
                pairs = sorted((self.form(name), self.form(item)) for name, item in value.items())
                known = self._number(("map", ",".join(f"{name}:{item}" for name, item in pairs)))
            else:
                known = self._number(("list", ",".join(str(self.form(item)) for item in value)))
            self._collections[id(value)] = known
        return known

    def _number(self, canonical: tuple[str, str]) -> int:
        return self._numbers.setdefault(canonical, len(self._numbers))

    def pay(self, old: Sequence[int], new: Sequence[int]) -> None:
        """Add the match of *old* and *new*, or refuse it before the matcher that would pass the budget."""
        counts = Counter(new)
        pairs = sum(counts[item] for item in old)
        self.spent += min(len(old), len(new)) * (len(old) + pairs)
        if self.spent > _MATCH_BUDGET:
            raise FormRefused("a list changed along too many items to match them")


def _changes(
    text: str,
    mapping: yaml.MappingNode,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    path: _Path,
    written_path: _Path,
    walk: _Walk,
) -> list[_AnyChange]:
    """What turns the *mapping* the file holds from *old* into *new*: removals first, so they apply last.

    A mapping's last stated key on a ``- `` line can go only once its successor stands beside it.
    """
    _refuse_mapping(mapping)
    pairs = {key.value: (key, value) for key, value in mapping.value if isinstance(key, yaml.ScalarNode)}
    removed: list[_AnyChange] = []
    for name in old:
        found = pairs.get(name)
        if name not in new and found is not None:
            _refuse_indirect(text, found[0].end_mark.index, found[1])
            removed.append(_Change("remove", name, path, written_path))
    changed: list[_AnyChange] = []
    for position, name in enumerate(new):
        if name in old and walk.form(old[name]) == walk.form(new[name]):
            continue
        found = pairs.get(name)
        if found is None:
            # Read here too, not only after the walk: each addition copies the names before it.
            if len(changed) >= _EDIT_LIMIT:
                raise FormRefused(_TOO_MANY_PLACES)
            changed.append(_Change("add", name, path, written_path, tuple(new)[:position]))
            continue
        key, value = found
        _refuse_indirect(text, key.end_mark.index, value)
        before, after = old.get(name), new[name]
        here, written_here = (*path, name), (*written_path, name)
        if _is_block_mapping(value) and isinstance(before, dict) and isinstance(after, dict) and after:
            changed.extend(_changes(text, value, before, after, here, written_here, walk))
        elif _is_block_list(value) and isinstance(before, list) and isinstance(after, list) and after:
            changed.extend(_list_changes(text, value, before, after, key.end_mark.index, here, written_here, walk))
        else:
            changed.append(_Change("replace", name, path, written_path, value=after))
    return removed + changed


def _refuse_items(text: str, sequence: yaml.SequenceNode, after: int) -> None:
    """Refuse a list the walk cannot place by lines; *after* is where its key ends.

    Each item must be written where it stands, alone behind a dash on a line, and
    hold no list of its own there: ``- - a`` has two items' dashes on one line.
    """
    for item in sequence.value:
        _refuse_indirect(text, after, item)
        start = item.start_mark.index
        on_its_line = _DASH_LINE.fullmatch(text[line_start(text, start) : start])
        if on_its_line is None or (isinstance(item, yaml.SequenceNode) and not item.flow_style):
            raise FormRefused("a list item does not stand on a line of its own")
        after = _guarded_end(start, item)


def _list_changes(
    text: str,
    sequence: yaml.SequenceNode,
    old: Sequence[Any],
    new: Sequence[Any],
    after: int,
    path: _Path,
    written_path: _Path,
    walk: _Walk,
) -> list[_AnyChange]:
    """What turns the block *sequence* the file holds from *old* into *new*, in the order of its items."""
    if len(sequence.value) != len(old):
        raise FormRefused("a list in the file differs in length from the model's")
    _refuse_items(text, sequence, after)
    old_forms = [walk.form(item) for item in old]
    new_forms = [walk.form(item) for item in new]
    shortest = min(len(old), len(new))
    head = 0
    while head < shortest and old_forms[head] == new_forms[head]:
        head += 1
    tail = 0
    while tail < shortest - head and old_forms[-1 - tail] == new_forms[-1 - tail]:
        tail += 1
    old_items = old_forms[head : len(old) - tail]
    new_items = new_forms[head : len(new) - tail]
    walk.pay(old_items, new_items)
    matcher = SequenceMatcher(None, old_items, new_items, autojunk=False)
    changes: list[_AnyChange] = []
    for tag, old_start, old_stop, new_start, new_stop in matcher.get_opcodes():
        start, written_start = old_start + head, new_start + head
        count = old_stop - old_start
        if tag == "replace" and count == new_stop - new_start:
            for shift in range(count):
                index, written_index = start + shift, written_start + shift
                changes.extend(
                    _item_changes(
                        text,
                        sequence.value[index],
                        old[index],
                        new[written_index],
                        path,
                        written_path,
                        index,
                        written_index,
                        walk,
                    )
                )
        elif tag != "equal":
            changes.append(_ItemsChange(path, written_path, start, start + count, written_start, new_stop + head))
    return changes


def _item_changes(
    text: str,
    item: yaml.Node,
    old: Any,
    new: Any,
    path: _Path,
    written_path: _Path,
    index: int,
    written_index: int,
    walk: _Walk,
) -> list[_AnyChange]:
    """What turns the pair of items, one replaced by the other: their keys, their span, or their lines."""
    if _is_block_mapping(item) and isinstance(old, dict) and isinstance(new, dict) and new:
        return _changes(text, item, old, new, (*path, index), (*written_path, written_index), walk)
    if _in_its_span(item, new):
        return [_ItemChange(path, written_path, index, written_index, new)]
    return [_ItemsChange(path, written_path, index, index + 1, written_index, written_index + 1)]


def _node_at(root: yaml.Node, path: _Path) -> yaml.Node:
    node = root
    for step in path:
        if isinstance(step, int):
            assert isinstance(node, yaml.SequenceNode)
            node = node.value[step]
        else:
            assert isinstance(node, yaml.MappingNode)
            found = find_key(node, step)
            assert found is not None
            node = found[1]
    return node


def _mapping_at(root: yaml.Node, path: _Path) -> yaml.MappingNode:
    node = _node_at(root, path)
    assert isinstance(node, yaml.MappingNode)
    return node


def _sequence_at(root: yaml.Node, path: _Path) -> yaml.SequenceNode:
    node = _node_at(root, path)
    assert isinstance(node, yaml.SequenceNode)
    return node


def _pair(mapping: yaml.MappingNode, name: str) -> tuple[yaml.Node, yaml.Node]:
    found = find_key(mapping, name)
    assert found is not None
    return found


def _guarded_end(after: int, value: yaml.Node) -> int:
    """:func:`last_end` of *value*, which stands past *after*, or a refusal when it is an alias marked before it.

    :func:`last_end` guards what lies under *value*; this is the check of *value* itself.
    """
    if value.start_mark.index < after:
        raise FormRefused(_INDIRECT)
    return last_end(value)


def _value_end(text: str, after: int, value: yaml.Node) -> int:
    """Where the text of *value*, which stands past *after*, ends, its trailing blank lines left to the file.

    A line of spaces alone is as blank as an empty one, unless it is a block scalar's last line of
    content (:func:`_closes_on_spaces`). What this does not reach: behind such a scalar a line of
    spaces goes with its lines; and the breaks of a ``|+`` stay in the file, which the caller's
    read-back judges.
    """
    end = _guarded_end(after, value)
    spaces_are_value = _closes_on_spaces(_last_descendant(value))
    while end > after and text[end - 1] == "\n":
        end -= 1
        start = line_start(text, end)
        if not spaces_are_value and start > after and not text[start:end].strip(" "):
            end = start
    return end


def _closes_on_spaces(node: yaml.Node) -> bool:
    """Whether the last line of *node*'s value, past its trailing breaks, holds spaces and nothing else."""
    if not isinstance(node, yaml.ScalarNode):
        return False
    last_line = node.value.rstrip("\n").rpartition("\n")[2]
    return bool(last_line) and not last_line.strip(" ")


def _shifted(fragment: str, shift: int) -> str:
    """*fragment* with every line after its first moved by *shift* columns; a blank line stays blank."""
    first, *rest = fragment.split("\n")
    lines = [first]
    for line in rest:
        if shift >= 0 or not line:
            lines.append(" " * shift + line if line else line)
        elif line[:-shift].strip(" "):
            raise FormRefused("the file's indentation cannot hold the new text")
        else:
            lines.append(line[-shift:])
    return "\n".join(lines)


def _flow_text(value: Any) -> str:
    """*value* as one line of flow: the keys in the writer's order, nothing wrapped."""
    return yaml.safe_dump(value, default_flow_style=True, width=_FLOW_WIDTH, sort_keys=False).rstrip("\n")


def _span_edit(
    text: str,
    value: yaml.Node,
    after: int,
    written: str,
    written_value: yaml.Node,
    written_after: int,
    shift: int,
    new: Any,
) -> TextEdit:
    """*value*'s span replaced; an implicit null, an empty span, takes a leading space."""
    start = value.start_mark.index
    end = _value_end(text, after, value)
    if isinstance(value, yaml.ScalarNode):
        fragment = written[written_value.start_mark.index : _value_end(written, written_after, written_value)]
        fragment = _shifted(fragment, shift)
    else:
        fragment = _flow_text(new)
    return TextEdit(start, end, ("" if end > start else " ") + fragment)


def _replacement(
    text: str, mapping: yaml.MappingNode, written: str, written_mapping: yaml.MappingNode, name: str, new: Any
) -> TextEdit:
    """The value of *name* replaced: within its span where :func:`_in_its_span` says so, else the value region."""
    key, value = _pair(mapping, name)
    written_key, written_value = _pair(written_mapping, name)
    shift = key.start_mark.column - written_key.start_mark.column
    after, written_after = key.end_mark.index, written_key.end_mark.index
    if _in_its_span(value, new):
        return _span_edit(text, value, after, written, written_value, written_after, shift, new)
    fragment = written[written_after : _value_end(written, written_after, written_value)]
    return TextEdit(after, _value_end(text, after, value), _shifted(fragment, shift))


def _line_insertion(text: str, end: int, lines: str) -> TextEdit:
    """*lines*, which hold no last break, as lines of their own after the line of *end*."""
    if text[end - 1] == "\n":  # A block scalar: its breaks are its own, and this is already a line start.
        return TextEdit(end, end, f"{lines}\n")
    at = line_end(text, end)
    if text[at - 1] == "\n":
        return TextEdit(at, at, f"{lines}\n")
    return TextEdit(at, at, f"\n{lines}")


def _key_insertion(
    text: str,
    mapping: yaml.MappingNode,
    written: str,
    written_mapping: yaml.MappingNode,
    name: str,
    before: Sequence[str],
) -> TextEdit:
    """*name* added after the nearest of *before* the file holds, else before the file's first key of *mapping*."""
    written_key, written_value = _pair(written_mapping, name)
    column = mapping.value[0][0].start_mark.column
    fragment = _shifted(
        written[written_key.start_mark.index : _value_end(written, written_key.end_mark.index, written_value)],
        column - written_key.start_mark.column,
    )
    indent = " " * column
    for prior in reversed(before):
        found = find_key(mapping, prior)
        if found is None:
            continue
        return _line_insertion(text, _guarded_end(found[0].end_mark.index, found[1]), f"{indent}{fragment}")
    at = mapping.value[0][0].start_mark.index
    return TextEdit(at, at, f"{fragment}\n{indent}")


def _key_removal(text: str, mapping: yaml.MappingNode, name: str) -> TextEdit:
    """*name*'s lines through its last descendant; on a ``- `` line, from the key to the next key.

    The next key takes the dash, so the lines between the two, a comment or a blank
    one, cannot stay where they stand: they move above the item, in their order.
    """
    key, value = _pair(mapping, name)
    start = key.start_mark.index
    last = _value_end(text, key.end_mark.index, value)
    if not text[line_start(text, start) : start].strip():
        return TextEdit(line_start(text, start), line_end(text, last), "")
    keys = [node for node, _ in mapping.value]
    following = keys[keys.index(key) + 1 :]
    if not following:
        raise FormRefused("the file would be left with an empty list item")
    next_key = following[0].start_mark.index
    gap = line_end(text, last)
    dash_line = line_start(text, start)
    return TextEdit(dash_line, next_key, text[gap : line_start(text, next_key)] + text[dash_line:start])


def _dash(text: str, item: yaml.Node) -> int:
    """The index of the dash on *item*'s line, which :func:`_refuse_items` has seen stands there."""
    return text.index("-", line_start(text, item.start_mark.index))


def _item_replacement(
    text: str, sequence: yaml.SequenceNode, written: str, written_sequence: yaml.SequenceNode, change: _ItemChange
) -> TextEdit:
    """One item replaced within its span."""
    item = sequence.value[change.index]
    written_item = written_sequence.value[change.written_index]
    shift = item.start_mark.column - written_item.start_mark.column
    return _span_edit(
        text, item, item.start_mark.index, written, written_item, written_item.start_mark.index, shift, change.value
    )


def _items_fragment(
    text: str, sequence: yaml.SequenceNode, written: str, written_sequence: yaml.SequenceNode, change: _ItemsChange
) -> str:
    """The new items' lines of *written* at the column of the file's list, or an empty string when there are none."""
    if change.written_start == change.written_stop:
        return ""
    first = written_sequence.value[change.written_start]
    last = written_sequence.value[change.written_stop - 1]
    written_dash = _dash(written, first)
    column = _dash(text, sequence.value[0]) - line_start(text, sequence.value[0].start_mark.index)
    fragment = written[written_dash : _value_end(written, last.start_mark.index, last)]
    return " " * column + _shifted(fragment, column - (written_dash - line_start(written, written_dash)))


def _items_edits(
    text: str, sequence: yaml.SequenceNode, written: str, written_sequence: yaml.SequenceNode, change: _ItemsChange
) -> list[TextEdit]:
    """Each old item's lines out, so a comment between them stays; the new items' lines in the place of the first."""
    lines = _items_fragment(text, sequence, written, written_sequence, change)
    items = sequence.value
    edits = []
    for item in items[change.start : change.stop]:
        end = line_end(text, _value_end(text, item.start_mark.index, item))
        edits.append(TextEdit(line_start(text, item.start_mark.index), end, ""))
    if edits:
        first = edits[0]
        kept_break = "\n" if text[first.end - 1] == "\n" else ""
        if lines:
            edits[0] = TextEdit(first.start, first.end, f"{lines}{kept_break}")
        return edits
    if change.start:
        prior = items[change.start - 1]
        return [_line_insertion(text, _guarded_end(prior.start_mark.index, prior), lines)]
    at = line_start(text, items[0].start_mark.index)
    return [TextEdit(at, at, f"{lines}\n")]


def _segments(text: str, edits: Sequence[TextEdit]) -> list[str]:
    """What stands between *edits*, which are in order and overlap nowhere, and what they put there.

    Joined once, so one change costs one pass over its text however many edits it holds: one change
    can hold an edit per removed item, which no count of changes sees.
    """
    segments: list[str] = []
    at = 0
    for edit in edits:
        segments += (text[at : edit.start], edit.text)
        at = edit.end
    segments.append(text[at:])
    return segments


def _applied(text: str, written: str, written_root: yaml.Node, change: _AnyChange) -> str:
    """*text* with *change* made; composed here, so the positions are this text's."""
    root = composed_root(text)
    if isinstance(change, _Change):
        mapping = _mapping_at(root, change.path)
        written_mapping = _mapping_at(written_root, change.written_path)
        if change.kind == "remove":
            edits = [_key_removal(text, mapping, change.name)]
        elif change.kind == "add":
            edits = [_key_insertion(text, mapping, written, written_mapping, change.name, change.before)]
        else:
            edits = [_replacement(text, mapping, written, written_mapping, change.name, change.value)]
    else:
        sequence = _sequence_at(root, change.path)
        written_sequence = _sequence_at(written_root, change.written_path)
        if isinstance(change, _ItemChange):
            edits = [_item_replacement(text, sequence, written, written_sequence, change)]
        else:
            edits = _items_edits(text, sequence, written, written_sequence, change)
    return "".join(_segments(text, edits))


def edit_element_text(text: str, old: Mapping[str, Any], new: Mapping[str, Any], written: str) -> str:
    """*text* with only the lines changed that turn *old* into *new*, or :class:`FormRefused`, a fixed sentence.

    Args:
        text: the file's text.
        old: the writer's dictionary of the element the file holds.
        new: the writer's dictionary of the element to hold.
        written: the writer's whole dump of *new*, where every new value's text comes from.

    Raises:
        FormRefused: every refusal the module docstring's *The refusals' reach* lists. A change
            that moves no byte is one too: the caller's whole write decides what it means.
    """
    _refuse_characters(text)
    crlf = "\r" in text
    if crlf:
        if not text.count("\r") == text.count("\r\n") == text.count("\n"):
            raise FormRefused("the file has mixed line ends")
        text = text.replace("\r\n", "\n")
    root = composed_root(text)
    if not _is_block_mapping(root):
        raise FormRefused("the file's root is no block mapping")
    _refuse_indirect(text, 0, root)
    _refuse_explicit_keys(text, root)
    changes = _changes(text, root, old, new, (), (), _Walk())
    if not changes:
        raise FormRefused(_NO_LINE)
    if len(changes) > _EDIT_LIMIT:
        raise FormRefused(_TOO_MANY_PLACES)
    if len(changes) * max(len(text), len(written)) > _RECOMPOSE_BUDGET:
        raise FormRefused("the change touches too many places for a file of this size")
    written_root = composed_root(written)
    _refuse_explicit_keys(written, written_root)
    edited = text
    for change in reversed(changes):
        edited = _applied(edited, written, written_root, change)
    if edited == text:
        raise FormRefused(_NO_LINE)
    return edited.replace("\n", "\r\n") if crlf else edited
