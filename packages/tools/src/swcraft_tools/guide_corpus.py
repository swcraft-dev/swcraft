"""The guide's corpus: the user guide cut into sections, for the ``guide`` tool to read.

``python -m swcraft_tools.guide_corpus --guide-dir DIR --out FILE`` reads the guide's Markdown under
*DIR*, as the site build sees it, and writes one JSON document, the corpus the MCP tool ``guide``
answers from (:mod:`swcraft_tools.guide_search`). ``--profiles FILE...`` in place of ``--out``
rewrites, in each agent profile, the region between :data:`INDEX_BEGIN_MARKER` and
:data:`INDEX_END_MARKER` with the index of the How-to pages (:func:`render_profile_index`).
The release notes (:data:`RELEASE_NOTES_SUBTREE`) are never read: they describe earlier releases.

A page is cut at each ``##`` and ``###`` outside a fenced block; an id is the page's site name
(``how-to/add-a-component``) or ``<page>#<anchor>``, with python-markdown's own anchors, and reading
it returns its subtree. Single-line authoring comments under a heading are read and removed:
``<!-- purpose: … -->`` (at most :data:`~swcraft_tools.guide_search.PURPOSE_CAP_CHARACTERS`
characters), ``<!-- keywords: … -->`` under the H1 and ``<!-- explains: … -->`` (refusal codes).
A subtree is split into parts at block boundaries so that every ``section`` reply fits
:data:`~swcraft_tools.guide_search.GUIDE_REPLY_BUDGET_WIRE_BYTES`.

The corpus is refused when:

- a page's line-by-line headings differ from the headings Markdown reads;
- a composed reply is over the budget;
- any text names a location of the build host (its directories, a ``~/`` or drive-letter spelling,
  or a root spelling such as ``//`` or a bare ``file://``);
- an entry section (:func:`entry_sections`) holds in a fenced block an identifier
  (:func:`code_identifiers`) its prose does not name and :data:`IDENTIFIER_ALLOWANCES` does not admit;
- ``markdown`` or ``mkdocs`` (development dependencies) are absent.

It reads the Markdown, not the rendered HTML, and matches an identifier's spelling, not its meaning;
it cannot tell whether a section's prose is true.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swcraft_tools import guide_search
from swcraft_tools._version import __version__
from swcraft_tools.guide_search import (
    CORPUS_FILENAME,
    CORPUS_NAME,
    CORPUS_SCHEMA,
    GUIDE_REPLY_BUDGET_WIRE_BYTES,
    PACKING_PART_PLACEHOLDER,
    PURPOSE_CAP_CHARACTERS,
    REQUEST_CAP_CHARACTERS,
    SEARCH_HIT_LIMIT,
    GuideIndex,
)
from swcraft_tools.wire_unit import escaped_bytes, measure

__all__ = [
    "CORPUS_FILENAME",
    "RELEASE_NOTES_SUBTREE",
    "HOW_TO_PAGE",
    "INDEX_ANSWER_RULE",
    "INDEX_BEGIN_MARKER",
    "INDEX_END_MARKER",
    "INDEX_RECIPE_COMMENT",
    "INDEX_REFUSAL_EXAMPLE",
    "IDENTIFIER_ALLOWANCES",
    "TOOL_POINTER_SECTIONS",
    "GuideCorpusError",
    "build_corpus",
    "check_every_reply",
    "code_identifiers",
    "effective_markdown_config",
    "entry_sections",
    "find_mkdocs_config",
    "host_locations",
    "identifier_violations",
    "is_release_notes",
    "main",
    "page_name",
    "produce",
    "recipe_sections",
    "render_profile_index",
    "rewrite_profile_index",
    "root_spellings",
    "scan_for_locations",
    "write_corpus",
    "write_profile_indexes",
]

MARKDOWN_ABSENT = (
    "the guide's corpus is built with `markdown` and `mkdocs`, development dependencies that this "
    "environment does not have; run it from the development environment the docs build uses"
)

#: The site's *Release notes* section: its index (``release-notes.md`` or
#: ``release-notes/index.md``) and every note below it. Never parsed into the corpus.
RELEASE_NOTES_SUBTREE = "release-notes"

#: A location spelled from the user's home directory -- the reply gate's ``HOME_SPELLED``.
HOME_SPELLED = re.compile(r"(?<![\w~.-])~[\\/]")

#: A drive-letter location -- the reply gate's ``DRIVE_LETTER_PATH``.
DRIVE_LETTER_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")

#: A token the reply gate reads as an absolute POSIX path -- its ``POSIX_SPELLED``.
POSIX_SPELLED = re.compile(r"(?<![\w.~-])/[^\s'\"()\[\]{}<>,;|`]+")

_COL0_FENCE = re.compile(r"^(`{3,}|~{3,})")
_ANY_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HASH_HEADING = re.compile(r"^(#{1,6})(.*)$")
_HEADING_ATTRS = re.compile(r"\s*\{:?[^}\n]*\}\s*$")
_AUTHORING_PREFIX = re.compile(r"^\s*<!--\s*(purpose|keywords|explains)\s*:", re.IGNORECASE)
_AUTHORING = re.compile(r"^<!-- (purpose|keywords|explains): (\S(?:.*\S)?) -->$")
_EXPLAINS_CODE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_RULE_ROW = re.compile(r"`(rule:[A-Za-z0-9_.\-]+)`\{ #[A-Za-z0-9_.:\-]+ \}")
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")

#: A character of each escaping class: a quote and a backslash, an ASCII control character, and
#: characters of two, three and four UTF-8 bytes. The worst request is made of whichever escapes widest.
_ESCAPE_CANDIDATES = ('"', "\\", "\x01", "é", "€", "\U0001f600")


class GuideCorpusError(Exception):
    """The guide cannot be turned into a corpus; the message says which page and why."""


# ---------------------------------------------------------------------------
# The Markdown configuration
# ---------------------------------------------------------------------------


def find_mkdocs_config(guide_dir: Path) -> Path:
    """The ``mkdocs.yml`` of the nearest directory at or above *guide_dir*."""
    for directory in (guide_dir.resolve(), *guide_dir.resolve().parents):
        candidate = directory / "mkdocs.yml"
        if candidate.is_file():
            return candidate
    raise GuideCorpusError(f"no mkdocs.yml at or above {guide_dir}; name one with --mkdocs-config")


def effective_markdown_config(config_file: Path) -> tuple[list[Any], dict[str, Any]]:
    """MkDocs' effective extension list and its configurations, as the site build uses them."""
    # `markdown` and MkDocs' configuration schema are imported explicitly: `load_config` imports
    # them lazily, so a missing `markdown` would otherwise escape as a raw ModuleNotFoundError
    # from inside MkDocs.
    try:
        # Python-Markdown ships no type information.
        import markdown  # type: ignore[import-untyped]  # noqa: F401, PLC0415 -- optional: markdown/mkdocs, dev group only, a missing one refused as GuideCorpusError
        import mkdocs.config.defaults  # noqa: F401, PLC0415 -- optional: markdown/mkdocs, dev group only, a missing one refused as GuideCorpusError
        from mkdocs.config import load_config  # noqa: PLC0415 -- optional: markdown/mkdocs, dev group only, a missing one refused as GuideCorpusError
        from mkdocs.exceptions import MkDocsException  # noqa: PLC0415 -- optional: markdown/mkdocs, dev group only, a missing one refused as GuideCorpusError
    except ImportError as exc:
        raise GuideCorpusError(MARKDOWN_ABSENT) from exc

    try:
        config = load_config(str(config_file))
    except ImportError as exc:
        raise GuideCorpusError(MARKDOWN_ABSENT) from exc
    except MkDocsException as exc:
        raise GuideCorpusError(f"{config_file}: MkDocs cannot read this configuration: {exc}") from exc
    return list(config["markdown_extensions"]), dict(config["mdx_configs"])


def _markdown(extensions: list[Any], configs: dict[str, Any]) -> Any:
    try:
        import markdown  # noqa: PLC0415 -- optional: markdown/mkdocs, dev group only, a missing one refused as GuideCorpusError
    except ImportError as exc:
        raise GuideCorpusError(MARKDOWN_ABSENT) from exc
    return markdown.Markdown(extensions=extensions, extension_configs=configs)


def _flatten_tokens(tokens: Sequence[Mapping[str, Any]]) -> Iterator[tuple[int, str]]:
    for token in tokens:
        yield int(token["level"]), str(token["id"])
        yield from _flatten_tokens(token["children"])


# ---------------------------------------------------------------------------
# One page, read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Heading:
    line: int
    level: int
    title: str
    anchor: str


@dataclass
class _Block:
    start: int
    end: int  # exclusive
    region: int  # the index of the cut heading the block belongs to
    text: str
    heading: bool = False
    authoring: tuple[str, str] | None = None


@dataclass
class _Page:
    page: str
    source: str  # the page's path relative to the guide, POSIX-spelled
    title: str
    keywords: list[str]
    cuts: list[_Heading]
    blocks: list[_Block]
    purposes: dict[int, str] = field(default_factory=dict)  # cut index -> purpose
    explains: dict[int, list[str]] = field(default_factory=dict)  # cut index -> codes


def page_name(relative: str) -> str:
    """The corpus page id of a guide page's source path: the path without ``.md`` and a trailing ``/index``."""
    stem = relative[: -len(".md")]
    if stem == "index":
        return "index"
    if stem.endswith("/index"):
        return stem[: -len("/index")]
    return stem


def _col0_fenced(lines: Sequence[str]) -> list[bool]:
    """The lines inside a fenced block as python-markdown's ``fenced_code`` reads one (column 0)."""
    fenced = [False] * len(lines)
    index = 0
    while index < len(lines):
        match = _COL0_FENCE.match(lines[index])
        if match is None:
            index += 1
            continue
        fence = match.group(1)
        close = next((j for j in range(index + 1, len(lines)) if lines[j].rstrip(" ") == fence), None)
        if close is None:
            index += 1
            continue
        for j in range(index, close + 1):
            fenced[j] = True
        index = close + 1
    return fenced


def _any_fenced(lines: Sequence[str]) -> list[bool]:
    """The lines inside a fenced block at any indentation -- where no part may be cut."""
    fenced = [False] * len(lines)
    fence: str | None = None
    for index, line in enumerate(lines):
        if fence is None:
            match = _ANY_FENCE.match(line)
            if match is not None:
                fence = match.group(1)
                fenced[index] = True
        else:
            fenced[index] = True
            if line.strip() == fence:
                fence = None
    return fenced


def _read_page(relative: str, text: str, md: Any) -> _Page:
    lines = text.split("\n")
    col0 = _col0_fenced(lines)
    anyf = _any_fenced(lines)

    scanned: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        if col0[index]:
            continue
        match = _HASH_HEADING.match(line)
        if match is None:
            continue
        title = match.group(2).strip().rstrip("#").strip()
        title = _HEADING_ATTRS.sub("", title).strip()
        scanned.append((index, len(match.group(1)), title))

    md.reset()
    md.convert(text)
    tokens = list(_flatten_tokens(md.toc_tokens))
    if [level for level, _ in tokens] != [level for _, level, _ in scanned]:
        raise GuideCorpusError(
            f"{relative}: the headings Markdown reads ({len(tokens)}) are not the headings the cut finds "
            f"({len(scanned)}); a heading the cut cannot see (setext, indented, inside HTML) is not allowed"
        )
    h1 = [entry for entry in scanned if entry[1] == 1]
    if len(h1) != 1:
        raise GuideCorpusError(f"{relative}: a page has exactly one level-one heading, this one has {len(h1)}")
    if scanned[0][1] != 1:
        raise GuideCorpusError(f"{relative}: the level-one heading must come before every other heading")

    cuts = [
        _Heading(line=index, level=level, title=title, anchor=anchor)
        for (index, level, title), (_, anchor) in zip(scanned, tokens, strict=True)
        if level <= 3
    ]
    cut_lines = {cut.line: number for number, cut in enumerate(cuts)}
    authoring_lines = {index for index, line in enumerate(lines) if not anyf[index] and _AUTHORING_PREFIX.match(line)}

    blocks: list[_Block] = []
    current: list[int] = []
    region = 0

    def flush() -> None:
        while current and not lines[current[-1]].strip():
            current.pop()
        if current:
            blocks.append(
                _Block(
                    start=current[0],
                    end=current[-1] + 1,
                    region=region,
                    text="\n".join(lines[current[0] : current[-1] + 1]),
                )
            )
        current.clear()

    for index, line in enumerate(lines):
        if index in cut_lines:
            flush()
            region = cut_lines[index]
            blocks.append(_Block(start=index, end=index + 1, region=region, text=line, heading=True))
            continue
        if not line.strip() and not anyf[index]:
            following = next((j for j in range(index + 1, len(lines)) if lines[j].strip()), None)
            continues = (
                current
                and following is not None
                and following not in cut_lines
                and following not in authoring_lines
                and lines[following][:1] in (" ", "\t")
                and not any(j in authoring_lines for j in current)
            )
            if continues:
                current.append(index)
            else:
                flush()
            continue
        current.append(index)
    flush()

    page = _Page(page=page_name(relative), source=relative, title=cuts[0].title, keywords=[], cuts=cuts, blocks=blocks)
    _read_authoring(page, lines, authoring_lines)
    return page


def _read_authoring(page: _Page, lines: Sequence[str], authoring_lines: set[int]) -> None:
    """Read the authoring comments, check where each stands, and mark their blocks."""
    previous: _Block | None = None
    for block in page.blocks:
        inside = [index for index in range(block.start, block.end) if index in authoring_lines]
        if not inside:
            previous = block
            continue
        where = f"{page.source}:{inside[0] + 1}"
        match = _AUTHORING.match(lines[inside[0]])
        if match is None:
            raise GuideCorpusError(
                f"{where}: an authoring comment is one line, `<!-- purpose: ... -->`, `<!-- keywords: ... -->` "
                "or `<!-- explains: ... -->`, lowercase, with a value"
            )
        if block.end - block.start != 1:
            raise GuideCorpusError(f"{where}: an authoring comment is a block of its own, with a blank line around it")
        kind, value = match.group(1), match.group(2)
        if previous is None or not previous.heading:
            raise GuideCorpusError(f"{where}: the {kind} comment must stand directly under a heading")
        owner = previous.region
        level = page.cuts[owner].level
        block.authoring = (kind, value)
        if kind == "keywords":
            if level != 1:
                raise GuideCorpusError(f"{where}: keywords stand under the page's level-one heading")
            if page.keywords:
                raise GuideCorpusError(f"{where}: a page has one keywords comment")
            words = [word.strip() for word in value.split(",")]
            if any(not word for word in words):
                raise GuideCorpusError(f"{where}: an empty keyword")
            page.keywords = words
        elif kind == "purpose":
            if owner in page.purposes:
                raise GuideCorpusError(f"{where}: a heading has one purpose comment")
            if len(value) > PURPOSE_CAP_CHARACTERS:
                raise GuideCorpusError(
                    f"{where}: a purpose is at most {PURPOSE_CAP_CHARACTERS} characters, this one is {len(value)}"
                )
            page.purposes[owner] = value
        else:
            if level == 1:
                raise GuideCorpusError(f"{where}: explains stands under a `##` or `###` heading, not the page's")
            if owner in page.explains:
                raise GuideCorpusError(f"{where}: a heading has one explains comment")
            codes = [code.strip() for code in value.split(",")]
            bad = [code for code in codes if not _EXPLAINS_CODE.match(code)]
            if bad:
                raise GuideCorpusError(f"{where}: not a code: {bad}")
            page.explains[owner] = codes


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


def _split_units(text: str) -> tuple[list[str], Callable[[Sequence[str]], str]] | None:
    """The units an oversized block splits into, and how a run of units becomes a block again."""
    lines = text.split("\n")
    if len(lines) >= 4 and all(line.lstrip().startswith("|") for line in lines) and _TABLE_DELIMITER.match(lines[1]):
        header = lines[:2]
        return lines[2:], lambda units: "\n".join([*header, *units])
    opener = _ANY_FENCE.match(lines[0])
    if opener is not None and len(lines) >= 4 and lines[-1].strip() == opener.group(1):
        first, last = lines[0], lines[-1]
        return lines[1:-1], lambda units: "\n".join([first, *units, last])
    if any(line.lstrip().startswith(("|", "```", "~~~")) for line in lines):
        return None
    if _LIST_ITEM.match(lines[0]) and sum(1 for line in lines if _LIST_ITEM.match(line)) >= 2:
        items: list[str] = []
        for line in lines:
            if _LIST_ITEM.match(line) or not items:
                items.append(line)
            else:
                items[-1] += "\n" + line
        return items, lambda units: "\n".join(units)
    if _SENTENCE_END.search(text) is None:
        return None
    sentences: list[str] = []
    begin = 0
    for match in _SENTENCE_END.finditer(text):
        sentences.append(text[begin : match.start()])
        begin = match.end()
    sentences.append(text[begin:])
    return sentences, lambda units: " ".join(units)


def _pack(blocks: Sequence[str], fits: Callable[[Sequence[str]], bool], where: str) -> list[list[tuple[int, str]]]:
    """Cut *blocks* into parts, greedily, each part's composed reply measured by *fits*.

    Each piece is tagged with the index of the block it is (or whose units it holds); a part's
    text is its pieces joined by a blank line.
    """
    parts: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []

    def fits_with(piece: str) -> bool:
        return fits([*(text for _, text in current), piece])

    for number, block in enumerate(blocks):
        if fits_with(block):
            current.append((number, block))
            continue
        if current and fits([block]):
            parts.append(current)
            current = [(number, block)]
            continue
        split = _split_units(block)
        if split is None:
            raise GuideCorpusError(f"{where}: a block cannot fit one reply and cannot be split")
        units, wrap = split
        index = 0
        while index < len(units):
            end = index
            while end < len(units) and fits_with(wrap(units[index : end + 1])):
                end += 1
            if end == index:
                if current:
                    parts.append(current)
                    current = []
                    continue
                raise GuideCorpusError(f"{where}: a single row, line or sentence cannot fit one reply")
            current.append((number, wrap(units[index:end])))
            index = end
            if index < len(units):
                parts.append(current)
                current = []
    if current or not parts:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def _subtree_end(page: _Page, cut: int) -> int:
    level = page.cuts[cut].level
    for later in range(cut + 1, len(page.cuts)):
        if page.cuts[later].level <= level:
            return later
    return len(page.cuts)


def _section_records(page: _Page) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, cut in enumerate(page.cuts):
        if number == 0:
            section_id = page.page
            breadcrumb = [page.title]
        else:
            section_id = f"{page.page}#{cut.anchor}"
            parent = next(
                (page.cuts[j].title for j in range(number - 1, 0, -1) if page.cuts[j].level < cut.level), None
            )
            breadcrumb = [page.title, *([parent] if parent is not None else []), cut.title]
        purpose = page.purposes.get(number, "")
        end = _subtree_end(page, number)
        # A page's subtree is the whole page, what stands above its H1 included; a
        # section's starts at its heading and ends before the next heading of its level.
        held = [
            block for block in page.blocks if block.authoring is None and (number == 0 or number <= block.region < end)
        ]
        record = {"id": section_id, "page": page.page, "breadcrumb": breadcrumb, "level": cut.level, "purpose": purpose}

        def fits(candidate: Sequence[str], record: dict[str, Any] = record) -> bool:
            reply = guide_search.compose_section_reply(
                record,
                page.title,
                PACKING_PART_PLACEHOLDER,
                PACKING_PART_PLACEHOLDER,
                "\n\n".join(candidate),
                {"section": record["id"], "part": PACKING_PART_PLACEHOLDER},
            )
            return measure(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES

        packed = _pack([block.text for block in held], fits, f"{page.source}, `{section_id}`")
        record["parts"] = ["\n\n".join(text for _, text in part) for part in packed]
        record["own"] = _own_body(held, number, packed)
        records.append(record)
    return records


def _own_body(held: Sequence[_Block], number: int, packed: Sequence[Sequence[tuple[int, str]]]) -> list[str]:
    """The section's own body, cut where its parts are cut: one text per part it reaches.

    Its own blocks are those of its own region (cut *number*); its heading's block is not its body.
    A part the own region reaches with its heading alone has an empty text.
    """
    own = {index for index, block in enumerate(held) if block.region == number}
    heading = next(index for index, block in enumerate(held) if block.region == number and block.heading)
    texts: list[str] = []
    for part in packed:
        if not any(index in own for index, _ in part):
            break
        texts.append("\n\n".join(text for index, text in part if index in own and index != heading))
    return texts


def _explains(pages: Sequence[_Page], records: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    explains: dict[str, dict[str, Any]] = {}

    def enter(key: str, section_id: str, part: int, where: str) -> None:
        if key in explains:
            raise GuideCorpusError(f"{where}: `{key}` is explained twice ({explains[key]['section']} and {section_id})")
        explains[key] = {"section": section_id, "part": part}

    for page in pages:
        for number, codes in page.explains.items():
            section_id = f"{page.page}#{page.cuts[number].anchor}"
            for code in codes:
                enter(code, section_id, 1, page.source)
        for block in page.blocks:
            if block.authoring is not None:
                continue
            for line in block.text.split("\n"):
                for match in _RULE_ROW.finditer(line):
                    cut = page.cuts[block.region]
                    section_id = page.page if block.region == 0 else f"{page.page}#{cut.anchor}"
                    parts = records[section_id]["parts"]
                    holding = next((n for n, text in enumerate(parts, 1) if line in text.split("\n")), None)
                    if holding is None:
                        raise GuideCorpusError(f"{page.source}: the row of `{match.group(1)}` is in no part")
                    enter(match.group(1), section_id, holding, page.source)
    return dict(sorted(explains.items()))


def is_release_notes(relative: str) -> bool:
    """Whether the guide's file *relative* (POSIX, from the guide's root) is of the *Release notes* section."""
    return relative == f"{RELEASE_NOTES_SUBTREE}.md" or relative.startswith(f"{RELEASE_NOTES_SUBTREE}/")


def build_corpus(guide_dir: Path, *, mkdocs_config: Path | None = None) -> dict[str, Any]:
    """The corpus of the guide under *guide_dir*, built and checked in memory; nothing is written.

    The *Release notes* section (:func:`is_release_notes`) is not read.

    Raises :class:`GuideCorpusError` for every refusal: a duplicate id, a reply that cannot fit, a
    malformed authoring comment, a page with no or two H1s, and ``markdown`` absent. The check of
    every reply and the build-host scan are :func:`produce`'s.
    """
    config_file = mkdocs_config if mkdocs_config is not None else find_mkdocs_config(guide_dir)
    extensions, configs = effective_markdown_config(config_file)
    md = _markdown(extensions, configs)
    sources = sorted(
        (
            path
            for path in guide_dir.rglob("*.md")
            if path.is_file() and not is_release_notes(path.relative_to(guide_dir).as_posix())
        ),
        key=lambda path: path.relative_to(guide_dir).as_posix(),
    )
    if not sources:
        raise GuideCorpusError(f"no guide page under {guide_dir}")
    pages = [
        _read_page(path.relative_to(guide_dir).as_posix(), path.read_text(encoding="utf-8"), md) for path in sources
    ]
    pages.sort(key=lambda page: page.page)

    sections: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for page in pages:
        for record in _section_records(page):
            if record["id"] in seen:
                raise GuideCorpusError(f"{page.source}: the id `{record['id']}` is also {seen[record['id']]}'s")
            seen[record["id"]] = page.source
            sections.append(record)
    by_id = {record["id"]: record for record in sections}
    return {
        "corpus": CORPUS_NAME,
        "schema": CORPUS_SCHEMA,
        "swcraft_version": __version__,
        "pages": [{"page": page.page, "title": page.title, "keywords": page.keywords} for page in pages],
        "sections": sections,
        "explains": _explains(pages, by_id),
    }


# ---------------------------------------------------------------------------
# The checks: every reply measured, and no location of the build host
# ---------------------------------------------------------------------------


def _widest(measure_one: Callable[[str], int]) -> str:
    """The :data:`~swcraft_tools.guide_search.REQUEST_CAP_CHARACTERS`-character request that escapes widest."""
    return max(
        (character * REQUEST_CAP_CHARACTERS for character in _ESCAPE_CANDIDATES),
        key=lambda request: (measure_one(request), request),
    )


def _every_reply(index: GuideIndex) -> Iterator[tuple[str, Mapping[str, Any] | str]]:
    for section_id, section in index.sections.items():
        for part in range(1, len(section["parts"]) + 1):
            yield f"section `{section_id}` part {part}", guide_search.compose_section(index, section_id, part)
    for page in (None, *index.pages):
        for part in range(1, len(guide_search.toc_parts(index, page)) + 1):
            yield f"toc of `{page or 'the guide'}` part {part}", guide_search.compose_toc(index, page, part)

    widest_query = _widest(lambda query: measure({"query": query}))
    largest = sorted(
        index.sections,
        key=lambda section_id: (
            -measure(guide_search.hit_entry(index, section_id, len(index.sections[section_id]["parts"]))),
            section_id,
        ),
    )[:SEARCH_HIT_LIMIT]
    hits = [(section_id, len(index.sections[section_id]["parts"])) for section_id in largest]
    yield "the worst search", guide_search.compose_search(index, widest_query, hits, True)
    yield "a search with no hit", guide_search.compose_search(index, widest_query, [], False)

    widest_echo = _widest(lambda echo: escaped_bytes(f"`{echo}`"))
    longest_ids = sorted(index.sections, key=lambda section_id: (-len(section_id.encode("utf-8")), section_id))
    most_parts = max([len(section["parts"]) for section in index.sections.values()] + [1])
    corpus_version = str(index.corpus["swcraft_version"])
    yield "guide-missing", guide_search.missing_refusal()
    yield "guide-version", guide_search.version_refusal(corpus_version, __version__)
    yield "guide-unreadable", guide_search.unreadable_refusal()
    yield "guide-mode", guide_search.mode_refusal()
    yield "guide-unknown-section", guide_search.unknown_section_refusal(widest_echo, longest_ids[:3])
    yield (
        "guide-unknown-part",
        guide_search.unknown_part_refusal(longest_ids[0], max(most_parts, PACKING_PART_PLACEHOLDER)),
    )
    yield "guide-request-too-long", guide_search.request_too_long_refusal()
    yield "guide-over-budget", guide_search.over_budget_refusal(longest_ids[0])


def check_every_reply(corpus: Mapping[str, Any]) -> list[tuple[str, int]]:
    """Every reply the corpus can produce that measures over the budget, with its wire bytes.

    Every part of every id, every toc part with and without a page, the worst search (the five
    largest hits under the widest-escaping 200-character query) and every refusal at its longest
    echo. An empty list is the corpus's pass.
    """
    index = GuideIndex.of(corpus)
    over: list[tuple[str, int]] = []
    for name, reply in _every_reply(index):
        size = guide_search.reply_wire_bytes(reply)
        if size > GUIDE_REPLY_BUDGET_WIRE_BYTES:
            over.append((name, size))
    return over


def _strings(corpus: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    for page in corpus["pages"]:
        yield page["page"], page["title"]
        for keyword in page["keywords"]:
            yield page["page"], keyword
    for section in corpus["sections"]:
        for value in (*section["breadcrumb"], section["purpose"], *section["parts"]):
            yield section["id"], value


def host_locations(*directories: Path) -> tuple[str, ...]:
    """The concrete spellings of *directories* and of the home and temporary directories.

    Each as given and resolved, native and POSIX-spelled; a root of under two parts is
    skipped, as the reply gate's ``filesystem_roots`` skips it.
    """
    spellings: set[str] = set()
    for candidate in (*directories, Path.home(), Path(tempfile.gettempdir())):
        for form in (candidate, candidate.resolve()):
            if len(form.parts) < 2:
                continue
            spellings.update({str(form), form.as_posix()})
    return tuple(sorted(spellings, key=lambda spelling: (-len(spelling), spelling)))


def root_spellings(text: str) -> list[str]:
    """Each token of *text* the reply gate reads as a POSIX path and that is only separators.

    What is left once the trailing ``.`` and ``:`` are stripped names a filesystem root on some
    host: ``//`` and ``///`` on every POSIX host, ``/\\`` where ``\\`` separates too.
    """
    return [
        token
        for token in POSIX_SPELLED.findall(text)
        if (candidate := token.rstrip(".:")) and not candidate.strip("/\\")
    ]


def scan_for_locations(corpus: Mapping[str, Any], locations: Sequence[str]) -> list[tuple[str, str]]:
    """Each ``(id, what)`` where the corpus names one of *locations*, a ``~/`` or drive-letter spelling, or a root."""
    found: list[tuple[str, str]] = []
    for where, text in _strings(corpus):
        found += [(where, location) for location in locations if location in text]
        found += [(where, match) for match in HOME_SPELLED.findall(text)]
        found += [(where, match) for match in DRIVE_LETTER_PATH.findall(text)]
        found += [(where, match) for match in root_spellings(text)]
    return found


def write_corpus(corpus: Mapping[str, Any], out: Path) -> None:
    """Write *corpus* to *out*: UTF-8, ``\\n``, keys sorted, one line -- the same bytes on every host."""
    text = json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")


def produce(guide_dir: Path, out: Path, *, mkdocs_config: Path | None = None) -> dict[str, Any]:
    """Build, check and write the corpus; nothing is written unless every check passed."""
    config_file = mkdocs_config if mkdocs_config is not None else find_mkdocs_config(guide_dir)
    corpus = build_corpus(guide_dir, mkdocs_config=config_file)
    over = check_every_reply(corpus)
    if over:
        listed = "; ".join(f"{name}: {size} wire bytes" for name, size in over[:10])
        raise GuideCorpusError(
            f"{len(over)} reply/replies over the {GUIDE_REPLY_BUDGET_WIRE_BYTES}-byte budget: {listed}"
        )
    unnamed = identifier_violations(corpus)
    if unnamed:
        listed = "; ".join(f"`{where}` names {what!r} in a code block only" for where, what in unnamed[:10])
        raise GuideCorpusError(f"{len(unnamed)} identifier(s) an entry section's prose does not name: {listed}")
    found = scan_for_locations(corpus, host_locations(guide_dir, config_file.parent, out.parent))
    if found:
        listed = "; ".join(f"`{where}` names {what!r}" for where, what in found[:10])
        raise GuideCorpusError(f"the guide names a location: {listed}")
    if not out.parent.is_dir():
        raise GuideCorpusError(f"the corpus's directory does not exist: {out.parent}")
    write_corpus(corpus, out)
    return corpus


# ---------------------------------------------------------------------------
# The agent profiles' guide index
# ---------------------------------------------------------------------------

#: The generated region of the agent profiles that names the How-to pages.
INDEX_BEGIN_MARKER = "<!-- BEGIN GENERATED: guide index -->"
INDEX_END_MARKER = "<!-- END GENERATED: guide index -->"

#: The guide's How-to section: its index page and the scenario pages below it.
HOW_TO_PAGE = "how-to"

#: The refusal code the index's last line gives as its example; a section must explain it.
INDEX_REFUSAL_EXAMPLE = "stale-baseline"

#: A How-to page's recipe block opens with this comment, a line of its own:
#: ``<!-- recipe: <title> -->``. The index gives each block a line of its own under its page's.
INDEX_RECIPE_COMMENT = re.compile(r"^<!-- recipe: (\S(?:.*\S)?) -->$")

#: The region's lead, before the page lines: how an agent answers a question about SWCraft from
#: the guide. Generated, so every profile that carries the region says it alike.
INDEX_ANSWER_RULE = (
    "**Answering from the guide.** A question about SWCraft itself — what something means, how to do",
    "something, what a command or setting does — is answered from the guide: call `guide` before",
    "answering, never from memory. When a person asks how to do something, answer in words: the",
    "steps, what they will see (the change in the review tab, and **Approve**), and the refusals",
    "they are likely to meet; offer to make the change yourself. A How-to page's change-set JSON is",
    "for your own call when you make the change: do not paste it into the answer unless the person",
    "asks for it. When you answer from a section, name the commands, flags, variables and files it",
    "names, exactly as written, in backticks. End every answer drawn from the guide with the link to",
    "the section you read: the `link` from the `guide` reply as `[title](link)`, never the `guide(…)`",
    "call.",
)


def _recipe_titles(text: str) -> list[str]:
    """The titles of the recipe comments in *text*, in order; a comment inside a fenced block is not one."""
    lines = text.split("\n")
    matches = (
        INDEX_RECIPE_COMMENT.match(line) for line, fenced in zip(lines, _any_fenced(lines), strict=True) if not fenced
    )
    return [match.group(1) for match in matches if match is not None]


def recipe_sections(corpus: Mapping[str, Any], page: str) -> list[tuple[str, str]]:
    """Each recipe block of *page*, in page order: its title and the id of the section it stands in.

    The section is the deepest one whose text holds the block's comment. A title standing twice on
    the page is refused.
    """
    records = [section for section in corpus["sections"] if section["page"] == page]
    whole = next(section for section in records if section["id"] == page)
    titles = _recipe_titles("\n\n".join(whole["parts"]))
    repeated = sorted({title for title in titles if titles.count(title) > 1})
    if repeated:
        raise GuideCorpusError(f"`{page}`: a recipe title stands more than once on the page: {repeated}")
    held: dict[str, tuple[int, str]] = {}
    for section in records:
        for title in set(_recipe_titles("\n\n".join(section["parts"]))):
            if title not in held or section["level"] > held[title][0]:
                held[title] = (section["level"], section["id"])
    return [(title, held[title][1]) for title in titles]


def render_profile_index(corpus: Mapping[str, Any]) -> str:
    """The profiles' guide index, markers included: the answer rule, the How-to pages, the refusal map.

    Each page line is the page's title, the ``guide`` call that reads it and its purpose, in id
    order; under it, one indented line per recipe block of the page. The last line says that a
    refusal code is a ``guide(search=...)`` query. What it does not reach: a section below a page
    that holds no recipe block, the How-to index page itself, a recipe block or any other page
    outside the How-to section, and whether a block's title says what its section answers.
    """
    titles = {page["page"]: page["title"] for page in corpus["pages"]}
    purposes = {section["id"]: section["purpose"] for section in corpus["sections"]}
    pages = sorted(page for page in titles if page.startswith(f"{HOW_TO_PAGE}/"))
    if not pages:
        raise GuideCorpusError(f"the guide has no page under `{HOW_TO_PAGE}/` to index")
    if INDEX_REFUSAL_EXAMPLE not in corpus["explains"]:
        raise GuideCorpusError(f"no section explains `{INDEX_REFUSAL_EXAMPLE}`, the index's example")
    lines = [INDEX_BEGIN_MARKER, *INDEX_ANSWER_RULE, ""]
    for page in pages:
        purpose = f" — {purposes[page]}" if purposes.get(page) else ""
        lines.append(f'- {titles[page]} → `guide(section="{page}")`{purpose}')
        for title, section_id in recipe_sections(corpus, page):
            lines.append(f'  - {title} → `guide(section="{section_id}")`')
    lines.append(
        '- A refusal code or a check\'s rule id → `guide(search="<the code>")`, e.g. '
        f'`guide(search="{INDEX_REFUSAL_EXAMPLE}")`: its first hit is the section that explains it'
    )
    lines.append(INDEX_END_MARKER)
    return "\n".join(lines) + "\n"


def rewrite_profile_index(text: str, region: str) -> str:
    """*text* with its guide index region replaced by *region* (markers included).

    Raises :class:`GuideCorpusError` when *text* does not carry both markers once each, in order.
    """
    begin = text.find(INDEX_BEGIN_MARKER)
    end = text.find(INDEX_END_MARKER)
    if begin < 0 or end < begin or text.count(INDEX_BEGIN_MARKER) != 1 or text.count(INDEX_END_MARKER) != 1:
        raise GuideCorpusError("the profile does not carry the guide index markers once each, in order")
    tail = end + len(INDEX_END_MARKER)
    if text[tail : tail + 1] == "\n":
        tail += 1
    return text[:begin] + region + text[tail:]


def write_profile_indexes(guide_dir: Path, profiles: Sequence[Path], *, mkdocs_config: Path | None = None) -> None:
    """Rewrite the guide index region of each of *profiles* from the guide under *guide_dir*.

    Every profile is read and checked before any is written, so a profile without the
    markers leaves all of them as they were. A profile already current is not written.
    """
    region = render_profile_index(build_corpus(guide_dir, mkdocs_config=mkdocs_config))
    updates: list[tuple[Path, str, str]] = []
    for path in profiles:
        text = path.read_text(encoding="utf-8")
        try:
            updates.append((path, text, rewrite_profile_index(text, region)))
        except GuideCorpusError as exc:
            raise GuideCorpusError(f"{path}: {exc}") from exc
    for path, text, updated in updates:
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# The entry sections name their identifiers in prose
# ---------------------------------------------------------------------------

#: The sections a tool description of the MCP server points to (its ``guide(section="<id>")``
#: line); the producer does not read the server's source, so the set must be kept equal to it.
TOOL_POINTER_SECTIONS = frozenset(
    {
        "cli#generating",
        "cli#the-two-commands-that-grade",
        "how-to/ask-about-components",
        "how-to/connect-components",
        "how-to/connect-components#resolve-the-names",
        "how-to/import-a-component",
    }
)

#: Identifiers an entry section may hold in a code block without naming them in its prose,
#: each with its reason. Held as data so that admitting one is a visible diff.
IDENTIFIER_ALLOWANCES: dict[str, str] = {
    "--project": (
        "every command line takes the manifest by `--project`; the command line page's *One shape* "
        "says it once for every command, and a sentence per command line would say nothing new"
    ),
    "--short": "git's own flag, in `$(git rev-parse --short HEAD)`, not one of SWCraft's",
}

#: The last segments that make a dotted token a file name rather than a key.
FILE_EXTENSIONS = frozenset(
    "arxml bat cfg cmd csv exe html ini js json lock log md png ps1 puml py rst sh svg toml ts txt vsix xml xsd "
    "yaml yml zip".split()
)

_IDENT_COMMAND = re.compile(r"(?<![\w./-])swcraft[ \t]+([a-z][a-z0-9-]*)")
_IDENT_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_UPPER_SNAKE = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
_IDENT_VARIABLE_EXPANDED = re.compile(rf"(?:\$\{{?|\$env:|%)({_UPPER_SNAKE})(?![\w])")
_IDENT_VARIABLE_SET = re.compile(rf'(?<![\w])"?({_UPPER_SNAKE})"?[ \t]*[:=]')
_IDENT_DOTTED = re.compile(r"(?<![\w./@:$-])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?![\w./@-])")
_IDENT_PATH = re.compile(r"(?<![\w./:<>~$%\\-])(?:[\w.-]+/)+[\w.-]*\.[A-Za-z0-9]+(?![\w/])")
_MARKDOWN_LINK = re.compile(r"\]\(\s*([^)\s]+)")


def code_identifiers(code: str) -> set[str]:
    """The identifiers *code* holds, by the narrow patterns the module's docstring names."""
    found = {f"swcraft {name}" for name in _IDENT_COMMAND.findall(code)}
    found.update(_IDENT_FLAG.findall(code))
    found.update(_IDENT_VARIABLE_EXPANDED.findall(code))
    found.update(_IDENT_VARIABLE_SET.findall(code))
    found.update(token for token in _IDENT_DOTTED.findall(code) if token.rsplit(".", 1)[1] not in FILE_EXTENSIONS)
    found.update(token for token in _IDENT_PATH.findall(code) if not token.startswith("../"))
    return found


def _split_code(text: str) -> tuple[str, str]:
    """*text*'s fenced lines and its other lines, each joined: the code and the prose."""
    lines = text.split("\n")
    fenced = _any_fenced(lines)
    code = "\n".join(line for line, inside in zip(lines, fenced, strict=True) if inside)
    prose = "\n".join(line for line, inside in zip(lines, fenced, strict=True) if not inside)
    return code, prose


def _link_target(corpus_pages: set[str], page: str, target: str) -> str | None:
    """The section id a relative Markdown link on *page* points to, or ``None`` for any other link.

    A page is a directory's ``index.md`` -- its links resolve from that directory -- when a
    page stands below it or it is :data:`HOW_TO_PAGE`, whose file is ``how-to/index.md``.
    """
    if "://" in target or target.startswith(("mailto:", "/")):
        return None
    path, _, fragment = target.partition("#")
    if path:
        is_index = page == HOW_TO_PAGE or any(other.startswith(f"{page}/") for other in corpus_pages)
        directory = page if is_index else posixpath.dirname(page)
        resolved = posixpath.normpath(posixpath.join(directory, path))
        if resolved.endswith(".md"):
            resolved = page_name(resolved)
        resolved = resolved.rstrip("/")
        if resolved.endswith("/index"):
            resolved = resolved[: -len("/index")]
        target_page = resolved
    else:
        target_page = page
    return f"{target_page}#{fragment}" if fragment else target_page


def entry_sections(corpus: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each entry section of *corpus* -- an id a pointer sends an agent to -- with what points to it.

    The pointers: ``tool`` (:data:`TOOL_POINTER_SECTIONS`), ``index`` (a How-to page and each of
    its recipe sections), ``explains`` (the section an ``explains`` key names) and ``how-to link`` (a
    relative link on a How-to page, outside a fenced block). An id the corpus does not hold is left
    out.
    """
    sections = {section["id"]: section for section in corpus["sections"]}
    pages = {page["page"] for page in corpus["pages"]}
    entries: dict[str, set[str]] = {}

    def add(section_id: str | None, why: str) -> None:
        if section_id is not None and section_id in sections:
            entries.setdefault(section_id, set()).add(why)

    for section_id in TOOL_POINTER_SECTIONS:
        add(section_id, "tool")
    for target in corpus["explains"].values():
        add(target["section"], "explains")
    for page in sorted(pages):
        if page != HOW_TO_PAGE and not page.startswith(f"{HOW_TO_PAGE}/"):
            continue
        if page != HOW_TO_PAGE:
            add(page, "index")
            for _, section_id in recipe_sections(corpus, page):
                add(section_id, "index")
        _, prose = _split_code("\n\n".join(sections[page]["parts"]))
        for target in _MARKDOWN_LINK.findall(prose):
            add(_link_target(pages, page, target), "how-to link")
    return {section_id: sorted(entries[section_id]) for section_id in sorted(entries)}


def identifier_violations(corpus: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each ``(entry id, identifier)`` the entry's code blocks hold and its prose does not name.

    The prose names an identifier when it holds the same spelling between non-word characters.
    An empty list is the corpus's pass.
    """
    sections = {section["id"]: section for section in corpus["sections"]}
    found: list[tuple[str, str]] = []
    for section_id in entry_sections(corpus):
        code, prose = _split_code("\n\n".join(sections[section_id]["parts"]))
        for identifier in sorted(code_identifiers(code)):
            if identifier in IDENTIFIER_ALLOWANCES:
                continue
            if not re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])", prose):
                found.append((section_id, identifier))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m swcraft_tools.guide_corpus",
        description="Build the guide's corpus for the assistant's `guide` tool, or the agent profiles' index of it.",
    )
    parser.add_argument("--guide-dir", type=Path, required=True, help="the guide's Markdown, as the site build sees it")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", type=Path, help=f"the corpus to write, `{CORPUS_FILENAME}`")
    target.add_argument(
        "--profiles", type=Path, nargs="+", help="agent profiles whose guide index region is rewritten instead"
    )
    parser.add_argument("--mkdocs-config", type=Path, default=None, help="default: the nearest mkdocs.yml above")
    arguments = parser.parse_args(argv)
    try:
        if arguments.profiles:
            write_profile_indexes(arguments.guide_dir, arguments.profiles, mkdocs_config=arguments.mkdocs_config)
            return 0
        corpus = produce(arguments.guide_dir, arguments.out, mkdocs_config=arguments.mkdocs_config)
    except GuideCorpusError as exc:
        print(f"guide-corpus: {exc}", file=sys.stderr)
        return 1
    print(f"guide-corpus: {len(corpus['pages'])} pages, {len(corpus['sections'])} ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
