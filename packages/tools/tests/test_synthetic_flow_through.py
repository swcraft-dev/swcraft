"""The sixteenth layer through every total consumer, with no production source edit.

The layer exists only inside this test process, declared by
``packages/tools/tests/fixtures/contracts/model-contract/fixtures/synthetic-layer.json``, bound to a test-local
Pydantic class and read off a real ``tmp_path`` project. The closed half of this check holds the consumers below
against the model contract's coverage declarations, both ways.

The consumers, one numbered stage each in the test's stage comments: ``reader``, ``listing``, ``snapshot``,
``differ``, ``session``, ``impact`` (both directions, and ``swcraft query``'s slice), ``authorization``,
``writer-mutations``, ``editing`` (``discover``, ``read``, ``change_set``, ``commit``), ``cli-scaffolding``,
``editor-schemas``, ``guide-reference`` and ``undeclared-keys``.

One ordered test proves the *same* element survives the whole path. Stage 9 commits a *removal*, so every stage
that must see the element runs before it; stage 6b writes a second element, since one witnesses nothing about
paging. The writer's class tables refuse a ``create`` of a sixteenth-layer element
(:func:`test_a_create_reaches_the_writer_and_is_refused_by_its_class_table`), so stage 9 commits a ``remove``.

**What it does not reach:** the partial consumers (``support/model_contract_fixtures.py::PARTIAL_CONSUMERS``), any
ARXML build, a layer written to a fixture on disk, and the member slice (witnessed in ``test_query.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from model_contract_fixtures import merged_binding, merged_contract
from swcraft_tools import guide_reference, reader
from swcraft_tools import session as session_mod
from swcraft_tools.authorization import _layer_fields, _layer_identity_fields, _layer_models, _layer_names
from swcraft_tools.cli import init_command
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.discovery import discover
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.registry import REGISTRY
from swcraft_tools.editor_schema import layer_schema
from swcraft_tools.guide_reference import render
from swcraft_tools.impact import Dependent, ElementRef, build_reference_graph, impact_of
from swcraft_tools.listing import LayerListing, list_model
from swcraft_tools.model_registry import ModelRegistry, build_model_registry, current_registry, use_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.project import require_project
from swcraft_tools.query import ABSENT, Clause, QueryError, dependencies, slice_layer
from swcraft_tools.snapshot import ModelSnapshot
from swcraft_tools.undeclared_keys import UndeclaredKey, undeclared_keys
from swcraft_tools.writer import mutations
from swcraft_tools.yaml_load import load_yaml_document
from synthetic_support import SyntheticElement

#: The sixteenth layer, as the fixture declares it.
LAYER = "synthetic"

#: The one element, and the composition it references. `S1 -> S` is the whole
#: reference topology this module needs: `field:synthetic.synthetic.composition`
#: is the fixture's one reference declaration and it targets `layer:compositions`.
#: `S` is not the root and places no member, so the reference is its only edge.
ELEMENT = {"name": "S1", "title": "One", "composition": "S"}
COMPOSITION = {"name": "S", "title": "The referenced composition"}


def _approve(proposal: ChangeSetProposal) -> ApprovalDecision:
    """A host approving both questions, bound to the proposal it answers (an approval never carries over)."""
    return ApprovalDecision(
        change_set_id=proposal.change_set_id,
        baseline=proposal.baseline,
        approved=True,
        safety_approved=True,
    )


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8")


@pytest.fixture
def merged() -> ModelRegistry:
    """The kernel over the merged contract and binding; a fixture, so no test sees another's mutation."""
    return build_model_registry(contract=merged_contract(), binding=merged_binding())


#: The one processor `hardware.no-processor` requires of any model that reaches
#: `check_cross_layer_consistency`, which stage 9c does through pass D: the smallest
#: hardware layer that passes (no core, no memory, so no budget is analysed).
PROCESSOR = {"name": "CPU", "kind": "processor", "description": "The one processor"}

#: The root `composition.no-root` requires of the same models; it declares no
#: member, so it stays out of the way of every stage as the processor does.
ROOT_COMPOSITION = {"name": "Root", "title": "Root composition", "root": True}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project: the two mandatory layers' elements, one referenced composition, one sixteenth-layer element.

    The other declared layers are absent, the ordinary state of a young project, which the readers
    answer with ``[]``.
    """
    root = tmp_path / "project"
    _write(root / "swcraft.yaml", {"name": "FlowThrough", "model_root": "model"})
    _write(root / "model" / "compositions" / "S.yaml", COMPOSITION)
    _write(root / "model" / "hardware" / "CPU.yaml", PROCESSOR)
    _write(root / "model" / "compositions" / "Root.yaml", ROOT_COMPOSITION)
    _write(root / "model" / LAYER / "S1.yaml", ELEMENT)
    return root


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    """The same project without the sixteenth layer — the diff's *old* side."""
    root = tmp_path / "baseline" / "model"
    _write(root / "compositions" / "S.yaml", COMPOSITION)
    _write(root / "hardware" / "CPU.yaml", PROCESSOR)
    _write(root / "compositions" / "Root.yaml", ROOT_COMPOSITION)
    return root


# ---------------------------------------------------------------------------
# The spine
# ---------------------------------------------------------------------------


def test_the_synthetic_layer_flows_through_every_total_consumer(
    project: Path, baseline: Path, tmp_path: Path, merged: ModelRegistry
) -> None:
    """One element through every stage, one `use_registry` block, no production source edit."""
    model_root = project / "model"

    with use_registry(merged):
        # -- 1. reader: the public generic read reaches a layer nothing names ---
        elements = reader.read_layer(str(model_root), LAYER)
        assert elements == [SyntheticElement(name="S1", title="One", composition="S")]
        assert reader.find_element_file(str(model_root), LAYER, "S1") == str(model_root / LAYER / "S1.yaml")

        # -- 1b. listing: `swcraft list` names the layer and its one element ---
        # Beside stage 1 rather than after stage 11: stage 9 commits a removal, after which a
        # listing could only witness the empty layer.
        listed = list_model(str(model_root))
        assert list(listed) == list(merged.display_order), "the listing is keyed in `displayOrder`"
        assert listed[LAYER] == LayerListing(identity_field="name", identities=("S1",))
        assert listed["compositions"].identities == ("Root", "S"), "the shipped layers are listed beside it"
        assert listed["swc"] == LayerListing(identity_field="name", identities=()), (
            "an absent directory is an empty listing and never a missing key"
        )
        assert list_model(str(model_root), LAYER) == {LAYER: listed[LAYER]}, "one layer, by name"

        # -- 2. model_state: the one layer traversal in the package ------------
        state = load_model_state(str(model_root))
        assert state.layer_names == merged.layer_names
        # Fifteen shipped layers, and this one.
        assert len(state.layer_names) == 16
        assert state[LAYER] == elements
        assert LAYER not in state.absent, "the directory exists, so the layer is present"
        assert "swc" in state.absent, "and the absent ones are still reported absent"

        # -- 3. snapshot: the adapter carries what it has no field for ---------
        snapshot = ModelSnapshot.from_state(state)
        assert snapshot.state[LAYER] == elements
        assert not hasattr(snapshot, LAYER), "the binding gives the synthetic layer no adapter field"
        assert snapshot.compositions == state["compositions"]

        # -- 4. differ: a sixteenth LayerDiff, keyed by the contract's name ----
        diff = diff_snapshots(ModelSnapshot.from_state(load_model_state(str(baseline))), snapshot)
        assert len(diff.by_layer.layer_names) == 16
        layer_diff = diff.by_layer[LAYER]
        assert layer_diff.added == elements
        assert layer_diff.removed == [] and layer_diff.changed == []
        assert diff.is_empty is False
        assert not hasattr(diff, LAYER), "and no adapter field for it either"

        # -- 5. session: the fingerprint sees it, the gate explains it ---------
        fingerprint = session_mod.model_fingerprint(str(model_root))
        _write(model_root / LAYER / "S1.yaml", {**ELEMENT, "title": "Two"})
        assert session_mod.model_fingerprint(str(model_root)) != fingerprint
        _write(model_root / LAYER / "S1.yaml", ELEMENT)
        assert session_mod.model_fingerprint(str(model_root)) == fingerprint, "and it is content, not mtime"

        touched = session_mod._touched_identities(diff)
        assert touched[LAYER] == {"S1"}, "keyed by the contract's name — there is no adapter spelling"
        assert session_mod._is_explained(f"{LAYER}/S1.yaml", model_root / LAYER / "S1.yaml", touched) is True
        assert session_mod._is_explained(f"{LAYER}/S1.txt", model_root / LAYER / "S1.yaml", touched) is False, (
            "containment: a suffix no layer globs is explained by nothing"
        )

        # -- 6. impact: the fixture's one reference, traversed -----------------
        graph = build_reference_graph(snapshot)
        target, dependent = ElementRef("composition", "S"), ElementRef(LAYER, "S1")
        assert graph[target][dependent] == (), "target -> dependent, with no detail"
        assert impact_of(snapshot, "S") == {"composition": [Dependent(dependent)]}

        # -- 6b. the same consumer's second surface: the query engine ----------
        # The impact consumer's second surface: `slice_layer` reads the layer's declared identity
        # field, each top-level kind's declared fields and the layer's placement table, off a layer
        # no production module names. Not reached: the member slice (the fixture layer declares no
        # collection; `members` is witnessed in `packages/tools/tests/test_query.py`).
        assert dependencies(state, "S1") == {LAYER: [Dependent(ElementRef("composition", "S"))]}, (
            "the forward direction reaches the referenced element of an existing layer"
        )
        assert slice_layer(state, LAYER).identities == ("S1",), "unfiltered, the layer's own identity"
        assert slice_layer(state, LAYER, where=(Clause(field="title", equals="One"),)).identities == ("S1",)
        assert slice_layer(state, LAYER, where=(Clause(field="title", equals="Two"),)).identities == (), (
            "an empty result is an answer about the model, not a refusal"
        )
        assert slice_layer(state, LAYER, where=(Clause(field="composition", exists=True),)).identities == ("S1",)
        with pytest.raises(QueryError):
            slice_layer(state, LAYER, where=(Clause(field="colour", equals="red"),))
        windowed = slice_layer(state, LAYER, limit=1)
        assert windowed.identities == ("S1",)
        assert (windowed.page.total, windowed.page.returned, windowed.page.truncated) == (1, 1, False)
        assert windowed.page.next is ABSENT, "a `limit` above the set size is not a truncation"

        # A one-element layer cannot witness a page: the three assertions above hold for an
        # implementation that never pages. A SECOND element is written for as long as the window
        # takes, as stage 5 perturbs and restores the fingerprint's input.
        _write(model_root / LAYER / "S2.yaml", {"name": "S2", "title": "Two", "composition": "S"})
        try:
            two = load_model_state(str(model_root))
            first = slice_layer(two, LAYER, limit=1)
            assert first.identities == ("S1",)
            assert (first.page.total, first.page.returned, first.page.truncated) == (2, 1, True)
            assert first.page.next == "S1", "the cursor is the last identity the window served"
            second = slice_layer(two, LAYER, limit=1, after=first.page.next)
            assert second.identities == ("S2",), "the cursor continued past what the first window served"
            assert (second.page.total, second.page.returned, second.page.truncated) == (2, 1, True)
            assert second.page.next is ABSENT, "the last window offers no cursor"
            assert slice_layer(two, LAYER, where=(Clause(field="title", equals="Two"),)).identities == ("S2",), (
                "and the filter still selects over the widened set"
            )
        finally:
            (model_root / LAYER / "S2.yaml").unlink()
        assert load_model_state(str(model_root))[LAYER] == elements, "the perturbation is restored"
        assert session_mod.model_fingerprint(str(model_root)) == fingerprint, "byte for byte"

        # -- 7. authorization: the four vocabulary functions -------------------
        assert _layer_names()[-1] == LAYER
        assert len(_layer_names()) == 16
        assert _layer_identity_fields()[LAYER] == "name"
        assert _layer_fields()[LAYER] == frozenset({"title", "composition"})
        assert _layer_models()[LAYER] is SyntheticElement

        # -- 8. writer/mutations: composition, pure and applied ----------------
        assert current_registry().compose_path(LAYER, "S1") == f"{LAYER}/S1.yaml"
        # An identity nothing holds composes its plain file name; asking for the file of `S1`,
        # which is on disk, is a refusal rather than the path of a file a create would overwrite.
        assert mutations._target_path(str(model_root), LAYER, "S2") == model_root / LAYER / "S2.yaml"
        with pytest.raises(ValueError, match="already exists"):
            mutations._target_path(str(model_root), LAYER, "S1")

        # -- 9a. editing/discover: the protocol publishes a descriptor for it --
        manifest = str(project / "swcraft.yaml")
        response = discover(require_project(manifest))
        (descriptor,) = [layer for layer in response["layers"] if layer["layer"] == LAYER]
        assert descriptor["present"] is True
        # The default `summary` omits `optional: false`: absent means `false`.
        assert "optional" not in descriptor
        assert descriptor["identity_field"] == "name"
        assert [kind["kind"] for kind in descriptor["element_kinds"]] == [LAYER]

        # -- 9b. editing/read: the element, published field by field -----------
        # Through `handle_request` and not through `read_value`: the seam this
        # stage exists for is the one where the request's registry is resolved,
        # and calling the inner function with an explicit registry would prove
        # only that the inner function threads what it is handed.
        baseline = handle_request({"operation": "discover"}, project=manifest)["baseline"]
        assert handle_request(
            {"operation": "read", "address": {"layer": LAYER, "element": "S1"}}, project=manifest
        ) == {"response": "read", "address": {"layer": LAYER, "element": "S1"}, "form": "full", "value": ELEMENT}

        # -- 9c. editing/change_set: staged, with the delta the review sees ----
        removal = {
            "operation": "change_set",
            "baseline": baseline,
            "intent": "Remove the synthetic layer's one element.",
            "operations": [{"operation": "remove", "address": {"layer": LAYER, "element": "S1"}}],
        }
        entry = {
            "operation": "remove",
            "address": {"layer": LAYER, "element": "S1"},
            "before": ELEMENT,
            "summary": f"{LAYER}: - S1",
        }
        assert handle_request(removal, project=manifest) == {
            "response": "change_set",
            "status": "staged",
            "baseline": baseline,
            "delta": [entry],
        }
        assert (model_root / LAYER / "S1.yaml").is_file(), "staged commits nothing; the shadow was dropped"

        # -- 9d. editing/commit: approved, applied, and the model moved --------
        committed = handle_request(removal, project=manifest, review=_approve)
        assert committed["status"] == "committed"
        assert committed["delta"] == [entry]
        assert committed["baseline"] != baseline, "the commit produced a new revision token"
        assert committed["audit_ref"], "a committed change set names its own record block"
        assert not (model_root / LAYER / "S1.yaml").exists(), "the synthetic layer's element is gone from disk"
        assert reader.read_layer(str(model_root), LAYER) == []

        # -- 10. cli: a fresh project is scaffolded with the directory ---------
        fresh = tmp_path / "fresh"
        created = init_command._scaffold_model_directories(fresh)
        assert fresh / LAYER in created
        assert (fresh / LAYER).is_dir()
        assert len(created) == 16, (
            "sixteen declared layers and no optional one since contract `2.0.0`, so every layer "
            "is scaffolded — the `optional` branch of `_scaffold_model_directories` simply never fires"
        )

        # -- 11. editor_schema: the sixteenth layer's JSON Schema --------------
        # The whole document: a layer nobody wrote a schema for arrives as a complete one. The
        # reference field is the plain string it is in the file (a cross-file fact is not a document
        # fact), and there is no `definitions` key because the layer reaches no member-only kind.
        assert layer_schema(merged.layer(LAYER)) == {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": f"https://swcraft.dev/schemas/{LAYER}.schema.json",
            "title": LAYER,
            "$comment": (
                "A synthetic layer that exists only inside the test process, to prove that the "
                "completeness checker names an unhandled layer entry by entry."
            ),
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {
                    "description": "The element's identity, as in every other layer but `requirements`.",
                    "type": "string",
                },
                "title": {
                    "description": "One ordinary scalar field: no enum, no unit, no collection, no reference.",
                    "type": "string",
                },
                "composition": {
                    "description": "One reference field, targeting an existing layer by its declared identity field.",
                    "type": "string",
                },
            },
            "required": ["name", "title", "composition"],
        }

        # -- 12. guide_reference: the sixteenth layer's page in the user guide --
        # The whole page: a layer nobody wrote prose for arrives as a complete page. No `Element
        # kinds` heading and no `Blocks used inside those files` section, because the layer declares
        # one kind and reaches no member-only one.
        pages = guide_reference.all_artifacts(registry=merged)
        assert pages[f"reference/model/{LAYER}.md"] == (
            f"{render.GENERATED_COMMENT}\n"
            "\n"
            + render.heading(
                1, LAYER, purpose=f"Where {LAYER} files live, the kinds they hold and every key they accept"
            )
            + "\n"
            "\n"
            f"{render.GENERATED_NOTICE}\n"
            "\n"
            "A synthetic layer that exists only inside the test process, to prove that the completeness "
            "checker names an unhandled layer entry by entry.\n"
            "\n"
            f"One file per `{LAYER}`, in `{LAYER}/` under the model root, named after its `name` and "
            "ending in `.yaml`. Where that file name is already taken by another element's file — compared "
            "without regard to case — the file gets the nearest free `~1`, `~2`, … instead; the element is "
            "identified by the `name` inside the file, never by its file name.\n"
            "\n"
            + render.heading(
                2,
                render.code(LAYER),
                anchor=LAYER,
                purpose=f"Writing `{LAYER}` files in {LAYER}: each key, its type and default",
            )
            + "\n"
            "\n"
            "The synthetic layer's one element kind: an identity, one ordinary scalar and one reference. "
            "Nothing about it is special, which is the point.\n"
            "\n"
            "| Field | Type | Required | Default | What it is |\n"
            "|---|---|---|---|---|\n"
            "| `name` | string | yes | — | The element's identity, as in every other layer but "
            "`requirements`. |\n"
            "| `title` | string | yes | — | One ordinary scalar field: no enum, no unit, no collection, "
            "no reference. |\n"
            "| `composition` | string, the `name` of [compositions](compositions.md) | yes | — | One reference field, "
            "targeting an existing layer by its declared identity field. |\n"
        )
        assert len([path for path in pages if path.startswith("reference/model/")]) == 17, (
            "sixteen layer pages plus their index"
        )
        assert f"[{LAYER}]({LAYER}.md)" in pages["reference/model/index.md"], "and the index links to it"

        # -- 13. undeclared_keys: the sixteenth layer's file walked for stray keys --
        # The predicate resolves the registry in its body, so inside this block it
        # walks the sixteenth layer's declared fields with no registry passed: the
        # element's own three keys pass and a misspelt one is named, at the top.
        clean = yaml.safe_dump(ELEMENT, sort_keys=False)
        assert undeclared_keys(clean, load_yaml_document(clean), LAYER) == ()
        stray = yaml.safe_dump({**ELEMENT, "titel": "One"}, sort_keys=False)
        assert undeclared_keys(stray, load_yaml_document(stray), LAYER) == (
            UndeclaredKey(kind=LAYER, key="titel", locator=()),
        )

    assert reader.read_layer(str(model_root), "compositions"), "the block is left; the shipped contract is back"
    with pytest.raises(KeyError):
        reader.read_layer(str(model_root), LAYER)


# ---------------------------------------------------------------------------
# Where the write path stops, and why it is not `editing/`
# ---------------------------------------------------------------------------


def test_a_create_reaches_the_writer_and_is_refused_by_its_class_table(project: Path, merged: ModelRegistry) -> None:
    """The `create` the spine does not attempt, and the wall it hits.

    Every registry-owned step passes, up to ``staging.materialize`` composing ``synthetic/S2.yaml``;
    the refusal is ``writer/elements.py``'s ``_check_emitted_short_names``, whose ``_NAME_GUARDS``
    and ``_NO_EMITTED_SHORT_NAME`` tables are keyed by the shipped top-level classes and fail closed
    with a ``TypeError``. The exception's text pins the raising module: a change making the writer
    generic must delete this test rather than leave it passing.
    """
    manifest = str(project / "swcraft.yaml")
    with use_registry(merged):
        baseline = handle_request({"operation": "discover"}, project=manifest)["baseline"]
        with pytest.raises(TypeError, match="no emitted-SHORT-NAME policy for SyntheticElement"):
            handle_request(
                {
                    "operation": "change_set",
                    "baseline": baseline,
                    "intent": "Create a second element of the synthetic layer.",
                    "operations": [
                        {
                            "operation": "create",
                            "address": {"layer": LAYER, "element": "S2"},
                            "value": {"name": "S2", "title": "Two", "composition": "S"},
                        }
                    ],
                },
                project=manifest,
            )
    assert not (project / "model" / LAYER / "S2.yaml").exists(), "a refused change set writes nothing"


# ---------------------------------------------------------------------------
# The case that distinguishes a closed seam from a stated one
# ---------------------------------------------------------------------------


def test_an_explicit_projection_serves_the_whole_request_against_a_foreign_context(
    project: Path, merged: ModelRegistry
) -> None:
    """The sixteenth layer read, staged and committed with **no** `use_registry` block.

    Inside the spine's block the threaded projection and the ambient registry are one object; here
    the context holds the shipped contract throughout and the sixteenth layer arrives only as
    ``registry=reg.current(merged)``. Guards the defect of a consumer that resolves
    ``current_registry()`` for itself (``snapshot.load_snapshot`` among them), which made the
    ``read`` answer ``unknown-field``; ``reg.kernel_context`` binds the projection's kernel registry
    for the request, and ``test_editing_registry_injection.py`` gates the class. All three verbs are
    here because the leaks were in each: ``read`` in the loader, the change set in the staging copy,
    the validator and the writer, the commit in the session's unexplained-file tripwire.
    """
    manifest = str(project / "swcraft.yaml")
    projection = reg.current(merged)
    assert current_registry().layer_names == REGISTRY.layer_names, "the context is the shipped contract"
    assert len(projection.layer_names) == 16, "and the projection is not"

    assert handle_request(
        {"operation": "read", "address": {"layer": LAYER, "element": "S1"}},
        project=manifest,
        registry=projection,
    ) == {"response": "read", "address": {"layer": LAYER, "element": "S1"}, "form": "full", "value": ELEMENT}

    baseline = handle_request({"operation": "discover"}, project=manifest, registry=projection)["baseline"]
    removal = {
        "operation": "change_set",
        "baseline": baseline,
        "intent": "Remove the synthetic layer's one element, from outside its context.",
        "operations": [{"operation": "remove", "address": {"layer": LAYER, "element": "S1"}}],
    }
    entry = {
        "operation": "remove",
        "address": {"layer": LAYER, "element": "S1"},
        "before": ELEMENT,
        "summary": f"{LAYER}: - S1",
    }
    staged = handle_request(removal, project=manifest, registry=projection)
    assert staged["status"] == "staged" and staged["delta"] == [entry]
    assert (project / "model" / LAYER / "S1.yaml").is_file(), "staged commits nothing"

    committed = handle_request(removal, project=manifest, review=_approve, registry=projection)
    assert committed["status"] == "committed"
    assert committed["delta"] == [entry]
    assert not (project / "model" / LAYER / "S1.yaml").exists()
    assert current_registry().layer_names == REGISTRY.layer_names, "and the context was restored"


# ---------------------------------------------------------------------------
# The claim the spine rests on
# ---------------------------------------------------------------------------


def test_the_binding_names_a_class_that_lives_outside_the_package(merged: ModelRegistry) -> None:
    """The precondition for the containment rule below, asserted rather than assumed.

    The bound class is resolved by the bare module name ``synthetic_support``,
    which :func:`importlib.import_module` finds because the pytest
    ``pythonpath = ["tests/support"]`` setting in ``packages/tools/pyproject.toml`` puts
    ``tests/support/`` on ``sys.path``. The last assert, that ``packages/tools/tests/`` has no
    ``__init__.py``, records that the test root is not a package; the module name's
    resolution does not rest on it.
    """
    bound = merged.bound_class(LAYER)
    assert merged.kind(LAYER).id == f"kind:{LAYER}.{LAYER}"
    assert bound is SyntheticElement
    assert Path(bound.__module__.replace(".", "/")).name == "synthetic_support"
    assert not (REPO_ROOT / "packages" / "tools" / "tests" / "__init__.py").exists()


@pytest.mark.parametrize(
    "root",
    (
        REPO_ROOT / "packages" / "tools" / "src",
        REPO_ROOT / "packages" / "mcp" / "src",
        REPO_ROOT / "packages" / "agent" / "src",
        REPO_ROOT / "vscode_extension" / "src",
    ),
    ids=lambda path: path.parent.name,
)
def test_no_production_source_tree_names_the_fixture(root: Path) -> None:
    """Re-asserted here, because this module is where the fixture is *used*.

    `test_model_contract_extension.py` makes the same scan (nothing production-side may load
    design-time evidence); a single `if layer == "synthetic"` under `src/` would make every assertion
    above prove nothing. An absent root is a FAILURE, not a skip: a missing one means the scan read
    nothing.
    """
    assert root.is_dir(), f"{root} is missing from this checkout; the containment scan would read nothing"
    needles = ("synthetic-layer.json", "synthetic-coverage.json", "synthetic_support", f"layer:{LAYER}", f'"{LAYER}"')
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {needle}"
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".js", ".json"}
        for needle in needles
        if needle in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], offenders
