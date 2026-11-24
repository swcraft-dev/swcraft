"""Tests for scripts/build_artifacts.py's user guide: the site, its release notes, its archive, the wheel's payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from build_artifacts_support import ARTIFACTS, BUILT_AT, VERSION, FakeRunner, make_source_repo
from conftest import REPO_ROOT, make_guide_site
from swcraft_tools import guide_site
from tracked_files_support import listed_files

# ---------------------------------------------------------------------------
# The user guide's site build
# ---------------------------------------------------------------------------
#
# ``build_docs_site`` is the one place mkdocs is invoked (``dev.py docs`` plans one step against it), so these cases
# pin the argv that step runs -- the module form, ``--strict`` and the output directory -- without mkdocs installed.
#
# Not reached: whether the rendered site is correct, whether any page links anywhere, and whether mkdocs is
# installed. Orphan pages are not among ``--strict``'s failures (MkDocs logs them at INFO), so the guide's
# link-and-nav gate owns that direction.


def test_the_version_page_is_written_from_the_one_version_derivation(tmp_path: Path) -> None:
    """``read_version`` — the same ``ast`` read the wheel filenames come from."""
    repo_root = make_source_repo(tmp_path, version="4.5.6")

    page = ARTIFACTS.write_guide_version_page(repo_root)

    assert page == repo_root / ARTIFACTS.GUIDE_VERSION_PAGE
    assert "4.5.6" in page.read_text(encoding="utf-8")


def test_the_version_page_is_deterministic(tmp_path: Path) -> None:
    """No timestamp and no host path, so two builds of one commit render alike."""
    first = ARTIFACTS.render_guide_version_page(VERSION)
    assert first == ARTIFACTS.render_guide_version_page(VERSION)
    assert str(tmp_path) not in first
    assert ARTIFACTS.render_guide_version_page("0.0.1") != first


def test_the_version_page_is_the_only_guide_page_that_is_not_committed() -> None:
    """The version page is gitignored and untracked, and every other guide page is committed.

    That is why the guide is not a version-lockstep site: the one page naming the version is generated. Both
    directions are asserted, since either alone would pass a tree with the page committed. ``git check-ignore``
    answers for a path whether or not the file exists, so nothing is written into the tree. Not reached: a page some
    other build step starts generating later.
    """
    relative = ARTIFACTS.GUIDE_VERSION_PAGE.as_posix()
    ignored = subprocess.run(
        ["git", "check-ignore", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    assert ignored.returncode == 0, f"{relative} is not gitignored"

    # The listing carries the untracked-but-not-ignored files too, so the guide's own
    # pages are listed here before their first commit as well as after.
    listed = listed_files("docs/guide", suffixes=(".md",))
    assert relative not in listed, f"{relative} is committed; the build regenerates it every run"
    assert "docs/guide/index.md" in listed, "the guide's own pages are missing from the repository"


def test_build_docs_site_runs_mkdocs_strict_into_dist_docs_site(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    calls: list[list[str]] = []

    site_dir = ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: calls.append([str(part) for part in argv]),
        python_executable="/fake/python",
    )

    assert site_dir == repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.DOCS_DIRNAME / ARTIFACTS.DOCS_SITE_DIRNAME
    assert len(calls) == 2, "mkdocs, then the guide's corpus"
    argv = calls[0]
    assert argv[:5] == ["/fake/python", "-m", "mkdocs", "build", "--strict"]
    assert argv[argv.index("--config-file") + 1] == str(repo_root / ARTIFACTS.MKDOCS_CONFIG_FILENAME)
    assert argv[argv.index("--site-dir") + 1] == str(site_dir)


def test_build_docs_site_writes_the_guide_corpus_into_the_site_after_mkdocs(tmp_path: Path) -> None:
    """The producer's module form, with mkdocs' interpreter and configuration, second.

    Second because ``mkdocs build`` cleans its site directory, so a corpus written
    before it would not survive; with the same ``python_executable``, because the
    producer imports ``markdown`` and ``mkdocs``, which that interpreter holds. What
    this does **not** reach: what the producer writes -- the runner is a fake, and
    the corpus's own rules are ``test_guide_corpus.py``'s.
    """
    repo_root = make_source_repo(tmp_path)
    calls: list[list[str]] = []

    site_dir = ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: calls.append([str(part) for part in argv]),
        python_executable="/fake/python",
    )

    assert [argv[1:3] for argv in calls] == [["-m", "mkdocs"], ["-m", "swcraft_tools.guide_corpus"]]
    argv = calls[1]
    assert argv[0] == "/fake/python"
    assert argv[argv.index("--guide-dir") + 1] == str(repo_root / ARTIFACTS.GUIDE_DIR)
    assert argv[argv.index("--mkdocs-config") + 1] == calls[0][calls[0].index("--config-file") + 1]
    assert argv[argv.index("--out") + 1] == str(site_dir / "guide-corpus.json")


def test_the_build_names_the_corpus_and_its_producer_as_the_producer_does() -> None:
    """The build script imports nothing from the packages it builds, so the name is spelled twice."""
    from swcraft_tools import guide_corpus

    assert ARTIFACTS.GUIDE_CORPUS_FILENAME == guide_corpus.CORPUS_FILENAME
    assert ARTIFACTS.GUIDE_CORPUS_MODULE == guide_corpus.__name__


def test_a_failing_corpus_producer_fails_the_site_build(tmp_path: Path) -> None:
    """A guide the producer refuses is a failed build, exactly as a failing mkdocs is.

    What the site holds afterwards is not asserted here: the runner is a fake, which
    writes nothing when it fails, so a check of the site would pass whatever the build
    did. That a refused producer writes no corpus is ``test_guide_corpus.py``'s
    (``produce`` writes nothing unless every check passed).
    """
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, fail_on=lambda argv: argv[1:3] == ["-m", ARTIFACTS.GUIDE_CORPUS_MODULE])

    with pytest.raises(subprocess.CalledProcessError):
        ARTIFACTS.build_docs_site(repo_root, run=runner, python_executable="/fake/python")

    assert [argv[1:3] for argv in runner.calls] == [["-m", "mkdocs"], ["-m", ARTIFACTS.GUIDE_CORPUS_MODULE]]


def test_a_failing_artifact_build_stops_at_a_refused_corpus(tmp_path: Path) -> None:
    """``build_all`` builds no wheel from a site whose corpus was refused."""
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, fail_on=lambda argv: argv[1:3] == ["-m", ARTIFACTS.GUIDE_CORPUS_MODULE])

    with pytest.raises(subprocess.CalledProcessError):
        ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    assert ["-m", "build"] not in [argv[1:3] for argv in runner.calls]


def test_build_docs_site_writes_the_version_page_before_running_mkdocs(tmp_path: Path) -> None:
    """Ordering matters: a nav or a link naming that page must find it on disk."""
    repo_root = make_source_repo(tmp_path)
    seen_when_mkdocs_ran: list[bool] = []

    ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: seen_when_mkdocs_ran.append((repo_root / ARTIFACTS.GUIDE_VERSION_PAGE).is_file()),
        python_executable="/fake/python",
    )

    assert seen_when_mkdocs_ran == [True, True], "mkdocs and the corpus producer both read it"


# ---------------------------------------------------------------------------
# The guide's Release notes section
# ---------------------------------------------------------------------------
#
# ``build_docs_site`` copies the release notes the guide carries into a generated,
# gitignored ``docs/guide/release-notes/`` before mkdocs runs, so ``dev.py docs``,
# the guide archive and the wheel's payload all carry them and ``upgrading.md``
# links them relatively. What these cases do **not** reach: whether mkdocs renders
# the copied notes (the ``artifact``-marked case below and ``dev.py docs`` do), and
# whether a note reads well to an architect -- the vocabulary gate reads a list of
# terms, never a register.


def _notes_repo(tmp_path: Path, versions: Sequence[str], *, page: str = "# Guide\n") -> Path:
    """A synthetic tree whose release notes are *versions* and whose one guide page is *page*."""
    repo_root = make_source_repo(tmp_path)
    notes = repo_root / ARTIFACTS.RELEASE_NOTES_DIR
    for existing in notes.iterdir():
        existing.unlink()
    for version in versions:
        # Bytes a text copy would change: a CRLF and a non-ASCII character.
        (notes / f"{version}.md").write_bytes(f"# SWCraft {version}\r\n\r\n× one\r\n".encode())
    (repo_root / ARTIFACTS.GUIDE_DIR / "index.md").write_text(page, encoding="utf-8", newline="\n")
    return repo_root


def test_the_section_carries_every_note_from_the_floor_on_and_every_older_note_a_page_links(tmp_path: Path) -> None:
    """0.9.0 on, plus 0.6.0 because a page links it; 0.5.0 and 0.8.0 are not carried."""
    page = "# Guide\n\nSee [what changed](release-notes/0.6.0.md) and [the list](release-notes/index.md).\n"
    repo_root = _notes_repo(tmp_path, ["0.5.0", "0.6.0", "0.8.0", "0.9.0", "0.10.0", "1.2.0"], page=page)

    carried = [note.name for note in ARTIFACTS.guide_release_notes(repo_root)]

    assert carried == ["1.2.0.md", "0.10.0.md", "0.9.0.md", "0.6.0.md"], "newest first, compared as versions"


def test_a_link_to_a_note_that_does_not_exist_selects_nothing(tmp_path: Path) -> None:
    repo_root = _notes_repo(tmp_path, ["0.9.0"], page="[gone](release-notes/0.4.0.md)\n")
    assert [note.name for note in ARTIFACTS.guide_release_notes(repo_root)] == ["0.9.0.md"]


def test_build_docs_site_writes_the_release_notes_section_before_running_mkdocs(tmp_path: Path) -> None:
    """The copy exists when mkdocs runs, byte for byte, with an index listing it newest first."""
    page = "# Guide\n\n[the hardware release](release-notes/0.6.0.md)\n"
    repo_root = _notes_repo(tmp_path, ["0.6.0", "0.8.0", "0.9.0", "0.10.0"], page=page)
    section = repo_root / ARTIFACTS.GUIDE_RELEASE_NOTES_DIR
    seen: list[list[str]] = []

    ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: seen.append(sorted(path.name for path in section.iterdir())),
        python_executable="/fake/python",
    )

    # Twice: mkdocs and the corpus producer each read the section.
    assert seen == [["0.10.0.md", "0.6.0.md", "0.9.0.md", ARTIFACTS.GUIDE_RELEASE_NOTES_INDEX]] * 2
    for name in ("0.6.0.md", "0.9.0.md", "0.10.0.md"):
        assert (section / name).read_bytes() == (repo_root / ARTIFACTS.RELEASE_NOTES_DIR / name).read_bytes()
    index = (section / ARTIFACTS.GUIDE_RELEASE_NOTES_INDEX).read_bytes().decode("utf-8")
    assert "\r" not in index
    assert index.index("(0.10.0.md)") < index.index("(0.9.0.md)") < index.index("(0.6.0.md)")
    assert "0.8.0" not in index


def test_a_note_that_left_the_selection_does_not_survive_the_next_build(tmp_path: Path) -> None:
    repo_root = _notes_repo(tmp_path, ["0.9.0"])
    section = repo_root / ARTIFACTS.GUIDE_RELEASE_NOTES_DIR
    section.mkdir()
    (section / "0.6.0.md").write_text("# an earlier build's copy\n", encoding="utf-8")

    ARTIFACTS.write_guide_release_notes(repo_root)

    assert sorted(path.name for path in section.iterdir()) == ["0.9.0.md", ARTIFACTS.GUIDE_RELEASE_NOTES_INDEX]


def test_a_section_with_no_note_is_refused_rather_than_rendered_empty(tmp_path: Path) -> None:
    repo_root = _notes_repo(tmp_path, ["0.5.0"])
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="no release note"):
        ARTIFACTS.write_guide_release_notes(repo_root)
    assert not (repo_root / ARTIFACTS.GUIDE_RELEASE_NOTES_DIR).exists(), "refused before anything was written"


def test_a_tree_without_release_notes_is_refused(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    shutil.rmtree(repo_root / ARTIFACTS.RELEASE_NOTES_DIR)
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="release notes"):
        ARTIFACTS.build_docs_site(repo_root, run=lambda argv, cwd: None, python_executable="/fake/python")


def test_the_index_is_deterministic_and_names_no_host_path(tmp_path: Path) -> None:
    first = ARTIFACTS.render_guide_release_notes_index(["0.13.0", "0.12.0"])
    assert first == ARTIFACTS.render_guide_release_notes_index(["0.13.0", "0.12.0"])
    assert str(tmp_path) not in first
    assert first.startswith("<!-- Generated by scripts/build_artifacts.py.")


def test_the_real_section_carries_every_note_from_the_floor_on_and_the_older_ones_the_guide_links() -> None:
    """Against this checkout: the selection ``dev.py docs`` makes today."""
    carried = {note.stem for note in ARTIFACTS.guide_release_notes(REPO_ROOT)}
    on_disk = {
        note.stem: version
        for note in (REPO_ROOT / ARTIFACTS.RELEASE_NOTES_DIR).glob("*.md")
        if (version := ARTIFACTS.release_note_version(note)) is not None
    }
    assert {stem for stem, version in on_disk.items() if version >= ARTIFACTS.GUIDE_RELEASE_NOTES_FLOOR} <= carried
    assert "0.6.0" in carried, "upgrading.md links the hardware release's note"
    assert "0.8.0" not in carried


def test_the_release_notes_section_is_gitignored_and_uncommitted() -> None:
    """``git check-ignore`` answers for a path whether or not the file exists."""
    relative = f"{ARTIFACTS.GUIDE_RELEASE_NOTES_DIR.as_posix()}/{ARTIFACTS.GUIDE_RELEASE_NOTES_INDEX}"
    ignored = subprocess.run(
        ["git", "check-ignore", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    assert ignored.returncode == 0, f"{relative} is not gitignored"
    listed = subprocess.run(
        ["git", "ls-files", "--", ARTIFACTS.GUIDE_RELEASE_NOTES_DIR.as_posix()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout.split()
    assert listed == [], f"the generated section is committed: {listed}"


def test_the_section_is_not_an_artifact_source_and_the_notes_are(tmp_path: Path) -> None:
    """A build's own copy would age ``dist/`` stale; an edited note must."""
    repo_root = make_source_repo(tmp_path)
    ARTIFACTS.write_guide_release_notes(repo_root)

    sources = {path.relative_to(repo_root).as_posix() for path in ARTIFACTS.collect_artifact_sources(repo_root)}

    assert f"{ARTIFACTS.RELEASE_NOTES_DIR.as_posix()}/{VERSION}.md" in sources
    assert not any(name.startswith(f"{ARTIFACTS.GUIDE_RELEASE_NOTES_DIR.as_posix()}/") for name in sources)


def test_build_docs_site_reports_a_missing_configuration_rather_than_running_mkdocs(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path, guide=False)
    calls: list[list[str]] = []

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_docs_site(repo_root, run=lambda argv, cwd: calls.append(list(argv)))

    assert ARTIFACTS.MKDOCS_CONFIG_FILENAME in str(excinfo.value)
    assert calls == []


def test_write_guide_version_page_reports_a_missing_guide_tree(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path, guide=False)
    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.write_guide_version_page(repo_root)
    assert ARTIFACTS.GUIDE_DIR.as_posix() in str(excinfo.value).replace("\\", "/")


def test_docs_only_builds_the_site_and_nothing_else(tmp_path, monkeypatch, capsys) -> None:
    repo_root = make_source_repo(tmp_path)
    built: list[Path] = []
    monkeypatch.setattr(ARTIFACTS, "build_docs_site", lambda root: built.append(root) or (root / "site"))
    monkeypatch.setattr(
        ARTIFACTS,
        "build_all",
        lambda *_a, **_k: pytest.fail("--docs-only must not build wheels or the .vsix"),
    )

    assert ARTIFACTS.main(["--repo-root", str(repo_root), "--docs-only"]) == 0
    assert built == [repo_root]
    assert "site" in capsys.readouterr().out


def test_docs_only_and_wheelhouse_are_refused_together(tmp_path, capsys) -> None:
    """Each replaces the artifact build; taking one silently would hide the other."""
    repo_root = make_source_repo(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        ARTIFACTS.main(["--repo-root", str(repo_root), "--docs-only", "--wheelhouse"])
    assert excinfo.value.code == 2
    assert "--docs-only" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The guide's release artifact: dist/docs/swcraft-guide-<version>.zip
# ---------------------------------------------------------------------------
#
# The *archive* is the manifest entry, never the rendered tree: `manifest.json` and
# `SHA256SUMS` keep one hash per file, which is what makes `sha256sum -c` from
# `dist/` a complete check of a release rather than a check of four files out of a
# few hundred.
#
# What these cases do **not** reach: whether the rendered site is correct, and
# whether an unpacked copy behaves like a served one. The second is a real,
# measured limitation of the delivery format rather than a gap in the tests --
# the site is built to be served (`archive_docs_site`'s docstring records what
# `file://` breaks, and how it was measured).


def _archive_names(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as opened:
        return opened.namelist()


def test_build_all_archives_the_rendered_guide_and_keeps_the_tree(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)

    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    archive = dist_root / ARTIFACTS.DOCS_DIRNAME / f"swcraft-guide-{VERSION}.zip"
    assert archive.is_file()
    # The tree stays beside it: it is what `dev.py docs` produces to be opened.
    assert (ARTIFACTS.docs_site_dir(dist_root) / "index.html").is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (entry,) = [item for item in manifest["artifacts"] if item["kind"] == "docs"]
    assert entry["name"] == "swcraft-guide"
    assert entry["filename"] == archive.name
    assert entry["sha256"] == ARTIFACTS.sha256_file(archive)
    assert ARTIFACTS.artifact_relative_path(entry) == f"docs/{archive.name}"


def test_the_archive_is_rooted_at_its_own_stem_and_holds_every_rendered_file(tmp_path: Path) -> None:
    """Unpacking beside other files leaves one directory and one note, not a spilled site."""
    repo_root = make_source_repo(tmp_path)
    ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    archive = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.DOCS_DIRNAME / f"swcraft-guide-{VERSION}.zip"
    names = _archive_names(archive)

    root = f"swcraft-guide-{VERSION}"
    assert names == [
        ARTIFACTS.DOCS_ARCHIVE_README,
        f"{root}/assets/app.css",
        f"{root}/{ARTIFACTS.GUIDE_CORPUS_FILENAME}",
        f"{root}/index.html",
    ]
    assert names == sorted(names), "member order is pinned, so the directory walk cannot leak in"


def test_the_note_beside_the_tree_names_the_directory_and_says_to_serve_it(tmp_path: Path) -> None:
    """The one thing an unpacked guide cannot say for itself.

    ``file://`` fails in two independent ways -- every internal link is a directory
    URL, and the search index loads over neither ``fetch`` nor a ``Worker`` -- and
    both failures are quiet, which is why the note is a member rather than a
    sentence on a page nobody reaches. It is checked for *content* and not merely
    for presence: a note that did not name the directory it is about would leave
    the reader with a command they cannot paste.
    """
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    destination = tmp_path / "swcraft-guide-9.9.9.zip"

    ARTIFACTS.archive_docs_site(site, destination, "1000000000")

    with zipfile.ZipFile(destination) as opened:
        note = opened.read(ARTIFACTS.DOCS_ARCHIVE_README).decode("utf-8")

    assert note.startswith("SWCraft user guide")
    assert "swcraft-guide-9.9.9/" in note, "the note must name the directory beside it"
    assert "python -m http.server" in note, "the note must give one way to serve the tree"
    assert "swcraft docs" in note, "an installed toolchain needs no zip at all"


def test_the_archive_pins_every_members_timestamp_and_mode(tmp_path: Path) -> None:
    """The three fields a zip otherwise records from the host it was built on."""
    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True)
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    (site / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    destination = tmp_path / "swcraft-guide-9.9.9.zip"

    ARTIFACTS.archive_docs_site(site, destination, "1000000000")

    expected = ARTIFACTS.zip_timestamp("1000000000")
    with zipfile.ZipFile(destination) as opened:
        infos = opened.infolist()
    assert infos, "the archive is empty"
    for info in infos:
        assert info.date_time == expected, info.filename
        assert info.external_attr >> 16 == 0o644, info.filename
        assert not info.is_dir(), "directory entries are not written; unpackers create parents"


def test_two_archives_of_one_tree_are_byte_identical_whatever_the_file_mtimes_are(tmp_path: Path) -> None:
    """The property the release rests on, at the archive level.

    The source files are aged between the two runs, so a member timestamp read
    from the filesystem -- the default a ``zipfile.write`` would take -- could not
    survive this. What it does not reach is whether *mkdocs* renders the same
    bytes twice; that is ``test_reproducibility.py``'s question, against a real build.
    """
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")

    first = ARTIFACTS.archive_docs_site(site, tmp_path / "first" / "swcraft-guide-9.9.9.zip", "1000000000")
    os.utime(site / "index.html", (1, 1))
    second = ARTIFACTS.archive_docs_site(site, tmp_path / "second" / "swcraft-guide-9.9.9.zip", "1000000000")

    assert first.read_bytes() == second.read_bytes()


def test_zip_timestamp_clamps_an_epoch_zip_cannot_express() -> None:
    """DOS timestamps start at 1980; a 1970 epoch must clamp, never raise."""
    assert ARTIFACTS.zip_timestamp("0") == (1980, 1, 1, 0, 0, 0)
    assert ARTIFACTS.zip_timestamp("1000000000") == (2001, 9, 9, 1, 46, 40)


def test_archiving_a_missing_or_empty_site_is_reported_rather_than_shipped(tmp_path: Path) -> None:
    """An empty archive would pass every digest check and deliver nothing."""
    destination = tmp_path / "swcraft-guide-9.9.9.zip"
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="missing"):
        ARTIFACTS.archive_docs_site(tmp_path / "absent", destination, "1000000000")

    empty = tmp_path / "site"
    empty.mkdir()
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="empty"):
        ARTIFACTS.archive_docs_site(empty, destination, "1000000000")


def test_the_site_build_runs_with_source_date_epoch_exported(tmp_path: Path) -> None:
    """``SOURCE_DATE_EPOCH`` is exported around the mkdocs call, and the environment restored after it.

    ``mkdocs.utils.get_build_datetime`` reads it; unset, the gzip header of ``sitemap.xml.gz`` carries today's date --
    the one byte that moves between two builds of one commit. Restoration is checked as equality with the previous
    value, not absence: a host that exports the variable would fail an absence check for doing the right thing, and
    an absence check passes a leak of the empty string.
    """
    repo_root = make_source_repo(tmp_path)
    before = os.environ.get("SOURCE_DATE_EPOCH")
    observed: list[str | None] = []

    ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: observed.append(os.environ.get("SOURCE_DATE_EPOCH")),
        python_executable="/fake/python",
    )

    assert observed and all(stamp and stamp.isdigit() for stamp in observed), observed
    assert os.environ.get("SOURCE_DATE_EPOCH") == before, "the build must leave the environment as it found it"


def test_the_site_build_restores_a_source_date_epoch_the_host_already_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the absence assertion above used to mask: the variable was already set.

    The build must still export *its own* stamp around the mkdocs call -- the
    host's value is not what the commit says -- and must put the host's back
    afterwards rather than deleting it.
    """
    repo_root = make_source_repo(tmp_path)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
    observed: list[str | None] = []

    ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda argv, cwd: observed.append(os.environ.get("SOURCE_DATE_EPOCH")),
        python_executable="/fake/python",
    )

    assert observed == [ARTIFACTS.source_date_epoch(repo_root)] * 2, "mkdocs and the corpus producer"
    assert os.environ["SOURCE_DATE_EPOCH"] == "1", "the host's own value was not restored"


def test_the_site_build_renders_where_it_is_told(tmp_path: Path) -> None:
    """``site_dir`` is honoured, so a caller with a dist root of its own is not sent to ``repo_root/dist``."""
    repo_root = make_source_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "site"
    argv: list[list[str]] = []

    returned = ARTIFACTS.build_docs_site(
        repo_root,
        run=lambda command, cwd: argv.append(command),
        python_executable="/fake/python",
        site_dir=elsewhere,
    )

    assert returned == elsewhere
    assert argv and argv[0][argv[0].index("--site-dir") + 1] == str(elsewhere)


def test_the_docs_archive_renders_into_the_dist_root_it_was_given(tmp_path: Path) -> None:
    """The site and the archive are both rooted at ``dist_root``; neither reaches ``repo_root/dist``."""
    repo_root = make_source_repo(tmp_path)
    dist_root = tmp_path / "somewhere-else"
    argv: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path) -> None:
        argv.append(command)
        rendered = ARTIFACTS.docs_site_dir(dist_root)
        rendered.mkdir(parents=True, exist_ok=True)
        (rendered / "index.html").write_text("<!doctype html>", encoding="utf-8")

    archive = ARTIFACTS.build_docs_archive(repo_root, dist_root, run=fake_run, python_executable="/fake/python")

    assert argv[0][argv[0].index("--site-dir") + 1] == str(ARTIFACTS.docs_site_dir(dist_root))
    assert archive.parent == dist_root / ARTIFACTS.DOCS_DIRNAME
    assert not (repo_root / "dist" / "docs").exists(), "the build reached the repository's own dist/"


def test_a_pre_1980_epoch_is_clamped_rather_than_raising() -> None:
    """Zip's DOS timestamp cannot express a year before 1980, and a build is not worth failing over it.

    Reachable rather than theoretical: ``source_date_epoch`` honours an exported
    ``SOURCE_DATE_EPOCH``, and ``SOURCE_DATE_EPOCH=0`` is a real thing to set.
    Without the clamp ``ZipInfo`` raises ``ValueError`` for the whole archive.
    """
    assert ARTIFACTS.zip_timestamp("0") == (1980, 1, 1, 0, 0, 0)
    assert ARTIFACTS.zip_timestamp("1000000000") == (2001, 9, 9, 1, 46, 40)


def test_every_archive_member_pins_its_create_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fourth pinned field: ``zipfile`` sets ``create_system`` from ``sys.platform`` otherwise.

    It is 0 on Windows and 3 elsewhere, so the same tree at the same epoch would archive to different bytes per build
    host. ``sys.platform`` is patched because ``3`` is the default on every host this suite runs on: without the patch
    the case passes with the assignment deleted. Nothing else in ``archive_docs_site`` reads it.
    """
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html>", encoding="utf-8")
    destination = tmp_path / "swcraft-guide-9.9.9.zip"
    monkeypatch.setattr(zipfile.sys, "platform", "win32")

    ARTIFACTS.archive_docs_site(site, destination, "1000000000")

    with zipfile.ZipFile(destination) as archive:
        systems = {info.create_system for info in archive.infolist()}
    assert systems == {3}, systems


def test_clean_output_dirs_empties_dist_docs_of_a_previous_release(tmp_path: Path) -> None:
    """Cleaning empties ``dist/docs`` of a previous release.

    mkdocs cleans its own ``site_dir`` and nothing else, so a previous version's archive sitting next
    to the new one has to be removed here.
    """
    dist_root = tmp_path / "dist"
    stale_archive = dist_root / ARTIFACTS.DOCS_DIRNAME / "swcraft-guide-0.0.9.zip"
    stale_archive.parent.mkdir(parents=True)
    stale_archive.write_text("an archive from an earlier version", encoding="utf-8")
    stale_page = ARTIFACTS.docs_site_dir(dist_root) / "removed-page" / "index.html"
    stale_page.parent.mkdir(parents=True)
    stale_page.write_text("a page the guide no longer has", encoding="utf-8")

    ARTIFACTS.clean_output_dirs(dist_root)

    assert not stale_archive.exists()
    assert not stale_page.exists()
    assert (dist_root / ARTIFACTS.DOCS_DIRNAME).is_dir()


def test_clean_output_dirs_leaves_the_wheelhouse_and_the_workstation_archive_alone(tmp_path: Path) -> None:
    """``clean_output_dirs`` never empties the wheelhouse or the workstation archive directory.

    ``dev.py artifacts`` produces neither, so emptying them would delete the network-populated closure (~29 MB nothing
    rebuilds by itself) and the archive built from it. Both are seeded with a file, because an empty directory that
    survives an ``rmtree``-then-``mkdir`` pair is indistinguishable from one that did not. Not reached: whether each
    directory's own replacement is correct, asserted where ``build_wheelhouse`` and ``build_workstation_archive`` are.
    """
    dist_root = tmp_path / "dist"
    kept = {}
    for name in (ARTIFACTS.WHEELHOUSE_DIRNAME, ARTIFACTS.WORKSTATION_DIRNAME):
        directory = dist_root / name
        directory.mkdir(parents=True)
        payload = directory / "expensive-to-rebuild"
        payload.write_text(name, encoding="utf-8")
        kept[name] = payload
    emptied = dist_root / ARTIFACTS.PYTHON_DIRNAME / "swcraft_tools-0.0.9-py3-none-any.whl"
    emptied.parent.mkdir(parents=True)
    emptied.write_text("a wheel from an earlier version", encoding="utf-8")

    ARTIFACTS.clean_output_dirs(dist_root)

    for name, payload in kept.items():
        assert payload.read_text(encoding="utf-8") == name, (
            f"dist/{name} was emptied by `artifacts`, which never writes it"
        )
    assert not emptied.exists(), "the control: a directory that IS on the list is emptied"


def test_a_stale_docs_archive_never_reaches_the_manifest(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    stale = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.DOCS_DIRNAME / "swcraft-guide-0.0.9.zip"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale archive", encoding="utf-8")

    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [entry["filename"] for entry in manifest["artifacts"] if entry["kind"] == "docs"] == [
        f"swcraft-guide-{VERSION}.zip"
    ]
    assert not stale.exists()


def test_the_guide_and_its_configuration_are_artifact_sources(tmp_path: Path) -> None:
    """An edited page must age ``dist/``, exactly as an edited ``.ts`` file does."""
    repo_root = make_source_repo(tmp_path)
    collected = {path.relative_to(repo_root).as_posix() for path in ARTIFACTS.collect_artifact_sources(repo_root)}

    assert "docs/guide/index.md" in collected
    assert ARTIFACTS.MKDOCS_CONFIG_FILENAME in collected


def test_the_generated_version_page_is_not_counted_as_a_source(tmp_path: Path) -> None:
    """It is an *output* of the build, sitting inside a directory of inputs."""
    repo_root = make_source_repo(tmp_path)
    ARTIFACTS.write_guide_version_page(repo_root)

    collected = {path.relative_to(repo_root).as_posix() for path in ARTIFACTS.collect_artifact_sources(repo_root)}

    assert (repo_root / ARTIFACTS.GUIDE_VERSION_PAGE).is_file(), "the page has to exist for this to mean anything"
    assert ARTIFACTS.GUIDE_VERSION_PAGE.as_posix() not in collected


def test_a_green_build_does_not_report_dist_stale(tmp_path: Path) -> None:
    """A green build does not report its own outputs as stale sources.

    ``build_all`` writes ``docs/guide/version.md`` and the wheel's ``guide_site/`` payload into source directories,
    and ``stale_artifact_source`` compares every source against the oldest artifact, so counting either would make a
    build stale the instant it finished.
    """
    repo_root = make_source_repo(tmp_path)
    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    reason = ARTIFACTS.stale_artifact_source(repo_root / ARTIFACTS.DIST_DIRNAME, manifest, repo_root)

    assert reason is None, reason


def test_an_edited_guide_page_does_report_dist_stale(tmp_path: Path) -> None:
    """The other direction: the exclusion is one file, not the whole tree."""
    repo_root = make_source_repo(tmp_path)
    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    page = repo_root / ARTIFACTS.GUIDE_DIR / "index.md"
    page.write_text("# Guide\n\nedited after the build\n", encoding="utf-8")
    os.utime(page, (BUILT_AT + 10_000, BUILT_AT + 10_000))

    assert ARTIFACTS.stale_artifact_source(repo_root / ARTIFACTS.DIST_DIRNAME, manifest, repo_root) == (
        "docs/guide/index.md"
    )


# ---------------------------------------------------------------------------
# The wheel's guide payload
# ---------------------------------------------------------------------------


def _fake_payload(repo_root: Path) -> Path:
    """A site tree already sitting in the package, as an aborted copy would leave it."""
    payload = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    (payload / "removed-page").mkdir(parents=True)
    (payload / "index.html").write_text("<html>a guide from an earlier build</html>", encoding="utf-8")
    (payload / "removed-page" / "index.html").write_text("a page the guide no longer has", encoding="utf-8")
    return payload


def _rendered_site(tmp_path: Path) -> Path:
    """A render for the copy to install, shaped the way ``build_docs_site`` leaves one."""
    site = make_guide_site(tmp_path / "site")
    (site / "search").mkdir(parents=True, exist_ok=True)
    (site / "search" / "search_index.json").write_text('{"docs": []}', encoding="utf-8")
    return site


def test_the_wheel_payload_is_not_an_artifact_source(tmp_path: Path) -> None:
    """The rendered site lands *inside* ``packages/tools/src``, which is walked as a source.

    Counting it would move ``source_inventory``'s ~100-file fingerprint to several
    thousand and -- because ``stale_artifact_source`` compares every source against
    the **oldest** artifact -- report ``dist/`` stale the instant a green build
    finished. ``GENERATED_SOURCE_FILES`` cannot express this: it is a set of files
    and this is a tree, which is why the exclusion is in ``IGNORED_SOURCE_DIRS``.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    assert (payload / "index.html").is_file(), "the tree has to exist for this to mean anything"

    collected = {path.relative_to(repo_root).as_posix() for path in ARTIFACTS.collect_artifact_sources(repo_root)}

    prefix = ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.as_posix() + "/"
    assert not [name for name in collected if name.startswith(prefix)]
    assert ARTIFACTS.VERSION_MODULE.as_posix() in collected, "the rest of packages/tools/src is still walked"


def test_install_guide_site_payload_copies_the_rendered_tree(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    site_dir = _rendered_site(tmp_path)
    rendered = (site_dir / "index.html").read_text(encoding="utf-8")

    payload = ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert payload == repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    assert (payload / "index.html").read_text(encoding="utf-8") == rendered
    assert (payload / "search" / "search_index.json").is_file()


def test_install_guide_site_payload_replaces_rather_than_merges(tmp_path: Path) -> None:
    """A page this render no longer has must not survive in the served payload.

    Merging would leave it reachable at its old address with nothing to say it is
    from a build ago -- the locator checks one filename and reads no manifest.
    """
    repo_root = make_source_repo(tmp_path)
    stale = _fake_payload(repo_root) / "removed-page" / "index.html"
    site_dir = _rendered_site(tmp_path)
    rendered = (site_dir / "index.html").read_text(encoding="utf-8")

    payload = ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert not stale.exists()
    assert (payload / "index.html").read_text(encoding="utf-8") == rendered


def test_install_guide_site_payload_refuses_a_directory_that_is_not_a_site(tmp_path: Path) -> None:
    """Copying an empty or absent render would ship a guide that serves nothing.

    The load-bearing half is *where* the refusal happens: before the destination is
    touched, so a bad render does not trade a working payload for an empty tree.
    That has an oracle only if a working payload is planted first: asserting the
    absence of a payload the case never created passes just as well with the guard
    moved after the ``rmtree``.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    before = {
        path.relative_to(payload).as_posix(): path.read_text(encoding="utf-8") for path in payload.rglob("*.html")
    }
    empty = tmp_path / "site"
    empty.mkdir()

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="rendered guide") as raised:
        ARTIFACTS.install_guide_site_payload(repo_root, empty)

    assert str(empty) in str(raised.value), "the refusal has to name the directory it refused"
    after = {path.relative_to(payload).as_posix(): path.read_text(encoding="utf-8") for path in payload.rglob("*.html")}
    assert after == before, "a refused copy must leave the working payload exactly as it was"


def test_install_guide_site_payload_reports_a_removal_it_could_not_make(tmp_path: Path, monkeypatch) -> None:
    """``ignore_errors=True`` would turn this into the *copy*'s failure instead.

    The reclaim of a sidecar a killed run left is the one removal that still fails
    the build, and it has to fail with the reason: swallowed, it would surface as
    ``copytree``'s ``FileExistsError``, which names the directory and not the
    permission or the busy file behind it, leaving a reader to rediscover that for
    themselves. The failing double stands in for the cleanup calls as well, which is
    why it also pins that a best-effort removal cannot replace the reported reason.
    """
    repo_root = make_source_repo(tmp_path)
    _fake_payload(repo_root)
    staging = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME
    staging.mkdir(parents=True)
    site_dir = _rendered_site(tmp_path)

    def _refuse(path, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ARTIFACTS.shutil, "rmtree", _refuse)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not remove the leftover guide payload staging"):
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)


def _fail_partway_through_the_copy(monkeypatch, after: int = 1) -> None:
    """Replace ``copytree`` with one that writes *after* files and then fails.

    A real mid-copy failure (an unreadable source, a full disk) leaves the destination partially written, with
    ``index.html`` among the first entries since ``copytree`` walks the root first; the double reproduces that partial
    tree. It raises ``shutil.Error``, which ``copytree`` raises for an unreadable member and is an ``OSError``.
    """

    def _partial_copytree(source: Path, destination: Path, *args, **kwargs) -> Path:
        written = 0
        for entry in sorted(Path(source).rglob("*"), key=lambda path: path.as_posix()):
            if not entry.is_file():
                continue
            target = Path(destination) / entry.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, target)
            written += 1
            if written >= after:
                break
        raise shutil.Error([(str(source), str(destination), "Input/output error")])

    monkeypatch.setattr(ARTIFACTS.shutil, "copytree", _partial_copytree)


def test_install_guide_site_payload_keeps_the_previous_payload_when_the_copy_fails(tmp_path: Path, monkeypatch) -> None:
    """A failed copy leaves the previous payload, never a half-installed guide.

    Copying straight into the destination would leave a truncated site whose ``index.html`` satisfies the locator's
    only guard, and ``swcraft docs`` would serve it with no failure signal. Staging the copy and renaming it into
    place keeps the previous payload.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    before = {path.relative_to(payload).as_posix(): path.read_bytes() for path in payload.rglob("*") if path.is_file()}
    site_dir = _rendered_site(tmp_path)
    _fail_partway_through_the_copy(monkeypatch)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not stage the guide payload"):
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    after = {path.relative_to(payload).as_posix(): path.read_bytes() for path in payload.rglob("*") if path.is_file()}
    assert after == before, "a failed copy must leave the previous payload exactly as it was"
    staging = payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME
    assert not staging.exists(), "the half-written tree must not be left in the package directory"


def test_install_guide_site_payload_leaves_nothing_servable_when_a_first_copy_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The same failure on a checkout with no payload yet leaves nothing.

    With no previous payload the requirement is an absent tree rather than a truncated one, because the locator
    refuses what is absent and accepts what is truncated.
    """
    repo_root = make_source_repo(tmp_path)
    site_dir = _rendered_site(tmp_path)
    _fail_partway_through_the_copy(monkeypatch)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not stage the guide payload"):
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    destination = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    assert not (destination / ARTIFACTS.SITE_INDEX_FILENAME).exists(), (
        "the locator's only guard is this file, so a partial tree carrying it is a served guide"
    )
    assert not (destination.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME).exists()


def test_install_guide_site_payload_removes_a_staging_tree_a_killed_run_left(tmp_path: Path) -> None:
    """The staging name is fixed, so the next install has to reclaim it.

    ``copytree`` refuses an existing destination, which would turn every build
    after a killed one into a failure that names a directory the reader has never
    heard of.
    """
    repo_root = make_source_repo(tmp_path)
    staging = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME
    staging.mkdir(parents=True)
    (staging / "index.html").write_text("<html>from a run that was killed</html>", encoding="utf-8")
    site_dir = _rendered_site(tmp_path)
    rendered = (site_dir / "index.html").read_text(encoding="utf-8")

    payload = ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert (payload / "index.html").read_text(encoding="utf-8") == rendered
    assert not staging.exists()


def _deny_the_rename_into_place(monkeypatch, *, failures: int, deny_the_restore: bool = False) -> list[str]:
    """Make the staging-to-payload rename raise ``PermissionError`` *failures* times.

    Windows' failure reproduced by its shape, not its platform: a directory rename there fails with ``[WinError 5]
    Access is denied`` while any handle inside the tree is open, and the same command succeeds when run again. A
    ``sys.platform`` gate would make every case below a no-op where it is written. Only the forward rename is denied
    unless *deny_the_restore* asks otherwise; the returned list records one entry per denied attempt.
    """
    real_replace = Path.replace
    attempts: list[str] = []
    staging_name = ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME
    superseded_name = ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME
    payload_name = ARTIFACTS.PACKAGE_GUIDE_SITE_DIRNAME

    def _replace(self: Path, target):
        into_place = self.name == staging_name and Path(target).name == payload_name
        restoring = self.name == superseded_name and Path(target).name == payload_name
        if into_place:
            attempts.append(self.name)
            if len(attempts) <= failures:
                raise PermissionError(5, "Access is denied")
        if restoring and deny_the_restore:
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace)
    monkeypatch.setattr(ARTIFACTS, "GUIDE_TREE_RETRY_WAITS", (0.0, 0.0, 0.0, 0.0))
    return attempts


def test_install_guide_site_payload_retries_a_rename_that_is_denied_and_then_is_not(
    tmp_path: Path, monkeypatch
) -> None:
    """A rename denied and then granted a moment later succeeds on retry.

    On Windows a directory cannot be renamed while a handle inside it is open, and the staged tree is one ``copytree``
    old when the rename runs -- long enough for a scanner to still hold one; the same command re-run succeeded.
    Without the retry that fails a ``verify`` stage, which gates the wheels.
    """
    repo_root = make_source_repo(tmp_path)
    _fake_payload(repo_root)
    site_dir = _rendered_site(tmp_path)
    rendered = (site_dir / "index.html").read_text(encoding="utf-8")
    attempts = _deny_the_rename_into_place(monkeypatch, failures=2)

    payload = ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert (payload / "index.html").read_text(encoding="utf-8") == rendered
    assert len(attempts) == 3, f"the two denials and the grant, not {len(attempts)} attempts"
    assert not (payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME).exists()
    assert not (payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME).exists()


def test_install_guide_site_payload_stops_retrying_and_reports_the_real_error(tmp_path: Path, monkeypatch) -> None:
    """A denial that outlives the budget fails, with the error the last attempt raised.

    The not-blind half of the case above: a retry that never gives up would hang a
    build instead of failing it, and one that reported a retry-flavoured error
    would cost the reader the ``WinError`` that names the cause. The attempt count
    is asserted against :data:`GUIDE_TREE_RETRY_WAITS` rather than against a
    literal, so widening the budget cannot leave this case pinning the old one.
    """
    repo_root = make_source_repo(tmp_path)
    _fake_payload(repo_root)
    site_dir = _rendered_site(tmp_path)
    attempts = _deny_the_rename_into_place(monkeypatch, failures=99)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not move the staged guide payload") as excinfo:
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert len(attempts) == len(ARTIFACTS.GUIDE_TREE_RETRY_WAITS) + 1
    assert "Access is denied" in str(excinfo.value), "the reader needs the cause, not that a retry ran out"


def test_install_guide_site_payload_puts_the_previous_payload_back_when_the_rename_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed rename puts the previous payload back, so the checkout still serves a guide.

    Left under the superseded name, which the locator does not look at, the checkout had no guide until the next
    successful build and ``swcraft docs`` refused.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    before = _tree_contents(payload)
    site_dir = _rendered_site(tmp_path)
    _deny_the_rename_into_place(monkeypatch, failures=99)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="the previous payload is unchanged") as excinfo:
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    assert _tree_contents(payload) == before, "the previous payload must be back, byte for byte"
    assert (payload / ARTIFACTS.SITE_INDEX_FILENAME).is_file(), "the locator's one guard has to be satisfied"
    assert not (payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME).exists()
    assert not (payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME).exists()
    assert "could not move the staged guide payload" in str(excinfo.value)


def test_install_guide_site_payload_says_so_when_it_cannot_put_the_payload_back(tmp_path: Path, monkeypatch) -> None:
    """The recovery can fail too, and then the message must name where the tree is.

    Without this case the wording above ("the previous payload is unchanged") is
    asserted only on the path where it is true, and a recovery that silently did
    nothing would still read as if it had worked -- the exact shape of claim this
    project's evidence rule exists to refuse.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    before = _tree_contents(payload)
    site_dir = _rendered_site(tmp_path)
    _deny_the_rename_into_place(monkeypatch, failures=99, deny_the_restore=True)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not be put back") as excinfo:
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    superseded = payload.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME
    assert _tree_contents(superseded) == before, "whole, under the name the message names"
    assert ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME in str(excinfo.value)
    assert not payload.exists(), "and the reader is told that precisely because it is not here"


def test_install_guide_site_payload_leaves_nothing_servable_when_a_first_rename_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The same failure on a checkout with no payload yet: absent, never truncated.

    With nothing to put back, the destination must not exist, because the locator refuses what is absent and serves
    what is truncated.
    """
    repo_root = make_source_repo(tmp_path)
    site_dir = _rendered_site(tmp_path)
    _deny_the_rename_into_place(monkeypatch, failures=99)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not move the staged guide payload"):
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)

    destination = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    assert not (destination / ARTIFACTS.SITE_INDEX_FILENAME).exists()
    assert not (destination.parent / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME).exists()


def _tree_contents(root: Path) -> dict[str, bytes]:
    """Every file under *root*, by relative path -- what a served payload actually is."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _fail_partway_through_every_removal(monkeypatch) -> None:
    """Replace ``rmtree`` with one that deletes all but ``index.html`` and then fails.

    A real mid-removal failure (a permission, a busy file) leaves the tree partly deleted; over the live payload that
    is a truncated site still holding the one file the locator checks. ``index.html`` is kept deliberately: a removal
    that took it first leaves a tree the locator refuses, which would make the case pass vacuously. Substituted for
    every ``rmtree`` call, so the case asks whether any removal in the function is over a tree the locator can
    resolve.
    """
    real_rmtree = shutil.rmtree

    def _partial_rmtree(path, *args, **kwargs):
        root = Path(path)
        for entry in sorted(root.rglob("*"), reverse=True, key=lambda path: path.as_posix()):  # deepest first
            if entry.name == ARTIFACTS.SITE_INDEX_FILENAME and entry.parent == root:
                continue
            if entry.is_dir():
                real_rmtree(entry)
            else:
                entry.unlink()
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ARTIFACTS.shutil, "rmtree", _partial_rmtree)


def _refuse_the_rename_of(monkeypatch, dirname: str) -> None:
    """Make ``Path.replace`` fail for the directory named *dirname*, and only it."""
    real_replace = Path.replace

    def _replace(self: Path, target):
        if self.name == dirname:
            raise PermissionError(13, "Permission denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace)


@pytest.mark.parametrize(
    ("break_a_step", "aborts"),
    [
        pytest.param(_fail_partway_through_the_copy, True, id="the copy into staging"),
        pytest.param(
            lambda monkeypatch: _refuse_the_rename_of(monkeypatch, ARTIFACTS.PACKAGE_GUIDE_SITE_DIRNAME),
            True,
            id="the rename of the previous payload aside",
        ),
        pytest.param(
            lambda monkeypatch: _refuse_the_rename_of(monkeypatch, ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME),
            True,
            id="the rename of the staged tree into place",
        ),
        pytest.param(_fail_partway_through_every_removal, False, id="every removal, partway through"),
    ],
)
def test_no_step_of_the_payload_rewrite_can_leave_a_truncated_payload(
    tmp_path: Path, monkeypatch, break_a_step, aborts: bool
) -> None:
    """Break each step in turn: what is left is a whole payload or none the locator takes.

    Testing each fix at the step it changed let one window close and another open, so the steps are enumerated and
    one invariant asserted across them: whatever holds ``index.html`` at ``guide_site/`` afterwards is byte for byte
    the previous payload or this render, never a subset (a guide served with pages missing).

    The removal case must not abort: the only ``rmtree`` over a populated tree is the superseded payload's, after the
    new one is in place under a name the locator does not look at, so a failure there costs disk and nothing else.
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    previous = _tree_contents(payload)
    site_dir = _rendered_site(tmp_path)
    rendered = _tree_contents(site_dir)
    assert previous != rendered, "the two states have to differ or the invariant below is vacuous"
    break_a_step(monkeypatch)

    raised: Exception | None = None
    try:
        ARTIFACTS.install_guide_site_payload(repo_root, site_dir)
    except Exception as exc:  # noqa: BLE001 -- the case is about the tree, not the type
        raised = exc

    if guide_site.is_site(payload):
        assert _tree_contents(payload) in (previous, rendered), (
            "what the locator accepts at guide_site/ is neither the previous guide nor this render, "
            "so a failure left a truncated tree that `swcraft docs` would still serve"
        )
    assert (raised is not None) == aborts, f"the build {'must abort' if aborts else 'must not abort'}: {raised!r}"
    if not aborts:
        assert _tree_contents(payload) == rendered, "a run that did not abort has to have installed this render"


@pytest.mark.parametrize(
    "dirname",
    [ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME, ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME],
)
def test_a_sidecar_tree_is_not_an_artifact_source_either(tmp_path: Path, dirname: str) -> None:
    """A killed run leaves one in ``packages/tools/src``, which is walked as a source tree.

    Counted there it would do exactly what the payload itself would: move
    ``source_inventory``'s fingerprint by several thousand files and report
    ``dist/`` stale immediately after a green build. Both sidecars are the payload one
    step before or one step after it *is* the payload, so both are excluded.
    """
    repo_root = make_source_repo(tmp_path)
    staging = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent / dirname
    staging.mkdir(parents=True)
    (staging / "index.html").write_text("<html>half a guide</html>", encoding="utf-8")

    collected = {path.relative_to(repo_root).as_posix() for path in ARTIFACTS.collect_artifact_sources(repo_root)}

    prefix = staging.relative_to(repo_root).as_posix() + "/"
    assert not [name for name in collected if name.startswith(prefix)]
    assert ARTIFACTS.VERSION_MODULE.as_posix() in collected, "the rest of packages/tools/src is still walked"


def test_build_all_installs_the_rendered_site_as_wheel_payload(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)

    ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    payload = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    assert (payload / "index.html").read_text(encoding="utf-8") == "<html>guide</html>"
    assert (payload / "assets" / "app.css").is_file(), "the payload is the whole tree, not just its root"
    assert (payload / ARTIFACTS.GUIDE_CORPUS_FILENAME).is_file(), "the corpus travels as a file of the site"


def test_build_all_renders_the_site_before_it_builds_the_wheels(tmp_path: Path) -> None:
    """``mkdocs`` runs before the first ``python -m build``.

    Pins the render's position and nothing else: the render touches no package tree, so moving the payload install
    after ``build_python_artifacts`` leaves this green (that is
    ``test_build_all_installs_the_guide_payload_before_it_builds_the_wheels``'s subject). It still matters: the
    install copies from the render.
    """
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root)

    ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    kinds = [argv[1:3] for argv in runner.calls]
    assert kinds.index(["-m", "mkdocs"]) < kinds.index(["-m", "build"])


def test_build_all_installs_the_guide_payload_before_it_builds_the_wheels(tmp_path: Path) -> None:
    """The wheel has to be built from this run's payload, not the last run's.

    Asserted on the payload's content at the moment ``-m build`` runs rather than on argv order: a stale payload makes
    a wheel whose digest describes developer state, not the commit. A payload from an earlier build is planted first,
    so the case fails both ways the install can be misplaced (last build's guide, or none on a fresh checkout).
    """
    repo_root = make_source_repo(tmp_path)
    payload = _fake_payload(repo_root)
    inner = FakeRunner(repo_root)
    seen_by_each_wheel_build: list[str | None] = []

    def runner(argv, cwd) -> None:
        if [str(part) for part in argv][1:3] == ["-m", "build"]:
            index = payload / "index.html"
            seen_by_each_wheel_build.append(index.read_text(encoding="utf-8") if index.is_file() else None)
        inner(argv, cwd)

    ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    assert len(seen_by_each_wheel_build) == len(ARTIFACTS.PYTHON_PROJECTS), "not every project was built"
    assert set(seen_by_each_wheel_build) == {"<html>guide</html>"}, (
        f"a wheel was built against a payload that is not this run's render: {seen_by_each_wheel_build}"
    )


def test_build_all_writes_two_trees_and_the_named_files_outside_dist(tmp_path: Path) -> None:
    """The checkout footprint, measured rather than described.

    ``build_all`` writes exactly two trees outside ``dist/``: the guide payload, inside a package source tree
    hatchling ships, and the gitignored *Release notes* section under ``docs/guide/``. It also writes the guide's
    version page and the root ``LICENSE`` and ``NOTICE`` into each Python project's root (for hatchling's default
    ``license-files``). The agent's system prompt is tracked package source, so no copy of it is written.

    Not reached: the npm half, which is behind ``FakeRunner`` (a real run restores ``node_modules/`` and rewrites
    ``vscode_extension/dist/``, which ships in the ``.vsix``), what a real ``python -m build`` leaves in its temporary
    directories, and what a failed run leaves. So this pins what ``build_all`` plans on the Python side, not the full
    footprint of ``dev.py artifacts``.
    """
    repo_root = make_source_repo(tmp_path)

    def snapshot() -> set[str]:
        return {
            path.relative_to(repo_root).as_posix()
            for path in repo_root.rglob("*")
            if path.is_file() and not path.relative_to(repo_root).as_posix().startswith(ARTIFACTS.DIST_DIRNAME + "/")
        }

    before = snapshot()
    ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")
    written = snapshot() - before

    payload_prefix = ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.as_posix() + "/"
    notes_prefix = ARTIFACTS.GUIDE_RELEASE_NOTES_DIR.as_posix() + "/"
    assert {name for name in written if not name.startswith((payload_prefix, notes_prefix))} == {
        ARTIFACTS.GUIDE_VERSION_PAGE.as_posix(),
        *(
            f"{ARTIFACTS.PACKAGES_DIR.as_posix()}/{project}/{name}"
            for project in ARTIFACTS.PYTHON_PROJECTS
            for name in ARTIFACTS.LICENCE_FILENAMES
        ),
    }, f"the run wrote something outside dist/ that no document describes: {sorted(written)}"
    assert {name for name in written if name.startswith(payload_prefix)}, "the payload tree was not written at all"
    assert {name for name in written if name.startswith(notes_prefix)} == {
        f"{notes_prefix}{ARTIFACTS.GUIDE_RELEASE_NOTES_INDEX}",
        f"{notes_prefix}{VERSION}.md",
    }, "the release notes section is the index and the one note the tree carries"
    assert payload_prefix.startswith("packages/tools/src/swcraft_tools/"), (
        "the payload is only shipped by a wheel because it sits inside the package hatchling packages"
    )


def test_the_artifact_build_invokes_mkdocs_exactly_once(tmp_path: Path) -> None:
    """The payload is copied from the archive's own render, never rendered again."""
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root)

    ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    assert [argv[1:4] for argv in runner.calls].count(["-m", "mkdocs", "build"]) == 1


def test_the_manifest_order_is_unchanged_by_the_payload_step(tmp_path: Path) -> None:
    """``build_docs_archive`` moved ahead of the wheels; the recorded set did not."""
    repo_root = make_source_repo(tmp_path)

    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [entry["kind"] for entry in manifest["artifacts"]] == [
        "docs",
        "vsix",
        "wheel",
        "sdist",
        "wheel",
        "sdist",
        "wheel",
        "sdist",
        "wheel",
        "sdist",
    ]


def test_a_developer_preview_does_not_write_the_package_tree(tmp_path: Path) -> None:
    """``dev.py docs`` renders into ``dist/docs/site/`` and never into ``packages/tools/src/``.

    A preview that wrote the package tree would age it past the artifacts on every run; ``SWCRAFT_GUIDE_SITE`` is the
    working-tree path instead. Not reached: ``docs/guide/version.md``, which every render rewrites, the archive step,
    and what a real mkdocs run writes.
    """
    repo_root = make_source_repo(tmp_path)

    ARTIFACTS.build_docs_site(repo_root, run=FakeRunner(repo_root))

    assert ARTIFACTS.docs_site_dir(repo_root / ARTIFACTS.DIST_DIRNAME).is_dir()
    assert not (repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR).exists()


def test_the_payload_destination_is_the_directory_hatchling_ships_and_the_command_looks_in() -> None:
    """``PACKAGE_GUIDE_SITE_DIR`` is the directory hatchling ships and ``guide_site.py`` resolves.

    A path assertion derived from the constant cannot police the constant itself.
    """
    assert ARTIFACTS.PACKAGE_GUIDE_SITE_DIR == Path("packages") / "tools" / "src" / "swcraft_tools" / "guide_site"
    assert ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.name == ARTIFACTS.PACKAGE_GUIDE_SITE_DIRNAME


def test_the_payload_tree_is_gitignored() -> None:
    """A build product written into ``packages/tools/src`` and never committed.

    Nothing else keeps it out of ``git status``, and an accidental commit of 71
    generated files is the kind of thing a reviewer notices only after it lands.
    The anchor matters as much as the entry: ``guide_site`` unanchored would also
    match ``guide_site.py``'s own directory names elsewhere in the tree.
    """
    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert f"/{ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.as_posix()}/" in entries


def test_both_sidecars_of_the_payload_rewrite_are_gitignored_too() -> None:
    """A run killed mid-rewrite leaves one of them, and either is a whole site tree.

    Same consequence as the payload's own entry -- a commit of thousands of generated
    files -- and the same anchoring, since both names sit in the package directory.
    """
    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    parent = ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent.as_posix()
    for dirname in (ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME, ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME):
        assert f"/{parent}/{dirname}/" in entries


def test_the_packaging_exclusion_names_both_payload_sidecars() -> None:
    """``packages/tools/pyproject.toml``'s hatch ``exclude`` names both payload sidecars and not the payload.

    Hatchling ships whatever sits under ``packages``; the entries are literal strings the builder cannot see, so this
    keeps them agreeing with the builder's constants. A widened pattern (``guide_site*``) would exclude the guide too,
    so the payload's own directory is asserted absent. Not reached: a built archive -- that ``exclude`` works is
    ``test_no_sidecar_of_the_payload_rewrite_rides_the_wheel_or_the_sdist``.
    """
    config = tomllib.loads((REPO_ROOT / "packages" / "tools" / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["exclude"]

    package_parent = ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent.relative_to(ARTIFACTS.PACKAGES_DIR / "tools").as_posix()
    for dirname in (ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME, ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME):
        assert f"/{package_parent}/{dirname}" in excluded, (
            f"{dirname} is not excluded from the built archives, so a cleanup that fails ships it"
        )
    assert f"/{package_parent}/{ARTIFACTS.PACKAGE_GUIDE_SITE_DIRNAME}" not in excluded, (
        "the payload itself must ship -- excluding it is how the guide silently stops being installed"
    )


def test_a_build_that_aborts_after_the_payload_keeps_it_and_writes_no_manifest(tmp_path: Path) -> None:
    """The payload is installed before the wheels, so an abort can happen after it.

    Two things are asserted because they pull in opposite directions: no manifest is
    written (``build_all``'s own promise -- an aborted build leaves no manifest
    describing a partial set), and the payload it already wrote is *kept*. Removing
    it on the way out would leave a developer's editable install with no guide to
    serve because a later, unrelated step failed.
    """
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, fail_on=lambda argv: argv[:3] == ["npm", "run", "package"])

    with pytest.raises(subprocess.CalledProcessError):
        ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    payload = repo_root / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR
    assert (payload / "index.html").read_text(encoding="utf-8") == "<html>guide</html>"
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.MANIFEST_FILENAME).exists()
