"""The address-keyed task-scope proposal.

Before a mutation turn runs, the host asks the LLM, in a separate tool-free structured call, to propose the scope
the turn needs; it is validated here and rendered for the user. Nothing here approves anything.

The envelope is closed: ``{summary: <non-empty string>, entries: [...]}``. Each entry is a ``$defs/scopeEntry``
(``{operation, address}``) parsed by :meth:`swcraft_tools.editing.address.ScopeEntry.from_json`, the parser the
engine's gates read an approved scope with. An empty ``entries`` list is the read-only form (every change is
refused), distinct from no scope at all, which is an unscoped run.

Every failure raises :class:`ScopeProposalError`; there is no permissive fallback scope. The layer, field and
operation vocabularies are derived from :mod:`swcraft_tools.editing.registry` and
:data:`swcraft_tools.editing.protocol.CHANGE_OPERATION_NAMES`, so a new declared field needs no registration here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swcraft_tools.editing import review
from swcraft_tools.editing.address import Address, AddressError, FieldStep, ScopeEntry
from swcraft_tools.editing.protocol import CHANGE_OPERATION_NAMES
from swcraft_tools.editing.registry import Registry, current, or_current
from swcraft_tools.model_registry import production_registry

if TYPE_CHECKING:
    from swcraft_agent.llm_client import LLMClient


class ScopeProposalError(RuntimeError):
    """The turn's scope could not be proposed, parsed or validated; terminal, with no fallback scope."""


@dataclass(frozen=True)
class ScopeProposal:
    """One turn's proposed authority: a sentence for the user and a set of scope entries for the engine."""

    summary: str
    entries: tuple[ScopeEntry, ...]

    @property
    def is_read_only(self) -> bool:
        """No entry approves any change — the nothing-approved form."""
        return not self.entries

    def to_json(self) -> list[dict[str, Any]]:
        """The ``SWCRAFT_APPROVED_SCOPE`` payload: a JSON array of ``$defs/scopeEntry``."""
        return [entry.to_json() for entry in self.entries]


def layer_field_vocabulary(registry: Registry | None = None) -> dict[str, tuple[str, ...]]:
    """Every field name addressable inside each layer, layer by layer.

    The union over the layer's element kinds, member-only ones included, since a path step may name a member's
    field. A vocabulary, not a resolution: which steps may follow which is ``editing.address.resolve``'s question.
    """
    return {
        layer.name: tuple(sorted({name for kind in layer.element_kinds for name in kind.field_names}))
        for layer in or_current(registry).layers
    }


def max_addressable_depth(registry: Registry | None = None) -> int:
    """:attr:`~swcraft_tools.editing.registry.Registry.max_address_depth`: the deepest address a proposal may carry.

    ``$defs/address.path`` has no maximum, and a very deep path renders one prompt line long enough to push the
    grants above it off the screen; it is refused rather than truncated, because a prompt showing part of a grant
    takes one ``y`` for all of it.
    """
    return or_current(registry).max_address_depth


def _check_vocabulary(entry: ScopeEntry, vocabulary: dict[str, tuple[str, ...]], max_depth: int) -> None:
    layer = entry.address.layer
    if layer not in vocabulary:
        raise ScopeProposalError(
            f"the proposed task scope names layer '{review.flatten(layer, _MESSAGE_LIMIT)}', which is not one "
            f"of {sorted(vocabulary)}"
        )
    if len(entry.address.path) > max_depth:
        raise ScopeProposalError(
            f"the proposed task scope carries an address {len(entry.address.path)} path steps deep; no address "
            f"into this model is deeper than {max_depth}, and a path this long is one prompt line long enough "
            "to push the grants above it off the screen"
        )
    declared = vocabulary[layer]
    for step in entry.address.path:
        if isinstance(step, FieldStep) and step.field not in declared:
            raise ScopeProposalError(
                f"the proposed task scope names field '{review.flatten(step.field, _MESSAGE_LIMIT)}', which "
                f"layer '{review.flatten(layer, _MESSAGE_LIMIT)}' does not declare; its fields are "
                f"{list(declared)}"
            )


def _vocabulary_text(vocabulary: dict[str, tuple[str, ...]]) -> str:
    """One ``layer: field, field, ...`` line per layer, in registry order."""
    return "\n".join(f"  {layer}: {', '.join(fields)}" for layer, fields in vocabulary.items())


def build_scope_json_schema(registry: Registry | None = None) -> dict[str, Any]:
    """The proposal schema, derived from *registry* and the protocol operations.

    Guidance, not enforcement: JSON Schema cannot express the per-layer field vocabulary or the ``path`` ->
    ``element`` dependency, so :func:`parse_proposal` remains the gate.
    """
    registry = or_current(registry)
    vocabulary = layer_field_vocabulary(registry)
    max_depth = max_addressable_depth(registry)
    field_names = sorted({name for names in vocabulary.values() for name in names})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "entries"],
        "description": (
            "The scope of authority the user's request needs, as the exact set of change "
            "operations and addresses it authorizes. Send an empty 'entries' array for a request "
            "that only reads the model: that authorizes no change at all. An address is "
            "{layer, element?, path?} and never a file path; omitting 'element' means every "
            "element of the layer and omitting 'path' means the whole element. A path step is "
            "{field: ...} for a declared field, {member: ...} for one member of a list addressed "
            "by its identity, or {key: ...} for one entry of a map. The fields each layer "
            f"declares are:\n{_vocabulary_text(vocabulary)}"
        ),
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "description": "One sentence naming the change, shown to the user for approval.",
            },
            "entries": {
                "type": "array",
                "description": ("One entry per operation-and-address pair the turn needs. Empty authorizes nothing."),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "address"],
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": list(CHANGE_OPERATION_NAMES),
                            "description": "The change operation this address is approved for. Matched exactly.",
                        },
                        "address": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["layer"],
                            "properties": {
                                "layer": {"type": "string", "enum": list(registry.layer_names)},
                                "element": {
                                    "type": "string",
                                    "description": (
                                        "The exact element identity. Omit the key entirely for every "
                                        "element of the layer — never send null."
                                    ),
                                },
                                "path": {
                                    "type": "array",
                                    "minItems": 1,
                                    # The bound `parse_proposal` refuses on.
                                    "maxItems": max_depth,
                                    "description": ("Omit the key entirely for the whole element — never send null."),
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "minProperties": 1,
                                        "maxProperties": 1,
                                        "properties": {
                                            "field": {"type": "string", "enum": field_names},
                                            "member": {
                                                "description": "The member's declared identity value.",
                                            },
                                            "key": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


#: The proposal schema the LLM is shown, built once at import from the shipped contract explicitly, so an import
#: inside a :func:`~swcraft_tools.model_registry.use_registry` block cannot freeze a perturbed contract's schema.
#: Not reached by a per-call ``registry``: :func:`propose_scope` validates against its own registry but prompts
#: with this; a contract-swapping caller asks :func:`build_scope_json_schema` for its own.
SCOPE_JSON_SCHEMA: dict[str, Any] = build_scope_json_schema(current(production_registry()))

PROPOSAL_INSTRUCTION = (
    "Classify the user's most recent request and propose the scope of authority it needs. "
    "Answer with one JSON object and nothing else. Do not call any tool and do not change the "
    "model: this call only proposes a scope, which the user must approve before anything runs. "
    "Propose the narrowest scope that can complete the request — every operation and address you "
    "list is authority the user is being asked to grant, and an operation is matched exactly, so "
    "an approved 'update' never authorizes a 'remove'. A request that only reads the model "
    "proposes an empty 'entries' array."
)

#: How many entries one proposal may carry: the user reads every one before answering.
MAX_SCOPE_ENTRIES = 100

#: The non-printable code points some renderer treats as a line break. Only words a refusal; the refusal covers
#: every non-printable code point, and this list must never become the rule.
_LINE_BREAKS = ("\n", "\r", "\v", "\f", "\x85", "\u2028", "\u2029")

#: How much of one LLM-authored fragment a refusal message quotes, escaped: refusals print to the terminal the
#: approval prompt uses.
_MESSAGE_LIMIT = 400


def propose_scope(
    llm: LLMClient,
    messages: list[dict],
    user_message: str,
    *,
    registry: Registry | None = None,
) -> ScopeProposal:
    """Ask *llm* for the scope the turn needs and validate it.

    The call carries the conversation history, the user's message and :data:`PROPOSAL_INSTRUCTION`, nothing
    else, through ``complete_structured()``, so the model cannot act while proposing.

    Args:
        messages: The conversation history so far (system prompt included).
        user_message: The user's turn, not yet appended to *messages*.
        registry: The model description to validate against (for tests).

    Raises:
        ScopeProposalError: The call failed, or its payload is not a valid scope.
    """
    registry = or_current(registry)
    proposal_messages = [
        *messages,
        {"role": "user", "content": user_message},
        {"role": "system", "content": PROPOSAL_INSTRUCTION},
    ]
    try:
        payload = llm.complete_structured(proposal_messages, SCOPE_JSON_SCHEMA)
    except Exception as exc:  # transport, SDK, response-shape — all terminal
        raise ScopeProposalError(f"the task scope could not be proposed: {exc}") from exc
    return parse_proposal(payload, registry=registry)


def parse_proposal(payload: Any, *, registry: Registry | None = None) -> ScopeProposal:
    """Validate a proposal payload into a :class:`ScopeProposal`.

    Raises:
        ScopeProposalError: The payload is not a valid scope. An
            :class:`~swcraft_tools.editing.address.AddressError` raised by the
            protocol's own parser is kept as the cause.
    """
    registry = or_current(registry)
    if not isinstance(payload, dict):
        raise ScopeProposalError(f"the proposed task scope must be an object, not {type(payload).__name__}")
    unknown = sorted(set(payload) - {"summary", "entries"})
    if unknown:
        # The key names are the model's own text and reach a terminal, so they are flattened.
        raise ScopeProposalError(
            f"the proposed task scope carries {[review.flatten(key, _MESSAGE_LIMIT) for key in unknown]}, "
            "which the envelope does not accept: a scope proposal carries a summary and an entries "
            "array and nothing else"
        )
    if "summary" not in payload or "entries" not in payload:
        raise ScopeProposalError("the proposed task scope must carry a summary and an entries array")
    summary = payload["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ScopeProposalError("the proposed task scope's summary must be a non-empty string")
    if not review.is_flat(summary):
        # `render_scope` escapes the summary anyway, but none of what `is_flat` rejects can be shown to a person
        # as itself. The message names the code point's class (a stray `\t` is no forgery) and reports it as
        # ``U+XXXX``, never raw.
        offender = next(character for character in summary if not review.is_flat(character))
        kind = (
            "a line break, which is the shape the forgery of the approval prompt takes"
            if offender in _LINE_BREAKS
            else "a control character, a separator or an invisible format character"
        )
        raise ScopeProposalError(
            f"the proposed task scope's summary carries U+{ord(offender):04X}: {kind}. A scope summary is one "
            "line of prose about the work, shown to a person who authorizes the turn by reading it, so it "
            "must carry printable text only"
        )
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list):
        raise ScopeProposalError(
            f"the proposed task scope's entries must be an array, not {type(raw_entries).__name__}; "
            "an empty array is the read-only form"
        )
    if len(raw_entries) > MAX_SCOPE_ENTRIES:
        # Refused, not truncated: a prompt showing part of the grant would take a `y` for all of it, and a
        # layer-wide entry says the same in one line.
        raise ScopeProposalError(
            f"the proposed task scope carries {len(raw_entries)} entries, more than the "
            f"{MAX_SCOPE_ENTRIES} a person can be asked to read before approving; propose a "
            "layer-wide entry or narrower work"
        )
    vocabulary = layer_field_vocabulary(registry)
    max_depth = max_addressable_depth(registry)
    entries: list[ScopeEntry] = []
    for raw in raw_entries:
        try:
            entry = ScopeEntry.from_json(raw)
        except AddressError as exc:
            # The parser's message quotes the model's own names: flattened where they enter the message.
            raise ScopeProposalError(
                f"the proposed task scope is not valid: {review.flatten(exc, _MESSAGE_LIMIT)}"
            ) from exc
        _check_vocabulary(entry, vocabulary, max_depth)
        entries.append(entry)
    return ScopeProposal(summary=summary, entries=tuple(entries))


_ANY_ELEMENT = "any element"

#: How much of the LLM's ``summary`` the approval prompt shows; the bound ``review._INTENT_LIMIT`` puts on a change
#: set's ``intent``, also model-authored prose.
_PROPOSAL_SUMMARY_LIMIT = 200

#: How long one rendered grant line may be: above any address :func:`parse_proposal` accepts, a last bound for a
#: caller that bypassed it; truncation announces itself.
_ENTRY_LINE_LIMIT = 800


def render_address(address: Address) -> str:
    """One address in :func:`swcraft_tools.editing.review.render_address`'s readable form.

    The same spelling as the delta review, so the user can compare the two prompts; a layer-wide wildcard is
    annotated with :data:`_ANY_ELEMENT` rather than left as a bare layer name that reads like a truncation.
    """
    rendered = review.render_address(address)
    if address.element is None:
        return f"{rendered} ({_ANY_ELEMENT})"
    return rendered


def render_scope(proposal: ScopeProposal) -> str:
    """Render *proposal* as the stable, readable block the approval prompt shows.

    Equal proposals render to equal strings; entries keep the proposal's order, each with its operation (an
    approved ``update`` never authorizes a ``remove``). A display, not a hashing protocol.

    The LLM-written ``summary`` goes through :func:`swcraft_tools.editing.review.flatten`: verbatim, a summary
    with newlines could print its own fake entries and question above the real block. The delta review's
    escaping does not reach this prompt. Each entry line is capped at :data:`_ENTRY_LINE_LIMIT`, with announced
    truncation; the entry count and address depth are refused earlier by :func:`parse_proposal`.
    """
    lines = [
        "Proposed task scope",
        f"  summary: {review.flatten(proposal.summary, _PROPOSAL_SUMMARY_LIMIT)}",
    ]
    if proposal.is_read_only:
        lines.append("  entries: none - no change to the model is authorized")
        return "\n".join(lines)
    lines.append("  entries:")
    lines.extend(
        review.flatten(f"  - {entry.operation} {render_address(entry.address)}", _ENTRY_LINE_LIMIT)
        for entry in proposal.entries
    )
    return "\n".join(lines)
