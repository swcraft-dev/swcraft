"""The session's commit gate reads storage facts from the kernel.

Which directory holds which layer, which suffix a layer's files carry and which layer
nests are the registry's ``StorageRecord``, resolved per call; ``session.py`` holds no
layer table. The tests also run a session over a layer only a ``use_registry`` block
declares. They do not reach a storage record shaped unlike the shipped ones (a nesting
depth beyond one, a second placement field: the contract declares none), the audit
rebasing or the fingerprint race.

**The whole-tree fingerprint is deliberately not generic** and is asserted here to
have stayed that way: ``model_fingerprint`` walks every file under the model root,
including files no layer reads, so a foreign file cannot hide from the baseline.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from model_contract_fixtures import merged_contract
from swcraft_tools import session as session_mod
from swcraft_tools.model_registry import (
    ModelRegistry,
    ModelRegistryError,
    build_model_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.session import ScopedSession, UnexplainedChangeError, model_fingerprint
from synthetic_support import (
    OVERLAPPING_NESTED_LAYERS,
    SIBLING_NESTED_LAYERS,
    merged_binding,
    merged_registry,
    nested_registry,
)

SYNTHETIC_LAYER = "synthetic"


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _make_project(root: Path) -> tuple[Path, Path]:
    """A minimal real project holding one SWC; returns ``(project_dir, model_root)``."""
    project_dir = root / "real"
    model_root = project_dir / "model"
    _write_yaml(model_root / "swc" / "SwcOne.yaml", {"name": "SwcOne", "type": "application"})
    _write_yaml(project_dir / "swcraft.yaml", {"name": "TestProject", "model_root": "model"})
    return project_dir, model_root


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


# ---------------------------------------------------------------------------
# The deleted constant
# ---------------------------------------------------------------------------


def test_a_file_with_a_foreign_suffix_is_explained_by_nothing(tmp_path: Path) -> None:
    """A file no layer's glob finds is explained by nothing, even naming an element the diff reports."""
    registry = production_registry()
    identity = registry.layer("swc").identity_field
    foreign = tmp_path / "SwcOne.txt"
    _write_yaml(foreign, {identity: "SwcOne", "type": "application"})
    touched = {"swcs": {"SwcOne"}}

    assert session_mod._is_explained("swc/SwcOne.txt", foreign, touched, registry=registry) is False
    assert session_mod._is_explained("swc/SwcOne.yaml", foreign, touched, registry=registry) is True


def test_the_accepted_suffix_is_the_layers_declared_one(tmp_path: Path) -> None:
    """The accepted suffix is the storage record's: a layer declaring ``.md`` flips which file is accepted."""
    contract = copy.deepcopy(merged_contract())
    for layer in contract["layers"]:
        if layer["name"] == SYNTHETIC_LAYER:
            layer["storage"]["fileSuffix"] = ".md"
    altered = build_model_registry(contract=contract, binding=merged_binding())

    element = tmp_path / "Alpha.md"
    _write_yaml(element, {"name": "Alpha", "composition": "VehicleTop"})
    touched = {SYNTHETIC_LAYER: {"Alpha"}}

    assert session_mod._is_explained("synthetic/Alpha.md", element, touched, registry=altered) is True
    assert session_mod._is_explained("synthetic/Alpha.yaml", element, touched, registry=altered) is False
    assert session_mod._is_explained("synthetic/Alpha.md", element, touched, registry=merged_registry()) is False


# ---------------------------------------------------------------------------
# The two derived views are gone, and the gate answers without them
# ---------------------------------------------------------------------------


def test_the_module_declares_neither_deleted_layer_table() -> None:
    """No layer table returns: ``_is_explained`` resolves the registry per call, so it would be a second description.

    ``test_generic_core_gate.py`` holds the same over the whole package by an AST scan.
    """
    assert not hasattr(session_mod, "_LAYER_DIRECTORIES")
    assert not hasattr(session_mod, "_RECURSIVE_LAYER_DIRECTORIES")


def test_the_commit_gate_answers_both_questions_from_the_registry(tmp_path: Path) -> None:
    """Directory-to-layer and nesting resolve with no module attribute to fall back on."""
    element = tmp_path / "SwcOne.yaml"
    _write_yaml(element, {"name": "SwcOne", "type": "application"})
    nested = tmp_path / "nested.yaml"
    _write_yaml(nested, {"name": "SwcTwo", "type": "application"})
    touched = {"swcs": {"SwcOne", "SwcTwo"}}

    assert session_mod._is_explained("swc/SwcOne.yaml", element, touched) is True
    assert session_mod._is_explained("swc/chassis/SwcTwo.yaml", nested, touched) is True
    assert session_mod._is_explained("interfaces/sub/SwcTwo.yaml", nested, touched) is False


# ---------------------------------------------------------------------------
# The sixteenth layer, through a real session
# ---------------------------------------------------------------------------


def test_the_touched_identities_of_a_layer_with_no_adapter_field_are_keyed_by_its_name(
    tmp_path: Path, merged: ModelRegistry
) -> None:
    """``swcs`` is the adapter's name for ``swc``; a layer with no adapter field keeps its own."""
    project_dir, _ = _make_project(tmp_path)
    with use_registry(merged):
        session = ScopedSession(str(project_dir / "swcraft.yaml"))
        try:
            session.open()
            _write_yaml(
                Path(session.shadow_model_root) / SYNTHETIC_LAYER / "Alpha.yaml",
                {"name": "Alpha", "title": "First", "composition": "VehicleTop"},
            )
            touched = session_mod._touched_identities(session.diff())
        finally:
            session.abandon()

    assert touched[SYNTHETIC_LAYER] == {"Alpha"}
    assert "swcs" in touched, "a layer the binding names keeps the adapter's spelling"
    assert "swc" not in touched, "and only that spelling — the compat test pins all ten keys"


def test_a_change_under_the_synthetic_layer_is_fingerprinted_explained_and_committed(
    tmp_path: Path, merged: ModelRegistry
) -> None:
    """The whole session round trip for a layer no shipped source names."""
    project_dir, model_root = _make_project(tmp_path)
    before = model_fingerprint(str(model_root))

    with use_registry(merged):
        session = ScopedSession(str(project_dir / "swcraft.yaml"))
        try:
            session.open()
            _write_yaml(
                Path(session.shadow_model_root) / SYNTHETIC_LAYER / "Alpha.yaml",
                {"name": "Alpha", "title": "First", "composition": "VehicleTop"},
            )
            applied = session.commit(session.diff())
        finally:
            session.abandon()

    target = model_root / SYNTHETIC_LAYER / "Alpha.yaml"
    assert applied == [str(target)]
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["name"] == "Alpha"
    assert model_fingerprint(str(model_root)) != before, "the whole-tree walk sees the new layer's file"


def test_the_fingerprint_sees_a_file_no_layer_reads(tmp_path: Path) -> None:
    """The baseline's ``rglob("*")`` is deliberately not a layer walk: a change to anything refuses the commit."""
    model_root = tmp_path / "model"
    _write_yaml(model_root / "swc" / "SwcOne.yaml", {"name": "SwcOne", "type": "application"})
    before = model_fingerprint(str(model_root))

    (model_root / "notes.txt").write_text("read by no layer", encoding="utf-8")

    assert model_fingerprint(str(model_root)) != before


def test_a_foreign_suffix_in_the_synthetic_layer_refuses_the_commit(tmp_path: Path, merged: ModelRegistry) -> None:
    """A file no layer reads cannot be accounted for, so the commit is refused before any file is applied."""
    project_dir, model_root = _make_project(tmp_path)

    with use_registry(merged):
        session = ScopedSession(str(project_dir / "swcraft.yaml"))
        try:
            session.open()
            shadow = Path(session.shadow_model_root)
            _write_yaml(shadow / SYNTHETIC_LAYER / "Alpha.yaml", {"name": "Alpha", "composition": "VehicleTop"})
            (shadow / SYNTHETIC_LAYER / "Alpha.txt").write_text("not a model file", encoding="utf-8")

            with pytest.raises(UnexplainedChangeError) as excinfo:
                session.commit(session.diff())
        finally:
            session.abandon()

    assert "synthetic/Alpha.txt" in str(excinfo.value)
    assert not (model_root / SYNTHETIC_LAYER).exists(), "the whole commit is refused, not the surplus file alone"


def test_the_synthetic_layer_is_unexplained_under_the_shipped_registry(tmp_path: Path, merged: ModelRegistry) -> None:
    """Staged under the merged registry, committed under the shipped one: the undeclared layer explains nothing."""
    project_dir, model_root = _make_project(tmp_path)

    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        with use_registry(merged):
            session.open()
            _write_yaml(
                Path(session.shadow_model_root) / SYNTHETIC_LAYER / "Alpha.yaml",
                {"name": "Alpha", "composition": "VehicleTop"},
            )
            diff = session.diff()
        with pytest.raises(UnexplainedChangeError):
            session.commit(diff)
    finally:
        session.abandon()

    assert not (model_root / SYNTHETIC_LAYER).exists()


# ---------------------------------------------------------------------------
# A layer stored in a directory of two segments
# ---------------------------------------------------------------------------


class TestTheLongestDeclaredDirectoryNamesTheLayer:
    """The commit gate resolves a written file to the layer of the LONGEST directory prefixing it.

    A first-segment rule refuses a nested layer's file; a first-match rule is fail-open, explaining
    ``synthetic/alpha/Foo.yaml`` out of the flat layer's touched identities. A layer inside another's
    directory (``OVERLAPPING_NESTED_LAYERS``) is refused at build, so the longest match is asserted on
    two sibling nested layers (``SIBLING_NESTED_LAYERS``), where the shorter directory is no layer's.
    """

    def test_a_layer_inside_another_layers_directory_is_refused_at_build(self) -> None:
        with pytest.raises(ModelRegistryError, match="may not be a prefix of another's"):
            nested_registry(*OVERLAPPING_NESTED_LAYERS)

    def test_the_depth_test_applies_below_the_longest_directory(self, tmp_path: Path) -> None:
        siblings = nested_registry(*SIBLING_NESTED_LAYERS)
        element = tmp_path / "Foo.yaml"
        _write_yaml(element, {"name": "Foo", "composition": "VehicleTop"})
        touched = {session_mod._touched_key(siblings.layer(name)): {"Foo"} for name, _ in SIBLING_NESTED_LAYERS}

        for path in ("nest/alpha/sub/Foo.yaml", "nest/other/Foo.yaml", "nest/alpha/Foo.txt"):
            assert session_mod._is_explained(path, element, touched, registry=siblings) is False, path

    def test_two_sibling_nested_layers_under_a_parent_no_layer_claims(self, tmp_path: Path) -> None:
        siblings = nested_registry(*SIBLING_NESTED_LAYERS)
        element = tmp_path / "Foo.yaml"
        _write_yaml(element, {"name": "Foo", "composition": "VehicleTop"})
        touched = {session_mod._touched_key(siblings.layer(name)): {"Foo"} for name, _ in SIBLING_NESTED_LAYERS}

        assert session_mod._is_explained("nest/alpha/Foo.yaml", element, touched, registry=siblings) is True
        assert session_mod._is_explained("nest/beta/Foo.yaml", element, touched, registry=siblings) is True
        assert session_mod._is_explained("nest/Foo.yaml", element, touched, registry=siblings) is False

    @pytest.mark.parametrize(
        ("touched_layer", "other_layer"), [("nest_alpha", "nest_beta"), ("nest_beta", "nest_alpha")]
    )
    def test_a_file_is_explained_only_by_its_own_layers_touched_names(
        self, tmp_path: Path, touched_layer: str, other_layer: str
    ) -> None:
        """The name "Foo" touched in ONE sibling explains that sibling's file and never the other's (fail-open)."""
        siblings = nested_registry(*SIBLING_NESTED_LAYERS)
        directory = dict(SIBLING_NESTED_LAYERS)
        element = tmp_path / "Foo.yaml"
        _write_yaml(element, {"name": "Foo", "composition": "VehicleTop"})
        touched = {session_mod._touched_key(siblings.layer(touched_layer)): {"Foo"}}

        own = f"{directory[touched_layer]}/Foo.yaml"
        other = f"{directory[other_layer]}/Foo.yaml"
        assert session_mod._is_explained(own, element, touched, registry=siblings) is True
        assert session_mod._is_explained(other, element, touched, registry=siblings) is False

    def test_a_change_under_the_nested_layer_is_explained_and_committed(self, tmp_path: Path) -> None:
        """The whole session round trip for a layer stored in a directory of two segments."""
        project_dir, model_root = _make_project(tmp_path)

        with use_registry(nested_registry(*SIBLING_NESTED_LAYERS)):
            session = ScopedSession(str(project_dir / "swcraft.yaml"))
            try:
                session.open()
                _write_yaml(
                    Path(session.shadow_model_root) / "nest" / "alpha" / "Alpha.yaml",
                    {"name": "Alpha", "title": "First", "composition": "VehicleTop"},
                )
                applied = session.commit(session.diff())
            finally:
                session.abandon()

        target = model_root / "nest" / "alpha" / "Alpha.yaml"
        assert applied == [str(target)]
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["name"] == "Alpha"
