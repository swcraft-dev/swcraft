"""Tests for `listing.py`, `swcraft list`'s read-only enumeration.

Two orders are observable output, each a declared fact:

* the **layer** order is the contract's `displayOrder` (`registry.display_order`); its
  end-to-end comparison with the differ's order is
  `test_cli.py::TestCliList::test_cli_list_layer_order_matches_the_order_diff_renders`, which
  compares the bytes a user sees without this module loading a snapshot;
* the **element** order inside a layer is the reader's: placement values, then declared
  identity, ordinally; a file name takes no part.

What these tests do **not** reach: they exercise the shipped contract and the synthetic layer,
so they witness that the module is generic over the registry, not that every contract is
listable. A layer whose bound class lacks its own `identityField` would raise `AttributeError`
from `_listing_of`, and nothing forbids such a binding; the conformance gate covers the shipped
contract.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import yaml
from conftest import _root_composition, _swc, _task, _write, _write_manifest, element_path, model_element_path
from swcraft_tools.listing import (
    LISTING_SCHEMA_VERSION,
    format_listing,
    format_listing_json,
    list_model,
)
from swcraft_tools.model_registry import ModelRegistry, production_registry, use_registry
from swcraft_tools.reader import ElementParseError
from synthetic_support import merged_registry


def _populated_model(root: Path) -> Path:
    """Write a model root with elements in six layers, spread across `displayOrder`.

    `requirements` is early and `tasks` late in the presentation order, so contract or insertion
    order fails the order assertions; `compositions` is here because this module lists it without
    a line of its own.
    """
    _write(
        element_path(root, "requirements", "REQ_ONE"),
        {
            "id": "REQ_ONE",
            "title": "One",
            "description": "The system shall list.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )
    _write(
        model_element_path(root, "float32", kind="base_type"),
        {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
    )
    _write(
        element_path(root, "interfaces", "IExample"),
        {
            "name": "IExample",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        },
    )
    _swc(root, "AlphaSWC")
    _root_composition(root, ["AlphaSWC"])
    _task(root, "Task_10ms")
    return root


# The happy path: every layer, one layer, and the identity field


def test_every_declared_layer_is_listed_in_display_order(tmp_path: Path) -> None:
    """The key order is `registry.display_order`, and every layer is a key."""
    model_root = _populated_model(tmp_path / "model")

    listings = list_model(str(model_root))

    assert list(listings) == list(production_registry().display_order)


def test_a_named_layer_yields_exactly_that_layer(tmp_path: Path) -> None:
    model_root = _populated_model(tmp_path / "model")

    listings = list_model(str(model_root), "swc")

    assert list(listings) == ["swc"]
    assert listings["swc"].identities == ("AlphaSWC",)


def test_each_layer_reports_its_own_declared_identity_field(tmp_path: Path) -> None:
    """Each layer reports its own declared identity field, asserted against the registry."""
    model_root = _populated_model(tmp_path / "model")
    registry = production_registry()

    listings = list_model(str(model_root))

    assert {layer: listing.identity_field for layer, listing in listings.items()} == {
        layer: registry.layer(layer).identity_field for layer in registry.display_order
    }
    assert listings["requirements"].identities == ("REQ_ONE",)


def test_a_nested_swc_is_listed_by_its_name_and_not_its_group(tmp_path: Path) -> None:
    """A nested SWC is listed by its identity, not its group or path."""
    model_root = tmp_path / "model"
    _swc(model_root, "NestedSWC")

    assert (model_root / "swc" / "group" / "NestedSWC.yaml").is_file()
    assert list_model(str(model_root))["swc"].identities == ("NestedSWC",)


def test_an_absent_layer_directory_yields_an_empty_tuple_and_not_a_missing_key(tmp_path: Path) -> None:
    """An absent layer directory is an empty tuple, so "no SWCs" differs from "no such layer"."""
    model_root = tmp_path / "model"
    _swc(model_root, "OnlySWC")

    listings = list_model(str(model_root))

    assert not (model_root / "hardware").exists()
    assert "hardware" in listings
    assert listings["hardware"].identities == ()


def test_an_existing_but_empty_model_root_lists_every_layer_empty(tmp_path: Path) -> None:
    """An empty model is an *answer*: every declared layer, no elements anywhere."""
    model_root = tmp_path / "model"
    model_root.mkdir()

    listings = list_model(str(model_root))

    assert list(listings) == list(production_registry().display_order)
    assert all(listing.identities == () for listing in listings.values())


# The two orders, each against its own declared fact


def test_the_element_order_is_placement_then_identity(tmp_path: Path) -> None:
    """Placement first: `zulu` in group `alpha` precedes `alpha` in group `beta`."""
    model_root = tmp_path / "model"
    _write(element_path(model_root, "swc", "zulu", group="alpha"), {"name": "zulu", "type": "application"})
    _write(element_path(model_root, "swc", "alpha", group="beta"), {"name": "alpha", "type": "application"})

    assert list_model(str(model_root))["swc"].identities == ("zulu", "alpha")


def test_a_file_name_takes_no_part_in_the_element_order(tmp_path: Path) -> None:
    """`b.yaml` declaring `A` and `a.yaml` declaring `B` list `A, B`: renaming a file reorders nothing."""
    model_root = tmp_path / "model"
    primitive = {"kind": "base_type", "size": 8, "encoding": "unsigned"}
    _write(model_element_path(model_root, "b", kind="base_type"), {"name": "A", **primitive})
    _write(model_element_path(model_root, "a", kind="base_type"), {"name": "B", **primitive})

    assert list_model(str(model_root))["base_types"].identities == ("A", "B")


# The failures: no partial answer, and no answer at all for a missing root


def test_one_invalid_element_file_raises_and_names_it(tmp_path: Path) -> None:
    """No partial answer: one unparseable element file fails the listing, naming the file.

    A silently dropped file would look like a smaller model.
    """
    model_root = tmp_path / "model"
    _swc(model_root, "GoodSWC")
    _write(element_path(model_root, "swc", "BadSWC", group="group"), {"name": "BadSWC", "type": "not_a_type"})

    with pytest.raises(ElementParseError) as exc_info:
        list_model(str(model_root))

    assert "BadSWC.yaml" in str(exc_info.value)


def test_malformed_yaml_in_one_element_file_raises(tmp_path: Path) -> None:
    """Broken YAML syntax fails the same way — a `ValueError`, not a bare parser error."""
    model_root = tmp_path / "model"
    _swc(model_root, "GoodSWC")
    (model_root / "swc" / "group" / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")

    with pytest.raises((ValueError, yaml.YAMLError)):
        list_model(str(model_root))


def test_a_model_root_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """A missing model root is refused, not listed as an empty, freshly scaffolded project."""
    with pytest.raises(FileNotFoundError):
        list_model(str(tmp_path / "nope"))


def test_an_undeclared_layer_name_is_refused_by_the_registry(tmp_path: Path) -> None:
    """An undeclared layer name is refused by `registry.layer`; the CLI's `choices` reject it earlier."""
    model_root = tmp_path / "model"
    model_root.mkdir()

    with pytest.raises(KeyError):
        list_model(str(model_root), "not_a_layer")


# The renderers


def test_the_text_form_is_one_layer_colon_element_line_per_element(tmp_path: Path) -> None:
    model_root = _populated_model(tmp_path / "model")

    rendered = format_listing(list_model(str(model_root)))

    assert rendered.splitlines() == [
        "requirements: REQ_ONE",
        "compositions: Root",
        "swc: AlphaSWC",
        "interfaces: IExample",
        "base_types: float32",
        "tasks: Task_10ms",
    ]
    assert rendered.endswith("\n")


def test_the_text_form_of_a_model_with_no_elements_is_empty(tmp_path: Path) -> None:
    """Nothing to list prints nothing — not a heading, and not a blank line."""
    model_root = tmp_path / "model"
    model_root.mkdir()

    assert format_listing(list_model(str(model_root))) == ""


def test_the_json_form_carries_the_schema_version_and_every_layer(tmp_path: Path) -> None:
    model_root = _populated_model(tmp_path / "model")

    document = json.loads(format_listing_json(list_model(str(model_root))))

    assert document["schemaVersion"] == LISTING_SCHEMA_VERSION
    assert list(document["layers"]) == list(production_registry().display_order)
    assert document["layers"]["swc"] == {"identityField": "name", "elements": ["AlphaSWC"]}
    assert document["layers"]["requirements"]["identityField"] == "id"
    assert document["layers"]["hardware"]["elements"] == []


def _levelled_model(root: Path) -> Path:
    """Write `_populated_model` with a safety level on every component and every requirement."""
    _populated_model(root)
    for layer, level in (("swc", "D"), ("requirements", "QM")):
        for path in sorted((root / layer).rglob("*.yaml"), key=lambda p: p.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["asil"] = level
            path.write_text(
                yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )
    return root


def test_a_declared_safety_level_changes_no_byte_of_the_listing(tmp_path: Path) -> None:
    """A declared safety level changes no byte of either rendered listing.

    Two different levels (`D`, `QM`) are written and the fixture is checked to declare them, so
    neither a leak nor a vacuous copy passes. Does not reach the derived level, which no read
    surface answers (`query`'s half is in `test_query.py`).
    """
    bare = _populated_model(tmp_path / "bare")
    levelled = _levelled_model(tmp_path / "levelled")

    declared = [
        path
        for layer in ("swc", "requirements")
        for path in (levelled / layer).rglob("*.yaml")
        if "asil" in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]
    assert len(declared) == 2, declared

    assert format_listing(list_model(str(levelled))) == format_listing(list_model(str(bare)))
    assert format_listing_json(list_model(str(levelled))) == format_listing_json(list_model(str(bare)))
    assert list_model(str(levelled)) == list_model(str(bare))


def test_the_json_form_is_newline_terminated(tmp_path: Path) -> None:
    """A stream a consumer reads line-wise must end its last line."""
    model_root = tmp_path / "model"
    model_root.mkdir()

    assert format_listing_json(list_model(str(model_root))).endswith("}\n")


# Generic over the registry, resolved per call


def test_the_registry_parameter_has_no_default_that_evaluates_a_registry() -> None:
    """The `registry` parameter defaults to `None`, resolved in the body.

    A default expression is evaluated once, binding every caller to the registry current at import.
    """
    assert inspect.signature(list_model).parameters["registry"].default is None


def test_a_synthetic_layer_is_listed_with_no_source_edit(tmp_path: Path) -> None:
    """An added layer is listed with no source edit, last, since it declares no `displayOrder`."""
    merged: ModelRegistry = merged_registry()
    model_root = tmp_path / "model"
    _write(
        element_path(model_root, "swc", "AlphaSWC", group="group"),
        {"name": "AlphaSWC", "type": "application"},
    )
    synthetic = model_root / "synthetic"
    synthetic.mkdir(parents=True)
    (synthetic / "Widget.yaml").write_text(
        yaml.safe_dump({"name": "Widget", "title": "A widget", "composition": "AlphaSWC"}, sort_keys=False),
        encoding="utf-8",
    )

    listings = list_model(str(model_root), registry=merged)

    assert list(listings)[-1] == "synthetic"
    assert listings["synthetic"].identities == ("Widget",)
    assert listings["swc"].identities == ("AlphaSWC",)


def test_an_explicit_registry_beats_the_context(tmp_path: Path) -> None:
    """An explicit registry beats the one `use_registry` made current."""
    merged = merged_registry()
    model_root = tmp_path / "model"
    model_root.mkdir()

    with use_registry(merged):
        assert "synthetic" in list_model(str(model_root))
        assert "synthetic" not in list_model(str(model_root), registry=production_registry())


def test_no_manifest_is_needed_to_list(tmp_path: Path) -> None:
    """The module takes a model root, not a project: it imports nothing from `project.py`."""
    model_root = tmp_path / "model"
    _swc(model_root, "OnlySWC")

    assert not (tmp_path / "swcraft.yaml").exists()
    assert list_model(str(model_root))["swc"].identities == ("OnlySWC",)


def test_the_manifest_helper_is_not_what_the_module_reads(tmp_path: Path) -> None:
    """A manifest alone lists nothing: the module reads the model root it is handed."""
    _write_manifest(tmp_path)

    with pytest.raises(FileNotFoundError):
        list_model(str(tmp_path / "model"))
