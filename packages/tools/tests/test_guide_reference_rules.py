"""The guide's page of checks: the content oracle, the refusal path, the seam it does not have.

``swcraft_tools.guide_reference.rules`` belongs to the ``guide-reference`` consumer. Each check is
derived from the contract, never transcribed:

* **the content oracle** — every declared rule is on the page with its ``summary`` verbatim;
* **the refusal path** — a family with no section and an ``appliesTo`` entry naming no layer or an
  undeclared one each raise :class:`~swcraft_tools.guide_reference.rules.UnrenderableRule`, shown
  firing against perturbed copies of the shipped contract;
* **the seam it does not have** — rule declarations reach no registry, so a ``use_registry`` block
  moves the layer pages and leaves this page byte-identical;
* **the warning paragraph** — a rule's class is stated once, after the intro, never as a second
  table row: one sentence per ``severity: warning`` rule in contract order, then the sentence that
  every other check is an error; a severity outside the vocabulary is refused.

**What this module does not reach.** The comparison with the committed file (the drift gate's);
whether a ``summary`` is true of its check (the validator suites'); and whether a link resolves
(``mkdocs build --strict``) -- only that its target is a page this package produces. The one
rendering done here is the anchors' oracle: the page through python-markdown with ``mkdocs.yml``'s
extension list, read for each rule row's ``id``.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any

import markdown
import pytest
from guide_fixtures import GUIDE_DIR, MKDOCS_YML, help_links_constant
from model_contract_fixtures import load_binding, load_contract, merged_binding, merged_contract
from swcraft_tools import guide_reference, validator
from swcraft_tools.guide_reference import model, render, rules
from swcraft_tools.model_registry import build_model_registry, use_registry
from swcraft_tools.validator.hardware import _RESOURCE_FINDING_RULES
from swcraft_tools.yaml_load import load_yaml_document


def _page() -> str:
    return rules.rules_page()


#: A rendered row's identifier cell: the contract id in a code span, then the
#: ``attr_list`` anchor :func:`rules.finding_rule_id` gives it.
_ROW_IDENTIFIER = r"^\| `(rule:[^`]+)`\{ #[A-Za-z0-9._-]+ \} \|"


def _row_cell(contract_id: str) -> str:
    """The identifier cell a rule's row opens with, its anchor included."""
    return f"| `{contract_id}`{{ #{rules.finding_rule_id(contract_id)} }} |"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _contract_rules() -> list[dict[str, Any]]:
    return list(load_contract()["rules"])


def _perturbed(mutate) -> dict[str, Any]:
    """A deep copy of the shipped contract with its first rule mutated in place, so no later load sees it."""
    document = copy.deepcopy(load_contract())
    mutate(document["rules"][0])
    return document


# The content oracle: every rule, from the contract


def test_every_declared_rule_appears_on_the_page() -> None:
    """The completeness the declaration does not claim through a granularity, claimed here instead.

    Matched on the identifier's code span, so a mention inside another summary cannot answer for a row.
    """
    page = _page()
    missing = [rule["id"] for rule in _contract_rules() if _row_cell(rule["id"]) not in page]
    assert missing == [], f"rules the guide's page of checks does not render as a row: {missing}"


def test_every_rule_summary_is_carried_verbatim() -> None:
    """The contract's sentence is the page's sentence, compared on whitespace-collapsed text."""
    flat = _flat(_page())
    missing = [rule["id"] for rule in _contract_rules() if _flat(rule["summary"]) not in flat]
    assert missing == [], f"rules whose summary the page does not carry verbatim: {missing}"


def test_no_rule_is_rendered_twice() -> None:
    """A rule in two sections would double-count a check the page presents as the complete list."""
    page = _page()
    repeated = [rule["id"] for rule in _contract_rules() if page.count(_row_cell(rule["id"])) != 1]
    assert repeated == [], f"rules rendered other than exactly once: {repeated}"


def test_the_rows_of_each_family_are_in_the_contracts_declaration_order() -> None:
    """Nothing sorts: per family, the page's row order is the contract's declaration order."""
    page = _page()
    rendered = re.findall(_ROW_IDENTIFIER, page, flags=re.MULTILINE)
    families = _declared_families()
    assert families, "the contract declares no rule family, so this comparison reads nothing"
    for family in families:
        declared = [rule["id"] for rule in _contract_rules() if rule["family"] == family]
        assert [entry for entry in rendered if entry in set(declared)] == declared, (
            f"the {family} section does not render its rules in the contract's declared order"
        )


def _declared_families() -> list[str]:
    """The rule families the contract declares, in first-declaration order.

    Read off the contract, not `rules._FAMILY_SECTIONS`: a test of the module's table is a test of the table.
    """
    seen: list[str] = []
    for rule in _contract_rules():
        if rule["family"] not in seen:
            seen.append(rule["family"])
    return seen


def _section_of_each_rule() -> dict[str, str]:
    """Which level-2 section each rendered rule row sits under, read off the page."""
    section = ""
    placement: dict[str, str] = {}
    for line in _page().splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif (match := re.match(_ROW_IDENTIFIER, line)) is not None:
            placement[match.group(1)] = section
    return placement


def test_every_declared_family_has_a_section_of_its_own() -> None:
    """Each family's rules sit together, under one heading, and no two families share one.

    The titles are the module's to choose, so the property is asserted, not the words.
    """
    placement = _section_of_each_rule()
    by_family: dict[str, set[str]] = {}
    for rule in _contract_rules():
        assert rule["id"] in placement, f"{rule['id']} is on no section of the page"
        by_family.setdefault(rule["family"], set()).add(placement[rule["id"]])

    scattered = {family: sections for family, sections in by_family.items() if len(sections) != 1}
    assert not scattered, f"a family's rules are split across sections: {scattered}"

    titles = [next(iter(sections)) for sections in by_family.values()]
    assert len(titles) == len(set(titles)), f"two families share one section: {titles}"
    assert all(title for title in titles), "a rule row sits above the page's first heading"


def test_every_layer_a_rule_points_at_is_a_page_some_generator_produces() -> None:
    """Every linked layer is a page this package renders; whether the link resolves is `mkdocs build --strict`'s."""
    targets = set(re.findall(r"\]\(model/(\w+)\.md\)", _page()))
    assert targets, "the page links to no layer page at all"
    produced = set(model.all_artifacts())
    missing = sorted(name for name in targets if f"reference/model/{name}.md" not in produced)
    assert missing == [], f"the page links to layer pages nothing produces: {missing}"


def test_every_layer_a_rule_names_is_linked_from_the_page() -> None:
    """The other direction: a layer some rule reads and the page never links to would be a lost row."""
    document = load_contract()
    labels = {layer["name"]: layer.get("label", layer["name"]) for layer in document["layers"]}
    expected = {name for rule in document["rules"] for name in rules.applies_to(rule, labels)}
    linked = set(re.findall(r"\]\(model/(\w+)\.md\)", _page()))
    assert expected <= linked, f"layers a rule reads that the page never links to: {sorted(expected - linked)}"


def test_the_layer_column_uses_the_layers_rendered_label() -> None:
    """The same name the layer index gives it, so one layer is not two names across two pages."""
    document = load_contract()
    swc = next(layer for layer in document["layers"] if layer["name"] == "swc")
    assert f"[{swc['label']}](model/swc.md)" in _page()


#: The topology checks minted by ``validator.check_hardware_topology``, in the order the contract declares them.
HARDWARE_TOPOLOGY_RULES: tuple[str, ...] = (
    "rule:hardware.layer-absent",
    "rule:hardware.no-processor",
    "rule:hardware.multiple-processors",
    "rule:hardware.duplicate-name",
    "rule:hardware.undeclared-partition",
    "rule:hardware.partition-not-a-partition",
    "rule:hardware.partition-undeclared-core",
    "rule:hardware.partition-core-not-a-core",
    "rule:hardware.undeclared-memory",
    "rule:hardware.memory-not-a-memory",
    "rule:hardware.accessible-undeclared-core",
    "rule:hardware.accessible-not-a-core",
    "rule:hardware.unreachable-memory",
)

#: The three budget rules ``resource_analysis`` mints and ``validator._RESOURCE_FINDING_RULES`` maps onto
#: identifiers: a second tuple because the two halves have different producers, which the page does not show.
HARDWARE_BUDGET_RULES: tuple[str, ...] = (
    "rule:hardware.missing-budget",
    "rule:hardware.threshold-breach",
    "rule:hardware.rate-monotonic-violation",
)

#: Every `hardware.` cross-element rule, in contract order. Transcribed on purpose beside the derived
#: oracle: a rule added to or removed from the family moves this list.
HARDWARE_CROSS_ELEMENT_RULES: tuple[str, ...] = HARDWARE_TOPOLOGY_RULES + HARDWARE_BUDGET_RULES


def test_the_page_lists_the_sixteen_hardware_cross_element_checks() -> None:
    """The hardware cross-element checks reach the page, each with its summary, in the cross-element family.

    The summaries are ``validator.CONSISTENCY_RULES``' descriptions carried through the contract (the
    conformance gate owns that equality); this is the last hop.

    **What this does not reach:** whether a summary is true of its check, and which producer mints a
    rule (``test_validator_rule_coverage.py``).
    """
    declared = [
        rule
        for rule in _contract_rules()
        if rule["id"].startswith("rule:hardware.") and rule["family"] == "cross-element"
    ]
    assert [rule["id"] for rule in declared] == list(HARDWARE_CROSS_ELEMENT_RULES)
    assert not set(HARDWARE_TOPOLOGY_RULES) & set(HARDWARE_BUDGET_RULES)

    page = _page()
    flat = _flat(page)
    for rule in declared:
        assert _row_cell(rule["id"]) in page, rule["id"]
        assert _flat(rule["summary"]) in flat, rule["id"]
        assert "[Hardware](model/hardware.md)" in page


def test_the_reference_index_lists_the_page_and_links_to_it() -> None:
    """The section index is rendered from the union, so a new page has to arrive in it with no second edit."""
    artifacts = guide_reference.all_artifacts()
    assert rules.RULES_PATH in artifacts
    assert "(rules.md)" in artifacts[guide_reference.INDEX_PATH]


# `applies_to`, the one derivation the page makes


def test_applies_to_deduplicates_and_keeps_the_declared_order() -> None:
    """Two entries of one layer are one column entry, and the order is the contract's, never sorted."""
    labels = {"tasks": "Tasks", "swc": "SWCs"}
    rule = {
        "id": "rule:test.order",
        "appliesTo": ["field:tasks.task.runnables", "kind:swc.swc", "layer:tasks"],
    }
    assert rules.applies_to(rule, labels) == ("tasks", "swc")


def test_a_layer_with_no_declared_label_is_named_by_its_own_name() -> None:
    """`label` is optional in the contract with a documented fallback; the page follows it."""
    document = copy.deepcopy(load_contract())
    layer = next(entry for entry in document["layers"] if entry["name"] == "swc")
    del layer["label"]
    assert "[swc](model/swc.md)" in rules.rules_page(document=document)


# The refusal path, shown firing


def test_a_family_with_no_section_is_refused() -> None:
    """A rule the page has nowhere to put fails the page rather than being left off it."""
    document = _perturbed(lambda rule: rule.update({"family": "invented"}))
    with pytest.raises(rules.UnrenderableRule) as raised:
        rules.rules_page(document=document)
    assert raised.value.rule_id == document["rules"][0]["id"]
    assert "invented" in raised.value.reason


@pytest.mark.parametrize(
    "entry",
    (
        "rule:connector.duplicate-name",
        "connectors.connector.name",
        "",
    ),
)
def test_an_applies_to_entry_that_names_no_layer_is_refused(entry: str) -> None:
    """Three shapes: a rule identifier (a rule about another rule, no column here), an unprefixed one, an empty one."""
    document = _perturbed(lambda rule: rule.update({"appliesTo": [entry]}))
    with pytest.raises(rules.UnrenderableRule, match="names no layer, kind or field|not a declared layer"):
        rules.rules_page(document=document)


def test_an_applies_to_entry_naming_an_undeclared_layer_is_refused() -> None:
    """The link would resolve to a page no generator produces — a dangling link, refused at the source."""
    document = _perturbed(lambda rule: rule.update({"appliesTo": ["field:invented.thing.name"]}))
    with pytest.raises(rules.UnrenderableRule, match="'invented', which is not a declared layer"):
        rules.rules_page(document=document)


def test_a_renderable_perturbation_still_renders() -> None:
    """The negative control: the harness itself does not make everything fail."""
    document = _perturbed(lambda rule: rule.update({"appliesTo": ["layer:swc"]}))
    page = rules.rules_page(document=document)
    assert _row_cell(document["rules"][0]["id"]) in page
    assert "[SWCs](model/swc.md)" in page


# Determinism, and the seam this page deliberately does not have


def test_two_renders_of_the_same_contract_are_byte_identical() -> None:
    """No sorting, no clock, no set iteration order reaching the output."""
    assert rules.rules_page() == rules.rules_page()


def test_a_use_registry_block_does_not_move_this_page() -> None:
    """Rule declarations reach no registry: the layer pages move under `use_registry` and this one does not."""
    merged = build_model_registry(contract=merged_contract(), binding=merged_binding())
    shipped = guide_reference.all_artifacts()
    with use_registry(merged):
        under_block = guide_reference.all_artifacts()
    assert under_block[rules.RULES_PATH] == shipped[rules.RULES_PATH]
    assert set(under_block) - set(shipped) == {"reference/model/synthetic.md"}


def test_the_page_names_the_two_commands_that_run_the_checks() -> None:
    """The page's one claim about the product beyond the contract's own strings, and one a reader acts on."""
    page = _page()
    assert "`swcraft check-model`" in page
    assert "`swcraft validate`" in page


# The warning paragraph: the one statement of class, after the intro

#: The rules the shipped contract declares with ``severity: warning``, in contract order. Transcribed on
#: purpose beside the derived checks: a warning rule added to or removed from the contract moves this pin,
#: not only a sentence on the page. The validator's gate holds the contract's set equal to its
#: ``WARNING_RULES``; this pin is the page's end of that chain.
SHIPPED_WARNING_RULES: tuple[str, ...] = (
    "rule:trace.requirement-unallocated",
    "rule:trace.requirement-stub",
    "rule:trace.link-version-not-stated",
    "rule:trace.link-needs-confirmation",
    # What the published `needs.json` cannot carry.
    "rule:trace.link-unpublishable-id",
    "rule:trace.requirement-blank-text",
    "rule:swc.variable.read-without-init",
)

#: A shipped error rule the synthetic cases make a warning on a deep copy, chosen by identifier so the
#: perturbation does not go inert. Declared after the shipped warning rules, which gives the order case
#: something to read.
_SYNTHETIC_WARNING_RULE = "rule:name.duplicate-swc"

_WARNING_SENTENCE = re.compile(
    r"`(rule:[^`]+)` is a warning: listed by `check-model` and `validate`, never a refusal; "
    r"`--warnings-as-errors` makes it one\."
)


def _warning_rules(document: dict[str, Any]) -> list[str]:
    """The identifiers of every rule the document declares as a warning, in declaration order."""
    return [rule["id"] for rule in document["rules"] if rule.get("severity") == "warning"]


def _table_rows(page: str) -> list[str]:
    """Every rule row of the page's family tables, in page order."""
    return [line for line in page.splitlines() if re.match(r"^\| `rule:", line)]


def _warning_paragraph_of(page: str) -> str:
    """The one paragraph of the page that carries a warning sentence, or the closing sentence alone."""
    paragraphs = [
        block for block in page.split("\n\n") if "is a warning:" in block or "check below is an error" in block
    ]
    assert len(paragraphs) == 1, f"the class is stated in {len(paragraphs)} paragraphs, not one: {paragraphs}"
    return paragraphs[0]


def test_the_warning_paragraph_names_each_warning_rule_of_the_contract() -> None:
    """One sentence per warning rule, in contract order, and the closing sentence about the rest.

    The rule keeps its one table row: the class is a paragraph beside the tables, never a second row.
    """
    document = load_contract()
    page = _page()
    paragraph = _warning_paragraph_of(page)
    assert _WARNING_SENTENCE.findall(paragraph) == _warning_rules(document)
    assert paragraph.endswith("Every other check is an error.")
    for rule_id in _warning_rules(document):
        assert page.count(_row_cell(rule_id)) == 1, rule_id


def test_the_shipped_contract_declares_exactly_the_seven_warning_rules() -> None:
    """The fixed pin beside the derived check: the shipped set, and the page says so."""
    assert tuple(_warning_rules(load_contract())) == SHIPPED_WARNING_RULES
    assert _WARNING_SENTENCE.findall(_warning_paragraph_of(_page())) == list(SHIPPED_WARNING_RULES)


def test_the_paragraph_follows_the_intro_and_precedes_the_first_family() -> None:
    """Where the class is stated: after the intro's gate sentence, before any table — not inside a family."""
    page = _page()
    intro = page.index("SWCraft checks a project every time it reads one.")
    paragraph = page.index(_warning_paragraph_of(page))
    first_section = page.index("\n## ")
    assert intro < paragraph < first_section


def test_a_further_warning_rule_renders_one_more_sentence_in_contract_order() -> None:
    """A warning rule arriving in the contract is a sentence by regeneration, in the contract's order.

    The family tables are compared row for row with the shipped page: a rule changing class moves no row.
    """
    document = copy.deepcopy(load_contract())
    synthetic = next(rule for rule in document["rules"] if rule["id"] == _SYNTHETIC_WARNING_RULE)
    assert "severity" not in synthetic, "the synthetic rule is already a warning; this case perturbs nothing"
    synthetic["severity"] = "warning"
    expected = _warning_rules(document)
    assert len(expected) == len(SHIPPED_WARNING_RULES) + 1 and expected[-1] == _SYNTHETIC_WARNING_RULE

    page = rules.rules_page(document=document)

    assert _WARNING_SENTENCE.findall(_warning_paragraph_of(page)) == expected
    assert _table_rows(page) == _table_rows(_page())
    assert page.count(_row_cell(_SYNTHETIC_WARNING_RULE)) == 1


def test_a_contract_with_no_warning_rule_states_that_every_check_is_an_error() -> None:
    """The empty set is a sentence, not an absent paragraph: the intro's phrase still has something to read against."""
    document = copy.deepcopy(load_contract())
    for rule in document["rules"]:
        rule.pop("severity", None)
    page = rules.rules_page(document=document)
    paragraph = _warning_paragraph_of(page)
    assert _WARNING_SENTENCE.findall(paragraph) == []
    assert paragraph == "Every check below is an error."
    assert _table_rows(page) == _table_rows(_page())


def test_a_severity_outside_the_contracts_vocabulary_is_refused() -> None:
    """Refuse, never guess: a class this page has no sentence for fails the page rather than reading as an error."""
    document = _perturbed(lambda rule: rule.update({"severity": "fatal"}))
    with pytest.raises(rules.UnrenderableRule, match="severity 'fatal' is neither absent nor 'warning'") as raised:
        rules.rules_page(document=document)
    assert raised.value.rule_id == document["rules"][0]["id"]


def test_an_explicit_null_severity_is_refused_and_not_read_as_absent() -> None:
    """Only a missing key is the contract's default: ``severity: null`` is a value, and no class this page knows."""
    document = _perturbed(lambda rule: rule.update({"severity": None}))
    with pytest.raises(rules.UnrenderableRule, match="severity None is neither absent nor 'warning'") as raised:
        rules.rules_page(document=document)
    assert raised.value.rule_id == document["rules"][0]["id"]


def test_the_page_names_the_flag_that_makes_a_warning_a_refusal() -> None:
    """The one option the page names: the reader's way to make the gate strict, pinned beside the two commands."""
    assert "`--warnings-as-errors`" in _page()


# The anchors: what a finding's rule name in the editor links to


def test_finding_rule_id_strips_the_contract_prefix() -> None:
    """The contract's ``rule:`` entry and the string ``check-model`` puts in a finding's ``rule``."""
    assert rules.finding_rule_id("rule:trace.link-needs-confirmation") == "trace.link-needs-confirmation"


@pytest.mark.parametrize("identifier", ("trace.link-needs-confirmation", "kind:swc.swc"))
def test_finding_rule_id_refuses_an_identifier_without_the_prefix(identifier: str) -> None:
    """A bare id, another entry kind: neither is a contract rule identifier."""
    with pytest.raises(rules.UnrenderableRule, match="does not start with 'rule:'"):
        rules.finding_rule_id(identifier)


def test_the_prefix_alone_is_refused_by_the_anchor_it_would_need() -> None:
    """``rule:`` with nothing after it: the empty anchor is refused, and the page with it."""
    document = _perturbed(lambda rule: rule.update({"id": "rule:"}))
    with pytest.raises(rules.UnrenderableRule, match="anchor ''") as raised:
        rules.rules_page(document=document)
    assert raised.value.rule_id == "rule:"


def test_an_anchor_longer_than_the_editor_links_is_refused() -> None:
    """129 characters: the editor's link pattern would refuse it, so the page refuses the row."""
    document = _perturbed(lambda rule: rule.update({"id": "rule:" + "a" * 129}))
    with pytest.raises(rules.UnrenderableRule, match="anchor 'a"):
        rules.rules_page(document=document)


def test_the_page_and_the_editor_spell_an_anchor_alike() -> None:
    """``render.ANCHOR_PATTERN`` is the editor's ``ANCHOR_PATTERN``, so every anchor the page carries is linkable.

    The bodies are compared (the editor's literal is ``/^…$/``, the page's is used with ``fullmatch``), and
    every finding rule id of the shipped contract passes the editor's pattern. **What this does not reach:**
    the two engines' regex dialects -- the body is a character class and a bound, which both read alike.
    """
    literal = help_links_constant("ANCHOR_PATTERN")
    found = re.fullmatch(r"/\^(.+)\$/", literal)
    assert found is not None, f"helpLinks.ts' ANCHOR_PATTERN is one anchored literal: {literal}"
    assert found.group(1) == render.ANCHOR_PATTERN.pattern
    editor = re.compile(found.group(1))
    refused = [rule["id"] for rule in _contract_rules() if editor.fullmatch(rules.finding_rule_id(rule["id"])) is None]
    assert refused == []


def test_a_rule_whose_anchor_cannot_be_spelled_is_refused() -> None:
    """Refused, never rendered as a row without its anchor or with an attribute list ``attr_list`` misreads."""
    document = _perturbed(lambda rule: rule.update({"id": "rule:has a space"}))
    with pytest.raises(rules.UnrenderableRule, match="anchor 'has a space'") as raised:
        rules.rules_page(document=document)
    assert raised.value.rule_id == "rule:has a space"


def test_a_rule_identifier_without_its_prefix_is_refused() -> None:
    document = _perturbed(lambda rule: rule.update({"id": "bare.identifier"}))
    with pytest.raises(rules.UnrenderableRule, match="does not start with 'rule:'"):
        rules.rules_page(document=document)


class _Ids(HTMLParser):
    """Every ``id`` attribute of a rendered HTML fragment, counted."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: Counter[str] = Counter()
        self.code_ids: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value is not None:
                self.ids[value] += 1
                if tag == "code":
                    self.code_ids[value] += 1


def _site_render(text: str) -> str:
    """*text* rendered by python-markdown with the extension list ``mkdocs.yml`` declares, read, not transcribed.

    mkdocs adds only its default extensions and path processing, so this is a fair oracle for the built ids.
    """
    config = load_yaml_document(MKDOCS_YML.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    names: list[str] = []
    configs: dict[str, Any] = {}
    for entry in config["markdown_extensions"]:
        if isinstance(entry, str):
            names.append(entry)
        else:
            ((name, options),) = entry.items()
            names.append(name)
            configs[name] = options or {}
    return markdown.Markdown(extensions=names, extension_configs=configs).convert(text)


def test_every_contract_rule_has_exactly_one_anchor_on_the_rendered_page() -> None:
    """The anchors' oracle: the page as the site renders it, one ``id="<finding rule id>"`` per contract rule.

    Read off the rendered HTML: the source spelling is an anchor only if ``attr_list`` makes it one.
    **What this does not reach:** the served site's theme and processing, and a browser's scroll.
    """
    parser = _Ids()
    parser.feed(_site_render(_page()))
    anchors = [rules.finding_rule_id(rule["id"]) for rule in _contract_rules()]
    assert len(anchors) == len(set(anchors)), "two contract rules share one anchor"
    wrong = {anchor: parser.ids[anchor] for anchor in anchors if parser.ids[anchor] != 1}
    assert wrong == {}, f"anchors present other than exactly once on the rendered page: {wrong}"
    assert set(parser.code_ids) == set(anchors), "an anchored code span is not a contract rule's, or one is missing"


def _emitted_rule_ids() -> frozenset[str]:
    """The finding rule ids the model check declares it can put in a finding, from the three declared sets.

    ``validator.CONSISTENCY_RULES``, the values of ``validator._RESOURCE_FINDING_RULES``, and the element-local
    rules ``binding.json`` attaches to a schema class's validator, each as the bare id a finding carries.
    """
    element_local = {
        rules.finding_rule_id(identifier) for identifier, target in load_binding()["rules"].items() if "kind" in target
    }
    return frozenset({*validator.CONSISTENCY_RULES, *_RESOURCE_FINDING_RULES.values(), *element_local})


def test_every_emitted_rule_has_an_anchor() -> None:
    """Every rule the model check declares it emits has its anchor on the page of the same release.

    No list of rules ships with the extension, so the page must carry every rule a finding's link names.
    **What this does not reach:** a rule string built at run time outside the three sets (its link opens
    the page at the top), a guide of another release, and whether each set is what the checks emit
    (``test_model_contract_conformance.py``, ``test_coverage_oracle.py``).
    """
    emitted = _emitted_rule_ids()
    assert len(emitted) > len(validator.CONSISTENCY_RULES), "the element-local rules read as empty"
    parser = _Ids()
    parser.feed(_site_render(_page()))
    missing = sorted(rule for rule in emitted if parser.code_ids[rule] != 1)
    assert missing == [], f"rules a finding can name that have no anchor on the page of checks: {missing}"


def _help_topics() -> dict[str, tuple[str, str]]:
    """``helpLinks.ts``' ``HELP_TOPICS``, read off the TypeScript: each topic's page and anchor."""
    table = help_links_constant("HELP_TOPICS")
    topics = {
        name: (page, anchor)
        for name, page, anchor in re.findall(r'(\w+): \{ page: "([^"]+)", anchor: "([^"]+)" \}', table)
    }
    assert topics, f"no topic read from HELP_TOPICS: {table}"
    return topics


def test_each_help_topic_anchor_is_one_id_on_its_rendered_page() -> None:
    """The *Learn more* anchors' oracle: each hand-written section, as the site renders it, carries its id once.

    **What this does not reach:** the served site's theme, and a browser's scroll to the fragment.
    """
    for name, (page, anchor) in sorted(_help_topics().items()):
        source = GUIDE_DIR / f"{page.rstrip('/')}.md"
        parser = _Ids()
        parser.feed(_site_render(source.read_text(encoding="utf-8")))
        assert parser.ids[anchor] == 1, f"{name}: {source.name} renders id={anchor!r} {parser.ids[anchor]} times"
