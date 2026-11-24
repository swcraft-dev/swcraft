"""The example project's ``.github/`` is what ``swcraft init`` writes.

``examples/basic/.github/`` holds the Copilot client's files of the ``vehicle-architecture``
template, rendered by ``swcraft init --integration moon`` with the default client set and
the example's project name. The template is the source: a change reaches the example by
re-running ``init`` and copying its ``.github/`` over. The comparison is byte for byte and
both ways, so an added or missing file fails as well as a changed one.

What this does not reach: the files ``init`` writes outside ``.github/`` (the manifest, the
moon build graph, ``.vscode/settings.json``, the model elements); the example keeps its
own manifest and model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO_ROOT
from swcraft_tools.cli import main
from swcraft_tools.yaml_load import load_yaml_document

EXAMPLE = REPO_ROOT / "examples" / "basic"

#: Where each file ``init`` writes under ``.github/`` lives in the example, by relative path.
#: The identity: no name differs, because nothing in the example's meaning requires one to.
EXAMPLE_PATH_FOR: dict[str, str] = {}


def _files_under(root: Path) -> dict[str, bytes]:
    """Every file below *root*, keyed by its POSIX path relative to *root*."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


@pytest.fixture(scope="module")
def init_github(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The ``.github/`` files ``swcraft init`` writes for the example's project name."""
    manifest = load_yaml_document((EXAMPLE / "swcraft.yaml").read_text(encoding="utf-8"))
    target = tmp_path_factory.mktemp("init") / "project"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", ["swcraft", "init", str(target), "--name", manifest["name"], "--integration", "moon"])
        main()
    return _files_under(target / ".github")


class TestTheExampleIsWhatInitWrites:
    def test_init_writes_the_copilot_files(self, init_github: dict[str, bytes]) -> None:
        """The default client is Copilot, and its three files are what ``.github/`` holds."""
        assert sorted(init_github) == [
            "agents/swcraft-architect.agent.md",
            "copilot-instructions.md",
            "hooks/swcraft.json",
        ]

    def test_the_example_holds_the_same_files(self, init_github: dict[str, bytes]) -> None:
        expected = {EXAMPLE_PATH_FOR.get(relative, relative) for relative in init_github}
        assert set(_files_under(EXAMPLE / ".github")) == expected, (
            "examples/basic/.github/ holds other files than `swcraft init --integration moon` writes"
        )

    def test_each_file_is_byte_equal(self, init_github: dict[str, bytes]) -> None:
        example = _files_under(EXAMPLE / ".github")
        drifted = [
            relative
            for relative, content in init_github.items()
            if example.get(EXAMPLE_PATH_FOR.get(relative, relative)) != content
        ]
        assert not drifted, (
            f"examples/basic/.github/ differs from `swcraft init`'s output in {drifted}: run "
            "`swcraft init <dir> --name swcraft-basic-example --integration moon` and copy its .github/ over"
        )

    def test_the_example_carries_no_mcp_configuration(self) -> None:
        """The extension registers the example's server as an init project's, so the example has no VS Code MCP file."""
        assert not (EXAMPLE / ".vscode" / "mcp.json").exists()
        assert not (EXAMPLE / ".vscode" / "mcp.windows.json").exists()
