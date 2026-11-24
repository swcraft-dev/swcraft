"""``build_artifacts.read_git_commit``: the commit an archive records, read from a scratch repository."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import build_artifacts_module
from scratch_git import quiet_maintenance

_ARTIFACTS = build_artifacts_module()


def test_read_git_commit_falls_back_to_unknown_outside_a_checkout(tmp_path: Path) -> None:
    # Traceability metadata must never be able to fail a build.
    assert _ARTIFACTS.read_git_commit(tmp_path) == "unknown"


def _commit(repo: Path, message: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Someone",
        "GIT_AUTHOR_EMAIL": "someone@example.com",
        "GIT_COMMITTER_NAME": "Someone",
        "GIT_COMMITTER_EMAIL": "someone@example.com",
    }
    git = ["git", "-c", "commit.gpgsign=false", "-C", str(repo)]
    subprocess.run([*git, "init", "--quiet"], check=True, env=env)
    quiet_maintenance(repo)
    subprocess.run([*git, "commit", "--quiet", "--allow-empty", "-m", message], check=True, env=env)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, check=True, env=env)
    return head.stdout.decode("ascii").strip()


def test_read_git_commit_names_head_without_a_source_trailer(tmp_path: Path) -> None:
    head = _commit(tmp_path, "an ordinary commit\n\nSource-Commit: not-a-sha")

    assert _ARTIFACTS.read_git_commit(tmp_path) == head


def test_read_git_commit_names_the_source_commit_of_an_assembled_snapshot(tmp_path: Path) -> None:
    # scripts/public_snapshot.py's commit names the gated commit it was taken from; an archive
    # built in the assembled tree (or the published copy) records that commit, not its own.
    source = "0123456789abcdef0123456789abcdef01234567"
    head = _commit(tmp_path, f"Public snapshot of 0123456789ab\n\nSource-Commit: {source}")

    assert head != source
    assert _ARTIFACTS.read_git_commit(tmp_path) == source


_SHA1_A = "0123456789abcdef0123456789abcdef01234567"
_SHA1_B = "fedcba9876543210fedcba9876543210fedcba98"
_SHA256 = "0123456789abcdef" * 4


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        pytest.param([_SHA1_A, _SHA1_B], _SHA1_B, id="two-trailers-the-last-wins"),
        pytest.param([_SHA1_A, "not-a-sha"], _SHA1_A, id="an-invalid-last-trailer-is-ignored"),
        pytest.param([_SHA256], _SHA256, id="sha-256"),
        pytest.param([_SHA1_A.upper()], None, id="uppercase-falls-back-to-head"),
        pytest.param([_SHA1_A[:12]], None, id="abbreviated-falls-back-to-head"),
        pytest.param([_SHA1_A + "0"], None, id="41-digits-falls-back-to-head"),
    ],
)
def test_read_git_commit_reads_only_a_full_lowercase_sha_and_the_last_one(
    tmp_path: Path, trailers: list[str], expected: str | None
) -> None:
    lines = "\n".join(f"Source-Commit: {value}" for value in trailers)
    head = _commit(tmp_path, f"Public snapshot\n\n{lines}")

    assert _ARTIFACTS.read_git_commit(tmp_path) == (expected or head)
