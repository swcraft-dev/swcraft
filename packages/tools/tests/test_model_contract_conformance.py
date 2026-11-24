"""The model contract's conformance gate.

Where ``test_model_contract.py`` asks whether the artifacts are *well formed*, this module asks whether the
contract and the Python classes bound to it describe the **same model**, both ways, with perturbations that show
each gate failing. The registry's tables are projections of the contract, held by ``test_model_registry.py``, so
no gate here compares them.

The fixture half runs the gate against a small in-memory contract and fixture classes, which cover every branch of
``registry._type_name`` and every shape ``structural_projection`` reports with no fact of the real model, so a
perturbation may mutate anything. The shipped half runs it against ``model-contract.json`` and the classes
``binding.json`` names, and adds the contract's internal cross-references and the "no Python names in a structural
field" rule. There is no draft mode and no vacuous pass: an empty or partial ``fields`` array for a bound kind
whose class declares fields fails.

**What the perturbations do not reach:** no test counts a gate's failure branches or pins a table's labels, so a
comparison added with no perturbation of its own, or a deleted perturbation row, is caught by nothing here.
"""

from __future__ import annotations

import ast
import copy
import importlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

import pytest
import validator_package
from conftest import REPO_ROOT
from emission_scan import EmissionScan, Scopes
from model_contract_fixtures import (
    bound_classes,
    comparison_fields,
    contract_kind_names,
    kind_conformance_failures,
    load_binding,
    load_contract,
    member_of_failures,
    meta_schema_validator,
    placement_failures,
    structural_conformance_failures,
    structural_projection,
    validation_messages,
)
from pydantic import BaseModel, model_validator
from swcraft_tools import safety, schema, validator
from swcraft_tools.validator.connectors import _UNRESOLVED_ENDPOINT_RULES
from swcraft_tools.validator.hardware import _RESOURCE_FINDING_RULES
from typing_extensions import TypeAliasType

# ---------------------------------------------------------------------------
# The fixture model: a complete little schema the registry has never seen
# ---------------------------------------------------------------------------


#: The fixture stand-in for `schema.py`'s `InitValue`: a NAMED recursive type
#: alias, which the projection publishes as `value`. The bare recursive union
#: does not build, and the namedness is what the branch matches.
FixtureValue = TypeAliasType("FixtureValue", "bool | int | float | str | list[FixtureValue] | dict[str, FixtureValue]")


class FixtureEndpoint(BaseModel):
    """An object-valued field's target — the fixture stand-in for `ConnectorEndpoint`."""

    swc: str
    port: str


class FixturePart(BaseModel):
    """A list member identified by one of its own fields."""

    name: str
    kind: Literal["primary", "spare"] = "primary"


class FixtureAlpha(BaseModel):
    """One arm of a discriminated union, so `_union_discriminator` has something to find."""

    type: Literal["alpha"]
    value: int


class FixtureBeta(BaseModel):
    """The other arm."""

    type: Literal["beta"]
    value: int


class FixtureWidget(BaseModel):
    """The fixture's one top-level kind; every field exercises a branch of the projection.

    There is deliberately **no** `group` attribute — that is the placement
    field, reproducing `SWComponent`'s shape rather than an easier one.
    """

    name: str
    kind: Literal["widget"] = "widget"
    size: int
    ratio: float | None = None
    tags: dict[str, int] = {}
    parts: list[FixturePart] = []
    events: list[FixtureAlpha | FixtureBeta] = []
    anchor: FixtureEndpoint
    note: FixtureValue | None = None


class _WidgetWithAnExtraField(FixtureWidget):
    """`schema.py` grew a field and the contract did not hear about it."""

    colour: str = ""


class _WidgetWithoutSize(BaseModel):
    """`schema.py` lost a field the contract still declares."""

    name: str
    kind: Literal["widget"] = "widget"
    ratio: float | None = None
    tags: dict[str, int] = {}
    parts: list[FixturePart] = []
    events: list[FixtureAlpha | FixtureBeta] = []
    anchor: FixtureEndpoint
    note: FixtureValue | None = None


class _WidgetWithARetypedField(BaseModel):
    """`size` became a string."""

    name: str
    kind: Literal["widget"] = "widget"
    size: str
    ratio: float | None = None
    tags: dict[str, int] = {}
    parts: list[FixturePart] = []
    events: list[FixtureAlpha | FixtureBeta] = []
    anchor: FixtureEndpoint
    note: FixtureValue | None = None


class _WidgetWithARequiredRatio(BaseModel):
    """An optional field became required and lost its default."""

    name: str
    kind: Literal["widget"] = "widget"
    size: int
    ratio: float
    tags: dict[str, int] = {}
    parts: list[FixturePart] = []
    events: list[FixtureAlpha | FixtureBeta] = []
    anchor: FixtureEndpoint
    note: FixtureValue | None = None


class _WidgetThatMaterializedItsPlacement(FixtureWidget):
    """The failure `reader.read_swc_model_with_placement` exists to prevent: a field beside the directory."""

    group: str = ""


#: The fixture binding: contract kind identifier -> class, shaped like `binding.json`.
FIXTURE_CLASSES: dict[str, type[BaseModel]] = {
    "kind:widgets.widget": FixtureWidget,
    "kind:widgets.part": FixturePart,
    "kind:widgets.endpoint": FixtureEndpoint,
    "kind:widgets.alpha": FixtureAlpha,
    "kind:widgets.beta": FixtureBeta,
}

_PLACEMENT_DESCRIPTION = "Placement metadata: the value is the element's directory rather than a key in its file."


def mini_contract() -> dict[str, Any]:
    """A complete, meta-schema-valid contract for the fixture model.

    Every call builds fresh dicts and lists, so a perturbation cannot leak into
    the next test. It is a *whole* contract rather than a fragment, because a
    gate exercised against a shape the real contract does not have would prove
    nothing about the real contract.
    """
    return {
        "contractVersion": "1.0.0",
        "swcraftVersion": "0.2.0",
        "layers": [
            {
                "id": "layer:widgets",
                "name": "widgets",
                "optional": False,
                "identityField": "name",
                "storage": {
                    "directory": "widgets",
                    "nesting": "group",
                    "fileSuffix": ".yaml",
                    "filenameFrom": "name",
                },
                "placement": {
                    "kind": "widget",
                    "field": "group",
                    "type": "string",
                    "description": _PLACEMENT_DESCRIPTION,
                },
                "description": "The fixture layer.",
                "kinds": [
                    {
                        "id": "kind:widgets.widget",
                        "kind": "widget",
                        "memberOnly": False,
                        "fields": [
                            {
                                "id": "field:widgets.widget.name",
                                "name": "name",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                            },
                            {
                                "id": "field:widgets.widget.group",
                                "name": "group",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                                "placement": True,
                                "description": _PLACEMENT_DESCRIPTION,
                            },
                            {
                                "id": "field:widgets.widget.kind",
                                "name": "kind",
                                "type": "string",
                                "required": False,
                                "default": "widget",
                                "immutable": True,
                                "enum": ["widget"],
                            },
                            {
                                "id": "field:widgets.widget.size",
                                "name": "size",
                                "type": "integer",
                                "required": True,
                                "immutable": False,
                            },
                            {
                                "id": "field:widgets.widget.ratio",
                                "name": "ratio",
                                "type": "number",
                                "required": False,
                                "default": None,
                                "immutable": False,
                            },
                            {
                                "id": "field:widgets.widget.tags",
                                "name": "tags",
                                "type": "map[string, integer]",
                                "required": False,
                                "default": {},
                                "immutable": False,
                                "collection": {
                                    "kind": "map",
                                    "memberIdentity": "whole_value",
                                },
                            },
                            {
                                "id": "field:widgets.widget.parts",
                                "name": "parts",
                                "type": "list[part]",
                                "required": False,
                                "default": [],
                                "immutable": False,
                                "collection": {
                                    "kind": "list",
                                    "memberIdentity": "field",
                                    "identityField": "name",
                                    "memberKinds": ["part"],
                                },
                            },
                            {
                                "id": "field:widgets.widget.events",
                                "name": "events",
                                "type": "list[alpha | beta]",
                                "required": False,
                                "default": [],
                                "immutable": False,
                                "collection": {
                                    "kind": "list",
                                    "memberIdentity": "whole_value",
                                    "memberKinds": ["alpha", "beta"],
                                    "discriminatorField": "type",
                                },
                            },
                            {
                                "id": "field:widgets.widget.anchor",
                                "name": "anchor",
                                "type": "endpoint",
                                "required": True,
                                "immutable": False,
                                "elementKind": "endpoint",
                            },
                            {
                                "id": "field:widgets.widget.note",
                                "name": "note",
                                "type": "value",
                                "required": False,
                                "default": None,
                                "immutable": False,
                            },
                        ],
                    },
                    {
                        "id": "kind:widgets.part",
                        "kind": "part",
                        "memberOnly": True,
                        "memberOf": [{"kind": "widget", "field": "parts"}],
                        "fields": [
                            {
                                "id": "field:widgets.part.name",
                                "name": "name",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                            },
                            {
                                "id": "field:widgets.part.kind",
                                "name": "kind",
                                "type": "string",
                                "required": False,
                                "default": "primary",
                                "immutable": False,
                                "enum": ["primary", "spare"],
                            },
                        ],
                    },
                    {
                        "id": "kind:widgets.endpoint",
                        "kind": "endpoint",
                        "memberOnly": True,
                        "memberOf": [{"kind": "widget", "field": "anchor"}],
                        "fields": [
                            {
                                "id": "field:widgets.endpoint.swc",
                                "name": "swc",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                            },
                            {
                                "id": "field:widgets.endpoint.port",
                                "name": "port",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                            },
                        ],
                    },
                    {
                        "id": "kind:widgets.alpha",
                        "kind": "alpha",
                        "discriminatorField": "type",
                        "memberOnly": True,
                        "memberOf": [{"kind": "widget", "field": "events"}],
                        "fields": [
                            {
                                "id": "field:widgets.alpha.type",
                                "name": "type",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                                "enum": ["alpha"],
                            },
                            {
                                "id": "field:widgets.alpha.value",
                                "name": "value",
                                "type": "integer",
                                "required": True,
                                "immutable": True,
                            },
                        ],
                    },
                    {
                        "id": "kind:widgets.beta",
                        "kind": "beta",
                        "discriminatorField": "type",
                        "memberOnly": True,
                        "memberOf": [{"kind": "widget", "field": "events"}],
                        "fields": [
                            {
                                "id": "field:widgets.beta.type",
                                "name": "type",
                                "type": "string",
                                "required": True,
                                "immutable": True,
                                "enum": ["beta"],
                            },
                            {
                                "id": "field:widgets.beta.value",
                                "name": "value",
                                "type": "integer",
                                "required": True,
                                "immutable": True,
                            },
                        ],
                    },
                ],
            }
        ],
        "rules": [],
    }


def _widget(contract: dict[str, Any]) -> dict[str, Any]:
    """The fixture's one top-level kind declaration, for a perturbation to reach into."""
    return contract["layers"][0]["kinds"][0]


def _widget_layer_kind(contract: dict[str, Any], kind: str) -> dict[str, Any]:
    """One kind declaration of the fixture layer, by its bare name."""
    for declaration in contract["layers"][0]["kinds"]:
        if declaration["kind"] == kind:
            return declaration
    raise KeyError(kind)


def _widget_field(contract: dict[str, Any], name: str) -> dict[str, Any]:
    for field in _widget(contract)["fields"]:
        if field["name"] == name:
            return field
    raise KeyError(name)


# ---------------------------------------------------------------------------
# The harness against itself: the fixture contract conforms
# ---------------------------------------------------------------------------


def test_the_fixture_contract_is_a_valid_contract_document() -> None:
    """The mini contract is held to the same meta-schema as the shipped one."""
    assert validation_messages(meta_schema_validator(), mini_contract()) == []


def test_the_fixture_contract_conforms_to_the_fixture_classes() -> None:
    """The baseline every perturbation below is measured against."""
    assert structural_conformance_failures(mini_contract(), FIXTURE_CLASSES) == []


def test_the_fixture_covers_every_shape_the_projection_reports() -> None:
    """The fixture exercises each branch of the projection, so no comparison goes untested."""
    projection = structural_projection(FixtureWidget, contract_kind_names(mini_contract(), FIXTURE_CLASSES))
    assert projection["name"].type == "string"
    assert projection["size"].type == "integer"
    assert projection["ratio"].type == "number"
    assert projection["tags"].type == "map[string, integer]"
    assert projection["parts"].type == "list[part]"
    assert projection["events"].type == "list[alpha | beta]"
    assert projection["anchor"].type == "endpoint"
    assert projection["anchor"].element_kind == "endpoint"
    assert projection["note"].type == "value"
    assert projection["note"].element_kind is None
    assert projection["kind"].enum == ("widget",)
    assert projection["events"].discriminator_field == "type"
    assert projection["ratio"].has_default is True
    assert projection["ratio"].default is None
    assert projection["name"].has_default is False
    assert "group" not in projection


# ---------------------------------------------------------------------------
# The projection reproduces the registry's derivation, byte for byte
# ---------------------------------------------------------------------------


def _shipped_kind_names() -> dict[type[BaseModel], str]:
    return contract_kind_names(load_contract(), bound_classes())


def test_the_projection_keys_on_the_serialization_alias() -> None:
    """`Connector.from_`, the schema's one aliased field, projects as `from`."""
    connector = bound_classes()["kind:connectors.connector"]
    projection = structural_projection(connector, _shipped_kind_names())
    assert "from" in projection
    assert "from_" not in projection
    assert projection["from"].element_kind == "connector_endpoint"


def test_every_bound_kind_projects_at_least_one_field() -> None:
    """No bound class is empty, so no kind's comparison can be vacuous by accident."""
    kind_names = _shipped_kind_names()
    for kind_id, model_class in bound_classes().items():
        assert structural_projection(model_class, kind_names), kind_id


# ---------------------------------------------------------------------------
# The placement exclusion, and its exact width
# ---------------------------------------------------------------------------


def test_the_comparison_set_is_contract_fields_minus_placement() -> None:
    """The comparison set is stated once, and it excludes exactly the flagged field."""
    widget = _widget(mini_contract())
    declared = {field["name"] for field in widget["fields"]}
    compared = set(comparison_fields(widget))
    assert declared - compared == {"group"}
    assert "group" not in FixtureWidget.model_fields


def test_a_placement_flag_cannot_hide_a_field_the_class_declares() -> None:
    """Flagging a second field `placement` fails, so the flag cannot remove a field from the comparison."""
    contract = mini_contract()
    _widget_field(contract, "size")["placement"] = True
    failures = structural_conformance_failures(contract, FIXTURE_CLASSES)
    assert any("'size' is flagged placement" in failure for failure in failures), failures
    assert any("a placement field exists precisely because the schema has none" in f for f in failures), failures


def test_the_fixture_placement_triple_agrees() -> None:
    """The baseline for the three pairings below."""
    assert placement_failures(mini_contract()) == []


def _rename_placement_field(contract: dict[str, Any]) -> None:
    contract["layers"][0]["placement"]["field"] = "cluster"


def _move_placement_to_another_kind(contract: dict[str, Any]) -> None:
    contract["layers"][0]["placement"]["kind"] = "part"


def _diverge_placement_type(contract: dict[str, Any]) -> None:
    contract["layers"][0]["placement"]["type"] = "integer"


def _diverge_placement_description(contract: dict[str, Any]) -> None:
    contract["layers"][0]["placement"]["description"] = "Some other wording of the same fact."


def _unflag_the_placement_field(contract: dict[str, Any]) -> None:
    del _widget_field(contract, "group")["placement"]


def _flatten_the_nesting(contract: dict[str, Any]) -> None:
    contract["layers"][0]["storage"]["nesting"] = "flat"


def _drop_the_placement_declaration(contract: dict[str, Any]) -> None:
    del contract["layers"][0]["placement"]


#: One perturbation per pairing the placement check makes, each breaking a
#: different one so that no two are proven by the same failure.
PLACEMENT_PERTURBATIONS: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "placement names another field",
        _rename_placement_field,
        "declares its placement field as 'cluster'",
    ),
    (
        "placement names another kind",
        _move_placement_to_another_kind,
        "declares its placement field on 'part'",
    ),
    (
        "placement type diverges",
        _diverge_placement_type,
        "the layer declares placement type 'integer'",
    ),
    (
        "placement description diverges",
        _diverge_placement_description,
        "differs from the field's own",
    ),
    (
        "the field loses its flag",
        _unflag_the_placement_field,
        "0 of its fields are flagged placement",
    ),
    (
        "nesting stops naming the field",
        _flatten_the_nesting,
        "storage.nesting is 'flat'",
    ),
    # Two rows, one perturbation: dropping the placement declaration yields two
    # independent failures, and each row pins one so neither is deletable.
    (
        "the layer loses its placement while still nesting",
        _drop_the_placement_declaration,
        "storage.nesting is 'group', but the layer declares no placement field",
    ),
    (
        "a flagged field survives its layer's placement",
        _drop_the_placement_declaration,
        "flagged placement, but layer 'widgets' declares no placement field",
    ),
)


@pytest.mark.parametrize(
    ("case", "perturb", "expected"),
    PLACEMENT_PERTURBATIONS,
    ids=[case for case, _, _ in PLACEMENT_PERTURBATIONS],
)
def test_each_placement_pairing_breaks_independently(
    case: str, perturb: Callable[[dict[str, Any]], None], expected: str
) -> None:
    """Breaking any one pairing of nesting, layer placement and the flagged field fails for its own reason."""
    contract = mini_contract()
    perturb(contract)
    failures = placement_failures(contract)
    assert failures, f"{case}: the placement check did not bite"
    assert any(expected in failure for failure in failures), failures


# ---------------------------------------------------------------------------
# The gate bites: the contract mutated away from the classes
# ---------------------------------------------------------------------------


def _add_an_undeclared_contract_field(contract: dict[str, Any]) -> None:
    _widget(contract)["fields"].append(
        {
            "id": "field:widgets.widget.colour",
            "name": "colour",
            "type": "string",
            "required": True,
            "immutable": False,
        }
    )


def _remove_a_declared_field(contract: dict[str, Any]) -> None:
    _widget(contract)["fields"] = [f for f in _widget(contract)["fields"] if f["name"] != "size"]


def _retype_a_field(contract: dict[str, Any]) -> None:
    _widget_field(contract, "size")["type"] = "string"


def _flip_requiredness(contract: dict[str, Any]) -> None:
    _widget_field(contract, "size")["required"] = False


def _change_a_default(contract: dict[str, Any]) -> None:
    _widget_field(contract, "kind")["default"] = "gadget"


def _drop_a_default(contract: dict[str, Any]) -> None:
    del _widget_field(contract, "ratio")["default"]


def _invent_a_default(contract: dict[str, Any]) -> None:
    _widget_field(contract, "size")["default"] = 0


def _change_enum_members(contract: dict[str, Any]) -> None:
    _widget_field(contract, "kind")["enum"] = ["gadget"]


def _reorder_enum_members(contract: dict[str, Any]) -> None:
    for field in contract["layers"][0]["kinds"][1]["fields"]:
        if field["name"] == "kind":
            field["enum"] = ["spare", "primary"]


def _drop_an_enum(contract: dict[str, Any]) -> None:
    del _widget_field(contract, "kind")["enum"]


def _change_a_collection_kind(contract: dict[str, Any]) -> None:
    _widget_field(contract, "tags")["collection"]["kind"] = "list"


def _change_member_kinds(contract: dict[str, Any]) -> None:
    _widget_field(contract, "parts")["collection"]["memberKinds"] = ["endpoint"]


def _reorder_member_kinds(contract: dict[str, Any]) -> None:
    _widget_field(contract, "events")["collection"]["memberKinds"] = ["beta", "alpha"]


def _change_the_collection_discriminator(contract: dict[str, Any]) -> None:
    _widget_field(contract, "events")["collection"]["discriminatorField"] = "kind"


def _drop_a_collection(contract: dict[str, Any]) -> None:
    del _widget_field(contract, "parts")["collection"]


def _change_an_element_kind(contract: dict[str, Any]) -> None:
    _widget_field(contract, "anchor")["elementKind"] = "part"


def _drop_an_element_kind(contract: dict[str, Any]) -> None:
    del _widget_field(contract, "anchor")["elementKind"]


def _empty_a_field_inventory(contract: dict[str, Any]) -> None:
    _widget(contract)["fields"] = []


def _retarget_a_fixture_member_of(contract: dict[str, Any]) -> None:
    _widget_layer_kind(contract, "part")["memberOf"] = [{"kind": "widget", "field": "anchor"}]


def _drop_a_fixture_member_of(contract: dict[str, Any]) -> None:
    del _widget_layer_kind(contract, "part")["memberOf"]


def _declare_an_unbound_kind(contract: dict[str, Any]) -> None:
    contract["layers"][0]["kinds"].append(
        {
            "id": "kind:widgets.gadget",
            "kind": "gadget",
            "memberOnly": False,
            "fields": [],
        }
    )


#: Every comparison the gate makes, with a contract-side perturbation proving it is live.
CONTRACT_PERTURBATIONS: tuple[tuple[str, str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "field set",
        "a contract field the class does not declare",
        _add_an_undeclared_contract_field,
        "the contract declares field 'colour', which FixtureWidget.model_fields does not",
    ),
    (
        "field set",
        "a class field the contract does not declare",
        _remove_a_declared_field,
        "FixtureWidget.model_fields declares 'size', which the contract does not",
    ),
    ("type", "a retyped field", _retype_a_field, "the contract declares type 'string'"),
    (
        "required",
        "requiredness flipped",
        _flip_requiredness,
        "the contract declares required=False",
    ),
    (
        "default",
        "a changed default",
        _change_a_default,
        "the contract declares default 'gadget'",
    ),
    (
        "default",
        "a dropped default",
        _drop_a_default,
        "the contract declares no default",
    ),
    (
        "default",
        "an invented default",
        _invent_a_default,
        "the contract declares a default",
    ),
    (
        "enum",
        "changed members",
        _change_enum_members,
        "the contract declares enum ['gadget']",
    ),
    (
        "enum",
        "reordered members",
        _reorder_enum_members,
        "order is part of the contract",
    ),
    ("enum", "a dropped enum", _drop_an_enum, "the contract declares enum []"),
    (
        "collection",
        "a changed collection kind",
        _change_a_collection_kind,
        "the contract declares a 'list' collection",
    ),
    (
        "collection",
        "changed member kinds",
        _change_member_kinds,
        "the contract declares member kinds ['endpoint']",
    ),
    (
        "collection",
        "reordered member kinds",
        _reorder_member_kinds,
        "the contract declares member kinds ['beta', 'alpha']",
    ),
    (
        "collection",
        "a changed collection discriminator",
        _change_the_collection_discriminator,
        "the contract declares collection discriminator 'kind'",
    ),
    (
        "collection",
        "a dropped collection",
        _drop_a_collection,
        "the contract declares no collection",
    ),
    (
        "elementKind",
        "a changed element kind",
        _change_an_element_kind,
        "the contract declares elementKind 'part'",
    ),
    (
        "elementKind",
        "a dropped element kind",
        _drop_an_element_kind,
        "the contract declares elementKind None",
    ),
    (
        "field set",
        "an emptied field inventory",
        _empty_a_field_inventory,
        "FixtureWidget.model_fields declares 'anchor', which the contract does not",
    ),
    (
        "binding",
        "a kind with no bound class",
        _declare_an_unbound_kind,
        "no class is bound to it",
    ),
    (
        "memberOf",
        "a member kind owned by the wrong field",
        _retarget_a_fixture_member_of,
        "the contract's own collections and element kinds put it in",
    ),
    (
        "memberOf",
        "a member kind that says it is owned by nothing",
        _drop_a_fixture_member_of,
        "declares memberOf=None",
    ),
)


@pytest.mark.parametrize(
    ("comparison", "case", "perturb", "expected"),
    CONTRACT_PERTURBATIONS,
    ids=[f"{comparison}: {case}" for comparison, case, _, _ in CONTRACT_PERTURBATIONS],
)
def test_a_perturbed_contract_fails_the_gate(
    comparison: str, case: str, perturb: Callable[[dict[str, Any]], None], expected: str
) -> None:
    """Contract to schema: a mutated declaration must be reported, for its own reason."""
    contract = mini_contract()
    perturb(contract)
    failures = structural_conformance_failures(contract, FIXTURE_CLASSES)
    assert failures, f"{comparison}/{case}: the gate did not bite"
    assert any(expected in failure for failure in failures), failures


def test_a_class_bound_to_a_kind_the_contract_does_not_declare_is_caught() -> None:
    """A class bound to a kind the contract does not declare fails the gate.

    Not expressible as a contract-side perturbation: removing a kind declaration
    removes the name every other declaration cross-references.
    """
    classes = dict(FIXTURE_CLASSES, **{"kind:widgets.gadget": FixtureEndpoint})
    failures = structural_conformance_failures(mini_contract(), classes)
    assert any("but the contract declares no such kind" in failure for failure in failures), failures


# ---------------------------------------------------------------------------
# The gate bites: the classes mutated away from the contract
# ---------------------------------------------------------------------------

#: The other direction: a perturbed contract cannot prove the schema-to-contract
#: comparison is live, so these fixture classes stand in for a mutated `schema.py`.
SCHEMA_PERTURBATIONS: tuple[tuple[str, type[BaseModel], str], ...] = (
    (
        "the class grew a field",
        _WidgetWithAnExtraField,
        "model_fields declares 'colour', which the contract does not",
    ),
    (
        "the class lost a field",
        _WidgetWithoutSize,
        "the contract declares field 'size', which _WidgetWithoutSize.model_fields does not",
    ),
    (
        "the class retyped a field",
        _WidgetWithARetypedField,
        "the class projects 'string'",
    ),
    (
        "the class made a field required",
        _WidgetWithARequiredRatio,
        "the class projects required=True",
    ),
    (
        "the class materialized the placement field",
        _WidgetThatMaterializedItsPlacement,
        "'group' is flagged placement",
    ),
)


@pytest.mark.parametrize(
    ("case", "model_class", "expected"),
    SCHEMA_PERTURBATIONS,
    ids=[case for case, _, _ in SCHEMA_PERTURBATIONS],
)
def test_a_perturbed_schema_fails_the_gate(case: str, model_class: type[BaseModel], expected: str) -> None:
    """Schema to contract: a class that drifted from its declaration must be reported."""
    classes = dict(FIXTURE_CLASSES, **{"kind:widgets.widget": model_class})
    failures = structural_conformance_failures(mini_contract(), classes)
    assert failures, f"{case}: the gate did not bite"
    assert any(expected in failure for failure in failures), failures


def test_an_empty_contract_field_set_fails_for_a_bound_non_empty_model() -> None:
    """An empty `fields` array for a bound class that declares fields fails, naming every one."""
    contract = mini_contract()
    _empty_a_field_inventory(contract)
    failures = kind_conformance_failures(
        "kind:widgets.widget",
        _widget(contract),
        FixtureWidget,
        contract_kind_names(contract, FIXTURE_CLASSES),
    )
    reported = {failure.split("declares '")[1].split("'")[0] for failure in failures if "declares '" in failure}
    assert reported == set(FixtureWidget.model_fields), reported


# ---------------------------------------------------------------------------
# The shipped contract, held to the whole gate
# ---------------------------------------------------------------------------


def test_the_shipped_contract_and_schema_py_describe_the_same_model() -> None:
    """Every bound kind and field agrees with `schema.py` in both directions, placement triple included."""
    assert structural_conformance_failures(load_contract(), bound_classes()) == []


def test_no_bound_kind_has_an_empty_field_inventory() -> None:
    """No shipped kind declares an empty field inventory."""
    contract = load_contract()
    empty = [kind["id"] for layer in contract["layers"] for kind in layer["kinds"] if not kind["fields"]]
    assert empty == [], empty


def test_the_shipped_placement_triple_is_tied() -> None:
    """`storage.nesting`, `layerDecl.placement` and the flagged `fieldDecl` agree in the shipped contract."""
    assert placement_failures(load_contract()) == []


def test_exactly_one_shipped_field_is_flagged_placement() -> None:
    """`field:swc.swc.group`, and nothing else (`read_swc_model_with_placement`)."""
    contract = load_contract()
    flagged = [
        field["id"]
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if field.get("placement", False)
    ]
    assert flagged == ["field:swc.swc.group"]


# ---------------------------------------------------------------------------
# Reading the contract's own declarations
# ---------------------------------------------------------------------------
#
# These helpers read the contract's declarations into the shapes the
# cross-reference gate and the Python-name scan index by.


def _contract_kind_decls(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every kind declaration by its bare kind name — the registry's own key."""
    return {kind["kind"]: kind for layer in contract["layers"] for kind in layer["kinds"]}


def _contract_layer_decls(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {layer["name"]: layer for layer in contract["layers"]}


def _contract_field_decls(
    contract: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """`(kind, field)` -> declaration, keyed exactly as the registry's tables are."""
    return {
        (kind["kind"], field["name"]): field
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
    }


def _shipped() -> dict[str, Any]:
    return load_contract()


# ---------------------------------------------------------------------------
# The contract's internal cross-references
# ---------------------------------------------------------------------------


def cross_reference_failures(contract: dict[str, Any]) -> list[str]:
    """Every dangling or mis-targeted cross-reference inside the contract itself.

    None of these rules is checkable by the meta-schema, which can say what a
    name looks like but not that something answers to it:

    * a `reference` names a declared layer AND that layer's own identity field —
      checked rather than assumed to be `name`, because `requirements` is `id`;
    * every `collection.memberKinds` entry and every `elementKind` is a declared
      kind;
    * a `discriminatorField` names a field the kinds it discriminates declare;
    * the `requirements` -> `id` identity special case appears exactly once, and
      every layer composes its file name from its own identity field;
    * a bare kind name is declared once across the whole contract. The
      meta-schema constrains only the prefixed `id`, unique *per layer*; every
      cross-reference is written in bare names, so a collision silently drops a
      kind from `_contract_kind_decls` and truncates this whole check. Nothing
      else makes this check.
    """
    layers = _contract_layer_decls(contract)
    kinds = _contract_kind_decls(contract)
    bare_names = [kind["kind"] for layer in contract["layers"] for kind in layer["kinds"]]
    field_names = {kind: {field["name"] for field in decl["fields"]} for kind, decl in kinds.items()}
    failures: list[str] = []

    for field in _contract_field_decls(contract).values():
        reference = field.get("reference")
        if reference is not None:
            target = layers.get(reference["layer"])
            if target is None:
                failures.append(f"{field['id']}: references layer '{reference['layer']}', which is not declared")
            elif reference["identityField"] != target["identityField"]:
                failures.append(
                    f"{field['id']}: references '{reference['layer']}' by '{reference['identityField']}', "
                    f"but that layer's identity field is '{target['identityField']}'"
                )
        element_kind = field.get("elementKind")
        if element_kind is not None and element_kind not in kinds:
            failures.append(f"{field['id']}: declares elementKind '{element_kind}', which is not a declared kind")
        collection = field.get("collection")
        if collection is None:
            continue
        members = collection.get("memberKinds", [])
        for member in members:
            if member not in kinds:
                failures.append(f"{field['id']}: declares member kind '{member}', which is not a declared kind")
        discriminator = collection.get("discriminatorField")
        if discriminator is None:
            continue
        if len(members) < 2:
            failures.append(
                f"{field['id']}: declares a collection discriminator over {len(members)} member kind(s); "
                "there is nothing to tell apart"
            )
        for member in members:
            if member in field_names and discriminator not in field_names[member]:
                failures.append(
                    f"{field['id']}: declares collection discriminator '{discriminator}', "
                    f"which member kind '{member}' does not declare"
                )

    for kind, decl in kinds.items():
        discriminator = decl.get("discriminatorField")
        if discriminator is not None and discriminator not in field_names[kind]:
            failures.append(f"{decl['id']}: declares discriminator '{discriminator}', which the kind does not declare")

    duplicated = sorted({name for name in bare_names if bare_names.count(name) > 1})
    if duplicated:
        failures.append(
            f"the bare kind name(s) {duplicated} are declared more than once; every cross-reference in the "
            "contract is written in bare names, so a collision silently truncates the comparison"
        )

    non_name = sorted(name for name, layer in layers.items() if layer["identityField"] != "name")
    if non_name != ["requirements"] or layers["requirements"]["identityField"] != "id":
        failures.append(
            f"the `requirements` -> `id` identity special case must appear exactly once; layers identified by "
            f"something other than `name` are {non_name}"
        )
    for layer in contract["layers"]:
        if layer["storage"]["filenameFrom"] != layer["identityField"]:
            failures.append(
                f"{layer['id']}: composes its file name from '{layer['storage']['filenameFrom']}' but is "
                f"identified by '{layer['identityField']}'"
            )
    return failures


def test_the_shipped_contract_has_no_dangling_cross_reference() -> None:
    assert cross_reference_failures(load_contract()) == []


def test_the_shipped_member_of_is_derivable_from_the_contracts_own_collections() -> None:
    """Every member kind's `memberOf` is derivable from the contract's own collections and element kinds.

    Guards a `memberOf` naming the wrong owning field, which no other gate caught.
    """
    assert member_of_failures(load_contract()) == []


def _reference_an_undeclared_layer(contract: dict[str, Any]) -> None:
    """Point a declared reference at a layer name nothing declares.

    Any `reference` would do; `field:swc.port.interface` is chosen for stability.
    """
    _contract_field_decls(contract)[("port", "interface")]["reference"]["layer"] = "subsystems"


def _reference_a_layer_by_the_wrong_field(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("value_type", "base")]["reference"]["identityField"] = "id"


def _declare_an_undeclared_member_kind(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("swc", "ports")]["collection"]["memberKinds"] = ["portal"]


def _declare_an_undeclared_element_kind(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("connector", "to")]["elementKind"] = "endpoint"


def _discriminate_a_kind_by_a_missing_field(contract: dict[str, Any]) -> None:
    _contract_kind_decls(contract)["value_type"]["discriminatorField"] = "flavour"


def _discriminate_members_by_a_missing_field(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("runnable", "events")]["collection"]["discriminatorField"] = "flavour"


def _discriminate_a_single_member_collection(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("runnable", "events")]["collection"]["memberKinds"] = ["timing_event"]


def _declare_a_bare_kind_name_twice(contract: dict[str, Any]) -> None:
    _contract_kind_decls(contract)["sequence"]["kind"] = "port"


def _identify_a_second_layer_by_id(contract: dict[str, Any]) -> None:
    _contract_layer_decls(contract)["sequences"]["identityField"] = "id"


def _compose_a_file_name_from_another_field(contract: dict[str, Any]) -> None:
    _contract_layer_decls(contract)["requirements"]["storage"]["filenameFrom"] = "name"


CROSS_REFERENCE_PERTURBATIONS: tuple[tuple[str, str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "reference",
        "an undeclared target layer",
        _reference_an_undeclared_layer,
        "which is not declared",
    ),
    (
        "reference",
        "the wrong identity field",
        _reference_a_layer_by_the_wrong_field,
        "identity field is 'name'",
    ),
    (
        "memberKinds",
        "an undeclared member kind",
        _declare_an_undeclared_member_kind,
        "is not a declared kind",
    ),
    (
        "elementKind",
        "an undeclared element kind",
        _declare_an_undeclared_element_kind,
        "declares elementKind",
    ),
    (
        "discriminatorField",
        "a kind discriminator naming no field",
        _discriminate_a_kind_by_a_missing_field,
        "which the kind does not declare",
    ),
    (
        "discriminatorField",
        "a collection discriminator naming no field",
        _discriminate_members_by_a_missing_field,
        "does not declare",
    ),
    (
        "discriminatorArity",
        "a discriminator over a single member kind",
        _discriminate_a_single_member_collection,
        "there is nothing to tell apart",
    ),
    (
        "kindNames",
        "a bare kind name declared twice",
        _declare_a_bare_kind_name_twice,
        "are declared more than once",
    ),
    (
        "identity",
        "a second layer identified by id",
        _identify_a_second_layer_by_id,
        "exactly once",
    ),
    (
        "identity",
        "a file name composed from another field",
        _compose_a_file_name_from_another_field,
        "composes its file name from",
    ),
)


@pytest.mark.parametrize(
    ("rule", "case", "perturb", "expected"),
    CROSS_REFERENCE_PERTURBATIONS,
    ids=[f"{rule}: {case}" for rule, case, _, _ in CROSS_REFERENCE_PERTURBATIONS],
)
def test_a_perturbed_cross_reference_is_caught(
    rule: str,
    case: str,
    perturb: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    contract = _shipped()
    perturb(contract)
    failures = cross_reference_failures(contract)
    assert any(expected in failure for failure in failures), (rule, case, failures)


# ---------------------------------------------------------------------------
# No Python names in the contract's structural fields
# ---------------------------------------------------------------------------
#
# The rule covers the STRUCTURAL positions only: `id`, `name`, `type`,
# `elementKind`, `enum` members, `reference.layer`, `storage.directory` and a
# rule's `id` and `appliesTo`.
#
# `description`, `absentDescription`, `unit` and a rule's `summary` are
# deliberately EXEMPT: those strings are **shipped text** — carried verbatim in
# the `discover` vectors and shown as the editor's hover — and a blanket scan
# would make a quotation a structural defect. The control below is a
# perturbation: a sentence naming Python put into a description, where the scan
# must stay green, and into a structural value, where it must not.

#: The keys whose values the scan below never reads, stated as data.
STRUCTURAL_EXEMPT_KEYS = ("description", "absentDescription", "unit", "summary")


#: Every position `structural_values` reads, in the order it reads them; the
#: self-test compares positions, not values, because several positions carry a
#: value another also produces (`"swc"` is a layer name and a storage directory).
STRUCTURAL_POSITIONS = (
    "layer.id",
    "layer.name",
    "layer.storage.directory",
    "kind.id",
    "kind.kind",
    "field.id",
    "field.name",
    "field.type",
    "field.elementKind",
    "field.reference.layer",
    "field.enum",
    "rule.id",
    "rule.appliesTo",
)


def structural_values(contract: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Every structural string of the contract, as `(where, position, value)` triples."""
    values: list[tuple[str, str, str]] = []
    for layer in contract["layers"]:
        values.append((layer["id"], "layer.id", layer["id"]))
        values.append((layer["id"], "layer.name", layer["name"]))
        values.append((layer["id"], "layer.storage.directory", layer["storage"]["directory"]))
        for kind in layer["kinds"]:
            values.append((kind["id"], "kind.id", kind["id"]))
            values.append((kind["id"], "kind.kind", kind["kind"]))
            for field in kind["fields"]:
                values.append((field["id"], "field.id", field["id"]))
                values.append((field["id"], "field.name", field["name"]))
                values.append((field["id"], "field.type", field["type"]))
                if "elementKind" in field:
                    values.append((field["id"], "field.elementKind", field["elementKind"]))
                if "reference" in field:
                    values.append(
                        (
                            field["id"],
                            "field.reference.layer",
                            field["reference"]["layer"],
                        )
                    )
                values.extend(
                    (field["id"], "field.enum", member) for member in field.get("enum", []) if isinstance(member, str)
                )
    for rule in contract["rules"]:
        # A rule identifier and its `appliesTo` are where a Python name would most
        # plausibly leak: an identifier minted from a validator's method name.
        values.append((rule["id"], "rule.id", rule["id"]))
        values.extend((rule["id"], "rule.appliesTo", entry) for entry in rule["appliesTo"])
    return values


#: The `(where, position)` pairs whose values quote an external standard instead
#: of the model's own vocabulary, and are therefore exempt from the lower-snake
#: branch alone. There are **two quoted vocabularies**:
#:
#: * `field:hardware.memory.type`'s enum members are the AUTOSAR ECU Resource
#:   Template's `memoryType` literals in the standard's own spelling (the vendor
#:   survey § 4.4 chose them), so a future export of the layer is lossless.
#: * the safety fields' enum members are ISO 26262-1:2018's own spelling of
#:   the safety integrity levels — `QM` and `A`…`D` in capitals, with no
#:   lower-case form anywhere. The rated `asil` fields carry **the rated set** —
#:   those five and the thirteen decomposed spellings `QM(A)` … `D(D)` of
#:   `[TPS_SAFEX_00201]` (AUTOSAR Safety Extensions R21-11, p. 18) — and the
#:   `asil_capability` fields **the capability set**, the five.
#:
#: The exemption is keyed on the pair, not on the value, so it reaches the enum
#: members of those fields and nothing else: a capitalized layer, kind or field
#: *name* is still refused, a capitalized enum member of any other field is still
#: refused, and the exempted values are still read by the class-name, module-name
#: and attribute-spelling branches. A later ASIL field adds its own entry rather
#: than inheriting a widened rule; another entry here is a decision, not a fix.
QUOTED_VOCABULARY_POSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("field:hardware.memory.type", "field.enum"),
        ("field:requirements.requirement.asil", "field.enum"),
        ("field:swc.swc.asil", "field.enum"),
        # Quoted from ISO 26262 for the level of the information a data element carries.
        ("field:interfaces.data_element.asil", "field.enum"),
        # Quoted from ISO 26262 for the level a core or a memory may host.
        ("field:hardware.core.asil_capability", "field.enum"),
        ("field:hardware.memory.asil_capability", "field.enum"),
        # A partition is rated like a component, not a capability: the rated set
        # of `[TPS_SAFEX_00201]`.
        ("field:hardware.partition.asil", "field.enum"),
    }
)


def python_name_failures(contract: dict[str, Any]) -> list[str]:
    """Every structural value that names a Python symbol.

    Three signatures, because the three ways a Python name leaks in look
    different: a `BaseModel` class name from `schema.py`, a module file name,
    and the one attribute spelling that differs from its serialization alias.
    An uppercase letter anywhere is refused on top of those, since every name
    in the model's own vocabulary is lower snake case — except at the positions
    `QUOTED_VOCABULARY_POSITIONS` names, where the value is a quotation from a
    standard rather than a word this model coined.
    """
    class_names = {name for name, obj in vars(schema).items() if isinstance(obj, type) and issubclass(obj, BaseModel)}
    attribute_names = set(load_binding()["fieldNames"].values())
    failures: list[str] = []
    for where, position, value in structural_values(contract):
        named = sorted(name for name in class_names if name in value)
        if named:
            failures.append(f"{where}: the structural value {value!r} names the Python class(es) {named}")
        if ".py" in value:
            failures.append(f"{where}: the structural value {value!r} names a Python module")
        if any(value.endswith(attribute) for attribute in attribute_names):
            failures.append(f"{where}: the structural value {value!r} uses a Python attribute spelling")
        if value != value.lower() and (where, position) not in QUOTED_VOCABULARY_POSITIONS:
            failures.append(f"{where}: the structural value {value!r} is not in the model's lower-snake vocabulary")
    return failures


def test_no_structural_contract_value_names_a_python_symbol() -> None:
    """The contract's structural surface is the model's vocabulary; only `binding.json` names Python."""
    assert python_name_failures(load_contract()) == []


#: The field names whose declared `enum` is the ASIL vocabulary: `asil` (a rated
#: level) and `asil_capability` (the level hardware may carry). Keyed on names so
#: a later field of either name inherits the gate below with no edit.
ASIL_FIELD_NAMES: frozenset[str] = frozenset({"asil", "asil_capability"})


def _asil_field_enums(contract: dict[str, Any]) -> dict[str, list[str]]:
    """`field id -> its declared enum`, for every field named in :data:`ASIL_FIELD_NAMES`."""
    return {
        field["id"]: field.get("enum", [])
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if field["id"].rsplit(".", 1)[-1] in ASIL_FIELD_NAMES
    }


#: Which of :data:`ASIL_FIELD_NAMES` carries which value set: a rated level
#: (`asil`) is one of :data:`safety.ASIL_VALUES`, the
#: eighteen of `[TPS_SAFEX_00201]`; a capability (`asil_capability`) is one of
#: :data:`safety.ASIL_LEVELS`, the five plain levels, because a capability is
#: not decomposed.
ASIL_VALUE_SET_BY_NAME: dict[str, tuple[str, ...]] = {
    "asil": safety.ASIL_VALUES,
    "asil_capability": safety.ASIL_LEVELS,
}


def _asil_vocabulary_failures(contract: dict[str, Any]) -> list[str]:
    """Every ASIL-named field whose `enum` is not its name's leaf tuple, order included."""
    failures: list[str] = []
    for field_id, enum in sorted(_asil_field_enums(contract).items()):
        expected = ASIL_VALUE_SET_BY_NAME[field_id.rsplit(".", 1)[-1]]
        if tuple(enum) != expected:
            failures.append(f"{field_id} declares {enum!r}, not {list(expected)!r}")
    return failures


def test_every_asil_field_declares_exactly_the_leaf_modules_vocabulary() -> None:
    """Every `.asil` field's enum is `safety.ASIL_VALUES` and every `.asil_capability`'s `safety.ASIL_LEVELS`.

    The leaf repeats the membership the contract holds, so this keeps the two
    spellings from drifting. Order is included: the rated set's is the
    **standard's listing order** (`[TPS_SAFEX_00201]`, R21-11 p. 18), not the
    rank (`B(D)` ranks with `B` and is listed after `QM(A)`); the capability
    set's is the rank, increasing. It fails rather than passes vacuously when
    no field of either name is declared.
    """
    declared = _asil_field_enums(load_contract())

    assert declared, f"the contract declares no field named any of {sorted(ASIL_FIELD_NAMES)}"
    assert {field_id.rsplit(".", 1)[-1] for field_id in declared} == set(ASIL_VALUE_SET_BY_NAME), (
        "a value set has no field to gate"
    )
    assert _asil_vocabulary_failures(load_contract()) == []


def test_the_asil_vocabulary_gate_is_not_inert() -> None:
    """A contract whose enum disagrees with the leaf is reported, for each set, reversed or swapped."""
    contract = load_contract()
    assert set(_asil_field_enums(contract)) == {
        field["id"]
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if field["name"] in ASIL_FIELD_NAMES
    }

    for name, other in (("asil", "asil_capability"), ("asil_capability", "asil")):
        for perturb in (
            lambda enum: list(reversed(enum)),
            lambda enum, other=other: list(ASIL_VALUE_SET_BY_NAME[other]),
        ):
            perturbed = copy.deepcopy(contract)
            for layer in perturbed["layers"]:
                for kind in layer["kinds"]:
                    for field in kind["fields"]:
                        if field["name"] == name:
                            field["enum"] = perturb(field["enum"])
            failures = _asil_vocabulary_failures(perturbed)
            assert failures, f"a perturbed {name} enum went unreported"
            assert all(failure.split(" ", 1)[0].endswith(f".{name}") for failure in failures), failures


def _name_a_python_class(contract: dict[str, Any]) -> None:
    _contract_kind_decls(contract)["connector_endpoint"]["kind"] = "ConnectorEndpoint"


def _name_a_python_module(contract: dict[str, Any]) -> None:
    _contract_layer_decls(contract)["swc"]["storage"]["directory"] = "schema.py"


def _use_the_python_attribute_spelling(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("connector", "from")]["name"] = "from_"


def _leave_a_structural_value_capitalized(contract: dict[str, Any]) -> None:
    _contract_field_decls(contract)[("task", "priority")]["name"] = "Priority"


PYTHON_NAME_PERTURBATIONS: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
    ("a Python class name", _name_a_python_class, "names the Python class"),
    ("a Python module name", _name_a_python_module, "names a Python module"),
    (
        "the Python attribute spelling",
        _use_the_python_attribute_spelling,
        "uses a Python attribute spelling",
    ),
    (
        "a value outside the lower-snake vocabulary",
        _leave_a_structural_value_capitalized,
        "lower-snake vocabulary",
    ),
)


@pytest.mark.parametrize(
    ("case", "perturb", "expected"),
    PYTHON_NAME_PERTURBATIONS,
    ids=[case for case, _, _ in PYTHON_NAME_PERTURBATIONS],
)
def test_a_python_name_in_a_structural_field_is_caught(
    case: str,
    perturb: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    contract = _shipped()
    perturb(contract)
    assert any(expected in failure for failure in python_name_failures(contract)), case


#: The rated set as `[TPS_SAFEX_00201]` spells it (AUTOSAR Safety Extensions
#: R21-11, p. 18), transcribed here rather than read from `safety.ASIL_VALUES`
#: so that this exemption test does not agree with the leaf by construction.
RATED_SET: frozenset[str] = frozenset(
    {
        "QM", "A", "B", "C", "D",
        "QM(A)", "QM(B)", "QM(C)", "QM(D)",
        "A(B)", "A(C)", "A(D)",
        "B(B)", "B(C)", "B(D)",
        "C(C)", "C(D)",
        "D(D)",
    }
)  # fmt: skip

#: The capability set — the five plain levels a core's and a memory's
#: ``asil_capability`` may hold, which are not decomposed — transcribed here for
#: the same reason as :data:`RATED_SET` rather than read from
#: `safety.ASIL_LEVELS`.
CAPABILITY_SET: frozenset[str] = frozenset({"QM", "A", "B", "C", "D"})


def test_the_quoted_vocabulary_exemption_is_exactly_the_two_quoted_vocabularies() -> None:
    """Each exempted position carries exactly its standard's values, and nothing else is capitalized.

    The positions: the four `memoryType` literals, the eighteen levels of
    `[TPS_SAFEX_00201]` on the rated `asil` positions, the five plain levels on
    the `asil_capability` positions. Asserted **per position**, because a union
    would stay green with the vocabularies swapped between their fields.
    """
    contract = load_contract()
    exempted: dict[tuple[str, str], set[str]] = {}
    for where, position, value in structural_values(contract):
        if (where, position) in QUOTED_VOCABULARY_POSITIONS:
            exempted.setdefault((where, position), set()).add(value)
    assert exempted == {
        ("field:hardware.memory.type", "field.enum"): {"RAM", "ROM", "EEPROM", "Flash"},
        ("field:requirements.requirement.asil", "field.enum"): RATED_SET,
        ("field:swc.swc.asil", "field.enum"): RATED_SET,
        ("field:interfaces.data_element.asil", "field.enum"): RATED_SET,
        ("field:hardware.core.asil_capability", "field.enum"): CAPABILITY_SET,
        ("field:hardware.memory.asil_capability", "field.enum"): CAPABILITY_SET,
        ("field:hardware.partition.asil", "field.enum"): RATED_SET,
    }

    remaining = {
        (where, position, value)
        for where, position, value in structural_values(contract)
        if value != value.lower() and (where, position) not in QUOTED_VOCABULARY_POSITIONS
    }
    assert remaining == set()


def test_a_capitalized_enum_member_of_another_field_is_still_refused() -> None:
    """The exemption is keyed on the position: a capitalized `enforcement` member beside `type` is refused."""
    contract = _shipped()
    field = _contract_field_decls(contract)[("memory", "enforcement")]
    field["enum"] = ["Strict", "advisory"]
    assert any(
        "field:hardware.memory.enforcement" in failure and "lower-snake vocabulary" in failure
        for failure in python_name_failures(contract)
    )


def test_the_exempted_field_is_still_scanned_at_its_other_positions() -> None:
    """Exempting `field.enum` does not exempt the field's `name` or its `id`."""
    contract = _shipped()
    _contract_field_decls(contract)[("memory", "type")]["name"] = "Type"
    assert any("lower-snake vocabulary" in failure for failure in python_name_failures(contract))


#: A description naming Python: the sentence the prose exemption is written for.
_PYTHON_NAMING_PROSE = "Serialized under the alias `from`; `schema.py` spells the attribute `from_`."


def test_the_prose_exemption_is_load_bearing_and_the_shipped_prose_is_carried_verbatim() -> None:
    """Prose naming Python passes the scan, the same text in a structural position does not, and prose ships verbatim.

    The carrying half: every kind and field description appears verbatim in its
    own layer's `discover-layer-<layer>-full.json` vector (in exactly that one
    where the text is unique) and in no default `summary` vector, so a reworded
    description fails until its vector is re-recorded, and a summary that
    shipped descriptions again would fail too.
    """
    contract = _shipped()
    field = _contract_field_decls(contract)[("connector", "from")]
    field["description"] = _PYTHON_NAMING_PROSE
    assert python_name_failures(contract) == [], "prose is exempt: the scan never reads a description"
    assert _PYTHON_NAMING_PROSE not in {value for _, _, value in structural_values(contract)}
    _contract_field_decls(contract)[("connector", "from")]["name"] = _PYTHON_NAMING_PROSE
    assert any("names a Python module" in failure for failure in python_name_failures(contract))

    shipped = _contract_field_decls(load_contract())[("connector", "from")]["description"]
    assert "schema.py" not in shipped and "from_" not in shipped, "worded for the hover reader"
    examples = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol" / "examples"
    assert not (examples / "discover-project-full.json").exists(), "the project-wide `full` is a negative since 28.1"
    contract_document = load_contract()
    full_vectors = {
        layer["name"]: json.dumps(
            json.loads(
                (examples / f"discover-layer-{layer['name'].replace('_', '-')}-full.json").read_text(encoding="utf-8")
            ),
            ensure_ascii=False,
        )
        for layer in contract_document["layers"]
    }
    # One `full` vector per declared layer; moves with the contract's layer count.
    assert len(full_vectors) == 15
    assert not (examples / "discover-layer-systems-full.json").exists(), "the layer left with contract `6.0.0`"
    summary = json.loads((examples / "discover-project-all-layers-present.json").read_text(encoding="utf-8"))
    summary_text = json.dumps(summary, ensure_ascii=False)
    assert shipped in full_vectors["connectors"], '`form: "full"` replays the shipped prose verbatim'
    assert shipped not in summary_text, "the default form strips it"

    # A description two layers share word for word is legitimately replayed by
    # both, so "exactly one" is asserted only where the text is unique.
    declared_texts = [
        description
        for layer in contract_document["layers"]
        for kind in layer["kinds"]
        for description in [kind.get("description"), *(field.get("description") for field in kind["fields"])]
        if description
    ]
    for layer in contract_document["layers"]:
        for kind in layer["kinds"]:
            described = [(kind["id"], kind.get("description"))] + [
                (field["id"], field.get("description")) for field in kind["fields"]
            ]
            for identifier, description in described:
                if not description:
                    continue
                quoted = json.dumps(description, ensure_ascii=False)
                carriers = [name for name, text in full_vectors.items() if quoted in text]
                if declared_texts.count(description) == 1:
                    assert carriers == [layer["name"]], (
                        f"{identifier}: replayed by {carriers}, expected {[layer['name']]}"
                    )
                else:
                    assert layer["name"] in carriers, f"{identifier}: not replayed by its own layer's `full` vector"
                assert quoted not in summary_text, f"{identifier}: the default form ships its prose"


def test_the_structural_scan_reaches_every_position_the_rule_names() -> None:
    """Every branch of `structural_values` is reached, compared by position.

    Guards a deleted branch hidden by a value another branch also produces.
    """
    scanned = {(position, value) for _, position, value in structural_values(load_contract())}
    reached = {position for position, _ in scanned}
    assert reached == set(STRUCTURAL_POSITIONS), sorted(reached ^ set(STRUCTURAL_POSITIONS))
    for position, value in (
        ("layer.id", "layer:requirements"),
        ("layer.name", "swc"),
        ("layer.storage.directory", "swc"),
        ("kind.id", "kind:implementation_types.value_type"),
        ("kind.kind", "connector_endpoint"),
        ("field.id", "field:connectors.connector.from"),
        ("field.name", "from"),
        ("field.type", "list[timing_event | init_event | operation_event | data_event | call_returned_event]"),
        ("field.elementKind", "connector_endpoint"),
        ("field.reference.layer", "implementation_types"),
        ("field.enum", "ecu_abstraction"),
        ("rule.id", "rule:connector.from-not-provide"),
        ("rule.appliesTo", "field:swc.port.direction"),
    ):
        assert (position, value) in scanned, position
    assert STRUCTURAL_EXEMPT_KEYS == (
        "description",
        "absentDescription",
        "unit",
        "summary",
    )


# ---------------------------------------------------------------------------
# The rule vocabulary and its bindings
# ---------------------------------------------------------------------------
#
# Three families, three oracles, and one rule: **the contract adopts a
# vocabulary, it never mints a parallel one**.
#
# - `cross-element` is `validator.CONSISTENCY_RULES`, adopted verbatim with one
#   `rule:` prefix in front of each key, held equal in both directions.
# - `element-local` is one identifier per `@field_validator` /
#   `@model_validator` in `schema.py`, minted here, so a validator added in
#   Python without a contract identifier fails.
# - `schema-config` is the one `model_config` key with validation meaning. Its
#   `appliesTo` is **derived** from the structural projection and compared both
#   ways, because a hand-written list of float fields would go stale.
#
# The binding is checked for **honesty**, not merely for presence. Some
# cross-element rules are not produced by a `ConsistencyResult` entry point at
# all — the `connector.*` compatibility rules are minted by
# `check_connector_compatibility`, and the budget rules are table rows keyed by
# a `resource_analysis` finding code — so a gate over the function inventory
# would miss them or force a false binding. What is asserted instead is that
# the named callable's own source contains the rule identifier.

#: `model_config` keys that carry no validation meaning, with the reason each
#: is exempt. Empty today. A named list rather than a branch, so the next key
#: added to that config fails until someone records which side it falls on.
MODEL_CONFIG_WITHOUT_VALIDATION_MEANING: dict[str, str] = {}

#: `number` as a whole word anywhere in a projected type name, so the predicate
#: reaches a float inside a collection as well as a scalar one: Pydantic applies
#: `allow_inf_nan=False` to every float it parses, `list[float]` members
#: included. The word boundary keeps `map[string, integer]` and the like out.
_NUMBER_IN_TYPE = re.compile(r"\bnumber\b")

#: `configKey` -> the projection predicate that derives the field set the key
#: governs: `allow_inf_nan` governs every projected field whose type holds `number`.
CONFIG_GOVERNED_FIELDS: dict[str, Callable[[Any], bool]] = {
    "allow_inf_nan": lambda projection: _NUMBER_IN_TYPE.search(projection.type) is not None,
}


def _rules_of(contract: dict[str, Any], family: str) -> list[dict[str, Any]]:
    return [rule for rule in contract["rules"] if rule["family"] == family]


def _entry_identifiers(contract: dict[str, Any]) -> set[str]:
    """Every layer, kind and field identifier the contract declares."""
    entries: set[str] = set()
    for layer in contract["layers"]:
        entries.add(layer["id"])
        for kind in layer["kinds"]:
            entries.add(kind["id"])
            entries.update(field["id"] for field in kind["fields"])
    return entries


# --- the vocabulary-equality gate -------------------------------------------


def _validator_key(identifier: str) -> str:
    """A rule *identifier* with exactly one required `rule:` stripped: the validator's key.

    Not `removeprefix`, which would let an unprefixed identifier compare equal.
    A doubled `rule:rule:` survives into the comparison and fails there. Every
    comparison of a contract identifier with a validator key here goes through
    this.
    """
    assert identifier.startswith("rule:"), identifier
    return identifier[len("rule:") :]


def cross_element_keys(contract: dict[str, Any]) -> list[str]:
    """The cross-element rule identifiers as validator keys (:func:`_validator_key`)."""
    return [_validator_key(rule["id"]) for rule in _rules_of(contract, "cross-element")]


def test_the_cross_element_vocabulary_is_the_validators_own() -> None:
    """The contract's cross-element rules are `CONSISTENCY_RULES`, exactly, in both directions."""
    declared = cross_element_keys(load_contract())
    assert len(declared) == len(set(declared)), "a cross-element identifier is declared twice"
    assert set(declared) == set(validator.CONSISTENCY_RULES), (
        f"only in the contract: {sorted(set(declared) - set(validator.CONSISTENCY_RULES))}; "
        f"only in the validator: {sorted(set(validator.CONSISTENCY_RULES) - set(declared))}"
    )


def test_the_cross_element_count_is_the_validators_own() -> None:
    """Stated as the validator's own length, never as a remembered figure."""
    assert len(cross_element_keys(load_contract())) == len(validator.CONSISTENCY_RULES)


def test_every_cross_element_summary_is_the_validators_description() -> None:
    """Every cross-element summary is the validator's description verbatim, not a paraphrase."""
    mismatched = [
        rule["id"]
        for rule in _rules_of(load_contract(), "cross-element")
        if rule["summary"] != validator.CONSISTENCY_RULES[rule["id"][len("rule:") :]]
    ]
    assert mismatched == []


def _rename_a_cross_element_rule(contract: dict[str, Any]) -> None:
    contract["rules"][0]["id"] = "rule:cross-layer.swc-without-any-requirement-link"


def _drop_a_cross_element_rule(contract: dict[str, Any]) -> None:
    contract["rules"].remove(_rules_of(contract, "cross-element")[0])


def _invent_a_cross_element_rule(contract: dict[str, Any]) -> None:
    contract["rules"].append(
        {
            "id": "rule:connector.endpoint-unreachable",
            "family": "cross-element",
            "appliesTo": ["layer:connectors"],
            "summary": "A rule no validator has.",
        }
    )


def _double_the_rule_prefix(contract: dict[str, Any]) -> None:
    contract["rules"][0]["id"] = "rule:rule:trace.link-unknown-requirement"


VOCABULARY_PERTURBATIONS: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
    ("a renamed rule", _rename_a_cross_element_rule),
    ("a dropped rule", _drop_a_cross_element_rule),
    ("a rule the validator does not have", _invent_a_cross_element_rule),
    ("a doubled `rule:` prefix", _double_the_rule_prefix),
)


@pytest.mark.parametrize(
    ("case", "perturb"),
    VOCABULARY_PERTURBATIONS,
    ids=[case for case, _ in VOCABULARY_PERTURBATIONS],
)
def test_a_perturbed_vocabulary_fails_the_equality_gate(
    case: str,
    perturb: Callable[[dict[str, Any]], None],
) -> None:
    contract = _shipped()
    perturb(contract)
    assert set(cross_element_keys(contract)) != set(validator.CONSISTENCY_RULES), case


def test_an_unprefixed_rule_identifier_is_refused_rather_than_stripped() -> None:
    """An unprefixed rule identifier is refused, so the prefix cannot become optional."""
    contract = _shipped()
    contract["rules"][0]["id"] = "trace.link-unknown-requirement"
    with pytest.raises(AssertionError):
        cross_element_keys(contract)


# --- the severity a rule declares --------------------------------------------

#: The rule the perturbations below move, chosen by identifier rather than by
#: position: a perturbation on `rules[0]` would go inert the day that rule
#: really became a warning.
_PERTURBED_SEVERITY_RULE = "rule:connector.duplicate-name"


def _rule_by_id(contract: dict[str, Any], identifier: str) -> dict[str, Any]:
    matches = [rule for rule in contract["rules"] if rule["id"] == identifier]
    assert len(matches) == 1, identifier
    return matches[0]


def warning_rule_keys(contract: dict[str, Any]) -> list[str]:
    """The identifiers declaring `severity: "warning"`, of any family, as validator keys (:func:`_validator_key`)."""
    return [_validator_key(rule["id"]) for rule in contract["rules"] if rule.get("severity") == "warning"]


def severity_failures(contract: dict[str, Any], warning_rules: frozenset[str]) -> list[str]:
    """Every rule the contract and `validator.WARNING_RULES` disagree about, in both directions, naming the side."""
    declared = set(warning_rule_keys(contract))
    failures = [
        f"{rule}: declared `severity: warning` in the contract, absent from validator.WARNING_RULES"
        for rule in sorted(declared - warning_rules)
    ]
    failures.extend(
        f"{rule}: in validator.WARNING_RULES, not declared `severity: warning` in the contract"
        for rule in sorted(warning_rules - declared)
    )
    return failures


def test_the_warning_rules_are_the_contracts_own() -> None:
    """`validator.WARNING_RULES` equals the contract's `severity: warning` rules, both directions.

    The contract is normative; the perturbations below prove the gate is not inert.
    """
    assert severity_failures(load_contract(), validator.WARNING_RULES) == []


#: A removed rule: an architecture element that satisfies no requirement is no
#: finding. Only the requirement's direction, ``trace.requirement-unallocated``,
#: stays checked.
_RETIRED_SWC_LINK_RULE = "cross-layer.swc-without-requirement-link"


def test_the_retired_swc_link_rule_is_declared_nowhere() -> None:
    """The removed identifier is in neither the contract, the binding nor the validator's two tables.

    **What this does not reach:** prose naming the rule as current behaviour.
    """
    contract_ids = {rule["id"] for rule in load_contract()["rules"]}
    assert f"rule:{_RETIRED_SWC_LINK_RULE}" not in contract_ids
    assert f"rule:{_RETIRED_SWC_LINK_RULE}" not in load_binding()["rules"]
    assert _RETIRED_SWC_LINK_RULE not in validator.CONSISTENCY_RULES
    assert _RETIRED_SWC_LINK_RULE not in validator.WARNING_RULES


def test_a_warning_declared_in_the_contract_alone_is_caught() -> None:
    contract = _shipped()
    _rule_by_id(contract, _PERTURBED_SEVERITY_RULE)["severity"] = "warning"

    failures = severity_failures(contract, validator.WARNING_RULES)

    assert failures == [
        "connector.duplicate-name: declared `severity: warning` in the contract, absent from validator.WARNING_RULES"
    ]


def test_a_warning_in_the_python_set_alone_is_caught() -> None:
    perturbed = validator.WARNING_RULES | {"connector.duplicate-name"}

    failures = severity_failures(load_contract(), perturbed)

    assert failures == [
        "connector.duplicate-name: in validator.WARNING_RULES, not declared `severity: warning` in the contract"
    ]


def test_every_warning_rule_names_a_declared_rule() -> None:
    """A member of the set that no check can emit would be a class with no finding."""
    assert validator.WARNING_RULES <= set(validator.CONSISTENCY_RULES)


#: The rule groups whose composed check a surface reads the error half of alone
#: (`issues` / `records`, never `.warnings`): the MCP
#: `check_connector_consistency` tool (`connector.*`) and the two
#: `resource_report` surfaces (`cli/read_commands.py`, `server.py`) reading
#: `check_hardware_topology(...).records` (`hardware.*`). A warning rule of
#: either group would be a finding those surfaces drop. The group is the rule
#: key before its first `.`, not a call graph: every rule the two checks mint
#: carries its group's prefix.
_GROUPS_READ_ERROR_HALF_ONLY = ("connector", "hardware")


def _rules_of_the_error_half_only_consumers(contract: dict[str, Any]) -> set[str]:
    keys = (_validator_key(rule["id"]) for rule in contract["rules"])
    return {key for key in keys if key.partition(".")[0] in _GROUPS_READ_ERROR_HALF_ONLY}


def test_no_warning_rule_belongs_to_a_family_a_consumer_reads_the_error_half_of_alone() -> None:
    """The gate behind the connector tool's docstring and the report's `records` read."""
    read_as_errors_only = _rules_of_the_error_half_only_consumers(load_contract())
    assert len(read_as_errors_only) > 1, "the two groups went empty; the gate reads nothing"
    assert validator.WARNING_RULES.isdisjoint(read_as_errors_only), sorted(
        validator.WARNING_RULES & read_as_errors_only
    )


def test_a_warning_rule_in_such_a_family_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    perturbed = validator.WARNING_RULES | {_validator_key(_PERTURBED_SEVERITY_RULE)}
    validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", perturbed)

    with pytest.raises(AssertionError):
        test_no_warning_rule_belongs_to_a_family_a_consumer_reads_the_error_half_of_alone()


def test_the_meta_schema_admits_the_warning_value() -> None:
    contract = _shipped()
    _rule_by_id(contract, _PERTURBED_SEVERITY_RULE)["severity"] = "warning"

    assert validation_messages(meta_schema_validator(), contract) == []


def test_the_meta_schema_refuses_a_spelled_error_severity() -> None:
    """Absent means error: a spelled `severity: error` fails the meta-schema rather than restating the default."""
    contract = _shipped()
    _rule_by_id(contract, _PERTURBED_SEVERITY_RULE)["severity"] = "error"

    messages = validation_messages(meta_schema_validator(), contract)

    assert len(messages) == 1
    assert messages[0].endswith("/severity: 'error' is not one of ['warning']"), messages


# --- every rule resolves, and every identifier is unique ---------------------


def rule_resolution_failures(contract: dict[str, Any], binding: dict[str, Any]) -> list[str]:
    """Every declared rule resolves through the binding to something real.

    Deliberately NOT "names a `ConsistencyResult` function": some rules
    legitimately do not, and that assertion is only satisfiable by a false
    binding. What is resolved is the form the binding used — a module-level
    callable, a validator method on a bound class, a `model_config` key, or a
    row of a declared table.
    """
    failures: list[str] = []
    for rule in contract["rules"]:
        rule_id = rule["id"]
        bound = binding["rules"].get(rule_id)
        if bound is None:
            failures.append(f"{rule_id}: declared with no binding")
            continue
        if "callable" in bound:
            module = importlib.import_module(bound["module"])
            owner = getattr(module, bound["callable"], None)
            if not callable(owner):
                failures.append(f"{rule_id}: {bound['module']}.{bound['callable']} is not an importable callable")
        elif "method" in bound:
            kind_binding = binding["kinds"].get(bound["kind"])
            if kind_binding is None:
                failures.append(f"{rule_id}: bound to the undeclared kind {bound['kind']}")
                continue
            model_class = getattr(importlib.import_module(kind_binding["module"]), kind_binding["class"])
            decorators = model_class.__pydantic_decorators__
            if (
                bound["method"] not in decorators.field_validators
                and bound["method"] not in decorators.model_validators
            ):
                failures.append(f"{rule_id}: {kind_binding['class']}.{bound['method']} is not a validator")
        elif "configKey" in bound:
            model_class = getattr(importlib.import_module(bound["module"]), bound["class"], None)
            if model_class is None:
                failures.append(f"{rule_id}: {bound['module']}.{bound['class']} does not exist")
            elif bound["configKey"] not in model_class.model_config:
                failures.append(f"{rule_id}: {bound['class']}.model_config has no key {bound['configKey']!r}")
        else:
            module = importlib.import_module(bound["module"])
            table = getattr(module, bound["table"], None)
            if table is None:
                failures.append(f"{rule_id}: {bound['module']}.{bound['table']} does not exist")
            elif bound["key"] not in table:
                failures.append(f"{rule_id}: {bound['table']} has no row {bound['key']!r}")
    return failures


def test_every_declared_rule_resolves_through_the_binding() -> None:
    assert rule_resolution_failures(load_contract(), load_binding()) == []


def test_every_rule_identifier_is_declared_once() -> None:
    """No rule identifier is declared twice; `rules` is an array, unlike `binding.json`'s map."""
    identifiers = [rule["id"] for rule in load_contract()["rules"]]
    duplicated = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    assert duplicated == []


def test_the_rule_inventory_is_the_three_families_and_nothing_else() -> None:
    """Three families, each counted from its own oracle and never from a figure."""
    contract = load_contract()
    assert len(_rules_of(contract, "cross-element")) == len(validator.CONSISTENCY_RULES)
    assert len(_rules_of(contract, "element-local")) == len(_schema_validator_definitions())
    assert len(_rules_of(contract, "schema-config")) == len(model_element_config_keys()) - len(
        MODEL_CONFIG_WITHOUT_VALIDATION_MEANING
    )
    assert len(contract["rules"]) == sum(
        len(_rules_of(contract, family)) for family in ("cross-element", "element-local", "schema-config")
    )


def _drop_a_rule_binding(binding: dict[str, Any]) -> None:
    del binding["rules"]["rule:trace.link-unknown-requirement"]


def _bind_a_rule_to_an_absent_callable(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:trace.link-unknown-requirement"]["callable"] = "check_nothing_at_all"


def _bind_a_validator_to_an_undeclared_kind(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:tasks.task.partition.not-blank"]["kind"] = "kind:tasks.gadget"


def _bind_a_rule_to_a_method_that_is_not_a_validator(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:tasks.task.partition.not-blank"]["method"] = "model_dump"


def _bind_a_config_rule_to_an_absent_class(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:model.float-finite"]["class"] = "NoSuchElement"


def _bind_a_config_rule_to_an_absent_key(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:model.float-finite"]["configKey"] = "str_max_length"


def _bind_a_rule_to_an_absent_table(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:hardware.missing-budget"]["table"] = "_NO_SUCH_TABLE"


def _bind_a_rule_to_an_absent_table_row(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:hardware.missing-budget"]["key"] = "no_such_finding"


#: One perturbed binding per resolution branch, named by the binding form it exercises.
RULE_RESOLUTION_PERTURBATIONS: tuple[tuple[str, str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "absent",
        "a declared rule with no binding row",
        _drop_a_rule_binding,
        "declared with no binding",
    ),
    (
        "callable",
        "a callable the module does not export",
        _bind_a_rule_to_an_absent_callable,
        "is not an importable callable",
    ),
    (
        "method",
        "a validator bound to an undeclared kind",
        _bind_a_validator_to_an_undeclared_kind,
        "bound to the undeclared kind kind:tasks.gadget",
    ),
    (
        "method",
        "a method that is not a validator",
        _bind_a_rule_to_a_method_that_is_not_a_validator,
        "Task.model_dump is not a validator",
    ),
    (
        "configKey",
        "a class the module does not define",
        _bind_a_config_rule_to_an_absent_class,
        "swcraft_tools.schema.NoSuchElement does not exist",
    ),
    (
        "configKey",
        "a key the class does not configure",
        _bind_a_config_rule_to_an_absent_key,
        "model_config has no key 'str_max_length'",
    ),
    (
        "table",
        "a table the module does not define",
        _bind_a_rule_to_an_absent_table,
        "swcraft_tools.validator.hardware._NO_SUCH_TABLE does not exist",
    ),
    (
        "table",
        "a row the table does not hold",
        _bind_a_rule_to_an_absent_table_row,
        "_RESOURCE_FINDING_RULES has no row 'no_such_finding'",
    ),
)


@pytest.mark.parametrize(
    ("form", "case", "perturb", "expected"),
    RULE_RESOLUTION_PERTURBATIONS,
    ids=[f"{form}: {case}" for form, case, _, _ in RULE_RESOLUTION_PERTURBATIONS],
)
def test_a_rule_that_does_not_resolve_is_caught(
    form: str,
    case: str,
    perturb: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """Every resolution branch bites against the shipped artifacts."""
    binding = copy.deepcopy(load_binding())
    perturb(binding)
    failures = rule_resolution_failures(load_contract(), binding)
    assert any(expected in failure for failure in failures), (form, case, failures)


# --- the honesty check ------------------------------------------------------


def binding_honesty_failures(contract: dict[str, Any], binding: dict[str, Any]) -> list[str]:
    """A bound callable's source must contain the rule it is bound to.

    The identifier is compared without its `rule:` prefix, because the emitting
    code quotes the bare `CONSISTENCY_RULES` key. A table binding is checked the
    other way round: the row the binding names must HOLD the identifier.
    """
    failures: list[str] = []
    for rule in contract["rules"]:
        rule_id = rule["id"]
        key = rule_id[len("rule:") :]
        bound = binding["rules"].get(rule_id)
        if bound is None or ("callable" not in bound and "table" not in bound):
            # Only a callable or a table row can be checked for honesty; a
            # `method` or `configKey` binding is resolved by
            # `rule_resolution_failures` instead.
            continue
        if "callable" in bound:
            owner = getattr(importlib.import_module(bound["module"]), bound["callable"])
            if f'"{key}"' not in inspect.getsource(owner):
                failures.append(f"{rule_id}: {bound['callable']}'s source does not contain the rule identifier")
        else:
            table = getattr(importlib.import_module(bound["module"]), bound["table"])
            if table.get(bound["key"]) != key:
                failures.append(f"{rule_id}: {bound['table']}[{bound['key']!r}] is not this rule")
    return failures


def test_every_bound_callable_really_mints_its_rule() -> None:
    assert binding_honesty_failures(load_contract(), load_binding()) == []


def test_the_three_cases_the_honesty_check_exists_for_are_bound_where_they_are_minted() -> None:
    """The rules minted outside a composing entry point are bound where they are minted, by name.

    Each would read plausibly if bound to the composing entry point; pinned by
    name so a flattened binding fails here with the reason attached.
    """
    rules = load_binding()["rules"]
    for connector_rule in ("from-not-provide", "to-not-require", "interface-mismatch"):
        assert rules[f"rule:connector.{connector_rule}"] == {
            "module": "swcraft_tools.validator.connectors",
            "callable": "check_connector_compatibility",
        }, "minted by check_connector_compatibility, which is not one of the ten entry points"
    assert rules["rule:trace.link-unknown-requirement"] == {
        "module": "swcraft_tools.validator.composer",
        "callable": "_unknown_requirement_link",
    }, "emitted by the composer's own module, in the function its check 1 calls per member"
    assert "_unknown_requirement_link" in validator_package.reached_functions(
        validator_package.defining_module("check_cross_layer_consistency"), "check_cross_layer_consistency"
    )
    # Selected on the binding SHAPE, never on an identifier prefix: the budget
    # rules share `rule:hardware.*` with the topology rules, which are bound by
    # `callable` and hold no `table`.
    #
    # Two tables: `check_connector_consistency` picks an unresolvable endpoint's
    # rule out of `_UNRESOLVED_ENDPOINT_RULES`, and `check_resource_budgets` a
    # budget rule out of `_RESOURCE_FINDING_RULES`, so their sources name the
    # table and not the identifiers. Each table's row count is read from the
    # table itself.
    by_table: dict[str, list[str]] = {}
    for rule, bound in rules.items():
        if "table" in bound:
            by_table.setdefault(bound["table"], []).append(rule)
    assert set(by_table) == {"_RESOURCE_FINDING_RULES", "_UNRESOLVED_ENDPOINT_RULES"}, sorted(by_table)
    assert len(by_table["_RESOURCE_FINDING_RULES"]) == len(_RESOURCE_FINDING_RULES)
    assert len(by_table["_UNRESOLVED_ENDPOINT_RULES"]) == len(_UNRESOLVED_ENDPOINT_RULES)
    for table_name, table_rules in by_table.items():
        for rule_id in table_rules:
            table = getattr(importlib.import_module(rules[rule_id]["module"]), table_name)
            assert table[rules[rule_id]["key"]] == rule_id[len("rule:") :]


def test_a_rule_bound_to_a_composing_entry_point_is_caught() -> None:
    """A rule bound to the composing entry point that only re-raises it fails the honesty check.

    `check_connector_consistency` re-raises the compatibility rules through

    `result.rule` (validator/connectors.py::check_connector_consistency) and never names one, so binding a rule to
    it names a callable whose source does not contain the rule.
    """
    contract = load_contract()
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:connector.from-not-provide"] = {
        "module": "swcraft_tools.validator.connectors",
        "callable": "check_connector_consistency",
    }
    failures = binding_honesty_failures(contract, binding)
    assert any("rule:connector.from-not-provide" in failure for failure in failures)


def test_a_resource_rule_bound_to_the_wrong_table_row_is_caught() -> None:
    contract = load_contract()
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:hardware.missing-budget"]["key"] = "threshold_breach"
    failures = binding_honesty_failures(contract, binding)
    assert any("rule:hardware.missing-budget" in failure for failure in failures)


def test_a_resource_rule_bound_to_the_budget_check_is_caught() -> None:
    """The other half of the same fiction: `check_resource_budgets` names none of them."""
    contract = load_contract()
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:hardware.threshold-breach"] = {
        "module": "swcraft_tools.validator.hardware",
        "callable": "check_resource_budgets",
    }
    failures = binding_honesty_failures(contract, binding)
    assert any("rule:hardware.threshold-breach" in failure for failure in failures)


# --- the element-local family, both directions ------------------------------


def _schema_validator_definitions(
    kind_classes: dict[str, type[BaseModel]] | None = None,
) -> dict[tuple[str, str], tuple[str, ...]]:
    """`(defining class name, method name)` -> the fields the decorator names.

    Read from the bound classes' pydantic decorators and keyed on the class that
    DEFINES each one: a validator inherited by five subclasses is one rule, not
    five. Keyed on the definer rather than filtered to the bound class, so a
    validator defined on a base such as `ModelElement` is not invisible.

    *kind_classes* is a parameter so the derivation can be run against a class
    with an inherited validator.
    """
    definitions: dict[tuple[str, str], tuple[str, ...]] = {}
    for model_class in (bound_classes() if kind_classes is None else kind_classes).values():
        decorators = model_class.__pydantic_decorators__
        for name, decorator in decorators.field_validators.items():
            definitions[(decorator.func.__qualname__.split(".")[0], name)] = tuple(decorator.info.fields)
        for name, decorator in decorators.model_validators.items():
            definitions[(decorator.func.__qualname__.split(".")[0], name)] = ()
    return definitions


def _definer_of(model_class: type[BaseModel] | None, method: str) -> str | None:
    """The class that DEFINES *method* on *model_class*, or `None` if it has no such validator.

    Pydantic collects inherited decorators into every subclass's
    `__pydantic_decorators__`, and each one remembers its own `__qualname__`.
    """
    if model_class is None:
        return None
    decorators = model_class.__pydantic_decorators__
    decorator = decorators.field_validators.get(method) or decorators.model_validators.get(method)
    return None if decorator is None else str(decorator.func.__qualname__).split(".")[0]


def element_local_failures(
    contract: dict[str, Any],
    binding: dict[str, Any],
    kind_classes: dict[str, type[BaseModel]] | None = None,
) -> list[str]:
    """Every decorator has an identifier and every identifier has a decorator.

    Element-local identifiers are minted by this contract, so nothing but this
    comparison stops a `@field_validator` added to `schema.py` from being a rule
    the contract has never heard of. `appliesTo` is checked against the
    decorator's own field list, under the serialization alias.

    **A binding names a kind; the comparison is keyed on the class that DEFINES
    the validator** (`_definer_of`), so a base-class validator is declarable.
    What the contract still cannot express is such a rule's SCOPE: `appliesTo`
    is resolved through the one kind the binding names, so a rule that really
    governs every kind is declared as governing one; no binding form names a
    base class.
    """
    failures: list[str] = []
    kind_classes = bound_classes() if kind_classes is None else kind_classes
    declared: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in _rules_of(contract, "element-local"):
        bound = binding["rules"].get(rule["id"])
        if bound is None or "method" not in bound:
            failures.append(f"{rule['id']}: an element-local rule needs a validator binding")
            continue
        kind_binding = binding["kinds"].get(bound["kind"])
        if kind_binding is None:
            failures.append(f"{rule['id']}: bound to the undeclared kind {bound['kind']}")
            continue
        site = (
            _definer_of(kind_classes.get(bound["kind"]), bound["method"]) or kind_binding["class"],
            bound["method"],
        )
        if site in declared:
            failures.append(
                f"{rule['id']}: {site[0]}.{site[1]} is already the validator of {declared[site]['id']}; "
                "one decorator is one rule"
            )
            continue
        declared[site] = rule

    definitions = _schema_validator_definitions(kind_classes)
    for site in sorted(set(definitions) - set(declared)):
        failures.append(f"{site[0]}.{site[1]}: a validator with no contract identifier")
    for site in sorted(set(declared) - set(definitions)):
        failures.append(f"{declared[site]['id']}: bound to {site[0]}.{site[1]}, which is not a validator there")

    for site in sorted(set(definitions) & set(declared)):
        rule = declared[site]
        bound = binding["rules"][rule["id"]]
        model_class = kind_classes[bound["kind"]]
        aliases = {attribute: (info.alias or attribute) for attribute, info in model_class.model_fields.items()}
        layer, kind = bound["kind"][len("kind:") :].split(".")
        if definitions[site]:
            expected = sorted(f"field:{layer}.{kind}.{aliases[field]}" for field in definitions[site])
        else:
            expected = [bound["kind"]]
        if sorted(rule["appliesTo"]) != expected:
            failures.append(f"{rule['id']}: appliesTo is {sorted(rule['appliesTo'])}, the decorator names {expected}")
    return failures


def test_the_element_local_family_and_schema_pys_decorators_are_the_same_set() -> None:
    assert element_local_failures(load_contract(), load_binding()) == []


def test_the_element_local_count_is_the_decorator_count() -> None:
    """One identifier per decorator, derived from the classes themselves.

    A multi-field decorator is ONE rule with several `appliesTo` entries. A
    `@model_validator` names no field and therefore addresses its own KIND:
    `appliesTo` is a single `kind:` entry. Both shapes are asserted as exact
    sets rather than counts, because a count is satisfied by a
    `@field_validator` whose `appliesTo` was mis-written as its kind.
    """
    contract = load_contract()
    assert len(_rules_of(contract, "element-local")) == len(_schema_validator_definitions())
    # The number of element-local rules (one per `@field_validator` /
    # `@model_validator` in `schema.py`); moves with every validator added or retired.
    assert len(_rules_of(contract, "element-local")) == 144
    multi_field = [rule["id"] for rule in _rules_of(contract, "element-local") if len(rule["appliesTo"]) > 1]
    assert sorted(multi_field) == [
        "rule:compositions.composition_member.reference-not-blank",
        "rule:compositions.composition_port.reference-not-blank",
        "rule:compositions.delegation.reference-not-blank",
        "rule:connectors.connector_endpoint.endpoint-not-blank",
        "rule:mapping_sets.map.reference-not-blank",
        "rule:sequences.sequence_step.reference-not-blank",
        "rule:swc.call_returned_event.reference-not-blank",
        "rule:swc.data_event.reference-not-blank",
        "rule:swc.data_read.reference-not-blank",
        "rule:swc.data_write.reference-not-blank",
        "rule:swc.disabled_mode.reference-form",
        "rule:swc.operation_call.reference-not-blank",
        "rule:swc.operation_event.reference-not-blank",
        "rule:tasks.task_runnable_ref.reference-not-blank",
    ]
    kind_addressed = {
        rule["id"]: rule["appliesTo"][0]
        for rule in _rules_of(contract, "element-local")
        if rule["appliesTo"][0].startswith("kind:")
    }
    assert kind_addressed == {
        "rule:compositions.composition_member.type-form": "kind:compositions.composition_member",
        "rule:connectors.connector_endpoint.type-form": "kind:connectors.connector_endpoint",
        # Exactly one of `swc` and `instance`: a statement about the entry.
        "rule:tasks.task_runnable_ref.selector-exactly-one": "kind:tasks.task_runnable_ref",
        "rule:sequences.sequence_step.selector-exactly-one": "kind:sequences.sequence_step",
        "rule:implementation_types.pointer.target-not-blank": "kind:implementation_types.pointer",
        "rule:application_types.range.bounds-coherent": "kind:application_types.range",
        "rule:implementation_types.reference_type.base-not-blank": "kind:implementation_types.reference_type",
        "rule:implementation_types.value_type.base-and-range-coherent": "kind:implementation_types.value_type",
        "rule:interfaces.data_element.calibration-access-supported": "kind:interfaces.data_element",
        # The side of a prototype's type: exactly one of two fields.
        "rule:interfaces.data_element.type-form": "kind:interfaces.data_element",
        "rule:interfaces.argument.type-form": "kind:interfaces.argument",
        "rule:swc.variable.type-form": "kind:swc.variable",
        # A property stated in at most one of its two forms, and an
        # enumeration's values in exactly one.
        "rule:application_types.application_type.unit-form": "kind:application_types.application_type",
        "rule:application_types.application_type.conversion-form": "kind:application_types.application_type",
        "rule:application_types.application_type.range-form": "kind:application_types.application_type",
        "rule:application_types.enum.literals-form": "kind:application_types.enum",
        "rule:data_constraints.data_constr.bounds-coherent": "kind:data_constraints.data_constr",
        # An interface stub's lists matched to its `external_dest`.
        "rule:interfaces.external_interface.member-kind": "kind:interfaces.external_interface",
        # A scale's two limits judged together.
        "rule:application_types.conversion.limits-coherent": "kind:application_types.conversion",
        "rule:compu_methods.linear.limits-coherent": "kind:compu_methods.linear",
        # The retired `wcet_us` key, read off the raw mapping before any field
        # validator; and a runnable's budget and its type declared together.
        "rule:swc.runnable.wcet.retired-spelling": "kind:swc.runnable",
        "rule:tasks.task.wcet.retired-spelling": "kind:tasks.task",
        "rule:swc.runnable.wcet_type.paired": "kind:swc.runnable",
        # Two retired keys, read off the raw mapping as `wcet.retired-spelling` is.
        "rule:requirements.requirement.retired-links": "kind:requirements.requirement",
        "rule:requirements.requirement.retired-status": "kind:requirements.requirement",
        # A call's companions only with its synchrony, judged over the whole entry.
        "rule:swc.operation_call.asynchronous-fields": "kind:swc.operation_call",
    }


def test_an_undeclared_validator_fails_the_element_local_gate() -> None:
    """A validator with no contract identifier fails (simulated by removing its rule from the contract)."""
    contract = _shipped()
    removed = next(rule for rule in contract["rules"] if rule["id"] == "rule:tasks.task.partition.not-blank")
    contract["rules"].remove(removed)
    failures = element_local_failures(contract, load_binding())
    assert any("Task.partition_must_not_be_blank" in failure for failure in failures)


def test_a_misdirected_element_local_applies_to_is_caught() -> None:
    contract = _shipped()
    rule = next(rule for rule in contract["rules"] if rule["id"] == "rule:tasks.task.partition.not-blank")
    rule["appliesTo"] = ["field:tasks.task.priority"]
    failures = element_local_failures(contract, load_binding())
    assert any("rule:tasks.task.partition.not-blank" in failure for failure in failures)


def _bind_an_element_local_rule_to_a_callable(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:tasks.task.partition.not-blank"] = {
        "module": "swcraft_tools.validator.scheduling",
        "callable": "check_task_consistency",
    }


def _bind_an_element_local_rule_to_an_undeclared_kind(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:tasks.task.partition.not-blank"]["kind"] = "kind:tasks.gadget"


def _bind_an_element_local_rule_to_a_plain_method(binding: dict[str, Any]) -> None:
    binding["rules"]["rule:tasks.task.partition.not-blank"]["method"] = "model_dump"


ELEMENT_LOCAL_PERTURBATIONS: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "an element-local rule bound to a callable",
        _bind_an_element_local_rule_to_a_callable,
        "an element-local rule needs a validator binding",
    ),
    (
        "an element-local rule bound to an undeclared kind",
        _bind_an_element_local_rule_to_an_undeclared_kind,
        "bound to the undeclared kind kind:tasks.gadget",
    ),
    (
        "an element-local rule bound to a method that is not a validator",
        _bind_an_element_local_rule_to_a_plain_method,
        "which is not a validator there",
    ),
)


@pytest.mark.parametrize(
    ("case", "perturb", "expected"),
    ELEMENT_LOCAL_PERTURBATIONS,
    ids=[case for case, _, _ in ELEMENT_LOCAL_PERTURBATIONS],
)
def test_a_misbound_element_local_rule_is_caught(
    case: str,
    perturb: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """Each binding-side branch bites, including a bound method that is no longer a validator."""
    binding = copy.deepcopy(load_binding())
    perturb(binding)
    failures = element_local_failures(load_contract(), binding)
    assert any(expected in failure for failure in failures), (case, failures)


class _ElementWithACrossCuttingRule(schema.ModelElement):
    """A validator defined on `ModelElement`, the likely home of the next cross-cutting rule."""

    @model_validator(mode="after")
    def every_element_is_addressable(self) -> _ElementWithACrossCuttingRule:
        return self


class _TaskInheritingACrossCuttingRule(_ElementWithACrossCuttingRule, schema.Task):
    """The bound class. Its extra validator is defined one level up, not here."""


def test_a_validator_defined_on_a_base_class_is_not_invisible_to_the_gate() -> None:
    """A validator inherited from a base class is demanded a contract identifier."""
    kind_classes = dict(bound_classes())
    kind_classes["kind:tasks.task"] = _TaskInheritingACrossCuttingRule
    failures = element_local_failures(load_contract(), load_binding(), kind_classes)
    assert any("_ElementWithACrossCuttingRule.every_element_is_addressable" in failure for failure in failures), (
        failures
    )
    assert any("a validator with no contract identifier" in failure for failure in failures), failures


def test_a_validator_defined_on_a_base_class_can_be_declared_and_bound() -> None:
    """A base-class validator can be declared and bound through one inheriting kind.

    Guards a gate no binding could satisfy. The rule's SCOPE is still not
    expressible (see `element_local_failures`).
    """
    kind_classes = dict(bound_classes())
    kind_classes["kind:tasks.task"] = _TaskInheritingACrossCuttingRule

    contract = copy.deepcopy(load_contract())
    contract["rules"].append(
        {
            "id": "rule:tasks.task.addressable",
            "family": "element-local",
            "summary": "Every element is addressable.",
            "appliesTo": ["kind:tasks.task"],
        }
    )
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:tasks.task.addressable"] = {
        "kind": "kind:tasks.task",
        "method": "every_element_is_addressable",
    }

    assert element_local_failures(contract, binding, kind_classes) == []


def test_two_rules_bound_to_one_validator_are_caught() -> None:
    """Two identifiers bound to one decorator are reported, not collapsed by the definer key."""
    contract = copy.deepcopy(load_contract())
    original = next(rule for rule in contract["rules"] if rule["id"] == "rule:tasks.task.period.positive")
    contract["rules"].append({**copy.deepcopy(original), "id": "rule:tasks.task.period.positive-again"})
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:tasks.task.period.positive-again"] = copy.deepcopy(
        binding["rules"]["rule:tasks.task.period.positive"]
    )

    failures = element_local_failures(contract, binding)
    assert any("is already the validator of" in failure for failure in failures), failures


# --- the schema-config family: a DERIVED appliesTo ---------------------------


def model_element_config_keys() -> dict[str, Any]:
    """`ModelElement.model_config`, as a plain dict the gate can be handed."""
    return dict(schema.ModelElement.model_config)


def schema_config_failures(
    contract: dict[str, Any],
    binding: dict[str, Any],
    kind_classes: dict[str, type[BaseModel]],
) -> list[str]:
    """The governed field set is computed, then compared with the declaration, in both directions.

    The derivation is scoped to the classes that inherit the configured class.
    """
    failures: list[str] = []
    for rule in _rules_of(contract, "schema-config"):
        bound = binding["rules"].get(rule["id"])
        if bound is None or "configKey" not in bound:
            failures.append(f"{rule['id']}: a schema-config rule needs a config binding")
            continue
        predicate = CONFIG_GOVERNED_FIELDS.get(bound["configKey"])
        if predicate is None:
            failures.append(
                f"{rule['id']}: no derivation is known for {bound['configKey']!r}, so its appliesTo cannot be checked"
            )
            continue
        configured = getattr(importlib.import_module(bound["module"]), bound["class"])
        kind_names = contract_kind_names(contract, kind_classes)
        governed: set[str] = set()
        for layer in contract["layers"]:
            for kind in layer["kinds"]:
                model_class = kind_classes.get(kind["id"])
                if model_class is None or not issubclass(model_class, configured):
                    continue
                layer_name = layer["name"]
                for name, projection in structural_projection(model_class, kind_names).items():
                    if predicate(projection):
                        governed.add(f"field:{layer_name}.{kind['kind']}.{name}")
        declared = set(rule["appliesTo"])
        for entry in sorted(governed - declared):
            failures.append(f"{rule['id']}: {entry} is governed by {bound['configKey']} and is not declared")
        for entry in sorted(declared - governed):
            failures.append(f"{rule['id']}: {entry} is declared and is not governed by {bound['configKey']}")
    return failures


def test_the_schema_config_applies_to_is_the_derived_field_set() -> None:
    assert schema_config_failures(load_contract(), load_binding(), bound_classes()) == []


def test_the_governed_set_is_every_float_field_of_every_element() -> None:
    """The float fields, named, so the derivation has a visible result.

    The config sits on the base class because a per-field `v < 0` guard passes
    NaN (`NaN < 0` is `False`).
    """
    rule = next(rule for rule in _rules_of(load_contract(), "schema-config") if rule["id"] == "rule:model.float-finite")
    assert sorted(rule["appliesTo"]) == [
        "field:application_types.conversion.factor",
        "field:application_types.conversion.offset",
        "field:application_types.conversion_limit_bound.value",
        "field:application_types.range.max",
        "field:application_types.range.min",
        "field:compu_methods.linear.factor",
        "field:compu_methods.linear.offset",
        "field:compu_methods.linear_limit_bound.value",
        "field:data_constraints.data_constr.max",
        "field:data_constraints.data_constr.min",
        "field:hardware.core.threshold",
        "field:hardware.memory.threshold",
        "field:implementation_types.value_type.value_max",
        "field:implementation_types.value_type.value_min",
        "field:swc.operation_call.timeout",
        "field:swc.port_com_spec.alive_timeout",
        "field:swc.runnable.min_start_interval",
        "field:swc.timing_event.period",
        "field:tasks.task.period",
    ]


class _TaskWithAnotherFloat(schema.Task):
    """A float field added to a bound class without touching the contract."""

    ratio: float = 0.5


def test_a_new_float_field_fails_until_it_is_declared() -> None:
    contract = load_contract()
    kind_classes = dict(bound_classes())
    kind_classes["kind:tasks.task"] = _TaskWithAnotherFloat
    failures = schema_config_failures(contract, load_binding(), kind_classes)
    assert any("field:tasks.task.ratio" in failure and "is not declared" in failure for failure in failures)


def test_a_declared_field_that_is_not_governed_fails() -> None:
    contract = _shipped()
    rule = next(rule for rule in _rules_of(contract, "schema-config"))
    rule["appliesTo"] = sorted([*rule["appliesTo"], "field:tasks.task.priority"])
    failures = schema_config_failures(contract, load_binding(), bound_classes())
    assert any("field:tasks.task.priority" in failure and "is not governed" in failure for failure in failures)


def test_a_schema_config_rule_with_no_config_binding_is_caught() -> None:
    """A `schema-config` rule bound in any other form than `configKey` is reported."""
    binding = copy.deepcopy(load_binding())
    del binding["rules"]["rule:model.float-finite"]["configKey"]
    failures = schema_config_failures(load_contract(), binding, bound_classes())
    assert any("needs a config binding" in failure for failure in failures), failures


def test_a_float_inside_a_collection_is_governed_too() -> None:
    """`allow_inf_nan` reaches a `list[float]`, and so must the derivation."""

    class _TaskWithAFloatCollection(schema.Task):
        ratios: list[float] = []

    contract = load_contract()
    kind_classes = dict(bound_classes())
    kind_classes["kind:tasks.task"] = _TaskWithAFloatCollection
    failures = schema_config_failures(contract, load_binding(), kind_classes)
    assert any("field:tasks.task.ratios" in failure and "is not declared" in failure for failure in failures), failures


def test_a_config_key_with_no_known_derivation_is_refused_rather_than_trusted() -> None:
    """An unknown `configKey` fails rather than falling back to the hand-written declared list."""
    contract = _shipped()
    binding = copy.deepcopy(load_binding())
    binding["rules"]["rule:model.float-finite"]["configKey"] = "str_max_length"
    failures = schema_config_failures(contract, binding, bound_classes())
    assert any("no derivation is known" in failure for failure in failures)


# --- the reverse direction: `model_config` itself ---------------------------


def model_config_failures(
    config: dict[str, Any],
    contract: dict[str, Any],
    binding: dict[str, Any],
    exempt: dict[str, str] | None = None,
) -> list[str]:
    """Every `model_config` key is either a declared rule or a named exemption.

    *config* and *exempt* are parameters so the gate can be run against a key
    or an exemption added next.
    """
    exemptions = MODEL_CONFIG_WITHOUT_VALIDATION_MEANING if exempt is None else exempt
    declared = {
        binding["rules"][rule["id"]]["configKey"]
        for rule in _rules_of(contract, "schema-config")
        if "configKey" in binding["rules"].get(rule["id"], {})
    }
    failures: list[str] = []
    for key in sorted(config):
        if key in declared or key in exemptions:
            continue
        failures.append(
            f"model_config key {key!r} is neither a declared schema-config rule nor a named no-validation-meaning key"
        )
    for key in sorted(declared & set(exemptions)):
        failures.append(f"model_config key {key!r} is both a declared rule and exempt")
    for key, reason in exemptions.items():
        if not reason.strip():
            failures.append(f"model_config key {key!r} is exempt with no reason")
    return failures


def test_every_model_config_key_is_declared_or_named_exempt() -> None:
    assert model_config_failures(model_element_config_keys(), load_contract(), load_binding()) == []


def test_a_key_that_is_both_declared_and_exempt_is_caught() -> None:
    """An exemption for a key that is already a rule is a contradiction, not a note."""
    failures = model_config_failures(
        model_element_config_keys(),
        load_contract(),
        load_binding(),
        exempt={"allow_inf_nan": "listed as carrying no validation meaning"},
    )
    assert any("is both a declared rule and exempt" in failure for failure in failures), failures


def test_an_exemption_with_no_reason_is_caught() -> None:
    """An exemption is a claim, and the map is where it has to be argued."""
    failures = model_config_failures(
        {**model_element_config_keys(), "str_max_length": 128},
        load_contract(),
        load_binding(),
        exempt={"str_max_length": "   "},
    )
    assert any("is exempt with no reason" in failure for failure in failures), failures


def test_the_next_model_config_key_cannot_take_effect_unannounced() -> None:
    """`str_max_length` on the base class would constrain every string field; adding it fails."""
    failures = model_config_failures(
        {**model_element_config_keys(), "str_max_length": 128},
        load_contract(),
        load_binding(),
    )
    assert any("str_max_length" in failure for failure in failures)


# --- `appliesTo` resolves, for every family ---------------------------------


def applies_to_failures(contract: dict[str, Any]) -> list[str]:
    """Every `appliesTo` entry names a layer, kind or field the contract declares."""
    entries = _entry_identifiers(contract)
    failures: list[str] = []
    for rule in contract["rules"]:
        if not rule["appliesTo"]:
            failures.append(f"{rule['id']}: appliesTo is empty")
        for entry in rule["appliesTo"]:
            if entry not in entries:
                failures.append(f"{rule['id']}: appliesTo names {entry}, which the contract does not declare")
    return failures


def test_every_rule_applies_to_something_the_contract_declares() -> None:
    assert applies_to_failures(load_contract()) == []


def test_an_applies_to_naming_an_undeclared_entry_is_caught() -> None:
    contract = _shipped()
    contract["rules"][0]["appliesTo"] = ["field:swc.swc.owner"]
    assert any("which the contract does not declare" in failure for failure in applies_to_failures(contract))


def test_a_rule_that_applies_to_nothing_is_caught() -> None:
    """An empty `appliesTo` fails this gate itself, not only the meta-schema's `minItems: 1`."""
    contract = _shipped()
    contract["rules"][0]["appliesTo"] = []
    assert any("appliesTo is empty" in failure for failure in applies_to_failures(contract))


# --- the cross-element family: `appliesTo` against the emission sites --------
#
# The cross-element `appliesTo` lists are hand-authored, and
# `applies_to_failures` only asks whether each entry *resolves*, never whether
# it is the right entry.
#
# The independent signal is the finding's own **subjects**: a `ConsistencyIssue`
# (and a `resource_analysis.Finding`) carries the model places it is about, and a
# `Subject`'s first member is a layer name. Those are read out of the emitting
# module's source below and compared with the layers `appliesTo` names.


def _rule_translations(binding: dict[str, Any]) -> dict[str, str]:
    """`finding code -> rule key`, from every binding that names a table.

    The budget rules are emitted under `resource_analysis`'s finding codes and
    translated through a table `binding.json` declares as `{module, table, key}`.
    Flat rather than per-module, because the table and the construction site
    are in different modules.
    """
    translations: dict[str, str] = {}
    for bound in binding["rules"].values():
        if "table" in bound:
            table = getattr(importlib.import_module(bound["module"]), bound["table"])
            translations.update(dict(table.items()))
    return translations


def _emission_modules(binding: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """Every module a cross-element rule can be emitted from, derived from the binding.

    The bindings name their own module directly. A `{module, table, key}`
    binding names the module the *translation* lives in, not where the finding
    is built; that module is recovered from the table's own keys, which are
    imported names whose `ImportFrom` says where from.
    """

    modules = {
        binding["rules"][rule["id"]]["module"]
        for rule in _rules_of(contract, "cross-element")
        if rule["id"] in binding["rules"]
    }
    for bound in list(binding["rules"].values()):
        if "table" not in bound:
            continue
        tree = ast.parse(inspect.getsource(importlib.import_module(bound["module"])))
        keys: set[str] = set()
        for node in ast.walk(tree):
            target = node.target if isinstance(node, ast.AnnAssign) else None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == bound["table"] and isinstance(node.value, ast.Dict):
                keys = {key.id for key in node.value.keys if isinstance(key, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and {alias.name for alias in node.names} & keys:
                modules.add(node.module)
    return sorted(modules)


def _identified(strings: set[str], translation: Mapping[str, str], declared: set[str]) -> set[str]:
    """*strings*, and their translations, narrowed to the rule keys the contract declares."""
    return {translation.get(value, value) for value in strings} & declared


def emission_layers(
    contract: dict[str, Any] | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, list[frozenset[str] | None]]:
    """`rule id -> one entry per emission site`: the layers it reports at, or `None`.

    A rule with no entry at all is emitted nowhere the scan can see. A `None` entry is a site the scan could not
    read, or read and found no layer in; both are failures of the gate rather than silent passes. A site attributed
    to no declared rule (its keywords and tables name none, or a keyword is read in part) is keyed
    `unattributed:<module>:<line>`, which no rule id matches, so the derivation gate fails it by name.

    **The `or None` on the append is the second half.** :meth:`EmissionScan.layers` answers `frozenset()` for a
    readable expression naming no `Subject` (a list bound empty and never filled, a literal `subjects=()`), and an
    empty set is contained in every declaration, so without it such a refactor would make the gate vacuous for its
    rule. What the collapse does NOT recover is a site that resolves to *fewer* layers than it really reports at
    through a shape `EmissionScan` names as not reached: the answer is non-empty and passes. That residue is the
    `completeness` limit named in :func:`cross_element_layer_failures`, and no gate here closes it.

    A site whose `subjects=` argument is another finding's own `.subjects` is a **re-emission**, not an emission:
    `check_resource_budgets` copies the subjects `resource_analysis` built. Those are skipped, and the rules they
    carry are attributed to the module the finding was really constructed in.
    """
    contract = load_contract() if contract is None else contract
    binding = load_binding() if binding is None else binding
    declared = {rule["id"][len("rule:") :] for rule in _rules_of(contract, "cross-element")}
    declared_layers = frozenset(layer["name"] for layer in contract["layers"])
    translation = _rule_translations(binding)

    observed: dict[str, list[frozenset[str] | None]] = {}
    for module_name in _emission_modules(binding, contract):
        module = importlib.import_module(module_name)
        scan = EmissionScan(inspect.getsource(module), vars(module), declared_layers=declared_layers)
        for call in scan.sites():
            subjects = EmissionScan.keyword(call, "subjects")
            if isinstance(subjects, ast.Attribute) and subjects.attr == "subjects":
                continue
            own = scan.keyword_strings(call)
            identified = _identified(own or set(), translation, declared)
            if own is not None and not identified:
                identified = _identified(scan.table_strings(call), translation, declared)
            keys = [f"rule:{key}" for key in sorted(identified)] or [f"unattributed:{module_name}:{call.lineno}"]
            for key in keys:
                observed.setdefault(key, []).append(scan.layers(subjects) or None)
    return observed


def cross_element_layer_failures(
    contract: dict[str, Any],
    observed: dict[str, list[frozenset[str] | None]] | None = None,
) -> list[str]:
    """Every layer a cross-element rule is reported at must be a layer it declares.

    **This is a containment check, and only one way.** A rule's `appliesTo` may
    legitimately name more than its subjects do — the side of a broken reference
    that is *missing* is named by the rule and pointed at by no subject
    (`port.undeclared-interface` reports at the referring port and declares
    `layer:interfaces` too), so a declared layer that is never reported at is not
    an error and is not checked. What it does prove is that no rule has been
    pointed at a part of the model it demonstrably has nothing to do with.

    **Three things it does not prove**, and no gate in this module does:

    * *the field*. `appliesTo` entries are field identifiers and this reads
      layers, so `field:connectors.connector.from` and
      `field:connectors.connector.to` are indistinguishable here;
    * *completeness*. A rule that should name a second layer and names one is
      contained in its own declaration and passes;
    * *the entries the subjects cannot reach at all*, which is every entry
      naming the far side of a reference.

    Those remain hand-authored data, and human review is the only control over them.
    """
    observed = emission_layers(contract) if observed is None else observed
    failures: list[str] = []
    for rule in _rules_of(contract, "cross-element"):
        sites = observed.get(rule["id"])
        if not sites:
            failures.append(f"{rule['id']}: no emission site names it, so nothing observes its appliesTo")
            continue
        if any(not site for site in sites):
            failures.append(f"{rule['id']}: an emission site yields no layer, so its appliesTo is unobserved")
            continue
        declared = {entry.split(":", 1)[1].split(".")[0] for entry in rule["appliesTo"]}
        for layer in sorted(frozenset().union(*sites) - declared):
            failures.append(f"{rule['id']}: reported at layer {layer}, which its appliesTo does not name")
    return failures


def test_every_cross_element_rule_applies_to_the_layers_it_reports_at() -> None:
    assert cross_element_layer_failures(load_contract()) == []


def test_the_reviewers_misdirected_applies_to_is_caught() -> None:
    """A cross-element rule pointed at a layer it never reports at fails, although the entry resolves."""
    contract = _shipped()
    rule = next(rule for rule in contract["rules"] if rule["id"] == "rule:connector.from-not-provide")
    rule["appliesTo"] = ["field:tasks.task.priority"]
    failures = cross_element_layer_failures(contract)
    # `in`, not `==`: an unrelated failure elsewhere must not fail this test.
    assert "rule:connector.from-not-provide: reported at layer connectors, which its appliesTo does not name" in (
        failures
    )


def test_the_derivation_reads_every_cross_element_rule() -> None:
    """No residue: every cross-element rule is observed, none by an unread site, and every site attributed.

    `emission_layers` writes `None` for both ways of observing nothing, so **zero** sites are unreadable and
    **zero** resolve to an empty set of layers; a site attributed to no declared rule is a key no rule id matches.
    If a refactor of the validator puts a subject expression or a rule identifier beyond `EmissionScan`, teach the
    scan the new shape rather than shrink the denominator or relax the assertion.
    """
    contract = load_contract()
    observed = emission_layers(contract)
    # The number of cross-element rules the contract declares; moves with every
    # rule minted or retired.
    assert len(_rules_of(contract, "cross-element")) == 207
    assert sorted(observed) == sorted(rule["id"] for rule in _rules_of(contract, "cross-element"))
    unread = sorted(rule for rule, sites in observed.items() if any(site is None for site in sites))
    assert unread == []


def test_no_emitting_module_binds_one_name_to_two_defs_in_one_scope() -> None:
    """No module or function scope of an emitting module binds one name with two `def`s, and every scope pairs.

    Which of two `def`s of one scope a call reaches is a runtime choice the scan cannot settle; a scope the
    scan could not pair with its symbol table resolves no call. A class body is exempt (an `@overload` group,
    a property's setter).
    """
    failures: list[str] = []
    for module_name in _emission_modules(load_binding(), load_contract()):
        module = importlib.import_module(module_name)
        scopes = Scopes(ast.parse(source := inspect.getsource(module)), source)
        failures += [f"{module_name}: {collision}" for collision in scopes.collisions()]
        failures += [f"{module_name}: unpaired scope at line {line}" for line in scopes.unpaired()]
    assert failures == []


CROSS_ELEMENT_LAYER_PERTURBATIONS: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
    (
        "a rule nothing emits",
        lambda observed: observed.pop("rule:connector.from-not-provide"),
        "no emission site names it",
    ),
    (
        "a rule emitted at a site the scan cannot read",
        lambda observed: observed.__setitem__("rule:connector.from-not-provide", [None]),
        "an emission site yields no layer",
    ),
    (
        "a rule emitted at a site the scan reads as empty",
        lambda observed: observed.__setitem__("rule:connector.from-not-provide", [frozenset()]),
        "an emission site yields no layer",
    ),
    (
        "a rule reported at a layer it does not name",
        lambda observed: observed.__setitem__("rule:connector.from-not-provide", [frozenset({"tasks"})]),
        "reported at layer tasks, which its appliesTo does not name",
    ),
)


@pytest.mark.parametrize(
    ("case", "perturb", "expected"),
    CROSS_ELEMENT_LAYER_PERTURBATIONS,
    ids=[case for case, _, _ in CROSS_ELEMENT_LAYER_PERTURBATIONS],
)
def test_each_cross_element_layer_branch_bites(
    case: str,
    perturb: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    observed = dict(emission_layers())
    perturb(observed)
    failures = cross_element_layer_failures(load_contract(), observed)
    assert any(expected in failure for failure in failures), (case, failures)


# ---------------------------------------------------------------------------
# The runnable's three access lists, as the contract declares them
# ---------------------------------------------------------------------------
#
# The bidirectional gate proves `schema.py` and the contract describe the same
# shapes, not that they are the intended ones: a composite collection keyed by
# the pair, omitted at its empty default, whose member kind belongs to the
# runnable alone. Each expectation is written out by hand: the expected value
# may not be read from the document it checks.

#: `(member kind, composite identity)` per access list. `by` is deliberately
#: absent from `reads`' identity: it is the receive form, and one pair may not
#: appear once in each form (`rule:swc.runnable.reads.no-duplicate-entry`).
ACCESS_LISTS: dict[str, tuple[str, list[str]]] = {
    "reads": ("data_read", ["port", "element"]),
    "writes": ("data_write", ["port", "element"]),
    "calls": ("operation_call", ["port", "operation"]),
}

#: Per member kind: the list it fills, its bound class and its fields in
#: declaration order.
ACCESS_KINDS: dict[str, tuple[str, str, list[str]]] = {
    "kind:swc.data_read": ("reads", "DataReadRef", ["port", "element", "by", "name"]),
    "kind:swc.data_write": ("writes", "DataWriteRef", ["port", "element", "by", "name"]),
    "kind:swc.operation_call": (
        "calls",
        "OperationCallRef",
        ["port", "operation", "asynchronous", "poll", "result", "timeout", "name"],
    ),
}

#: The six element-local rules, each bound to the validator that mints it: the
#: three duplicate guards on the runnable and the three blank guards on the
#: entry classes (the `task_runnable_ref` / `sequence_step` precedent).
ACCESS_RULES: dict[str, tuple[str, str, list[str]]] = {
    "rule:swc.runnable.reads.no-duplicate-entry": ("kind:swc.runnable", "reads_must_not_have_duplicates", ["reads"]),
    "rule:swc.runnable.writes.no-duplicate-entry": (
        "kind:swc.runnable",
        "writes_must_not_have_duplicates",
        ["writes"],
    ),
    "rule:swc.runnable.calls.no-duplicate-entry": ("kind:swc.runnable", "calls_must_not_have_duplicates", ["calls"]),
    "rule:swc.data_read.reference-not-blank": (
        "kind:swc.data_read",
        "ref_fields_must_not_be_empty",
        ["port", "element"],
    ),
    "rule:swc.data_write.reference-not-blank": (
        "kind:swc.data_write",
        "ref_fields_must_not_be_empty",
        ["port", "element"],
    ),
    "rule:swc.operation_call.reference-not-blank": (
        "kind:swc.operation_call",
        "ref_fields_must_not_be_empty",
        ["port", "operation"],
    ),
}


def _shipped_kind_decls() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def test_the_three_access_lists_are_composite_collections_omitted_at_default() -> None:
    """Each access list is `list[<kind>]`, default `[]`, composite identity, omitted at default.

    Without `omitAtDefault` the generic writer, which emits an empty list at
    default, would add `reads: []` and its siblings to every rewritten SWC file.
    The class's own default is checked beside it.
    """
    fields = _contract_field_decls(load_contract())
    runnable = bound_classes()["kind:swc.runnable"]
    for name, (member_kind, identity) in ACCESS_LISTS.items():
        declared = fields[("runnable", name)]
        assert declared["id"] == f"field:swc.runnable.{name}"
        assert declared["type"] == f"list[{member_kind}]"
        assert declared["required"] is False and declared["immutable"] is False
        assert declared["default"] == []
        assert declared["omitAtDefault"] is True, f"{name}: D3a — the empty list must be left out of the file"
        assert declared["collection"] == {
            "kind": "list",
            "memberIdentity": "composite",
            "memberKinds": [member_kind],
            "identityFields": identity,
        }
        assert "reference" not in declared and "relation" not in declared, (
            f"{name}: an access names a port of its OWN component, never another layer's element"
        )
        info = runnable.model_fields[name]
        assert not info.is_required() and info.default == []


def test_the_three_access_kinds_are_member_only_of_the_runnable_and_bound_to_their_classes() -> None:
    """Each member kind belongs to exactly one list of the runnable, and `by` has its declared form per kind.

    A call refuses `by` by shape: the field is absent from its kind and its class.
    """
    kinds = _shipped_kind_decls()
    binding = load_binding()["kinds"]
    classes = bound_classes()
    for kind_id, (list_name, class_name, names) in ACCESS_KINDS.items():
        kind = kinds[kind_id]
        assert kind["memberOnly"] is True
        assert kind["memberOf"] == [{"kind": "runnable", "field": list_name}]
        assert [field["name"] for field in kind["fields"]] == names
        assert binding[kind_id]["class"] == class_name
        assert classes[kind_id].__name__ == class_name
        assert list(classes[kind_id].model_fields) == names
        for field in kind["fields"]:
            # `by` is a form, `name` a stated SHORT-NAME, and a call's synchrony and
            # companions are options: none is part of the pair that identifies the entry.
            if field["name"] not in ("by", "name", "asynchronous", "poll", "result", "timeout"):
                assert field["required"] is True and field["immutable"] is True, field["id"]
        name = next(field for field in kind["fields"] if field["name"] == "name")
        assert name["required"] is False and name["default"] is None and name["immutable"] is False
    by = next(field for field in kinds["kind:swc.data_read"]["fields"] if field["name"] == "by")
    assert by["required"] is False and by["default"] is None and by["immutable"] is False
    assert by["enum"] == ["value", "implicit"]
    write_by = next(field for field in kinds["kind:swc.data_write"]["fields"] if field["name"] == "by")
    assert write_by["required"] is False and write_by["default"] is None and write_by["enum"] == ["implicit"]
    assert "by" not in classes["kind:swc.operation_call"].model_fields


def test_the_runnable_description_names_its_three_access_lists() -> None:
    """The runnable's description, shown as the editor's hover and on the guide's model page, names its access lists."""
    description = _shipped_kind_decls()["kind:swc.runnable"]["description"]
    for name in ("`reads`", "`writes`", "`calls`"):
        assert name in description, f"the runnable's description does not name {name}"
    assert (
        "mapped to an OS task through the task's `runnables` list, by its component — "
        "the type by `swc` or the occurrence by `instance`" in description
    )


def test_the_six_element_local_access_rules_are_bound_to_their_validators() -> None:
    """Each of the six rules resolves to the decorator that mints it, and names exactly its fields.

    The set gate cannot see a rule re-bound to a neighbour's guard of the same shape.
    """
    contract = load_contract()
    binding = load_binding()["rules"]
    classes = bound_classes()
    declared = {rule["id"]: rule for rule in _rules_of(contract, "element-local")}
    for rule_id, (kind_id, method, fields) in ACCESS_RULES.items():
        assert binding[rule_id] == {"kind": kind_id, "method": method}, rule_id
        validators = classes[kind_id].__pydantic_decorators__.field_validators
        assert method in validators, f"{kind_id} has no validator {method}"
        assert list(validators[method].info.fields) == fields, rule_id
        kind_prefix = kind_id.removeprefix("kind:")
        assert declared[rule_id]["appliesTo"] == [f"field:{kind_prefix}.{field}" for field in fields], rule_id
