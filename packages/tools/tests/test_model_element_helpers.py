"""The test helpers that write one element file, its directory derived from the registry.

``model_element_storage_name``, ``model_element_path`` and ``write_model_element``
(``packages/tools/tests/conftest.py``) are how the suites build a data type or a mapping
set without spelling its directory, so a layer that moves its directory, or a
kind that moves to another layer, reaches every test built on them with no edit
there. Three copies exist -- this suite's, ``mcp_server``'s and ``agent``'s,
because no suite can import another's conftest -- and the first test holds them
equal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, model_element_path, model_element_storage_name, write_model_element
from swcraft_tools.model_registry import current_registry, use_registry
from synthetic_support import SIBLING_NESTED_LAYERS, nested_registry

HELPERS = ("model_element_storage_name", "model_element_path", "write_model_element")

CONFTESTS = (
    REPO_ROOT / "packages" / "tools" / "tests" / "conftest.py",
    REPO_ROOT / "packages" / "mcp" / "tests" / "conftest.py",
    REPO_ROOT / "packages" / "agent" / "tests" / "conftest.py",
)


def _definitions(conftest: Path) -> dict[str, str]:
    source = conftest.read_text(encoding="utf-8")
    return {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    }


def test_the_three_copies_are_equal() -> None:
    tools, *others = (_definitions(conftest) for conftest in CONFTESTS)
    assert sorted(tools) == sorted(HELPERS)
    for conftest, copy in zip(CONFTESTS[1:], others, strict=True):
        assert copy == tools, conftest.relative_to(REPO_ROOT).as_posix()


def _kinds_by_declaring_layer() -> list[tuple[str, str]]:
    """``(kind, directory)`` for every top-level kind the registry's ``kinds`` answers.

    Includes ``base_type`` and ``mapping_set``, which no ``kind`` field
    discriminates. A layer of recursive storage (``swc``, placed by group) is
    not one these helpers compose.
    """
    registry = current_registry()
    return [
        (kind.kind, layer.storage.directory)
        for layer in registry.layers
        if not layer.storage.is_recursive
        for kind in layer.top_level_kinds
        if kind.kind in registry.kinds
    ]


def test_the_parametrisation_holds_the_kinds_without_a_discriminator() -> None:
    """The routing rows include the two data description kinds no ``kind`` field discriminates."""
    rows = dict(_kinds_by_declaring_layer())
    assert rows["base_type"] == "data_types/base"
    assert rows["mapping_set"] == "data_types/mapping_sets"


@pytest.mark.parametrize(("kind", "directory"), _kinds_by_declaring_layer())
def test_a_kind_is_stored_in_the_directory_of_the_layer_that_declares_it(kind: str, directory: str) -> None:
    assert model_element_storage_name("Probe", kind=kind) == f"{directory}/Probe.yaml"


@pytest.mark.parametrize("layer", [layer.name for layer in current_registry().layers if not layer.storage.is_recursive])
def test_a_named_layer_is_stored_in_its_own_directory(layer: str) -> None:
    directory = current_registry().storage(layer).directory
    assert model_element_storage_name("Probe", layer=layer) == f"{directory}/Probe.yaml"


def test_the_stem_is_the_caller_s_and_is_not_validated() -> None:
    # A test that writes a file named apart from its element passes that stem.
    assert model_element_storage_name("t~1", kind="value_type").endswith("/t~1.yaml")


@pytest.mark.parametrize("arguments", [{}, {"kind": "value_type", "layer": "mapping_sets"}])
def test_exactly_one_of_kind_and_layer_is_named(arguments: dict[str, str]) -> None:
    with pytest.raises(TypeError, match="exactly one of kind= and layer="):
        model_element_storage_name("Probe", **arguments)


def test_a_kind_no_layer_declares_is_refused() -> None:
    with pytest.raises(ValueError, match="no layer declares the kind 'no_such_kind'"):
        model_element_storage_name("Probe", kind="no_such_kind")


def test_a_nested_layer_directory_is_followed() -> None:
    (name, directory), _ = SIBLING_NESTED_LAYERS
    with use_registry(nested_registry(*SIBLING_NESTED_LAYERS)):
        assert model_element_storage_name("Probe", kind=name) == f"{directory}/Probe.yaml"
        assert model_element_storage_name("Probe", layer=name) == f"{directory}/Probe.yaml"
        assert model_element_path("/root", "Probe", kind=name) == Path("/root", *directory.split("/"), "Probe.yaml")


def test_the_path_is_the_storage_name_under_the_root(tmp_path: Path) -> None:
    assert model_element_path(tmp_path, "Probe", kind="value_type") == tmp_path / model_element_storage_name(
        "Probe", kind="value_type"
    )
    assert model_element_path(str(tmp_path), "Probe", layer="mapping_sets") == tmp_path / model_element_storage_name(
        "Probe", layer="mapping_sets"
    )


def test_a_payload_is_written_where_its_kind_is_stored_in_its_own_key_order(tmp_path: Path) -> None:
    payload = {"name": "Probe", "kind": "value_type", "base": "probe_u8"}
    path = write_model_element(tmp_path / "model", payload)
    assert path == model_element_path(tmp_path / "model", "Probe", kind="value_type")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == payload
    assert list(yaml.safe_load(path.read_text(encoding="utf-8"))) == ["name", "kind", "base"]


def test_a_payload_without_a_kind_is_written_into_the_named_layer(tmp_path: Path) -> None:
    payload = {"name": "ProbeMaps", "maps": []}
    path = write_model_element(tmp_path, payload, layer="mapping_sets")
    assert path == model_element_path(tmp_path, "ProbeMaps", layer="mapping_sets")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == payload


def test_a_payload_without_a_kind_and_no_layer_is_a_test_defect(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        write_model_element(tmp_path, {"name": "ProbeMaps", "maps": []})
