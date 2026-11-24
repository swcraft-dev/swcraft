"""The guide's model reference: the content oracle, the refusal path, the seam.

``swcraft_tools.guide_reference.model`` is a total consumer of the model contract. Each check is
derived from the contract, never transcribed:

* **the content oracle** — every layer, kind and field the loaded contract declares appears in the
  rendered set, computed by walking the contract's own JSON;
* **the refusal path** — a field type with no rendering raises
  :class:`~swcraft_tools.guide_reference.model.UnrenderableFieldType` instead of a blank cell,
  shown firing against a perturbed copy of the synthetic contract fixture;
* **the registry seam** — the synthetic fixture's extra layer renders inside a ``use_registry``
  block with no production edit (the full page is ``test_synthetic_flow_through.py``'s).

**What this module does not reach.** The comparison with the committed files (the drift gate's);
whether the rendered prose is correct (the contract's ``description`` strings are rendered verbatim,
so their truth is the contract's); and whether a relative link resolves (``mkdocs build --strict``).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from guide_fixtures import vocabulary_hits
from model_contract_fixtures import load_contract, merged_binding, merged_contract
from swcraft_tools import __version__, editor_schema, guide_reference
from swcraft_tools.guide_reference import model, render
from swcraft_tools.guide_reference.render import escape_markup
from swcraft_tools.model_registry import ModelRegistry, build_model_registry, current_registry, use_registry

#: The layer the synthetic fixture adds, used by the refusal tests.
SYNTHETIC = "synthetic"


def _shipped_artifacts() -> dict[str, str]:
    return model.all_artifacts()


def _contract_layers() -> list[dict[str, Any]]:
    return list(load_contract()["layers"])


def _perturbed_registry(mutate) -> ModelRegistry:
    """A registry over the synthetic-extended contract, *mutate* applied in place to the synthetic scalar field.

    Nothing is written to disk: the merge exists in this process alone.
    """
    contract = copy.deepcopy(merged_contract())
    layer = next(entry for entry in contract["layers"] if entry["name"] == SYNTHETIC)
    mutate(layer["kinds"][0]["fields"][1])
    return build_model_registry(contract=contract, binding=merged_binding())


# The content oracle: every layer, kind and field, from the contract


def test_every_declared_layer_has_a_page() -> None:
    """One page per layer, both directions, plus the index."""
    artifacts = _shipped_artifacts()
    expected = {model.MODEL_INDEX_PATH} | {f"reference/model/{layer['name']}.md" for layer in _contract_layers()}
    assert set(artifacts) == expected


def test_every_declared_kind_appears_on_its_layers_page() -> None:
    """Every kind of every layer — top-level and member-only alike — is a heading on that layer's page.

    On the heading, not the bare name: a name inside a description would satisfy a substring check.
    """
    artifacts = _shipped_artifacts()
    missing = [
        f"{layer['name']}: {kind['kind']}"
        for layer in _contract_layers()
        for kind in layer["kinds"]
        if f"### `{kind['kind']}`" not in artifacts[f"reference/model/{layer['name']}.md"]
        and f"## `{kind['kind']}`" not in artifacts[f"reference/model/{layer['name']}.md"]
    ]
    assert missing == [], f"kinds the guide's model reference does not render as a section: {missing}"


def test_every_guide_anchor_of_the_reference_table_is_a_heading_on_its_layers_page() -> None:
    """Each top-level kind's ``guideAnchor`` in ``references.json`` anchors a heading of the page a lens opens.

    The anchor is read off the reference table, not recomputed, so the table and the page are held together.
    """
    artifacts = _shipped_artifacts()
    layers = json.loads(editor_schema.references_artifact())["layers"]
    missing = []
    for layer in current_registry().layers:
        page = artifacts[model.layer_page_path(layer)]
        for kind in layers[layer.name]["topLevelKinds"]:
            heading = re.compile(rf"^#+ .* \{{ #{re.escape(kind['guideAnchor'])} \}}$", re.MULTILINE)
            if heading.search(page) is None:
                missing.append(f"{layer.name}: {kind['guideAnchor']}")
    assert missing == [], f"guide anchors with no heading on their layer's page: {missing}"


def test_every_declared_field_appears_as_a_row_on_its_layers_page() -> None:
    """Every field of every kind is a table row, with its description carried verbatim.

    The row is matched on its leading cell, the description on whitespace-collapsed text with
    ``render.escape_markup`` composed (not restated), so a re-wording fails and a re-wrap or the escape does not.
    """
    artifacts = _shipped_artifacts()
    missing: list[str] = []
    for layer in _contract_layers():
        page = artifacts[f"reference/model/{layer['name']}.md"]
        flat = re.sub(r"\s+", " ", page)
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if f"| `{field['name']}` |" not in page:
                    missing.append(f"{field['id']}: no table row")
                elif escape_markup(re.sub(r"\s+", " ", field["description"])) not in flat:
                    missing.append(f"{field['id']}: description not carried verbatim")
    assert missing == [], f"fields the guide's model reference does not render: {missing}"


def test_the_index_lists_every_layer_and_links_to_its_page() -> None:
    """The at-a-glance table is complete, and every link target is a page the generator produces."""
    artifacts = _shipped_artifacts()
    index = artifacts[model.MODEL_INDEX_PATH]
    for layer in _contract_layers():
        assert f"({layer['name']}.md)" in index, f"the layer index does not link to {layer['name']}"
    targets = set(re.findall(r"\]\((\w+\.md)\)", index))
    produced = {path.rsplit("/", 1)[-1] for path in artifacts}
    assert targets <= produced, f"the layer index links to pages nothing produces: {sorted(targets - produced)}"


def test_the_pages_are_ordered_by_the_contracts_display_order() -> None:
    """The reader-facing order is `registry.display_order` — the same order `swcraft list` prints."""
    registry = current_registry()
    rendered = [path for path in model.all_artifacts() if path != model.MODEL_INDEX_PATH]
    assert rendered == [f"reference/model/{name}.md" for name in registry.display_order]


# The refusal path, shown firing


@pytest.mark.parametrize(
    "type_name",
    (
        "duration",
        "list[duration]",
        "map[integer, string]",
        "map[string, duration]",
    ),
)
def test_a_field_type_with_no_rendering_is_refused(type_name: str) -> None:
    """Four spellings, one per branch of `type_label`'s resolution, each raising rather than blanking."""
    registry = _perturbed_registry(lambda field: field.update({"type": type_name}))
    with pytest.raises(model.UnrenderableFieldType) as raised:
        model.layer_page(registry.layer(SYNTHETIC))
    assert raised.value.type_name == type_name
    assert raised.value.field_id == f"field:{SYNTHETIC}.{SYNTHETIC}.title"


def test_the_refusal_names_the_field_and_the_type_in_its_message() -> None:
    """The message is the fix instruction: it says which declaration to look at and what it says."""
    registry = _perturbed_registry(lambda field: field.update({"type": "duration"}))
    with pytest.raises(model.UnrenderableFieldType, match=r"field:synthetic\.synthetic\.title.*'duration'"):
        model.layer_page(registry.layer(SYNTHETIC))


def test_the_value_type_renders_the_readers_name_for_it_rather_than_refusing() -> None:
    """`value` is the one spelling whose shape another declaration decides; its rule is in the description."""
    registry = _perturbed_registry(lambda field: field.update({"type": "value"}))
    page = model.layer_page(registry.layer(SYNTHETIC))
    assert "| `title` | a scalar, a list, or a mapping, which may nest | yes | — |" in page


def test_the_shipped_init_row_names_its_shape_and_carries_the_contracts_own_rule() -> None:
    """The field this rendering exists for, read off the shipped contract.

    What this does not reach: that the sentence is true of the loader -- the `rule:interface.init-*` shape rules
    enforce it, rendered in `test_guide_reference_rules.py`. The shape cell says composites *may* nest and
    nothing about depth, asserted both ways: a value nested past CPython's recursion limit raises, so
    "to any depth" would be a promise the guide cannot keep.
    """
    registry = current_registry()
    page = model.layer_page(registry.layer("interfaces"))
    row = next(line for line in page.splitlines() if line.startswith("| `init` |"))
    assert "a scalar, a list, or a mapping, which may nest" in row
    assert "any depth" not in row
    assert "| no | absent |" in row
    assert "follows the element's own declared type" in row
    assert "reported as a model error rather than converted" in row


def test_a_renderable_perturbation_still_renders() -> None:
    """The negative control: the perturbation harness itself does not make everything fail."""
    registry = _perturbed_registry(lambda field: field.update({"type": "integer"}))
    page = model.layer_page(registry.layer(SYNTHETIC))
    assert "| `title` | integer | yes | — |" in page


def test_a_declared_label_with_an_angle_bracket_survives_to_the_page() -> None:
    """A layer's ``label`` is a source string, and it reaches a heading and a link — neither of which escapes.

    The one place a contract string reaches an unescaped emitter; an angle-bracketed label would otherwise
    render as nothing in the built site while the Markdown looked right.
    """
    contract = copy.deepcopy(merged_contract())
    layer = next(entry for entry in contract["layers"] if entry["name"] == SYNTHETIC)
    layer["label"] = "Widgets <all>"
    registry = build_model_registry(contract=contract, binding=merged_binding())
    facts = registry.layer(SYNTHETIC)
    assert facts.display_label == "Widgets <all>", "the label reaches the generator unescaped"
    assert model.layer_page(facts).startswith(f"{render.GENERATED_COMMENT}\n\n# Widgets &lt;all>\n")
    assert f"[Widgets &lt;all>]({SYNTHETIC}.md)" in model.model_index_page(registry=registry)


def test_the_index_refuses_a_page_it_has_no_summary_for() -> None:
    """`UnsummarizedPage`, shown firing: a generated page with no index row is refused, not listed bare."""
    with pytest.raises(guide_reference.UnsummarizedPage, match="reference/invented.md"):
        guide_reference.index_page({"reference/invented.md": "# Invented\n"})


# The seam, and what the pages may not carry


def test_the_registry_is_resolved_at_call_time_not_at_import_time() -> None:
    """A `use_registry` block is seen by `all_artifacts`: the explicit `registry=` and the ambient context."""
    merged = build_model_registry(contract=merged_contract(), binding=merged_binding())
    explicit = model.all_artifacts(registry=merged)
    assert f"reference/model/{SYNTHETIC}.md" in explicit

    with use_registry(merged):
        ambient = model.all_artifacts()
    assert ambient == explicit
    assert f"reference/model/{SYNTHETIC}.md" not in model.all_artifacts(), "and the context was restored"


def test_no_rendered_page_carries_the_product_version() -> None:
    """The guide is not a sixth lockstep site: the version lives in the build-written ``version.md``."""
    carriers = [path for path, text in guide_reference.all_artifacts().items() if __version__ in text]
    assert carriers == [], f"pages embedding the product version {__version__!r}: {carriers}"


def test_no_rendered_page_carries_a_host_path_or_a_date() -> None:
    """Determinism by construction: a host path or a date would make a re-render elsewhere a drift."""
    date = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")
    offenders = [
        path
        for path, text in guide_reference.all_artifacts().items()
        if "/Users/" in text or "/home/" in text or date.search(text)
    ]
    assert offenders == [], f"pages carrying a host path or a date: {offenders}"


#: Phrasings that report *observed project state*: a static page cannot observe a project, and a
#: reader with a different one is told something false about their own model.
_PROJECT_STATE = re.compile(
    r"\b(currently|at present|today|so far|as of now|no project (?:yet |currently )?(?:declares|has))\b",
    re.IGNORECASE,
)

#: A citation into the engineering record: a source line, or a path into a tree the guide's reader never opens.
_CITATION = re.compile(r"\b\w+\.py:\d+|\b(?:docs|packages|tools|mcp_server|agent|vscode_extension)/")


def test_the_hardware_page_is_dual_audience_prose() -> None:
    """The hardware page's descriptions are written for the guide's reader.

    No engineering vocabulary (``guide_fixtures.FORBIDDEN_VOCABULARY``, the list ``test_guide_surface.py``
    applies), no citation into the engineering record, no report of observed project state. **What this
    does not reach:** prose that clears all three and is still written for the wrong reader.
    """
    page = _shipped_artifacts()["reference/model/hardware.md"]

    assert vocabulary_hits(page) == []
    assert _CITATION.search(page) is None, "the page cites a source line or a tree the reader never opens"
    assert _PROJECT_STATE.search(page) is None, "the page reports state a static page cannot observe"

    layer = current_registry().layer("hardware")
    assert [kind.kind for kind in layer.top_level_kinds] == ["processor", "core", "memory", "partition"]
    for kind in layer.top_level_kinds:
        assert f"## `{kind.kind}`" in page
        for field in kind.fields:
            assert f"| `{field.name}` |" in page, f"{kind.kind}.{field.name}"


@pytest.mark.parametrize(
    ("pattern", "offending"),
    [
        # Assembled, so this module carries no line citation of its own.
        (_CITATION, "the rule lives in validator.py" + ":3124"),
        (_CITATION, "the reasoning is in docs/concepts/model-contract.md"),
        (_PROJECT_STATE, "no project currently declares more than one"),
        (_PROJECT_STATE, "today every core is strict"),
    ],
    ids=["source-line", "engineering-tree", "observed-state", "today"],
)
def test_the_dual_audience_gates_fire(pattern: re.Pattern[str], offending: str) -> None:
    """Each guard shown failing, because one that has never failed proves nothing."""
    assert pattern.search(offending) is not None


def test_two_renders_of_the_same_registry_are_byte_identical() -> None:
    """No sorting, no clock, no set iteration order reaching the output."""
    assert guide_reference.all_artifacts() == guide_reference.all_artifacts()


# The emitters


def test_a_table_cell_escapes_the_column_separator() -> None:
    """A description carrying a pipe would otherwise reshape the table around it."""
    assert render.cell("a | b") == r"a \| b"


def test_a_table_cell_collapses_a_hard_line_break() -> None:
    """A newline in a cell ends the row; the collapse is what keeps a wrapped source string one row."""
    assert render.cell("a\nb") == "a b"


def test_a_ragged_table_row_is_refused() -> None:
    """A short row renders a missing column and a long one drops its tail; both look like content."""
    with pytest.raises(ValueError, match="row 0 has 1 cells, the table has 2 columns"):
        render.table(("a", "b"), [("only",)])


def test_the_generated_comment_names_the_command_that_regenerates_the_page() -> None:
    """The comment is the developer's fix instruction, and the first line of every page's source."""
    assert render.REGENERATE_COMMAND in render.GENERATED_COMMENT
    assert render.GENERATED_COMMENT.startswith("<!--")
    assert render.GENERATED_COMMENT.endswith("-->")
    assert all(text.splitlines()[0] == render.GENERATED_COMMENT for text in guide_reference.all_artifacts().values())


def test_the_visible_notice_speaks_to_the_guide_reader_and_names_no_developer_command() -> None:
    """The rendered notice is read by an architect with an installed release and no `scripts/dev.py`."""
    assert "scripts/dev.py" not in render.GENERATED_NOTICE
    assert render.REGENERATE_COMMAND not in render.GENERATED_NOTICE
    assert "edited by hand" not in render.GENERATED_NOTICE
    assert all(render.GENERATED_NOTICE in text for text in guide_reference.all_artifacts().values())


# The second copy of the contract's type grammar


class TestTheTypeGrammarIsCopiedTwiceAndAgrees:
    """`guide_reference.model` and `editor_schema` parse the contract's `typeName` separately.

    A deliberate copy (the guide generator may not import a module of the editor generator's weight), so
    what is asserted is agreement, not identity: the branch each field takes and the refusal each
    unrenderable spelling gets. It fails the day one half learns a spelling the other does not.
    """

    @staticmethod
    def _label_branch(field) -> str:
        """Which of `type_label`'s five branches *field* takes, by the shape of its answer."""
        if field.element_kind is not None:
            return "element-kind"
        if field.collection is not None and field.collection.member_kinds:
            return "member-kinds"
        if field.type in model._SCALAR_TYPES:
            return "scalar"
        if field.type == model._VALUE_TYPE:
            return "value"
        if model._LIST_TYPE.match(field.type):
            return "list"
        if model._MAP_TYPE.match(field.type):
            return "map"
        return "unrenderable"

    @staticmethod
    def _schema_branch(field) -> str:
        """The same question of `_type_schema`, read off the schema it returns."""
        schema = editor_schema._type_schema(field)
        if "$ref" in schema:
            return "element-kind"
        if isinstance(schema.get("type"), list):
            # The one branch that answers with several JSON types at once.
            return "value"
        if schema.get("type") == "array" and field.collection is not None and field.collection.member_kinds:
            return "member-kinds"
        if schema.get("type") == "array":
            return "list"
        if schema.get("type") == "object":
            return "map"
        return "scalar"

    def test_the_two_grammars_are_the_same_four_definitions(self) -> None:
        """The data half of the copy: the scalar set, the two patterns and `value`."""
        assert model._SCALAR_TYPES == editor_schema._SCALAR_TYPES
        assert model._LIST_TYPE.pattern == editor_schema._LIST_TYPE.pattern
        assert model._MAP_TYPE.pattern == editor_schema._MAP_TYPE.pattern
        assert model._VALUE_TYPE == editor_schema._VALUE_TYPE

    def test_every_declared_field_takes_the_same_branch_in_both(self) -> None:
        """The dispatch half, over the shipped contract's whole field population."""
        registry = current_registry()
        fields = [field for layer in registry.layers for kind in layer.kinds for field in kind.fields]
        assert fields, "the registry declares no field, so this comparison reads nothing"
        disagreements = [
            (field.id, self._label_branch(field), self._schema_branch(field))
            for field in fields
            if self._label_branch(field) != self._schema_branch(field)
        ]
        assert not disagreements, f"the two copies of the type grammar disagree: {disagreements}"

    def test_both_branches_of_the_dispatch_are_exercised_by_the_corpus(self) -> None:
        """Without which the comparison above could be two constant functions agreeing."""
        registry = current_registry()
        taken = {
            self._label_branch(field) for layer in registry.layers for kind in layer.kinds for field in kind.fields
        }
        assert {"scalar", "element-kind", "member-kinds", "value"} <= taken, taken

    @pytest.mark.parametrize(
        "type_name",
        ["list[interface]", "map[integer, string]", "map[string, interface]", "duration"],
        ids=["list-of-non-scalar", "non-string-key", "non-scalar-value", "unknown-spelling"],
    )
    def test_a_spelling_neither_can_express_is_refused_by_both(self, type_name: str) -> None:
        """The refusal half: one silently rendering what the other refuses is the failure this catches."""
        registry = _perturbed_registry(lambda field: field.update({"type": type_name}))
        field = registry.layer(SYNTHETIC).kinds[0].fields[1]
        with pytest.raises(model.UnrenderableFieldType):
            model.type_label(field)
        with pytest.raises(editor_schema.UnsupportedFieldType):
            editor_schema._type_schema(field)

    def test_a_spelling_both_can_express_is_refused_by_neither(self) -> None:
        """The negative control for the refusal half, so neither raise is unconditional."""
        registry = _perturbed_registry(lambda field: field.update({"type": "integer"}))
        field = registry.layer(SYNTHETIC).kinds[0].fields[1]
        assert model.type_label(field) == "integer"
        assert editor_schema._type_schema(field) == {"type": "integer"}
