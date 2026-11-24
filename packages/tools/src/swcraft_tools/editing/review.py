"""The two review renderings: what a human is shown before a change set commits.

Each function takes a :class:`~swcraft_tools.editing.engine.ChangeSetProposal` and
returns text, with no I/O. :func:`render_delta_review` answers ``approved``;
:func:`render_safety_review` answers ``safety_approved``, only over a non-empty
verdict.

**Client text cannot forge the message**: every non-printable code point of client
text is escaped and every component is bounded.

**Every entry is shown**: payload halves share one **total** budget max-min fairly,
and every bound that bites says what it dropped, with the count, and where it can
be read, or that it cannot be.

**The warning.** A file written whole loses comments, undeclared keys and layout,
so a ``WARNING:`` block stands above the delta; a host highlights it from
:attr:`DeltaReview.warning_lines`, never by matching client text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from swcraft_tools.authorization import SafetyChangeKind
from swcraft_tools.editing.address import Address, FieldStep, KeyStep, MemberStep
from swcraft_tools.editing.delta import elided_fields
from swcraft_tools.editing.engine import ChangeSetProposal
from swcraft_tools.editing.protocol import ABSENT, DeltaEntry
from swcraft_tools.editing.registry import current

__all__ = [
    "DeltaReview",
    "delta_review",
    "flatten",
    "is_flat",
    "render_address",
    "render_delta_review",
    "render_safety_review",
]

#: The key one payload half is allocated under: ``(entry position, half name)``.
K = TypeVar("K")

#: Readable spellings for three non-printable code points (JSON's own short
#: escapes). A convenience only: it never decides **whether** a code point is
#: escaped, so no test may derive an adversary set from it.
_NAMED_ESCAPES: dict[str, str] = {"\t": "\\t", "\n": "\\n", "\r": "\\r"}

#: How long the client's stated intent may be: a sentence fits, a screenful of
#: forged review block does not.
_INTENT_LIMIT = 200

#: How long one element identity may be inside a rendered address. Longer than
#: any identity the storage layer accepts, so a legitimate one is never elided.
_IDENTITY_LIMIT = 200

#: How long one ``summary`` line may be. ``delta.py`` bounds each value, not the
#: number of components it concatenates.
_SUMMARY_LIMIT = 400

#: How much of one collection step's JSON is shown inside a rendered address, so
#: that a *path* cannot flood its entry's line.
_STEP_VALUE_LIMIT = 120

# The number of address steps rendered is a contract fact, resolved per call by
# :func:`_step_limit`, not a constant.

#: How long one rendered entry headline may be, all its components together, so
#: the message is bounded by the entry count times a constant.
_HEADLINE_LIMIT = 1000

#: How many characters of ``before``/``after`` payload one prompt shows in
#: **total**, shared out max-min fairly by :func:`_shares`. A payload that does not
#: fit is elided *with its size*.
_VALUE_BUDGET = 20_000

#: What a whole write loses, one statement of it, below the warning's first line.
#: ``file(s)`` is literal, in the first line and the question too.
_REWRITTEN_SENTENCE = (
    "The file(s) below are rewritten whole, in SWCraft's form, because they cannot be edited line by line.",
    "The model's values change only as the delta says; the layout of the rest of each file moves,",
    "and its comments and any keys SWCraft does not declare are LOST.",
)

#: The lead for removed elements beside a created one: their file is deleted, not
#: rewritten, so the sentence above would say something false of it.
_REMOVED_SENTENCE = (
    "The element(s) below are removed beside a created element and are no rename: the new file is written",
    "whole, and the removed file's comments, layout and any keys SWCraft does not declare are not carried.",
)

#: How many of a rewritten file's undeclared keys its warning line names before
#: ``and N more``: a key is model text, bounded like every other piece of it.
_DROPPED_KEYS_SHOWN = 5

#: There is deliberately **no per-half ceiling** and **no minimum share**: see
#: :func:`_shares`. "Never elide a ``create``" is not the rule either: an
#: unconditional exemption would let a megabyte ``create`` push a ``remove`` out
#: of view.


def _escape_char(char: str) -> str:
    """One code point, or its escaped spelling when it is not printable.

    The spelling is JSON's (``\\uXXXX``, a surrogate pair above the BMP).
    """
    if char.isprintable():
        return char
    named = _NAMED_ESCAPES.get(char)
    if named is not None:
        return named
    code = ord(char)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def _escape(text: str) -> str:
    """*text* with every non-printable code point replaced by its spelling."""
    if text.isprintable():
        return text
    return "".join(_escape_char(char) for char in text)


def flatten(text: object, limit: int) -> str:
    """*text* as one bounded line carrying no non-printable code point.

    Public so a host composing its own prompt from LLM text (the agent's task
    scope) neutralizes it by the same rule rather than a second spelling of it.

    Escapes before truncating: the bound is over what is *shown*. A bound that
    bites says what it dropped, and the marker counts against *limit* (which must
    hold the marker, about 20 characters), so a caller can lay a prompt out from
    the constant it passed.
    """
    flattened = _escape(str(text))
    if len(flattened) <= limit:
        return flattened
    # The marker's own length changes the count it states. Round 2 re-sizes it for
    # the marker's characters (the one round that can grow the digit count); round
    # 3 is the fixed point.
    dropped = len(flattened) - limit
    for _ in range(3):
        marker = f"… (+{dropped} characters)"
        kept = max(0, limit - len(marker))
        dropped = len(flattened) - kept
    return flattened[:kept] + marker


def is_flat(text: str) -> bool:
    """Whether *text* would survive :func:`flatten`'s escaping unchanged.

    For a caller that would rather **refuse** a value than show it escaped (a
    scope proposal, which is fail-closed) without restating the rule.
    """
    return _escape(text) == text


def _payload(value: object) -> str:
    """One delta value as the JSON a client would send back: escaped, unbounded.

    ``default=str`` degrades an unexpected type instead of raising inside the
    review boundary.
    """
    return _escape(json.dumps(value, ensure_ascii=False, default=str))


#: Where one elided half can be recovered from. Only a ``before``: ``read``
#: answers the real model and nothing is applied while the prompt is shown, so
#: an ``after`` is not there, and a step is part of the address itself. Where
#: there is no recovery path, the prompt names the only move that works.
_RECOVERY: dict[str, str] = {
    "before": "read the full value with 'read'",
    "after": (
        "not retrievable — nothing has been applied, so 'read' answers the model as it is now. "
        "Answer no and have the client re-submit a smaller change set to see it"
    ),
    "step": (
        "not retrievable — it is part of the address itself. Answer no and have the client "
        "re-submit a smaller change set to see it"
    ),
}


def _elide(rendered: str, budget: int, *, half: str) -> str:
    """*rendered*, or its first *budget* characters with the elision stated.

    The size is stated because a shortened value that looked whole is the same
    defect as a forged one. *half* (a key of :data:`_RECOVERY`) decides where the
    sentence says the text can be read. The audit records addresses only, so the
    elided text is in no other artifact a human reads.
    """
    if len(rendered) <= budget:
        return rendered
    elided = len(rendered) - budget
    return f"{rendered[:budget]}… ({elided} characters elided; {_RECOVERY[half]})"


def _shares(lengths: dict[K, int]) -> dict[K, int]:
    """:data:`_VALUE_BUDGET` shared out over *lengths*, max-min fairly.

    Every half is offered an equal share of what is left; the ones that need less
    take it and return the rest to the pool. A delta whose payloads fit the budget
    is rendered whole, and one that does not elides only the halves bigger than an
    equal share, never the small ones next to a flood.

    There is no floor: the equal share already is one (a constant floor could
    never exceed it, since ``remaining >= floor * len(pending)`` is invariant).
    There is **no per-half ceiling**: every half reaching the terminal branch is
    oversized by construction, so a ceiling could only elide content with budget
    left unspent (a lone 20 000-character ``create`` cut at 4 000).
    """
    remaining = _VALUE_BUDGET
    pending = dict(lengths)
    shares: dict[K, int] = {}
    while pending:
        offer = remaining // len(pending)
        fits = {key: length for key, length in pending.items() if length <= offer}
        if not fits:
            # `len(pending) * offer <= remaining` by the definition of the offer.
            shares.update(dict.fromkeys(pending, offer))
            break
        for key, length in fits.items():
            shares[key] = length
            remaining -= length
            del pending[key]
    return shares


def _step_limit() -> int:
    """The registry's own deepest addressable path, asked per render.

    Never bites a legal address; it is the renderer's half of the depth refusal
    ``scope_proposal.parse_proposal`` makes, bounding a headline whatever a caller
    hands over. Asked per render, not bound at import, so a contract that
    addresses deeper does not have its legal addresses elided.
    """
    return current().max_address_depth


def render_address(address: Address) -> str:
    """One address in the form a person reads: ``layer/element`` plus its steps.

    A field step is ``.name``; the collection steps carry their form (``[member
    ...]``, ``[key ...]``) so a member identity cannot be mistaken for a map key.
    The element and every step are client text, flattened and bounded, and at most
    :func:`_step_limit` steps are shown, with the rest counted.
    """
    step_limit = _step_limit()
    rendered = (
        address.layer if address.element is None else f"{address.layer}/{flatten(address.element, _IDENTITY_LIMIT)}"
    )
    for step in address.path[:step_limit]:
        if isinstance(step, FieldStep):
            rendered += f".{flatten(step.field, _STEP_VALUE_LIMIT)}"
        elif isinstance(step, KeyStep):
            rendered += f"[key {_elide(_payload(step.key), _STEP_VALUE_LIMIT, half='step')}]"
        elif isinstance(step, MemberStep):
            rendered += f"[member {_elide(_payload(step.member), _STEP_VALUE_LIMIT, half='step')}]"
        else:  # pragma: no cover - `PathStep` is a closed union of the three
            rendered += f"[{step}]"
    if len(address.path) > step_limit:
        rendered += f"… (+{len(address.path) - step_limit} steps not shown)"
    return rendered


def _render_entry(position: int, entry: DeltaEntry, halves: dict[str, str]) -> list[str]:
    """One delta entry: its operation, its address, and the halves it carries.

    *halves* holds the rendered ``before``/``after`` text, and **presence is the
    distinction**: an absent half produces no line, a ``None`` one ``null``. A
    ``create`` has no ``before``, and ``before: null`` would say otherwise.
    """
    headline = f"  {position}. {entry.operation} {render_address(entry.address)}"
    if entry.origin == "server":
        headline += " [added by the server]"
    elif entry.origin == "client_and_server":
        headline += " [the server added to this]"
    if entry.summary:
        headline += f" — {flatten(entry.summary, _SUMMARY_LIMIT)}"
    lines = [flatten(headline, _HEADLINE_LIMIT)]
    for half in ("before", "after"):
        if half not in halves:
            continue
        if half == "before" and entry.before_form is not None:
            lines.append(f"       before ({entry.before_form}): {halves[half]}")
            lines.append(f"       {_elision_sentence(entry)}")
        else:
            lines.append(f"       {half}: {halves[half]}")
    return lines


def _elision_sentence(entry: DeltaEntry) -> str:
    """What a summarised ``before`` left out, with the counts, and where the whole value is read.

    Each counted collection is named with its count (an empty one dropped nothing
    and is left out), then how many other fields the summary does not show.
    """
    counted, hidden = elided_fields(entry, registry=current())
    parts = [f"{count} {name}" for name, count in counted.items() if count]
    if hidden:
        parts.append(f"{hidden} other field{'' if hidden == 1 else 's'}")
    dropped = ", ".join(parts) if parts else "only empty collections"
    return flatten(f"the whole value is elided: {dropped}; {_RECOVERY['before']}", _SUMMARY_LIMIT)


def _rewritten_warning(proposal: ChangeSetProposal) -> list[str]:
    """The warning block: what the delta cannot show, with every element it names.

    Every element is listed: the one boolean approves them all. Every component
    is flattened (a layer and a reason too: this is handed a proposal, not a
    promise), so each returned item is one line, which :func:`delta_review`'s span
    counts on. A rename names its old identity, ``(renamed from X)``; a file with
    undeclared keys names the ones dropped, at most :data:`_DROPPED_KEYS_SHOWN`.
    Removed elements follow under :data:`_REMOVED_SENTENCE`.
    """
    lines = [f"WARNING: more than the delta will change in {len(proposal.rewritten)} file(s)."]
    rewritten = [file for file in proposal.rewritten if not file.removed]
    removed = [file for file in proposal.rewritten if file.removed]
    for lead, files in ((_REWRITTEN_SENTENCE, rewritten), (_REMOVED_SENTENCE, removed)):
        if not files:
            continue
        lines.extend(lead)
        for file in files:
            name = f"{flatten(file.layer, _IDENTITY_LIMIT)}/{flatten(file.identity, _IDENTITY_LIMIT)}"
            if file.renamed_from is not None:
                name += f" (renamed from {flatten(file.renamed_from, _IDENTITY_LIMIT)})"
            lines.append(f"  - {name}: {flatten(file.reason, _SUMMARY_LIMIT)}{_dropped_keys(file.dropped_keys)}")
    return lines


def _dropped_keys(keys: tuple[str, ...]) -> str:
    """``; drops the undeclared key(s) 'a', 'b'`` for a warning line, or nothing when *keys* is empty."""
    if not keys:
        return ""
    shown = ", ".join(f"'{flatten(key, _IDENTITY_LIMIT)}'" for key in keys[:_DROPPED_KEYS_SHOWN])
    more = len(keys) - _DROPPED_KEYS_SHOWN
    return f"; drops the undeclared key(s) {shown}" + (f" and {more} more" if more > 0 else "")


@dataclass(frozen=True)
class DeltaReview:
    """The first question, with the lines of its warning.

    ``warning_lines`` is ``(start, end)``, zero-based and half-open, over
    ``text.split("\\n")``, or ``None`` when the message has no warning. It is
    presentation only: the answer is bound to ``text`` and to nothing else.
    """

    text: str
    warning_lines: tuple[int, int] | None


def render_delta_review(proposal: ChangeSetProposal) -> str:
    """The first question's text: :func:`delta_review` for a caller that highlights nothing."""
    return delta_review(proposal).text


def delta_review(proposal: ChangeSetProposal) -> DeltaReview:
    """The first question: the intent, the exact delta, and "commit this?".

    The **delta**, never the requested operations: a ``remove`` whose referrers
    are cleared produces cascade entries nobody asked for by name, and the human
    decides over what would land. A file written whole adds a warning above the
    delta, and the question names the rewrite.

    Returns:
        The elicitation message, ending in the question the boolean answers, and
        the warning's lines, taken as they are composed.
    """
    intent = flatten(proposal.intent, _INTENT_LIMIT) if proposal.intent else "(not stated)"
    entries = list(proposal.delta)
    lines = [
        "Review this change set before it is committed.",
        "",
        f"Intent: {intent}",
        "",
    ]
    warning_lines: tuple[int, int] | None = None
    if proposal.rewritten:
        start = len(lines)
        lines.extend(_rewritten_warning(proposal))
        warning_lines = (start, len(lines))
        lines.append("")
    lines.append(f"The exact delta ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}):")
    if not entries:
        lines.append("  (none)")
    # Rendered in full first: a share depends on every other half. Every entry,
    # because the boolean commits every entry: there must be no `entries[:limit]`.
    # Nor can a long change set be split: the staged result is validated whole.
    payloads = {
        (position, half): _payload(getattr(entry, half))
        for position, entry in enumerate(entries)
        for half in ("before", "after")
        if getattr(entry, half) is not ABSENT
    }
    budgets = _shares({key: len(text) for key, text in payloads.items()})
    # Grouped once: the entry count is unbounded, and a rescan per entry is quadratic.
    by_position: dict[int, dict[str, str]] = {}
    for (index, half), text in payloads.items():
        by_position.setdefault(index, {})[half] = _elide(text, budgets[(index, half)], half=half)
    for position, entry in enumerate(entries):
        lines.extend(_render_entry(position + 1, entry, by_position.get(position, {})))
    question = "Commit exactly this delta?"
    whole = sum(1 for file in proposal.rewritten if not file.removed)
    if whole:
        question = f"Commit exactly this delta and rewrite {whole} file(s) whole?"
    lines.extend(["", f"Nothing has been applied yet and the model is byte-identical. {question}"])
    return DeltaReview("\n".join(lines), warning_lines)


def render_safety_review(proposal: ChangeSetProposal) -> str:
    """The second question: only the safety changes, and "approve those too?".

    The opening sentence follows the kinds present: a
    :data:`~swcraft_tools.authorization.SafetyChangeKind.GRANT_PARTITION_TRUST`
    widens a permission rather than weakening a check, so grants alone open
    "widens a permission", a grant beside another kind opens both, and every
    other set "weakens a check".

    Returns:
        The elicitation message, ending in the question the boolean answers.

    Raises:
        ValueError: the verdict is empty, so there is no second question. A host
            that asked one anyway would show an empty prompt and read the answer
            as an approval of nothing.
    """
    changes = proposal.safety.changes
    if not changes:
        raise ValueError(
            "render_safety_review was called over a change set with no safety change: "
            "an empty SafetyVerdict means there is no second question to ask"
        )
    if all(change.kind is SafetyChangeKind.GRANT_PARTITION_TRUST for change in changes):
        opening, subject = "This change set widens a permission.", "the widening"
    elif any(change.kind is SafetyChangeKind.GRANT_PARTITION_TRUST for change in changes):
        opening = "This change set weakens a check and widens a permission."
        subject = "the weakening and the widening"
    else:
        opening, subject = "This change set weakens a check.", "the weakening"
    lines = [
        opening,
        "",
        "The safety guard is always on and is orthogonal to the delta you just reviewed:",
        f"approving the delta is not approving {subject} below.",
        "",
    ]
    # Every weakening, flattened (`describe()` interpolates client identities): one
    # boolean approves all of them.
    lines.extend(f"  - {flatten(change.describe(), _SUMMARY_LIMIT)}" for change in changes)
    # The guard's notes (an ambiguous rename pairing; a rated consumer port moved
    # onto a stub, which says the interface became uncheckable and reaches no
    # unconnected port and no connection whose provider end or consumer changed)
    # go BELOW the list, never instead of any of it, flattened and bounded: the
    # weakenings are what the boolean answers for, and a note that displaced one
    # would buy an approval.
    if proposal.safety.notes:
        lines.append("")
        lines.extend(f"  note: {flatten(note, _SUMMARY_LIMIT)}" for note in proposal.safety.notes)
    lines.extend(["", f"Approve {subject} as well?"])
    return "\n".join(lines)
