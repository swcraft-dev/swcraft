"""Tests for ``scripts/build_artifacts.py`` — the local artifact builder.

The builder is split into pure helpers (filename -> kind/name mapping, SHA-256,
manifest assembly, ``SHA256SUMS`` rendering) and orchestration whose every
subprocess call goes through an injectable ``run`` callable. That split is what
lets the expensive-to-reproduce failure modes be tested without executing a real
build:

* a stale wheel from an earlier version must not survive into the manifest;
* a failing sub-build must abort with a non-zero exit and write **no** manifest —
  a half-written manifest describing a partial artifact set is worse than none;
* the extension must always be packaged through ``npm ci`` -> ``clean`` ->
  ``build`` -> ``package``, since ``checkPackagePreconditions()``'s freshness
  guard is only half the protection against shipping a stale bundle.

The one end-to-end case (``@pytest.mark.artifact``) really builds the wheels and
the ``.vsix``; it is deselected from the fast test run and executed only by
``dev.py test-artifacts``.
"""

from __future__ import annotations

import ast
import base64
import gzip
import hashlib
import http.client
import inspect
import io
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import urllib.error
import urllib.parse
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from build_artifacts_support import (
    ARCHIVE_TOKENS,
    ARTIFACTS,
    BUILT_AT,
    FIXTURE_ELKJS_LICENCE,
    FIXTURE_ELKJS_VERSION,
    FIXTURE_EXTENSIONS,
    FIXTURE_NODE_VERSION,
    FIXTURE_PYTHON_BUILD_TAG,
    FIXTURE_PYTHON_VERSION,
    FIXTURE_WHEEL_TIMESTAMP,
    NPM_LICENCE_DECOY,
    VERSION,
    FakeRunner,
    fixture_elkjs_body,
    fixture_node_body,
    fixture_python_body,
    fixture_target,
    fixture_vsix_body,
    make_source_repo,
    stage_fixture_elkjs_payload,
    write_pin_files,
    write_runtime_pins,
)
from conftest import (
    REPO_ROOT,
    require_workstation_archive,
    workstation_install_module,
)
from mode_bit_support import require_executable_bit
from symlink_support import skip_without_symlink_support
from windows_newline_support import assert_no_crlf, windows_newline_translation

_SCRIPT_PATH = REPO_ROOT / "scripts" / "build_artifacts.py"

#: The agent's system prompt, tracked inside the package hatchling packages.
AGENT_PACKAGE_PROMPT = Path("packages") / "agent" / "src" / "swcraft_agent" / "agent-system-prompt.md"


@pytest.fixture(autouse=True)
def _no_download_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every download and Marketplace query in this module makes one attempt unless a test says otherwise.

    With the real ``DOWNLOAD_RETRY_WAITS`` each test whose fake fails transport on every
    attempt would sleep 65 s -- four of them, 260 s added to the fast gate. The retry's own
    tests set the waits to ``(0, 0, 0)`` and record them through a patched ``time.sleep``.
    """
    monkeypatch.setattr(ARTIFACTS, "DOWNLOAD_RETRY_WAITS", ())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("swcraft_tools-0.1.0-py3-none-any.whl", "wheel"),
        ("swcraft_mcp-0.1.0.tar.gz", "sdist"),
        ("swcraft-vscode-0.1.0.vsix", "vsix"),
        ("swcraft-guide-0.1.0.zip", "docs"),
    ],
)
def test_artifact_kind(filename: str, kind: str) -> None:
    assert ARTIFACTS.artifact_kind(filename) == kind


def test_artifact_kind_rejects_an_unrecognized_filename() -> None:
    """`.zip` alone is not a kind — the docs rule keys off the *prefix*.

    This is why `artifact_kind` matches `swcraft-guide-`, not `.zip`: a stray
    archive in `dist/` must keep raising rather than be filed as documentation and
    silently gain a manifest entry.
    """
    with pytest.raises(ValueError, match="unrecognized artifact filename"):
        ARTIFACTS.artifact_kind("swcraft_tools-0.1.0.zip")


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("swcraft_tools-0.1.0-py3-none-any.whl", "swcraft-tools"),
        ("swcraft_mcp-0.1.0-py3-none-any.whl", "swcraft-mcp"),
        ("swcraft_agent-0.1.0.tar.gz", "swcraft-agent"),
        ("swcraft-vscode-0.1.0.vsix", "swcraft-vscode"),
        ("swcraft-guide-0.1.0.zip", "swcraft-guide"),
    ],
)
def test_artifact_name_reports_the_canonical_distribution_name(filename: str, name: str) -> None:
    assert ARTIFACTS.artifact_name(filename) == name


def test_artifact_name_does_not_strip_a_vsix_length_suffix_off_the_docs_archive() -> None:
    """`.zip` is four characters and `.vsix` is five: a shared branch would mangle it.

    Asserted on a version whose last character survives being over-stripped, so
    the case fails on the length rather than on a name that happens to differ.
    """
    assert ARTIFACTS.artifact_name("swcraft-guide-1.2.3.zip") == "swcraft-guide"


@pytest.mark.parametrize(
    ("kind", "filename", "expected"),
    [
        ("wheel", "swcraft_tools-0.1.0-py3-none-any.whl", "python/swcraft_tools-0.1.0-py3-none-any.whl"),
        ("sdist", "swcraft_tools-0.1.0.tar.gz", "python/swcraft_tools-0.1.0.tar.gz"),
        ("vsix", "swcraft-vscode-0.1.0.vsix", "vscode/swcraft-vscode-0.1.0.vsix"),
        ("docs", "swcraft-guide-0.1.0.zip", "docs/swcraft-guide-0.1.0.zip"),
    ],
)
def test_artifact_relative_path_files_each_kind_in_its_own_directory(kind: str, filename: str, expected: str) -> None:
    assert ARTIFACTS.artifact_relative_path({"kind": kind, "filename": filename}) == expected


def test_artifact_relative_path_refuses_an_unrecognized_kind() -> None:
    """Not defaulted to `dist/vscode/`: SHA256SUMS would point at nothing."""
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="unrecognized artifact kind"):
        ARTIFACTS.artifact_relative_path({"kind": "tarball", "filename": "swcraft-0.1.0.tar"})


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "artifact.whl"
    payload = b"x" * (1024 * 1024 + 17)  # spans the reader's chunk boundary
    target.write_bytes(payload)

    assert ARTIFACTS.sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_build_manifest_describes_every_artifact(tmp_path: Path) -> None:
    files = []
    for filename in (
        "swcraft-vscode-0.1.0.vsix",
        "swcraft-guide-0.1.0.zip",
        "swcraft_tools-0.1.0-py3-none-any.whl",
        "swcraft_tools-0.1.0.tar.gz",
    ):
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        files.append(path)

    manifest = ARTIFACTS.build_manifest(
        version="0.1.0",
        git_commit="abc123",
        built_at=datetime(2026, 8, 1, 10, 30, 0, 123456, tzinfo=UTC),
        artifact_paths=files,
        sources={"count": 4, "sha256": "deadbeef"},
    )

    assert manifest["version"] == "0.1.0"
    assert manifest["git_commit"] == "abc123"
    assert manifest["built_at"] == "2026-08-01T10:30:00Z"
    assert manifest[ARTIFACTS.SOURCES_KEY] == {"count": 4, "sha256": "deadbeef"}

    # Sorted by filename, so two builds of the same tree render the same document.
    # `swcraft-g` sorts before `swcraft-v`, which is what puts the docs archive first.
    assert [entry["filename"] for entry in manifest["artifacts"]] == [
        "swcraft-guide-0.1.0.zip",
        "swcraft-vscode-0.1.0.vsix",
        "swcraft_tools-0.1.0-py3-none-any.whl",
        "swcraft_tools-0.1.0.tar.gz",
    ]
    assert [entry["kind"] for entry in manifest["artifacts"]] == ["docs", "vsix", "wheel", "sdist"]
    for entry, path in zip(manifest["artifacts"], sorted(files, key=lambda p: p.name), strict=True):
        assert set(entry) == {"name", "kind", "filename", "sha256", "size"}
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["size"] == path.stat().st_size


# ---------------------------------------------------------------------------
# Artifact staleness (what makes `dev.py test-artifacts` assert against the
# sources on disk rather than against whatever was built last)
# ---------------------------------------------------------------------------


#: The repository's own exclusion list, read rather than transcribed: the
#: freshness derivation is only as good as this file, so the fixtures exercise it.
_VSCODEIGNORE = (REPO_ROOT / ARTIFACTS.VSCODE_PROJECT / ARTIFACTS.VSCODE_IGNORE_FILENAME).read_text(encoding="utf-8")


def _write_vscodeignore(repo_root: Path) -> None:
    """Give a synthetic repo the real packaging exclusions."""
    target = repo_root / ARTIFACTS.VSCODE_PROJECT / ARTIFACTS.VSCODE_IGNORE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_VSCODEIGNORE, encoding="utf-8")
    os.utime(target, (BUILT_AT - 60, BUILT_AT - 60))


def _built_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    """A repo tree with one packaged source file and one built artifact for it.

    The real ``.vscodeignore`` is written *before* the manifest, so the recorded
    source inventory describes the same tree every case below then edits. A test
    that adds a file afterwards is exercising an addition (which the mtime scan
    reports); the deletion cases need the inventory.
    """
    repo_root = tmp_path / "repo"
    source = repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py"
    source.parent.mkdir(parents=True)
    source.write_text("# source\n", encoding="utf-8")
    _write_vscodeignore(repo_root)

    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    wheel = dist_root / ARTIFACTS.PYTHON_DIRNAME / f"swcraft_tools-{VERSION}-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_text("wheel", encoding="utf-8")
    os.utime(wheel, (BUILT_AT, BUILT_AT))
    os.utime(source, (BUILT_AT - 60, BUILT_AT - 60))

    manifest = ARTIFACTS.build_manifest(
        version=VERSION,
        git_commit="abc123",
        built_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        artifact_paths=[wheel],
        sources=ARTIFACTS.source_inventory(repo_root),
    )
    return repo_root, dist_root, manifest


def test_stale_artifact_source_reports_nothing_when_the_artifacts_are_newer(tmp_path: Path) -> None:
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) is None


def test_stale_artifact_source_names_a_source_edited_after_the_build(tmp_path: Path) -> None:
    """A source edited after the build makes the artifacts stale, whatever the manifest's version says.

    The defect this exists for: the version in `manifest.json` is a constant for the whole of
    development, so "the version matches" accepted wheels built from older sources and
    `dev.py test-artifacts` asserted against code it never packaged — and reported green.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    source = repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py"
    os.utime(source, (BUILT_AT + 60, BUILT_AT + 60))

    assert (
        ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) == "packages/tools/src/swcraft_tools/writer.py"
    )


def test_stale_artifact_source_watches_the_extension_sources_too(tmp_path: Path) -> None:
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    extension_source = repo_root / ARTIFACTS.VSCODE_PROJECT / "src" / "extension.ts"
    extension_source.parent.mkdir(parents=True)
    extension_source.write_text("// source\n", encoding="utf-8")
    os.utime(extension_source, (BUILT_AT + 60, BUILT_AT + 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) == "vscode_extension/src/extension.ts"


@pytest.mark.parametrize("relative", ["package-lock.json", "tsconfig.json", ".vscodeignore"])
def test_stale_artifact_source_watches_the_vsix_packaging_inputs(relative: str, tmp_path: Path) -> None:
    """A change to the `.vsix` packaging inputs makes the extension stale.

    The `.vsix` is not built from `src/` alone: `npm ci` restores the locked dependency tree,
    `tsc`/esbuild read the TypeScript configuration, and vsce honours the packaging exclusions. A
    change confined to one of those (a dependency bump, an exclusion edit) would otherwise leave the
    packaged extension judged fresh.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    packaging_input = repo_root / ARTIFACTS.VSCODE_PROJECT / relative
    packaging_input.parent.mkdir(parents=True, exist_ok=True)
    packaging_input.write_text("{}\n", encoding="utf-8")
    os.utime(packaging_input, (BUILT_AT + 60, BUILT_AT + 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) == f"vscode_extension/{relative}"


@pytest.mark.parametrize("relative", ["media/icon.svg", "README.md", "biome.json"])
def test_stale_artifact_source_watches_files_that_only_ship_in_the_vsix(relative: str, tmp_path: Path) -> None:
    """A file that only ships in the `.vsix` is watched: the set is derived from `.vscodeignore`.

    The defect this exists for: the freshness set was a hand-list of *build* inputs, so an icon, a
    README or a bundled config -- packaged verbatim, read by no build step -- could change with the
    `.vsix` still judged fresh, and `dev.py test-artifacts` would reuse yesterday's archive.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    packaged = repo_root / ARTIFACTS.VSCODE_PROJECT / relative
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_text("shipped\n", encoding="utf-8")
    os.utime(packaged, (BUILT_AT + 60, BUILT_AT + 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) == f"vscode_extension/{relative}"


def test_stale_artifact_source_ignores_extension_files_that_are_not_packaged(tmp_path: Path) -> None:
    """A touched extension file that is not packaged forces no rebuild.

    `.vscodeignore` is the authority in both directions: a debug launch config neither ships nor
    builds anything.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    excluded = repo_root / ARTIFACTS.VSCODE_PROJECT / ".vscode" / "launch.json"
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text("{}\n", encoding="utf-8")
    os.utime(excluded, (BUILT_AT + 60, BUILT_AT + 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) is None


def test_stale_artifact_source_notices_a_packaged_source_that_was_deleted(tmp_path: Path) -> None:
    """A packaged source deleted after the build makes the artifacts stale.

    The defect this exists for: mtimes can only compare files that are still there. A module or
    asset *deleted* after the build leaves no path to be found newer than anything, so the wheels and
    the `.vsix` — which still carry the removed code — were judged current and `dev.py
    test-artifacts` reused them.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    (repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py").unlink()

    stale = ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root)

    assert stale is not None
    assert "packaged sources changed" in stale


def test_stale_artifact_source_notices_a_packaged_file_that_was_renamed(tmp_path: Path) -> None:
    """A rename with the mtime carried over makes the artifacts stale.

    A rename by `cp -p` or an archive restore changes no content and ages nothing — only the inventory
    sees it.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    source = repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py"
    renamed = source.with_name("mutations.py")
    source.rename(renamed)
    os.utime(renamed, (BUILT_AT - 60, BUILT_AT - 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) is not None


def test_stale_artifact_source_notices_a_content_edit_that_did_not_age_the_file(tmp_path: Path) -> None:
    """A content edit that did not age the file makes the artifacts stale.

    The defect this exists for: the inventory used to fingerprint the source *paths* only, so content
    freshness rested entirely on mtimes -- and an mtime is not a truthful record of content. `cp -p`,
    an archive or worktree restore, a checkout that rewinds a file, or a coarse-granularity filesystem
    all reproduce an edit with an unchanged (here: deliberately older) timestamp, and `dev.py
    test-artifacts` would install wheels built from different code.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    source = repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py"
    source.write_text("# edited, same length as before\n", encoding="utf-8")
    os.utime(source, (BUILT_AT - 60, BUILT_AT - 60))

    stale = ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root)

    assert stale is not None
    assert "packaged sources changed" in stale


def test_source_inventory_moves_when_a_packaged_file_s_content_changes(tmp_path: Path) -> None:
    """The fingerprint moves with a packaged file's bytes, independent of any mtime.

    Same path, same file count, different bytes must be a different digest.
    """
    repo_root, _, _ = _built_repo(tmp_path)
    before = ARTIFACTS.source_inventory(repo_root)
    source = repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py"
    source.write_text("# different content\n", encoding="utf-8")

    after = ARTIFACTS.source_inventory(repo_root)

    assert after["count"] == before["count"]
    assert after["sha256"] != before["sha256"]


def test_source_inventory_ignores_mtimes(tmp_path: Path) -> None:
    """The negative control: touching a file does not move the inventory.

    Touching changes nothing about what was packaged (the mtime scan owns that question, and it is the
    one that can name the file).
    """
    repo_root, _, _ = _built_repo(tmp_path)
    before = ARTIFACTS.source_inventory(repo_root)
    os.utime(repo_root / "packages" / "tools" / "src" / "swcraft_tools" / "writer.py", (BUILT_AT + 600, BUILT_AT + 600))

    assert ARTIFACTS.source_inventory(repo_root) == before


def test_stale_artifact_source_treats_a_manifest_without_an_inventory_as_stale(tmp_path: Path) -> None:
    """A manifest without a source inventory is stale.

    A `dist/` built before the inventory existed cannot answer the deletion question at all. One
    rebuild is cheaper than an acceptance run against artifacts nothing can verify.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    del manifest[ARTIFACTS.SOURCES_KEY]

    stale = ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root)

    assert stale is not None
    assert "no packaged-source inventory" in stale


def test_source_inventory_covers_the_same_set_the_freshness_scan_walks(tmp_path: Path) -> None:
    """The inventory watches the same file set the mtime scan walks.

    One derivation, not two: a different set would leave a deletion outside it unreported.
    """
    repo_root, _, _ = _built_repo(tmp_path)
    inventory = ARTIFACTS.source_inventory(repo_root)

    assert inventory["count"] == len(ARTIFACTS.collect_artifact_sources(repo_root))
    assert ARTIFACTS.source_inventory(repo_root) == inventory, "the fingerprint must be stable"


def test_source_inventory_ignores_a_file_that_is_not_packaged(tmp_path: Path) -> None:
    """The negative control: a file the `.vsix` never ships does not move the fingerprint.

    Adding it (and so also removing it) leaves the inventory as it was.
    """
    repo_root, _, _ = _built_repo(tmp_path)
    before = ARTIFACTS.source_inventory(repo_root)
    excluded = repo_root / ARTIFACTS.VSCODE_PROJECT / ".vscode" / "launch.json"
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text("{}\n", encoding="utf-8")

    assert ARTIFACTS.source_inventory(repo_root) == before


def test_packaged_vscode_files_covers_the_repository_extension() -> None:
    """The packaged-file derivation holds for the real extension project, not a fixture."""
    packaged = {
        path.relative_to(REPO_ROOT / ARTIFACTS.VSCODE_PROJECT).as_posix()
        for path in ARTIFACTS.packaged_vscode_files(REPO_ROOT)
    }

    assert {"package.json", "README.md", "biome.json", "media/icon.svg"} <= packaged
    assert not [name for name in packaged if name.startswith(("src/", "out/", "node_modules/", ".vscode/"))]


@pytest.mark.parametrize(
    ("relative", "packaged"),
    [
        ("package.json", True),
        ("media/icon.svg", True),
        ("scripts/tsc.mjs", True),
        ("src/extension.ts", False),
        (".vscode/launch.json", False),
        ("src/deep/nested/thing.ts", False),
        ("fixtures/agents/copilot-instructions.md", False),
        ("dist/extension.js.map", False),
        ("tsconfig.json", False),
    ],
)
def test_is_packaged_follows_the_vscodeignore_globs(relative: str, packaged: bool) -> None:
    rules = ARTIFACTS.parse_vscodeignore(_VSCODEIGNORE)
    assert ARTIFACTS.is_packaged(relative, rules) is packaged


def test_parse_vscodeignore_skips_comments_and_honours_negation() -> None:
    rules = ARTIFACTS.parse_vscodeignore("# a comment\n\nfixtures/**\n!fixtures/keep.json\n")

    assert rules == [("fixtures/**", False), ("fixtures/keep.json", True)]
    assert ARTIFACTS.is_packaged("fixtures/drop.json", rules) is False
    assert ARTIFACTS.is_packaged("fixtures/keep.json", rules) is True


def test_stale_artifact_source_ignores_build_output_and_caches(tmp_path: Path) -> None:
    """Build output and caches never make the artifacts stale.

    A `__pycache__` written by the very test run asking the question would otherwise make every
    artifact set permanently stale.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    for relative in (
        ARTIFACTS.PACKAGES_DIR / "tools" / "src" / "swcraft_tools" / "__pycache__" / "writer.cpython-311.pyc",
        ARTIFACTS.PACKAGES_DIR / "tools" / "src" / "swcraft_tools.egg-info" / "PKG-INFO",
        Path(ARTIFACTS.VSCODE_PROJECT) / "src" / "node_modules" / "pkg" / "index.js",
    ):
        noise = repo_root / relative
        noise.parent.mkdir(parents=True, exist_ok=True)
        noise.write_text("noise", encoding="utf-8")
        os.utime(noise, (BUILT_AT + 60, BUILT_AT + 60))

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) is None


def test_stale_artifact_source_reports_nothing_when_no_artifact_is_on_disk(tmp_path: Path) -> None:
    """With no artifact on disk, the freshness check reports nothing.

    Absence is the caller's own (earlier) condition; this function only answers the freshness
    question, and must not claim staleness it cannot see.
    """
    repo_root, dist_root, manifest = _built_repo(tmp_path)
    (dist_root / ARTIFACTS.PYTHON_DIRNAME / f"swcraft_tools-{VERSION}-py3-none-any.whl").unlink()

    assert ARTIFACTS.stale_artifact_source(dist_root, manifest, repo_root) is None


def test_format_sha256sums_uses_coreutils_format_with_dist_relative_paths() -> None:
    entries = [
        {"kind": "vsix", "filename": "swcraft-vscode-0.1.0.vsix", "sha256": "cc"},
        {"kind": "wheel", "filename": "swcraft_tools-0.1.0-py3-none-any.whl", "sha256": "aa"},
        {"kind": "sdist", "filename": "swcraft_tools-0.1.0.tar.gz", "sha256": "bb"},
    ]

    assert ARTIFACTS.format_sha256sums(entries) == (
        "aa  python/swcraft_tools-0.1.0-py3-none-any.whl\n"
        "bb  python/swcraft_tools-0.1.0.tar.gz\n"
        "cc  vscode/swcraft-vscode-0.1.0.vsix\n"
    )


def test_write_manifest_emits_lf_even_where_the_platform_would_translate(tmp_path: Path) -> None:
    """``write_manifest`` writes LF bytes even where the platform would translate them.

    ``SHA256SUMS`` is consumed by ``sha256sum -c``, and a CRLF one fails on the machine that checks it:
    coreutils reads the trailing ``\r`` as part of the filename and reports "No such file or
    directory". ``manifest.json`` travels beside it and is compared across builds.

    The bytes are the assertion, not the keyword -- on Linux ``newline=None``
    already writes LF, so :func:`windows_newline_translation` supplies the
    translating platform. ``write_manifest`` opens two files, which is the case
    a source `grep` would get wrong: naming the keyword on one of them and not
    the other still fails here.
    """
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    manifest = {
        "version": "1.2.3",
        "artifacts": [
            {"kind": "wheel", "filename": "swcraft_tools-1.2.3-py3-none-any.whl", "sha256": "aa"},
            {"kind": "vsix", "filename": "swcraft-vscode-1.2.3.vsix", "sha256": "bb"},
        ],
    }

    with windows_newline_translation():
        manifest_path = ARTIFACTS.write_manifest(dist_root, manifest)

    assert_no_crlf(manifest_path, what="the written manifest.json")
    sums = assert_no_crlf(dist_root / ARTIFACTS.SHA256SUMS_FILENAME, what="the written SHA256SUMS")
    assert sums == ARTIFACTS.format_sha256sums(manifest["artifacts"]).encode("utf-8")


def test_read_version_reads_the_single_source_of_truth(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path, version="1.2.3")

    assert ARTIFACTS.read_version(repo_root) == "1.2.3"


def test_read_version_reports_a_version_module_with_no_literal(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    (repo_root / ARTIFACTS.VERSION_MODULE).write_text("__version__ = str(1)\n", encoding="utf-8")

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="__version__"):
        ARTIFACTS.read_version(repo_root)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_build_all_writes_the_manifest_and_sha256sums(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root)

    manifest_path = ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == VERSION
    assert {entry["filename"] for entry in manifest["artifacts"]} == {
        f"swcraft_tools-{VERSION}-py3-none-any.whl",
        f"swcraft_tools-{VERSION}.tar.gz",
        f"swcraft_mcp-{VERSION}-py3-none-any.whl",
        f"swcraft_mcp-{VERSION}.tar.gz",
        f"swcraft_agent-{VERSION}-py3-none-any.whl",
        f"swcraft_agent-{VERSION}.tar.gz",
        f"swcraft_specimens-{VERSION}-py3-none-any.whl",
        f"swcraft_specimens-{VERSION}.tar.gz",
        f"swcraft-vscode-{VERSION}.vsix",
        f"swcraft-guide-{VERSION}.zip",
    }

    sums = (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.SHA256SUMS_FILENAME).read_text(encoding="utf-8")
    assert len(sums.splitlines()) == len(manifest["artifacts"])
    assert f"vscode/swcraft-vscode-{VERSION}.vsix" in sums
    assert f"docs/swcraft-guide-{VERSION}.zip" in sums

    # The .vsix is moved out of the extension project into dist/vscode/.
    assert not list((repo_root / ARTIFACTS.VSCODE_PROJECT).glob("*.vsix"))
    assert (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.VSCODE_DIRNAME / f"swcraft-vscode-{VERSION}.vsix").is_file()


def test_extension_is_packaged_through_npm_ci_clean_package(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root)

    ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    assert runner.npm_calls == [list(step) for step in ARTIFACTS.NPM_PACKAGE_SEQUENCE]
    assert runner.npm_calls == [["ci"], ["run", "clean"], ["run", "package"]], (
        "the .vsix must always be built from the checked-out sources against the locked "
        "dependency tree — dropping ci or clean lets a stale bundle ship (`run package` "
        "rebuilds the bundle itself, which is why there is no separate `run build` step)"
    )


def test_a_package_run_that_produces_no_vsix_fails_with_a_named_error(tmp_path: Path) -> None:
    """`npm run package` exiting 0 without writing a .vsix must not pass silently."""
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, package_effect=lambda: None)  # the package step produces nothing

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="produced no .vsix"):
        ARTIFACTS.build_vscode_artifact(repo_root, repo_root / ARTIFACTS.DIST_DIRNAME, run=runner, npm="npm")


def test_two_vsix_files_after_packaging_fail_rather_than_pick_one(tmp_path: Path) -> None:
    """Which of two archives is *the* artifact is not something to guess."""
    repo_root = make_source_repo(tmp_path)
    project_root = repo_root / ARTIFACTS.VSCODE_PROJECT

    def two_vsix_files() -> None:
        for name in (f"swcraft-vscode-{VERSION}.vsix", "swcraft-vscode-other.vsix"):
            (project_root / name).write_text("vsix", encoding="utf-8")

    runner = FakeRunner(repo_root, package_effect=two_vsix_files)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as error:
        ARTIFACTS.build_vscode_artifact(repo_root, repo_root / ARTIFACTS.DIST_DIRNAME, run=runner, npm="npm")

    assert "exactly one .vsix" in str(error.value)
    assert "swcraft-vscode-other.vsix" in str(error.value), "the message must name what it found"


def test_main_reports_an_os_error_in_the_error_shape_rather_than_a_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A full disk or an unwritable dist/ is a failure, not a stack trace."""
    repo_root = make_source_repo(tmp_path)

    def explode(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ARTIFACTS, "build_all", explode)

    assert ARTIFACTS.main(["--repo-root", str(repo_root)]) == 1
    assert "Error: " in capsys.readouterr().err


def test_the_builder_never_passes_shell_to_a_subprocess_call() -> None:
    """The repo-wide no-shell rule (moon ``command:`` and Windows PowerShell 5.x), asserted on the AST.

    AST-based rather than a substring scan, so the module can keep documenting the rule in prose.
    """
    tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(_SCRIPT_PATH))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "shell"
    ]
    assert offenders == [], f"scripts/build_artifacts.py passes shell= at line(s) {offenders}"


def test_stale_dist_from_an_earlier_version_leaves_no_foreign_wheel_in_the_manifest(
    tmp_path: Path,
) -> None:
    repo_root = make_source_repo(tmp_path)
    stale = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.PYTHON_DIRNAME / "swcraft_tools-0.0.9-py3-none-any.whl"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale wheel from an earlier version", encoding="utf-8")
    stale_vsix = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.VSCODE_DIRNAME / "swcraft-vscode-0.0.9.vsix"
    stale_vsix.parent.mkdir(parents=True)
    stale_vsix.write_text("stale vsix", encoding="utf-8")

    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filenames = [entry["filename"] for entry in manifest["artifacts"]]
    assert not any("0.0.9" in filename for filename in filenames), filenames
    assert not stale.exists()
    assert not stale_vsix.exists()


def test_a_stale_vsix_in_the_extension_project_is_removed_before_packaging(tmp_path: Path) -> None:
    # vsce writes the .vsix into the (gitignored) project root; a leftover from an
    # earlier version would otherwise make the "exactly one .vsix" glob ambiguous.
    repo_root = make_source_repo(tmp_path)
    (repo_root / ARTIFACTS.VSCODE_PROJECT / "swcraft-vscode-0.0.9.vsix").write_text("stale", encoding="utf-8")

    manifest_path = ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vsix_entries = [entry for entry in manifest["artifacts"] if entry["kind"] == "vsix"]
    assert [entry["filename"] for entry in vsix_entries] == [f"swcraft-vscode-{VERSION}.vsix"]


def test_a_failing_sub_build_aborts_and_writes_no_manifest(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, fail_on=lambda argv: argv[-1].endswith("mcp"))

    with pytest.raises(subprocess.CalledProcessError):
        ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    assert not (dist_root / ARTIFACTS.MANIFEST_FILENAME).exists()
    assert not (dist_root / ARTIFACTS.SHA256SUMS_FILENAME).exists()
    # The extension is never even reached once a Python build fails.
    assert runner.npm_calls == []


def test_a_failing_npm_step_aborts_and_writes_no_manifest(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    runner = FakeRunner(repo_root, fail_on=lambda argv: argv[:3] == ["npm", "run", "package"])

    with pytest.raises(subprocess.CalledProcessError):
        ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.MANIFEST_FILENAME).exists()


def test_main_reports_a_failing_sub_build_with_a_non_zero_exit(tmp_path, monkeypatch, capsys) -> None:
    repo_root = make_source_repo(tmp_path)

    def failing_build_all(*_args, **_kwargs):
        raise subprocess.CalledProcessError(3, ["python", "-m", "build", "tools"])

    monkeypatch.setattr(ARTIFACTS, "build_all", failing_build_all)

    assert ARTIFACTS.main(["--repo-root", str(repo_root)]) == 3
    assert "build step failed" in capsys.readouterr().err
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.MANIFEST_FILENAME).exists()


def test_main_reports_a_missing_npm_actionably(tmp_path, monkeypatch, capsys) -> None:
    repo_root = make_source_repo(tmp_path)
    monkeypatch.setattr(ARTIFACTS.shutil, "which", lambda _name: None)

    assert ARTIFACTS.main(["--repo-root", str(repo_root)]) == 1
    assert "npm was not found on PATH" in capsys.readouterr().err


def test_main_succeeds_and_prints_the_manifest_path(tmp_path, monkeypatch, capsys) -> None:
    repo_root = make_source_repo(tmp_path)
    monkeypatch.setattr(ARTIFACTS, "build_all", lambda *_a, **_k: repo_root / "dist" / "manifest.json")

    assert ARTIFACTS.main(["--repo-root", str(repo_root)]) == 0
    assert "manifest.json" in capsys.readouterr().out


#: The ``created_at`` every fixture ``PLATFORM.json`` carries, for the same reason:
#: ``wheelhouse_platform()`` stamps the current second, and the archive copies the
#: file in verbatim. ``check_wheelhouse_platform`` compares only ``python_version``
#: and ``platform_tag``, so pinning the third key changes nothing it reads.
_FIXTURE_STAMP_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)


def _write_wheel(path: Path, metadata: str | None = None, files: dict[str, str] | None = None) -> Path:
    """A real (tiny) wheel at *path*: ``<name>-<version>.dist-info/METADATA`` and *files*, by member name.

    ``require_swcraft_wheels`` reads it as a zip and the notices' *Python packages* section reads its
    ``METADATA``. *metadata* defaults to a name and a version and no licence field.
    """
    name, version = path.name.split("-")[:2]
    dist_info = f"{name}-{version}.dist-info"
    if metadata is None:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"{dist_info}/METADATA", date_time=FIXTURE_WHEEL_TIMESTAMP), metadata)
        for member, text in sorted((files or {}).items()):
            archive.writestr(zipfile.ZipInfo(member, date_time=FIXTURE_WHEEL_TIMESTAMP), text)
    return path


_THIRD_PARTY_CLOSURE = (
    "lxml-6.1.3-cp311-cp311-macosx_11_0_arm64.whl",
    "pyyaml-6.0.3-py3-none-any.whl",
)


def _make_workstation_repo(
    tmp_path: Path,
    version: str = VERSION,
    *,
    stamp: dict | None = None,
    third_party: tuple[str, ...] = _THIRD_PARTY_CLOSURE,
    vsix: bool = True,
    wheelhouse: bool = True,
    wheelhouse_swcraft: bool = True,
    runtime: bool = True,
) -> Path:
    """A checkout whose ``dist/`` already holds everything the archive composes.

    The builder downloads and builds nothing: it copies ``scripts/workstation/``, the ``.vsix``, ``dist/wheelhouse/``
    and ``dist/runtime/``'s payload into one zip, so this fixture writes those and no runner is injected in this
    section. The runtime payloads are what :func:`_write_runtime_payload` leaves.
    """
    repo_root = tmp_path / "repo"
    version_module = repo_root / ARTIFACTS.VERSION_MODULE
    version_module.parent.mkdir(parents=True)
    version_module.write_text(f'__version__ = "{version}"\n', encoding="utf-8", newline="\n")

    scripts = repo_root / ARTIFACTS.WORKSTATION_SCRIPTS_DIR
    scripts.mkdir(parents=True)
    (scripts / "README.md").write_text("# SWCraft\n\nUnpacked archive.\n", encoding="utf-8", newline="\n")
    (scripts / "install.py").write_text("print('installer')\n", encoding="utf-8", newline="\n")
    (scripts / "swcraft_install.cmd").write_text("@echo off\npy -3.11 install.py %*\n", encoding="utf-8", newline="\n")
    (scripts / "swcraft_install.sh").write_text(
        '#!/bin/sh\nexec python3.11 install.py "$@"\n', encoding="utf-8", newline="\n"
    )

    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    for distribution in ARTIFACTS.SWCRAFT_DISTRIBUTIONS:
        _write_wheel(dist_root / ARTIFACTS.PYTHON_DIRNAME / f"{distribution}-{version}-py3-none-any.whl")

    (dist_root / ARTIFACTS.VSCODE_DIRNAME).mkdir(parents=True, exist_ok=True)
    if vsix:
        (dist_root / ARTIFACTS.VSCODE_DIRNAME / f"swcraft-vscode-{version}.vsix").write_text(
            "vsix", encoding="utf-8", newline="\n"
        )

    if wheelhouse:
        directory = dist_root / ARTIFACTS.WHEELHOUSE_DIRNAME
        directory.mkdir(parents=True, exist_ok=True)
        if wheelhouse_swcraft:
            for distribution in ARTIFACTS.SWCRAFT_DISTRIBUTIONS:
                _write_wheel(directory / f"{distribution}-{version}-py3-none-any.whl")
        for name in third_party:
            _write_wheel(directory / name)
        if stamp is None:
            stamp = ARTIFACTS.wheelhouse_platform(_FIXTURE_STAMP_MOMENT)
        (directory / ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text(
            json.dumps(stamp, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    write_pin_files(repo_root)
    if runtime:
        _write_runtime_payload(repo_root)
    return repo_root


#: The ``THIRD-PARTY-NOTICES.md`` every workstation fixture carries. The archive builder never opens it either.
_FIXTURE_NOTICES = "# Third-party notices\n"


def _write_runtime_payload(repo_root: Path, token: str | None = None) -> Path:
    """What a successful ``runtime`` stage leaves in ``dist/runtime/`` for *token*; returns it.

    *token* defaults to the running platform's -- which every archive case pins through
    ``mapped_build_host`` -- so the record agrees with the archive being built.
    """
    token = ARTIFACTS.workstation_platform_token() if token is None else token
    directory = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.RUNTIME_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ARTIFACTS.node_payload_filename(token)).write_bytes(fixture_node_body(token))
    (directory / ARTIFACTS.PYTHON_PAYLOAD_FILENAME).write_bytes(fixture_python_body(token))
    # The build-time tree `runtime` unpacks for `wheelhouse`, so a case can show it never travels.
    interpreter = ARTIFACTS.runtime_python(repo_root / ARTIFACTS.DIST_DIRNAME, token)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"the carried interpreter, unpacked at build time\n")
    for ident, _, _ in FIXTURE_EXTENSIONS:
        (directory / ARTIFACTS.extension_payload_filename(ident)).write_bytes(
            fixture_vsix_body(ident, fixture_target(ident, token))
        )
    (directory / ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME).write_text(_FIXTURE_NOTICES, encoding="utf-8", newline="\n")
    record = ARTIFACTS.runtime_record(
        node_version=FIXTURE_NODE_VERSION,
        elkjs_version=FIXTURE_ELKJS_VERSION,
        python_version=FIXTURE_PYTHON_VERSION,
        python_build_tag=FIXTURE_PYTHON_BUILD_TAG,
        # What the fixture PLATFORM.json says too, so the exact wheelhouse check passes.
        python_platform_tag=sysconfig.get_platform(),
        token=token,
        extensions=[
            {"id": ident, "version": version, "target": fixture_target(ident, token)}
            for ident, version, _ in FIXTURE_EXTENSIONS
        ],
        now=_FIXTURE_STAMP_MOMENT,
    )
    (directory / ARTIFACTS.RUNTIME_RECORD_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return directory


def _archive_members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as opened:
        return {name: opened.read(name) for name in opened.namelist()}


@pytest.mark.parametrize(
    ("tag", "token"),
    [
        ("win-amd64", "windows-x64"),
        ("macosx-11.0-arm64", "macos-arm64"),
        ("macosx-14.0-arm64", "macos-arm64"),
        ("linux-x86_64", "linux-x64"),
    ],
)
def test_the_platform_token_maps_the_three_platforms_the_archive_is_built_for(tag: str, token: str) -> None:
    assert ARTIFACTS.workstation_platform_token(tag) == token


@pytest.mark.parametrize(
    "tag",
    ["macosx-10.9-x86_64", "macosx-11.0-universal2", "win32", "win-arm64", "linux-aarch64", "linux-armv7l"],
)
def test_an_unmapped_platform_is_refused_by_name(tag: str) -> None:
    """Refused rather than guessed: the archive's name carries the architecture."""
    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.workstation_platform_token(tag)
    assert tag in str(excinfo.value)


def test_the_default_token_is_the_running_interpreters_platform() -> None:
    """The no-argument call is ``sysconfig.get_platform()``, refusal included.

    Asserted as an equivalence, because this host's tag need not be one the archive is built for: on an Intel Mac or
    an aarch64 Linux both calls refuse. That is why this case alone does not take :func:`mapped_build_host`.
    """
    tag = sysconfig.get_platform()
    try:
        explicit = ARTIFACTS.workstation_platform_token(tag)
    except ARTIFACTS.BuildArtifactsError as refusal:
        with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape(str(refusal))):
            ARTIFACTS.workstation_platform_token()
    else:
        assert ARTIFACTS.workstation_platform_token() == explicit


#: The tag, the pin and the fixture are ``conftest.MAPPED_BUILD_TAG`` / :func:`conftest.mapped_build_host`. Every case
#: here composes a synthetic wheelhouse whose stamp is written by the same patched call, so the two sides of a
#: platform comparison agree by construction.


def test_the_archive_lands_in_dist_workstation_named_for_its_version_and_platform(
    tmp_path: Path, mapped_build_host: str
) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    archive = ARTIFACTS.build_workstation_archive(repo_root)

    assert archive.parent == repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME
    assert archive.name == f"swcraft-{VERSION}-{mapped_build_host}.zip"
    assert zipfile.is_zipfile(archive)


@pytest.mark.usefixtures("mapped_build_host")
def test_the_archive_holds_the_readme_the_installer_the_two_wrappers_the_vsix_and_the_wheelhouse(
    tmp_path: Path,
) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))

    assert set(members) == {
        ARTIFACTS.SHA256SUMS_FILENAME,
        "README.md",
        "install.py",
        "swcraft_install.cmd",
        "swcraft_install.sh",
        f"swcraft-vscode-{VERSION}.vsix",
        f"wheels/{ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME}",
        *(f"wheels/{name}" for name in _THIRD_PARTY_CLOSURE),
        *(f"wheels/{distribution}-{VERSION}-py3-none-any.whl" for distribution in ARTIFACTS.SWCRAFT_DISTRIBUTIONS),
        "runtime/RUNTIME.json",
        "runtime/node.tar.gz",  # the pinned host is macos-arm64, whose publisher file is a .tar.gz
        "runtime/python.tar.gz",
        *(f"extensions/{ident}.vsix" for ident, _, _ in FIXTURE_EXTENSIONS),
        "THIRD-PARTY-NOTICES.md",
    }


@pytest.mark.usefixtures("mapped_build_host")
def test_the_members_are_written_in_sorted_order_and_under_no_root_directory(tmp_path: Path) -> None:
    """Flat by design: ``swcraft_install.cmd`` runs ``install.py`` *beside itself*.

    Three directories and no fourth: ``wheels/``, ``runtime/`` (the Node.js and CPython
    payloads) and ``extensions/`` (the companion editor extensions).
    """
    repo_root = _make_workstation_repo(tmp_path)

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        names = opened.namelist()

    assert names == sorted(names), "the filesystem's directory order must not reach the archive"
    assert {name.split("/")[0] for name in names if "/" in name} == {"wheels", "runtime", "extensions"}
    assert {name.count("/") for name in names if name.startswith("extensions/")} == {1}, "extensions/ is flat"


@pytest.mark.usefixtures("mapped_build_host")
def test_the_checksums_inside_cover_every_other_member(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))
    text = members[ARTIFACTS.SHA256SUMS_FILENAME].decode("utf-8")
    listed = {name: digest for digest, name in (line.split("  ", 1) for line in text.splitlines())}

    assert set(listed) == set(members) - {ARTIFACTS.SHA256SUMS_FILENAME}
    for name, digest in listed.items():
        assert hashlib.sha256(members[name]).hexdigest() == digest, name


@pytest.mark.usefixtures("mapped_build_host")
def test_the_checksums_beside_the_archive_cover_the_zip(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    archive = ARTIFACTS.build_workstation_archive(repo_root)
    beside = (archive.parent / ARTIFACTS.SHA256SUMS_FILENAME).read_text(encoding="utf-8")

    assert beside == f"{ARTIFACTS.sha256_file(archive)}  {archive.name}\n"


@pytest.mark.usefixtures("mapped_build_host")
def test_the_installer_shipped_inside_can_read_the_archive_it_travels_in(tmp_path: Path) -> None:
    """The installer's three archive readers against the builder's writer, on real bytes."""
    install = workstation_install_module()
    repo_root = _make_workstation_repo(tmp_path)
    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))

    entries = install.parse_sha256sums(members[ARTIFACTS.SHA256SUMS_FILENAME].decode("utf-8"))
    install.verify_members(
        entries,
        lambda name: hashlib.sha256(members[name]).hexdigest() if name in members else None,
    )
    assert install.archive_version(members) == VERSION
    install.check_platform_stamp(
        json.loads(members[f"wheels/{ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME}"].decode("utf-8")),
        ARTIFACTS.wheelhouse_platform(),
    )


@pytest.mark.usefixtures("mapped_build_host")
def test_two_builds_from_the_same_inputs_are_byte_identical(tmp_path: Path) -> None:
    """Two checkouts, written at different moments: no mtime may reach the bytes.

    The checkouts are identical by construction only because the fixture pins ``FIXTURE_WHEEL_TIMESTAMP`` and
    ``_FIXTURE_STAMP_MOMENT``; what this asserts is the builder's own contribution -- the member set, their order, and
    the timestamp, mode and create-system every member carries.
    """
    first = ARTIFACTS.build_workstation_archive(_make_workstation_repo(tmp_path / "a"))
    os.utime(_make_workstation_repo(tmp_path / "b") / ARTIFACTS.WORKSTATION_SCRIPTS_DIR / "install.py", (0, 0))
    second = ARTIFACTS.build_workstation_archive(tmp_path / "b" / "repo")

    assert first.read_bytes() == second.read_bytes()


@pytest.mark.usefixtures("mapped_build_host")
def test_a_rebuild_in_the_same_checkout_produces_the_same_bytes(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    before = ARTIFACTS.build_workstation_archive(repo_root).read_bytes()
    after = ARTIFACTS.build_workstation_archive(repo_root).read_bytes()

    assert before == after


@pytest.mark.usefixtures("mapped_build_host")
def test_every_member_pins_its_timestamp_mode_and_create_system(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)
    expected = ARTIFACTS.zip_timestamp(ARTIFACTS.source_date_epoch(repo_root))

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        infos = opened.infolist()

    assert infos, "an empty archive would pass every assertion below vacuously"
    for info in infos:
        assert info.date_time == expected, info.filename
        assert info.create_system == 3, info.filename  # Unix, pinned against the build host
        expected_mode = 0o755 if info.filename.endswith(".sh") else 0o644
        assert (info.external_attr >> 16) & 0o777 == expected_mode, info.filename
        # The file-type bits, not only the permission bits: `ditto -x -k` (Finder's Archive Utility, the default
        # double-click unpack on macOS) reads a zero type nibble as "not a regular file" and falls back to the
        # umask, so `swcraft_install.sh` arrived non-executable.
        # `test_the_shell_wrapper_survives_a_finder_style_unpack` is the end-to-end half.
        assert (info.external_attr >> 16) & 0o170000 == 0o100000, (
            f"{info.filename}: S_IFREG is missing from the external attributes"
        )


@pytest.mark.usefixtures("mapped_build_host")
def test_the_unix_create_system_is_written_and_not_inherited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``create_system`` pin, asserted where it can fail.

    ``zipfile.ZipInfo`` defaults ``create_system`` to ``3`` on every host but ``win32``, so the case above passes with
    the assignment deleted; an archive built on Windows would record ``0``, and an extractor that reads the external
    attributes as a Unix mode only for a Unix creator would drop the wrapper's executable bit. So ``sys.platform`` is
    patched for the build (the platform token comes from ``mapped_build_host``). Not reached: building on Windows.
    """
    repo_root = _make_workstation_repo(tmp_path)
    monkeypatch.setattr(zipfile.sys, "platform", "win32")

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        systems = {info.filename: info.create_system for info in opened.infolist()}

    assert systems, "an empty archive would pass vacuously"
    assert set(systems.values()) == {3}, systems


@pytest.mark.artifact
def test_the_shell_wrapper_survives_a_finder_style_unpack(tmp_path: Path) -> None:
    """The real archive, unpacked the way a macOS user unpacks it, keeps the wrapper executable.

    The guide says to unpack and run ``./swcraft_install.sh``. Finder runs ``ditto -x -k``, which reads a member's
    external attributes as a full ``st_mode`` and, with the file-type nibble missing, restores no permission bits.
    ``unzip`` masks the type off, so this case deliberately does not use ``zipfile.extractall`` (which restores no
    mode at all). ``artifact``-marked because it reads ``dist/workstation/``.

    Not reached: Windows. ``bsdtar`` stands in where ``ditto`` is absent (the Linux CI runner carries
    ``libarchive-tools``); a POSIX host with neither fails rather than skips, since a silent skip is how this defect
    survived. The one skip is probed: ``require_executable_bit`` skips where the executable bit means nothing.
    """
    require_executable_bit(tmp_path)
    archive = require_workstation_archive()
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()

    ditto = shutil.which("ditto")
    bsdtar = shutil.which("bsdtar")
    if ditto:
        argv = [ditto, "-x", "-k", str(archive), str(unpacked)]
    elif bsdtar:
        argv = [bsdtar, "-x", "-f", str(archive), "-C", str(unpacked)]
    else:
        pytest.fail("neither `ditto` nor `bsdtar` is on this host, so the mode-honouring unpack cannot be measured")
    subprocess.run(argv, check=True)

    wrapper = unpacked / ARTIFACTS.WORKSTATION_SHELL_WRAPPER
    assert wrapper.is_file(), sorted(path.name for path in unpacked.iterdir())
    assert os.access(wrapper, os.X_OK), (
        f"`{argv[0]}` restored {oct(wrapper.stat().st_mode & 0o777)} -- "
        "`./swcraft_install.sh` would end in `permission denied`"
    )
    installer = unpacked / "install.py"
    assert installer.is_file()
    assert not os.access(installer, os.X_OK), "only the shell wrapper is executable"


@pytest.mark.artifact
def test_the_real_archive_carries_the_pinned_runtime_payloads_for_its_own_platform() -> None:
    """The real archive against the committed pins, not a fixture's synthetic ones.

    ``artifact``-marked because it reads ``dist/workstation/``. The three runtime members are present and stored, the
    inner ``SHA256SUMS`` covers each, the Node member's digest is the publisher digest committed for this platform
    (still comparable with nodejs.org's ``SHASUMS256.txt``), and ``RUNTIME.json`` names this platform and the
    committed versions, elkjs included although that payload rides in the ``swcraft-tools`` wheel. Not reached:
    whether the payloads run, which is asserted where the installer is.
    """
    archive = require_workstation_archive()
    token = ARTIFACTS.workstation_platform_token()
    node_member = f"runtime/{ARTIFACTS.node_payload_filename(token)}"
    expected = {"runtime/RUNTIME.json", node_member, "runtime/python.tar.gz"}

    with zipfile.ZipFile(archive) as opened:
        runtime = {info.filename: info for info in opened.infolist() if info.filename.startswith("runtime/")}
        assert set(runtime) == expected
        sums = opened.read(ARTIFACTS.SHA256SUMS_FILENAME).decode("utf-8")
        listed = {name: digest for digest, name in (line.split("  ", 1) for line in sums.splitlines())}
        for name in expected:
            assert listed[name] == hashlib.sha256(opened.read(name)).hexdigest(), name
        record = json.loads(opened.read("runtime/RUNTIME.json").decode("utf-8"))
        platform_stamp = json.loads(opened.read("wheels/PLATFORM.json").decode("utf-8"))

    assert {name for name, info in runtime.items() if info.compress_type == zipfile.ZIP_STORED} == expected - {
        "runtime/RUNTIME.json"
    }
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)
    node_version, node_sha256 = ARTIFACTS.node_pin(pins, token)
    python_version, python_build_tag, python_sha256 = ARTIFACTS.python_pin(pins, token)
    assert listed[node_member] == node_sha256
    assert listed["runtime/python.tar.gz"] == python_sha256, (
        "the release's own file, still comparable with its SHA256SUMS"
    )
    assert record["platform_token"] == token
    assert record["node_version"] == node_version
    assert (record["python_version"], record["python_build_tag"]) == (python_version, python_build_tag)
    assert record["elkjs_version"] == pins["elkjs"]["version"]
    assert not [key for key in record if "likec4" in key], record
    assert platform_stamp["platform_tag"] == record["python_platform_tag"], (
        "the wheels were resolved on the carried Python, which stamped PLATFORM.json itself"
    )


@pytest.mark.artifact
def test_the_real_archive_carries_the_pinned_companion_extensions_and_notices_that_rederive_their_licences() -> None:
    """The real archive's ``extensions/`` against the committed pins, and its notices against the packages.

    Each ``.vsix`` is stored, its SHA-256 is the pinned one and the inner ``SHA256SUMS``'s, its own
    ``extension/package.json`` names the pinned version, and ``RUNTIME.json`` records the build each took for this
    platform. Each companion's licence is read out of its package and looked for in its notice section, never compared
    with a transcribed string. Not reached: whether VS Code installs them, and any platform but this one.
    """
    archive = require_workstation_archive()
    token = ARTIFACTS.workstation_platform_token()
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)
    pinned = ARTIFACTS.extension_pins(pins, token)
    expected = {
        f"extensions/{ARTIFACTS.extension_payload_filename(ident)}": entry for entry in pinned for ident in entry[:1]
    }

    with zipfile.ZipFile(archive) as opened:
        infos = {info.filename: info for info in opened.infolist() if info.filename.startswith("extensions/")}
        assert set(infos) == set(expected)
        sums = opened.read(ARTIFACTS.SHA256SUMS_FILENAME).decode("utf-8")
        listed = {name: digest for digest, name in (line.split("  ", 1) for line in sums.splitlines())}
        record = json.loads(opened.read("runtime/RUNTIME.json").decode("utf-8"))
        notices = opened.read(ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME).decode("utf-8")
        assert ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME in listed
        for name, (ident, version, _target, digest) in expected.items():
            data = opened.read(name)
            assert infos[name].compress_type == zipfile.ZIP_STORED, name
            assert listed[name] == digest == hashlib.sha256(data).hexdigest(), name
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                manifest = json.loads(package.read("extension/package.json").decode("utf-8"))
            assert manifest["version"] == version, f"{name} is not the version the pins name"
            section = _notice_section(notices, ident).splitlines()
            assert section[0] == f"### `{ident}` {manifest['version']}"
            assert f"Declared licence: {manifest['license']}" in section

    assert record["platform_token"] == token
    assert record["extensions"] == [
        {"id": ident, "version": version, "target": target} for ident, version, target, _ in pinned
    ]


@pytest.mark.usefixtures("mapped_build_host")
def test_the_batch_wrapper_is_the_one_member_converted_to_windows_line_endings(tmp_path: Path) -> None:
    """The archive is the last moment a ``.cmd`` bound for `cmd.exe` can gain its CRLF."""
    repo_root = _make_workstation_repo(tmp_path)

    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))

    batch = members["swcraft_install.cmd"]
    assert b"\r\n" in batch
    assert batch.replace(b"\r\n", b"").count(b"\n") == 0, "a bare LF is left in the batch file"
    assert b"\r" not in members["swcraft_install.sh"], "a CR would break the shell wrapper's shebang"
    assert b"\r" not in members["install.py"]


@pytest.mark.usefixtures("mapped_build_host")
def test_the_readme_is_a_member_at_the_archives_root(tmp_path: Path) -> None:
    """Flat like every other member: the first file the unpacker sees, not one level down."""
    repo_root = _make_workstation_repo(tmp_path)

    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))

    assert "README.md" in members
    assert members["README.md"].decode("utf-8").startswith("# SWCraft")


def test_the_shipped_scripts_directory_holds_exactly_the_four_files_it_ships() -> None:
    """The real ``scripts/workstation/`` holds exactly the four files the archive ships.

    ``workstation_archive_members`` ships every file in that directory, with no allow-list, and the archive is the one
    file a user with no checkout is handed: anything left beside the installer (a scratch copy, a ``.DS_Store``) would
    travel with every other gate green, since the member-set case runs on a fixture. Not reached: nested directories,
    which ``is_file()`` drops -- widen the builder before widening this set.
    """
    scripts = REPO_ROOT / ARTIFACTS.WORKSTATION_SCRIPTS_DIR
    present = sorted(path.name for path in scripts.iterdir() if path.is_file())

    assert present == ["README.md", "install.py", "swcraft_install.cmd", "swcraft_install.sh"], (
        f"{scripts} holds {present}; every one of those files ships inside the workstation archive. "
        "Remove what does not belong there, or add it here deliberately."
    )


@pytest.mark.usefixtures("mapped_build_host")
def test_the_source_wrappers_are_left_untouched_in_the_checkout(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)
    batch = repo_root / ARTIFACTS.WORKSTATION_SCRIPTS_DIR / "swcraft_install.cmd"
    before = batch.read_bytes()

    ARTIFACTS.build_workstation_archive(repo_root)

    assert batch.read_bytes() == before, "the conversion belongs to the archive, not to the tree"


@pytest.mark.usefixtures("mapped_build_host")
def test_it_empties_dist_workstation_before_writing(tmp_path: Path) -> None:
    """Nothing else would remove a previous version's archive sitting beside the new one."""
    repo_root = _make_workstation_repo(tmp_path)
    workstation = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME
    workstation.mkdir(parents=True)
    stale = workstation / "swcraft-0.0.1-windows-x64.zip"
    stale.write_text("yesterday's release", encoding="utf-8", newline="\n")

    archive = ARTIFACTS.build_workstation_archive(repo_root)

    assert not stale.exists()
    assert sorted(path.name for path in workstation.iterdir()) == sorted([archive.name, ARTIFACTS.SHA256SUMS_FILENAME])


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_dist_python_with_no_wheels_of_the_current_version(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)
    for wheel in (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.PYTHON_DIRNAME).glob("*.whl"):
        wheel.unlink()

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="is missing from"):
        ARTIFACTS.build_workstation_archive(repo_root)


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_wheelhouse_that_was_never_populated(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path, wheelhouse=False)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="--wheelhouse"):
        ARTIFACTS.build_workstation_archive(repo_root)


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_wheelhouse_populated_for_another_platform(tmp_path: Path) -> None:
    foreign = dict(ARTIFACTS.wheelhouse_platform(), platform_tag="win-amd64", python_version="3.9")
    repo_root = _make_workstation_repo(tmp_path, stamp=foreign)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="different environment"):
        ARTIFACTS.build_workstation_archive(repo_root)


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_wheelhouse_that_holds_no_swcraft_wheel(tmp_path: Path) -> None:
    """The second ``require_swcraft_wheels``, over the wheelhouse the installer's ``pip`` resolves against.

    A wheelhouse populated before the version bump, or by a ``--wheelhouse`` run whose copy step failed, carries the
    third-party closure and no SWCraft wheel; the archive built from it would fail on the user's machine with no
    checkout to debug it in.
    """
    repo_root = _make_workstation_repo(tmp_path, wheelhouse_swcraft=False)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    message = str(excinfo.value)
    assert ARTIFACTS.WHEELHOUSE_DIRNAME in message, message
    assert "swcraft-tools" in message or "swcraft_tools" in message, message


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_missing_vsix(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path, vsix=False)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)
    assert ".vsix" in str(excinfo.value)


@pytest.mark.usefixtures("mapped_build_host")
def test_it_refuses_a_wheelhouse_holding_two_versions_of_one_distribution_by_name(tmp_path: Path) -> None:
    """A wheelhouse that accreted versions holds one distribution twice; it is refused by name."""
    repo_root = _make_workstation_repo(
        tmp_path,
        third_party=(
            "lxml-6.1.1-cp311-cp311-macosx_11_0_arm64.whl",
            "lxml-6.1.3-cp311-cp311-macosx_11_0_arm64.whl",
            "pyyaml-6.0.3-py3-none-any.whl",
        ),
    )

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    message = str(excinfo.value)
    assert "lxml" in message and "6.1.1" in message and "6.1.3" in message
    assert "pyyaml" not in message, "a distribution present once is not an offender"


@pytest.mark.usefixtures("mapped_build_host")
def test_a_duplicated_distribution_is_found_whichever_way_its_name_is_cased(tmp_path: Path) -> None:
    """``PyYAML`` and ``pyyaml`` are one distribution (PEP 503), and both spellings ship.

    Not a hypothetical: PyYAML's own wheels carry the project's casing
    (``PyYAML-6.0.1-cp311-...whl``) while pip's newer builds write it lowercase, so
    a directory that has seen both holds one distribution under two names.
    """
    repo_root = _make_workstation_repo(
        tmp_path,
        third_party=("PyYAML-6.0.1-cp311-cp311-macosx_11_0_arm64.whl", "pyyaml-6.0.3-py3-none-any.whl"),
    )

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="pyyaml") as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)
    assert "6.0.1" in str(excinfo.value) and "6.0.3" in str(excinfo.value)


def _wheelhouse_of(repo_root: Path) -> Path:
    return repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WHEELHOUSE_DIRNAME


@pytest.mark.usefixtures("mapped_build_host")
def test_a_stamp_with_retired_bundled_rows_still_composes_and_ships_no_extra_wheel(tmp_path: Path) -> None:
    """``PLATFORM.json`` is read for the platform alone: a ``bundled`` key an older wheelhouse wrote is ignored.

    No wheel is taken on its word; SWCraft's four wheels are required by name instead.
    """
    repo_root = _make_workstation_repo(tmp_path)
    stamp = {
        **ARTIFACTS.wheelhouse_platform(_FIXTURE_STAMP_MOMENT),
        "bundled": [{"distribution": "example-gen", "version": "0.9.0", "sha256": "1" * 64}],
    }
    (_wheelhouse_of(repo_root) / ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text(
        json.dumps(stamp, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        names = opened.namelist()
    assert not [name for name in names if "example_gen" in name]
    assert f"wheels/swcraft_specimens-{VERSION}-py3-none-any.whl" in names


@pytest.mark.usefixtures("mapped_build_host")
def test_a_wheelhouse_without_the_generators_wheel_is_refused_by_name(tmp_path: Path) -> None:
    """A wheelhouse without the specimen generator's wheel is refused by name before anything is written.

    The specimen generator is one of SWCraft's own wheels: an archive without it would install no
    ``swcraft-specimens``.
    """
    repo_root = _make_workstation_repo(tmp_path)
    (_wheelhouse_of(repo_root) / f"swcraft_specimens-{VERSION}-py3-none-any.whl").unlink()

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    assert f"swcraft_specimens-{VERSION}-*.whl is missing" in str(excinfo.value)
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME).exists()


@pytest.mark.usefixtures("mapped_build_host")
def test_a_wheelhouse_holding_an_sdist_is_refused_by_name(tmp_path: Path) -> None:
    """The notices list wheels alone, so an sdist pip fell back to would ship unlisted."""
    repo_root = _make_workstation_repo(tmp_path)
    (_wheelhouse_of(repo_root) / "nowheel-1.0.tar.gz").write_bytes(b"an sdist")

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    assert "nowheel-1.0.tar.gz" in str(excinfo.value) and "not a wheel" in str(excinfo.value)


@pytest.mark.usefixtures("mapped_build_host")
def test_a_refusal_writes_no_archive(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path, vsix=False)

    with pytest.raises(ARTIFACTS.BuildArtifactsError):
        ARTIFACTS.build_workstation_archive(repo_root)

    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME).exists()


@pytest.mark.usefixtures("mapped_build_host")
def test_a_refusal_leaves_the_archive_of_the_previous_run_standing(tmp_path: Path) -> None:
    """The ordering the case above cannot see: refuse **before** the emptying.

    ``build_workstation_archive`` ``rmtree``s ``dist/workstation/`` -- nothing else
    would ever remove a previous version's archive, since it carries no manifest
    entry. On a repo that never had one, moving that ``rmtree`` above the four
    refusals still leaves the directory absent, so the case above passes either
    way. This is the state the ordering was written for: an archive a person could
    reasonably already have handed to a colleague, and a build that then failed.
    """
    repo_root = _make_workstation_repo(tmp_path, vsix=False)
    workstation = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME
    workstation.mkdir(parents=True)
    stale = workstation / f"swcraft-0.0.1-{ARTIFACTS.workstation_platform_token()}.zip"
    stale.write_bytes(b"the archive of the previous run")

    with pytest.raises(ARTIFACTS.BuildArtifactsError):
        ARTIFACTS.build_workstation_archive(repo_root)

    assert stale.read_bytes() == b"the archive of the previous run"


# ---------------------------------------------------------------------------
# The runtime payloads inside the archive
# ---------------------------------------------------------------------------

#: The three runtime members an archive built on the pinned (macos-arm64) host carries.
_RUNTIME_MEMBERS = ("runtime/RUNTIME.json", "runtime/node.tar.gz", "runtime/python.tar.gz")


@pytest.mark.usefixtures("mapped_build_host")
def test_the_runtime_payloads_are_ordinary_0o644_regular_files(tmp_path: Path) -> None:
    """What travelling as single archive files buys: no loose executable, so no mode to lose.

    A loose ``node`` binary would need ``0o755`` and would arrive non-executable under
    Finder's ``ditto`` unpack -- the defect class the shell wrapper already paid for. The
    payloads are containers, so the mode table stays one executable name wide.
    """
    repo_root = _make_workstation_repo(tmp_path)

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        infos = {info.filename: info for info in opened.infolist()}

    for name in _RUNTIME_MEMBERS:
        assert infos[name].external_attr >> 16 == stat.S_IFREG | 0o644, name


@pytest.mark.usefixtures("mapped_build_host")
def test_the_checksums_inside_cover_the_runtime_payloads_and_the_node_digest_is_the_pinned_one(
    tmp_path: Path,
) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    members = _archive_members(ARTIFACTS.build_workstation_archive(repo_root))
    text = members[ARTIFACTS.SHA256SUMS_FILENAME].decode("utf-8")
    listed = {name: digest for digest, name in (line.split("  ", 1) for line in text.splitlines())}

    for name in _RUNTIME_MEMBERS:
        assert listed[name] == hashlib.sha256(members[name]).hexdigest(), name
    pins = ARTIFACTS.read_runtime_pins(repo_root)
    _, pinned = ARTIFACTS.node_pin(pins, "macos-arm64")
    assert listed["runtime/node.tar.gz"] == pinned
    assert members["runtime/node.tar.gz"] == fixture_node_body("macos-arm64"), "the Node file travels byte for byte"
    assert listed["runtime/python.tar.gz"] == ARTIFACTS.python_pin(pins, "macos-arm64")[2]
    assert members["runtime/python.tar.gz"] == fixture_python_body("macos-arm64"), "so does the CPython file"


@pytest.mark.usefixtures("mapped_build_host")
def test_the_runtime_payloads_are_stored_and_every_other_member_is_deflated(tmp_path: Path) -> None:
    """The payloads are already compressed; deflating them buys kilobytes for a recompression."""
    repo_root = _make_workstation_repo(tmp_path)

    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        methods = {info.filename: info.compress_type for info in opened.infolist()}

    stored = {name for name, method in methods.items() if method == zipfile.ZIP_STORED}
    assert stored == {
        "runtime/node.tar.gz",
        "runtime/python.tar.gz",
        *(f"extensions/{ident}.vsix" for ident, _, _ in FIXTURE_EXTENSIONS),
    }
    assert all(method == zipfile.ZIP_DEFLATED for name, method in methods.items() if name not in stored)


@pytest.mark.parametrize(
    ("arcname", "expected"),
    [
        ("runtime/node.tar.xz", zipfile.ZIP_STORED),
        ("runtime/node.tar.gz", zipfile.ZIP_STORED),
        ("runtime/node.zip", zipfile.ZIP_STORED),
        ("runtime/python.tar.gz", zipfile.ZIP_STORED),
        ("runtime/RUNTIME.json", zipfile.ZIP_DEFLATED),
        ("extensions/redhat.vscode-xml.vsix", zipfile.ZIP_STORED),
        ("extensions/notes.txt", zipfile.ZIP_DEFLATED),
        ("THIRD-PARTY-NOTICES.md", zipfile.ZIP_DEFLATED),
        ("swcraft-vscode-9.9.9.vsix", zipfile.ZIP_DEFLATED),
        ("wheels/lxml-6.1.3-cp311-cp311-macosx_11_0_arm64.whl", zipfile.ZIP_DEFLATED),
        ("node.zip", zipfile.ZIP_DEFLATED),
    ],
)
def test_only_a_runtime_container_is_stored(arcname: str, expected: int) -> None:
    """The existing members keep their deflate, so their bytes in the archive do not move."""
    assert ARTIFACTS.workstation_member_compression(arcname) == expected


@pytest.mark.usefixtures("mapped_build_host")
def test_two_builds_from_the_same_inputs_carry_the_runtime_payloads_byte_identically(tmp_path: Path) -> None:
    """The reproducibility cases above cover the payloads only if the payloads are there."""
    first = ARTIFACTS.build_workstation_archive(_make_workstation_repo(tmp_path / "a"))
    second = ARTIFACTS.build_workstation_archive(_make_workstation_repo(tmp_path / "b"))

    with zipfile.ZipFile(first) as opened:
        assert set(_RUNTIME_MEMBERS) <= set(opened.namelist())
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("token", sorted(ARTIFACTS.RUNTIME_PLATFORMS))
def test_every_runtime_member_name_is_one_the_installers_sums_reader_accepts(token: str) -> None:
    """POSIX-spelled, no ``..``, no ``:`` and not absolute, on every platform.

    The names are derived from the builder's own constants rather than restated, so a Node
    payload name that grew a version or a drive-like ``:`` fails here before any archive
    is built for that platform.
    """
    install = workstation_install_module()
    names = [
        f"{ARTIFACTS.WORKSTATION_RUNTIME_DIRNAME}/{filename}"
        for filename in (
            ARTIFACTS.RUNTIME_RECORD_FILENAME,
            ARTIFACTS.node_payload_filename(token),
            ARTIFACTS.PYTHON_PAYLOAD_FILENAME,
        )
    ]
    text = "".join(f"{'0' * 64}  {name}\n" for name in names)

    assert [name for _, name in install.parse_sha256sums(text)] == names
    for name in names:
        assert "\\" not in name and ":" not in name and ".." not in name.split("/"), name
        assert not re.search(r"\d+\.\d+", name), f"{name} carries a version"


def _remove_runtime_directory(repo_root: Path) -> None:
    shutil.rmtree(repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.RUNTIME_DIRNAME)


def _runtime_file(repo_root: Path, name: str) -> Path:
    return repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.RUNTIME_DIRNAME / name


def _rewrite_record(repo_root: Path, **changes: str) -> None:
    path = _runtime_file(repo_root, ARTIFACTS.RUNTIME_RECORD_FILENAME)
    record = dict(json.loads(path.read_text(encoding="utf-8")), **changes)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")


#: A CPython version of the fixture's own minor that is not its pin: stale, yet still the
#: interpreter PLATFORM.json names, so the refusal is the staleness one and not the wheels'.
_STALE_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.0"

#: Each way ``dist/runtime/`` can be unfit to ship, and the words its refusal must carry.
_RUNTIME_REFUSALS = {
    "stale-python": (
        lambda root: _rewrite_record(root, python_version=_STALE_PYTHON_VERSION),
        f"python_version is '{_STALE_PYTHON_VERSION}'",
    ),
    "stale-python-build": (
        lambda root: _rewrite_record(root, python_build_tag="19990101"),
        "python_build_tag is '19990101'",
    ),
    "no-python": (lambda root: _runtime_file(root, "python.tar.gz").unlink(), "python.tar.gz"),
    "python-changed": (
        lambda root: _runtime_file(root, "python.tar.gz").write_bytes(b"a different python archive\n"),
        "changed after `runtime` verified it",
    ),
    "no-interpreter-recorded": (
        lambda root: _rewrite_record(root, python_platform_tag=""),
        "records no Python the archive carries",
    ),
    "never-populated": (_remove_runtime_directory, "dev.py runtime"),
    "no-record": (lambda root: _runtime_file(root, "RUNTIME.json").unlink(), "RUNTIME.json is missing"),
    "unreadable-record": (
        lambda root: _runtime_file(root, "RUNTIME.json").write_text("{", encoding="utf-8", newline="\n"),
        "not readable JSON",
    ),
    "another-platform": (lambda root: _rewrite_record(root, platform_token="windows-x64"), "'windows-x64'"),
    "stale-node": (lambda root: _rewrite_record(root, node_version="0.0.1"), "node_version is '0.0.1'"),
    "stale-elkjs": (lambda root: _rewrite_record(root, elkjs_version="0.0.1"), "elkjs_version is '0.0.1'"),
    "no-elkjs-recorded": (lambda root: _rewrite_record(root, elkjs_version=None), "elkjs_version is None"),
    "no-node": (lambda root: _runtime_file(root, "node.tar.gz").unlink(), "node.tar.gz"),
    "node-changed": (
        lambda root: _runtime_file(root, "node.tar.gz").write_bytes(b"a different node archive\n"),
        "changed after `runtime` verified it",
    ),
    "stale-extension": (
        lambda root: _rewrite_record(
            root,
            extensions=[
                {"id": "example.universal-ext", "version": "0.0.1", "target": "universal"},
                {"id": "example.platform-ext", "version": "2.0.0", "target": "darwin-arm64"},
            ],
        ),
        "committed pins name example.universal-ext 1.0.0 (universal)",
    ),
    "another-platforms-extension-build": (
        lambda root: _rewrite_record(
            root,
            extensions=[
                {"id": "example.universal-ext", "version": "1.0.0", "target": "universal"},
                {"id": "example.platform-ext", "version": "2.0.0", "target": "win32-x64"},
            ],
        ),
        "example.platform-ext 2.0.0 (darwin-arm64) for macos-arm64",
    ),
    "no-extensions-recorded": (lambda root: _rewrite_record(root, extensions=None), "the companion extensions None"),
    "no-extension": (
        lambda root: _runtime_file(root, "example.platform-ext.vsix").unlink(),
        "example.platform-ext.vsix",
    ),
    "extension-changed": (
        lambda root: _runtime_file(root, "example.universal-ext.vsix").write_bytes(b"a different package\n"),
        "changed after `runtime` verified it",
    ),
    "no-notices": (lambda root: _runtime_file(root, "THIRD-PARTY-NOTICES.md").unlink(), "THIRD-PARTY-NOTICES.md"),
}


@pytest.mark.usefixtures("mapped_build_host")
@pytest.mark.parametrize("case", sorted(_RUNTIME_REFUSALS))
def test_an_unfit_runtime_payload_is_refused_by_name_and_writes_no_archive(tmp_path: Path, case: str) -> None:
    mutate, named = _RUNTIME_REFUSALS[case]
    repo_root = _make_workstation_repo(tmp_path)
    mutate(repo_root)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    assert named in str(excinfo.value)
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME).exists()


@pytest.mark.usefixtures("mapped_build_host")
def test_another_platforms_node_beside_this_ones_does_not_travel(tmp_path: Path) -> None:
    """Named members, not a directory listing: a stray file in ``dist/runtime/`` stays there."""
    repo_root = _make_workstation_repo(tmp_path)
    _runtime_file(repo_root, "node.zip").write_bytes(fixture_node_body("windows-x64"))
    _runtime_file(repo_root, "notes.txt").write_text("scratch\n", encoding="utf-8", newline="\n")

    names = set(_archive_members(ARTIFACTS.build_workstation_archive(repo_root)))

    assert {name for name in names if name.startswith("runtime/")} == set(_RUNTIME_MEMBERS)


@pytest.mark.usefixtures("mapped_build_host")
def test_a_likec4_payload_an_earlier_runtime_left_does_not_travel(tmp_path: Path) -> None:
    """A ``dist/runtime/`` staged by an older ``runtime`` still holds ``likec4.tar.xz``; it does not travel.

    Named members keep it out, and its record -- ``likec4_version`` and no ``elkjs_version`` -- is refused as stale
    before anything is written.
    """
    repo_root = _make_workstation_repo(tmp_path)
    _runtime_file(repo_root, "likec4.tar.xz").write_bytes(b"a packed likec4 tree\n")

    names = set(_archive_members(ARTIFACTS.build_workstation_archive(repo_root)))

    assert {name for name in names if name.startswith("runtime/")} == set(_RUNTIME_MEMBERS)
    assert not [name for name in names if "likec4" in name.lower()]

    record_path = _runtime_file(repo_root, ARTIFACTS.RUNTIME_RECORD_FILENAME)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    del record["elkjs_version"]
    record["likec4_version"] = "1.59.3"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="elkjs_version is None"):
        ARTIFACTS.build_workstation_archive(repo_root)


@pytest.mark.usefixtures("mapped_build_host")
def test_the_build_time_python_tree_is_never_an_archive_member(tmp_path: Path) -> None:
    """``dist/runtime/python/`` is where ``wheelhouse`` resolves; the member is the vendor's tarball.

    The fixture writes the unpacked interpreter beside the payloads, exactly where
    ``runtime`` leaves it, so a builder that listed the directory would ship it.
    """
    repo_root = _make_workstation_repo(tmp_path)
    assert ARTIFACTS.runtime_python(repo_root / "dist", "macos-arm64").is_file()

    names = set(_archive_members(ARTIFACTS.build_workstation_archive(repo_root)))

    assert not any(name.startswith("runtime/python/") for name in names)
    assert "runtime/python.tar.gz" in names


@pytest.mark.usefixtures("mapped_build_host")
def test_a_wheelhouse_resolved_for_another_deployment_target_is_refused_by_the_archive(tmp_path: Path) -> None:
    """The archive compares ``PLATFORM.json`` with the carried Python **exactly**.

    A closure resolved by the build host's interpreter (``macosx-26.0-arm64``, measured) can
    hold a wheel the carried Python (``macosx-11.0-arm64``) cannot install, although the two
    agree on family and architecture -- which is all the clean-install check compares.
    """
    stamp = dict(ARTIFACTS.wheelhouse_platform(_FIXTURE_STAMP_MOMENT), platform_tag="macosx-26.0-arm64")
    repo_root = _make_workstation_repo(tmp_path, stamp=stamp)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_workstation_archive(repo_root)

    message = str(excinfo.value)
    assert "different environment" in message and "the Python the archive carries" in message
    assert "macosx-26.0-arm64" in message and "macosx-11.0-arm64" in message
    assert not (repo_root / "dist" / "workstation").exists()


@pytest.mark.usefixtures("mapped_build_host")
def test_an_absent_runtime_payload_leaves_the_archive_of_the_previous_run_standing(tmp_path: Path) -> None:
    """The runtime refusal sits with the six wheel refusals, before ``dist/workstation/`` is emptied."""
    repo_root = _make_workstation_repo(tmp_path, runtime=False)
    workstation = repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME
    workstation.mkdir(parents=True)
    stale = workstation / f"swcraft-0.0.1-{ARTIFACTS.workstation_platform_token()}.zip"
    stale.write_bytes(b"the archive of the previous run")

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="dev.py runtime"):
        ARTIFACTS.build_workstation_archive(repo_root)

    assert stale.read_bytes() == b"the archive of the previous run"


def test_the_archive_is_not_an_artifact_the_manifest_can_record() -> None:
    """It is provisioning, like the wheelhouse it wraps, and carries no manifest entry."""
    assert ARTIFACTS.WORKSTATION_DIRNAME not in set(ARTIFACTS.ARTIFACT_DIRECTORIES.values())
    with pytest.raises(ValueError, match="unrecognized artifact filename"):
        ARTIFACTS.artifact_kind(f"swcraft-{VERSION}-windows-x64.zip")


@pytest.mark.usefixtures("mapped_build_host")
def test_building_the_archive_leaves_the_manifest_and_its_sums_untouched(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)
    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    (dist_root / ARTIFACTS.MANIFEST_FILENAME).write_text('{"artifacts": []}\n', encoding="utf-8", newline="\n")
    (dist_root / ARTIFACTS.SHA256SUMS_FILENAME).write_text("", encoding="utf-8", newline="\n")

    ARTIFACTS.build_workstation_archive(repo_root)

    assert (dist_root / ARTIFACTS.MANIFEST_FILENAME).read_text(encoding="utf-8") == '{"artifacts": []}\n'
    assert (dist_root / ARTIFACTS.SHA256SUMS_FILENAME).read_text(encoding="utf-8") == ""


def test_main_workstation_archive_flag_builds_and_reports(tmp_path, capsys, mapped_build_host: str) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    assert ARTIFACTS.main(["--repo-root", str(repo_root), "--workstation-archive"]) == 0

    out = capsys.readouterr().out
    assert f"swcraft-{VERSION}-{mapped_build_host}.zip" in out


@pytest.mark.parametrize("other", ["--wheelhouse", "--docs-only"])
def test_main_refuses_the_workstation_flag_beside_another_mode(tmp_path, capsys, other: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        ARTIFACTS.main(["--repo-root", str(tmp_path), "--workstation-archive", other])

    assert excinfo.value.code == 2
    assert "at most one" in capsys.readouterr().err


@pytest.mark.usefixtures("mapped_build_host")
def test_main_reports_a_workstation_refusal_in_the_error_shape(tmp_path, capsys) -> None:
    repo_root = _make_workstation_repo(tmp_path, vsix=False)

    assert ARTIFACTS.main(["--repo-root", str(repo_root), "--workstation-archive"]) == 1
    assert capsys.readouterr().err.startswith("Error: ")


#: The epoch every hermetic pack in this section is stamped with.
_RUNTIME_EPOCH = "1700000000"


def _marketplace_body(url: str) -> bytes:
    """What the fake Marketplace serves at a ``vspackage`` URL: the package for that id and target, gzip-encoded."""
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.path.split("/")
    ident = f"{parts[parts.index('publishers') + 1]}.{parts[parts.index('vsextensions') + 1]}"
    target = urllib.parse.parse_qs(parsed.query).get("targetPlatform", [ARTIFACTS.UNIVERSAL_TARGET])[0]
    return gzip.compress(fixture_vsix_body(ident, target), mtime=0)


class _FakeProbe:
    """Stands in for asking the unpacked interpreter what it is; records what was asked, and whether it was there."""

    def __init__(self, version: str = FIXTURE_PYTHON_VERSION, platform_tag: str = "macosx-11.0-arm64") -> None:
        self.version = version
        self.platform_tag = platform_tag
        self.asked: list[Path] = []
        self.present: list[bool] = []

    def __call__(self, interpreter: Path) -> dict[str, str]:
        self.asked.append(Path(interpreter))
        self.present.append(Path(interpreter).is_file())
        return {"python_version": self.version, "platform_tag": self.platform_tag}


def _make_runtime_repo(tmp_path: Path, *, node_version: str = FIXTURE_NODE_VERSION) -> Path:
    """A checkout holding a pins file for fake payload bodies."""
    repo_root = tmp_path / "repo"
    write_pin_files(repo_root, node_version=node_version)
    return repo_root


class _RuntimeFetcher:
    """Serves Node for nodejs.org, CPython for the release, each package for the Marketplace and elkjs for npm.

    *vsix* replaces what the Marketplace serves, for a case that needs a package other than
    the one pinned; *elkjs* what the npm registry serves. Records the URLs.
    """

    def __init__(
        self,
        body: bytes,
        python_body: bytes = b"no CPython body was given to this fetcher\n",
        vsix: bytes | None = None,
        elkjs: bytes | None = None,
    ) -> None:
        self.body = body
        self.python_body = python_body
        self.vsix = vsix
        self.elkjs = elkjs
        self.urls: list[str] = []

    def __call__(self, url: str, destination: Path) -> None:
        self.urls.append(url)
        if url.startswith(ARTIFACTS.NPM_REGISTRY_URL):
            destination.write_bytes(fixture_elkjs_body() if self.elkjs is None else self.elkjs)
        elif url.startswith(ARTIFACTS.MARKETPLACE_GALLERY_URL):
            destination.write_bytes(_marketplace_body(url) if self.vsix is None else self.vsix)
        elif url.startswith(ARTIFACTS.PYTHON_RELEASE_URL):
            destination.write_bytes(self.python_body)
        else:
            destination.write_bytes(self.body)


def _build_runtime(repo_root: Path, token: str = "macos-arm64", **overrides) -> Path:
    arguments = {
        "token": token,
        "fetch": _RuntimeFetcher(fixture_node_body(token), fixture_python_body(token)),
        "probe": _FakeProbe(),
        "now": _FIXTURE_STAMP_MOMENT,
    }
    arguments.update(overrides)
    return ARTIFACTS.build_runtime_payload(repo_root, **arguments)


#: What a macOS ``runtime`` build leaves in ``dist/runtime/``, sorted as ``iterdir`` names sort.
_RUNTIME_DIRECTORY_LISTING = sorted(
    [
        "RUNTIME.json",
        "THIRD-PARTY-NOTICES.md",
        "node.tar.gz",
        "python",
        "python.tar.gz",
        *(f"{ident}.vsix" for ident, _, _ in FIXTURE_EXTENSIONS),
    ]
)


def _directory_bytes(directory: Path) -> dict[str, bytes]:
    """Every file under *directory*, by POSIX path relative to it -- the unpacked ``python/`` tree included."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def test_the_runtime_platform_table_maps_exactly_the_archives_three_tokens() -> None:
    assert sorted(ARTIFACTS.RUNTIME_PLATFORMS) == ARCHIVE_TOKENS


@pytest.mark.parametrize(
    ("token", "vendor_file", "payload"),
    [
        ("linux-x64", "node-v1.2.3-linux-x64.tar.xz", "node.tar.xz"),
        ("windows-x64", "node-v1.2.3-win-x64.zip", "node.zip"),
        ("macos-arm64", "node-v1.2.3-darwin-arm64.tar.gz", "node.tar.gz"),
    ],
)
def test_each_platform_row_spells_the_publishers_file(token: str, vendor_file: str, payload: str) -> None:
    """The platform table row by row, with the publisher's measured extensions, and no npm keys.

    npm's ``--os`` / ``--cpu`` spelled the platform for staging a tree the archive no longer carries.
    """
    assert ARTIFACTS.node_vendor_filename("1.2.3", token) == vendor_file
    assert ARTIFACTS.node_download_url("1.2.3", token) == f"https://nodejs.org/dist/v1.2.3/{vendor_file}"
    assert ARTIFACTS.node_payload_filename(token) == payload
    assert not [key for key in ARTIFACTS.RUNTIME_PLATFORMS[token] if key.startswith("npm")]


@pytest.mark.parametrize(
    ("token", "triple", "interpreter"),
    [
        ("linux-x64", "x86_64-unknown-linux-gnu", "bin/python3"),
        ("windows-x64", "x86_64-pc-windows-msvc", "python.exe"),
        ("macos-arm64", "aarch64-apple-darwin", "bin/python3"),
    ],
)
def test_each_platform_row_spells_the_python_release_file_and_where_its_interpreter_sits(
    token: str, triple: str, interpreter: str
) -> None:
    """The release's own asset name (a ``.tar.gz`` on all three, measured) and a version-free interpreter."""
    vendor_file = f"cpython-1.2.3+20000101-{triple}-install_only.tar.gz"
    assert ARTIFACTS.python_vendor_filename("1.2.3", "20000101", token) == vendor_file
    assert ARTIFACTS.python_download_url("1.2.3", "20000101", token) == (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20000101/"
        f"cpython-1.2.3%2B20000101-{triple}-install_only.tar.gz"
    )
    assert ARTIFACTS.runtime_python(Path("dist"), token) == Path("dist", "runtime", "python", *interpreter.split("/"))


@pytest.mark.parametrize(
    ("token", "vscode_target", "licence"),
    [
        ("linux-x64", "linux-x64", "lib/python3.11/LICENSE.txt"),
        ("windows-x64", "win32-x64", "LICENSE.txt"),
        ("macos-arm64", "darwin-arm64", "lib/python3.11/LICENSE.txt"),
    ],
)
def test_each_platform_row_names_its_marketplace_target_and_where_cpython_keeps_its_licence(
    token: str, vscode_target: str, licence: str
) -> None:
    """The fifth vocabulary (the Marketplace's ``targetPlatform``) and the licence path, measured on all three trees."""
    row = ARTIFACTS.RUNTIME_PLATFORMS[token]
    assert row["vscode_target"] == vscode_target
    assert row["python_licence"].format(minor="3.11") == licence


@pytest.mark.parametrize("token", ["macos-x64", "linux-arm64", "win-x64", "darwin-arm64", ""])
def test_an_unmapped_runtime_token_is_refused_by_name(token: str) -> None:
    for resolve in (ARTIFACTS.runtime_platform, ARTIFACTS.node_payload_filename):
        with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
            resolve(token)
        assert repr(token) in str(excinfo.value)


def test_the_committed_pins_pin_node_for_exactly_the_three_tokens() -> None:
    """Every key present for every platform the archive is built for."""
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)

    assert sorted(pins["node"]["platforms"]) == ARCHIVE_TOKENS
    digests = set()
    for token in ARCHIVE_TOKENS:
        version, digest = ARTIFACTS.node_pin(pins, token)
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        digests.add(digest)
    assert len(digests) == len(ARCHIVE_TOKENS), "three platforms are three different vendor files"


def test_the_committed_pins_pin_the_carried_python_for_exactly_the_three_tokens() -> None:
    """One release tag and version for all three, a digest each, and the language version the wheels bind."""
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)

    assert sorted(pins["python"]["platforms"]) == ARCHIVE_TOKENS
    digests = set()
    for token in ARCHIVE_TOKENS:
        version, build_tag, digest = ARTIFACTS.python_pin(pins, token)
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        assert re.fullmatch(r"\d{8}", build_tag)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        digests.add(digest)
        assert tuple(int(part) for part in version.split(".")[:2]) == workstation_install_module().REQUIRED_PYTHON, (
            "the wheelhouse's cp311 wheels bind the minor version the installer requires"
        )
    assert len(digests) == len(ARCHIVE_TOKENS), "three platforms are three different release files"


def test_the_likec4_payload_is_gone_from_the_builder_the_pins_and_the_checkout() -> None:
    """No ``likec4`` constant or function, no lockfile directory, no pins entry, no npm in ``runtime``."""
    assert [name for name in vars(ARTIFACTS) if "likec4" in name.lower()] == []
    assert not (REPO_ROOT / "scripts" / "workstation" / "likec4").exists()
    pins_text = (REPO_ROOT / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8")
    assert "likec4" not in pins_text.lower()
    assert "likec4" not in ARTIFACTS.RUNTIME_PINS_DESCRIPTION.lower()
    parameters = inspect.signature(ARTIFACTS.build_runtime_payload).parameters
    assert "npm" not in parameters and "run" not in parameters, "the runtime stage downloads files and packs nothing"


def test_the_pin_files_live_in_subdirectories_the_archive_does_not_ship(tmp_path: Path) -> None:
    """The four-file guard above still holds although ``scripts/workstation/`` holds a directory.

    :func:`workstation_archive_members` ships every file directly in that directory and skips directories, so the
    pins never reach a user -- asserted against a copy of the real directory. A copy, because composing members also
    checks ``dist/runtime/`` against the pins and no synthetic body matches a real publisher digest: the copy's pins
    file alone is rewritten, while every shipped file stays the repository's own bytes.
    """
    scripts = REPO_ROOT / ARTIFACTS.WORKSTATION_SCRIPTS_DIR
    directories = sorted(path.name for path in scripts.iterdir() if path.is_dir() and path.name != "__pycache__")
    assert directories == ["pins"]
    assert (REPO_ROOT / ARTIFACTS.RUNTIME_PINS_FILE).parent.name == "pins"

    repo_root = tmp_path / "repo"
    shutil.copytree(
        scripts, repo_root / ARTIFACTS.WORKSTATION_SCRIPTS_DIR, ignore=shutil.ignore_patterns("__pycache__")
    )
    write_pin_files(repo_root)
    _write_runtime_payload(repo_root, token="macos-arm64")
    dist_root = repo_root / ARTIFACTS.DIST_DIRNAME
    (dist_root / ARTIFACTS.WHEELHOUSE_DIRNAME).mkdir(parents=True)
    vsix = tmp_path / f"swcraft-vscode-{VERSION}.vsix"
    vsix.write_bytes(b"vsix")
    from_runtime = {arcname for arcname, _ in ARTIFACTS.runtime_archive_members(repo_root, dist_root, "macos-arm64")}
    members = sorted(
        arcname
        for arcname, _ in ARTIFACTS.workstation_archive_members(repo_root, dist_root, vsix, "macos-arm64")
        if arcname not in from_runtime
    )

    assert members == sorted(["README.md", "install.py", "swcraft_install.cmd", "swcraft_install.sh", vsix.name])


def test_a_runtime_build_stages_the_node_file_byte_for_byte_and_the_record_and_no_likec4(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    fetch = _RuntimeFetcher(fixture_node_body("macos-arm64"), fixture_python_body("macos-arm64"))

    runtime = _build_runtime(repo_root, fetch=fetch)

    assert runtime == repo_root / "dist" / "runtime"
    assert sorted(path.name for path in runtime.iterdir()) == _RUNTIME_DIRECTORY_LISTING
    assert (runtime / "node.tar.gz").read_bytes() == fixture_node_body("macos-arm64")
    assert fetch.urls == [
        "https://registry.npmjs.org/elkjs/-/elkjs-0.9.9.tgz",
        "https://nodejs.org/dist/v1.2.3/node-v1.2.3-darwin-arm64.tar.gz",
        ARTIFACTS.python_download_url(FIXTURE_PYTHON_VERSION, FIXTURE_PYTHON_BUILD_TAG, "macos-arm64"),
        f"{ARTIFACTS.MARKETPLACE_GALLERY_URL}/publishers/example/vsextensions/universal-ext/1.0.0/vspackage",
        f"{ARTIFACTS.MARKETPLACE_GALLERY_URL}/publishers/example/vsextensions/platform-ext/2.0.0/vspackage"
        "?targetPlatform=darwin-arm64",
    ]
    assert not [path.name for path in runtime.rglob("*") if "likec4" in path.name.lower()]
    assert not (repo_root / "dist" / "runtime.new").exists()
    assert not (repo_root / "dist" / "runtime.previous").exists()


def test_the_record_names_what_was_used_and_agrees_with_both_pins(tmp_path: Path) -> None:
    """The pins say what should be fetched, ``RUNTIME.json`` what was -- and they agree."""
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(repo_root, token="linux-x64")

    record = json.loads((runtime / "RUNTIME.json").read_text(encoding="utf-8"))
    assert record == {
        "node_version": "1.2.3",
        "elkjs_version": FIXTURE_ELKJS_VERSION,
        "python_version": FIXTURE_PYTHON_VERSION,
        "python_build_tag": FIXTURE_PYTHON_BUILD_TAG,
        "python_triple": "x86_64-unknown-linux-gnu",
        "python_platform_tag": "macosx-11.0-arm64",
        "extensions": [
            {"id": "example.universal-ext", "version": "1.0.0", "target": "universal"},
            {"id": "example.platform-ext", "version": "2.0.0", "target": "linux-x64"},
        ],
        "platform_tag": sysconfig.get_platform(),
        "platform_token": "linux-x64",
        "created_at": ARTIFACTS.utc_timestamp(_FIXTURE_STAMP_MOMENT),
    }
    pins = ARTIFACTS.read_runtime_pins(repo_root)
    assert record["elkjs_version"] == pins["elkjs"]["version"], "the elkjs payload the same run staged"


def test_a_runtime_build_needs_no_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage downloads files and packs nothing, so no npm is asked for."""
    repo_root = _make_runtime_repo(tmp_path)
    monkeypatch.setattr(ARTIFACTS.shutil, "which", lambda *_args, **_kwargs: None)

    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("the runtime stage started a process")

    monkeypatch.setattr(ARTIFACTS.subprocess, "run", no_subprocess)

    runtime = _build_runtime(repo_root)

    assert sorted(path.name for path in runtime.iterdir()) == _RUNTIME_DIRECTORY_LISTING


def test_a_node_download_whose_digest_does_not_match_is_refused_and_leaves_dist_runtime_untouched(
    tmp_path: Path,
) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    previous = repo_root / "dist" / "runtime"
    previous.mkdir(parents=True)
    (previous / "RUNTIME.json").write_bytes(b"the previous payload's record")
    before = _directory_bytes(previous)
    fetch = _RuntimeFetcher(b"not the publisher's bytes")

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(repo_root, fetch=fetch)

    message = str(excinfo.value)
    assert "node-v1.2.3-darwin-arm64.tar.gz" in message
    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in message
    assert hashlib.sha256(fixture_node_body("macos-arm64")).hexdigest() in message
    assert hashlib.sha256(b"not the publisher's bytes").hexdigest() in message
    assert len(fetch.urls) == 2, "the elkjs tarball, then the refused Node file -- and nothing after it"
    assert _directory_bytes(previous) == before
    assert not (repo_root / "dist" / "runtime.new").exists()


def test_a_rebuild_replaces_the_previous_payload_whole(tmp_path: Path) -> None:
    """A Node file of another platform left by an earlier run does not survive beside this one's."""
    repo_root = _make_runtime_repo(tmp_path)
    previous = repo_root / "dist" / "runtime"
    previous.mkdir(parents=True)
    (previous / "node.zip").write_bytes(b"another platform's node")

    runtime = _build_runtime(repo_root)

    assert sorted(path.name for path in runtime.iterdir()) == _RUNTIME_DIRECTORY_LISTING


def test_a_staging_directory_left_by_a_crashed_run_ships_none_of_its_files(tmp_path: Path) -> None:
    """``dist/runtime.new/`` from a run that died is emptied before this run stages into it."""
    repo_root = _make_runtime_repo(tmp_path)
    leftover = repo_root / "dist" / "runtime.new"
    (leftover / "python").mkdir(parents=True)
    (leftover / "node.zip").write_bytes(b"another platform's node, from a run that died")
    (leftover / "python" / "stray.py").write_bytes(b"left behind in the interpreter tree")

    runtime = _build_runtime(repo_root)

    assert sorted(path.name for path in runtime.iterdir()) == _RUNTIME_DIRECTORY_LISTING
    assert not (runtime / "python" / "stray.py").exists()
    assert not leftover.exists()


def _live_and_staged(tmp_path: Path) -> tuple[Path, Path]:
    """A previous ``dist/runtime/`` and a complete ``dist/runtime.new/`` beside it."""
    live = tmp_path / "dist" / "runtime"
    live.mkdir(parents=True)
    (live / "RUNTIME.json").write_bytes(b"the previous payload")
    staging = tmp_path / "dist" / "runtime.new"
    staging.mkdir()
    (staging / "RUNTIME.json").write_bytes(b"the staged payload")
    return live, staging


def _refuse_renames(monkeypatch: pytest.MonkeyPatch, *refused: tuple[str, str]) -> None:
    """``Path.replace`` raises for each ``(source name, target name)`` in *refused*, on every retry, with no wait."""
    real = Path.replace

    def replace(self: Path, target):
        if (self.name, Path(target).name) in refused:
            raise PermissionError(13, "refused by the test", str(self))
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(ARTIFACTS, "GUIDE_TREE_RETRY_WAITS", (0,))


_ASIDE = ("runtime", "runtime.previous")
_INTO_PLACE = ("runtime.new", "runtime")
_PUT_BACK = ("runtime.previous", "runtime")


def test_a_previous_payload_that_cannot_be_moved_aside_is_left_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, staging = _live_and_staged(tmp_path)
    _refuse_renames(monkeypatch, _ASIDE)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._swap_in_staged_directory(staging, live, "runtime payload")

    message = str(excinfo.value)
    assert message.startswith(f"could not move the previous runtime payload aside from {live}: ")
    assert message.endswith("it is unchanged and the staged one was discarded")
    assert (live / "RUNTIME.json").read_bytes() == b"the previous payload"
    assert not staging.exists()
    assert not live.with_name("runtime.previous").exists()


def test_a_staged_payload_that_cannot_take_its_place_puts_the_previous_one_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, staging = _live_and_staged(tmp_path)
    _refuse_renames(monkeypatch, _INTO_PLACE)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._swap_in_staged_directory(staging, live, "runtime payload")

    message = str(excinfo.value)
    assert message.startswith(f"could not move the staged runtime payload into {live}: ")
    assert message.endswith(f"the previous one is unchanged at {live}")
    assert (live / "RUNTIME.json").read_bytes() == b"the previous payload"
    assert not staging.exists()
    assert not live.with_name("runtime.previous").exists()


def test_a_previous_payload_that_cannot_be_put_back_is_named_where_it_was_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, staging = _live_and_staged(tmp_path)
    _refuse_renames(monkeypatch, _INTO_PLACE, _PUT_BACK)
    superseded = live.with_name("runtime.previous")

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._swap_in_staged_directory(staging, live, "runtime payload")

    assert str(excinfo.value).endswith(f"the previous one could not be put back and is at {superseded}")
    assert (superseded / "RUNTIME.json").read_bytes() == b"the previous payload"
    assert not live.exists()
    assert not staging.exists()


def test_a_runtime_build_whose_swap_fails_leaves_the_previous_payload_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staged-swap promise of :func:`build_runtime_payload`, reached where it can fail: at the swap itself."""
    repo_root = _make_runtime_repo(tmp_path)
    previous = repo_root / "dist" / "runtime"
    previous.mkdir(parents=True)
    (previous / "RUNTIME.json").write_bytes(b"the previous payload's record")
    before = _directory_bytes(previous)
    _refuse_renames(monkeypatch, _INTO_PLACE)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not move the staged runtime payload"):
        _build_runtime(repo_root)

    assert _directory_bytes(previous) == before
    assert not (repo_root / "dist" / "runtime.new").exists()


def test_the_carried_python_is_fetched_verified_stored_unpacked_and_asked(tmp_path: Path) -> None:
    """The release's own file byte for byte, and a version-free interpreter ``wheelhouse`` can run."""
    repo_root = _make_runtime_repo(tmp_path)
    probe = _FakeProbe(platform_tag="linux-x86_64")

    runtime = _build_runtime(repo_root, token="linux-x64", probe=probe)

    assert (runtime / "python.tar.gz").read_bytes() == fixture_python_body("linux-x64")
    interpreter = runtime / "python" / "bin" / "python3"
    assert interpreter.read_bytes() == b"the interpreter for linux-x64\n"
    assert not (runtime / "python" / "python").exists(), "the release's leading python/ is stripped"
    assert probe.asked == [repo_root / "dist" / "runtime.new" / "python" / "bin" / "python3"]
    assert probe.present == [True], "the interpreter is asked once it is on disk, before the swap"
    record = json.loads((runtime / "RUNTIME.json").read_text(encoding="utf-8"))
    assert record["python_platform_tag"] == "linux-x86_64", "what the carried interpreter reported, not the host"
    assert ARTIFACTS.runtime_python(repo_root / "dist", "linux-x64") == interpreter


def test_a_python_download_whose_digest_does_not_match_is_refused_and_leaves_dist_runtime_untouched(
    tmp_path: Path,
) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    previous = repo_root / "dist" / "runtime"
    previous.mkdir(parents=True)
    (previous / "RUNTIME.json").write_bytes(b"the previous payload's record")
    before = _directory_bytes(previous)
    fetch = _RuntimeFetcher(fixture_node_body("macos-arm64"), b"not the release's bytes")
    probe = _FakeProbe()

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(repo_root, fetch=fetch, probe=probe)

    message = str(excinfo.value)
    assert ARTIFACTS.python_vendor_filename(FIXTURE_PYTHON_VERSION, FIXTURE_PYTHON_BUILD_TAG, "macos-arm64") in message
    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in message
    assert hashlib.sha256(fixture_python_body("macos-arm64")).hexdigest() in message
    assert hashlib.sha256(b"not the release's bytes").hexdigest() in message
    assert probe.asked == [], "nothing is unpacked or asked after a refused download"
    assert len(fetch.urls) == 3, "no companion is fetched after a refused download"
    assert _directory_bytes(previous) == before
    assert not (repo_root / "dist" / "runtime.new").exists()


def test_an_interpreter_that_reports_another_version_than_the_pin_is_refused(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(repo_root, probe=_FakeProbe(version="3.0.0"))

    assert "reports Python 3.0.0" in str(excinfo.value)
    assert FIXTURE_PYTHON_VERSION in str(excinfo.value)
    assert not (repo_root / "dist" / "runtime").exists()
    assert not (repo_root / "dist" / "runtime.new").exists()


def _write_python_tarball(path: Path, entries: list[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w:gz") as bundle:
        for info in entries:
            bundle.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return path


def _tar_file(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 1
    return info


def test_a_python_tarball_with_a_member_outside_its_leading_directory_is_refused(tmp_path: Path) -> None:
    archive = _write_python_tarball(tmp_path / "python.tar.gz", [_tar_file("python/bin/python3"), _tar_file("etc/x")])

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.unpack_python_payload(archive, tmp_path / "python")

    assert "'etc/x'" in str(excinfo.value)


def test_a_link_out_of_the_python_tree_is_refused(tmp_path: Path) -> None:
    """The ``data`` filter: no link may point outside the directory it is unpacked into."""
    link = tarfile.TarInfo("python/bin/escape")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    archive = _write_python_tarball(tmp_path / "python.tar.gz", [_tar_file("python/bin/python3"), link])

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not be unpacked"):
        ARTIFACTS.unpack_python_payload(archive, tmp_path / "python")


def test_a_member_that_climbs_out_through_its_leading_directory_is_refused(tmp_path: Path) -> None:
    """``python/../../x`` passes the leading-directory check, and the stripped ``../../x`` is the filter's to refuse."""
    archive = _write_python_tarball(
        tmp_path / "python.tar.gz", [_tar_file("python/bin/python3"), _tar_file("python/../../escaped")]
    )

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="could not be unpacked"):
        ARTIFACTS.unpack_python_payload(archive, tmp_path / "out" / "python")

    assert not (tmp_path / "escaped").exists()


def test_a_hard_link_is_rewritten_under_the_stripped_leading_directory(tmp_path: Path) -> None:
    """A hard link names its target from the archive's root, so the strip that renames members must rename it too."""
    link = tarfile.TarInfo("python/bin/python3")
    link.type, link.linkname = tarfile.LNKTYPE, "python/bin/python3.11"
    archive = _write_python_tarball(tmp_path / "python.tar.gz", [_tar_file("python/bin/python3.11"), link])

    unpacked = ARTIFACTS.unpack_python_payload(archive, tmp_path / "python")

    assert (unpacked / "bin" / "python3").read_bytes() == b"x"
    assert (unpacked / "bin" / "python3.11").read_bytes() == b"x"
    assert not (unpacked / "python").exists()


def test_a_relative_link_inside_the_python_tree_survives_the_unpack(tmp_path: Path) -> None:
    """Every real ``install_only`` build carries ``bin/python3 -> python3.11``; the filter must keep it a link."""
    skip_without_symlink_support(tmp_path)
    link = tarfile.TarInfo("python/bin/python3")
    link.type, link.linkname = tarfile.SYMTYPE, "python3.11"
    archive = _write_python_tarball(tmp_path / "python.tar.gz", [_tar_file("python/bin/python3.11"), link])

    unpacked = ARTIFACTS.unpack_python_payload(archive, tmp_path / "python")

    interpreter = unpacked / "bin" / "python3"
    assert interpreter.is_symlink()
    assert os.readlink(interpreter) == "python3.11"
    assert interpreter.read_bytes() == b"x"


def test_the_wheelhouse_is_resolved_on_the_carried_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped_build_host: str
) -> None:
    """``--wheelhouse`` hands the carried interpreter from ``runtime`` to ``pip download``."""
    repo_root = tmp_path / "repo"
    interpreter = ARTIFACTS.runtime_python(repo_root.resolve() / "dist", mapped_build_host)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    calls: list[tuple[Path, dict]] = []

    def building(root: Path, **keywords) -> Path:
        calls.append((root, keywords))
        return root / "dist" / "wheelhouse"

    monkeypatch.setattr(ARTIFACTS, "build_wheelhouse", building)

    assert ARTIFACTS.main(["--repo-root", str(repo_root), "--wheelhouse"]) == 0
    assert calls == [(repo_root.resolve(), {"python_executable": str(interpreter)})]


def test_the_wheelhouse_refuses_by_name_before_runtime_has_run(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, mapped_build_host: str
) -> None:
    called: list[Path] = []
    monkeypatch.setattr(ARTIFACTS, "build_wheelhouse", lambda root, **_: called.append(root))

    assert ARTIFACTS.main(["--repo-root", str(tmp_path), "--wheelhouse"]) == 1

    error = capsys.readouterr().err
    assert error.startswith("Error: ") and "python scripts/dev.py runtime" in error
    assert called == [], "nothing is resolved for the build host's interpreter instead"


@pytest.mark.parametrize(
    ("mutate", "named"),
    [
        (lambda pins: pins["node"]["platforms"].pop("windows-x64"), "node.platforms must pin exactly"),
        (lambda pins: pins["node"]["platforms"]["linux-x64"].update(sha256="ABC"), "node.platforms.linux-x64.sha256"),
        (lambda pins: pins["node"].update(version="^22"), "node.version"),
        (lambda pins: pins.pop("node"), "no 'node' object"),
        (lambda pins: pins["python"]["platforms"].pop("linux-x64"), "python.platforms must pin exactly"),
        (lambda pins: pins["python"]["platforms"]["windows-x64"].update(sha256=None), "python.platforms.windows-x64"),
        (lambda pins: pins["python"].update(build_tag="latest"), "python.build_tag"),
        (lambda pins: pins.pop("python"), "no 'python' object"),
    ],
)
def test_an_incomplete_pins_file_is_refused_by_name_before_anything_is_fetched(tmp_path: Path, mutate, named) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    path = repo_root / ARTIFACTS.RUNTIME_PINS_FILE
    pins = json.loads(path.read_text(encoding="utf-8"))
    mutate(pins)
    write_runtime_pins(repo_root, pins)
    fetch = _RuntimeFetcher(fixture_node_body("macos-arm64"))

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape(named)):
        _build_runtime(repo_root, fetch=fetch)

    assert fetch.urls == []
    assert not (repo_root / "dist").exists()


def test_a_missing_pins_file_is_refused_by_name(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).unlink()

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(repo_root)

    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in str(excinfo.value)
    assert "missing" in str(excinfo.value)


def test_two_runtime_builds_from_the_same_inputs_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ARTIFACTS.SOURCE_DATE_EPOCH_ENV, _RUNTIME_EPOCH)
    repo_root = _make_runtime_repo(tmp_path)

    first = _directory_bytes(_build_runtime(repo_root))
    second = _directory_bytes(_build_runtime(repo_root))

    assert first == second


def test_a_failed_download_is_a_refusal_naming_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_args, **_kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(ARTIFACTS.urllib.request, "urlopen", offline)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape("https://nodejs.org/dist/v1.2.3/x.zip")):
        ARTIFACTS._download("https://nodejs.org/dist/v1.2.3/x.zip", tmp_path / "x.zip")


class _CannedSocket:
    """Just enough of a socket for a real :class:`http.client.HTTPResponse` to read *raw* from."""

    def __init__(self, raw: bytes) -> None:
        self._file = io.BytesIO(raw)

    def makefile(self, *_args, **_kwargs) -> io.BytesIO:
        return self._file


def _serve_over_urlopen(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> list[tuple[object, object]]:
    """Answer every ``urlopen`` with a real ``HTTPResponse`` parsed from *raw*; returns the requests it was handed."""
    requests: list[tuple[object, object]] = []

    def urlopen(request, timeout=None):
        requests.append((request, timeout))
        response = http.client.HTTPResponse(_CannedSocket(raw), method=request.get_method())
        response.begin()
        return response

    monkeypatch.setattr(ARTIFACTS.urllib.request, "urlopen", urlopen)
    return requests


_DOWNLOAD_URL = "https://nodejs.org/dist/v1.2.3/x.tar.gz"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial", id="short-of-its-content-length"),
        pytest.param(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n64\r\npartial", id="cut-mid-chunk"),
    ],
)
def test_a_download_cut_off_part_way_is_a_refusal_naming_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    """Neither cut-off is an ``OSError``, and one of them does not raise at all.

    A chunked body raises ``http.client.IncompleteRead``, an ``HTTPException``, which escaped ``main`` as a traceback.
    A body short of its ``Content-Length`` raises nothing on 3.11: ``HTTPResponse.read(amt)`` answers what arrived and
    then ``b""``, so the file was silently kept short -- and ``update-pins`` would have pinned it.
    """
    _serve_over_urlopen(monkeypatch, raw)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert str(excinfo.value).startswith(f"could not download {_DOWNLOAD_URL}: ")


def test_a_complete_download_writes_the_body_and_names_itself_with_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _serve_over_urlopen(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\nthe whole body")
    destination = tmp_path / "x.tar.gz"

    ARTIFACTS._download(_DOWNLOAD_URL, destination)

    assert destination.read_bytes() == b"the whole bod", "exactly the declared length is read"
    ((request, timeout),) = requests
    assert request.full_url == _DOWNLOAD_URL
    assert request.get_header("User-agent") == "swcraft-build-artifacts"
    assert timeout == ARTIFACTS.DOWNLOAD_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# The download retry: transport is retried over DOWNLOAD_RETRY_WAITS, content never
# ---------------------------------------------------------------------------

_WHOLE_BODY = b"the whole body"
_WHOLE = b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\n\r\n" + _WHOLE_BODY
#: Longer than :data:`_WHOLE_BODY` on purpose: a retry that rewound without truncating
#: would leave its tail behind the whole body.
_CUT_OFF_LONGER = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 40
_CUT_MID_CHUNK = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n64\r\n" + b"x" * 40


def _http_error(code: int, reason: str) -> urllib.error.HTTPError:
    """A real :class:`urllib.error.HTTPError`, the exception ``urlopen`` raises for a non-2xx answer."""
    return urllib.error.HTTPError(_DOWNLOAD_URL, code, reason, http.client.HTTPMessage(), None)


def _serve_in_turn(monkeypatch: pytest.MonkeyPatch, *answers: bytes | BaseException) -> list[object]:
    """Answer the n-th ``urlopen`` with the n-th of *answers* -- raised when an exception, served when bytes.

    The last answer repeats, so one exception is a failure on every attempt. Returns the
    requests handed to ``urlopen``, one per attempt.
    """
    requests: list[object] = []

    def urlopen(request, timeout=None):
        requests.append(request)
        answer = answers[min(len(requests), len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        response = http.client.HTTPResponse(_CannedSocket(answer), method=request.get_method())
        response.begin()
        return response

    monkeypatch.setattr(ARTIFACTS.urllib.request, "urlopen", urlopen)
    return requests


@pytest.fixture
def retry_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Three retries with distinct waits; returns the waits :func:`time.sleep` was asked for, in order.

    ``time.sleep`` is replaced, so no test waits; the waits are distinct so that a retry
    reading the wrong one (the first every time, or one ahead) fails on the recorded order.
    """
    monkeypatch.setattr(ARTIFACTS, "DOWNLOAD_RETRY_WAITS", (1, 2, 3))
    taken: list[float] = []
    monkeypatch.setattr(ARTIFACTS.time, "sleep", taken.append)
    return taken


def test_a_download_that_fails_twice_then_answers_writes_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    requests = _serve_in_turn(
        monkeypatch, ConnectionResetError(10054, "An existing connection was forcibly closed"), TimeoutError(), _WHOLE
    )
    destination = tmp_path / "x.tar.gz"

    ARTIFACTS._download(_DOWNLOAD_URL, destination)

    assert destination.read_bytes() == _WHOLE_BODY
    assert len(requests) == 3
    assert retry_waits == [1, 2], "the first two waits, in order, one before each retry"


def test_a_download_that_fails_every_time_is_refused_naming_four_attempts_and_the_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    requests = _serve_in_turn(monkeypatch, OSError("network is unreachable"))

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert str(excinfo.value) == f"could not download {_DOWNLOAD_URL}: network is unreachable (4 attempts)"
    assert len(requests) == 4
    assert retry_waits == [1, 2, 3]


@pytest.mark.parametrize(
    "cut_off", [_CUT_OFF_LONGER, _CUT_MID_CHUNK], ids=["short-of-its-content-length", "cut-mid-chunk"]
)
def test_a_cut_off_body_is_retried_and_the_retry_leaves_exactly_the_whole_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float], cut_off: bytes
) -> None:
    """The destination is truncated between attempts: the cut-off's 40 bytes do not survive behind the 14."""
    requests = _serve_in_turn(monkeypatch, cut_off, _WHOLE)
    destination = tmp_path / "x.tar.gz"

    ARTIFACTS._download(_DOWNLOAD_URL, destination)

    assert destination.read_bytes() == _WHOLE_BODY
    assert len(requests) == 2
    assert retry_waits == [1]


def test_a_body_cut_off_on_every_attempt_is_refused_naming_the_cut_off_and_four_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    requests = _serve_in_turn(monkeypatch, _CUT_OFF_LONGER)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert str(excinfo.value) == (
        f"could not download {_DOWNLOAD_URL}: "
        "the body was cut off, 40 of the 100 bytes it declares arrived (4 attempts)"
    )
    assert len(requests) == 4
    assert retry_waits == [1, 2, 3]


def test_a_retried_http_error_is_closed_before_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """A 5xx holds its response open; the retry releases it rather than leaving it to the collector."""
    body = io.BytesIO(b"busy")
    error = urllib.error.HTTPError(_DOWNLOAD_URL, 503, "Service Unavailable", http.client.HTTPMessage(), body)
    closed_at_wait: list[bool] = []
    monkeypatch.setattr(ARTIFACTS.time, "sleep", lambda _seconds: closed_at_wait.append(body.closed))
    _serve_in_turn(monkeypatch, error, _WHOLE)

    ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert closed_at_wait == [True]


def test_a_destination_that_cannot_be_opened_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """No attempt was made, so the refusal carries no attempt count."""
    requests = _serve_in_turn(monkeypatch, _WHOLE)
    destination = tmp_path / "no-such-directory" / "x.tar.gz"

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, destination)

    message = str(excinfo.value)
    assert message.startswith(f"could not download {_DOWNLOAD_URL}: {destination} cannot be written: ")
    assert "attempt" not in message
    assert requests == []
    assert retry_waits == []


@pytest.mark.parametrize(("code", "reason"), [(503, "Service Unavailable"), (429, "Too Many Requests")])
def test_a_server_error_or_too_many_requests_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float], code: int, reason: str
) -> None:
    requests = _serve_in_turn(monkeypatch, _http_error(code, reason), _WHOLE)
    destination = tmp_path / "x.tar.gz"

    ARTIFACTS._download(_DOWNLOAD_URL, destination)

    assert destination.read_bytes() == _WHOLE_BODY
    assert len(requests) == 2
    assert retry_waits == [1]


def test_a_not_found_is_refused_on_the_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """A wrong pin URL is content, not transport: no wait, one request, *(1 attempt)*."""
    requests = _serve_in_turn(monkeypatch, _http_error(404, "Not Found"), _WHOLE)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert str(excinfo.value) == f"could not download {_DOWNLOAD_URL}: HTTP Error 404: Not Found (1 attempt)"
    assert len(requests) == 1
    assert retry_waits == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            id="as-a-urlerror-reason",
        ),
        pytest.param(ssl.SSLCertVerificationError(1, "certificate verify failed"), id="bare"),
    ],
)
def test_a_certificate_that_fails_verification_is_refused_on_the_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float], failure: BaseException
) -> None:
    requests = _serve_in_turn(monkeypatch, failure, _WHOLE)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape("(1 attempt)")) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert "certificate verify failed" in str(excinfo.value)
    assert len(requests) == 1
    assert retry_waits == []


def test_a_write_error_from_the_sink_is_retried_three_times_and_refused_with_it_as_the_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """A full disk is an ``OSError`` the retry cannot tell from transport, so it is retried like one."""
    requests = _serve_in_turn(monkeypatch, _WHOLE)

    def full_disk(_source, _sink, _length=0):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ARTIFACTS.shutil, "copyfileobj", full_disk)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, tmp_path / "x.tar.gz")

    assert str(excinfo.value).endswith("No space left on device (4 attempts)")
    assert len(requests) == 4
    assert retry_waits == [1, 2, 3]


class _SinkThatFails(io.BytesIO):
    """A destination whose ``flush`` fails *flush_failures* times, and whose ``close`` fails if *close_fails*."""

    def __init__(self, flush_failures: int = 0, close_fails: bool = False) -> None:
        super().__init__()
        self.flush_failures = flush_failures
        self.close_fails = close_fails

    def flush(self) -> None:
        if self.flush_failures:
            self.flush_failures -= 1
            raise OSError(28, "No space left on device")
        super().flush()

    def close(self) -> None:
        if self.close_fails:
            self.close_fails = False
            raise OSError(5, "Input/output error")
        super().close()


def _open_destination_as(monkeypatch: pytest.MonkeyPatch, destination: Path, sink: io.BytesIO) -> None:
    """``destination.open`` answers *sink*; every other path opens as usual."""
    original = Path.open

    def open_(self, *args, **kwargs):
        return sink if self == destination else original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)


def test_a_write_error_held_back_by_the_buffer_is_retried_inside_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """The flush is part of the attempt: a deferred write error is retried, not raised bare after it."""
    requests = _serve_in_turn(monkeypatch, _WHOLE)
    destination = tmp_path / "x.tar.gz"
    sink = _SinkThatFails(flush_failures=1)
    _open_destination_as(monkeypatch, destination, sink)

    ARTIFACTS._download(_DOWNLOAD_URL, destination)

    assert len(requests) == 2
    assert retry_waits == [1]
    assert sink.closed


def test_a_destination_that_cannot_be_closed_is_refused_naming_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """Not a raw ``OSError``: the refusal names the download, and carries no attempt count, since none failed."""
    requests = _serve_in_turn(monkeypatch, _WHOLE)
    destination = tmp_path / "x.tar.gz"
    _open_destination_as(monkeypatch, destination, _SinkThatFails(close_fails=True))

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS._download(_DOWNLOAD_URL, destination)

    message = str(excinfo.value)
    assert (
        message
        == f"could not download {_DOWNLOAD_URL}: {destination} could not be closed: [Errno 5] Input/output error"
    )
    assert len(requests) == 1
    assert retry_waits == []


def test_a_digest_mismatch_is_refused_with_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    """The digest check sits outside the attempts: a whole body of the wrong bytes is a pin question."""
    repo_root = _make_runtime_repo(tmp_path)
    wrong = b"not the registry's bytes"
    requests = _serve_in_turn(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(wrong) + wrong)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape(hashlib.sha256(wrong).hexdigest())):
        ARTIFACTS.install_elkjs_payload(repo_root)

    assert len(requests) == 1
    assert retry_waits == []


def test_main_runtime_flag_builds_and_reports(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[Path] = []

    def fake_build(repo_root: Path) -> Path:
        built.append(repo_root)
        return repo_root / "dist" / "runtime"

    monkeypatch.setattr(ARTIFACTS, "build_runtime_payload", fake_build)

    assert ARTIFACTS.main(["--repo-root", str(tmp_path), "--runtime"]) == 0
    assert built == [tmp_path.resolve()]
    assert "Populated" in capsys.readouterr().out


#: Every function ``main`` dispatches a mode to.
_MODE_SEAMS = (
    "build_all",
    "build_wheelhouse",
    "build_docs_site",
    "build_workstation_archive",
    "build_runtime_payload",
    "update_runtime_pins",
    "install_elkjs_payload",
)


def _record_every_mode(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace each mode's function with a recorder; returns the names called."""
    called: list[str] = []
    for name in _MODE_SEAMS:
        monkeypatch.setattr(ARTIFACTS, name, lambda *_args, _name=name, **_kwargs: called.append(_name))
    return called


@pytest.mark.parametrize("other", ["--wheelhouse", "--docs-only", "--workstation-archive", "--update-pins", "--elkjs"])
def test_main_refuses_the_runtime_flag_beside_another_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    """A usage error (exit 2) before any mode runs: nothing is built, and nothing is written."""
    called = _record_every_mode(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        ARTIFACTS.main(["--repo-root", str(tmp_path), "--runtime", other])

    assert excinfo.value.code == 2
    assert called == []
    assert list(tmp_path.iterdir()) == []


def test_main_reports_a_runtime_refusal_in_the_error_shape(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing(_repo_root: Path) -> Path:
        raise ARTIFACTS.BuildArtifactsError("the pins say otherwise")

    monkeypatch.setattr(ARTIFACTS, "build_runtime_payload", refusing)

    assert ARTIFACTS.main(["--repo-root", str(tmp_path), "--runtime"]) == 1
    assert capsys.readouterr().err.startswith("Error: the pins say otherwise")


# ---------------------------------------------------------------------------
# The companion extensions and the notices
# ---------------------------------------------------------------------------


def _notice_section(notices: str, ident: str) -> str:
    """The ``### `<id>` <version>`` section of *notices*, up to the next heading of that level or the end."""
    sections = [section for section in notices.split("\n### ") if section.startswith(f"`{ident}` ")]
    assert len(sections) == 1, f"the notices hold {len(sections)} sections for {ident}"
    return "### " + sections[0]


def test_the_committed_pins_carry_the_extension_pack_in_its_order_for_exactly_the_three_tokens() -> None:
    """The pack's ids in its order, one exact release, a build per platform -- its own or universal."""
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)
    manifest = json.loads((REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))

    assert [entry["id"] for entry in pins["extensions"]] == manifest["extensionPack"]
    assert ARTIFACTS.read_extension_pack(REPO_ROOT) == manifest["extensionPack"]
    for token in ARCHIVE_TOKENS:
        for _, version, target, digest in ARTIFACTS.extension_pins(pins, token):
            assert re.fullmatch(r"\d+\.\d+\.\d+", version)
            assert target in (ARTIFACTS.UNIVERSAL_TARGET, ARTIFACTS.RUNTIME_PLATFORMS[token]["vscode_target"])
            assert re.fullmatch(r"[0-9a-f]{64}", digest)
    for entry in pins["extensions"]:
        builds = {build["target"] for build in entry["platforms"].values()}
        digests = {build["sha256"] for build in entry["platforms"].values()}
        assert len(digests) == len(builds), f"{entry['id']}: one file per build -- a universal build is one file"


def _drop_extensions(pins: dict) -> None:
    del pins["extensions"]


def _empty_extensions(pins: dict) -> None:
    pins["extensions"] = []


def _rename_first(pins: dict) -> None:
    pins["extensions"][0]["id"] = "Not An Id"


def _pin_first_twice(pins: dict) -> None:
    pins["extensions"].append(json.loads(json.dumps(pins["extensions"][0])))


def _range_the_first(pins: dict) -> None:
    pins["extensions"][0]["version"] = "^1.0.0"


def _drop_a_platform(pins: dict) -> None:
    del pins["extensions"][0]["platforms"]["linux-x64"]


def _foreign_target(pins: dict) -> None:
    pins["extensions"][1]["platforms"]["linux-x64"]["target"] = "win32-x64"


def _bad_digest(pins: dict) -> None:
    pins["extensions"][0]["platforms"]["macos-arm64"]["sha256"] = "ABC"


#: Each way an extension pin can be unfit, and the words its refusal must carry.
_EXTENSION_PIN_DEFECTS = {
    "no-list": (_drop_extensions, "no 'extensions' list"),
    "empty-list": (_empty_extensions, "no 'extensions' list"),
    "not-an-id": (_rename_first, "lowercase publisher.name"),
    "pinned-twice": (_pin_first_twice, "a second time"),
    "a-range": (_range_the_first, "exact X.Y.Z"),
    "a-platform-missing": (_drop_a_platform, "must pin exactly"),
    "another-platforms-build": (_foreign_target, "linux-x64.target must be one of universal, linux-x64"),
    "a-bad-digest": (_bad_digest, "64 lowercase hexadecimal"),
}


@pytest.mark.parametrize("case", sorted(_EXTENSION_PIN_DEFECTS))
def test_an_unfit_extension_pin_is_refused_by_name_before_anything_is_fetched(tmp_path: Path, case: str) -> None:
    mutate, named = _EXTENSION_PIN_DEFECTS[case]
    repo_root = _make_runtime_repo(tmp_path)
    pins = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    mutate(pins)
    write_runtime_pins(repo_root, pins)
    fetch = _RuntimeFetcher(fixture_node_body("macos-arm64"), fixture_python_body("macos-arm64"))

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(repo_root, fetch=fetch)

    assert named in str(excinfo.value)
    assert fetch.urls == [], "the pins are checked whole before the first byte is fetched"


@pytest.mark.parametrize(("target", "query"), [("universal", ""), ("win32-x64", "?targetPlatform=win32-x64")])
def test_the_marketplace_url_names_a_platform_build_only_when_one_is_pinned(target: str, query: str) -> None:
    assert ARTIFACTS.extension_download_url("example.platform-ext", "1.2.3", target) == (
        "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/example/vsextensions/platform-ext/"
        f"1.2.3/vspackage{query}"
    )


def test_a_gzip_encoded_marketplace_body_is_decoded_and_a_plain_package_is_left_as_it_came(tmp_path: Path) -> None:
    """The endpoint gzips even under ``Accept-Encoding: identity``; what is hashed and stored is the ``.vsix``."""
    package = fixture_vsix_body("example.universal-ext", "universal")
    encoded = tmp_path / "encoded.vsix"
    encoded.write_bytes(gzip.compress(package, mtime=0))
    plain = tmp_path / "plain.vsix"
    plain.write_bytes(package)

    assert ARTIFACTS.decode_marketplace_package(encoded).read_bytes() == package
    assert ARTIFACTS.decode_marketplace_package(plain).read_bytes() == package


def test_each_companion_is_stored_gunzipped_under_a_version_free_name_as_the_build_its_platform_pins(
    tmp_path: Path,
) -> None:
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(
        repo_root,
        token="windows-x64",
        fetch=_RuntimeFetcher(fixture_node_body("windows-x64"), fixture_python_body("windows-x64")),
    )

    assert (runtime / "example.universal-ext.vsix").read_bytes() == fixture_vsix_body(
        "example.universal-ext", "universal"
    )
    assert (runtime / "example.platform-ext.vsix").read_bytes() == fixture_vsix_body(
        "example.platform-ext", "win32-x64"
    )
    for package in runtime.glob("*.vsix"):
        assert not re.search(r"\d+\.\d+", package.name), f"{package.name} carries a version"
        assert not any(target in package.name for target in ("win32", "linux", "darwin")), package.name


def test_a_companion_whose_digest_does_not_match_is_refused_and_leaves_dist_runtime_untouched(tmp_path: Path) -> None:
    """Another platform's build served in place of the pinned one is exactly what the pinned digest is for."""
    repo_root = _make_runtime_repo(tmp_path)
    previous = repo_root / "dist" / "runtime"
    previous.mkdir(parents=True)
    (previous / "RUNTIME.json").write_bytes(b"the previous payload's record")
    before = _directory_bytes(previous)
    impostor = gzip.compress(fixture_vsix_body("example.platform-ext", "linux-x64"), mtime=0)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        _build_runtime(
            repo_root,
            fetch=_RuntimeFetcher(fixture_node_body("macos-arm64"), fixture_python_body("macos-arm64"), vsix=impostor),
        )

    message = str(excinfo.value)
    assert "example.universal-ext 1.0.0 (universal)" in message
    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in message
    assert hashlib.sha256(fixture_vsix_body("example.universal-ext", "universal")).hexdigest() in message
    assert _directory_bytes(previous) == before
    assert not (repo_root / "dist" / "runtime.new").exists()


@pytest.mark.parametrize("token", ARCHIVE_TOKENS)
def test_the_notices_carry_each_payloads_own_licence_text_and_no_likec4(tmp_path: Path, token: str) -> None:
    """Node's ``LICENSE`` and CPython's ``LICENSE.txt``, read out of the payloads.

    On every token, because the three differ where it matters: Node's zip on Windows against
    its tarballs, and CPython's licence at the tree's root on Windows against beside the
    standard library elsewhere. npm's nested ``LICENSE`` must never stand in for Node's.
    """
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(repo_root, token=token)

    notices = (runtime / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert notices.startswith("# Third-party notices\n")
    assert "## Node.js 1.2.3" in notices
    assert f"The Node.js licence, as published for {token}." in notices
    assert NPM_LICENCE_DECOY.strip() not in notices, "npm's nested LICENSE is not Node's"
    assert f"## Python {FIXTURE_PYTHON_VERSION}" in notices
    assert f"The CPython licence, as built for {token}." in notices
    assert FIXTURE_PYTHON_BUILD_TAG in notices
    assert "likec4" not in notices.lower(), "the likec4 tree and its closure left the archive"
    assert [line for line in notices.splitlines() if line.startswith("| ")] == [], "no closure table is left"
    assert [line for line in notices.splitlines() if line.startswith("## ")] == [
        "## Node.js 1.2.3",
        f"## Python {FIXTURE_PYTHON_VERSION}",
        f"## elkjs {FIXTURE_ELKJS_VERSION}",
        "## Visual Studio Code extensions",
    ]


def test_the_notices_rederive_each_companions_licence_from_its_own_manifest(tmp_path: Path) -> None:
    """The licence is read out of each staged package here, and looked for in its section.

    Not compared with a transcribed MIT / EPL-2.0: a publisher that relicenses changes the
    package, the package changes the notice, and this reads the package.
    """
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(repo_root)

    notices = (runtime / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for ident, _, _ in FIXTURE_EXTENSIONS:
        with zipfile.ZipFile(runtime / f"{ident}.vsix") as package:
            manifest = json.loads(package.read("extension/package.json").decode("utf-8"))
            text = package.read("extension/LICENSE.txt").decode("utf-8")
        section = _notice_section(notices, ident)
        assert section.splitlines()[0] == f"### `{ident}` {manifest['version']}"
        assert f"Declared licence: {manifest['license']}" in section.splitlines()
        assert text.strip() in section
        assert "from `extension/LICENSE.txt` inside it" in section


def test_two_runtime_builds_write_byte_identical_notices(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)

    first = (_build_runtime(repo_root) / "THIRD-PARTY-NOTICES.md").read_bytes()
    second = (_build_runtime(repo_root) / "THIRD-PARTY-NOTICES.md").read_bytes()

    assert first == second


def test_a_package_that_carries_no_licence_file_is_named_as_carrying_none(tmp_path: Path) -> None:
    package = tmp_path / "bare.vsix"
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("extension/package.json", json.dumps({"version": "1.0.0"}))

    notice = ARTIFACTS.read_vsix_notice(package)
    rendered = ARTIFACTS.render_third_party_notices(
        node_version="1.2.3",
        node_licence="n",
        python_version="3.11.0",
        python_build_tag="20000101",
        python_licence="p",
        extensions=[("example.bare", "1.0.0", "universal", notice)],
        elkjs={"version": "0.9.9", "licence": "EPL-2.0", "text": "e"},
    )

    assert notice == {"version": "1.0.0", "licence": "not declared", "text": "", "source": ""}
    section = _notice_section(rendered, "example.bare")
    assert "Declared licence: not declared" in section.splitlines()
    assert "The package carries no licence file." in section


def test_a_licence_text_holding_a_fence_cannot_close_its_own_block() -> None:
    lines = ARTIFACTS._fenced("before\n```\nafter\r\n")
    assert lines == ["````text", "before", "```", "after", "````"]


def test_a_payload_without_its_licence_file_is_refused_by_name(tmp_path: Path) -> None:
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("node-v1.2.3-darwin-arm64/README.md")
        bundle.addfile(info, io.BytesIO(b""))

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="holds no LICENSE"):
        ARTIFACTS.read_node_licence(archive)
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="LICENSE.txt"):
        ARTIFACTS.read_python_licence(tmp_path, "3.11.0", "windows-x64")


def test_a_node_zip_without_its_licence_file_or_that_is_no_zip_is_refused_by_name(tmp_path: Path) -> None:
    """The zip branch Windows takes: npm's nested ``LICENSE`` is not the one directly inside the leading directory."""
    archive = tmp_path / "node.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("node-v1.2.3-win-x64/README.md", "")
        bundle.writestr("node-v1.2.3-win-x64/node_modules/npm/LICENSE", NPM_LICENCE_DECOY)
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="node.zip holds no LICENSE"):
        ARTIFACTS.read_node_licence(archive)

    archive.write_bytes(b"not a zip at all")
    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="node.zip could not be read for its licence"):
        ARTIFACTS.read_node_licence(archive)


def _write_vsix(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, text in members.items():
            bundle.writestr(name, text)
    return path


_VSIX_MANIFEST_MEMBER = ("extension/package.json", json.dumps({"version": "1.0.0", "license": "MIT"}))


@pytest.mark.parametrize(
    "descriptor",
    [
        pytest.param(None, id="no-descriptor"),
        pytest.param("<PackageManifest><Metadata/></PackageManifest>", id="a-descriptor-naming-no-licence"),
        pytest.param("<License>extension/LICENSE.txt</License>", id="a-descriptor-naming-an-absent-member"),
    ],
)
def test_a_vsix_whose_descriptor_names_no_licence_it_holds_falls_back_to_its_top_level_licence_file(
    tmp_path: Path, descriptor: str | None
) -> None:
    """The fallback: the one ``LICENSE*`` at ``extension/``'s root -- never one nested deeper, like a dependency's."""
    members = dict(
        [
            _VSIX_MANIFEST_MEMBER,
            ("extension/node_modules/dependency/LICENSE", "a dependency's licence, not the extension's"),
            ("extension/Licence.md", "the extension's own licence"),
        ]
    )
    if descriptor is not None:
        members["extension.vsixmanifest"] = descriptor
    package = _write_vsix(tmp_path / "fallback.vsix", members)

    assert ARTIFACTS.read_vsix_notice(package) == {
        "version": "1.0.0",
        "licence": "MIT",
        "text": "the extension's own licence",
        "source": "extension/Licence.md",
    }


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(
            lambda path: _write_vsix(path, {"extension/LICENSE.txt": "a licence with no package beside it"}),
            id="no-package-json",
        ),
        pytest.param(lambda path: path.write_bytes(b"not a zip at all"), id="not-a-zip"),
    ],
)
def test_a_vsix_that_cannot_be_read_is_refused_by_name(tmp_path: Path, write) -> None:
    package = tmp_path / "broken.vsix"
    write(package)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=r"broken\.vsix is not a readable \.vsix"):
        ARTIFACTS.read_vsix_notice(package)


@pytest.mark.usefixtures("mapped_build_host")
def test_the_companion_extensions_travel_stored_as_ordinary_files_with_the_pinned_digest(tmp_path: Path) -> None:
    repo_root = _make_workstation_repo(tmp_path)

    archive = ARTIFACTS.build_workstation_archive(repo_root)

    with zipfile.ZipFile(archive) as opened:
        infos = {info.filename: info for info in opened.infolist()}
        sums = opened.read(ARTIFACTS.SHA256SUMS_FILENAME).decode("utf-8")
        listed = {name: digest for digest, name in (line.split("  ", 1) for line in sums.splitlines())}
        pins = ARTIFACTS.read_runtime_pins(repo_root)
        for ident, _, target, digest in ARTIFACTS.extension_pins(pins, "macos-arm64"):
            name = f"extensions/{ident}.vsix"
            assert opened.read(name) == fixture_vsix_body(ident, target), "the package travels byte for byte"
            assert listed[name] == digest
            assert infos[name].compress_type == zipfile.ZIP_STORED
            assert infos[name].external_attr >> 16 == stat.S_IFREG | 0o644
        notices = opened.read("THIRD-PARTY-NOTICES.md").decode("utf-8")
        assert notices.startswith(_FIXTURE_NOTICES), "the runtime's notices travel first, unchanged"
        assert "\n## Python packages\n" in notices
        assert infos["THIRD-PARTY-NOTICES.md"].compress_type == zipfile.ZIP_DEFLATED


# ---------------------------------------------------------------------------
# The notices' Python packages section: every third-party wheel, read from the wheel
# ---------------------------------------------------------------------------

#: Five third-party wheels, one per way a wheel states its licence, written in an order that is
#: not the sorted one. ``(file name, METADATA, extra members)``.
_NOTICE_WHEELS = (
    (
        "zeta_pkg-1.0.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: zeta-pkg\nVersion: 1.0.0\n",
        {},
    ),
    (
        "numpy-2.4.6-cp311-cp311-macosx_14_0_arm64.whl",
        "Metadata-Version: 2.4\nName: numpy\nVersion: 2.4.6\nLicense-Expression: BSD-3-Clause AND MIT\n"
        "License: ignored when an expression is declared\nLicense-File: LICENSE.txt\n"
        "License-File: numpy/random/LICENSE.md\n",
        {
            "numpy-2.4.6.dist-info/licenses/LICENSE.txt": "BSD",
            "numpy-2.4.6.dist-info/licenses/numpy/random/LICENSE.md": "MIT",
            "numpy-2.4.6.dist-info/RECORD": "",
            "numpy/__init__.py": "",
        },
    ),
    (
        "pyyaml-6.0.3-cp311-cp311-macosx_11_0_arm64.whl",
        "Metadata-Version: 2.1\nName: PyYAML\nVersion: 6.0.3\nLicense: MIT\n"
        "Classifier: License :: OSI Approved :: MIT License\n",
        {"pyyaml-6.0.3.dist-info/licenses/LICENSE": "MIT"},
    ),
    (
        "scipy-1.17.1-cp311-cp311-macosx_14_0_arm64.whl",
        "Metadata-Version: 2.1\nName: scipy\nVersion: 1.17.1\n"
        "License: Copyright (c) 2001-2002 Enthought, Inc.\n        All rights reserved.\n"
        "Classifier: License :: OSI Approved :: BSD License\nClassifier: Programming Language :: Python\n",
        {"scipy-1.17.1.dist-info/LICENSE.txt": "BSD"},
    ),
    (
        "mdurl-0.1.2-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: mdurl\nVersion: 0.1.2\nClassifier: License :: OSI Approved :: MIT License\n",
        {"mdurl-0.1.2.dist-info/LICENSE": "MIT", "mdurl-0.1.2.dist-info/WHEEL": ""},
    ),
)


def _make_notices_repo(tmp_path: Path, wheels: Sequence[tuple[str, str, dict[str, str]]] = _NOTICE_WHEELS) -> Path:
    """A workstation checkout whose wheelhouse holds *wheels* and SWCraft's four."""
    repo_root = _make_workstation_repo(tmp_path, third_party=())
    wheelhouse = _wheelhouse_of(repo_root)
    for name, metadata, files in wheels:
        _write_wheel(wheelhouse / name, metadata, files)
    return repo_root


def _archive_notices(repo_root: Path) -> str:
    with zipfile.ZipFile(ARTIFACTS.build_workstation_archive(repo_root)) as opened:
        return opened.read(ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME).decode("utf-8")


def _python_packages(notices: str) -> str:
    """The ``## Python packages`` section of *notices*, which closes the document."""
    head, found, section = notices.partition("\n## Python packages\n")
    assert found, "the notices hold no Python packages section"
    assert "\n## " not in section, "the Python packages section is the last one"
    return section


@pytest.mark.usefixtures("mapped_build_host")
def test_the_notices_list_every_third_party_wheel_with_its_declared_licence_and_licence_files(
    tmp_path: Path,
) -> None:
    """Name, version, the declared licence (``License-Expression``, ``License``, then classifiers), licence files."""
    notices = _archive_notices(_make_notices_repo(tmp_path))

    assert notices.startswith(_FIXTURE_NOTICES), "the runtime's own notices come first, as written"
    section = _python_packages(notices)
    headings = [line for line in section.splitlines() if line.startswith("### ")]
    assert headings == [
        "### `mdurl` 0.1.2",
        "### `numpy` 2.4.6",
        "### `PyYAML` 6.0.3",
        "### `scipy` 1.17.1",
        "### `zeta-pkg` 1.0.0",
    ], "every third-party wheel, under the name and version its METADATA declares, in canonical-name order"

    numpy = _notice_section(notices, "numpy").splitlines()
    assert "The `wheels/numpy-2.4.6-cp311-cp311-macosx_14_0_arm64.whl` file." in numpy
    assert "Declared licence: BSD-3-Clause AND MIT (its `License-Expression` field)" in numpy
    assert "- `numpy-2.4.6.dist-info/licenses/LICENSE.txt`" in numpy
    assert "- `numpy-2.4.6.dist-info/licenses/numpy/random/LICENSE.md`" in numpy
    assert not [line for line in numpy if "RECORD" in line or "__init__" in line], "only licence files are named"

    pyyaml = _notice_section(notices, "PyYAML").splitlines()
    assert "Declared licence: MIT (its `License` field)" in pyyaml
    assert "- `pyyaml-6.0.3.dist-info/licenses/LICENSE`" in pyyaml

    scipy = _notice_section(notices, "scipy").splitlines()
    assert "Declared licence: BSD License (its `License ::` classifiers)" in scipy, (
        "a License field that is a licence text and not a name gives way to the classifiers"
    )
    assert "- `scipy-1.17.1.dist-info/LICENSE.txt`" in scipy, "a licence file at the .dist-info's root counts"

    mdurl = _notice_section(notices, "mdurl").splitlines()
    assert "Declared licence: MIT License (its `License ::` classifiers)" in mdurl
    assert "- `mdurl-0.1.2.dist-info/LICENSE`" in mdurl
    assert not [line for line in mdurl if "WHEEL" in line]


@pytest.mark.usefixtures("mapped_build_host")
def test_a_wheel_that_declares_no_licence_is_listed_as_not_declared_never_skipped(tmp_path: Path) -> None:
    notices = _archive_notices(_make_notices_repo(tmp_path))

    zeta = _notice_section(notices, "zeta-pkg").splitlines()
    assert "Declared licence: not declared" in zeta
    assert "It carries no licence file in its `.dist-info`." in zeta


@pytest.mark.usefixtures("mapped_build_host")
def test_swcrafts_own_wheels_the_specimen_generators_among_them_are_not_listed(tmp_path: Path) -> None:
    """Not third-party: SWCraft's four, the specimen generator's among them."""
    repo_root = _make_notices_repo(tmp_path)
    archive = ARTIFACTS.build_workstation_archive(repo_root)
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        section = _python_packages(opened.read(ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME).decode("utf-8"))

    assert "swcraft_specimens" in ARTIFACTS.SWCRAFT_DISTRIBUTIONS
    for distribution in ARTIFACTS.SWCRAFT_DISTRIBUTIONS:
        assert f"wheels/{distribution}-{VERSION}-py3-none-any.whl" in names
        assert f"### `{distribution}`" not in section
        assert f"### `{distribution.replace('_', '-')}`" not in section
    preamble = section.split("\n### ", 1)[0]
    assert "the specimen generator's among them, are not third-party" in preamble
    assert "bundled" not in preamble, "the bundled sequence went with the private generator"


@pytest.mark.usefixtures("mapped_build_host")
def test_the_python_packages_section_is_deterministic(tmp_path: Path) -> None:
    """Two builds are byte-identical, and the order is the names', not the order the wheels were written in."""
    first = _archive_notices(_make_notices_repo(tmp_path / "first"))
    second = _archive_notices(_make_notices_repo(tmp_path / "second", tuple(reversed(_NOTICE_WHEELS))))

    assert first == second
    assert str(tmp_path) not in first, "no host path reaches the notices"


@pytest.mark.usefixtures("mapped_build_host")
def test_a_third_party_wheel_without_metadata_is_refused_by_name(tmp_path: Path) -> None:
    """Its notice cannot be written, so the archive is not: nothing is skipped silently."""
    repo_root = _make_notices_repo(tmp_path)
    broken = _wheelhouse_of(repo_root) / "broken-1.0-py3-none-any.whl"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("broken/__init__.py", "")

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape(broken.name)):
        ARTIFACTS.build_workstation_archive(repo_root)
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME / ARTIFACTS.WORKSTATION_DIRNAME).exists()


@pytest.mark.parametrize("token", sorted(ARTIFACTS.RUNTIME_PLATFORMS))
def test_every_extension_member_name_is_version_and_target_free_and_one_the_sums_reader_accepts(token: str) -> None:
    """Over the committed pins: ``extensions/<publisher>.<name>.vsix`` and nothing else in the name."""
    install = workstation_install_module()
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)
    names = [
        f"{ARTIFACTS.WORKSTATION_EXTENSIONS_DIRNAME}/{ARTIFACTS.extension_payload_filename(ident)}"
        for ident, _, _, _ in ARTIFACTS.extension_pins(pins, token)
    ]
    names.append(ARTIFACTS.THIRD_PARTY_NOTICES_FILENAME)
    text = "".join(f"{'0' * 64}  {name}\n" for name in names)

    assert [name for _, name in install.parse_sha256sums(text)] == names
    targets = {row["vscode_target"] for row in ARTIFACTS.RUNTIME_PLATFORMS.values()}
    for name in names:
        assert "\\" not in name and ":" not in name and ".." not in name.split("/"), name
        assert not re.search(r"\d+\.\d+", name), f"{name} carries a version"
        assert not any(target in name for target in (*targets, *ARTIFACTS.RUNTIME_PLATFORMS)), name


@pytest.mark.parametrize("token", sorted(ARTIFACTS.RUNTIME_PLATFORMS))
def test_the_record_names_the_build_each_extension_took_for_its_own_platform(tmp_path: Path, token: str) -> None:
    """``RUNTIME.json``'s platform is the archive's token, and each extension build recorded is its own or universal."""
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(repo_root, token=token)

    record = json.loads((runtime / "RUNTIME.json").read_text(encoding="utf-8"))
    assert record["platform_token"] == token
    assert record["extensions"] == [
        {"id": ident, "version": version, "target": fixture_target(ident, token)}
        for ident, version, _ in FIXTURE_EXTENSIONS
    ]
    for entry in record["extensions"]:
        assert entry["target"] in (ARTIFACTS.UNIVERSAL_TARGET, ARTIFACTS.RUNTIME_PLATFORMS[token]["vscode_target"])


def _marketplace_version(version: str, target: str | None = None, *, pre_release: bool = False) -> dict:
    entry: dict = {"version": version, "properties": []}
    if pre_release:
        entry["properties"].append({"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "true"})
    if target is not None:
        entry["targetPlatform"] = target
    return entry


def _marketplace_answer() -> dict:
    """An ``extensionquery`` answer for the two fixture extensions, pre-releases newest as measured on the real ones."""
    return {
        "results": [
            {
                "extensions": [
                    {
                        "publisher": {"publisherName": "example"},
                        "extensionName": "universal-ext",
                        "versions": [
                            _marketplace_version("1.1.0", pre_release=True),
                            _marketplace_version("1.0.0"),
                            _marketplace_version("0.9.0"),
                        ],
                    },
                    {
                        "publisher": {"publisherName": "example"},
                        "extensionName": "platform-ext",
                        "versions": [
                            _marketplace_version("3.0.0", "linux-x64", pre_release=True),
                            *(
                                _marketplace_version("2.0.0", target)
                                for target in ("win32-x64", "linux-x64", "darwin-arm64", "linux-arm64", None)
                            ),
                            _marketplace_version("1.0.0"),
                        ],
                    },
                ]
            }
        ]
    }


def _elkjs_registry_metadata(tarball: bytes | None = None) -> dict:
    """What the npm registry answers for the fixture version: the tarball's URL and its sha512 ``integrity``."""
    body = fixture_elkjs_body() if tarball is None else tarball
    return {
        "name": "elkjs",
        "version": FIXTURE_ELKJS_VERSION,
        "dist": {
            "tarball": ARTIFACTS.elkjs_tarball_url(FIXTURE_ELKJS_VERSION),
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode("ascii"),
        },
    }


class _PinsFetcher:
    """Serves both publishers' sums files and the Marketplace's packages; records the URLs."""

    def __init__(
        self,
        *,
        drop_node_line: str | None = None,
        drop_python_line: str | None = None,
        elkjs_metadata: object = None,
        elkjs_tarball: bytes | None = None,
    ) -> None:
        self.drop_node_line = drop_node_line
        self.drop_python_line = drop_python_line
        self.elkjs_metadata = _elkjs_registry_metadata() if elkjs_metadata is None else elkjs_metadata
        self.elkjs_tarball = fixture_elkjs_body() if elkjs_tarball is None else elkjs_tarball
        self.urls: list[str] = []

    def __call__(self, url: str, destination: Path) -> None:
        self.urls.append(url)
        if url == ARTIFACTS.elkjs_metadata_url(FIXTURE_ELKJS_VERSION):
            destination.write_text(json.dumps(self.elkjs_metadata), encoding="utf-8", newline="\n")
        elif url.startswith(ARTIFACTS.NPM_REGISTRY_URL):
            destination.write_bytes(self.elkjs_tarball)
        elif url.endswith("/SHASUMS256.txt"):
            lines = [f"{'1' * 64}  node-v1.2.3-headers.tar.gz"]
            for token in ARCHIVE_TOKENS:
                name = ARTIFACTS.node_vendor_filename(FIXTURE_NODE_VERSION, token)
                if name != self.drop_node_line:
                    lines.append(f"{hashlib.sha256(fixture_node_body(token)).hexdigest()}  {name}")
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        elif url.endswith("/SHA256SUMS"):
            names = {
                token: ARTIFACTS.python_vendor_filename(FIXTURE_PYTHON_VERSION, FIXTURE_PYTHON_BUILD_TAG, token)
                for token in ARCHIVE_TOKENS
            }
            lines = [
                f"{hashlib.sha256(fixture_python_body(token)).hexdigest()}  {name}"
                for token, name in names.items()
                if name != self.drop_python_line
            ]
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        else:
            destination.write_bytes(_marketplace_body(url))


def _make_pins_repo(tmp_path: Path) -> Path:
    """A checkout whose pins name the fixture Node and CPython releases with stale digests and no extensions."""
    repo_root = tmp_path / "repo"
    write_pin_files(repo_root)
    stale = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    for section in ("node", "python"):
        for build in stale[section]["platforms"].values():
            build["sha256"] = "0" * 64
    stale["elkjs"]["sha256"] = "0" * 64
    del stale["extensions"]
    write_runtime_pins(repo_root, stale)
    manifest = repo_root / "vscode_extension" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"extensionPack": [ident for ident, _, _ in FIXTURE_EXTENSIONS]}), encoding="utf-8", newline="\n"
    )
    return repo_root


def test_update_pins_transcribes_the_publishers_digests_and_mints_each_companions_newest_release(
    tmp_path: Path,
) -> None:
    """What ``update-pins`` writes is exactly the pins the runtime cases build from."""
    repo_root = _make_pins_repo(tmp_path)
    expected_root = tmp_path / "expected"
    write_pin_files(expected_root)
    expected = json.loads((expected_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    expected["description"] = ARTIFACTS.RUNTIME_PINS_DESCRIPTION
    fetch = _PinsFetcher()
    asked: list[list[str]] = []

    def query(ids):
        asked.append(list(ids))
        return _marketplace_answer()

    path = ARTIFACTS.update_runtime_pins(repo_root, query=query, fetch=fetch)

    assert path == repo_root / ARTIFACTS.RUNTIME_PINS_FILE
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert list(json.loads(path.read_text(encoding="utf-8"))) == [
        "description",
        "node",
        "python",
        "elkjs",
        "extensions",
    ]
    assert "likec4" not in path.read_text(encoding="utf-8").lower(), "elkjs is touched, and no likec4"
    assert asked == [["example.universal-ext", "example.platform-ext"]], "the pack's ids, in the pack's order"
    packages = [url for url in fetch.urls if url.startswith(ARTIFACTS.MARKETPLACE_GALLERY_URL)]
    assert len(packages) == 4, "a universal build is downloaded once; a platform build once per platform"
    assert fetch.urls[:4] == [
        "https://nodejs.org/dist/v1.2.3/SHASUMS256.txt",
        f"{ARTIFACTS.PYTHON_RELEASE_URL}/{FIXTURE_PYTHON_BUILD_TAG}/SHA256SUMS",
        "https://registry.npmjs.org/elkjs/0.9.9",
        "https://registry.npmjs.org/elkjs/-/elkjs-0.9.9.tgz",
    ], "the elkjs metadata is read before the tarball it vouches for"
    # The round trip: the stages' own reader accepts the written file, and a runtime builds from it.
    assert ARTIFACTS.read_runtime_pins(repo_root) == expected
    assert sorted(path.name for path in _build_runtime(repo_root).iterdir()) == _RUNTIME_DIRECTORY_LISTING


def _answer_in_the_publishers_own_case() -> dict:
    """The answer with ``publisherName`` and ``extensionName`` as a publisher may capitalise them."""
    answer = _marketplace_answer()
    for extension in answer["results"][0]["extensions"]:
        extension["publisher"]["publisherName"] = "Example"
        extension["extensionName"] = extension["extensionName"].title()
    return answer


def test_update_pins_matches_the_packs_lowercase_ids_to_a_publishers_own_capitals(tmp_path: Path) -> None:
    """VS Code resolves ``publisher.name`` case-insensitively, and the pins keep the pack's spelling."""
    repo_root = _make_pins_repo(tmp_path)
    expected_root = tmp_path / "expected"
    write_pin_files(expected_root)
    expected = json.loads((expected_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    expected["description"] = ARTIFACTS.RUNTIME_PINS_DESCRIPTION

    path = ARTIFACTS.update_runtime_pins(
        repo_root, query=lambda ids: _answer_in_the_publishers_own_case(), fetch=_PinsFetcher()
    )

    assert json.loads(path.read_text(encoding="utf-8")) == expected


_SUMS_DIGEST = hashlib.sha256(b"a published file").hexdigest()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(f"{_SUMS_DIGEST}  node.zip", id="text-mode"),
        pytest.param(f"{_SUMS_DIGEST} *node.zip", id="binary-mode"),
        pytest.param(f"{_SUMS_DIGEST.upper()}  node.zip", id="uppercase-digest"),
        pytest.param(f"{_SUMS_DIGEST.upper()} *node.zip\r", id="uppercase-binary-crlf"),
    ],
)
def test_a_sums_line_is_read_in_each_spelling_a_sums_tool_writes(line: str) -> None:
    """``sha256sum`` marks a binary-mode name ``*``, and a digest may arrive in capitals; the pin is lowercase."""
    sums = "\n".join([f"{'1' * 64}  node.zip.sig", "not a sums line", line, ""])

    assert ARTIFACTS.published_digest(sums, "node.zip", "https://example.invalid/SUMS") == _SUMS_DIGEST


def test_a_sums_file_naming_the_file_only_beside_a_malformed_digest_is_refused_by_name() -> None:
    sums = f"{'1' * 64}  node.zip.sig\nnot-a-digest  node.zip\n{'2' * 63}  node.zip\n"

    with pytest.raises(
        ARTIFACTS.BuildArtifactsError, match=re.escape("https://example.invalid/SUMS lists no node.zip")
    ):
        ARTIFACTS.published_digest(sums, "node.zip", "https://example.invalid/SUMS")


def test_the_marketplace_query_asks_for_every_version_with_its_properties_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Marketplace query's request body, literally.

    ``flags`` is ``17`` -- ``IncludeVersions`` (``0x1``) and ``IncludeVersionProperties`` (``0x10``) -- and
    ``filterType`` ``7`` is ``ExtensionName``. Without ``0x10`` no version carries its ``properties``,
    :func:`newest_release` reads such a version as a release, and ``update-pins`` would silently pin the newest
    pre-release, as measured on the two Red Hat extensions. The literals are written here, not read off the module.
    """
    answer = json.dumps({"results": []}).encode("utf-8")
    requests = _serve_over_urlopen(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(answer) + answer)

    assert ARTIFACTS.query_marketplace(["redhat.vscode-yaml", "redhat.vscode-xml"]) == {"results": []}

    ((request, timeout),) = requests
    assert request.full_url == "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
    assert request.get_method() == "POST"
    assert request.get_header("Accept") == "application/json;api-version=3.0-preview.1"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {
        "filters": [
            {
                "criteria": [
                    {"filterType": 7, "value": "redhat.vscode-yaml"},
                    {"filterType": 7, "value": "redhat.vscode-xml"},
                ],
                "pageNumber": 1,
                "pageSize": 2,
                "sortBy": 0,
                "sortOrder": 0,
            }
        ],
        "flags": 17,
    }
    assert timeout == ARTIFACTS.DOWNLOAD_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"results"', id="cut-off"),
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nnot json!", id="not-json"),
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]", id="not-an-object"),
    ],
)
def test_a_marketplace_answer_that_cannot_be_used_is_a_refusal_naming_the_endpoint(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    _serve_over_urlopen(monkeypatch, raw)

    with pytest.raises(
        ARTIFACTS.BuildArtifactsError, match=re.escape(f"{ARTIFACTS.MARKETPLACE_GALLERY_URL}/extensionquery")
    ):
        ARTIFACTS.query_marketplace(["example.universal-ext"])


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nnot json!",
            f"could not query {ARTIFACTS.MARKETPLACE_GALLERY_URL}/extensionquery: "
            "Expecting value: line 1 column 1 (char 0)",
            id="not-json",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]",
            f"{ARTIFACTS.MARKETPLACE_GALLERY_URL}/extensionquery answered something that is not an object.",
            id="not-an-object",
        ),
    ],
)
def test_a_marketplace_answer_that_is_not_usable_json_is_refused_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float], raw: bytes, message: str
) -> None:
    """A 200 with bad JSON is content: the decoding sits outside the attempts, and its refusal counts none."""
    requests = _serve_in_turn(monkeypatch, raw, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.query_marketplace(["example.universal-ext"])

    assert str(excinfo.value) == message
    assert "attempt" not in str(excinfo.value)
    assert len(requests) == 1
    assert retry_waits == []


def test_a_marketplace_query_cut_off_is_retried(monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]) -> None:
    answer = b'{"results": []}'
    requests = _serve_in_turn(
        monkeypatch,
        b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"results"',
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(answer) + answer,
    )

    assert ARTIFACTS.query_marketplace(["example.universal-ext"]) == {"results": []}
    assert len(requests) == 2
    assert retry_waits == [1]


def test_a_marketplace_query_that_fails_every_time_names_its_attempts(
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float]
) -> None:
    _serve_in_turn(monkeypatch, _http_error(502, "Bad Gateway"))

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.query_marketplace(["example.universal-ext"])

    assert str(excinfo.value) == (
        f"could not query {ARTIFACTS.MARKETPLACE_GALLERY_URL}/extensionquery: HTTP Error 502: Bad Gateway (4 attempts)"
    )


def test_a_version_that_declares_no_properties_is_a_release() -> None:
    """The Marketplace omits ``properties`` on a version that has none; only a declared pre-release is skipped."""
    extension = {
        "versions": [
            {"version": "2.0.0"},
            {"version": "3.0.0", "properties": None},
            {"version": "4.0.0", "properties": [{"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "True"}]},
        ]
    }

    assert ARTIFACTS.newest_release(extension, "example.ext") == ("3.0.0", frozenset({"universal"}))


def test_the_newest_release_skips_pre_releases_and_is_decided_by_the_versions_numbers() -> None:
    extension = {
        "versions": [
            _marketplace_version("11.0.0", pre_release=True),
            _marketplace_version("9.0.0"),
            _marketplace_version("10.0.0", "linux-x64"),
            _marketplace_version("10.0.0"),
            _marketplace_version("not-a-version"),
        ]
    }

    assert ARTIFACTS.newest_release(extension, "example.ext") == ("10.0.0", frozenset({"linux-x64", "universal"}))


def _answer_without_platform_ext() -> dict:
    answer = _marketplace_answer()
    answer["results"][0]["extensions"].pop()
    return answer


def _answer_with_only_pre_releases() -> dict:
    answer = _marketplace_answer()
    answer["results"][0]["extensions"][0]["versions"] = [_marketplace_version("1.1.0", pre_release=True)]
    return answer


def _answer_with_no_build_for_a_platform() -> dict:
    answer = _marketplace_answer()
    answer["results"][0]["extensions"][1]["versions"] = [_marketplace_version("2.0.0", "linux-arm64")]
    return answer


@pytest.mark.parametrize(
    ("answer", "fetch_options", "named"),
    [
        (_answer_without_platform_ext, {}, "answered nothing for example.platform-ext"),
        (_answer_with_only_pre_releases, {}, "publishes no release version of example.universal-ext"),
        (_answer_with_no_build_for_a_platform, {}, "neither a linux-x64 build nor a universal one"),
        (_marketplace_answer, {"drop_node_line": "node-v1.2.3-win-x64.zip"}, "lists no node-v1.2.3-win-x64.zip"),
        (
            _marketplace_answer,
            {
                "drop_python_line": ARTIFACTS.python_vendor_filename(
                    FIXTURE_PYTHON_VERSION, FIXTURE_PYTHON_BUILD_TAG, "linux-x64"
                )
            },
            f"SHA256SUMS lists no cpython-{FIXTURE_PYTHON_VERSION}+{FIXTURE_PYTHON_BUILD_TAG}-x86_64-unknown-linux-gnu",
        ),
        (lambda: {}, {}, "answered in a shape this cannot read"),
        (lambda: {"results": []}, {}, "answered in a shape this cannot read"),
        (lambda: {"results": [{"extensions": [{"publisher": "example"}]}]}, {}, "answered in a shape this cannot read"),
        (lambda: {"results": [{"extensions": None}]}, {}, "answered in a shape this cannot read"),
    ],
)
def test_update_pins_refuses_by_name_and_leaves_the_committed_pins_untouched(
    tmp_path: Path, answer, fetch_options: dict, named: str
) -> None:
    repo_root = _make_pins_repo(tmp_path)
    before = (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes()

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.update_runtime_pins(repo_root, query=lambda ids: answer(), fetch=_PinsFetcher(**fetch_options))

    assert named in str(excinfo.value)
    assert (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes() == before


def test_update_pins_needs_the_releases_it_transcribes_named_first(tmp_path: Path) -> None:
    repo_root = _make_pins_repo(tmp_path)
    pins = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    pins["node"]["version"] = "22"
    write_runtime_pins(repo_root, pins)
    fetch = _PinsFetcher()

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="edit them by hand"):
        ARTIFACTS.update_runtime_pins(repo_root, query=lambda ids: _marketplace_answer(), fetch=fetch)
    assert fetch.urls == []


def test_main_update_pins_flag_rewrites_and_reports(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    rewritten: list[Path] = []

    def fake_update(repo_root: Path) -> Path:
        rewritten.append(repo_root)
        return repo_root / ARTIFACTS.RUNTIME_PINS_FILE

    monkeypatch.setattr(ARTIFACTS, "update_runtime_pins", fake_update)

    assert ARTIFACTS.main(["--repo-root", str(tmp_path), "--update-pins"]) == 0
    assert rewritten == [tmp_path.resolve()]
    assert "Wrote" in capsys.readouterr().out


@pytest.mark.parametrize("other", ["--wheelhouse", "--docs-only", "--workstation-archive", "--runtime", "--elkjs"])
def test_main_refuses_the_update_pins_flag_beside_another_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    """A usage error (exit 2) before any mode runs: no pins file is rewritten and nothing is built."""
    called = _record_every_mode(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        ARTIFACTS.main(["--repo-root", str(tmp_path), "--update-pins", other])

    assert excinfo.value.code == 2
    assert called == []
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The retired ``bundled`` key, and the lock the wheelhouse is resolved under
# ---------------------------------------------------------------------------


def test_the_committed_pins_carry_no_bundled_entry_and_the_description_names_none() -> None:
    """The committed pins carry no ``bundled`` entry, and the description names none.

    The specimen generator is built from source, so nothing is pinned from the release store's private area.
    """
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)

    assert ARTIFACTS.RETIRED_RUNTIME_PINS_KEY not in pins
    for retired in ("bundled", "private", "Carchetype", "SHA256SUMS;"):
        assert retired not in ARTIFACTS.RUNTIME_PINS_DESCRIPTION, retired


def _runtime_pins_fixture() -> dict:
    """The synthetic pins :func:`write_pin_files` writes, as an object."""
    return {
        "description": "synthetic",
        "node": {
            "version": FIXTURE_NODE_VERSION,
            "platforms": {token: {"sha256": "0" * 64} for token in ARCHIVE_TOKENS},
        },
        "python": {
            "version": FIXTURE_PYTHON_VERSION,
            "build_tag": FIXTURE_PYTHON_BUILD_TAG,
            "platforms": {token: {"sha256": "0" * 64} for token in ARCHIVE_TOKENS},
        },
        "elkjs": {"version": FIXTURE_ELKJS_VERSION, "sha256": "0" * 64},
        "extensions": [
            {
                "id": ident,
                "version": version,
                "platforms": {
                    token: {"target": fixture_target(ident, token), "sha256": "0" * 64} for token in ARCHIVE_TOKENS
                },
            }
            for ident, version, _ in FIXTURE_EXTENSIONS
        ],
    }


def test_a_pins_object_that_still_carries_bundled_is_refused_by_name() -> None:
    """Refused, never ignored: a hand-edited or stale pins file would otherwise pin a wheel nothing reads."""
    assert ARTIFACTS.check_runtime_pins(_runtime_pins_fixture())
    pins = {**_runtime_pins_fixture(), "bundled": []}

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.check_runtime_pins(pins)

    message = str(excinfo.value)
    assert "'bundled'" in message and "retired" in message and "python scripts/dev.py artifacts" in message


def test_update_pins_refuses_a_bundled_key_before_anything_is_fetched(tmp_path: Path) -> None:
    repo_root = _make_pins_repo(tmp_path)
    pins = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    pins["bundled"] = []
    write_runtime_pins(repo_root, pins)
    before = (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes()
    fetch = _PinsFetcher()

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="'bundled', which is retired"):
        ARTIFACTS.update_runtime_pins(repo_root, query=lambda ids: _marketplace_answer(), fetch=fetch)
    assert fetch.urls == []
    assert (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes() == before


def _lock_pin(constraints: str, name: str) -> list[str]:
    """The lines of *constraints* that pin *name*, by canonical name."""
    return [line for line in constraints.splitlines() if ARTIFACTS.requirement_name(line) == name]


_RANGE_CLAUSE = re.compile(r"^(>=|<)(\d+(?:\.\d+)*)$")
_NUMERIC_RELEASE = re.compile(r"^\d+(?:\.\d+)*$")


def _release(version: str) -> tuple[int, ...]:
    """A plain numeric release, padded so ``2.4`` and ``2.4.0`` compare equal; any other form fails the test by name."""
    assert _NUMERIC_RELEASE.match(version), f"{version!r} is not a plain numeric release"
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (4 - len(parts))


def _range_bounds(specifier: str) -> dict[str, str]:
    """The ``>=A,<B`` *specifier* as ``{">=": A, "<": B}``; any other set of clauses fails the test by name."""
    matches = [_RANGE_CLAUSE.match(clause) for clause in specifier.split(",")]
    clauses = [match.groups() for match in matches if match]
    assert len(clauses) == len(matches) == 2, f"{specifier!r} is not two '>=' / '<' clauses over a numeric release"
    bounds = dict(clauses)
    assert sorted(bounds) == ["<", ">="], f"{specifier!r} is not one bounded '>=A,<B' range"
    return bounds


def test_the_wheelhouse_constraints_pin_numpy_scipy_and_pyyaml_at_the_locks_runtime_versions() -> None:
    """The constraints rendered from the committed lock pin numpy, scipy and PyYAML at the runtime's versions.

    For the runtime's CPython the three distributions the specimens' bytes rest on are pinned once each, at the
    version the lock resolves for that interpreter. The lock alone pins numpy and scipy: the generator declares a
    bounded range for each, and the lock's version lies inside it.
    """
    runtime = ARTIFACTS.runtime_python_version(REPO_ROOT)
    assert runtime.startswith("3.11."), "the workstation runtime is CPython 3.11, the specimens' measured one"
    lock = (REPO_ROOT / ARTIFACTS.DEV_LOCK_FILE).read_text(encoding="utf-8")
    constraints = ARTIFACTS.lock_constraints(REPO_ROOT)
    assert constraints == ARTIFACTS.lock_constraints_text(lock, runtime)

    generator = tomllib.loads((REPO_ROOT / "packages" / "specimens" / "pyproject.toml").read_text(encoding="utf-8"))
    declared: dict[str, list[str]] = {}
    for requirement in generator["project"]["dependencies"]:
        declared.setdefault(ARTIFACTS.requirement_name(requirement), []).append(requirement.replace(" ", ""))
    ranges: dict[str, dict[str, str]] = {}
    for name in ("numpy", "scipy"):
        (requirement,) = declared[name]
        assert ";" not in requirement, f"{name} is declared for every platform: {requirement}"
        project_name, specifier = re.split(r"(?=[<>=!~])", requirement, maxsplit=1)
        assert ARTIFACTS.requirement_name(project_name) == name, (name, requirement)
        ranges[name] = _range_bounds(specifier)
    for name in ("numpy", "scipy", "pyyaml"):
        (line,) = _lock_pin(constraints, name)
        assert ";" not in line, f"{name} is pinned for every platform of the runtime"
        locked = [entry for entry in lock.splitlines() if ARTIFACTS.requirement_name(entry) == name]
        assert any(entry.startswith(line) for entry in locked), (name, line, locked)
        if name in ranges:
            pinned = line.split("==", 1)[1]
            low, high = ranges[name][">="], ranges[name]["<"]
            assert _release(low) <= _release(pinned) < _release(high), (name, pinned, ranges[name])


# ---------------------------------------------------------------------------
# The elkjs payload: pinned, staged by `install` and `runtime`, checked by `artifacts`
# ---------------------------------------------------------------------------


def test_the_committed_pins_pin_elkjs_by_version_and_digest() -> None:
    """The committed elkjs pin, by version and digest: one tarball for every platform."""
    pins = ARTIFACTS.read_runtime_pins(REPO_ROOT)

    assert pins["elkjs"] == {
        "version": "0.12.0",
        "sha256": "c1d7719723e020b10724e3ccbc935696a2884f1e78d361cdd03766309e8e8e2a",
    }
    assert list(pins) == ["description", "node", "python", "elkjs", "extensions"], "update-pins' own key order"
    assert pins["description"] == ARTIFACTS.RUNTIME_PINS_DESCRIPTION, "the file is update-pins' output"


def test_the_payload_is_the_five_files_the_layout_needs() -> None:
    assert ARTIFACTS.ELKJS_PAYLOAD_FILES == (
        "LICENSE.md",
        "lib/elk-api.js",
        "lib/elk-worker.min.js",
        "lib/main.js",
        "package.json",
    )
    assert ARTIFACTS.PACKAGE_ELKJS_DIR.as_posix() == "packages/tools/src/swcraft_tools/writer/diagrams/elkjs"


@pytest.mark.parametrize(
    ("mutate", "named"),
    [
        (lambda pins: pins.pop("elkjs"), "no 'elkjs' object"),
        (lambda pins: pins["elkjs"].update(version="^0.12.0"), "elkjs.version must be an exact X.Y.Z"),
        (lambda pins: pins["elkjs"].update(sha256="ABC"), "elkjs.sha256 must be 64 lowercase"),
    ],
    ids=["absent", "a-range", "a-bad-digest"],
)
def test_an_unfit_elkjs_pin_is_refused_by_name_before_anything_is_fetched(tmp_path: Path, mutate, named: str) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    pins = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    mutate(pins)
    write_runtime_pins(repo_root, pins)
    fetch = _RuntimeFetcher(fixture_node_body("macos-arm64"))

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=re.escape(named)):
        ARTIFACTS.install_elkjs_payload(repo_root, fetch=fetch)
    assert fetch.urls == []


def test_install_stages_exactly_the_five_files_of_the_pinned_tarball(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    fetch = _RuntimeFetcher(b"unused")

    payload = ARTIFACTS.install_elkjs_payload(repo_root, fetch=fetch)

    assert payload == repo_root / ARTIFACTS.PACKAGE_ELKJS_DIR
    assert fetch.urls == ["https://registry.npmjs.org/elkjs/-/elkjs-0.9.9.tgz"]
    staged = _directory_bytes(payload)
    assert sorted(staged) == list(ARTIFACTS.ELKJS_PAYLOAD_FILES), "no README, no fourth lib file, no nested tree"
    with tarfile.open(fileobj=io.BytesIO(fixture_elkjs_body())) as bundle:
        for relative, content in staged.items():
            assert content == bundle.extractfile(f"package/{relative}").read(), f"{relative} is not byte for byte"
    assert not payload.with_name("elkjs.incoming").exists()
    assert not payload.with_name("elkjs.previous").exists()
    assert ARTIFACTS.require_elkjs_payload(repo_root) == payload


def test_install_replaces_a_previous_payload_whole(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    stale = stage_fixture_elkjs_payload(repo_root, version="0.1.0")
    (stale / "lib" / "leftover.js").write_bytes(b"from an older release")

    ARTIFACTS.install_elkjs_payload(repo_root, fetch=_RuntimeFetcher(b"unused"))

    assert sorted(_directory_bytes(stale)) == list(ARTIFACTS.ELKJS_PAYLOAD_FILES)
    assert json.loads((stale / "package.json").read_text(encoding="utf-8"))["version"] == FIXTURE_ELKJS_VERSION


def test_a_tarball_whose_digest_is_not_the_pinned_one_is_refused_and_leaves_the_previous_payload(
    tmp_path: Path,
) -> None:
    repo_root = _make_runtime_repo(tmp_path)
    previous = stage_fixture_elkjs_payload(repo_root)
    before = _directory_bytes(previous)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.install_elkjs_payload(repo_root, fetch=_RuntimeFetcher(b"unused", elkjs=b"not the registry's bytes"))

    message = str(excinfo.value)
    assert "elkjs-0.9.9.tgz from https://registry.npmjs.org/elkjs/-/elkjs-0.9.9.tgz" in message
    assert hashlib.sha256(fixture_elkjs_body()).hexdigest() in message
    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in message
    assert _directory_bytes(previous) == before
    assert not previous.with_name("elkjs.incoming").exists()


def test_a_tarball_of_another_version_is_refused_even_when_its_digest_is_pinned(tmp_path: Path) -> None:
    """The version check is the second guard: a pins file whose digest and version disagree is a commit's defect."""
    repo_root = _make_runtime_repo(tmp_path)
    body = fixture_elkjs_body(version="0.9.8")
    pins = json.loads((repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
    pins["elkjs"]["sha256"] = hashlib.sha256(body).hexdigest()
    write_runtime_pins(repo_root, pins)
    previous = stage_fixture_elkjs_payload(repo_root)
    before = _directory_bytes(previous)

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="declares version '0.9.8'"):
        ARTIFACTS.install_elkjs_payload(repo_root, fetch=_RuntimeFetcher(b"unused", elkjs=body))

    assert _directory_bytes(previous) == before
    assert not previous.with_name("elkjs.incoming").exists()


def test_a_tarball_missing_a_payload_file_is_refused_by_name(tmp_path: Path) -> None:
    tarball = tmp_path / "elkjs-0.9.9.tgz"
    with tarfile.open(tarball, "w:gz") as bundle:
        info = tarfile.TarInfo("package/lib/main.js")
        bundle.addfile(info, io.BytesIO(b""))

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=r"no regular file package/LICENSE\.md"):
        ARTIFACTS.unpack_elkjs_payload(tarball, tmp_path / "out")


def _extra_payload_file(repo_root: Path) -> None:
    (stage_fixture_elkjs_payload(repo_root) / "README.md").write_bytes(b"x")


def _missing_payload_file(repo_root: Path) -> None:
    (stage_fixture_elkjs_payload(repo_root) / "lib" / "main.js").unlink()


@pytest.mark.parametrize(
    ("prepare", "named"),
    [
        (lambda repo_root: None, "is missing"),
        (_extra_payload_file, "not exactly LICENSE.md"),
        (_missing_payload_file, "not exactly LICENSE.md"),
        (lambda repo_root: stage_fixture_elkjs_payload(repo_root, version="0.1.0"), "holds elkjs '0.1.0'"),
    ],
    ids=["absent", "an-extra-file", "a-missing-file", "a-stale-version"],
)
def test_artifacts_refuses_a_payload_that_is_not_the_pinned_one_by_name(tmp_path: Path, prepare, named: str) -> None:
    repo_root = make_source_repo(tmp_path, elkjs=False)
    prepare(repo_root)
    runner = FakeRunner(repo_root)

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.build_all(repo_root, run=runner, npm="npm")

    message = str(excinfo.value)
    assert named in message
    assert "elkjs" in message
    assert "python scripts/dev.py install" in message
    assert runner.calls == [], "refused before the first build step"
    assert not (repo_root / ARTIFACTS.DIST_DIRNAME).exists(), "refused before dist/ is touched"


def test_artifacts_fetches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The payload is checked by ``artifacts`` and never fetched there."""

    def no_network(*_args, **_kwargs):
        raise AssertionError("artifacts reached for the network")

    monkeypatch.setattr(ARTIFACTS, "_download", no_network)
    monkeypatch.setattr(ARTIFACTS.urllib.request, "urlopen", no_network)
    repo_root = make_source_repo(tmp_path)

    ARTIFACTS.build_all(repo_root, run=FakeRunner(repo_root), npm="npm")


def test_a_runtime_build_stages_the_elkjs_payload_and_notices_it_as_riding_the_wheel(tmp_path: Path) -> None:
    repo_root = _make_runtime_repo(tmp_path)

    runtime = _build_runtime(repo_root)

    assert ARTIFACTS.require_elkjs_payload(repo_root) == repo_root / ARTIFACTS.PACKAGE_ELKJS_DIR
    assert sorted(path.name for path in runtime.iterdir()) == _RUNTIME_DIRECTORY_LISTING, "no elkjs file in runtime/"
    notices = (runtime / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert "inside the `swcraft-tools`\nwheel it installs" in notices, "the preamble covers the wheel too"
    section = notices.split(f"## elkjs {FIXTURE_ELKJS_VERSION}\n", 1)[1].split("\n## ", 1)[0]
    assert "`swcraft_tools/writer/diagrams/elkjs/`" in section
    assert "`swcraft-tools` wheel" in section
    assert f"Declared licence: {FIXTURE_ELKJS_LICENCE}" in section.splitlines()
    assert "SWCraft distributes it under EPL-2.0. Its licence text, from the `LICENSE.md` file" in section
    assert "Eclipse Public License - v 2.0 (the fixture's text)" in section, "the text is read out of the payload"


def test_a_payload_that_no_longer_offers_epl_is_refused_rather_than_relabelled(tmp_path: Path) -> None:
    tree = stage_fixture_elkjs_payload(tmp_path)
    (tree / "package.json").write_text(
        json.dumps({"version": "1.0.0", "license": "GPL-3.0-or-later"}), encoding="utf-8", newline="\n"
    )

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match="does not offer EPL-2.0"):
        ARTIFACTS.read_elkjs_notice(tree)


def test_update_pins_computes_the_elkjs_digest_the_runtime_then_verifies(tmp_path: Path) -> None:
    repo_root = _make_pins_repo(tmp_path)

    ARTIFACTS.update_runtime_pins(repo_root, query=lambda ids: _marketplace_answer(), fetch=_PinsFetcher())

    pins = ARTIFACTS.read_runtime_pins(repo_root)
    assert pins["elkjs"] == {
        "version": FIXTURE_ELKJS_VERSION,
        "sha256": hashlib.sha256(fixture_elkjs_body()).hexdigest(),
    }
    ARTIFACTS.install_elkjs_payload(repo_root, fetch=_RuntimeFetcher(b"unused"))


_FOREIGN_TARBALL_METADATA = {"dist": {"tarball": "https://example.invalid/elkjs.tgz", "integrity": "sha512-x"}}
_SHA1_METADATA = {"dist": {"tarball": "https://registry.npmjs.org/elkjs/-/elkjs-0.9.9.tgz", "integrity": "sha1-abc"}}


@pytest.mark.parametrize(
    ("fetch_options", "named"),
    [
        ({"elkjs_tarball": b"a tarball the registry did not publish"}, "has integrity sha512-"),
        ({"elkjs_metadata": _FOREIGN_TARBALL_METADATA}, "names the tarball 'https://example.invalid/elkjs.tgz'"),
        ({"elkjs_metadata": _SHA1_METADATA}, "publishes no sha512 integrity"),
        ({"elkjs_metadata": ["not", "an", "object"]}, "answered no readable dist.tarball"),
    ],
    ids=["a-tarball-the-integrity-refuses", "a-foreign-tarball-url", "no-sha512", "unreadable-metadata"],
)
def test_update_pins_refuses_an_elkjs_tarball_the_registry_does_not_vouch_for(
    tmp_path: Path, fetch_options: dict, named: str
) -> None:
    repo_root = _make_pins_repo(tmp_path)
    before = (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes()

    with pytest.raises(ARTIFACTS.BuildArtifactsError) as excinfo:
        ARTIFACTS.update_runtime_pins(
            repo_root, query=lambda ids: _marketplace_answer(), fetch=_PinsFetcher(**fetch_options)
        )

    assert named in str(excinfo.value)
    assert (repo_root / ARTIFACTS.RUNTIME_PINS_FILE).read_bytes() == before


def test_main_elkjs_flag_stages_and_reports(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    staged: list[Path] = []

    def fake_install(repo_root: Path) -> Path:
        staged.append(repo_root)
        return repo_root / ARTIFACTS.PACKAGE_ELKJS_DIR

    monkeypatch.setattr(ARTIFACTS, "install_elkjs_payload", fake_install)

    assert ARTIFACTS.main(["--repo-root", str(tmp_path), "--elkjs"]) == 0
    assert staged == [tmp_path.resolve()]
    assert "Staged" in capsys.readouterr().out


def test_the_elkjs_payload_and_its_sidecars_are_ignored_and_the_sidecars_ship_in_no_archive() -> None:
    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    parent = ARTIFACTS.PACKAGE_ELKJS_DIR.parent
    names = (
        ARTIFACTS.PACKAGE_ELKJS_DIRNAME,
        ARTIFACTS.PACKAGE_ELKJS_STAGING_DIRNAME,
        ARTIFACTS.PACKAGE_ELKJS_SUPERSEDED_DIRNAME,
    )
    for dirname in names:
        assert f"/{parent.as_posix()}/{dirname}/" in entries
    config = tomllib.loads((REPO_ROOT / "packages" / "tools" / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["exclude"]
    package_parent = parent.relative_to(ARTIFACTS.PACKAGES_DIR / "tools").as_posix()
    for dirname in names[1:]:
        assert f"/{package_parent}/{dirname}" in excluded
    assert f"/{package_parent}/{names[0]}" not in excluded, "the payload itself must ship"
    assert set(names) <= ARTIFACTS.IGNORED_SOURCE_DIRS, "package data the build stages, not a source of it"
    assert ARTIFACTS.RUNTIME_PINS_FILE.as_posix() in ARTIFACTS.ARTIFACT_SOURCE_FILES, "a bump marks dist/ stale"


# ---------------------------------------------------------------------------
# End-to-end (deselected from the fast run; executed by `dev.py test-artifacts`)
# ---------------------------------------------------------------------------


@pytest.mark.artifact
def test_build_artifacts_produces_the_full_local_artifact_set() -> None:
    """Really build the four wheels + sdists (the specimen generator's the fifth artifact pair) and the ``.vsix``.

    Provisioning is allowed to use the network here (``python -m build`` creates an
    isolated build env, ``npm ci`` restores the locked tree); the *assertions* only
    read local files.
    """
    manifest_path = ARTIFACTS.build_all(REPO_ROOT)
    dist_root = REPO_ROOT / ARTIFACTS.DIST_DIRNAME

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = ARTIFACTS.read_version(REPO_ROOT)
    assert manifest["version"] == version

    python_dir = dist_root / ARTIFACTS.PYTHON_DIRNAME
    assert len(list(python_dir.glob("*.whl"))) == 4
    assert len(list(python_dir.glob("*.tar.gz"))) == 4
    assert (python_dir / f"swcraft_specimens-{version}-py3-none-any.whl").is_file(), "the generator's wheel"
    assert len(list((dist_root / ARTIFACTS.VSCODE_DIRNAME).glob("*.vsix"))) == 1
    assert len(list((dist_root / ARTIFACTS.DOCS_DIRNAME).glob("*.zip"))) == 1

    # Every wheel carries the root licence and notice in its `.dist-info/licenses/`, every sdist at its root, and the
    # .vsix both (vsce renames the licence `LICENSE.txt`), each byte-equal to the root file.
    root_files = {name: (REPO_ROOT / name).read_bytes() for name in ARTIFACTS.LICENCE_FILENAMES}
    for wheel in sorted(python_dir.glob("*.whl"), key=lambda path: path.as_posix()):
        with zipfile.ZipFile(wheel) as opened:
            for name, content in root_files.items():
                (member,) = [m for m in opened.namelist() if m.endswith(f".dist-info/licenses/{name}")]
                assert opened.read(member) == content, f"{wheel.name}: {member} is not the root {name}"
    for sdist in sorted(python_dir.glob("*.tar.gz"), key=lambda path: path.as_posix()):
        with tarfile.open(sdist) as opened:
            for name, content in root_files.items():
                (member,) = [m for m in opened.getnames() if m.count("/") == 1 and m.endswith(f"/{name}")]
                extracted = opened.extractfile(member)
                assert extracted is not None and extracted.read() == content, f"{sdist.name}: {member}"
    # The agent wheel carries the tracked system prompt, byte-equal.
    with zipfile.ZipFile(python_dir / f"swcraft_agent-{version}-py3-none-any.whl") as opened:
        assert opened.read("swcraft_agent/agent-system-prompt.md") == (REPO_ROOT / AGENT_PACKAGE_PROMPT).read_bytes()
    (vsix,) = (dist_root / ARTIFACTS.VSCODE_DIRNAME).glob("*.vsix")
    with zipfile.ZipFile(vsix) as opened:
        assert opened.read("extension/LICENSE.txt") == root_files["LICENSE"]
        assert opened.read("extension/NOTICE") == root_files["NOTICE"]

    assert {entry["name"] for entry in manifest["artifacts"]} == {
        "swcraft-tools",
        "swcraft-mcp",
        "swcraft-agent",
        "swcraft-specimens",
        "swcraft-vscode",
        "swcraft-guide",
    }

    # Every recorded digest matches the file actually on disk.
    for entry in manifest["artifacts"]:
        path = dist_root / ARTIFACTS.artifact_relative_path(entry)
        assert path.is_file()
        assert entry["sha256"] == ARTIFACTS.sha256_file(path)
        assert entry["size"] == path.stat().st_size

    sums = (dist_root / ARTIFACTS.SHA256SUMS_FILENAME).read_text(encoding="utf-8")
    assert len(sums.splitlines()) == len(manifest["artifacts"]) == 10

    # The guide ships as one archive, and it is a real rendered site rather than
    # whatever a `mkdocs build` that wrote nothing would have left behind.
    (docs_entry,) = [entry for entry in manifest["artifacts"] if entry["kind"] == "docs"]
    archive = dist_root / ARTIFACTS.artifact_relative_path(docs_entry)
    assert archive.name == f"swcraft-guide-{version}.zip"
    assert f"{docs_entry['sha256']}  {ARTIFACTS.DOCS_DIRNAME}/{archive.name}\n" in sums
    with zipfile.ZipFile(archive) as opened:
        members = opened.namelist()
    root = f"swcraft-guide-{version}"
    assert f"{root}/index.html" in members
    assert ARTIFACTS.DOCS_ARCHIVE_README in members, "the archive must say what it is and that it has to be served"
    assert all(name.startswith(f"{root}/") for name in members if name != ARTIFACTS.DOCS_ARCHIVE_README), (
        "the rendered site must unpack into one directory; only the note sits beside it"
    )
    assert members == sorted(members), "member order is pinned so the archive is reproducible"

    # The wheelhouse is machine-local provisioning, not a release artifact.
    assert not any(
        ARTIFACTS.WHEELHOUSE_DIRNAME in ARTIFACTS.artifact_relative_path(entry) for entry in manifest["artifacts"]
    )
    assert ARTIFACTS.WHEELHOUSE_DIRNAME not in manifest_path.read_text(encoding="utf-8")


@pytest.mark.artifact
def test_the_built_wheel_carries_the_rendered_guide(tmp_path: Path) -> None:
    """The guide payload, asserted against a real wheel rather than a layout.

    The payload is gitignored at the repository root while the wheel's project root is ``packages/tools/``, a
    combination the "no force-include needed" comment in ``packages/tools/pyproject.toml`` says nothing about, so it is
    measured. The render is real and installed into the checkout as ``build_all`` does, so the case does not depend on
    ``dev.py artifacts`` having run. ``search/search_index.json`` is asserted because it is one level down: a
    packaging that shipped only the payload's root would pass on ``index.html``.

    It leaves ``docs/guide/version.md`` and the payload tree in the checkout: both are gitignored build products, and
    removing the payload would delete one the developer's own ``dev.py artifacts`` had installed. That shared state is
    safe because the ``artifact`` stage runs sequentially.
    """
    site_dir = ARTIFACTS.build_docs_site(REPO_ROOT, python_executable=sys.executable, site_dir=tmp_path / "site")
    ARTIFACTS.install_guide_site_payload(REPO_ROOT, site_dir)

    outdir = tmp_path / "wheel"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT / "packages" / "tools")],
        cwd=str(REPO_ROOT),
        check=True,
    )
    (wheel,) = sorted(outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "swcraft_tools/guide_site/index.html" in names, (
        "the wheel carries no guide payload -- `swcraft docs` would refuse on every pip install; "
        "the fallback is [tool.hatch.build.targets.wheel] artifacts = ['swcraft_tools/guide_site/**']"
    )
    assert "swcraft_tools/guide_site/search/search_index.json" in names, (
        "the payload's subdirectories did not ship, so the served guide would not search"
    )
    assert f"swcraft_tools/guide_site/{ARTIFACTS.GUIDE_CORPUS_FILENAME}" in names, (
        "the wheel carries no guide corpus -- the assistant's `guide` tool would answer guide-missing"
    )


@pytest.mark.artifact
def test_the_built_wheel_carries_the_elkjs_payload(tmp_path: Path) -> None:
    """Against a real wheel: the ignored payload under ``writer/diagrams/elkjs/`` ships.

    The payload is staged into the checkout as ``dev.py install`` does (network use here is provisioning, like
    ``python -m build``'s isolated environment) and left there: a build product the developer's install would write.
    """
    ARTIFACTS.install_elkjs_payload(REPO_ROOT)

    outdir = tmp_path / "wheel"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT / "packages" / "tools")],
        cwd=str(REPO_ROOT),
        check=True,
    )
    (wheel,) = sorted(outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())

    prefix = "swcraft_tools/writer/diagrams/elkjs/"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        carried = sorted(name.removeprefix(prefix) for name in names if name.startswith(prefix))

    assert carried == list(ARTIFACTS.ELKJS_PAYLOAD_FILES), "the wheel carries exactly the five payload files"
    # The bridge's Node script is committed package data beside the payload; `layout.py` imports it by the path
    # `elkjs/lib/main.js` relative to itself.
    assert "swcraft_tools/writer/diagrams/elk-layout.mjs" in names, "the wheel carries the layout script"


@pytest.mark.artifact
def test_the_built_wheel_carries_the_passport_schema(tmp_path: Path) -> None:
    """The passport schema is package data beside the pass, so an installed toolchain reads it.

    ``swcraft_tools.passport.schema_text`` reads it through ``importlib.resources``; a wheel without
    it would leave the pass's schema behind in the checkout. Committed under ``packages``, so it
    needs no payload staged first.
    """
    outdir = tmp_path / "wheel"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT / "packages" / "tools")],
        cwd=str(REPO_ROOT),
        check=True,
    )
    (wheel,) = sorted(outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "swcraft_tools/model_passport.schema.json" in names, "the wheel carries no passport schema"
    assert "swcraft_tools/passport.py" in names, "the wheel carries no passport pass"


@pytest.mark.artifact
def test_no_sidecar_of_the_payload_rewrite_rides_the_wheel_or_the_sdist(tmp_path: Path) -> None:
    """No sidecar of the payload rewrite rides the wheel or the sdist, against real archives.

    Hatchling ships everything under ``packages``, so ``guide_site.incoming/`` or ``guide_site.outgoing/`` would ship
    if present at build time -- and one can be: ``install_guide_site_payload`` runs just before
    ``build_python_artifacts`` and its last step, removing the superseded tree, is best-effort.
    ``IGNORED_SOURCE_DIRS`` governs the source fingerprint, not packaging. Both sidecars are planted with a file one
    level down too, since an exclusion matching only the directory entry would still carry the contents.

    Two builds: the bare ``python -m build`` is what a release runs, but it builds the wheel from the sdist, so the
    direct ``--wheel`` build is what shows the shared ``[tool.hatch.build]`` table reaches the wheel target. Any
    sidecar already present is removed first, and both are removed afterwards.
    """
    package_dir = REPO_ROOT / ARTIFACTS.PACKAGE_GUIDE_SITE_DIR.parent
    sidecars = [
        package_dir / ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME,
        package_dir / ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME,
    ]
    try:
        for sidecar in sidecars:
            shutil.rmtree(sidecar, ignore_errors=True)
            (sidecar / "search").mkdir(parents=True)
            (sidecar / ARTIFACTS.SITE_INDEX_FILENAME).write_text("<html>stale</html>", encoding="utf-8")
            (sidecar / "search" / "search_index.json").write_text("{}", encoding="utf-8")

        release_outdir = tmp_path / "dist"
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(release_outdir), str(REPO_ROOT / "packages" / "tools")],
            cwd=str(REPO_ROOT),
            check=True,
        )
        wheel_outdir = tmp_path / "wheel"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(wheel_outdir),
                str(REPO_ROOT / "packages" / "tools"),
            ],
            cwd=str(REPO_ROOT),
            check=True,
        )
        (release_wheel,) = sorted(release_outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())
        (sdist,) = sorted(release_outdir.glob("swcraft_tools-*.tar.gz"), key=lambda path: path.as_posix())
        (direct_wheel,) = sorted(wheel_outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())
        with zipfile.ZipFile(release_wheel) as archive:
            release_wheel_names = archive.namelist()
        with tarfile.open(sdist) as archive:
            sdist_names = archive.getnames()
        with zipfile.ZipFile(direct_wheel) as archive:
            direct_wheel_names = archive.namelist()
    finally:
        for sidecar in sidecars:
            shutil.rmtree(sidecar, ignore_errors=True)

    archives = (
        ("wheel of the release build", release_wheel_names),
        ("sdist", sdist_names),
        ("directly built wheel", direct_wheel_names),
    )
    for dirname in (ARTIFACTS.PACKAGE_GUIDE_SITE_STAGING_DIRNAME, ARTIFACTS.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME):
        for label, names in archives:
            carried = [name for name in names if f"/{dirname}/" in f"/{name}"]
            assert carried == [], (
                f"the {label} carries {len(carried)} file(s) from {dirname}/, so a payload rewrite whose "
                f"cleanup failed would ship a stale or half-copied guide: {sorted(carried)[:3]}"
            )


def test_the_agent_package_carries_its_system_prompt_as_source() -> None:
    """The prompt an installation reads ships because it is package source, not a build copy.

    It is tracked, not ignored, inside ``packages/agent/pyproject.toml``'s packaged directory, and a source of the
    artifacts (an edit marks ``dist/`` stale). That the build writes no copy is ``test_build_artifacts_guide.py``'s
    ``test_build_all_writes_two_trees_and_the_named_files_outside_dist``; that a real wheel carries it is
    ``test_build_artifacts_produces_the_full_local_artifact_set``'s.
    """
    relative = AGENT_PACKAGE_PROMPT.as_posix()
    assert (REPO_ROOT / AGENT_PACKAGE_PROMPT).is_file(), f"{relative} is missing"
    pyproject = tomllib.loads((REPO_ROOT / "packages" / "agent" / "pyproject.toml").read_text(encoding="utf-8"))
    (package,) = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert relative.startswith(f"packages/agent/{package}/"), f"{relative} is outside the packaged {package}"
    assert relative not in ARTIFACTS.GENERATED_SOURCE_FILES, "the prompt is a source of the artifacts"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert tracked.returncode == 0, f"{relative} is not tracked (this test needs a git checkout): {tracked.stderr}"
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", relative], cwd=REPO_ROOT, capture_output=True, check=False
    )
    assert ignored.returncode == 1, f"{relative} is VCS-ignored, so hatchling would leave it out of the wheel"


def test_install_licence_payload_copies_both_root_files_into_every_python_project(tmp_path: Path) -> None:
    """Each wheel and sdist carries the root licence and notice, copied to each project's root.

    There hatchling's default ``license-files`` patterns find them; that a real wheel carries them is
    ``test_build_artifacts_produces_the_full_local_artifact_set``'s.
    """
    repo_root = make_source_repo(tmp_path)
    (repo_root / "packages" / "tools" / "LICENSE").write_text("stale\n", encoding="utf-8", newline="\n")

    copies = ARTIFACTS.install_licence_payload(repo_root)

    assert sorted(path.relative_to(repo_root).as_posix() for path in copies) == sorted(
        f"{ARTIFACTS.PACKAGES_DIR.as_posix()}/{project}/{name}"
        for project in ARTIFACTS.PYTHON_PROJECTS
        for name in ARTIFACTS.LICENCE_FILENAMES
    )
    for path in copies:
        assert path.read_bytes() == (repo_root / path.name).read_bytes(), f"{path} is not the root file's bytes"
    assert not any(path.name.endswith(".incoming") for path in (repo_root / "packages" / "tools").iterdir()), (
        "no staging name beside the copy: hatchling's LICEN[CS]E* pattern would ship it"
    )


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_install_licence_payload_refuses_a_missing_root_file(tmp_path: Path, name: str) -> None:
    repo_root = make_source_repo(tmp_path)
    (repo_root / name).unlink()

    with pytest.raises(ARTIFACTS.BuildArtifactsError, match=name):
        ARTIFACTS.install_licence_payload(repo_root)

    assert not (repo_root / "packages" / "tools" / name).exists(), "nothing staged when a root file is missing"


def test_build_all_stages_the_licence_files_before_the_first_wheel(tmp_path: Path) -> None:
    repo_root = make_source_repo(tmp_path)
    staged_at_first_build: list[bool] = []

    class Recording(FakeRunner):
        def __call__(self, argv, cwd):  # type: ignore[override]
            if list(argv[1:3]) == ["-m", "build"] and not staged_at_first_build:
                staged_at_first_build.append(
                    all(
                        (repo_root / ARTIFACTS.PACKAGES_DIR / project / name).is_file()
                        for project in ARTIFACTS.PYTHON_PROJECTS
                        for name in ARTIFACTS.LICENCE_FILENAMES
                    )
                )
            return super().__call__(argv, cwd)

    ARTIFACTS.build_all(repo_root, run=Recording(repo_root), npm="npm")

    assert staged_at_first_build == [True]


def test_the_staged_licence_copies_are_gitignored_and_no_source_of_the_artifacts(tmp_path: Path) -> None:
    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    packages = ARTIFACTS.PACKAGES_DIR.as_posix()
    for project in (*(f"{packages}/{label}" for label in ARTIFACTS.PYTHON_PROJECTS), ARTIFACTS.VSCODE_PROJECT):
        for name in ARTIFACTS.LICENCE_FILENAMES:
            assert f"/{project}/{name}" in entries, f"{project}/{name} is a build product and must be ignored"
    for name in ARTIFACTS.LICENCE_FILENAMES:
        assert f"{ARTIFACTS.VSCODE_PROJECT}/{name}" in ARTIFACTS.GENERATED_SOURCE_FILES
    for name in ARTIFACTS.LICENCE_FILENAMES:
        assert name in ARTIFACTS.ARTIFACT_SOURCE_FILES, f"an edit to the root {name} must mark dist/ stale"

    repo_root = make_source_repo(tmp_path)
    before = ARTIFACTS.source_inventory(repo_root)
    for name in ARTIFACTS.LICENCE_FILENAMES:
        (repo_root / ARTIFACTS.VSCODE_PROJECT / name).write_bytes((repo_root / name).read_bytes())
    assert ARTIFACTS.source_inventory(repo_root) == before, "a staged .vsix copy changed the fingerprint"
    (repo_root / "LICENSE").write_text("edited\n", encoding="utf-8", newline="\n")
    assert ARTIFACTS.source_inventory(repo_root) != before, "an edited root licence left the fingerprint"
