"""The synthetic extension fixture: one layer added to the contract in this process only.

The fixture is checked to be what it claims (the fragment validates against the
meta-schema, the merged document validates as a whole contract, and the layer is
deliberately *unremarkable*: a `name` identity, a flat storage record, one ordinary
scalar and one reference to an existing layer, so it cannot quietly prove that an
interesting shape was handled), its deliberate dispositions are checked, and the
merge is checked to be contained: no `.py`, `.ts`, `.js` or `.json` file of the four
`PRODUCTION_SOURCE_ROOTS` names the fixture and no project fixture on disk gains a
`synthetic/` directory.

Does not reach: whether a synthetic layer flows through the generic kernel, and a
file outside those roots or of another suffix that names the fixture.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from model_contract_fixtures import (
    CONTRACT_PACKAGE_ROOT,
    NORMATIVE_CONTRACT_FILES,
    PARTIAL_CONSUMERS,
    expand_entries,
    fragment_validator,
    load_contract,
    load_synthetic_coverage,
    load_synthetic_layer,
    merged_contract,
    meta_schema_validator,
    validation_messages,
)

#: The synthetic layer's identifier and its one kind's, named once.
SYNTHETIC_LAYER = "layer:synthetic"
SYNTHETIC_KIND = "kind:synthetic.synthetic"


#: The two entries the synthetic layer adds at a *populated* granularity.
SYNTHETIC_ENTRIES = (SYNTHETIC_LAYER, SYNTHETIC_KIND)


#: The model roots in the repository: where a layer the merge wrote to disk would land.
PROJECT_FIXTURE_ROOTS = (
    REPO_ROOT / "examples",
    REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates",
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures",
)

#: The source trees that must not know the fixture exists. `docs/` is excluded:
#: its design-time evidence names the fixture on purpose.
PRODUCTION_SOURCE_ROOTS = (
    REPO_ROOT / "packages" / "tools" / "src",
    REPO_ROOT / "packages" / "mcp" / "src",
    REPO_ROOT / "packages" / "agent" / "src",
    REPO_ROOT / "vscode_extension" / "src",
)


def _deliberate_rows() -> list[tuple[str, str]]:
    """Every `(consumer, entry)` `synthetic-coverage.json` deliberately provides."""
    return sorted(
        (consumer, entry)
        for consumer, dispositions in load_synthetic_coverage()["dispositions"].items()
        for entry in dispositions
    )


# ---------------------------------------------------------------------------
# The fixture is what it claims to be
# ---------------------------------------------------------------------------


def test_the_synthetic_fragment_validates_as_a_layer_declaration() -> None:
    """The fragment is held to the same `layerDecl` shape the shipped layers are."""
    validator = fragment_validator("layerDecl")
    for layer in load_synthetic_layer()["layers"]:
        assert validation_messages(validator, layer) == [], layer["id"]


def test_the_merged_document_validates_as_a_whole_contract() -> None:
    """Merging must produce a contract, not a contract-shaped dict."""
    assert validation_messages(meta_schema_validator(), merged_contract()) == []


def test_the_fragment_adds_exactly_one_layer_with_exactly_one_kind() -> None:
    """One added layer, one kind, so the fixture cannot quietly grow."""
    layers = load_synthetic_layer()["layers"]
    assert [layer["id"] for layer in layers] == [SYNTHETIC_LAYER]
    assert [kind["id"] for kind in layers[0]["kinds"]] == [SYNTHETIC_KIND]


def test_the_synthetic_layer_is_deliberately_unremarkable() -> None:
    """The layer is a `name` identity, flat storage, not optional, one plain scalar and one reference.

    If the checker accounts for a layer this dull it accounts for any, so the
    dullness is asserted rather than described.
    """
    layer = load_synthetic_layer()["layers"][0]
    assert layer["identityField"] == "name"
    assert layer["optional"] is False
    assert layer["arxmlBound"] is False, "a synthetic layer must not arrive inside the AUTOSAR Identifier rule"
    assert layer["token"] == layer["name"], (
        "the fixture declares its token rather than exercising the fallback, for the reason it "
        "answers arxmlBound — it stands in for a layer someone would really add — and the value "
        "is its own name, because a layer with an interesting token would prove only that the "
        "interesting token was handled"
    )
    assert layer["storage"]["nesting"] == "flat"
    assert "placement" not in layer, "a placement field is exactly the special case this must not have"

    kind = layer["kinds"][0]
    assert kind["memberOnly"] is False
    assert "discriminatorField" not in kind and "memberOf" not in kind

    fields = {field["name"]: field for field in kind["fields"]}
    assert sorted(fields) == ["composition", "name", "title"]

    identity = fields["name"]
    assert identity["type"] == "string" and identity["required"] is True and identity["immutable"] is True

    scalar = fields["title"]
    assert scalar["type"] == "string"
    assert not (set(scalar) & {"enum", "unit", "collection", "reference", "elementKind", "placement"}), (
        "the ordinary scalar field must stay ordinary"
    )

    reference = fields["composition"]
    assert reference["type"] == "string"
    assert set(reference["reference"]) == {"layer", "identityField"}


def test_the_reference_targets_a_layer_the_shipped_contract_declares() -> None:
    """The reference targets a shipped layer by that layer's own identity field."""
    reference = next(
        field["reference"]
        for field in load_synthetic_layer()["layers"][0]["kinds"][0]["fields"]
        if "reference" in field
    )
    targets = {layer["name"]: layer["identityField"] for layer in load_contract()["layers"]}
    assert reference["layer"] in targets, f"the reference names an undeclared layer: {reference['layer']}"
    assert reference["identityField"] == targets[reference["layer"]]


def test_the_merge_adds_exactly_the_expected_entries_at_each_granularity() -> None:
    """What the merge does to the entry expansions, granularity by granularity."""
    shipped = load_contract()
    merged = merged_contract()
    assert set(expand_entries(merged, "layer")) - set(expand_entries(shipped, "layer")) == {SYNTHETIC_LAYER}
    assert set(expand_entries(merged, "kind")) - set(expand_entries(shipped, "kind")) == {SYNTHETIC_KIND}
    assert set(expand_entries(merged, "field")) - set(expand_entries(shipped, "field")) == {
        "field:synthetic.synthetic.composition",
        "field:synthetic.synthetic.name",
        "field:synthetic.synthetic.title",
    }
    assert set(expand_entries(merged, "rule")) == set(expand_entries(shipped, "rule"))


def test_the_merge_refuses_to_redeclare_a_shipped_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixture able to override a layer could change the contract, not extend it."""
    import model_contract_fixtures

    collision = copy.deepcopy(load_synthetic_layer())
    collision["layers"][0]["id"] = "layer:compositions"
    monkeypatch.setattr(model_contract_fixtures, "load_synthetic_layer", lambda: collision)
    with pytest.raises(ValueError, match="redeclares a shipped layer: layer:compositions"):
        model_contract_fixtures.merged_contract()


# ---------------------------------------------------------------------------
# The deliberate dispositions
# ---------------------------------------------------------------------------


def test_every_deliberate_disposition_names_a_partial_consumer_and_a_synthetic_entry() -> None:
    """The fixture's rows name only partial consumers and synthetic entries.

    A row for a total consumer would read as coverage that is not there; a row for
    a shipped entry would let the fixture change the shipped gap list.
    """
    for consumer, entry in _deliberate_rows():
        assert consumer in PARTIAL_CONSUMERS, f"{consumer} is not a partial consumer"
        assert entry in SYNTHETIC_ENTRIES, f"{consumer} / {entry} is not a synthetic entry"


def test_the_fixture_leaves_at_least_one_consumer_partly_disposed_of() -> None:
    """One consumer is given every synthetic entry and one only some.

    That shape shows the checker names the missing *entry* rather than the
    missing *consumer*.
    """
    given: dict[str, set[str]] = {}
    for consumer, entry in _deliberate_rows():
        given.setdefault(consumer, set()).add(entry)
    assert any(entries == set(SYNTHETIC_ENTRIES) for entries in given.values()), "no fully disposed-of consumer"
    assert any(entries < set(SYNTHETIC_ENTRIES) for entries in given.values()), "no partly disposed-of consumer"


# ---------------------------------------------------------------------------
# Containment: the merge exists in this process and nowhere else
# ---------------------------------------------------------------------------


def test_the_shipped_contract_does_not_declare_the_synthetic_layer() -> None:
    """The shipped contract does not declare the synthetic layer."""
    contract = load_contract()
    assert SYNTHETIC_LAYER not in expand_entries(contract, "layer")
    assert SYNTHETIC_KIND not in expand_entries(contract, "kind")


@pytest.mark.parametrize("root", PRODUCTION_SOURCE_ROOTS, ids=lambda path: path.parent.name)
def test_no_production_source_tree_names_the_fixture(root: Path) -> None:
    """No production source tree names the fixture, so a test input never becomes part of the product.

    Only `.py`, `.ts`, `.js` and `.json` files are read. An absent root FAILS
    rather than skips: all four trees exist in every checkout, so a missing one
    means the scan looks at nothing. The ids name the project because all four
    directories are called `src`.
    """
    assert root.is_dir(), f"{root} is missing from this checkout; the containment scan would read nothing"
    needles = ("synthetic-layer.json", "synthetic-coverage.json", SYNTHETIC_LAYER)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {needle}"
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".js", ".json"}
        for needle in needles
        if needle in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], offenders


def test_no_project_fixture_on_disk_gains_a_synthetic_directory() -> None:
    """The merge writes nothing: no model root has the synthetic layer's declared `storage.directory`."""
    directory = load_synthetic_layer()["layers"][0]["storage"]["directory"]
    found = [
        path.relative_to(REPO_ROOT).as_posix()
        for root in PROJECT_FIXTURE_ROOTS
        if root.is_dir()
        for path in root.rglob(directory)
        if path.is_dir()
    ]
    assert found == [], found


def test_the_fixture_files_are_design_time_evidence_not_package_data() -> None:
    """The fixture lives under `docs/`, never in the package-data contract the wheel ships."""
    assert sorted(path.name for path in CONTRACT_PACKAGE_ROOT.iterdir()) == sorted(NORMATIVE_CONTRACT_FILES)


def test_the_fixture_documents_itself_for_a_reader() -> None:
    """Both files carry `purpose` prose: JSON has no comments."""
    for document in (load_synthetic_layer(), load_synthetic_coverage()):
        assert document["purpose"].strip(), "a fixture with no stated purpose"


def test_the_fixture_carries_no_key_the_merge_would_ignore() -> None:
    """Two keys each, so a third — silently unmerged — is a failure rather than a note."""
    assert sorted(load_synthetic_layer()) == ["layers", "purpose"]
    assert sorted(load_synthetic_coverage()) == ["dispositions", "purpose"]
