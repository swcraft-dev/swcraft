"""The open-copy marker: ``public_snapshot.py assemble`` writes it, ``dev.py test-internal`` keys on it.

In an open copy ``test-internal`` says its tests are not part of the repository and exits 0; in the
work repository a missing tests folder still fails. The decision rests on the marker's presence and
exact text, never on the folder's absence alone, and a tree holding both is refused.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from conftest import dev_module, make_venv_marker, public_snapshot_module
from scratch_git import quiet_maintenance

_DEV = dev_module()
_SNAPSHOT = public_snapshot_module()

_ALLOW = "README.md\ntools/\npublic-paths.txt\n"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Someone",
            "GIT_AUTHOR_EMAIL": "someone@example.com",
            "GIT_COMMITTER_NAME": "Someone",
            "GIT_COMMITTER_EMAIL": "someone@example.com",
        }
    )
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.decode("utf-8")


def _source(tmp_path: Path, allow_list: str = _ALLOW, extra: dict[str, str] | None = None) -> Path:
    """A one-commit work repository with a tests folder of its own, which the allow-list keeps closed."""
    repo = tmp_path / "source"
    files = {
        "README.md": "open\n",
        "tools/a.py": "a = 1\n",
        f"{_DEV.INTERNAL_DIR}/tests/test_x.py": "closed\n",
        "public-paths.txt": allow_list,
        **(extra or {}),
    }
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "init", "--quiet", "--initial-branch=main")
    quiet_maintenance(repo)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "source")
    return repo


def _open_copy(tmp_path: Path) -> Path:
    """An open copy as ``assemble`` makes it: the marker written, no tests folder, no ``.venv``."""
    out = tmp_path / "public"
    _SNAPSHOT.assemble(out, repo=_source(tmp_path))
    return out


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, stdin=None, env=()) -> int:
        self.calls.append([str(part) for part in argv])
        return 0


def _test_internal(repo_root: Path, *flags: str) -> tuple[int, _Runner]:
    runner = _Runner()
    return _DEV.main(["--repo-root", str(repo_root), *flags, _DEV.TEST_INTERNAL_COMMAND], run=runner), runner


# ---------------------------------------------------------------------------
# The marker in the snapshot
# ---------------------------------------------------------------------------


def test_both_scripts_name_the_same_marker_and_text() -> None:
    assert _DEV.OPEN_COPY_MARKER == _SNAPSHOT.OPEN_COPY_MARKER
    assert _DEV.OPEN_COPY_MARKER_TEXT == _SNAPSHOT.OPEN_COPY_MARKER_TEXT


def test_assemble_writes_and_commits_the_marker_and_check_accepts_it(tmp_path: Path) -> None:
    out = _open_copy(tmp_path)

    assert (out / _SNAPSHOT.OPEN_COPY_MARKER).read_bytes() == _SNAPSHOT.OPEN_COPY_MARKER_TEXT.encode("utf-8")
    committed = _git(out, "ls-tree", "HEAD", "--", _SNAPSHOT.OPEN_COPY_MARKER).split()
    assert committed[0] == "100644"
    assert not (out / _DEV.INTERNAL_DIR).exists()
    assert _SNAPSHOT.check(out, repo=tmp_path / "source") == []


def test_check_finds_a_snapshot_without_the_marker(tmp_path: Path) -> None:
    out = _open_copy(tmp_path)
    (out / _SNAPSHOT.OPEN_COPY_MARKER).unlink()

    assert _SNAPSHOT.check(out, repo=tmp_path / "source") == [f"missing: {_SNAPSHOT.OPEN_COPY_MARKER}"]


def test_check_finds_a_marker_with_other_text(tmp_path: Path) -> None:
    out = _open_copy(tmp_path)
    sha = _git(tmp_path / "source", "rev-parse", "HEAD").strip()
    (out / _SNAPSHOT.OPEN_COPY_MARKER).write_text("forged\n", encoding="utf-8", newline="\n")

    assert _SNAPSHOT.check(out, repo=tmp_path / "source") == [
        f"differs on disk from {sha[:12]}: {_SNAPSHOT.OPEN_COPY_MARKER}"
    ]


def test_an_allow_list_opening_a_file_of_the_markers_name_is_refused(tmp_path: Path) -> None:
    marker = _SNAPSHOT.OPEN_COPY_MARKER
    repo = _source(tmp_path, allow_list=_ALLOW + f"{marker}\n", extra={marker: "a source file\n"})

    with pytest.raises(_SNAPSHOT.SnapshotError, match=re.escape(f"opens {marker}")):
        _SNAPSHOT.assemble(tmp_path / "public", repo=repo)


# ---------------------------------------------------------------------------
# test-internal's four cases
# ---------------------------------------------------------------------------


def test_in_an_open_copy_it_says_so_and_exits_zero_without_a_venv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _open_copy(tmp_path)
    assert not (out / _DEV.VENV_DIRNAME).exists()

    returncode, runner = _test_internal(out)

    assert returncode == 0
    assert runner.calls == []
    assert _DEV.OPEN_COPY_NOTICE in capsys.readouterr().out


def test_a_marker_with_windows_line_endings_is_still_the_marker(tmp_path: Path) -> None:
    root = tmp_path / "copy"
    root.mkdir()
    crlf = _DEV.OPEN_COPY_MARKER_TEXT.replace("\n", "\r\n").encode("utf-8")
    (root / _DEV.OPEN_COPY_MARKER).write_bytes(crlf)

    assert _DEV.open_copy_notice(root) == _DEV.OPEN_COPY_NOTICE


def test_in_the_work_repository_a_missing_tests_folder_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_venv_marker(tmp_path)

    returncode, runner = _test_internal(tmp_path)

    assert returncode == 1
    assert runner.calls == []
    assert f"{_DEV.INTERNAL_DIR}/ is missing" in capsys.readouterr().err


def test_in_the_work_repository_the_tests_run(tmp_path: Path) -> None:
    make_venv_marker(tmp_path)
    (tmp_path / _DEV.INTERNAL_DIR).mkdir()

    returncode, runner = _test_internal(tmp_path)

    assert returncode == 0
    assert len(runner.calls) == 2, "ruff, then pytest"


def test_a_marker_beside_the_tests_folder_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A marker copied into the work repository can neither skip its tests nor run them unannounced."""
    make_venv_marker(tmp_path)
    (tmp_path / _DEV.INTERNAL_DIR).mkdir()
    (tmp_path / _DEV.OPEN_COPY_MARKER).write_text(_DEV.OPEN_COPY_MARKER_TEXT, encoding="utf-8", newline="\n")

    returncode, runner = _test_internal(tmp_path)

    assert returncode == 1
    assert runner.calls == []
    assert _DEV.OPEN_COPY_NOTICE not in capsys.readouterr().out


@pytest.mark.parametrize(
    "text", ["", "open copy\n", _DEV.OPEN_COPY_MARKER_TEXT + "more\n"], ids=["empty", "other", "longer"]
)
def test_a_stray_file_of_the_markers_name_is_no_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    make_venv_marker(tmp_path)
    (tmp_path / _DEV.OPEN_COPY_MARKER).write_text(text, encoding="utf-8", newline="\n")

    returncode, runner = _test_internal(tmp_path)

    assert returncode == 1
    assert runner.calls == []
    assert _DEV.OPEN_COPY_NOTICE not in capsys.readouterr().out


def test_a_folder_of_the_markers_name_is_no_marker(tmp_path: Path) -> None:
    (tmp_path / _DEV.OPEN_COPY_MARKER).mkdir()

    with pytest.raises(_DEV.DevError):
        _DEV.open_copy_notice(tmp_path)


@pytest.mark.parametrize("flag", ["--require-rhapsody-export", "--require-second-export"])
def test_an_open_copy_refuses_a_demanded_export_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    returncode, runner = _test_internal(_open_copy(tmp_path), flag)

    assert returncode == 1
    assert runner.calls == []
    assert _DEV.OPEN_COPY_NOTICE not in capsys.readouterr().out
