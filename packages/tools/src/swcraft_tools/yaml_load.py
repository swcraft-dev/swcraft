"""The one YAML reader: every YAML text SWCraft reads is parsed here, by libyaml.

libyaml (PyYAML's ``CSafeLoader``) is the only parser the product uses, so a file one command reads is read the same
way by every other. Not reached: the VS Code extension, which reads three manifest keys line by line in TypeScript.

**No fallback.** A document libyaml refuses is refused: the ``yaml.YAMLError`` it raises is the caller's to name, and
the pure-Python parser is never asked. A PyYAML build without libyaml is refused on import with an ``ImportError``
naming libyaml. As far as a random comparison on short texts found, libyaml differs from the pure parser on
hand-written shapes only: it accepts a tab used as separation, a block-scalar header followed directly by ``#`` and a
``?`` inside a plain scalar in flow context; it reads ``a: !`` as ``''``; as YAML 1.2.2 does, it refuses an escaped
surrogate (``"\\uD800"``) and a U+FEFF at column 0 that leaves what follows outside its block; against YAML 1.2.2, it
refuses a tab after a block scalar's indentation, an omitted value before a flow indicator (``{a:}``), an empty
explicit flow pair (``[? ]``) and a reserved directive (``%FOO bar``). A U+FEFF at column 0 of a line counts as one
column, so ``"p:\\n\\ufeffa: 1\\n"`` reads ``{'p': {'a': 1}}``.

**A bounded nesting depth.** A document nested deeper than :data:`_NESTING_LIMIT` (1 000) collections is refused
with :class:`NestingTooDeepError`, a ``yaml.YAMLError`` naming the bound and a line and column, before libyaml
composes it; one deeper than the deep copy of a cached read can return (about 492 nested collections) is refused
with it too, naming its depth. So every ``str`` parses or raises ``yaml.YAMLError``. Not reached: Windows' smaller C
stack, unmeasured; and a text over the cache's limit is returned without a copy, so a caller recursing over it may
raise ``RecursionError``.

**One parse per text.** The parse sits under an LRU cache keyed by the text itself, bounded by entries (16 384); a
text over 1 Mi characters is not held. Every read returns a deep copy, so a consumer's mutation never reaches the next
read. The cache is cleared at the start of every CLI command and editing-protocol request; a long-lived server's
direct tools never clear it. Under threads a race costs a re-parse, never a wrong value; :func:`parse_cache_info`
shows the hits.

**A leaf module**: it has readers on both sides of the kernel chain, so it imports nothing from ``swcraft_tools``.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Iterator
from typing import NamedTuple

import yaml

# libyaml or nothing: every binary wheel PyPI publishes carries it, and a build
# without it is refused here, by name, before any text is read.
if not getattr(yaml, "__with_libyaml__", False) or not hasattr(yaml, "CSafeLoader"):
    raise ImportError(
        "swcraft_tools.yaml_load needs PyYAML built with libyaml (yaml.CSafeLoader); "
        "this PyYAML has no libyaml -- install PyYAML's binary wheel"
    )

#: The largest text the cache holds, in characters; a longer one is parsed on every read.
_CACHED_TEXT_LIMIT = 1 << 20

#: The deepest collection nesting this reader composes; libyaml crashed past about 47 600.
_NESTING_LIMIT = 1000

#: The depth the deep copy of a read returns at most, about (it moves with the caller's stack). Named in the
#: refusal of a deeper document.
_RETURNED_NESTING_ABOUT = 490

#: The characters every collection needs at least one of: ``[`` and ``{`` open a
#: flow collection, ``-`` marks a block sequence entry, ``:`` and ``?`` a mapping
#: entry (block or a flow pair).
_COLLECTION_INDICATORS = "[{-:?"


class ParseCacheInfo(NamedTuple):
    """The parse cache's counters, as :func:`functools.lru_cache` reports them."""

    hits: int
    misses: int
    maxsize: int | None
    currsize: int


class NestingTooDeepError(yaml.MarkedYAMLError):
    """A document nested too deep to read: refused as a ``yaml.YAMLError``, never a crash or a ``RecursionError``.

    Raised before composing for a document deeper than :data:`_NESTING_LIMIT`
    collections, and after composing for one the read's deep copy cannot return
    (:func:`_unreturnable_nesting`).
    """


def load_yaml_document(text: str) -> object:
    """Parse one YAML document from *text* with libyaml, or raise ``yaml.YAMLError``.

    Takes the text and never a path, so the caller owns the read (its encoding) and the naming of a refusal.
    Returns a value of its own (a deep copy of the cached document). A cached document too deep for the copy
    is refused with :class:`NestingTooDeepError` on every read of it; a text over the cache's limit is parsed
    and returned without a copy (the module docstring says what that leaves).
    """
    if len(text) > _CACHED_TEXT_LIMIT:
        return _load(text)
    document = _parse(text)
    try:
        return copy.deepcopy(document)
    except RecursionError as exc:
        raise _unreturnable_nesting(text) from exc


def compose_yaml_document(text: str) -> yaml.Node | None:
    """Compose one YAML document from *text* with libyaml into nodes with positions, or raise ``yaml.YAMLError``.

    Each node's ``start_mark`` and ``end_mark`` carry its line, column and ``index``, counted in **characters**
    of *text*. ``None`` for an empty document. Nothing is cached. **What this does not reach:** an alias
    composes to its anchor's node, whose marks are the anchor's; in a text starting with U+FEFF every index is
    one short; a mark after a line break is one column of its line, so what follows it reads one column on.
    """
    try:
        _refuse_deep_nesting(text)
        return yaml.compose(text, Loader=yaml.CSafeLoader)
    except UnicodeEncodeError as exc:
        raise yaml.reader.ReaderError(
            "<unicode string>", exc.start, ord(text[exc.start]), None, "special characters are not allowed"
        ) from exc


def clear_parse_cache() -> None:
    """Forget every parsed document; called at the start of every CLI command and protocol request."""
    _parse.cache_clear()


def parse_cache_info() -> ParseCacheInfo:
    """The cache's hits, misses, bound and current size -- the instrument for the parse counts."""
    return ParseCacheInfo(*_parse.cache_info())


@functools.lru_cache(maxsize=16384)
def _parse(text: str) -> object:
    return _load(text)


def _load(text: str) -> object:
    """libyaml's parse of *text*, with a lone surrogate in the text refused as a ``yaml.YAMLError``.

    libyaml UTF-8-encodes a ``str`` first, so a raw surrogate (from ``surrogateescape``) surfaces as a
    ``UnicodeEncodeError``; it is refused with the ``ReaderError`` the pure parser raised for it.
    """
    try:
        _refuse_deep_nesting(text)
        return yaml.load(text, Loader=yaml.CSafeLoader)
    except UnicodeEncodeError as exc:
        # Positional, not keywords: the exception's ``args`` (its repr, its pickle)
        # hold only positional arguments, and these are the pure parser's own.
        raise yaml.reader.ReaderError(
            "<unicode string>",  # name: what PyYAML's pure ``Reader`` reports for a ``str``
            exc.start,  # position
            ord(text[exc.start]),  # character
            None,  # encoding: a ``str`` has none
            "special characters are not allowed",  # reason: the pure ``Reader``'s own
        ) from exc


def _refuse_deep_nesting(text: str) -> None:
    """Raise :class:`NestingTooDeepError` when *text* nests collections deeper than the bound.

    Every collection holds at least one of :data:`_COLLECTION_INDICATORS` and no indicator belongs to two, so a
    text with no more of them than the bound is let through by counting. Any other text is walked as libyaml's
    event stream, which does not recurse. A parse error met on the walk is left for the composing load to raise.
    """
    if sum(text.count(indicator) for indicator in _COLLECTION_INDICATORS) <= _NESTING_LIMIT:
        return
    too_deep = None
    try:
        for depth, start_mark in _collection_starts(text):
            if depth > _NESTING_LIMIT:
                too_deep = start_mark
                break
    except yaml.YAMLError:
        return
    if too_deep is not None:
        raise NestingTooDeepError(
            None,
            None,
            f"found a collection nested deeper than {_NESTING_LIMIT} levels, the most this reader reads",
            too_deep,
        )


def _unreturnable_nesting(text: str) -> NestingTooDeepError:
    """The :class:`NestingTooDeepError` for a composed *text* whose deep copy raised ``RecursionError``.

    The refusal names the document's depth, the first collection that reaches it, and the depth a read returns.
    """
    deepest, deepest_mark = 0, None
    for depth, start_mark in _collection_starts(text):
        if depth > deepest:
            deepest, deepest_mark = depth, start_mark
    return NestingTooDeepError(
        None,
        None,
        f"found a collection nested {deepest} levels deep, too deep for this reader to return "
        f"(it returns about {_RETURNED_NESTING_ABOUT} levels)",
        deepest_mark,
    )


def _collection_starts(text: str) -> Iterator[tuple[int, yaml.Mark | None]]:
    """Each collection start of *text* with its depth, from libyaml's event stream (which does not recurse)."""
    depth = 0
    for event in yaml.parse(text, Loader=yaml.CSafeLoader):
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            # types-PyYAML types a CSafeLoader event's mark as `yaml._yaml.Mark` beside
            # `yaml.error.Mark`, and `MarkedYAMLError` admits only the latter; the C mark
            # carries the same name, line, column and snippet the error reads.
            yield depth, event.start_mark  # type: ignore[misc]
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
