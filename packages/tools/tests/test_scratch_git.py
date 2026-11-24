"""``scratch_git`` switches automatic maintenance off: in a fresh repository's own file, or on one command.

The ``agent`` tree's copy of the module is held to this one's code, and the git helper's ``init`` (the ``tools``
and ``mcp`` trees' one ``_git``) to the quieting it promises.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import scratch_git
from conftest import REPO_ROOT
from scratch_git import NO_AUTO_MAINTENANCE, git, quiet_maintenance


def _local(repo: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get", key],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"{key} is not set in {repo}'s own configuration: {result.stderr}"
    return result.stdout.strip()


def _effective(repo: Path, key: str, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments, "-C", str(repo), "config", "--get", key], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, f"{key} has no value in {repo}: {result.stderr}"
    return result.stdout.strip()


def test_a_quieted_repository_holds_both_keys_in_its_own_configuration(tmp_path: Path) -> None:
    # A separate git directory, as `commit_into_a_damaged_repository` makes: the keys land where git reads them.
    repo, git_directory = tmp_path / "repo", tmp_path / "repo.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--separate-git-dir", str(git_directory)], cwd=repo, check=True)

    quiet_maintenance(repo)

    assert (_local(repo, "maintenance.auto"), _local(repo, "gc.auto")) == ("false", "0")
    in_the_git_directory = subprocess.run(
        ["git", "config", "--file", str(git_directory / "config"), "--get", "gc.auto"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert in_the_git_directory.stdout.strip() == "0", in_the_git_directory.stderr


def test_the_carried_arguments_switch_maintenance_off_for_the_command(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "--local", "gc.auto", "6700"], cwd=tmp_path, check=True)

    carried = (
        _effective(tmp_path, "maintenance.auto", *NO_AUTO_MAINTENANCE),
        _effective(tmp_path, "gc.auto", *NO_AUTO_MAINTENANCE),
    )

    assert carried == ("false", "0")


def test_the_git_helper_quiets_the_repository_it_inits(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)

    assert (_local(tmp_path, "maintenance.auto"), _local(tmp_path, "gc.auto")) == ("false", "0")


def _code(path: Path) -> str:
    """The module's code without its docstring, which says which tree each copy serves."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return ast.dump(ast.Module(body=tree.body[1:], type_ignores=[]))


def test_the_agent_tree_s_copy_is_this_module_s_code() -> None:
    agent_copy = REPO_ROOT / "packages" / "agent" / "tests" / "support" / "scratch_git.py"
    assert _code(agent_copy) == _code(Path(scratch_git.__file__))
