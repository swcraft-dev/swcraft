"""The trace search's candidates for one requirement: ``find`` with ``for_requirement``.

The ranking of :mod:`swcraft_tools.trace_search` for **one** requirement, with
the default :class:`~swcraft_tools.trace_search.TraceConfig`, over the request's
snapshot. Each place and row comes in the ranking's order with the search's own
fields plus the protocol ``address`` whose ``satisfies`` list would carry the
link; the requirement's existing links follow, each with its address.

**The refusals**: :class:`RequirementNotFound`, :class:`RequirementIsStub` (a stub
has no text to rank), and :class:`CandidatesRefused` (an ambiguous identity, an
unreadable decisions file). No message names where the project is stored.

**What this does not reach.** A non-default configuration (the CLI's
``--config``); the experiment's passes, labelling and self-measurement; the
``rejectedEarlier`` count and ``result.json`` fingerprints; the ``proposals.json``
guard. A file changed between the ambiguity check's **second read** of the model
root and the first is not caught by the baseline token. Existing links are bounded
by :data:`~swcraft_tools.editing.protocol.FIND_EXISTING_LINKS_MAX` (past it the
answer counts the rest). Two malformed spellings at one site (``R@x`` beside a
bare ``R``) are two links here and one row there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# A module, read at the call: `HINT_PAGE_SIZE` is the published page size every hint offers.
from swcraft_tools.editing import read as read_mod
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.protocol import (
    FIND_EXISTING_LINKS_MAX,
    FindCandidatesResponse,
    ReadRequest,
    WhereClause,
)
from swcraft_tools.model_state import single_read
from swcraft_tools.project import SwcraftProject
from swcraft_tools.trace import site_kind_layer
from swcraft_tools.trace_records import TraceRecordsError, read_decisions, rejected_pairs
from swcraft_tools.trace_search import (
    AmbiguousIdentityError,
    ExistingLink,
    Place,
    Ranking,
    Row,
    TargetAddress,
    TraceConfig,
    build_index,
    project_name_words,
    rank,
    refuse_ambiguous_identities,
    score_figure,
)
from swcraft_tools.validator import check_cross_layer_consistency

__all__ = [
    "CandidatesRefused",
    "RequirementIsStub",
    "RequirementNotFound",
    "candidates_for",
    "links_read",
    "project_ranking",
    "protocol_address",
]

#: A component member's kind -> the component's collection holding it.
_OWNED_FIELDS: Mapping[str, str] = {"port": "ports", "runnable": "runnables"}


class RequirementNotFound(LookupError):
    """No requirement of the model carries the id; *address* is the one the request names."""

    def __init__(self, address: Address) -> None:
        super().__init__(
            f"the requirements layer holds no requirement '{address.element}': the trace search ranks candidates "
            "for a declared requirement"
        )
        self.address = address


class RequirementIsStub(ValueError):
    """The requirement is a stub: it has no text, and the trace search never ranks one."""

    def __init__(self, address: Address) -> None:
        super().__init__(
            f"requirement '{address.element}' is a stub: it carries no text to rank candidates by, and the trace "
            "search never proposes a link for one"
        )
        self.address = address


class CandidatesRefused(ValueError):
    """The search is refused for the model as a whole: an ambiguous identity, or the decisions file unreadable."""


def protocol_address(target: TargetAddress) -> Address:
    """The protocol address of the element or member *target* names, where its ``satisfies`` list is."""
    layer = site_kind_layer(target.kind)
    if target.owner is not None:
        return Address(
            layer=layer,
            element=target.owner,
            path=(FieldStep(_OWNED_FIELDS[target.kind]), MemberStep(target.name)),
        )
    return Address(layer=layer, element=target.name)


def _row(row: Row) -> dict[str, Any]:
    return {
        "address": protocol_address(row.address).to_json(),
        "target": row.address.to_json(),
        "score": score_figure(row.score),
        "band": row.band,
        "evidence": [dict(record) for record in row.evidence],
    }


def _place(place: Place) -> dict[str, Any]:
    return {
        "group": place.group.to_json(),
        "score": score_figure(place.score),
        "evidence": [dict(record) for record in place.evidence],
        "rows": [_row(row) for row in place.rows],
    }


def _link(link: ExistingLink) -> dict[str, Any]:
    return {"address": protocol_address(link.target).to_json(), **link.to_json()}


def links_read(requirement: str) -> ReadRequest:
    """The ``read`` that pages every link naming *requirement*, sent back verbatim as ``existing_links_read``.

    An ``index`` read of the requirements layer, ``where`` on the ``id``, asking
    ``realised_by`` (one row per link site). It starts at the first link: the read is
    the whole set and the answer's listed links its preview, except that two malformed
    spellings at one site are two links in the answer and one row in the read.
    """
    return ReadRequest(
        address=Address(layer="requirements"),
        form="index",
        where=(WhereClause(field="id", equals=requirement),),
        limit=read_mod.HINT_PAGE_SIZE,
        derived=("realised_by",),
    )


def project_ranking(
    ranking: Ranking, links: tuple[ExistingLink, ...], *, baseline: str, for_requirement: str
) -> FindCandidatesResponse:
    """The answer: *ranking*'s places in order and the requirement's *links*, as the wire carries them.

    Links past :data:`~swcraft_tools.editing.protocol.FIND_EXISTING_LINKS_MAX` are
    counted in ``existing_links_more`` and reachable through ``existing_links_read``.
    """
    places = tuple(_place(place) for place in ranking.places)
    left_out = len(links) - FIND_EXISTING_LINKS_MAX
    return FindCandidatesResponse(
        baseline=baseline,
        for_requirement=for_requirement,
        neighbours=ranking.neighbours,
        places=places if places else "none",
        existing_links=tuple(_link(link) for link in links[:FIND_EXISTING_LINKS_MAX]),
        existing_links_more=left_out if left_out > 0 else None,
        existing_links_read=links_read(for_requirement) if left_out > 0 else None,
    )


_RECORD_POINTER = re.compile(r"/decisions/(\d+)(?:/|$)")


def _decisions_fault(unreadable: TraceRecordsError) -> str:
    """Where the decisions file is refused, in words that quote nothing the file holds.

    A top-level refusal is the reader's own sentence and is forwarded. A refusal
    inside the file is named by its record alone: the reader's sentence and pointer
    quote the file's values and keys, which may hold a storage location the protocol
    never names. ``swcraft experiment trace`` states the fault in full.
    """
    if not unreadable.pointer:
        return f"top level: {unreadable.sentence}"
    record = _RECORD_POINTER.match(unreadable.pointer)
    where = f"record decisions/{record.group(1)}" if record else "a key of its top level"
    return f"{where} is not one the format holds; `swcraft experiment trace` states the fault in full"


def candidates_for(
    project: SwcraftProject, snapshot: Any, baseline: str, identifier: str, candidates: int
) -> FindCandidatesResponse:
    """The trace search's ranking for requirement *identifier*, built as ``experiment trace`` builds it.

    *snapshot* is the request's own and *baseline* the token taken before it was
    loaded.

    Raises:
        RequirementNotFound: no requirement carries *identifier*.
        RequirementIsStub: the requirement is a stub.
        CandidatesRefused: an ambiguous identity or an unreadable decisions file.
    """
    state = snapshot.state
    address = Address(layer="requirements", element=identifier)
    declared = [requirement for requirement in state["requirements"] if requirement.id == identifier]
    if not declared:
        raise RequirementNotFound(address)
    ranked = next((requirement for requirement in declared if not requirement.stub), None)
    if ranked is None:
        raise RequirementIsStub(address)
    try:
        with single_read():
            consistency = check_cross_layer_consistency(
                str(project.resolved_model_root), project.resolved_arxml_version
            )
        refuse_ambiguous_identities(consistency)
    except AmbiguousIdentityError as ambiguous:
        raise CandidatesRefused(str(ambiguous)) from None
    try:
        rejected = rejected_pairs(read_decisions(project))
    except TraceRecordsError as unreadable:
        raise CandidatesRefused(
            f"the search is refused: the project's trace decisions file cannot be read "
            f"({_decisions_fault(unreadable)}); it is repaired outside the protocol"
        ) from None
    index = build_index(state, TraceConfig(), rejected, project_name_words(project))
    ranking = rank(index, ranked, candidates)
    return project_ranking(
        ranking, tuple(index.links.get(identifier, ())), baseline=baseline, for_requirement=identifier
    )
