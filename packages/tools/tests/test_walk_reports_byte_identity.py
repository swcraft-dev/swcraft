"""The CLI generates every committed model tree's ARXML, at both versions, to its recorded digest.

The record is borrowed: this module reads ``RECORDED`` from ``test_diagram_switch_byte_identity.py``
rather than committing a second copy. The cost: a re-recording there moves this oracle with it and
no test here notices, so the two modules are one record read through two paths, not two
independent records. Some of its digests were re-frozen by later output changes (the enum
category among them; ``test_enum_category_refreeze.py`` maps each back).

What it adds to that module: it reaches the generators through the CLI (``swcraft generate arxml
--arxml-version`` over a manifest), which that module names as outside its reach. A refused tree
must exit 1 with the refusal on stderr and write no file.

This is a record, not a golden to refresh: a change that moves a digest is a change to ARXML
output and carries its own Qualification Impact section.

The termination handling re-freeze is undone before hashing, not re-recorded: every component now
declares ``handle_termination_and_restart: no_support`` and gains one
``HANDLE-TERMINATION-AND-RESTART`` line, which ``termination_strip_support.strip_recorded``
removes component by component; ``test_termination_refreeze.py`` gates the helper.

What it does not reach: a model tree that is not committed (the real tool exports); the component
exports of ``generate arxml --component`` / ``--all-components``, which the component-exchange
suites pin; ``validate``, which generates through the same ``generate_arxml`` call and is pinned by
``test_cli.py``; an edited writer whose bytes happened not to move; and the RST column of the
borrowed record, which no CLI command generates any more and stays unread.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from conftest import REPO_ROOT, _write_manifest
from swcraft_tools.cli import main
from termination_strip_support import strip_recorded
from test_diagram_switch_byte_identity import RECORDED

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [(tree, artifact, RECORDED[tree][index]) for tree in RECORDED for index, artifact in enumerate(_ARTIFACTS)]


def _project(tmp_path: Path, tree: str, arxml_version: str) -> Path:
    """A manifest over a copy of *tree*, declaring *arxml_version* so no override warning is printed."""
    shutil.copytree(REPO_ROOT / tree, tmp_path / "model")
    return _write_manifest(
        tmp_path,
        {
            "name": "byte-identity",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
            "arxml_version": arxml_version,
            "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
        },
    )


@pytest.mark.parametrize(("tree", "artifact", "recorded"), _CASES, ids=[f"{t}:{a}" for t, a, _ in _CASES])
def test_the_cli_generates_the_branch_points_bytes(
    tree: str, artifact: str, recorded: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    version = artifact.removeprefix("arxml-")
    manifest = _project(project, tree, version)
    argv = ["swcraft", "generate", "arxml", "--arxml-version", version, "--project", str(manifest)]
    output = project / "dist" / "model.arxml"
    monkeypatch.setattr("sys.argv", argv)

    if recorded.startswith("refused: "):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert recorded.removeprefix("refused: ") in capsys.readouterr().err
        assert not output.exists()
        return

    main()
    data = output.read_bytes()
    assert hashlib.sha256(strip_recorded(tree, artifact, data)).hexdigest() == recorded
