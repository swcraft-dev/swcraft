"""``discover``: the registry projected into a ``discoverResponse``.

The shipped vectors run in ``test_editing_vectors.py``; this module asserts what a
vector cannot state about itself:

* no storage root anywhere in the response, for any manifest;
* full fidelity as a rule: every declared field of every kind is published and every
  collection publishes its ``member_identity``, checked against the registry;
* the ``optional``/``present`` pair, including a present-but-empty layer;
* no abridgement: a layer-narrowed answer equals that layer's project-wide entry;
* the two forms as a structural diff: ``summary`` (the default) is ``full`` without any
  ``description`` and without the layer ``optional`` and field ``required``/``immutable``
  booleans where they are ``false``. Writing ``false`` back restores ``full`` minus its
  prose, and the served ``summary`` validates against the protocol schema, which refuses
  a ``false`` left at one of them.

Not here: the budget regime, which is ``packages/tools/tests/test_discover_budget.py``'s.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from jsonschema import Draft202012Validator
from model_contract_fixtures import merged_binding, merged_contract
from referencing import Registry as SchemaRegistry
from referencing import Resource as SchemaResource
from swcraft_tools._version import __version__
from swcraft_tools.editing import protocol, read
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.discovery import SUMMARY_OMITTED_DEFAULTS, LayerNotDeclaredError, discover
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.model_registry import build_model_registry, use_registry
from swcraft_tools.project import require_project
from swcraft_tools.session import model_fingerprint

REVISION_TOKEN = re.compile(r"^rev[0-9]+:[A-Za-z0-9_-]{8,512}$")

#: The four ``project_info`` keys the passport drops: storage roots and the model hash.
FORBIDDEN_PASSPORT_KEYS = ("model_root", "dist_root", "views_root", "model_hash")


def _project(tmp_path: Path, vector: str = "discover-project-all-layers-present") -> Any:
    """A loaded manifest over one of the seeded vector fixtures."""
    root = fixtures.build_fixture(tmp_path / vector, vector)
    return require_project(str(root / "swcraft.yaml"))


@pytest.fixture
def project(tmp_path: Path) -> Any:
    return _project(tmp_path)


@pytest.fixture
def response(project: Any) -> dict[str, Any]:
    return discover(project)


# The response envelope


def test_the_response_carries_exactly_the_six_declared_keys(response: dict[str, Any]) -> None:
    # `form` is echoed so a caller that named no form can see which it got.
    assert response["response"] == "discover"
    assert set(response) == {"response", "project", "baseline", "reads", "form", "layers"}


def test_the_layers_are_the_ten_registry_layers_in_registry_order(response: dict[str, Any]) -> None:
    assert [layer["layer"] for layer in response["layers"]] == list(reg.REGISTRY.layer_names)


# The passport, and the roots it does not carry


def test_the_passport_is_name_version_and_toolchain(response: dict[str, Any]) -> None:
    assert response["project"] == {
        "name": fixtures.BASE_PROJECT_NAME,
        "arxml_version": "4.2.2",
        "swcraft_version": __version__,
    }


def test_the_passport_carries_no_storage_root(response: dict[str, Any]) -> None:
    assert not set(response["project"]) & set(FORBIDDEN_PASSPORT_KEYS)


def test_no_storage_root_appears_anywhere_in_the_response(tmp_path: Path) -> None:
    """Absent by construction, not filtered out at serialization.

    A serializer that dropped roots by name would leak a root added to the manifest tomorrow.
    """
    root = fixtures.build_fixture(tmp_path / "renamed", "discover-project-all-layers-present")
    (root / "zzparentzz").mkdir()
    shutil.move(str(root / "model"), str(root / "zzparentzz" / "zzmodelrootzz"))
    (root / "swcraft.yaml").write_text(
        "name: renamed-roots-project\n"
        "model_root: zzparentzz/zzmodelrootzz\n"
        "dist_root: zzdistrootzz\n"
        "views_root: zzviewsrootzz\n",
        encoding="utf-8",
    )

    serialized = json.dumps(discover(require_project(str(root / "swcraft.yaml"))))
    for marker in ("zzmodelrootzz", "zzdistrootzz", "zzviewsrootzz", "zzparentzz", *FORBIDDEN_PASSPORT_KEYS):
        assert marker not in serialized, f"the response discloses {marker!r}"


def test_the_passport_reports_the_declared_arxml_version(tmp_path: Path) -> None:
    root = fixtures.build_fixture(tmp_path / "v430", "discover-project-all-layers-present")
    manifest = (root / "swcraft.yaml").read_text(encoding="utf-8")
    (root / "swcraft.yaml").write_text(f"{manifest}arxml_version: '4.3.0'\n", encoding="utf-8")
    assert discover(require_project(str(root / "swcraft.yaml")))["project"]["arxml_version"] == "4.3.0"


# `optional` is a registry fact; `present` is the one probe


def test_no_layer_is_optional_any_more(project: Any, response: dict[str, Any]) -> None:
    """No layer is optional; asserted in both directions so it cannot pass vacuously."""
    full = _full(project)
    published = [layer for layer in full["layers"] if "optional" in layer]
    # Every declared layer; a contract that declares or retires a layer moves it.
    assert len(published) == len(full["layers"]) == 15
    assert [layer["layer"] for layer in published if layer["optional"]] == []
    assert [layer["layer"] for layer in response["layers"] if "optional" in layer] == []


def test_hardware_is_published_as_a_mandatory_layer_with_its_four_kinds(response: dict[str, Any]) -> None:
    """The hardware layer is published mandatory, with all four kinds and their inventory."""
    (hardware,) = [layer for layer in response["layers"] if layer["layer"] == "hardware"]
    assert "optional" not in hardware, "a `summary` omits `optional: false`; absent means `false`"
    assert hardware["identity_field"] == "name"
    assert [kind["kind"] for kind in hardware["element_kinds"]] == ["processor", "core", "memory", "partition"]


def test_the_task_partition_field_publishes_a_reference_to_a_hardware_name(response: dict[str, Any]) -> None:
    """`Task.partition` is published as a reference; the task's old `core` is gone."""
    (tasks,) = [layer for layer in response["layers"] if layer["layer"] == "tasks"]
    (task,) = [kind for kind in tasks["element_kinds"] if kind["kind"] == "task"]
    names = [field["name"] for field in task["fields"]]
    assert "core" not in names
    assert names.index("partition") == 3, "the field took `core`'s slot, the fourth"
    (partition,) = [field for field in task["fields"] if field["name"] == "partition"]
    assert partition["reference"] == {"layer": "hardware", "identity_field": "name"}
    assert partition["type"] == "string"
    assert partition["required"] is True

    (hardware,) = [layer for layer in response["layers"] if layer["layer"] == "hardware"]
    (kind,) = [kind for kind in hardware["element_kinds"] if kind["kind"] == "partition"]
    (core,) = [field for field in kind["fields"] if field["name"] == "core"]
    assert core["reference"] == {"layer": "hardware", "identity_field": "name"}
    assert core["required"] is True


def test_the_registry_declares_no_absent_description(response: dict[str, Any]) -> None:
    """The registry declares no `absentDescription`, so no layer's prose depends on the probe.

    Asserted on the registry because two rendered responses can agree by accident.
    """
    registry = reg.current()
    declared = [descriptor.name for descriptor in registry.layers if descriptor.absent_description is not None]
    assert declared == []
    # Non-vacuous: the selector still exists and still returns the one string.
    for descriptor in registry.layers:
        assert descriptor.described(present=False) == descriptor.description
    # Every declared layer; a contract that declares or retires a layer moves it.
    assert len(registry.layers) == len(response["layers"]) == 15


def test_no_layer_publishes_a_description_that_depends_on_project_state(tmp_path: Path) -> None:
    """No layer's description differs between a project missing a layer and one that is not."""
    # `form="full"`: the summary strips every description, so the comparison would
    # hold vacuously. Asked one layer at a time, the only way `full` answers.
    absent = _full(_project(tmp_path / "a", "discover-layer-hardware-absent"))
    present = _full(_project(tmp_path / "p"))
    assert any("description" in layer for layer in present["layers"]), "non-vacuous: some layer has prose"
    moved = [
        layer["layer"]
        for layer, other in zip(absent["layers"], present["layers"], strict=True)
        if layer.get("description") != other.get("description")
    ]
    assert moved == []


def test_every_layer_of_a_complete_project_is_present(response: dict[str, Any]) -> None:
    """Every declared layer of the complete fixture project (a copy of `examples/basic/model`) probes present."""
    assert all(layer["present"] for layer in response["layers"])


def test_an_absent_mandatory_layer_is_not_present_and_is_not_optional(tmp_path: Path) -> None:
    """An absent mandatory layer probes `present: false`, `optional: false`, with its inventory."""
    project = _project(tmp_path, "discover-layer-hardware-absent")
    answer = discover(project, layer="hardware")
    # The `summary` omits `optional: false` but never `present: false`: the
    # omission is a table of three declared positions, not a filter of false values.
    assert answer["form"] == "summary"
    assert "optional" not in answer["layers"][0]
    assert answer["layers"][0]["present"] is False
    described = discover(project, layer="hardware", form="full")["layers"][0]
    assert described["optional"] is False
    assert described["present"] is False


def test_a_present_but_empty_layer_is_present(tmp_path: Path) -> None:
    """Removing the last element of a layer leaves it present: the protocol never removes a directory.

    That keeps `hardware.no-processor` and `hardware.layer-absent` apart.
    """
    root = fixtures.build_fixture(tmp_path / "emptied", "discover-project-all-layers-present")
    for path in (root / "model" / "hardware").glob("*.yaml"):
        path.unlink()
    answer = discover(require_project(str(root / "swcraft.yaml")), layer="hardware")
    assert answer["layers"][0]["present"] is True


def test_an_absent_layer_still_publishes_its_whole_inventory(tmp_path: Path) -> None:
    """A client can author the first element of an absent layer without a second round trip."""
    absent = discover(_project(tmp_path / "a", "discover-layer-hardware-absent"), layer="hardware")
    present = discover(_project(tmp_path / "p"), layer="hardware")
    assert absent["layers"][0]["element_kinds"] == present["layers"][0]["element_kinds"]


# The `reads` block: what a read answers with, and what it refuses above

#: The four address shapes ``reads.default_form`` publishes a default for, as
#: addresses the engine can answer on the vector base. The question, never the
#: expected answer: each default is read off the published block.
_SHAPE_ADDRESSES: dict[str, dict[str, Any]] = {
    "layer": {"layer": "swc"},
    "collection": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]},
    "element": {"layer": "swc", "element": "ExampleSWC"},
    "field": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]},
}


def test_the_published_forms_are_the_read_modules_own(response: dict[str, Any]) -> None:
    assert response["reads"]["forms"] == list(read.READ_FORMS)


def test_the_published_budget_is_the_constant_read_enforces(project: Any, response: dict[str, Any]) -> None:
    assert response["reads"]["budget_wire_bytes"] == read.RESPONSE_BUDGET_WIRE_BYTES


def test_the_budget_is_read_at_the_call_rather_than_bound_at_import(
    project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A value copied at import would advertise a budget the server does not honour.
    # The figure is above this response's own size, since `discover` is measured
    # against the same constant.
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 123_456)
    assert discover(project)["reads"]["budget_wire_bytes"] == 123_456


def test_every_published_default_is_the_form_a_read_actually_serves(tmp_path: Path) -> None:
    """Each published default is the form a read of that shape actually serves.

    A table hand-written beside ``read.default_form`` would pass a comparison against itself.
    """
    root = fixtures.build_fixture(tmp_path / "defaults", "discover-project-all-layers-present")
    published = discover(require_project(str(root / "swcraft.yaml")))["reads"]["default_form"]
    assert set(published) == set(_SHAPE_ADDRESSES), "the published shapes are the four the protocol names"
    for shape, address in _SHAPE_ADDRESSES.items():
        answered = handle_request({"operation": "read", "address": address}, project=root)
        assert answered.get("form") == published[shape], f"{shape}: {answered}"


def test_a_narrowed_answer_carries_the_same_reads_block(project: Any, response: dict[str, Any]) -> None:
    assert discover(project, layer="hardware")["reads"] == response["reads"]


def test_the_block_is_published_once_and_never_per_layer(response: dict[str, Any]) -> None:
    # Not in `projectPassport` either: the forms and the budget are facts about
    # this *server*, not about this project's identity and not about one layer.
    assert all("reads" not in entry for entry in response["layers"])
    assert "reads" not in response["project"]


# The two discover forms: the strip, the default and the echo


def _keys_at_every_depth(node: Any) -> set[str]:
    """Every mapping key at every depth of *node*."""
    if isinstance(node, dict):
        return set(node).union(*(_keys_at_every_depth(value) for value in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_keys_at_every_depth(item) for item in node)) if node else set()
    return set()


def _without_descriptions(node: Any) -> Any:
    """*node* with every ``description`` key removed, at every depth.

    Not imported from ``discovery``: an oracle calling the implementation would compare it with itself.
    """
    if isinstance(node, dict):
        return {key: _without_descriptions(value) for key, value in node.items() if key != "description"}
    if isinstance(node, list):
        return [_without_descriptions(item) for item in node]
    return node


def _full(project: Any) -> dict[str, Any]:
    """The whole project at ``form: "full"``, assembled one layer at a time.

    The project-wide ``full`` is refused over the budget, so the prose is read as
    the refusal hints: one layer-narrowed ``full`` per declared layer.
    """
    per_layer = [discover(project, layer=layer, form="full") for layer in reg.current().layer_names]
    first = per_layer[0]
    for answer in per_layer[1:]:
        assert {key: value for key, value in answer.items() if key != "layers"} == {
            key: value for key, value in first.items() if key != "layers"
        }
    return {**first, "layers": [layer for answer in per_layer for layer in answer["layers"]]}


def test_the_published_discover_block_is_the_protocols_own(response: dict[str, Any]) -> None:
    assert response["reads"]["discover"] == {
        "forms": list(protocol.DISCOVER_FORMS),
        "default_form": protocol.DEFAULT_DISCOVER_FORM,
    }


def test_a_request_that_names_no_form_is_served_the_published_default(project: Any, response: dict[str, Any]) -> None:
    # Derived, not restated: a default moved in one place and not the other fails here.
    assert response["form"] == response["reads"]["discover"]["default_form"]
    assert response["form"] == "summary"


@pytest.mark.parametrize("form", protocol.DISCOVER_FORMS)
@pytest.mark.parametrize("layer", [None, "hardware"])
def test_the_served_form_is_echoed_at_both_scopes(project: Any, form: str, layer: str | None) -> None:
    if layer is None and form == "full":
        # The project-wide `full` is refused over the budget and echoes no form.
        # Asserted rather than skipped, so the day it fits reads as a change.
        with pytest.raises(read.OverBudget):
            discover(project, layer=layer, form=form)
        return
    assert discover(project, layer=layer, form=form)["form"] == form


def _discover_response_validator() -> Draft202012Validator:
    """``protocol.schema.json#/$defs/discoverResponse``, the half of the oracle a no-op fails."""
    schema = json.loads(fixtures.PROTOCOL_PATH.read_text(encoding="utf-8"))
    registry = SchemaRegistry().with_resources([(schema["$id"], SchemaResource.from_contents(schema))])
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/discoverResponse"}, registry=registry)


@pytest.fixture(params=["shipped", "optional-synthetic-layer"])
def contract(request: pytest.FixtureRequest) -> Iterator[str]:
    """The shipped contract, and one that declares an optional extra layer.

    No shipped layer is optional, so the synthetic fragment of
    ``test_synthetic_flow_through.py``, declared ``optional: true`` here, puts both
    values of all three keys on the wire.
    """
    if request.param == "shipped":
        yield request.param
        return
    merged = merged_contract()
    (synthetic,) = [layer for layer in merged["layers"] if layer["name"] == "synthetic"]
    synthetic["optional"] = True
    with use_registry(build_model_registry(contract=merged, binding=merged_binding())):
        yield request.param


def _scopes() -> list[str | None]:
    """The project-wide scope and every per-layer scope of the contract in force."""
    return [None, *reg.current().layer_names]


def _full_at(project: Any, scope: str | None) -> dict[str, Any]:
    return _full(project) if scope is None else discover(project, layer=scope, form="full")


def test_every_omitted_default_row_sits_at_a_position_the_strip_applies() -> None:
    """Every omitted-default table row sits at a position the strip has a loop for.

    A row at a third position would be ignored by both loops, and the hand-written
    restoration oracle would not notice either.
    """
    assert {position for position, _, _ in SUMMARY_OMITTED_DEFAULTS} <= {"layer", "field"}, (
        "a SUMMARY_OMITTED_DEFAULTS row names a position _strip_descriptions_and_defaults has no loop for; "
        "write that loop, and extend the hand-written restoration in editing_vector_fixtures"
    )
    assert all(default is False for _, _, default in SUMMARY_OMITTED_DEFAULTS), SUMMARY_OMITTED_DEFAULTS


def test_writing_the_defaults_back_restores_full_without_its_prose(project: Any, contract: str) -> None:
    """Writing the defaults back into ``summary`` gives ``full`` without its prose, at every scope.

    The restoration half of the oracle; the envelope differs only in the echoed ``form``.
    """
    for scope in _scopes():
        full = _full_at(project, scope)
        summary = discover(project, layer=scope, form="summary")
        restored = fixtures.with_the_summary_defaults_restored(summary["layers"])
        assert restored == _without_descriptions(full["layers"]), scope
        assert {key: value for key, value in summary.items() if key != "layers"} == (
            {key: value for key, value in full.items() if key != "layers"} | {"form": "summary"}
        ), scope


def test_the_served_summary_leaves_no_default_on_the_wire(project: Any, contract: str) -> None:
    """The served ``summary`` validates against the schema, which refuses a published ``false``.

    Restoration alone would pass a no-op derivation that still published every ``false``.
    """
    validator = _discover_response_validator()
    for scope in _scopes():
        validator.validate(discover(project, layer=scope))
        # Per-layer only: the project-wide `full` is refused over the budget.
        if scope is not None:
            validator.validate(discover(project, layer=scope, form="full"))


def _omitted_and_kept(full_layers: list[dict[str, Any]], summary_layers: list[dict[str, Any]]) -> Counter[str]:
    """Per position and value: how often ``full`` carries the key, and whether the ``summary`` kept it."""
    counts: Counter[str] = Counter()

    def tally(position: str, key: str, full: dict[str, Any], summary: dict[str, Any]) -> None:
        value = "true" if full[key] else "false"
        counts[f"{position}.{key}={value}:{'kept' if key in summary else 'omitted'}"] += 1

    for full_layer, summary_layer in zip(full_layers, summary_layers, strict=True):
        tally("layer", "optional", full_layer, summary_layer)
        for full_kind, summary_kind in zip(full_layer["element_kinds"], summary_layer["element_kinds"], strict=True):
            for full_field, summary_field in zip(full_kind["fields"], summary_kind["fields"], strict=True):
                tally("field", "required", full_field, summary_field)
                tally("field", "immutable", full_field, summary_field)
    return counts


def test_every_default_is_omitted_and_every_true_survives(project: Any, contract: str) -> None:
    """Every ``false`` is omitted from ``summary`` and every ``true`` kept, at each position.

    Both counts are non-zero, or the oracle could pass vacuously; ``optional: true``
    exists on the synthetic contract only.
    """
    counts = _omitted_and_kept(_full(project)["layers"], discover(project)["layers"])
    assert not {key for key in counts if key.endswith("=false:kept") or key.endswith("=true:omitted")}, counts
    for position in ("layer.optional", "field.required", "field.immutable"):
        assert counts[f"{position}=false:omitted"] > 0, (position, counts)
    for position in ("field.required", "field.immutable"):
        assert counts[f"{position}=true:kept"] > 0, (position, counts)
    if contract == "optional-synthetic-layer":
        assert counts["layer.optional=true:kept"] == 1, counts
    else:
        assert counts["layer.optional=true:kept"] == 0, "the shipped contract declares no optional layer"


def test_every_full_answer_is_the_recorded_full_vector(tmp_path: Path) -> None:
    """Each per-layer ``full`` answer equals its recorded vector.

    The ``baseline`` token and the recorded ``swcraft_version`` are set aside, as the
    vector harness sets them aside.
    """
    recorded = {
        vector["input"]["layer"]: vector
        for vector in fixtures.load_vectors().values()
        if vector["kind"] == "example"
        and vector["input"].get("operation") == "discover"
        and vector["input"].get("form") == "full"
    }
    assert set(recorded) == set(reg.REGISTRY.layer_names), "a full vector per layer is the whole snapshot"
    for layer, vector in recorded.items():
        expected = vector["expected"]["response"]
        actual = discover(_project(tmp_path / layer, vector["name"]), layer=layer, form="full")
        for answer in (actual, expected):
            answer.pop("baseline")
            answer["project"].pop("swcraft_version")
        assert actual == expected, layer


def test_the_strip_is_non_vacuous_at_the_layer_the_kind_and_the_field(project: Any) -> None:
    """The diff above would hold if `full` carried no prose at all; this shows it does."""
    full = _full(project)
    assert any("description" in layer for layer in full["layers"])
    assert any("description" in kind for layer in full["layers"] for kind in layer["element_kinds"])
    assert any(
        "description" in field
        for layer in full["layers"]
        for kind in layer["element_kinds"]
        for field in kind["fields"]
    )


def test_no_description_key_survives_at_any_depth_of_a_summary(project: Any) -> None:
    assert "description" not in _keys_at_every_depth(discover(project, form="summary")["layers"])
    assert "description" in _keys_at_every_depth(_full(project)["layers"])


def test_a_field_named_description_keeps_its_row_and_loses_only_its_prose(project: Any) -> None:
    """A field named ``description`` keeps its row and loses only its prose.

    The kinds carrying it are counted from the registry, so a dropped row reads as
    a lost field rather than a shrunk contract.
    """
    expected = [
        (kind.kind, declared)
        for layer in reg.REGISTRY.layers
        for kind in layer.element_kinds
        if (declared := kind.get_field("description")) is not None
    ]
    assert expected, "no shipped kind declares a field named `description`, so this test asserts nothing"
    rows = [
        (kind["kind"], field)
        for layer in discover(project, form="summary")["layers"]
        for kind in layer["element_kinds"]
        for field in kind["fields"]
        if field["name"] == "description"
    ]
    assert [kind for kind, _row in rows] == [kind for kind, _declared in expected]
    for (_kind, row), (_same, declared) in zip(rows, expected, strict=True):
        booleans = {key for key in ("required", "immutable") if getattr(declared, key)}
        assert set(row) == {"name", "type"} | booleans, row
        assert all(row[key] is True for key in booleans), row
        assert row["type"] == "string"


def test_everything_but_the_prose_survives_the_strip(project: Any) -> None:
    """Nothing but the prose and the declared defaults is left out, over the key vocabulary.

    ``optional`` leaves the vocabulary on the shipped contract only because no shipped layer is optional.
    """
    full = _keys_at_every_depth(_full(project)["layers"])
    summary = _keys_at_every_depth(discover(project, form="summary")["layers"])
    assert full - summary == {"description", "optional"}
    # Non-vacuous, and named: the facts a client authors an element from.
    assert {"type", "required", "immutable", "enum", "unit", "reference", "collection", "member_identity"} <= summary


def test_the_engine_serves_and_echoes_the_form_the_request_names(tmp_path: Path, response: dict[str, Any]) -> None:
    root = fixtures.build_fixture(tmp_path / "forms", "discover-project-all-layers-present")
    assert handle_request({"operation": "discover"}, project=root)["form"] == "summary"
    assert handle_request({"operation": "discover", "layer": "swc", "form": "full"}, project=root)["form"] == "full"
    assert handle_request({"operation": "discover", "form": "summary"}, project=root) == response
    # The project-wide `full` is the engine's refusal: `over-budget`, addressless,
    # hinting the summary first.
    refused = handle_request({"operation": "discover", "form": "full"}, project=root)
    assert refused["response"] == "error" and refused["error"] == "over-budget"
    assert "form" not in refused and "addresses" not in refused
    assert refused["hints"][0] == {"operation": "discover", "form": "summary"}


def test_the_engine_refuses_a_form_the_enum_does_not_carry(tmp_path: Path) -> None:
    root = fixtures.build_fixture(tmp_path / "badform", "discover-project-all-layers-present")
    for form in ("index", "FULL", None, 1):
        refused = handle_request({"operation": "discover", "form": form}, project=root)
        assert refused["response"] == "error", form
        assert refused["error"] == "schema-violation", form


# The layer-narrowed form: the same shape, and no abridgement


@pytest.mark.parametrize("layer", reg.REGISTRY.layer_names)
def test_a_narrowed_answer_is_the_project_wide_entry_unchanged(
    layer: str, project: Any, response: dict[str, Any]
) -> None:
    narrowed = discover(project, layer=layer)
    assert [entry["layer"] for entry in narrowed["layers"]] == [layer]
    assert narrowed["layers"][0] == next(entry for entry in response["layers"] if entry["layer"] == layer)


def test_a_narrowed_answer_keeps_the_passport_and_the_baseline(project: Any, response: dict[str, Any]) -> None:
    narrowed = discover(project, layer="tasks")
    assert narrowed["project"] == response["project"]
    assert narrowed["baseline"] == response["baseline"]


def test_an_undeclared_layer_is_a_lookup_failure(project: Any) -> None:
    # `$defs/layerName` is a pattern, not an enum: an undeclared layer is
    # well-formed on the wire and rejected semantically.
    with pytest.raises(LayerNotDeclaredError, match="widgets"):
        discover(project, layer="widgets")


# Full fidelity, asserted against the registry rather than a transcript


def _published_kinds(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {kind["kind"]: kind for layer in response["layers"] for kind in layer["element_kinds"]}


def test_every_registry_element_kind_is_published(response: dict[str, Any]) -> None:
    assert set(_published_kinds(response)) == set(reg.REGISTRY.kind_names())


@pytest.mark.parametrize("kind", reg.REGISTRY.kind_names())
def test_every_declared_field_of_every_kind_is_published(kind: str, response: dict[str, Any]) -> None:
    published = _published_kinds(response)[kind]
    assert [field["name"] for field in published["fields"]] == list(reg.REGISTRY.all_fields(kind))


@pytest.mark.parametrize("kind", reg.REGISTRY.kind_names())
def test_every_published_field_carries_name_type_and_its_booleans_by_rule(kind: str, response: dict[str, Any]) -> None:
    """``name`` and ``type`` always; ``required`` and ``immutable`` absent means ``false``.

    Held against the registry: a boolean dropped where it was ``true`` fails here.
    """
    declared = {field.name: field for field in reg.REGISTRY.element_kind(kind).fields}
    for field in _published_kinds(response)[kind]["fields"]:
        assert set(field) >= {"name", "type"}
        for key in ("required", "immutable"):
            assert key not in field or field[key] is True, (field["name"], key)
            assert field.get(key, False) is getattr(declared[field["name"]], key), (field["name"], key)


def test_the_placement_field_is_published_like_any_other(response: dict[str, Any]) -> None:
    group = next(field for field in _published_kinds(response)["swc"]["fields"] if field["name"] == "group")
    assert group["type"] == "string" and group["required"] and group["immutable"]
    assert "placement" not in group, "`placement` is registry bookkeeping, not published"


def test_every_collection_publishes_its_member_identity(response: dict[str, Any]) -> None:
    """Without it a client cannot construct a member address: there is no ordinal step."""
    collections = [
        (kind["kind"], field["name"], field["collection"])
        for kind in _published_kinds(response).values()
        for field in kind["fields"]
        if "collection" in field
    ]
    # Declared collections across every kind; a contract that adds or retires one moves it.
    assert len(collections) == 47, [name for name, _field, _c in collections]
    for _kind, _field, collection in collections:
        assert collection["kind"] in {"list", "map"}
        assert collection["member_identity"] in {"field", "composite", "whole_value"}


def test_a_member_only_kind_publishes_where_it_appears(response: dict[str, Any]) -> None:
    for kind in _published_kinds(response).values():
        if kind.get("member_only"):
            assert kind["member_of"], f"{kind['kind']} is member_only and says nowhere it appears"
        else:
            assert "member_of" not in kind


def test_no_optional_key_is_published_as_null(response: dict[str, Any]) -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert value is not None, f"{key} is published as null rather than omitted"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(response)


# The baseline token


def test_the_baseline_is_a_revision_token(response: dict[str, Any]) -> None:
    assert REVISION_TOKEN.fullmatch(response["baseline"])


def test_the_token_is_opaque_rather_than_the_fingerprint(project: Any) -> None:
    # A verifiable digest would make "what is hashed" a contract, and the
    # fingerprint deliberately covers every regular file under the model root.
    fingerprint = model_fingerprint(project.resolved_model_root)
    assert fingerprint not in discover(project)["baseline"]


def test_one_model_state_yields_one_token(project: Any) -> None:
    assert discover(project)["baseline"] == discover(project)["baseline"]


def test_the_token_is_computed_fresh_and_never_cached(project: Any) -> None:
    before = discover(project)["baseline"]
    path = Path(project.resolved_model_root) / "tasks" / "Task_10ms.yaml"
    path.write_text(f"{path.read_text(encoding='utf-8')}description: moved\n", encoding="utf-8")
    assert discover(project)["baseline"] != before


def test_a_file_no_reader_parses_still_moves_the_token(project: Any) -> None:
    """Inherited from ``model_fingerprint``, and the reason it is the honest baseline."""
    before = discover(project)["baseline"]
    (Path(project.resolved_model_root) / "notes.txt").write_text("hand-written", encoding="utf-8")
    assert discover(project)["baseline"] != before


def test_two_distinct_model_states_do_not_share_a_token(tmp_path: Path) -> None:
    full = _project(tmp_path / "full")
    without_hardware = _project(tmp_path / "partial", "discover-layer-hardware-absent")
    assert baseline_token(full.resolved_model_root) != baseline_token(without_hardware.resolved_model_root)


def test_a_missing_model_root_is_an_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        baseline_token(tmp_path / "nowhere")


# The engine seam


def test_the_engine_answers_a_discover_request(tmp_path: Path, response: dict[str, Any]) -> None:
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    assert handle_request({"operation": "discover"}, project=root) == response


def test_the_engine_accepts_a_manifest_path_as_well_as_a_root(tmp_path: Path) -> None:
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    from_root = handle_request({"operation": "discover"}, project=root)
    from_manifest = handle_request({"operation": "discover"}, project=root / "swcraft.yaml")
    assert from_root == from_manifest


def test_the_engine_narrows_on_the_layer_key(tmp_path: Path) -> None:
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    answer = handle_request({"operation": "discover", "layer": "sequences"}, project=root)
    assert [layer["layer"] for layer in answer["layers"]] == ["sequences"]


def test_discovery_needs_no_approved_scope(tmp_path: Path) -> None:
    # Reads are unscoped and unaudited; discovery reads registry facts and the passport only.
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    unscoped = handle_request({"operation": "discover"}, project=root, approved_scope=None)
    empty_scope = handle_request({"operation": "discover"}, project=root, approved_scope=[])
    assert unscoped == empty_scope


def test_a_well_formed_change_set_is_never_answered_with_an_error(tmp_path: Path) -> None:
    # A well-formed request that passes every pass gets a `changeSetResponse`,
    # never a protocol code. It must pass the envelope check and leave a valid
    # staged result, or the answer under test would not be the dispatcher's.
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    request = {
        "operation": "change_set",
        "baseline": handle_request({"operation": "discover"}, project=root)["baseline"],
        "operations": [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                "value": 6,
            }
        ],
    }
    answer = handle_request(request, project=root)
    assert answer["response"] == "change_set"
    assert answer["status"] == "staged"


def test_a_malformed_request_is_a_protocol_error_and_not_an_exception(tmp_path: Path) -> None:
    # A request the protocol shapes refuse is the client's mistake: an `errorResult`.
    root = fixtures.build_fixture(tmp_path / "seam", "discover-project-all-layers-present")
    answer = handle_request({"operation": "change_set"}, project=root)
    assert answer["response"] == "error"
    assert answer["error"] == "schema-violation"
    assert answer["failure_class"] == "invalid"
