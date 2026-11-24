"""The guide's page of checks: every rule the model declares.

It renders every contract rule -- its identifier, the contract's one-sentence ``summary`` and the
layers it reads -- grouped by family, from ``contract.load_contract()["rules"]`` (the contract
*document*, not the registry, which carries no rule).

**Nothing is dropped silently.** A rule whose ``family`` has no section here, whose ``appliesTo``
names nothing placeable on a layer, or whose ``severity`` this page has no sentence for raises
:class:`UnrenderableRule`.

**What this page does not express:** which field or kind a rule applies to; what the check computes;
and whether the summary is true of the code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import swcraft_tools.guide_reference.render as render
from swcraft_tools import contract

__all__ = [
    "RULES_PATH",
    "UnrenderableRule",
    "all_artifacts",
    "applies_to",
    "finding_rule_id",
    "rules_page",
]

#: The relative path of the page this module renders.
RULES_PATH = "reference/rules.md"

#: The ``appliesTo`` prefixes whose first segment is a layer name. The contract also admits a rule
#: identifier, which no shipped rule uses and this page has no place for.
_LAYER_BEARING_PREFIXES = frozenset({"layer", "kind", "field"})

#: The one ``severity`` value the contract admits: without the key a rule is an error, with this
#: value a warning. Spelled here because this module reads the contract, nothing of the validator.
_WARNING_SEVERITY = "warning"

#: One section per rule family, in reading order: one file, then the project, then a kind of value.
#: Each entry: the family token (never rendered), the heading, its explicit anchor, its one-line
#: purpose for the guide's corpus, and the section's prose.
_FAMILY_SECTIONS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "element-local",
        "Checks on a single element",
        "checks-on-a-single-element",
        "Why one file is refused on its own: a value out of range, a blank name, an empty list",
        "These read one file and need nothing else: a value outside its range, a name left blank, a list that "
        "has to hold at least one entry. They are reported as soon as the file is read, so they are the ones "
        "the editor and the assistant can answer immediately.",
    ),
    (
        "cross-element",
        "Checks between elements",
        "checks-between-elements",
        "Why elements disagree: a connector and its ports, a task and its runnables, a requirement link",
        "These compare one element with another — a connector against the ports it names, a task against the "
        "runnables it schedules, a component's `satisfies` link against the requirement it names — so they are "
        "answered once the whole project has been read, and a project that is valid file by file can still fail here.",
    ),
    (
        "schema-config",
        "Checks on a kind of value",
        "checks-on-a-kind-of-value",
        "The checks that hold for a kind of value wherever it appears, across layers",
        "These are not about one layer. They hold wherever the kind of value they describe appears, which is "
        "why one of them can list fields from several layers at once.",
    ),
)

_TABLE_HEADERS = ("Check", "What it requires", "Where it applies")


class UnrenderableRule(ValueError):
    """A rule this page cannot place, refused rather than dropped from the list.

    Raised for a ``family`` with no section, an ``appliesTo`` entry naming no declared layer, or a
    ``severity`` other than absent or ``warning``.
    """

    def __init__(self, rule_id: str, reason: str) -> None:
        self.rule_id = rule_id
        self.reason = reason
        super().__init__(f"{rule_id}: {reason}")


def _document(document: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The contract document to render from, resolved at call time."""
    return contract.load_contract() if document is None else document


def _layer_labels(document: Mapping[str, Any]) -> dict[str, str]:
    """Each layer's rendered heading, keyed by its name: its ``label``, or its ``name`` without one."""
    return {layer["name"]: layer.get("label") or layer["name"] for layer in document["layers"]}


def applies_to(rule: Mapping[str, Any], layers: Mapping[str, str]) -> tuple[str, ...]:
    """The layer names one rule reads, deduplicated, in the order its ``appliesTo`` declares them."""
    names: list[str] = []
    for entry in rule["appliesTo"]:
        prefix, _, rest = entry.partition(":")
        if prefix not in _LAYER_BEARING_PREFIXES or not rest:
            raise UnrenderableRule(rule["id"], f"{entry!r} names no layer, kind or field")
        name = rest.split(".")[0]
        if name not in layers:
            raise UnrenderableRule(rule["id"], f"{entry!r} names {name!r}, which is not a declared layer")
        if name not in names:
            names.append(name)
    return tuple(names)


#: The prefix a contract rule identifier carries and a finding's ``rule`` does not
#: (``rule:trace.link-needs-confirmation`` against ``trace.link-needs-confirmation``).
_RULE_PREFIX = "rule:"


def finding_rule_id(contract_id: str) -> str:
    """The string ``check-model`` puts in a finding's ``rule`` for the contract rule *contract_id*.

    The identifier without its ``rule:`` prefix; one without the prefix raises :class:`UnrenderableRule`.
    The prefix alone passes here, and its empty anchor is refused by :func:`render.anchored_code`.
    """
    if not contract_id.startswith(_RULE_PREFIX):
        raise UnrenderableRule(contract_id, f"the identifier does not start with {_RULE_PREFIX!r}")
    return contract_id[len(_RULE_PREFIX) :]


def _rule_row(rule: Mapping[str, Any], layers: Mapping[str, str]) -> tuple[str, ...]:
    """One row of a family's table, its identifier cell anchored by the finding's rule id.

    The editor's Problems panel links a finding to ``reference/rules/#<finding rule id>``. An anchor
    :func:`render.anchored_code` refuses raises :class:`UnrenderableRule`.
    """
    targets = ", ".join(render.link(layers[name], f"model/{name}.md") for name in applies_to(rule, layers))
    anchor = finding_rule_id(rule["id"])
    try:
        identifier = render.anchored_code(rule["id"], anchor)
    except ValueError as exc:
        raise UnrenderableRule(rule["id"], str(exc)) from exc
    return (identifier, rule["summary"], targets)


def _grouped(document: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Every rule, bucketed by family in declaration order; a family with no section fails the page."""
    known = {family for family, *_ in _FAMILY_SECTIONS}
    buckets: dict[str, list[Mapping[str, Any]]] = {family: [] for family, *_ in _FAMILY_SECTIONS}
    for rule in document["rules"]:
        family = rule["family"]
        if family not in known:
            raise UnrenderableRule(rule["id"], f"no section on this page renders the {family!r} family")
        buckets[family].append(rule)
    return buckets


def _is_warning(rule: Mapping[str, Any]) -> bool:
    """Whether *rule*'s findings are warnings, refusing a severity this page has no sentence for.

    An absent key is an error (the contract's default); any value other than ``warning``, null
    included, raises :class:`UnrenderableRule`.
    """
    if "severity" not in rule:
        return False
    severity = rule["severity"]
    if severity == _WARNING_SEVERITY:
        return True
    raise UnrenderableRule(rule["id"], f"severity {severity!r} is neither absent nor {_WARNING_SEVERITY!r}")


def _warning_paragraph(document: Mapping[str, Any]) -> str:
    """The paragraph stating which checks are warnings, rendered from the contract after the intro.

    One sentence per warning rule in declaration order, then the sentence that every other check is an
    error; with no warning rule, the closing sentence alone.
    """
    sentences = [
        f"{render.code(rule['id'])} is a warning: listed by `check-model` and `validate`, never a refusal; "
        "`--warnings-as-errors` makes it one."
        for rule in document["rules"]
        if _is_warning(rule)
    ]
    if sentences:
        sentences.append("Every other check is an error.")
    else:
        sentences.append("Every check below is an error.")
    return render.paragraph(" ".join(sentences))


def _family_blocks(
    title: str,
    anchor: str,
    purpose: str,
    prose: str,
    rules: Sequence[Mapping[str, Any]],
    layers: Mapping[str, str],
) -> tuple[str, ...]:
    """One family's heading, its prose and its table; nothing for a family with no rule."""
    if not rules:
        return ()
    return (
        render.heading(2, title, anchor=anchor, purpose=purpose),
        render.paragraph(prose),
        render.table(_TABLE_HEADERS, (_rule_row(rule, layers) for rule in rules)),
    )


def rules_page(*, document: Mapping[str, Any] | None = None) -> str:
    """The whole page: the intro, the paragraph of warning checks, then one section per family that has rules."""
    resolved = _document(document)
    layers = _layer_labels(resolved)
    buckets = _grouped(resolved)
    blocks = [
        render.GENERATED_COMMENT,
        render.heading(
            1,
            "The checks",
            purpose="What each check requires, where it applies, and which checks are only warnings",
        ),
        render.GENERATED_NOTICE,
        render.paragraph(
            "SWCraft checks a project every time it reads one. `swcraft check-model` runs every check below "
            "and writes nothing, which is what a build runs first; `swcraft validate` runs the same checks "
            "and, once every check that is an error has passed, generates the AUTOSAR file and checks that. "
            "So an error on this page never produces a file at all, while a file that fails the AUTOSAR "
            "check is left on disk to look at. The assistant runs them too, before it commits a change."
        ),
        _warning_paragraph(resolved),
        render.paragraph(
            "Each check has a name the product knows it by, one sentence saying what it requires, and the "
            "layers it reads. The sentence is the check's own: it is the same text SWCraft was built against."
        ),
    ]
    for family, title, anchor, purpose, prose in _FAMILY_SECTIONS:
        blocks.extend(_family_blocks(title, anchor, purpose, prose, buckets[family], layers))
    return render.document(*blocks)


def all_artifacts(*, document: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The one page this module renders, keyed by its path relative to ``docs/guide/``.

    Takes a contract *document*, not a registry: rule declarations reach no registry.
    """
    return {RULES_PATH: rules_page(document=document)}
