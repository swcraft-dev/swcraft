"""The ``guide`` tool's replies, composed from the guide's corpus by pure functions.

The MCP tool ``guide`` answers *how is X done in SWCraft* from one JSON corpus of the user guide
(:mod:`swcraft_tools.guide_corpus`, its producer, which composes and measures every reply at build
time, so an accepted corpus holds no reply over :data:`GUIDE_REPLY_BUDGET_WIRE_BYTES`). Three modes:
:func:`compose_search` (a few section ids with breadcrumbs, purposes and the ready ``read`` request),
:func:`compose_toc` (the pages, or one page's sections) and :func:`compose_section` (one part of an
id's subtree, with ``next``). A reply is measured in the server's own unit
(:func:`swcraft_tools.wire_unit.measure`, :func:`swcraft_tools.wire_unit.escaped_bytes`).

A section reply and every search hit carry ``link`` (:func:`section_link`), a ``vscode:`` URI of the
SWCraft extension that opens the guide at the section; it names no location and opens nothing in
another editor or without the extension. Every refusal is ``ERROR: <code>: <sentence>``, one closed
code of :data:`REFUSAL_CODES`, naming no location.

**The reader** (:func:`answer`, :func:`load_index`) reads the corpus of the **installed** site alone
(:func:`swcraft_tools.guide_site.package_site_dir`, then
:func:`swcraft_tools.guide_site.bundle_site_dir`), **never** ``SWCRAFT_GUIDE_SITE``, since the server
copies a project's ``.env`` into its environment. The corpus is checked (``corpus``, ``schema``, its
shape, ``swcraft_version`` against the running release) and kept once per process; a refusal is not
kept, so a later install is seen without a restart. ``GuideSiteMissing`` is never raised.

**The ranking** (:func:`rank`) is lexical: an exact lookup (an ``explains`` key, with or without
``rule:``, or a section id), then :data:`OVERVIEW_QUERIES` ranking :data:`OVERVIEW_SECTION` first,
then the ``ranker=`` of :data:`RANKERS`. The tool uses :data:`DEFAULT_RANKER`; the other rankers are
kept for measurement only.

It reads nothing but the installed corpus. A checkout whose guide was edited after its last build
serves the older corpus under the same version, and the ranking matches words, not meanings.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeGuard

from swcraft_tools import guide_site
from swcraft_tools._version import __version__
from swcraft_tools.bm25 import BM25_B, Corpus, FieldCorpus, bm25_scores, bm25f_scores
from swcraft_tools.wire_unit import escaped_bytes, measure

__all__ = [
    "ANCESTOR_WEIGHT",
    "AUXILIARY_STOP_WORDS",
    "BM25F_BOOSTS",
    "BM25F_FIELDS",
    "BODY_COUNT_CAP",
    "BODY_WEIGHT",
    "CORPUS_FILENAME",
    "CORPUS_NAME",
    "CORPUS_SCHEMA",
    "CORPUS_VERSION_CAP_CHARACTERS",
    "CORPUS_VERSION_PATTERN",
    "DEFAULT_RANKER",
    "GUIDE_REPLY_BUDGET_WIRE_BYTES",
    "HEADING_WEIGHT",
    "NEAREST_LIMIT",
    "NO_MATCH_SENTENCE",
    "OVERVIEW_QUERIES",
    "OVERVIEW_SECTION",
    "PACKING_PART_PLACEHOLDER",
    "PAGE_WEIGHT",
    "PURPOSE_CAP_CHARACTERS",
    "PURPOSE_WEIGHT",
    "RANKERS",
    "RANKER_BM25",
    "RANKER_BM25F",
    "RANKER_WEIGHTS",
    "RANKER_WEIGHTS_FIELDS",
    "RANKER_WEIGHTS_FIELDS_NORM",
    "RANKER_WEIGHTS_FIELDS_NORM_PARTS",
    "RANKER_WEIGHTS_FIELDS_PARTS",
    "RANKER_WEIGHTS_NORM",
    "RANKER_WEIGHTS_NORM_PARTS",
    "RANKER_WEIGHTS_PARTS",
    "REFUSAL_CODES",
    "REQUEST_CAP_CHARACTERS",
    "SEARCH_HIT_LIMIT",
    "SECTION_LINK_PREFIX",
    "STOP_WORDS",
    "WEIGHTS_VARIANTS",
    "GuideIndex",
    "GuideRefusal",
    "MeasurementIndex",
    "SearchIndex",
    "WeightsVariant",
    "answer",
    "bm25_ranking_scores",
    "compose_search",
    "compose_section",
    "compose_section_reply",
    "compose_toc",
    "hit_entry",
    "load_index",
    "missing_refusal",
    "mode_refusal",
    "nearest",
    "over_budget_refusal",
    "rank",
    "reply_wire_bytes",
    "request_too_long_refusal",
    "reset_cache",
    "score",
    "search",
    "section",
    "section_link",
    "toc",
    "toc_entries",
    "tokens",
    "toc_parts",
    "unknown_part_refusal",
    "unknown_section_refusal",
    "unreadable_refusal",
    "version_refusal",
]

#: The opening of every section's link (:func:`section_link`): the editor's product scheme, the
#: SWCraft VS Code extension's id (``<publisher>.<name>`` of ``vscode_extension/package.json``)
#: and the path and query its URI handler accepts.
SECTION_LINK_PREFIX = "vscode://swcraft-local.swcraft-vscode/guide?section="

#: The budget of every ``guide`` reply, in wire bytes: under VS Code's 8 192-byte spill (a tool
#: answer over it goes to a file and costs a second read). Every mode and refusal is held to it.
GUIDE_REPLY_BUDGET_WIRE_BYTES = 8_000

#: The longest purpose the producer accepts (an authored one) or writes (a composed
#: one, truncated); it bounds the search and toc replies.
PURPOSE_CAP_CHARACTERS = 100

#: The longest ``search`` query, ``section`` id or ``page`` name a request may
#: carry, in characters. A longer one is refused with a constant sentence that
#: echoes nothing (:func:`request_too_long_refusal`), so a reply's echo is bounded.
REQUEST_CAP_CHARACTERS = 200

#: The most hits one ``search`` reply carries.
SEARCH_HIT_LIMIT = 5

#: The corpus's file name in the site's root.
CORPUS_FILENAME = "guide-corpus.json"

#: The corpus document's ``corpus`` value and ``schema`` number.
CORPUS_NAME = "swcraft-guide"
CORPUS_SCHEMA = 1

#: The longest ``swcraft_version`` a corpus may carry, in characters.
CORPUS_VERSION_CAP_CHARACTERS = 64

#: A corpus's ``swcraft_version``: a release in PEP 440's normalized form, of at most
#: :data:`CORPUS_VERSION_CAP_CHARACTERS`. The ``guide-version`` refusal echoes it, so the alphabet
#: holds no path separator. What it does not reach: the permissive spellings PEP 440 also admits
#: (``v0.22``, ``0.22-rc1``), which no producer writes; such a corpus is refused as unreadable.
CORPUS_VERSION_PATTERN = re.compile(
    r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?"
)

#: The sentence a search reply with no hit carries.
NO_MATCH_SENTENCE = "nothing in the guide matches; `guide(toc=True)` lists its pages"

#: The closed refusal words, each composed by one function below.
REFUSAL_CODES = (
    "guide-missing",
    "guide-version",
    "guide-unreadable",
    "guide-mode",
    "guide-unknown-section",
    "guide-unknown-part",
    "guide-request-too-long",
    "guide-over-budget",
)


class GuideRefusal(Exception):
    """A ``guide`` request the corpus cannot answer; :attr:`reply` is the text the tool sends."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


# ---------------------------------------------------------------------------
# The corpus, indexed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuideIndex:
    """A corpus with its sections and pages looked up by id and by page name, and its toc packed.

    Built once per corpus (:meth:`of`), so neither the producer's check of every reply nor a
    request re-indexes the corpus or re-packs a toc.
    """

    corpus: Mapping[str, Any]
    sections: Mapping[str, Mapping[str, Any]]
    pages: Mapping[str, Mapping[str, Any]]
    #: Each page's own ``##``/``###`` section ids, in document order.
    page_sections: Mapping[str, tuple[str, ...]]
    #: The toc's parts (:func:`toc_parts`) of the whole guide (key ``None``) and of each page.
    toc: Mapping[str | None, tuple[tuple[Mapping[str, Any], ...], ...]]

    @classmethod
    def of(cls, corpus: Mapping[str, Any]) -> GuideIndex:
        sections = {section["id"]: section for section in corpus["sections"]}
        pages = {page["page"]: page for page in corpus["pages"]}
        page_sections: dict[str, list[str]] = {name: [] for name in pages}
        for section in corpus["sections"]:
            if section["level"] > 1:
                page_sections[section["page"]].append(section["id"])
        frozen_sections = MappingProxyType({name: tuple(ids) for name, ids in page_sections.items()})
        toc: dict[str | None, tuple[tuple[Mapping[str, Any], ...], ...]] = {}
        for page in (None, *pages):
            entries = _toc_entries(sections, pages, frozen_sections, page)
            toc[page] = tuple(tuple(MappingProxyType(entry) for entry in part) for part in _pack_toc(page, entries))
        return cls(
            corpus=corpus,
            sections=MappingProxyType(sections),
            pages=MappingProxyType(pages),
            page_sections=frozen_sections,
            toc=MappingProxyType(toc),
        )


def section_link(section_id: str) -> str:
    """The link that opens *section_id* in the guide's editor tab: the prefix, then the id."""
    return SECTION_LINK_PREFIX + section_id


def reply_wire_bytes(reply: Mapping[str, Any] | str) -> int:
    """The wire bytes of one ``guide`` reply: a mapping by ``measure``, a text by ``escaped_bytes``."""
    if isinstance(reply, str):
        return escaped_bytes(reply)
    return measure(reply)


# ---------------------------------------------------------------------------
# section
# ---------------------------------------------------------------------------


def compose_section_reply(
    section: Mapping[str, Any],
    page_title: str,
    part: int,
    parts: int,
    text: str,
    next_part: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The ``section`` reply for one part of *section*, its text given.

    It takes the pieces rather than an index because the producer composes candidate parts before
    the corpus exists.
    """
    return {
        "response": "guide",
        "mode": "section",
        "id": section["id"],
        "page_title": page_title,
        "breadcrumb": list(section["breadcrumb"]),
        "purpose": section["purpose"],
        "link": section_link(section["id"]),
        "part": part,
        "parts": parts,
        "next": dict(next_part) if next_part is not None else None,
        "text": text,
    }


def compose_section(index: GuideIndex, section_id: str, part: int = 1) -> dict[str, Any]:
    """Part *part* of *section_id*'s subtree; ``next`` names the following part, or ``None``.

    Raises :class:`KeyError` for an id the corpus does not hold and :class:`GuideRefusal` for a
    part the id does not have.
    """
    section = index.sections[section_id]
    parts = section["parts"]
    if not 1 <= part <= len(parts):
        raise GuideRefusal(unknown_part_refusal(section_id, len(parts)))
    following = {"section": section_id, "part": part + 1} if part < len(parts) else None
    page_title = index.pages[section["page"]]["title"]
    return compose_section_reply(section, page_title, part, len(parts), parts[part - 1], following)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def hit_entry(index: GuideIndex, section_id: str, part: int = 1) -> dict[str, Any]:
    """One search hit: the id, where it sits, its purpose, its link, its parts and the ready ``read`` request."""
    section = index.sections[section_id]
    return {
        "id": section_id,
        "page_title": index.pages[section["page"]]["title"],
        "breadcrumb": list(section["breadcrumb"]),
        "purpose": section["purpose"],
        "link": section_link(section_id),
        "parts": len(section["parts"]),
        "read": {"section": section_id, "part": part},
    }


def compose_search(index: GuideIndex, query: str, hits: Sequence[tuple[str, int]], more: bool) -> dict[str, Any]:
    """The ``search`` reply for *query*: *hits* as ``(id, part)`` in rank order, at most five.

    *more* says the ranking found more than the reply carries. With no hit the
    reply carries :data:`NO_MATCH_SENTENCE` as ``note``.
    """
    if len(hits) > SEARCH_HIT_LIMIT:
        raise ValueError(f"a search reply carries at most {SEARCH_HIT_LIMIT} hits, not {len(hits)}")
    reply: dict[str, Any] = {
        "response": "guide",
        "mode": "search",
        "query": query,
        "hits": [hit_entry(index, section_id, part) for section_id, part in hits],
        "more": more,
    }
    if not hits:
        reply["note"] = NO_MATCH_SENTENCE
    return reply


# ---------------------------------------------------------------------------
# toc
# ---------------------------------------------------------------------------


def _section_entry(section: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": section["id"],
        "title": section["breadcrumb"][-1],
        "purpose": section["purpose"],
        "level": section["level"],
        "parts": len(section["parts"]),
    }


def _toc_entries(
    sections: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Mapping[str, Any]],
    page_sections: Mapping[str, tuple[str, ...]],
    page: str | None,
) -> list[dict[str, Any]]:
    if page is None:
        return [
            {"page": name, "title": record["title"], "sections": len(page_sections[name])}
            for name, record in pages.items()
        ]
    return [_section_entry(sections[section_id]) for section_id in page_sections[page]]


def toc_entries(index: GuideIndex, page: str | None) -> list[dict[str, Any]]:
    """What ``toc`` lists: the pages without *page*, that page's sections with it.

    Without *page*: every page, each entry's ``sections`` the number of entries
    ``toc(page=…)`` lists for it. With *page*: that page's ``##``/``###`` sections
    in document order. Raises :class:`KeyError` for a page the corpus does not hold.
    """
    if page is not None and page not in index.pages:
        raise KeyError(page)
    return _toc_entries(index.sections, index.pages, index.page_sections, page)


def _toc_reply(
    page: str | None, entries: Sequence[Mapping[str, Any]], part: int, parts: int, following: Mapping | None
) -> dict[str, Any]:
    reply: dict[str, Any] = {"response": "guide", "mode": "toc"}
    if page is not None:
        reply["page"] = page
    reply["pages" if page is None else "sections"] = [dict(entry) for entry in entries]
    reply["part"] = part
    reply["parts"] = parts
    reply["next"] = dict(following) if following is not None else None
    return reply


def _toc_next(page: str | None, part: int) -> dict[str, Any]:
    request: dict[str, Any] = {"toc": True}
    if page is not None:
        request["page"] = page
    request["part"] = part
    return request


#: The widest ``part``/``parts`` a toc part or a section part is packed for: the packing composes
#: a candidate before the number of parts is known, and the real reply is never wider while a list
#: has at most this many parts.
PACKING_PART_PLACEHOLDER = 999


def _pack_toc(page: str | None, entries: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    placeholder = _toc_next(page, PACKING_PART_PLACEHOLDER)
    parts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        candidate = [*current, entry]
        reply = _toc_reply(page, candidate, PACKING_PART_PLACEHOLDER, PACKING_PART_PLACEHOLDER, placeholder)
        if current and measure(reply) > GUIDE_REPLY_BUDGET_WIRE_BYTES:
            parts.append(current)
            current = [entry]
        else:
            current = candidate
    parts.append(current)
    return parts


def toc_parts(index: GuideIndex, page: str | None) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """The toc's entries for *page*, cut greedily into parts that each fit the budget.

    A single entry that cannot fit alone is still a part of its own; the producer's check of every
    reply refuses it. Raises :class:`KeyError` for a page the corpus does not hold.
    """
    return index.toc[page]


def compose_toc(index: GuideIndex, page: str | None = None, part: int = 1) -> dict[str, Any]:
    """Part *part* of the toc of *page* (of the whole guide without one).

    Raises :class:`KeyError` for a page the corpus does not hold and :class:`GuideRefusal` for a
    part the toc does not have.
    """
    parts = toc_parts(index, page)
    if not 1 <= part <= len(parts):
        raise GuideRefusal(unknown_part_refusal(page if page is not None else "toc", len(parts)))
    following = _toc_next(page, part + 1) if part < len(parts) else None
    return _toc_reply(page, parts[part - 1], part, len(parts), following)


# ---------------------------------------------------------------------------
# The refusals: one sentence each, no location
# ---------------------------------------------------------------------------


def _refusal(code: str, sentence: str) -> str:
    return f"ERROR: {code}: {sentence}"


def missing_refusal() -> str:
    """No installed site, or a site with no corpus."""
    return _refusal(
        "guide-missing",
        "this installation carries no guide for the assistant to read; "
        "the guide comes with a SWCraft installation, so reinstall SWCraft to get it",
    )


def version_refusal(corpus_version: str, running_version: str) -> str:
    """A corpus built for another release; names both versions."""
    return _refusal(
        "guide-version",
        f"the installed guide was built for SWCraft {corpus_version}, and this is SWCraft {running_version}; "
        "reinstall SWCraft so the guide matches it",
    )


def unreadable_refusal() -> str:
    """A malformed corpus, or one of an unknown schema."""
    return _refusal(
        "guide-unreadable",
        "the installed guide cannot be read: its file is malformed or of a form this release does not know; "
        "reinstall SWCraft",
    )


def mode_refusal() -> str:
    """Not exactly one mode, or an argument outside the mode it belongs to."""
    return _refusal(
        "guide-mode",
        "ask exactly one thing per call: `search` with words, or `toc=True` (optionally with `page`), "
        "or `section` with an id; `page` belongs to toc alone, and `part` to toc and section",
    )


def unknown_section_refusal(requested: str, nearest: Sequence[str]) -> str:
    """An id (or a toc page) the guide does not hold; echoes it, with the nearest ids."""
    sentence = f"the guide has no section `{requested}`"
    if nearest:
        sentence += "; the nearest are " + ", ".join(f"`{section_id}`" for section_id in nearest)
    return _refusal("guide-unknown-section", sentence)


def unknown_part_refusal(target: str, parts: int) -> str:
    """A part the id (or the toc) does not have. Echoes the target, never the part asked."""
    return _refusal("guide-unknown-part", f"`{target}` has {parts} part(s); ask for a part from 1 to {parts}")


def request_too_long_refusal() -> str:
    """A query, id or page over the bound: a constant sentence that echoes nothing."""
    return _refusal(
        "guide-request-too-long",
        f"a search, a section id or a page name is at most {REQUEST_CAP_CHARACTERS} characters",
    )


def over_budget_refusal(target: str | None = None) -> str:
    """A reply over the budget -- a defect no corpus the producer accepted can reach.

    Names *target* (a request's, at most :data:`REQUEST_CAP_CHARACTERS`); without one --
    a refusal that is itself over the budget -- a constant sentence that echoes nothing.
    """
    subject = "a reply" if target is None else f"the reply for `{target}`"
    return _refusal(
        "guide-over-budget",
        f"{subject} is over the {GUIDE_REPLY_BUDGET_WIRE_BYTES}-byte budget; "
        "this is a defect of the installed guide, so report it",
    )


# ---------------------------------------------------------------------------
# The ranking
# ---------------------------------------------------------------------------

#: A heading's weight per matched query token: the section's own heading.
HEADING_WEIGHT = 8

#: An ancestor heading's weight: a heading between the page and the section.
ANCESTOR_WEIGHT = 4

#: An authored purpose's weight: the one line under the section's heading saying when
#: it helps, weighed as an ancestor heading is.
PURPOSE_WEIGHT = 4

#: The page's weight: one of its author keywords, or its title.
PAGE_WEIGHT = 3

#: The body's weight per occurrence, counted up to :data:`BODY_COUNT_CAP`.
BODY_WEIGHT = 1

#: The most occurrences of one query token the body is credited with.
BODY_COUNT_CAP = 3

#: The most ids an unknown-section refusal names as the nearest.
NEAREST_LIMIT = 3

#: The section an overview query ranks first: the home page, which says what SWCraft is.
OVERVIEW_SECTION = "index"

#: The overview queries, each as :func:`_overview_form` spells a query, stop words kept. Only
#: these route to :data:`OVERVIEW_SECTION`, because their words alone are stop words or words every
#: page could hold. The rule reads the query before it is tokenised.
OVERVIEW_QUERIES = frozenset(
    {
        "what is swcraft",
        "what s swcraft",
        "what is swcraft for",
        "what is swcraft used for",
        "what does swcraft do",
        "what can swcraft do",
        "overview of swcraft",
        "an overview of swcraft",
        "swcraft overview",
        "introduction to swcraft",
        "about swcraft",
    }
)

#: The words no query token matches: English function words, five auxiliaries and the product's own
#: name, which every page carries. A query of stop words alone matches nothing but one of :data:`OVERVIEW_QUERIES`.
STOP_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "at",
        "by", "for", "with", "from", "as", "is", "are", "was", "be", "it",
        "its", "this", "that", "these", "those", "do", "does", "how", "what", "which",
        "who", "i", "my", "me", "we", "you", "your", "can", "into", "if",
        "has", "have", "will", "shall", "should", "swcraft",
    }
)  # fmt: skip

#: The five auxiliaries and the product's name of :data:`STOP_WORDS`, which a BM25 measurement can
#: do without: with an ``idf`` a word every page holds weighs little anyway.
AUXILIARY_STOP_WORDS = frozenset({"has", "have", "will", "shall", "should", "swcraft"})

#: A measurement ranker: the weights and the order above, each section scored over its whole own body.
RANKER_WEIGHTS = "weights"

#: A measurement ranker: plain BM25 (:func:`swcraft_tools.bm25.bm25_scores`) over one bag per
#: section holding every field's words -- its heading, its ancestor headings, its purpose, its
#: page's title and keywords and its own body -- ordered by the score, then the id.
RANKER_BM25 = "bm25"

#: A measurement ranker: BM25F (:func:`swcraft_tools.bm25.bm25f_scores`) over
#: :data:`BM25F_FIELDS` with :data:`BM25F_BOOSTS`, ordered by the score, then the id.
RANKER_BM25F = "bm25f"

#: The weights ranker with one structural change against a section with a long body
#: (:data:`WEIGHTS_VARIANTS`): the coverage of the order counts the heading, ancestor,
#: purpose and page fields alone; the body counts in the score only.
RANKER_WEIGHTS_FIELDS = "weights-fields"

#: The weights ranker with the body's credit normalised by the body's length, BM25-style
#: (:func:`_normalised_count`); the coverage still counts a body match.
RANKER_WEIGHTS_NORM = "weights-norm"

#: The weights ranker scoring each corpus part of a section's own body as
#: a unit of its own (the section's heading, ancestor, purpose and page fields with that part's
#: body); a section ranks by its best part (the first part on a tie), and its hit's ``read``
#: names that part. A section whose own body fits one part ranks as under :data:`RANKER_WEIGHTS`.
RANKER_WEIGHTS_PARTS = "weights-parts"

#: :data:`RANKER_WEIGHTS_FIELDS` and :data:`RANKER_WEIGHTS_NORM` together.
RANKER_WEIGHTS_FIELDS_NORM = "weights-fields-norm"

#: The default ranker: :data:`RANKER_WEIGHTS_FIELDS` and :data:`RANKER_WEIGHTS_PARTS` together
#: -- the coverage over the non-body fields, each part of the own body scored, the best part
#: ranking the section and named by its hit's ``read``.
RANKER_WEIGHTS_FIELDS_PARTS = "weights-fields-parts"

#: :data:`RANKER_WEIGHTS_NORM` and :data:`RANKER_WEIGHTS_PARTS` together (normalised
#: against the average part).
RANKER_WEIGHTS_NORM_PARTS = "weights-norm-parts"

#: All three structural changes together.
RANKER_WEIGHTS_FIELDS_NORM_PARTS = "weights-fields-norm-parts"


@dataclass(frozen=True)
class WeightsVariant:
    """One structural setting of the weights ranker: what the coverage counts, how the body is credited, the unit."""

    #: A body match counts toward the coverage (the distinct query tokens matched).
    body_in_coverage: bool
    #: The body's count is divided by its length normalisation (:func:`_normalised_count`).
    body_normalised: bool
    #: Each corpus part of the section's own body is scored as a unit; the best part ranks.
    per_part: bool


#: The weights rankers by their structural setting.
WEIGHTS_VARIANTS: Mapping[str, WeightsVariant] = MappingProxyType(
    {
        RANKER_WEIGHTS: WeightsVariant(body_in_coverage=True, body_normalised=False, per_part=False),
        RANKER_WEIGHTS_FIELDS: WeightsVariant(body_in_coverage=False, body_normalised=False, per_part=False),
        RANKER_WEIGHTS_NORM: WeightsVariant(body_in_coverage=True, body_normalised=True, per_part=False),
        RANKER_WEIGHTS_PARTS: WeightsVariant(body_in_coverage=True, body_normalised=False, per_part=True),
        RANKER_WEIGHTS_FIELDS_NORM: WeightsVariant(body_in_coverage=False, body_normalised=True, per_part=False),
        RANKER_WEIGHTS_FIELDS_PARTS: WeightsVariant(body_in_coverage=False, body_normalised=False, per_part=True),
        RANKER_WEIGHTS_NORM_PARTS: WeightsVariant(body_in_coverage=True, body_normalised=True, per_part=True),
        RANKER_WEIGHTS_FIELDS_NORM_PARTS: WeightsVariant(body_in_coverage=False, body_normalised=True, per_part=True),
    }
)

#: The rankers :func:`rank` accepts: the weights variants, then the two BM25 rankers.
RANKERS = (*WEIGHTS_VARIANTS, RANKER_BM25, RANKER_BM25F)

#: The ranker :func:`rank`, :func:`search` and so the tool use when none is named.
DEFAULT_RANKER = RANKER_WEIGHTS_FIELDS_PARTS

#: BM25F's fields, each a bag of a section's tokens: its own heading, the headings between
#: the page and it, its authored purpose, its page's title and keywords, its own body.
BM25F_FIELDS = ("heading", "ancestors", "purpose", "page", "body")

#: BM25F's boost per field: the weights rankers' weights (the body without :data:`BODY_COUNT_CAP`:
#: ``k1`` saturates it).
BM25F_BOOSTS: Mapping[str, float] = MappingProxyType(
    {
        "heading": HEADING_WEIGHT,
        "ancestors": ANCESTOR_WEIGHT,
        "purpose": PURPOSE_WEIGHT,
        "page": PAGE_WEIGHT,
        "body": BODY_WEIGHT,
    }
)

_TOKEN = re.compile(r"[0-9a-z]+")

#: A plural ``s`` is dropped from a token longer than this many letters.
_PLURAL_MIN_LENGTH = 3


def tokens(text: str, stop_words: frozenset[str] = STOP_WORDS) -> list[str]:
    """*text*'s tokens: NFKC, lowercased, runs of ``[0-9a-z]``, stop words out, a plural ``s`` off.

    A stop word is recognised before the ``s`` is dropped (``does`` is a stop word, not ``doe``);
    the ``s`` is dropped from a token longer than three characters (``bus`` stays ``bus``).
    """
    found = []
    for token in _TOKEN.findall(unicodedata.normalize("NFKC", text).lower()):
        if token in stop_words:
            continue
        if len(token) > _PLURAL_MIN_LENGTH and token.endswith("s"):
            token = token[:-1]
        found.append(token)
    return found


@dataclass(frozen=True)
class _Scored:
    """One section's tokens, by where they stand (its body is in its units)."""

    heading: frozenset[str]
    ancestors: frozenset[str]
    purpose: frozenset[str]
    page: frozenset[str]


def _own_text(guide: GuideIndex, section_id: str) -> str:
    """The section's whole own body: the corpus record's ``own``, its parts joined as the parts are."""
    return "\n\n".join(guide.sections[section_id]["own"])


@dataclass(frozen=True)
class _Unit:
    """One scoring unit of a weights variant: a section's own body, or one part of it."""

    part: int
    body: Mapping[str, int]
    #: The body's length normalisation (:func:`_normalised_count`), against the average unit.
    norm: float


def _with_norms(bodies: Mapping[str, Sequence[tuple[int, Mapping[str, int]]]]) -> Mapping[str, tuple[_Unit, ...]]:
    """*bodies* as units, each with BM25's length normalisation, ``1 - b + b * length / average``."""
    lengths = [sum(body.values()) for found in bodies.values() for _, body in found]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    return MappingProxyType(
        {
            section_id: tuple(
                _Unit(
                    part=number,
                    body=MappingProxyType(dict(body)),
                    norm=(1 - BM25_B + BM25_B * sum(body.values()) / average) if average else 1.0,
                )
                for number, body in found
            )
            for section_id, found in bodies.items()
        }
    )


@dataclass(frozen=True)
class SearchIndex:
    """A corpus indexed for the ranking: each section's tokens by where they stand, its body by part.

    Built once per loaded corpus (:meth:`of`); holds what the per-part rankers read, the default
    among them.
    """

    guide: GuideIndex
    scored: Mapping[str, _Scored]
    #: Every section's per-part units: one per corpus part its own body (``own``) reaches.
    units: Mapping[str, tuple[_Unit, ...]]

    @classmethod
    def of(cls, guide: GuideIndex) -> SearchIndex:
        scored: dict[str, _Scored] = {}
        bodies: dict[str, list[tuple[int, Counter[str]]]] = {}
        for section_id, section in guide.sections.items():
            page = guide.pages[section["page"]]
            page_words = set(tokens(page["title"]))
            for keyword in page["keywords"]:
                page_words.update(tokens(keyword))
            breadcrumb = section["breadcrumb"]
            ancestors = {token for heading in breadcrumb[1:-1] for token in tokens(heading)}
            scored[section_id] = _Scored(
                heading=frozenset(tokens(breadcrumb[-1])),
                ancestors=frozenset(ancestors),
                purpose=frozenset(tokens(section["purpose"])),
                page=frozenset(page_words),
            )
            bodies[section_id] = [(number, Counter(tokens(text))) for number, text in enumerate(section["own"], 1)]
        return cls(guide=guide, scored=MappingProxyType(scored), units=_with_norms(bodies))


@dataclass(frozen=True)
class MeasurementIndex(SearchIndex):
    """A :class:`SearchIndex` with what the measurement rankers alone read, built at once.

    The whole-body units and BM25's plain and fielded corpora over *stop_words*. The tool never
    builds one.
    """

    #: Every section's one whole-body unit.
    whole_units: Mapping[str, tuple[_Unit, ...]]
    #: The stop words of the BM25 corpora and of a BM25 ranker's query.
    stop_words: frozenset[str]
    plain: Corpus
    fielded: FieldCorpus

    @classmethod
    def over(cls, search_index: SearchIndex, stop_words: frozenset[str] = STOP_WORDS) -> MeasurementIndex:
        guide = search_index.guide
        plain, fielded = Corpus(), FieldCorpus(BM25F_FIELDS)
        for section_id in guide.sections:
            bags = _field_bags(guide, section_id, stop_words)
            plain.add(section_id, [token for name in BM25F_FIELDS for token in bags[name]])
            fielded.add(section_id, bags)
        whole = {section_id: [(1, Counter(tokens(_own_text(guide, section_id))))] for section_id in guide.sections}
        return cls(
            guide=guide,
            scored=search_index.scored,
            units=search_index.units,
            whole_units=_with_norms(whole),
            stop_words=stop_words,
            plain=plain,
            fielded=fielded,
        )


def _field_bags(guide: GuideIndex, section_id: str, stop_words: frozenset[str]) -> dict[str, list[str]]:
    """*section_id*'s tokens per field of :data:`BM25F_FIELDS`, every occurrence kept."""
    section = guide.sections[section_id]
    page = guide.pages[section["page"]]
    page_words = tokens(page["title"], stop_words)
    for keyword in page["keywords"]:
        page_words.extend(tokens(keyword, stop_words))
    breadcrumb = section["breadcrumb"]
    return {
        "heading": tokens(breadcrumb[-1], stop_words),
        "ancestors": [token for heading in breadcrumb[1:-1] for token in tokens(heading, stop_words)],
        "purpose": tokens(section["purpose"], stop_words),
        "page": page_words,
        "body": tokens(_own_text(guide, section_id), stop_words),
    }


def _measurement(search_index: SearchIndex, ranker: str) -> MeasurementIndex:
    if not isinstance(search_index, MeasurementIndex):
        raise ValueError(f"the ranker {ranker!r} reads a MeasurementIndex (MeasurementIndex.over)")
    return search_index


def bm25_ranking_scores(search_index: SearchIndex, query: str, ranker: str) -> dict[str, float]:
    """Every section's score under a BM25 ranker, the sections no query token reaches left out.

    *search_index* is a :class:`MeasurementIndex`, whose stop words the query is tokenised with.
    """
    measured = _measurement(search_index, ranker)
    query_tokens = tokens(query, measured.stop_words)
    if ranker == RANKER_BM25:
        return bm25_scores(measured.plain, query_tokens)
    if ranker == RANKER_BM25F:
        return bm25f_scores(measured.fielded, query_tokens, BM25F_BOOSTS)
    raise ValueError(f"not a BM25 ranker: {ranker!r}")


def _normalised_count(unit: _Unit, token: str) -> float:
    """*token*'s count in *unit*'s body divided by the unit's length normalisation."""
    return unit.body.get(token, 0) / unit.norm


def _variant_score(
    scored: _Scored, unit: _Unit, query: Sequence[str], variant: WeightsVariant
) -> tuple[int, float, bool]:
    """``(coverage, score, any token matched)`` of one unit under a weights variant, per distinct query token.

    The body is credited :data:`BODY_WEIGHT` times ``min(count, cap)`` or, normalised,
    ``min(count / norm, cap)``; a body match counts toward the coverage only when
    :attr:`WeightsVariant.body_in_coverage`.
    """
    covered = 0
    total = 0.0
    matched = False
    for token in query:
        fields = 0
        if token in scored.heading:
            fields += HEADING_WEIGHT
        if token in scored.ancestors:
            fields += ANCESTOR_WEIGHT
        if token in scored.purpose:
            fields += PURPOSE_WEIGHT
        if token in scored.page:
            fields += PAGE_WEIGHT
        count = unit.body.get(token, 0)
        credited = _normalised_count(unit, token) if variant.body_normalised else count
        weight = fields + BODY_WEIGHT * min(credited, BODY_COUNT_CAP)
        if weight:
            matched = True
            total += weight
        if fields or (variant.body_in_coverage and count):
            covered += 1
    return covered, total, matched


def _weights_units(search_index: SearchIndex, ranker: str) -> tuple[WeightsVariant, Mapping[str, tuple[_Unit, ...]]]:
    """A weights *ranker*'s variant and the units it scores: the per-part units, or a measurement's whole ones."""
    variant = WEIGHTS_VARIANTS[ranker]
    if isinstance(search_index, MeasurementIndex) and search_index.stop_words != STOP_WORDS:
        raise ValueError("the weights rankers' stop words are STOP_WORDS")
    if variant.per_part:
        return variant, search_index.units
    return variant, _measurement(search_index, ranker).whole_units


def _best(
    search_index: SearchIndex,
    section_id: str,
    query_tokens: Sequence[str],
    variant: WeightsVariant,
    units: Mapping[str, tuple[_Unit, ...]],
) -> tuple[int, float, int] | None:
    """``(coverage, score, part)`` of *section_id*'s best unit (the first on a tie); ``None`` when none matched."""
    scored = search_index.scored[section_id]
    best: tuple[int, float, int] | None = None
    for unit in units[section_id]:
        covered, total, matched = _variant_score(scored, unit, query_tokens, variant)
        if matched and (best is None or (covered, total) > best[:2]):
            best = (covered, total, unit.part)
    return best


def score(search_index: SearchIndex, section_id: str, query: str, *, ranker: str = DEFAULT_RANKER) -> tuple[int, float]:
    """``(coverage, score)`` of *section_id* for *query* under a weights *ranker*, at its best unit.

    ``(0, 0)`` when no query token reaches the section.
    """
    variant, units = _weights_units(search_index, ranker)
    best = _best(search_index, section_id, sorted(set(tokens(query))), variant, units)
    return (0, 0) if best is None else (best[0], best[1])


def _exact(search_index: SearchIndex, query: str) -> tuple[str, int] | None:
    """The section an exact lookup names, with the part that holds it; ``None`` when none does."""
    key = query.strip()
    explains = search_index.guide.corpus["explains"]
    for candidate in (key, f"rule:{key}"):
        if candidate in explains:
            entry = explains[candidate]
            return entry["section"], entry["part"]
    if key in search_index.guide.sections:
        return key, 1
    return None


def _overview_form(query: str) -> str:
    """*query* as :data:`OVERVIEW_QUERIES` spells one: NFKC, lowercased, ``[0-9a-z]`` runs, one space."""
    return " ".join(_TOKEN.findall(unicodedata.normalize("NFKC", query).lower()))


def _overview(search_index: SearchIndex, query: str) -> tuple[str, int] | None:
    """:data:`OVERVIEW_SECTION` at part 1 for an overview query; ``None`` for any other."""
    if _overview_form(query) in OVERVIEW_QUERIES and OVERVIEW_SECTION in search_index.guide.sections:
        return OVERVIEW_SECTION, 1
    return None


def rank(
    search_index: SearchIndex,
    query: str,
    among: Sequence[str] | None = None,
    *,
    ranker: str = DEFAULT_RANKER,
) -> list[tuple[str, int]]:
    """Every section *query* matches, best first, as ``(id, part)``.

    An exact lookup first: a query that is, stripped, a key of ``explains`` (a refusal code, a rule
    id with or without ``rule:``) or a section id ranks that section first, at the part that holds
    the explanation; failing that, an overview query ranks :data:`OVERVIEW_SECTION` first. Then,
    under a weights *ranker*, every section that matches at least one distinct query token, ordered
    by the coverage, then the score, then the id ascending -- at the best part's number under a
    per-part ranker, else at part 1; under a BM25 ranker every section with a score above 0, ordered
    by the score, then the id, at part 1. *among* narrows the sections ranked, and then neither
    lookup applies. A whole-body or a BM25 ranker reads a :class:`MeasurementIndex`, and a weights
    ranker refuses one built over other stop words than :data:`STOP_WORDS`.
    """
    if ranker not in RANKERS:
        raise ValueError(f"unknown ranker {ranker!r}; one of {', '.join(RANKERS)}")
    ranked: list[tuple[str, int]] = []
    exact = (_exact(search_index, query) or _overview(search_index, query)) if among is None else None
    if exact is not None:
        ranked.append(exact)
    candidates = [
        section_id
        for section_id in (among if among is not None else search_index.scored)
        if exact is None or section_id != exact[0]
    ]
    keyed: list[tuple[tuple[float, float, str], str, int]] = []
    if ranker in WEIGHTS_VARIANTS:
        variant, units = _weights_units(search_index, ranker)
        query_tokens = sorted(set(tokens(query)))
        for section_id in candidates:
            best = _best(search_index, section_id, query_tokens, variant, units)
            if best is not None:
                keyed.append(((-best[0], -best[1], section_id), section_id, best[2]))
    else:
        scores = bm25_ranking_scores(search_index, query, ranker)
        for section_id in candidates:
            value = scores.get(section_id, 0.0)
            if value > 0:
                keyed.append(((0, -value, section_id), section_id, 1))
    ranked.extend((section_id, part) for _, section_id, part in sorted(keyed))
    return ranked


def nearest(search_index: SearchIndex, requested: str, among: Sequence[str] | None = None) -> list[str]:
    """The ids nearest *requested* by the ranking of its words, at most :data:`NEAREST_LIMIT`."""
    return [section_id for section_id, _ in rank(search_index, requested, among)[:NEAREST_LIMIT]]


# ---------------------------------------------------------------------------
# The three modes over a loaded corpus
# ---------------------------------------------------------------------------


def search(search_index: SearchIndex, query: str, *, ranker: str = DEFAULT_RANKER) -> dict[str, Any]:
    """The ``search`` reply for *query*: the first :data:`SEARCH_HIT_LIMIT` hits and ``more``, by *ranker*."""
    ranked = rank(search_index, query, ranker=ranker)
    return compose_search(search_index.guide, query, ranked[:SEARCH_HIT_LIMIT], more=len(ranked) > SEARCH_HIT_LIMIT)


def toc(search_index: SearchIndex, page: str | None = None, part: int = 1) -> dict[str, Any]:
    """The ``toc`` reply: the pages, or *page*'s sections, part *part*.

    An unknown page is refused as an unknown section, naming the nearest pages. A page named with
    its source file's ``.md`` is that page, and the reply names it without the suffix.
    """
    if page is not None:
        page = resolve_page(search_index.guide, page)
    try:
        return compose_toc(search_index.guide, page, part)
    except KeyError:
        pages = [section_id for section_id, record in search_index.guide.sections.items() if record["level"] == 1]
        raise GuideRefusal(unknown_section_refusal(str(page), nearest(search_index, str(page), pages))) from None


def section(search_index: SearchIndex, section_id: str, part: int = 1) -> dict[str, Any]:
    """The ``section`` reply: part *part* of *section_id*'s subtree.

    An unknown id is refused naming the nearest ids by the ranking of its words, so a stale id
    recovers in one call; when its page part names a page the guide holds, the nearest are that page
    and its own sections. An id whose page part is spelled as its source file
    (``upgrading.md#the-procedure``) is the id without the ``.md``, and the reply names that
    canonical id.
    """
    guide = search_index.guide
    resolved = resolve_section_id(guide, section_id)
    try:
        return compose_section(guide, resolved, part)
    except KeyError:
        page = resolve_page(guide, section_id.partition("#")[0])
        if page in guide.pages:
            near = nearest_on_page(search_index, page, section_id)
        else:
            near = nearest(search_index, section_id)
        raise GuideRefusal(unknown_section_refusal(section_id, near)) from None


#: The suffix of a guide page's source file, which an agent may write into a page or an id.
SOURCE_SUFFIX = ".md"


def resolve_page(guide: GuideIndex, page: str) -> str:
    """*page*, or the guide page it names without :data:`SOURCE_SUFFIX`.

    The suffix is stripped only when *page* is not a page, ends in the suffix and the guide holds the
    stripped page; else *page* is returned unchanged.
    """
    if page in guide.pages or not page.endswith(SOURCE_SUFFIX):
        return page
    stripped = page[: -len(SOURCE_SUFFIX)]
    return stripped if stripped in guide.pages else page


def resolve_section_id(guide: GuideIndex, section_id: str) -> str:
    """*section_id*, or its canonical form when its page part carries :data:`SOURCE_SUFFIX`.

    ``reference/rules.md`` is ``reference/rules`` and ``upgrading.md#the-procedure`` is
    ``upgrading#the-procedure`` -- only when the guide holds the resulting id; an id the
    guide holds, or one whose suffixed form names no section, is returned unchanged.
    """
    if section_id in guide.sections:
        return section_id
    page, hash_sign, anchor = section_id.partition("#")
    resolved_page = resolve_page(guide, page)
    if resolved_page == page:
        return section_id
    candidate = resolved_page + hash_sign + anchor
    return candidate if candidate in guide.sections else section_id


def nearest_on_page(search_index: SearchIndex, page: str, requested: str) -> list[str]:
    """The nearest ids to *requested* on *page*, at most :data:`NEAREST_LIMIT`.

    The page first, then its own sections by the ranking of *requested*'s words, filled up in
    document order.
    """
    own = search_index.guide.page_sections[page]
    chosen = [page]
    for section_id, _ in rank(search_index, requested, own):
        if len(chosen) == NEAREST_LIMIT:
            break
        chosen.append(section_id)
    for section_id in own:
        if len(chosen) == NEAREST_LIMIT:
            break
        if section_id not in chosen:
            chosen.append(section_id)
    return chosen


# ---------------------------------------------------------------------------
# The reader: the installed corpus, checked, loaded once
# ---------------------------------------------------------------------------

_LOADED: SearchIndex | None = None


def reset_cache() -> None:
    """Forget the loaded corpus; the next request reads it again."""
    global _LOADED
    _LOADED = None


def _installed_corpus() -> Path | None:
    """The corpus file of the installed site: the package payload, then the bundle; never the override."""
    for candidate in (guide_site.package_site_dir(), guide_site.bundle_site_dir()):
        if candidate is not None and guide_site.is_site(candidate):
            return candidate / CORPUS_FILENAME
    return None


def _strings(value: Any, *, empty: bool = True) -> TypeGuard[list[str]]:
    """Whether *value* is a list of strings (a non-empty list unless *empty*)."""
    return isinstance(value, list) and (empty or bool(value)) and all(isinstance(item, str) for item in value)


def _well_formed_section(record: Any, pages: set[str]) -> bool:
    """Whether *record* has a section's shape: a page's own at level 1, a ``##``/``###`` below it."""
    if not (
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and isinstance(record.get("page"), str)
        and record["page"] in pages
    ):
        return False
    level = record.get("level")
    parts = record.get("parts")
    own = record.get("own")
    return (
        type(level) is int
        and 1 <= level <= 3  # the cut's three levels: the page, `##`, `###`
        and (level == 1) == (record["id"] == record["page"])
        and isinstance(record.get("purpose"), str)
        and _strings(record.get("breadcrumb"), empty=False)
        and _strings(parts, empty=False)
        and _strings(own, empty=False)
        and len(own) <= len(parts)
    )


def _well_formed(corpus: Any) -> bool:
    """Whether *corpus* has the shape of schema 1.

    The header: the name, the schema and a ``swcraft_version`` matching
    :data:`CORPUS_VERSION_PATTERN` within its cap. The pages: unique names, a title and keywords.
    The sections: unique ids of a page the corpus holds, at level 1 (the page's own, one per page),
    2 or 3, with a purpose, a non-empty breadcrumb, at least one part and an ``own`` body of at most
    as many parts. The ``explains`` entries: a section the corpus holds, at a part it has. What this
    does not reach: whether the texts are the guide's, and whether a breadcrumb names the headings
    above its section.
    """
    if not (
        isinstance(corpus, dict)
        and corpus.get("corpus") == CORPUS_NAME
        and corpus.get("schema") == CORPUS_SCHEMA
        and isinstance(corpus.get("swcraft_version"), str)
        and len(corpus["swcraft_version"]) <= CORPUS_VERSION_CAP_CHARACTERS
        and CORPUS_VERSION_PATTERN.fullmatch(corpus["swcraft_version"]) is not None
    ):
        return False
    pages = corpus.get("pages")
    sections = corpus.get("sections")
    explains = corpus.get("explains")
    if not (isinstance(pages, list) and isinstance(sections, list) and isinstance(explains, dict)):
        return False
    names: set[str] = set()
    for page in pages:
        if not (
            isinstance(page, dict)
            and isinstance(page.get("page"), str)
            and page["page"] not in names
            and isinstance(page.get("title"), str)
            and _strings(page.get("keywords"))
        ):
            return False
        names.add(page["page"])
    parts_of: dict[str, int] = {}
    for record in sections:
        if not _well_formed_section(record, names) or record["id"] in parts_of:
            return False
        parts_of[record["id"]] = len(record["parts"])
    if not names <= set(parts_of):
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("section"), str)
        and entry["section"] in parts_of
        and type(entry.get("part")) is int
        and 1 <= entry["part"] <= parts_of[entry["section"]]
        for entry in explains.values()
    )


#: What walking a JSON-shaped value raises: a corpus this release cannot read, refused as one; any
#: other class is a defect and surfaces.
_SHAPE_ERRORS = (TypeError, ValueError, KeyError, AttributeError, IndexError, RecursionError)


def load_index() -> SearchIndex:
    """The installed corpus, checked and indexed; kept once loaded, a refusal never kept.

    Raises :class:`GuideRefusal`: ``guide-missing`` when neither step holds a site or the site holds
    no corpus, ``guide-unreadable`` for a malformed corpus or one of an unknown name or schema (an
    index that cannot be built from it included), ``guide-version`` for a corpus built for another
    release. No refusal names a location.
    """
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    corpus_file = _installed_corpus()
    if corpus_file is None or not corpus_file.is_file():
        raise GuideRefusal(missing_refusal())
    try:
        corpus = json.loads(corpus_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):  # a deep nesting is refused too
        raise GuideRefusal(unreadable_refusal()) from None
    try:
        well_formed = _well_formed(corpus)
    except _SHAPE_ERRORS:
        well_formed = False
    if not well_formed:
        raise GuideRefusal(unreadable_refusal())
    if corpus["swcraft_version"] != __version__:
        raise GuideRefusal(version_refusal(corpus["swcraft_version"], __version__))
    try:
        loaded = SearchIndex.of(GuideIndex.of(corpus))
    except _SHAPE_ERRORS:  # a shape the check above does not reach
        raise GuideRefusal(unreadable_refusal()) from None
    _LOADED = loaded
    return _LOADED


def _one_mode(searching: bool, toc_wanted: bool, page: str, section_id: str, part: int | None) -> bool:
    modes = [searching, bool(toc_wanted), bool(section_id)]
    if modes.count(True) != 1:
        return False
    if page and not toc_wanted:
        return False
    return part is None or not searching


def answer(
    search_text: str = "",
    toc_wanted: bool = False,
    page: str = "",
    section_id: str = "",
    part: int | None = None,
) -> dict[str, Any] | str:
    """The ``guide`` tool's reply to one request: a mapping, or a refusal's ``ERROR:`` text.

    Exactly one mode per request -- *search_text* with a character other than whitespace,
    *toc_wanted* (with an optional *page*), or *section_id* non-empty; *page* belongs to the toc
    alone and *part* to the toc and the section (1 when omitted). A *page* asked with neither a
    search nor a section is a toc request even without *toc_wanted*; beside a search or a section it
    is ``guide-mode``'s refusal. The request is checked before the corpus is read: the mode, then the
    bounds. A reply over :data:`GUIDE_REPLY_BUDGET_WIRE_BYTES` is refused as ``guide-over-budget``
    naming its target; a refusal over it gives way to that code's constant sentence.
    """
    searching = bool(search_text.strip())
    toc_wanted = bool(toc_wanted) or (bool(page) and not searching and not section_id)
    if not _one_mode(searching, toc_wanted, page, section_id, part):
        return _within_budget(mode_refusal())
    for value in (search_text, page, section_id):
        if len(value) > REQUEST_CAP_CHARACTERS:
            return _within_budget(request_too_long_refusal())
    resolved_part = 1 if part is None else part
    try:
        search_index = load_index()
        if searching:
            reply: dict[str, Any] = search(search_index, search_text)
            target = "search"
        elif toc_wanted:
            reply = toc(search_index, page or None, resolved_part)
            target = page or "toc"
        else:
            reply = section(search_index, section_id, resolved_part)
            target = section_id
    except GuideRefusal as refusal:
        return _within_budget(refusal.reply)
    if reply_wire_bytes(reply) > GUIDE_REPLY_BUDGET_WIRE_BYTES:
        return _within_budget(over_budget_refusal(target))
    return reply


def _within_budget(refusal: str) -> str:
    """*refusal*, or -- when it is over the budget -- the constant over-budget sentence."""
    if reply_wire_bytes(refusal) > GUIDE_REPLY_BUDGET_WIRE_BYTES:
        return over_budget_refusal()
    return refusal
