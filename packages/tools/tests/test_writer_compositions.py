"""The compositions layer on the write path.

What `create_composition` accepts and refuses, what the layer's bytes look like on
disk, and whether every committed model root carries exactly one root composition.

**The serializer equality is the point of the module.** `compositions/` is written by
the hand-written `yaml_documents.composition_to_dict` (`create_composition`) and the
contract-derived `elements.element_to_dict` (the editing protocol's write path); a file
created by one and rewritten by the other must not move by a line. The equality is over
a **fully populated** composition, because the defect class is a field added to
:class:`~swcraft_tools.schema.Composition` and to only one of the two.

Two form decisions are pinned as *file* facts, because a file is what `git diff` shows:

* a member always writes `name` **and** whichever of `type` / `composition` it
  carries, even when `name` and `type` are equal; the unset one is left out rather
  than written `null`, which `CompositionMember.exactly_one_type_field` refuses;
* `Connector.composition` is never written when it is `None`: `None` means the root,
  whose name is a property of the model, not a constant.

What this module does **not** reach: everything that needs more than one file in hand
(a member's `type` declared, exactly one `root: true` in the layer, a delegation's ports
agreeing) — those are `swcraft check-model`'s rules. `test_schema_compositions.py` holds
the loading half, `test_writer_mutations_storage.py` the path/refusal oracle.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools.auditor import AuditLog
from swcraft_tools.model_registry import production_registry
from swcraft_tools.reader import read_compositions, read_swc_model
from swcraft_tools.schema import Composition, Connector, SWComponent
from swcraft_tools.writer import create_composition
from swcraft_tools.writer.elements import element_to_dict, write_element
from swcraft_tools.writer.yaml_documents import composition_to_dict, connector_to_dict, swc_to_dict

# ---------------------------------------------------------------------------
# One fully populated composition, shared by the equality and the round trip
# ---------------------------------------------------------------------------


def _populated() -> Composition:
    """A composition that exercises every field and both member forms.

    ``Echo``/``Echo`` is a member named after its type; ``Sub`` is a member whose type
    lives in the ``compositions`` layer rather than in ``swc``.
    """
    return Composition(
        name="VehicleTop",
        title="Vehicle top-level composition",
        description="Two members, one of them another composition.",
        root=True,
        ports=[
            {"name": "pSpeed", "direction": "provide", "interface": "ISpeed", "description": "published"},
            {"name": "rTorque", "direction": "require", "interface": "ITorque"},
        ],
        members=[
            {"name": "Alpha", "type": "Beta", "description": "a member whose name differs from its type"},
            {"name": "Echo", "type": "Echo"},
            {"name": "Sub", "composition": "Powertrain"},
        ],
        delegations=[
            {"name": "dSpeed", "port": "pSpeed", "member": "Alpha", "member_port": "pSpeed", "description": "out"},
            {"name": "dTorque", "port": "rTorque", "member": "Sub", "member_port": "rTorque"},
        ],
        # The sub-package, written last by both serializers.
        package="Vehicle",
        # The trace link, written after `package`.
        satisfies=["REQ_TOP@1", "REQ_SAFE@2"],
    )


def _auditor(tmp_path: Path) -> AuditLog:
    return AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))


# ---------------------------------------------------------------------------
# The two serializers
# ---------------------------------------------------------------------------


def test_the_two_serializers_produce_the_same_document() -> None:
    """The hand-written dump and the contract-derived one, compared whole over a full composition."""
    composition = _populated()

    assert composition_to_dict(composition) == element_to_dict(composition)


def test_the_two_serializers_produce_the_same_bytes(tmp_path: Path) -> None:
    """The same claim as bytes: the dump call is written out twice, and a diverging argument escapes a dict check."""
    composition = _populated()
    model_root = tmp_path / "model"

    created = Path(create_composition(str(model_root), "VehicleTop", "placeholder"))
    created.unlink()
    written = Path(write_element(str(model_root), "compositions/VehicleTop.yaml", composition))

    by_hand = yaml.safe_dump(composition_to_dict(composition), default_flow_style=False, sort_keys=False)

    assert written.read_text(encoding="utf-8") == by_hand


def test_the_populated_composition_really_populates_every_declared_field() -> None:
    """:func:`_populated` writes every field the registry declares, so the equality above is over a full element.

    ``element_to_dict`` omits a field at an omittable default, so a field missing from
    both ``composition_to_dict`` and :func:`_populated` would pass the equality green.

    **Does not reach**: whether the value is meaningful (only the *key* is checked), or a
    field added to ``schema.py`` and not to the contract (the conformance gate's).
    """
    document = element_to_dict(_populated())
    layer = next(facts for facts in production_registry().layers if facts.name == "compositions")
    written: dict[str, set[str]] = {
        "composition": set(document),
        "composition_port": {key for port in document["ports"] for key in port},
        "composition_member": {key for member in document["members"] for key in member},
        "delegation": {key for delegation in document["delegations"] for key in delegation},
    }

    for kind in layer.kinds:
        declared = {field.name for field in kind.fields}
        missing = declared - written[kind.kind]
        assert not missing, (
            f"`_populated()` leaves {sorted(missing)} of `{kind.id}` at a default, so "
            "`element_to_dict` omits it and the serializer equality above compares two "
            "documents that are both missing it. Populate the field here."
        )


# ---------------------------------------------------------------------------
# The file, read back
# ---------------------------------------------------------------------------


def test_a_written_composition_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """Write, read, re-serialize — the same bytes; a field one side drops would erase content on a whole-file write."""
    model_root = tmp_path / "model"
    path = Path(write_element(str(model_root), "compositions/VehicleTop.yaml", _populated()))
    first = path.read_text(encoding="utf-8")

    parsed = read_compositions(str(model_root))
    assert [composition.name for composition in parsed] == ["VehicleTop"]

    rewritten = Path(write_element(str(model_root), "compositions/VehicleTop.yaml", parsed[0]))

    assert rewritten.read_text(encoding="utf-8") == first


def test_a_member_writes_its_name_and_its_type_even_when_they_are_equal(tmp_path: Path) -> None:
    """A member named after its own type writes the pair, never the one-word form ``- Echo``."""
    model_root = tmp_path / "model"
    path = Path(write_element(str(model_root), "compositions/VehicleTop.yaml", _populated()))

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    echo = next(member for member in document["members"] if member["name"] == "Echo")

    assert echo == {"name": "Echo", "type": "Echo"}


def test_a_composition_typed_member_writes_composition_and_no_null_type(tmp_path: Path) -> None:
    """A composition-typed member writes `composition`, and no `null` `type` the loader would refuse."""
    model_root = tmp_path / "model"
    path = Path(write_element(str(model_root), "compositions/VehicleTop.yaml", _populated()))

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    sub = next(member for member in document["members"] if member["name"] == "Sub")

    assert sub == {"name": "Sub", "composition": "Powertrain"}
    assert "type" not in sub


def test_a_connectors_composition_is_written_only_when_it_is_set(tmp_path: Path) -> None:
    """`Connector.composition` is absent at ``None`` (the root) and written when set."""
    model_root = tmp_path / "model"
    at_root = Connector(
        name="Example_to_Consumer", **{"from": {"swc": "A", "port": "pOut"}, "to": {"swc": "B", "port": "rIn"}}
    )
    inside = at_root.model_copy(update={"composition": "Powertrain"})

    root_path = Path(write_element(str(model_root), "connectors/Example_to_Consumer.yaml", at_root))
    inside_path = Path(
        write_element(str(model_root), "connectors/Inside.yaml", inside.model_copy(update={"name": "Inside"}))
    )

    assert "composition" not in yaml.safe_load(root_path.read_text(encoding="utf-8"))
    assert yaml.safe_load(inside_path.read_text(encoding="utf-8"))["composition"] == "Powertrain"
    assert "composition" not in connector_to_dict(at_root)


def test_an_external_stub_round_trips_through_a_file(tmp_path: Path) -> None:
    """An external stub round-trips with its reference verbatim.

    The writer emits the prototype from ``external_ref`` and ``external_dest``, so an
    altered reference is a broken document.
    """
    model_root = tmp_path / "model"
    stub = SWComponent(
        name="Rte",
        external=True,
        external_ref="/Project/SupplierPlatforms/Bsw/Bsw",
        external_dest="APPLICATION-SW-COMPONENT-TYPE",
    )
    path = Path(write_element(str(model_root), "swc/external/Rte.yaml", stub))
    first = path.read_text(encoding="utf-8")

    parsed = read_swc_model(str(model_root))

    assert [component.name for component in parsed] == ["Rte"]
    assert parsed[0].external is True
    assert parsed[0].external_ref == "/Project/SupplierPlatforms/Bsw/Bsw"
    assert parsed[0].external_dest == "APPLICATION-SW-COMPONENT-TYPE"
    assert parsed[0].ports == []
    assert yaml.safe_dump(swc_to_dict(parsed[0]), default_flow_style=False, sort_keys=False) == first


# ---------------------------------------------------------------------------
# create_composition
# ---------------------------------------------------------------------------


def test_create_composition_writes_the_minimum_and_audits_it(tmp_path: Path) -> None:
    """The creator writes an empty composition; members are edits to it.

    ``root`` is written only when true and ``description`` only when non-empty; the three
    collections are written **at** their empty default.
    """
    model_root = tmp_path / "model"
    auditor = _auditor(tmp_path)

    path = Path(create_composition(str(model_root), "Root", "Root composition", auditor=auditor))

    assert path == model_root / "compositions" / "Root.yaml"
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "name": "Root",
        "title": "Root composition",
        "ports": [],
        "members": [],
        "delegations": [],
    }

    entries = auditor.get_entries()
    assert [entry["operation"] for entry in entries] == ["create_composition"]
    assert entries[0]["args"] == {"name": "Root", "title": "Root composition"}


def test_create_composition_writes_root_when_asked(tmp_path: Path) -> None:
    path = Path(create_composition(str(tmp_path / "model"), "Root", "Root composition", root=True))

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["root"] is True


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        ("../escape", "name"),
        ("sub/Root", "name"),
        ("1Root", "Identifier"),
        ("Root-Two", "Identifier"),
        ("R" * 129, "Identifier"),
    ],
)
def test_create_composition_refuses_the_names_a_single_call_can_judge(tmp_path: Path, name: str, match: str) -> None:
    """An empty, a path-escaping and a non-``Identifier`` name are refused: one call can judge them."""
    with pytest.raises(ValueError, match=match):
        create_composition(str(tmp_path / "model"), name, "A title")


def test_create_composition_refuses_a_name_the_layer_already_carries(tmp_path: Path) -> None:
    model_root = str(tmp_path / "model")
    create_composition(model_root, "Root", "Root composition")

    with pytest.raises(ValueError, match="already exists"):
        create_composition(model_root, "Root", "Another title")


def test_create_composition_writes_a_second_root_because_one_call_cannot_see_the_first(tmp_path: Path) -> None:
    """A second root is written: ``rule:composition.multiple-roots`` is `check-model`'s, over the whole layer."""
    model_root = str(tmp_path / "model")
    create_composition(model_root, "Root", "Root composition", root=True)
    create_composition(model_root, "Second", "Second root", root=True)

    assert [composition.root for composition in read_compositions(model_root)] == [True, True]


# ---------------------------------------------------------------------------
# The committed model roots
# ---------------------------------------------------------------------------


#: Every committed model root loaded **as a model**, and so owing a root composition.
#:
#: **Does not reach**: the two `fixtures/editor_schema/` trees (read against a JSON
#: Schema only) and `vscode_extension/src/test/fixtures/model` (two files the
#: extension's Go to Definition smoke test reads); none is loaded as a model.
MIGRATED_MODEL_ROOTS: tuple[str, ...] = (
    "examples/basic/model",
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model",
    "packages/tools/tests/fixtures/access_points/model",
    "packages/tools/tests/fixtures/component_exchange/model",
    "packages/tools/tests/fixtures/hardware/model",
    "packages/tools/tests/fixtures/init_values/model",
    "packages/tools/tests/fixtures/recorded_oracles/impact_model",
)


@pytest.mark.parametrize("relative", MIGRATED_MODEL_ROOTS)
def test_every_migrated_model_root_loads_and_declares_exactly_one_root(relative: str) -> None:
    """Every committed model root loads and declares exactly one root composition."""
    declared = read_compositions(str(REPO_ROOT / relative))

    assert declared, f"{relative} declares no composition"
    roots = [composition.name for composition in declared if composition.root]
    assert len(roots) == 1, f"{relative} declares {len(roots)} roots: {roots}"
    assert all(composition.title for composition in declared), f"{relative} has a composition with no title"


@pytest.mark.parametrize("relative", MIGRATED_MODEL_ROOTS)
def test_every_migrated_root_places_each_declared_component_once(relative: str) -> None:
    """Every top-level component is a member exactly once, and no member names nothing.

    Counted, not merely contained: a type placed twice makes a connector endpoint ambiguous.
    The template is equal at zero on both sides.
    """
    model_root = str(REPO_ROOT / relative)
    components = sorted(component.name for component in read_swc_model(model_root))
    placed = [
        member.type for composition in read_compositions(model_root) for member in composition.members if member.type
    ]

    assert sorted(placed) == components, f"{relative}: placed {sorted(placed)} against declared {components}"


# ---------------------------------------------------------------------------
# The layer's emitted name sites, as one table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionNameSite:
    """One place a composition file writes a name AUTOSAR emits as a ``SHORT-NAME``.

    Four sites: the ``COMPOSITION-SW-COMPONENT-TYPE`` itself, one ``{P,R}-PORT-PROTOTYPE``
    per boundary port, one ``SW-COMPONENT-PROTOTYPE`` per member and one
    ``DELEGATION-SW-CONNECTOR`` per delegation. Three consumers drive their cases from this
    table — ``test_writer_elements.py`` (``elements._check_composition_names``),
    ``test_validator_compositions.py``'s reporting limbs (``check_identifier_conformance``)
    and its write-path pairs (``arxml_generation._check_composition_names``) — so a new
    site cannot be graded in one and unguarded in the others.

    Attributes:
        field: the collection the name lives in, or ``None`` for the element's
            own identity.
        guard_label: the fragment both write-path guards word the site with.
        report_word: the fragment ``check_identifier_conformance`` words it with.
    """

    field: str | None
    guard_label: str
    report_word: str

    @property
    def id(self) -> str:
        return self.field or "name"

    def plant(self, document: dict, name: str) -> dict:
        """Return *document* with *name* at this site and the file still coherent.

        The collection's first entry is perturbed, and every **intra-file** reference to it
        moves with it, so a case fails for its own reason rather than a dangling reference.
        Cross-*file* references stay the caller's.
        """
        planted = copy.deepcopy(document)
        if self.field is None:
            planted["name"] = name
            return planted
        previous = planted[self.field][0]["name"]
        planted[self.field][0]["name"] = name
        reference = {"ports": "port", "members": "member"}.get(self.field)
        if reference is not None:
            for delegation in planted.get("delegations") or ():
                if delegation.get(reference) == previous:
                    delegation[reference] = name
        return planted


#: The four sites, in the order one composition file writes them.
#: :func:`test_the_name_site_table_is_every_site_the_layer_declares` derives the
#: same set from the shipped contract, so a fifth collection fails here first.
COMPOSITION_NAME_SITES: tuple[CompositionNameSite, ...] = (
    CompositionNameSite(None, "Composition name", "Composition"),
    CompositionNameSite("ports", "port name", "port"),
    CompositionNameSite("members", "member name", "member"),
    CompositionNameSite("delegations", "delegation name", "delegation"),
)


#: One composition carrying exactly one of each site, for consumers that build in memory.
def one_of_each_site() -> dict:
    """A conforming composition document with one port, member and delegation."""
    return {
        "name": "Root",
        "title": "T",
        "ports": [{"name": "pOut", "direction": "provide", "interface": "ISignal"}],
        "members": [{"name": "Alpha", "type": "Beta"}],
        "delegations": [{"name": "D", "port": "pOut", "member": "Alpha", "member_port": "p"}],
    }


def test_the_name_site_table_is_every_site_the_layer_declares() -> None:
    """A new name-bearing collection fails here before it reaches a guard.

    Derived from the shipped registry (``collections()``, ``layer().identity_field``).
    ``satisfies``, a ``whole_value`` collection of nameless link strings, holds no name
    site and is set apart by its declared member identity.

    **Does not reach**: a collection identified by something other than the layer's
    identity field fails the second assertion rather than being classified — deliberately,
    since such a site needs a decision about what the writer emits.
    """
    registry = production_registry()
    layer = registry.layer("compositions")
    in_layer = tuple(collection for collection in registry.collections() if collection.layer == "compositions")
    assert {c.field for c in in_layer if c.member_identity == "whole_value"} == {"satisfies"}
    declared = tuple(collection for collection in in_layer if collection.member_identity != "whole_value")

    assert {site.field for site in COMPOSITION_NAME_SITES} == {None} | {c.field for c in declared}
    assert {c.identity_field for c in declared} == {layer.identity_field}
