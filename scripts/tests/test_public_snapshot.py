"""Tests for ``scripts/public_snapshot.py`` -- the open copy's assembler and its check.

Every case builds a small git repository in ``tmp_path``, with global and system git configuration
switched off so a developer's defaults cannot change an answer. The last class validates this
repository's own ``public-paths.txt`` at ``HEAD``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
from conftest import REPO_ROOT, build_artifacts_module, public_snapshot_module
from scratch_git import quiet_maintenance
from symlink_support import skip_without_symlink_support

_SNAPSHOT = public_snapshot_module()
SnapshotError = _SNAPSHOT.SnapshotError

_DATE = "1700000000 +0100"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.delenv(name, raising=False)


def _git(repo: Path, *args: str, date: str = _DATE) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Someone",
            "GIT_AUTHOR_EMAIL": "someone@example.com",
            "GIT_COMMITTER_NAME": "Someone",
            "GIT_COMMITTER_EMAIL": "someone@example.com",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
    )
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.decode("utf-8")


def _write(root: Path, relative: str, content: str | bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8", newline="\n")


def _repo(tmp_path: Path, files: dict[str, str | bytes], allow_list: str | None, object_format: str = "sha1") -> Path:
    """A one-commit repository holding *files* and, unless ``None``, the allow-list."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}")
    quiet_maintenance(repo)
    for relative, content in files.items():
        _write(repo, relative, content)
    if allow_list is not None:
        _write(repo, "public-paths.txt", allow_list)
    _git(repo, "add", "--all", "--force")
    _git(repo, "commit", "--quiet", "-m", "source commit")
    return repo


_FILES: dict[str, str | bytes] = {
    "README.md": "open readme\n",
    "ruff.toml": "line-length = 120\n",
    "NOTES.md": "closed root file\n",
    "tools/a.py": "a = 1\n",
    "tools/deep/nested/b.py": "b = 2\n",
    "tools/ignored.log": "tracked although ignored\n",
    "docs/guide/page.md": "guide\n",
    "docs/plans/plan.md": "closed plan\n",
    ".gitignore": "*.log\n",
}

_ALLOW = "# comment\n\ntools/\ndocs/guide/   # inline comment\nREADME.md\nruff.toml\n.gitignore\npublic-paths.txt\n"

_OPEN = {
    "README.md",
    "ruff.toml",
    ".gitignore",
    "public-paths.txt",
    "tools/a.py",
    "tools/deep/nested/b.py",
    "tools/ignored.log",
    "docs/guide/page.md",
}

#: What a snapshot of ``_FILES`` holds: the open files and the open-copy marker ``assemble`` writes.
_SNAPSHOTTED = _OPEN | {_SNAPSHOT.OPEN_COPY_MARKER}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


class TestAssemble:
    def test_assembling_starts_no_background_maintenance(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``commit`` starts ``git maintenance run --auto --detach`` unless configured off.

        That background process writes into the snapshot's ``.git`` while it is read and removed;
        git's own trace of every process it starts shows none.
        """
        repo = _repo(tmp_path, _FILES, _ALLOW)
        trace = tmp_path / "trace.json"
        monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
        _SNAPSHOT.assemble(tmp_path / "public", repo=repo)
        started = [
            event["argv"]
            for line in trace.read_text(encoding="utf-8").splitlines()
            if (event := json.loads(line)).get("event") == "start"
        ]
        assert any("commit" in argv for argv in started), "the trace holds the snapshot's commit"
        assert not [argv for argv in started if "maintenance" in argv or "gc" in argv], started

    def test_the_snapshot_is_exactly_the_open_tracked_files(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        # Neither an untracked file nor an ignored one in the working tree enters.
        _write(repo, "tools/untracked.py", "x = 0\n")
        _write(repo, "tools/new.log", "ignored and untracked\n")
        out = tmp_path / "public"

        sha = _SNAPSHOT.assemble(out, repo=repo, rev="HEAD")

        assert set(_tree(out)) == _SNAPSHOTTED
        assert _tree(out)["tools/deep/nested/b.py"] == b"b = 2\n"
        assert sha == _git(repo, "rev-parse", "HEAD").strip()
        assert set(_git(out, "ls-files").split()) == _SNAPSHOTTED

    def test_the_commit_carries_the_fixed_identity_the_source_date_and_the_trailer(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"

        sha = _SNAPSHOT.assemble(out, repo=repo, rev="main")

        fields = _git(out, "log", "-1", "--format=%an|%ae|%cn|%ce|%ad|%cd", "--date=raw").strip().split("|")
        assert fields == ["SWCraft snapshot", "snapshot@invalid"] * 2 + [_DATE, _DATE]
        assert _git(out, "rev-list", "--count", "HEAD").strip() == "1"
        assert _SNAPSHOT.source_trailer(out) == sha
        assert _git(out, "log", "-1", "--format=%B").rstrip().endswith(f"Source-Commit: {sha}")

    @pytest.mark.parametrize(
        ("trailers", "expected"),
        [
            (["<sha>", "garbage"], "<sha>"),
            (["garbage", "<sha>"], "<sha>"),
            (["garbage"], None),
            (["<SHA>"], None),
            (["<sha12>"], None),
        ],
        ids=["full-then-garbage", "garbage-then-full", "garbage-only", "uppercase", "abbreviated"],
    )
    def test_the_trailer_is_read_by_build_artifacts_own_rule(
        self, tmp_path: Path, trailers: list[str], expected: str | None
    ) -> None:
        """The last full lowercase SHA wins and anything else is ignored -- the rule ``read_git_commit`` applies."""
        repo = _repo(tmp_path, _FILES, _ALLOW)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        spelled = {"<sha>": sha, "<SHA>": sha.upper(), "<sha12>": sha[:12], "garbage": "garbage"}
        body = "".join(f"Source-Commit: {spelled[value]}\n" for value in trailers)
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", f"subject\n\n{body}")

        found = _SNAPSHOT.source_trailer(repo)

        assert found == (None if expected is None else spelled[expected])
        if expected is not None:
            assert build_artifacts_module().read_git_commit(repo) == found

    def test_an_archive_built_in_the_snapshot_names_the_source_commit_and_its_time(self, tmp_path: Path) -> None:
        artifacts = build_artifacts_module()
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"

        sha = _SNAPSHOT.assemble(out, repo=repo)

        assert artifacts.read_git_commit(out) == sha
        assert artifacts.read_git_commit_epoch(out) == artifacts.read_git_commit_epoch(repo) == "1700000000"

    def test_assembling_the_assembled_tree_gives_a_byte_identical_tree(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        first, second = tmp_path / "first", tmp_path / "second"
        _SNAPSHOT.assemble(first, repo=repo)

        _SNAPSHOT.assemble(second, repo=first)

        assert _tree(second) == _tree(first)
        assert _git(second, "rev-parse", "HEAD^{tree}") == _git(first, "rev-parse", "HEAD^{tree}")
        assert _git(second, "log", "-1", "--format=%cd", "--date=raw").strip() == _DATE

    def test_the_executable_bit_comes_from_the_source_tree_not_the_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REV's tree supplies the executable bit even where git sees none on disk.

        Git for Windows initialises with ``core.filemode=false``; forced here for every git call, so the
        snapshot's ``add`` sees no executable bit on disk, and only REV's tree can supply it.
        """
        repo = _repo(tmp_path, _FILES, _ALLOW)
        _git(repo, "update-index", "--chmod=+x", "--", "tools/a.py")
        _git(repo, "commit", "--quiet", "-m", "an executable script")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.filemode")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
        out = tmp_path / "public"

        _SNAPSHOT.assemble(out, repo=repo)

        modes = {line.split()[3]: line.split()[0] for line in _git(out, "ls-tree", "-r", "HEAD").splitlines()}
        assert modes["tools/a.py"] == "100755"
        assert modes["tools/deep/nested/b.py"] == "100644"
        assert _SNAPSHOT.check(out, repo=repo) == []

    def test_an_older_revision_is_read_with_its_own_allow_list(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        old = _git(repo, "rev-parse", "HEAD").strip()
        _write(repo, "public-paths.txt", _ALLOW + "NOTES.md\n")
        _git(repo, "commit", "--quiet", "-am", "open the notes")
        out = tmp_path / "public"

        _SNAPSHOT.assemble(out, repo=repo, rev=old)

        assert "NOTES.md" not in _tree(out)

    def test_a_non_empty_output_folder_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _write(out, "left-over.txt", "x\n")

        with pytest.raises(SnapshotError, match="not empty"):
            _SNAPSHOT.assemble(out, repo=repo)

    def test_an_existing_empty_output_folder_is_used(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        out.mkdir()

        _SNAPSHOT.assemble(out, repo=repo)

        assert set(_tree(out)) == _SNAPSHOTTED

    def test_a_revision_that_is_not_a_commit_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)

        with pytest.raises(SnapshotError, match="is not a commit"):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo, rev="no-such-branch")

    def test_an_uncommitted_allow_list_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, None)
        _write(repo, "public-paths.txt", _ALLOW)

        with pytest.raises(SnapshotError, match="public-paths.txt is not committed"):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo)

    def test_an_allow_list_with_a_byte_order_mark_is_read(self, tmp_path: Path) -> None:
        """A Windows editor may save the list with a UTF-8 BOM; its first entry is still an entry."""
        repo = _repo(tmp_path, _FILES, None)
        _write(repo, "public-paths.txt", "\ufeff" + "tools/\n" + _ALLOW.replace("tools/\n", ""))
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "-m", "a list with a BOM")
        out = tmp_path / "public"

        _SNAPSHOT.assemble(out, repo=repo)

        assert set(_tree(out)) == _SNAPSHOTTED
        assert _SNAPSHOT.check(out, repo=repo) == []

    def test_an_interpreter_without_the_data_filter_is_refused_before_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        monkeypatch.delattr(tarfile, "data_filter")
        out = tmp_path / "public"

        with pytest.raises(SnapshotError, match=r"needs Python 3\.11\.4 or later"):
            _SNAPSHOT.assemble(out, repo=repo)
        assert not out.exists(), "a refused interpreter writes nothing"


# ---------------------------------------------------------------------------
# The refusals of the allow-list
# ---------------------------------------------------------------------------


class TestAllowListRefusals:
    @pytest.mark.parametrize(
        ("allow_list", "message"),
        [
            pytest.param("tools/\nmissing/\n", r"public-paths.txt:2: 'missing/' does not exist", id="absent-folder"),
            pytest.param("README.md\nABSENT.md\n", r"public-paths.txt:2: 'ABSENT.md' does not exist", id="absent-file"),
            pytest.param(
                "tools/a.py\n",
                r"public-paths.txt:1: 'tools/a.py' is a file entry that is not at the root",
                id="file-below-root",
            ),
            pytest.param(
                "tools/\nREADME.md\ntools/\n", r"public-paths.txt:3: 'tools/' duplicates line 1", id="duplicate"
            ),
            pytest.param(
                "docs/\ndocs/guide/\n",
                r"public-paths.txt:2: 'docs/guide/' is inside 'docs/' \(line 1\)",
                id="inside-another",
            ),
            pytest.param(
                "docs/guide/\ndocs/\n",
                r"public-paths.txt:1: 'docs/guide/' is inside 'docs/' \(line 2\)",
                id="inside-a-later",
            ),
            pytest.param("tools\n", r"public-paths.txt:1: 'tools' names a folder", id="folder-without-slash"),
            pytest.param("README.md/\n", r"public-paths.txt:1: 'README.md/' names a file", id="file-with-slash"),
            pytest.param("/tools/\n", r"'/tools/' is not a canonical relative path", id="absolute"),
            pytest.param("docs/../tools/\n", r"is not a canonical relative path", id="dot-dot"),
            pytest.param("./tools/\n", r"is not a canonical relative path", id="dot"),
            pytest.param("docs//guide/\n", r"is not a canonical relative path", id="double-slash"),
            pytest.param("/\n", r"is not a canonical relative path", id="root"),
        ],
    )
    def test_each_refusal_names_the_entry_and_its_line(self, tmp_path: Path, allow_list: str, message: str) -> None:
        repo = _repo(tmp_path, _FILES, allow_list)

        with pytest.raises(SnapshotError, match=message):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo)
        assert not (tmp_path / "public").exists(), "a refused list assembles nothing"

    @pytest.mark.parametrize(
        "allow_list",
        [pytest.param("", id="empty"), pytest.param("# only a comment\n\n   # another\n", id="comments-only")],
    )
    def test_a_list_with_no_entry_is_refused_rather_than_archiving_the_whole_commit(
        self, tmp_path: Path, allow_list: str
    ) -> None:
        repo = _repo(tmp_path, _FILES, allow_list)

        with pytest.raises(SnapshotError, match=r"public-paths.txt at [0-9a-f]{12} names no entry"):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo)
        assert not (tmp_path / "public").exists(), "an empty list assembles nothing"

    def test_an_empty_folder_entry_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, "tools/\nvendor/\n")
        # A folder holding only a submodule carries no file into the archive.
        head = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/module")
        _git(repo, "commit", "--quiet", "-m", "a submodule")

        with pytest.raises(SnapshotError, match=r"public-paths.txt:2: 'vendor/' is an empty folder"):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo)


# ---------------------------------------------------------------------------
# Windows-shaped input: separators and line endings
# ---------------------------------------------------------------------------


class TestWindowsShapes:
    def test_a_backslash_entry_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, "docs\\guide/\n")

        with pytest.raises(SnapshotError, match=r"public-paths.txt:1: 'docs\\\\guide/' is not a canonical"):
            _SNAPSHOT.assemble(tmp_path / "public", repo=repo)

    def test_the_committed_line_endings_are_written_untouched(self, tmp_path: Path) -> None:
        files: dict[str, str | bytes] = {"tools/crlf.txt": b"one\r\ntwo\r\n", "tools/lf.txt": b"one\ntwo\n"}
        repo = _repo(tmp_path, files, "tools/\npublic-paths.txt\n")
        # Git for Windows' default: it must not turn the committed LF into CRLF in the archive.
        _git(repo, "config", "core.autocrlf", "true")
        out = tmp_path / "public"

        _SNAPSHOT.assemble(out, repo=repo)

        assert (out / "tools/crlf.txt").read_bytes() == b"one\r\ntwo\r\n"
        assert (out / "tools/lf.txt").read_bytes() == b"one\ntwo\n"
        assert _SNAPSHOT.check(out, repo=repo) == []

    def test_check_reports_nested_paths_in_posix_spelling(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        _write(out, "tools/deep/nested/extra.py", "x\n")

        assert _SNAPSHOT.check(out, repo=repo) == ["extra: tools/deep/nested/extra.py"]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


class TestCheck:
    def test_a_fresh_snapshot_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)

        assert _SNAPSHOT.check(out, repo=repo) == []

    def test_an_extra_and_a_missing_path_are_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        _write(out, "NOTES.md", "closed\n")
        (out / "tools/a.py").unlink()

        assert _SNAPSHOT.check(out, repo=repo) == ["extra: NOTES.md", "missing: tools/a.py"]

    def test_a_committed_change_of_content_is_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        _write(out, "tools/a.py", "a = 2\n")
        _git(out, "commit", "--quiet", "--amend", "--no-edit", "-a")

        assert _SNAPSHOT.check(out, repo=repo) == [
            f"differs from {sha[:12]}: tools/a.py",
            f"differs on disk from {sha[:12]}: tools/a.py",
        ]

    def test_an_uncommitted_change_of_content_on_disk_is_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        with (out / "tools/a.py").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("closed = 'appended after the assembly'\n")

        assert _SNAPSHOT.check(out, repo=repo) == [f"differs on disk from {sha[:12]}: tools/a.py"]

    def test_a_committed_path_outside_the_expansion_is_named_although_gone_from_disk(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        _write(out, "NOTES.md", "closed\n")
        _git(out, "add", "NOTES.md")
        _git(out, "commit", "--quiet", "--amend", "--no-edit")
        (out / "NOTES.md").unlink()

        assert _SNAPSHOT.check(out, repo=repo) == ["extra (committed): NOTES.md"]

    def test_a_history_of_more_than_one_commit_is_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        # Closed content in an earlier commit, gone from the last one.
        _write(out, "NOTES.md", "closed\n")
        _git(out, "add", "NOTES.md")
        _git(out, "commit", "--quiet", "-m", "closed")
        _git(out, "rm", "--quiet", "NOTES.md")
        _git(out, "commit", "--quiet", "-m", f"gone again\n\nSource-Commit: {sha}")

        assert _SNAPSHOT.check(out, repo=repo) == ["history: HEAD has 3 commits; a snapshot has exactly one"]

    def test_an_expected_path_the_commit_lacks_is_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        _git(out, "rm", "--quiet", "--cached", "tools/a.py")
        _git(out, "commit", "--quiet", "--amend", "--no-edit")

        assert _SNAPSHOT.check(out, repo=repo) == [f"differs from {sha[:12]}: tools/a.py"]

    def test_a_committed_change_of_mode_is_named(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        _git(out, "update-index", "--chmod=+x", "--", "tools/a.py")
        _git(out, "commit", "--quiet", "--amend", "--no-edit")

        assert _SNAPSHOT.check(out, repo=repo) == [f"differs from {sha[:12]}: tools/a.py"]

    def test_a_symlinked_folder_on_disk_is_an_extra_path(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        (out / "tools" / "linked").symlink_to(out / "docs", target_is_directory=True)

        assert _SNAPSHOT.check(out, repo=repo) == ["extra: tools/linked"]

    def test_a_symbolic_link_replaced_by_a_file_holding_its_target_is_named(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        repo = _repo(tmp_path, _FILES, _ALLOW)
        (repo / "tools" / "link").symlink_to("a.py")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "-m", "a link")
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        assert _SNAPSHOT.check(out, repo=repo) == []
        # The same bytes, so the same blob id: only the kind on disk tells them apart.
        (out / "tools" / "link").unlink()
        _write(out, "tools/link", b"a.py")

        assert _SNAPSHOT.check(out, repo=repo) == [f"differs on disk from {sha[:12]}: tools/link"]

    def test_a_regular_file_replaced_by_a_symbolic_link_is_named(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        repo = _repo(tmp_path, {**_FILES, "tools/target.txt": "a.py"}, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        (out / "tools" / "target.txt").unlink()
        (out / "tools" / "target.txt").symlink_to("a.py")

        assert _SNAPSHOT.check(out, repo=repo) == [f"differs on disk from {sha[:12]}: tools/target.txt"]

    def test_a_sha256_source_gives_a_sha256_snapshot_that_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW, object_format="sha256")
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)

        assert len(sha) == 64
        assert _git(out, "rev-parse", "--show-object-format").strip() == "sha256"
        assert _SNAPSHOT.check(out, repo=repo) == []

    def test_a_snapshot_in_another_object_format_is_one_finding(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW, object_format="sha256")
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        # The same files re-committed in a SHA-1 repository: every committed blob id differs in kind.
        git_dir = out / ".git"
        for path in sorted(git_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                # Git writes its object files read-only, and Windows refuses to unlink a read-only file.
                if not path.is_symlink():
                    path.chmod(stat.S_IREAD | stat.S_IWRITE)
                path.unlink()
            else:
                path.rmdir()
        git_dir.rmdir()
        _git(out, "init", "--quiet", "--initial-branch=main", "--object-format=sha1")
        quiet_maintenance(out)
        _git(out, "add", "--all", "--force")
        _git(out, "commit", "--quiet", "-m", f"snapshot\n\nSource-Commit: {sha}")

        assert _SNAPSHOT.check(out, repo=repo) == [
            "object format: sha1, the source's is sha256; the committed tree is not compared"
        ]

    def test_the_source_is_read_from_the_trailer_or_named_with_rev(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        _write(repo, "tools/later.py", "x\n")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "-m", "later")

        # The trailer names the assembled commit, so a later source commit does not matter ...
        assert _SNAPSHOT.check(out, repo=repo) == []
        # ... and naming the later commit shows what the snapshot lacks.
        later = _git(repo, "rev-parse", "HEAD").strip()
        assert _SNAPSHOT.check(out, repo=repo, rev="HEAD") == [
            "missing: tools/later.py",
            f"differs from {later[:12]}: tools/later.py",
        ]
        assert _SNAPSHOT.check(out, repo=repo, rev=sha) == []

    def test_a_tree_without_the_trailer_needs_rev(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)

        with pytest.raises(SnapshotError, match="carries no Source-Commit trailer"):
            _SNAPSHOT.check(repo, repo=repo)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        assert _SNAPSHOT.check(repo, repo=repo, rev="HEAD") == [
            "extra: NOTES.md",
            "extra: docs/plans/plan.md",
            "extra (committed): NOTES.md",
            "extra (committed): docs/plans/plan.md",
            f"missing: {_SNAPSHOT.OPEN_COPY_MARKER}",
            f"differs from {sha[:12]}: {_SNAPSHOT.OPEN_COPY_MARKER}",
        ]

    def test_a_folder_that_is_not_a_repository_is_refused(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)

        with pytest.raises(SnapshotError, match=r"no \.git"):
            _SNAPSHOT.check(tmp_path, repo=repo)


class TestDenyList:
    @pytest.fixture
    def snapshot(self, tmp_path: Path) -> tuple[Path, Path]:
        files = dict(_FILES)
        files["tools/hidden.py"] = "x = 1\n# Made at ACME Corp.\nname = 'acme_widget'\n"
        files["tools/acme-notes.md"] = "nothing here\n"
        files["tools/fine.py"] = "acmeish = 'not a whole word'\nsuperacme = 1\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        return repo, out

    def test_a_match_names_the_file_the_line_and_the_list_line_never_the_word(
        self, tmp_path: Path, snapshot: tuple[Path, Path]
    ) -> None:
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_text("# closed words\n\nAcme\n", encoding="utf-8", newline="\n")

        findings = _SNAPSHOT.check(out, repo=repo, deny_list=deny)

        hidden = hashlib.sha256(b"acme-notes.md").hexdigest()[:12]
        assert findings == [
            f"tools/<sha256:{hidden}>: the path name matches deny-list line 3",
            "tools/hidden.py:2: matches deny-list line 3",
            "tools/hidden.py:3: matches deny-list line 3",
        ]
        assert not any("acme" in finding.casefold() for finding in findings)

    def test_a_matching_folder_name_is_hidden_in_every_finding_of_its_files(self, tmp_path: Path) -> None:
        files = dict(_FILES)
        files["tools/acme/inner.py"] = "# acme\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\n", encoding="utf-8", newline="\n")

        hidden = hashlib.sha256(b"acme").hexdigest()[:12]
        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == [
            f"tools/<sha256:{hidden}>/inner.py: the path name matches deny-list line 1",
            f"tools/<sha256:{hidden}>/inner.py:1: matches deny-list line 1",
        ]

    def test_undecodable_bytes_do_not_hide_a_word_beside_them(self, tmp_path: Path) -> None:
        files = dict(_FILES)
        files["tools/blob.bin"] = b"\xff\xfe\x80 ACME \x81\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/blob.bin:1: matches deny-list line 1"]

    def test_extra_missing_and_differing_paths_are_masked_too(self, tmp_path: Path) -> None:
        files = dict(_FILES)
        files["tools/acme-a.py"] = "x = 1\n"
        files["tools/acme-b.py"] = "y = 1\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        sha = _SNAPSHOT.assemble(out, repo=repo)
        (out / "tools/acme-a.py").unlink()
        _write(out, "tools/acme-b.py", "y = 2\n")
        _write(out, "acme-extra.md", "x\n")
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\n", encoding="utf-8", newline="\n")

        findings = _SNAPSHOT.check(out, repo=repo, deny_list=deny)

        def hidden(name: str) -> str:
            return f"<sha256:{hashlib.sha256(name.encode()).hexdigest()[:12]}>"

        assert findings == [
            f"extra: {hidden('acme-extra.md')}",
            f"missing: tools/{hidden('acme-a.py')}",
            f"differs on disk from {sha[:12]}: tools/{hidden('acme-b.py')}",
            f"tools/{hidden('acme-b.py')}: the path name matches deny-list line 1",
        ]
        assert not any("acme" in finding.casefold() for finding in findings)

    def test_a_symbolic_link_is_scanned_by_its_target(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        repo = _repo(tmp_path, _FILES, _ALLOW)
        (repo / "tools" / "link").symlink_to("acme.md")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "-m", "a link")
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/link:1: matches deny-list line 1"]

    def test_a_deny_list_with_a_byte_order_mark_matches_its_first_entry(
        self, tmp_path: Path, snapshot: tuple[Path, Path]
    ) -> None:
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_bytes("acme corp\n".encode("utf-8-sig"))

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/hidden.py:2: matches deny-list line 1"]

    def test_an_empty_list_checks_nothing_beyond_the_expansion(
        self, tmp_path: Path, snapshot: tuple[Path, Path]
    ) -> None:
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_text("# nothing yet\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == []

    def test_a_missing_list_is_refused_rather_than_skipped(self, tmp_path: Path, snapshot: tuple[Path, Path]) -> None:
        repo, out = snapshot

        with pytest.raises(SnapshotError, match="deny-list .* does not exist"):
            _SNAPSHOT.check(out, repo=repo, deny_list=tmp_path / "absent.txt")

    def test_a_host_entry_refuses_its_host_and_passes_a_sibling_of_its_domain(self, tmp_path: Path) -> None:
        """A host entry refuses its host and passes a public sibling of its domain.

        A private host is an entry of its own; a bare-domain entry would refuse both, because ``.``
        separates words.
        """
        files = dict(_FILES)
        files["tools/site.md"] = "Read [the project site](https://www.example.org).\n"
        files["tools/ci.md"] = "Runs on https://ci.example.org/swcraft and CI.EXAMPLE.ORG.\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        hosts = tmp_path / "hosts.txt"
        hosts.write_text("ci.example.org\n", encoding="utf-8", newline="\n")
        bare = tmp_path / "bare.txt"
        bare.write_text("example.org\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=hosts) == ["tools/ci.md:1: matches deny-list line 1"]
        assert _SNAPSHOT.check(out, repo=repo, deny_list=bare) == [
            "tools/ci.md:1: matches deny-list line 1",
            "tools/site.md:1: matches deny-list line 1",
        ]

    def test_a_pattern_entry_matches_its_alternatives_whole_word_and_in_any_case(self, tmp_path: Path) -> None:
        """A ``re:`` entry refuses each alternative as a whole word, in any case and across a line break.

        It is a regular expression between the literal entries' word bounds: each alternative is refused
        in any case and across a line break where the pattern allows whitespace, and a word that only
        contains the phrase (``thirdfirst``) or a neighbour it does not name passes.
        """
        files = dict(_FILES)
        files["tools/a.md"] = "The First Widget Export moved.\n"
        files["tools/b.md"] = "x\nthe second widget\nexport too\n"
        files["tools/c.md"] = "a thirdfirst widget export, and the third widget export\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("re:(first|second)\\s+widget\\s+exports?\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == [
            "tools/a.md:1: matches deny-list line 1",
            "tools/b.md:2: matches deny-list line 1",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "x = 1\n# the first widget\n# export moved\n",
            "x = 1\n    #: the first\n    #: widget export\n",
            "x = 1\n// the first widget\n// export moved\n",
            "x = 1\n * the first widget\n * export moved\n",
            "x = 1\n> the first widget\n> > export moved\n",
            "x = 1\nthe first widget   \n\texport\n",
        ],
    )
    def test_a_phrase_wrapped_behind_a_comment_prefix_is_found_on_the_line_it_starts(
        self, tmp_path: Path, text: str
    ) -> None:
        """A phrase a comment wraps is still refused.

        A wrap with ``#``, ``#:``, ``//``, ``*`` or ``>`` between two words of a pattern or a multi-word
        entry is read as one blank.
        """
        files = dict(_FILES)
        files["tools/a.py"] = text
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        pattern = tmp_path / "pattern.txt"
        pattern.write_text("re:(first|second)\\s+widget\\s+exports?\n", encoding="utf-8", newline="\n")
        literal = tmp_path / "literal.txt"
        literal.write_text("first widget export\n", encoding="utf-8", newline="\n")

        for deny in (pattern, literal):
            assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/a.py:2: matches deny-list line 1"]

    def test_a_wrap_joins_no_words_a_prefix_does_not_separate(self, tmp_path: Path) -> None:
        """A wrap joins no words a prefix does not separate.

        Only a line break is read as a blank: words on one line keep their text, and a ``#`` inside a line
        is no prefix, so ``first # widget export`` stays apart from the pattern's phrase.
        """
        files = dict(_FILES)
        files["tools/a.py"] = "x = 'first' # widget export\nthe first\nwidgetexport\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("re:(first|second)\\s+widget\\s+exports?\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == []

    @pytest.mark.parametrize(
        ("text", "joined", "starts"),
        [
            ("a\nb", "a b", [2]),
            ("a  \n  # b\n//c", "a b c", [2, 4]),
            ("a\n\nb", "a  b", [2, 3]),
            ("a", "a", []),
        ],
    )
    def test_unwrapped_joins_each_break_once_and_records_where_each_line_starts(
        self, text: str, joined: str, starts: list[int]
    ) -> None:
        assert _SNAPSHOT.unwrapped(text) == (joined, starts)

    def test_text_findings_reads_a_text_as_check_reads_a_file(self, tmp_path: Path) -> None:
        """Case-folded and unwrapped, each (deny-list line, text line) pair once, the matched text never returned."""
        deny = tmp_path / "deny.txt"
        deny.write_text("acme corp\nwidget\n", encoding="utf-8", newline="\n")
        patterns = _SNAPSHOT.read_deny_list(deny)
        text = "one\nan ACME\n# Corp and a widget, a widget\nWidget\n"

        assert _SNAPSHOT.text_findings(text, patterns) == [(1, 2), (2, 3), (2, 4)]
        assert _SNAPSHOT.text_findings("nothing here\n", patterns) == []

    def test_a_pattern_entry_that_does_not_compile_is_refused_by_its_line(
        self, tmp_path: Path, snapshot: tuple[Path, Path]
    ) -> None:
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\nre:(first|second\n", encoding="utf-8", newline="\n")

        with pytest.raises(SnapshotError, match="line 2: the pattern does not compile"):
            _SNAPSHOT.check(out, repo=repo, deny_list=deny)

    @pytest.mark.parametrize("line", ["re:issue\\s*#\\d+", "re:a[#]b", "re:widget  # a comment"])
    def test_a_pattern_entry_holding_a_hash_is_refused_by_its_line(
        self, tmp_path: Path, snapshot: tuple[Path, Path], line: str
    ) -> None:
        """The comment strip would cut the pattern at ``#`` and leave one that still compiles."""
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_text(f"acme  # a literal's comment is fine\n{line}\n", encoding="utf-8", newline="\n")

        with pytest.raises(SnapshotError, match="line 2: a pattern entry holds '#'"):
            _SNAPSHOT.check(out, repo=repo, deny_list=deny)

    def test_a_pattern_spells_a_hash_as_its_escape(self, tmp_path: Path) -> None:
        files = dict(_FILES)
        files["tools/a.md"] = "see issue #12 here\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("re:issue\\s*\\x23\\d+\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/a.md:1: matches deny-list line 1"]

    def test_a_multi_word_entry_matches_case_insensitively(self, tmp_path: Path, snapshot: tuple[Path, Path]) -> None:
        repo, out = snapshot
        deny = tmp_path / "deny.txt"
        deny.write_text("acme corp\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.check(out, repo=repo, deny_list=deny) == ["tools/hidden.py:2: matches deny-list line 1"]


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


class TestCommandLine:
    def test_assemble_then_check_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"

        assert _SNAPSHOT.main(["assemble", "--repo", str(repo), "--rev", "HEAD", "--out", str(out)]) == 0
        assert _SNAPSHOT.main(["check", str(out), "--repo", str(repo)]) == 0
        assert "exactly the allow-list's expansion" in capsys.readouterr().out

    def test_a_finding_or_a_refusal_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo(tmp_path, _FILES, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        _write(out, "NOTES.md", "closed\n")

        assert _SNAPSHOT.main(["check", str(out), "--repo", str(repo)]) == 1
        assert "extra: NOTES.md" in capsys.readouterr().out
        assert _SNAPSHOT.main(["assemble", "--repo", str(repo), "--out", str(out)]) == 1
        assert "not empty" in capsys.readouterr().err

    def test_check_takes_the_deny_list_from_the_command_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        files = dict(_FILES)
        files["tools/hidden.py"] = "# acme\n"
        repo = _repo(tmp_path, files, _ALLOW)
        out = tmp_path / "public"
        _SNAPSHOT.assemble(out, repo=repo)
        deny = tmp_path / "deny.txt"
        deny.write_text("acme\n", encoding="utf-8", newline="\n")

        assert _SNAPSHOT.main(["check", str(out), "--repo", str(repo), "--deny-list", str(deny)]) == 1
        captured = capsys.readouterr()
        assert captured.out == "tools/hidden.py:1: matches deny-list line 1\n"
        assert "acme" not in (captured.out + captured.err).casefold()

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-command"),
            pytest.param(["assemble"], id="assemble-without-out"),
            pytest.param(["check"], id="check-without-folder"),
            pytest.param(["publish", "x"], id="unknown-command"),
        ],
    )
    def test_a_usage_error_exits_two(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as raised:
            _SNAPSHOT.main(argv)
        assert raised.value.code == 2
        assert "usage:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# This repository's own allow-list
# ---------------------------------------------------------------------------


class TestThisRepository:
    def test_the_committed_allow_list_is_valid_at_head(self) -> None:
        paths = _SNAPSHOT.expansion(REPO_ROOT, "HEAD")

        assert "public-paths.txt" in paths
        assert "scripts/public_snapshot.py" in paths
        closed = (
            "docs/plans/",
            ".gitea/",
            ".claude/",
            "internal/",
            "docs/checklists/agent-task-results/",
            "docs/concepts/",
            "docs/adr/",
            "docs/contracts/",
            "docs/checklists/",
            "docs/scenarios/",
        )
        assert not any(path.startswith(closed) for path in paths)
        assert "CLAUDE.md" not in paths
