"""BM25 and BM25F, the one implementation the trace link search and the guide's rankers share.

A leaf: it imports the standard library alone, so it knows no layer, kind, field or guide page; a
caller hands it bags of terms it has already tokenised.

Two corpora, one formula:

* :class:`Corpus` -- one bag of terms per document key. :func:`bm25_stats` and
  :func:`bm25_scores` score a query against it, with an exact leave-one-out (:class:`Removal`).
* :class:`FieldCorpus` -- one bag per **field** per document key, for BM25F (:func:`bm25f_scores`):
  each field's term frequency is normalised by that field's length against the field's average,
  weighed by the field's boost and summed into one pseudo-frequency, which saturates once through
  :data:`BM25_K1`. The ``idf`` counts the documents holding the term in any field. With one field at
  boost 1 it is BM25 again.

``k1 = 1.2``, ``b = 0.75`` and ``idf = ln(1 + (N - df + 0.5) / (df + 0.5))``, which
is never negative. A query term no document holds contributes nothing; the terms
are summed in sorted order, so no set's order reaches a sum.

**What this does not reach.** It ranks terms, not meanings: the tokeniser, the stop
words and the fields are the caller's, and so is every decision about what a score
is worth.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BM25_B",
    "BM25_K1",
    "NO_REMOVAL",
    "Bm25Stats",
    "Corpus",
    "FieldCorpus",
    "Removal",
    "bm25_scores",
    "bm25_stats",
    "bm25f_scores",
    "idf",
    "scores_of",
    "term_contributions",
]

#: BM25's saturation: the standard default.
BM25_K1 = 1.2
#: BM25's length normalisation: the standard default.
BM25_B = 0.75


def idf(size: int, document_frequency: int) -> float:
    """``ln(1 + (N - df + 0.5) / (df + 0.5))``: *size* documents, *document_frequency* of them holding the term."""
    return math.log(1 + (size - document_frequency + 0.5) / (document_frequency + 0.5))


@dataclass
class Corpus:
    """One BM25 corpus: a bag of terms per document key, with an inverted index.

    A corpus with no bag, or whose bags are all empty, scores 0 for every document.
    """

    lengths: dict[Any, int] = field(default_factory=dict)
    frequencies: dict[Any, Counter[str]] = field(default_factory=dict)
    postings: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, key: Any, bag: Sequence[str]) -> None:
        counts = Counter(bag)
        self.lengths[key] = len(bag)
        self.frequencies[key] = counts
        for term in sorted(counts):
            self.postings.setdefault(term, []).append(key)

    @property
    def size(self) -> int:
        return len(self.lengths)

    @property
    def total_length(self) -> int:
        return sum(self.lengths.values())

    def document_frequency(self, term: str) -> int:
        return len(self.postings.get(term, ()))


@dataclass(frozen=True)
class Removal:
    """The terms one requirement's text adds to some bags of a corpus, to be left out of them (the leave-one-out)."""

    #: The document keys whose bag holds the text.
    keys: frozenset[Any]
    counts: Counter[str]

    @property
    def length(self) -> int:
        return sum(self.counts.values())


#: No leave-one-out: every bag as it stands.
NO_REMOVAL = Removal(frozenset(), Counter())


@dataclass(frozen=True)
class Bm25Stats:
    """A corpus's statistics for one query, the leave-one-out applied: ``N``, ``avgdl`` and each term's ``idf``."""

    corpus: Corpus
    removal: Removal
    size: int
    average: float
    #: Distinct query term -> its idf over the reduced corpus, for the terms some reduced bag still holds.
    idf: dict[str, float]

    def frequency(self, key: Any, term: str) -> int:
        """The term's frequency in *key*'s bag after the leave-one-out."""
        frequency = self.corpus.frequencies[key].get(term, 0)
        if key in self.removal.keys:
            frequency -= self.removal.counts.get(term, 0)
        return frequency

    def length(self, key: Any) -> int:
        return self.corpus.lengths[key] - (self.removal.length if key in self.removal.keys else 0)

    def contribution(self, key: Any, term: str) -> float:
        """One term's BM25 contribution to *key*'s bag; 0 when the bag does not hold it."""
        idf = self.idf.get(term)
        if idf is None:
            return 0.0
        frequency = self.frequency(key, term)
        if frequency <= 0:
            return 0.0
        norm = frequency + BM25_K1 * (1 - BM25_B + BM25_B * self.length(key) / self.average)
        return idf * frequency * (BM25_K1 + 1) / norm


def bm25_stats(corpus: Corpus, query: Sequence[str], removal: Removal = NO_REMOVAL) -> Bm25Stats | None:
    """The reduced corpus's statistics for *query*, or ``None`` when it has no bag or only empty ones.

    The leave-one-out is exact: the removed text leaves every bag in
    ``removal.keys``, a bag left empty leaves the corpus, and ``N``, ``avgdl``
    and every ``df`` are those of the reduced corpus.
    """
    removed_length = removal.length
    dropped = sum(1 for key in removal.keys if corpus.lengths[key] == removed_length)
    size = corpus.size - dropped
    total = corpus.total_length - removed_length * len(removal.keys)
    if size == 0 or total == 0:
        return None
    idfs: dict[str, float] = {}
    for term in sorted(set(query)):
        document_frequency = corpus.document_frequency(term)
        removed = removal.counts.get(term, 0)
        if removed:
            document_frequency -= sum(1 for key in removal.keys if corpus.frequencies[key].get(term, 0) == removed)
        if document_frequency > 0:
            idfs[term] = idf(size, document_frequency)
    return Bm25Stats(corpus, removal, size, total / size, idfs)


def bm25_scores(corpus: Corpus, query: Sequence[str], removal: Removal = NO_REMOVAL) -> dict[Any, float]:
    """BM25 of the distinct *query* terms against every document they reach, *removal* left out exactly.

    A document no query term reaches is absent from the answer, its score 0, and so is every
    document of a corpus with no bag or only empty ones.
    """
    return scores_of(bm25_stats(corpus, query, removal))


def scores_of(stats: Bm25Stats | None) -> dict[Any, float]:
    """The BM25 score of every document *stats*' query terms reach, summed in sorted term order."""
    if stats is None:
        return {}
    scores: dict[Any, float] = {}
    for term in sorted(stats.idf):
        for key in stats.corpus.postings[term]:
            contribution = stats.contribution(key, term)
            if contribution > 0:
                scores[key] = scores.get(key, 0.0) + contribution
    return scores


def term_contributions(stats: Bm25Stats | None, key: Any) -> dict[str, float]:
    """Each query term's BM25 contribution to the document *key*, the ones above 0, for the text record."""
    if stats is None:
        return {}
    contributions = {term: stats.contribution(key, term) for term in sorted(stats.idf)}
    return {term: value for term, value in contributions.items() if value > 0}


@dataclass
class FieldCorpus:
    """One BM25F corpus: per document key, one bag of terms per field of :attr:`fields`.

    A document's field missing from :meth:`add`'s mapping is an empty bag.
    """

    fields: tuple[str, ...]
    #: Field -> document key -> that field's bag length.
    lengths: dict[str, dict[Any, int]] = field(default_factory=dict)
    #: Document key -> field -> that field's term counts.
    frequencies: dict[Any, dict[str, Counter[str]]] = field(default_factory=dict)
    #: Term -> the document keys holding it in any field, in the order they were added.
    postings: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, key: Any, bags: Mapping[str, Sequence[str]]) -> None:
        unknown = sorted(set(bags) - set(self.fields))
        if unknown:
            raise ValueError(f"fields not of this corpus: {unknown}")
        counts = {name: Counter(bags.get(name, ())) for name in self.fields}
        self.frequencies[key] = counts
        for name in self.fields:
            self.lengths.setdefault(name, {})[key] = len(bags.get(name, ()))
        for term in sorted({term for bag in counts.values() for term in bag}):
            self.postings.setdefault(term, []).append(key)

    @property
    def size(self) -> int:
        return len(self.frequencies)

    def average(self, name: str) -> float:
        """The field's average bag length over every document; 0.0 for a corpus with no document."""
        lengths = self.lengths.get(name, {})
        return sum(lengths.values()) / len(lengths) if lengths else 0.0


def bm25f_scores(corpus: FieldCorpus, query: Sequence[str], boosts: Mapping[str, float]) -> dict[Any, float]:
    """BM25F of the distinct *query* terms against every document they reach.

    Per term and document, the pseudo-frequency is the sum over the fields of
    ``boost * tf / (1 - b + b * length / average)``; the term contributes
    ``idf * tf' * (k1 + 1) / (tf' + k1)``. A field whose average length is 0 holds
    no term, so it adds nothing; a field not in *boosts* weighs 0. A document no
    query term reaches is absent from the answer.
    """
    if corpus.size == 0:
        return {}
    averages = {name: corpus.average(name) for name in corpus.fields}
    scores: dict[Any, float] = {}
    for term in sorted(set(query)):
        keys = corpus.postings.get(term, ())
        if not keys:
            continue
        weight = idf(corpus.size, len(keys))
        for key in keys:
            frequency = 0.0
            for name in corpus.fields:
                boost = boosts.get(name, 0.0)
                count = corpus.frequencies[key][name].get(term, 0)
                if boost and count:
                    norm = 1 - BM25_B + BM25_B * corpus.lengths[name][key] / averages[name]
                    frequency += boost * count / norm
            if frequency > 0:
                scores[key] = scores.get(key, 0.0) + weight * frequency * (BM25_K1 + 1) / (frequency + BM25_K1)
    return scores
