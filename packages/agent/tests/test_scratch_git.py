"""``scratch_git`` switches automatic maintenance off: in a fresh repository's own file, or on one command."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scratch_git import NO_AUTO_MAINTENANCE, quiet_maintenance


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
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    quiet_maintenance(tmp_path)

    assert (_local(tmp_path, "maintenance.auto"), _local(tmp_path, "gc.auto")) == ("false", "0")


def test_the_carried_arguments_switch_maintenance_off_for_the_command(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "--local", "gc.auto", "6700"], cwd=tmp_path, check=True)

    carried = (
        _effective(tmp_path, "maintenance.auto", *NO_AUTO_MAINTENANCE),
        _effective(tmp_path, "gc.auto", *NO_AUTO_MAINTENANCE),
    )

    assert carried == ("false", "0")
