"""The association gate: ``package.json``'s ``contributes.yamlValidation`` equals the generator's.

``python scripts/dev.py schemas`` writes the block into ``vscode_extension/package.json``
(the Node build must not resolve a ``.venv``), so it can drift like a committed schema
file. The comparison is parsed-JSON equality, order included; the writer's byte form is
pinned in ``test_editor_schema.py``.

What this module does not reach: whether the schema files the ``url``\\ s name exist
(``vscode_extension/src/yamlValidation.test.ts``), are byte-current
(``test_editor_schema_drift.py``), are packaged (``packageVsix.test.ts``) or are
registered by a running VS Code host.
"""

from __future__ import annotations

import json

from conftest import REPO_ROOT
from swcraft_tools.editor_schema import yaml_validation_entries

PACKAGE_JSON = REPO_ROOT / "vscode_extension" / "package.json"

REGENERATE = "regenerate with `python scripts/dev.py schemas` (it rewrites the block) and commit the result"


def _manifest() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def test_yaml_validation_block_equals_the_generated_entries() -> None:
    """The committed block is exactly what the generator renders, compared as JSON values."""
    committed = _manifest().get("contributes", {}).get("yamlValidation")
    assert committed is not None, f"{PACKAGE_JSON} carries no contributes.yamlValidation — {REGENERATE}"
    expected = yaml_validation_entries()
    assert committed == expected, (
        f"contributes.yamlValidation in {PACKAGE_JSON} drifted from the generator — {REGENERATE}"
    )


def test_extension_pack_names_the_two_members_and_no_hard_dependency() -> None:
    """Two ``extensionPack`` members, as pack members rather than hard dependencies.

    It sits on the Python side because ``write_yaml_validation`` re-renders the whole
    document, so a dropped sibling key shows here. It does not reach whether a member id
    resolves to a real extension.
    """
    manifest = _manifest()
    assert manifest.get("extensionPack") == [
        "redhat.vscode-yaml",
        "redhat.vscode-xml",
    ]
    assert "extensionDependencies" not in manifest


def test_categories_are_the_functional_set() -> None:
    """The four categories, each earned by a contribution.

    The reasoning and the declined categories are in ``vscode_extension/src/extensionPack.test.ts``;
    this is the second site a change of the set must edit.
    """
    assert _manifest().get("categories") == [
        "Programming Languages",
        "Visualization",
        "AI",
        "Extension Packs",
    ]


def test_engines_floor_is_the_highest_measured_pack_member_floor() -> None:
    """``engines.vscode`` is pinned at ``^1.110.0``, at least every pack member's floor.

    The members left need ``^1.67.0``; lowering the floor would declare the extension
    installable on hosts its own code was never run against. Pinned as equality so a
    re-measurement is a deliberate edit; ``vscode_extension/src/engineFloor.test.ts`` pins
    the ``@types/vscode`` devDependency to it. Not reached: no test re-reads a member's own
    manifest, so a member raising its floor goes unnoticed until re-measured by hand.
    """
    assert _manifest()["engines"]["vscode"] == "^1.110.0"
