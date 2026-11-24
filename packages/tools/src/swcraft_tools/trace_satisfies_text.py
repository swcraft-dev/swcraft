"""The trace Apply's edit of a model file: only the ``satisfies`` lines change, every other byte stays.

Each holder's change -- members appended, a member confirmed at a new version -- is an edit of the file's
**text**, located through the composed node tree, whose positions count characters.

**The forms handled**, per holder (the top-level element, or one port or runnable of a component):

- an existing block list ``satisfies:`` with ``- ID@N`` items: a new item line
  after the last one, with that item's exact prefix;
- an existing one-line flow list, ``[A@1, B@2]`` or ``[]``: the member inserted
  before its ``]``;
- no ``satisfies`` key: a new block, indentless as the writer writes a list,
  appended at the end of the file for a top-level element and on the line after
  the item's ``name:`` line for a port or a runnable;
- a confirm: the old item's scalar replaced in place, or, when the current
  member is already listed, the old item's line deleted (block lists only) --
  a comment on that line kept, on a line of its own at the dash's column.

**Every other form is refused for that holder**, never reformatted: the caller reports its links *not
written* with the sentence of :class:`FormRefused`. It imports nothing from :mod:`swcraft_tools` but the leaf
:mod:`~swcraft_tools.yaml_text_edit`.

**What it does not reach.** It proves nothing about the result: the caller reads the edited text back and
compares it with the element it planned. A member is appended after the last item, never sorted. Refused: a
file holding a carriage return or starting with a byte-order mark, and a holder whose ``satisfies`` is an
alias, is anchored or tagged, or is given twice. A mark before the first key of a holder with no
``satisfies`` is left to the caller's read-back, which then refuses the whole run.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import yaml

from swcraft_tools.yaml_text_edit import (
    FormRefused,
    TextEdit,
    composed_root,
    find_key,
    is_indirect,
    line_end,
    line_start,
)

#: A member written as a plain scalar; anything else is single-quoted.
_PLAIN_MEMBER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*@[0-9]+")
#: What may stand before a block item on its line: indentation, the dash, spacing.
_ITEM_PREFIX = re.compile(r"[ ]*-[ ]+")
#: What may follow a single-line value on its line: spacing and an optional comment.
_LINE_REST = re.compile(r"[ \t]*(#.*)?")


@dataclass
class HolderChange:
    """One holder's change: ``member`` is ``None`` for the top-level element, else ``(collection, name)``.

    ``appends`` are the members added, in order; ``confirms`` the ``(old, new)`` pairs.
    """

    member: tuple[str, str] | None
    appends: list[str] = field(default_factory=list)
    confirms: list[tuple[str, str]] = field(default_factory=list)


def scalar_text(member: str) -> str:
    """*member* as it is written: plain when it is a plain ``ID@N``, otherwise single-quoted with ``'`` doubled."""
    if _PLAIN_MEMBER.fullmatch(member):
        return member
    return "'" + member.replace("'", "''") + "'"


def _single_line_rest_is_empty(text: str, node: yaml.Node) -> bool:
    end = node.end_mark.index
    rest = text[end : line_end(text, end)].rstrip("\n")
    return node.start_mark.line == node.end_mark.line and _LINE_REST.fullmatch(rest) is not None


def _holder(root: yaml.Node, member: tuple[str, str] | None) -> yaml.MappingNode:
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise FormRefused("the file is not a block mapping")
    if member is None:
        return root
    collection, name = member
    found = find_key(root, collection)
    if found is None or not isinstance(found[1], yaml.SequenceNode):
        raise FormRefused(f"its {collection} are not a list")
    holders = []
    for item in found[1].value:
        if isinstance(item, yaml.MappingNode):
            named = find_key(item, "name")
            if named is not None and isinstance(named[1], yaml.ScalarNode) and named[1].value == name:
                holders.append(item)
    if len(holders) != 1:
        raise FormRefused(f"its {collection} name '{name}' {'twice' if holders else 'nowhere'}")
    if holders[0].flow_style:
        raise FormRefused("the item is a flow mapping")
    return holders[0]


def _block_items(text: str, sequence: yaml.SequenceNode) -> list[yaml.ScalarNode]:
    items = []
    for item in sequence.value:
        if not isinstance(item, yaml.ScalarNode):
            raise FormRefused("an item is not a single value")
        span = text[item.start_mark.index : item.end_mark.index]
        if span[:1] in ("&", "*", "!") or not _single_line_rest_is_empty(text, item):
            raise FormRefused("an item carries an anchor, a tag or more than its value on its line")
        prefix = text[line_start(text, item.start_mark.index) : item.start_mark.index]
        if _ITEM_PREFIX.fullmatch(prefix) is None:
            raise FormRefused("an item does not stand on a line of its own")
        items.append(item)
    return items


def _flow_items(text: str, sequence: yaml.SequenceNode) -> list[yaml.ScalarNode]:
    if sequence.start_mark.line != sequence.end_mark.line:
        raise FormRefused("a flow list over several lines")
    items = []
    for item in sequence.value:
        span = text[item.start_mark.index : item.end_mark.index]
        if not isinstance(item, yaml.ScalarNode) or span[:1] in ("&", "*", "!"):
            raise FormRefused("an item carries an anchor, a tag or is not a single value")
        items.append(item)
    return items


def _confirm_edits(
    text: str, items: Sequence[yaml.ScalarNode], confirms: Sequence[tuple[str, str]], *, block: bool
) -> list[TextEdit]:
    edits = []
    present = {item.value for item in items}
    for old, new in confirms:
        copies = [item for item in items if item.value == old]
        if len(copies) != 1:
            raise FormRefused(f"'{old}' is listed {'more than once' if copies else 'nowhere'}")
        item = copies[0]
        if new in present:
            if not block:
                raise FormRefused("a confirm that removes an item of a flow list")
            start = line_start(text, item.start_mark.index)
            end = line_end(text, item.end_mark.index)
            # A comment on the removed item's line stays, on a line of its own at the dash's column:
            # the read-back proof cannot see a comment, so nothing else would keep it.
            comment = _LINE_REST.fullmatch(text[item.end_mark.index : end].rstrip("\n"))
            kept = ""
            if comment is not None and comment.group(1) is not None:
                dash = text.index("-", start)
                kept = " " * (dash - start) + comment.group(1) + "\n"
            edits.append(TextEdit(start, end, kept))
        else:
            edits.append(TextEdit(item.start_mark.index, item.end_mark.index, scalar_text(new)))
            present.add(new)
    return edits


def _holder_edits(text: str, holder: yaml.MappingNode, change: HolderChange) -> list[TextEdit]:
    found = find_key(holder, "satisfies")
    if found is None:
        if change.confirms:
            raise FormRefused("a confirm on a holder without a satisfies list")
        if not change.appends:
            return []
        if change.member is None:
            lead = "" if text == "" or text.endswith("\n") else "\n"
            lines = "".join(f"- {scalar_text(member)}\n" for member in change.appends)
            return [TextEdit(len(text), len(text), f"{lead}satisfies:\n{lines}")]
        named = find_key(holder, "name")
        assert named is not None  # _holder found the item by it
        key, value = named
        if not _single_line_rest_is_empty(text, value) or key.start_mark.line != value.start_mark.line:
            raise FormRefused("the item's name does not stand on a single line")
        indent = " " * key.start_mark.column
        at = line_end(text, value.end_mark.index)
        lead = "" if text[:at].endswith("\n") else "\n"
        lines = "".join(f"{indent}- {scalar_text(member)}\n" for member in change.appends)
        return [TextEdit(at, at, f"{lead}{indent}satisfies:\n{lines}")]
    key_node, value = found
    if sum(1 for key, _value in holder.value if isinstance(key, yaml.ScalarNode) and key.value == "satisfies") > 1:
        raise FormRefused("its satisfies key is given twice")
    if is_indirect(text, key_node.end_mark.index, value):
        raise FormRefused("its satisfies is an alias, anchored or tagged")
    if not isinstance(value, yaml.SequenceNode):
        raise FormRefused("its satisfies is not a list")
    if value.flow_style:
        items = _flow_items(text, value)
        edits = _confirm_edits(text, items, change.confirms, block=False)
        if change.appends:
            added = ", ".join(scalar_text(member) for member in change.appends)
            if items:
                at = items[-1].end_mark.index
                edits.append(TextEdit(at, at, f", {added}"))
            else:
                closing = text.rfind("]", value.start_mark.index, value.end_mark.index)
                edits.append(TextEdit(closing, closing, added))
        return edits
    items = _block_items(text, value)
    edits = _confirm_edits(text, items, change.confirms, block=True)
    if change.appends:
        last = items[-1]
        prefix = text[line_start(text, last.start_mark.index) : last.start_mark.index]
        at = line_end(text, last.end_mark.index)
        lead = "" if text[:at].endswith("\n") else "\n"
        lines = "".join(f"{prefix}{scalar_text(member)}\n" for member in change.appends)
        edits.append(TextEdit(at, at, lead + lines))
    return edits


def edit_satisfies(text: str, changes: Sequence[HolderChange]) -> tuple[str, dict[int, str]]:
    """*text* with every holder's change made, and the refused holders: ``{index into changes: reason}``.

    A refused holder's change is left out entirely; the others are made. A text that cannot be composed, holds a
    carriage return, a second document or a document end marker refuses every holder.
    """
    refused: dict[int, str] = {}
    try:
        root = composed_root(text)
    except FormRefused as exc:
        return text, dict.fromkeys(range(len(changes)), str(exc))
    edits: list[TextEdit] = []
    for number, change in enumerate(changes):
        try:
            edits.extend(_holder_edits(text, _holder(root, change.member), change))
        except FormRefused as exc:
            refused[number] = str(exc)
    for edit in sorted(edits, key=lambda edit: (edit.start, edit.end), reverse=True):
        text = text[: edit.start] + edit.text + text[edit.end :]
    return text, refused
