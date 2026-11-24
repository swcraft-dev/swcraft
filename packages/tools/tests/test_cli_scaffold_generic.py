"""``swcraft init``'s layer scaffolding comes from the model registry.

The oracle is the code the derivation replaced: ``fixtures/recorded_oracles/scaffold_directories.json`` was recorded
from the hand-written directory list and the directories a real ``swcraft init`` produced from it. It is never
re-recorded from the derivation; each later layer change was edited into it by hand, as its ``note`` records.

No shipped layer is optional, so "the rule drops an optional layer" is vacuous against the shipped contract; it
is asserted against a registry that declares one, so the mechanism is shown to bite. Order is not asserted as a
sequence: the derivation is in contract order, unlike the recorded list, and nothing observes the order.

:func:`_required_directories` restates the rule from a registry it names, a second implementation on purpose:
reading it out of ``_scaffold_model_directories`` would compare the function with itself. It is never the only
witness: the tests that call the scaffolder into ``tmp_path`` read the directories back off disk.

What these tests do not reach: anything ``swcraft init`` writes besides the model subdirectories (the manifest,
``.vscode/mcp.json``, the ``--integration moon`` template: ``test_cli.py``'s and ``test_template_contents.py``'s);
what is written into a scaffolded directory; ``swc``'s ``{group}/`` placement directories, created per element;
whether a scaffolded project validates.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from model_contract_fixtures import merged_contract
from swcraft_tools.cli import main
from swcraft_tools.cli.init_command import _scaffold_model_directories
from swcraft_tools.model_registry import (
    ModelRegistry,
    build_model_registry,
    production_registry,
    use_registry,
)
from synthetic_support import merged_binding, merged_registry

#: The frozen table, recorded from the code before the derivation.
FIXTURE = Path(__file__).parent / "fixtures" / "recorded_oracles" / "scaffold_directories.json"


def _frozen() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _subdirectories(model_root: Path) -> set[str]:
    """Every layer directory under *model_root*, model-root-relative and POSIX-spelled.

    Recursive, since a layer may be stored in a nested directory (``data_types/base``); a parent directory no
    shipped layer declares (``data_types``) is left out, so the comparison stays one of layer directories.
    """
    found = {path.relative_to(model_root).as_posix() for path in model_root.rglob("*") if path.is_dir()}
    registry = production_registry()
    declared = {registry.storage(name).directory for name in registry.layer_names}
    parents = {directory.rsplit("/", 1)[0] for directory in found if "/" in directory}
    return {directory for directory in found if directory in declared or directory not in parents}


def _required_directories(registry: ModelRegistry) -> tuple[str, ...]:
    """The scaffolding rule over *registry*: every non-optional layer's directory."""
    return tuple(facts.storage.directory for facts in registry.layers if not facts.optional)


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


# The derived rule equals the frozen list


def test_the_derived_rule_is_the_frozen_fifteen_directories() -> None:
    """Membership and count, which is everything the frozen list guaranteed."""
    frozen = _frozen()["declared"]
    derived = _required_directories(production_registry())
    assert set(derived) == set(frozen)
    assert len(derived) == len(frozen)


def test_the_derived_rule_has_no_duplicate_directory() -> None:
    """Two layers storing in one directory would scaffold one directory fewer, silently."""
    derived = _required_directories(production_registry())
    assert len(derived) == len(set(derived))


def test_no_shipped_layer_is_optional_so_the_rule_scaffolds_every_one() -> None:
    """No shipped layer is optional and the rule scaffolds every one: two different claims, both asserted."""
    registry = production_registry()
    # The shipped contract's layer count; it moves with each added or retired layer.
    assert [facts.optional for facts in registry.layers] == [False] * 15
    assert set(_required_directories(registry)) == {facts.storage.directory for facts in registry.layers}
    assert "resources" not in {facts.name for facts in registry.layers}


def test_no_declared_layer_is_optional_and_the_rule_still_drops_one() -> None:
    """A planted optional layer is dropped: the non-vacuity control the shipped contract cannot provide."""
    contract = merged_contract()
    (synthetic,) = [layer for layer in contract["layers"] if layer["id"] == "layer:synthetic"]
    synthetic["optional"] = True
    registry = build_model_registry(contract=contract, binding=merged_binding())

    assert registry.layer("synthetic").optional is True
    assert "synthetic" not in _required_directories(registry)
    assert set(_required_directories(registry)) == set(_frozen()["scaffolded"])


def test_the_derived_rule_is_contract_order_and_that_is_stated() -> None:
    """Contract order puts ``connectors`` before ``data_types/implementation``; the frozen list has the reverse."""
    derived = _required_directories(production_registry())
    frozen = _frozen()["declared"]
    assert list(derived) != frozen
    assert derived.index("connectors") < derived.index("data_types/implementation")
    assert frozen.index("data_types/implementation") < frozen.index("connectors")


def test_the_fixture_is_not_vacuous() -> None:
    """A positive control: an empty or truncated oracle would pass every check above."""
    frozen = _frozen()
    assert set(frozen) == {"declared", "scaffolded", "note"}
    assert len(frozen["declared"]) == 15
    assert sorted(frozen["declared"]) == frozen["scaffolded"]


# The function behind the view


def test_the_scaffolder_takes_an_optional_registry() -> None:
    """``registry=None`` resolved in the body, never a default expression evaluated at import."""
    parameter = inspect.signature(_scaffold_model_directories).parameters["registry"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_scaffolder_creates_the_frozen_ten(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _scaffold_model_directories(model_root)
    assert sorted(_subdirectories(model_root)) == _frozen()["scaffolded"]


def test_the_scaffolder_creates_only_directories(tmp_path: Path) -> None:
    """No seed file, no ``.gitkeep``: the layers are empty and the project has no elements."""
    model_root = tmp_path / "model"
    _scaffold_model_directories(model_root)
    assert [path for path in model_root.rglob("*") if path.is_file()] == []


def test_the_scaffolder_does_not_create_a_placement_directory(tmp_path: Path) -> None:
    """``swc``'s ``{group}/`` is per-element placement, written when an element is."""
    model_root = tmp_path / "model"
    _scaffold_model_directories(model_root)
    assert list((model_root / "swc").iterdir()) == []


def test_the_scaffolder_is_idempotent_over_an_existing_project(tmp_path: Path) -> None:
    """A second scaffold over an initialised directory neither raises nor disturbs what is there."""
    model_root = tmp_path / "model"
    _scaffold_model_directories(model_root)
    (model_root / "swc" / "sensors").mkdir()
    (model_root / "swc" / "sensors" / "Foo.yaml").write_text("name: Foo\n", encoding="utf-8")

    _scaffold_model_directories(model_root)

    # The group directory the test made is the one directory beyond the scaffolded ones.
    assert sorted(_subdirectories(model_root) - {"swc/sensors"}) == _frozen()["scaffolded"]
    assert (model_root / "swc" / "sensors" / "Foo.yaml").read_text(encoding="utf-8") == "name: Foo\n"


def test_the_scaffolder_returns_what_it_created_in_registry_order(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    created = _scaffold_model_directories(model_root)
    assert created == [model_root / directory for directory in _required_directories(production_registry())]


# A merged layer, with no source edit


def test_a_merged_registry_scaffolds_the_synthetic_layer(tmp_path: Path, merged: ModelRegistry) -> None:
    """A merged registry in the context adds ``synthetic/`` to the scaffolded directories."""
    model_root = tmp_path / "model"
    with use_registry(merged):
        _scaffold_model_directories(model_root)

    assert sorted(_subdirectories(model_root)) == sorted([*_frozen()["scaffolded"], "synthetic"])
    assert (model_root / "synthetic").is_dir()


def test_an_explicitly_passed_registry_needs_no_context_block(tmp_path: Path, merged: ModelRegistry) -> None:
    model_root = tmp_path / "model"
    _scaffold_model_directories(model_root, registry=merged)
    assert (model_root / "synthetic").is_dir()


def test_the_retired_layer_stays_absent_under_a_merged_registry(tmp_path: Path, merged: ModelRegistry) -> None:
    """Adding a layer must not bring the retired one back: sixteen directories, no ``resources``."""
    model_root = tmp_path / "model"
    with use_registry(merged):
        _scaffold_model_directories(model_root)

    assert "resources" not in _subdirectories(model_root)
    assert len(_subdirectories(model_root)) == 16


def test_an_explicit_registry_beats_the_context(tmp_path: Path, merged: ModelRegistry) -> None:
    """The argument wins over ``use_registry``, not only over the absence of one."""
    model_root = tmp_path / "model"
    with use_registry(merged):
        _scaffold_model_directories(model_root, registry=production_registry())

    assert "synthetic" not in _subdirectories(model_root)
    assert sorted(_subdirectories(model_root)) == _frozen()["scaffolded"]


# The command itself produces the frozen list


def test_swcraft_init_creates_exactly_the_frozen_directories(tmp_path: Path, monkeypatch) -> None:
    """End to end through ``main()``, which is where the frozen list was recorded from."""
    target = tmp_path / "fresh-project"
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
    main()

    assert sorted(_subdirectories(target / "model")) == _frozen()["scaffolded"]


def test_swcraft_init_reports_every_created_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    """The ``Created <path>`` line per directory is the output the command always had."""
    target = tmp_path / "fresh-project"
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target)])
    main()

    printed = capsys.readouterr().out
    for directory in _frozen()["scaffolded"]:
        assert f"Created {target.resolve() / 'model' / directory}" in printed
    assert f"Created {target.resolve() / 'model' / 'resources'}" not in printed
    # The hardware elements are reported as the directories are: they go through `_write_if_absent`.
    for element in ("CPU.yaml", "CORE0.yaml", "PARTITION0.yaml"):
        assert f"Created {target.resolve() / 'model' / 'hardware' / element}" in printed
