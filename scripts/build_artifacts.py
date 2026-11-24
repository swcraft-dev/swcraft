"""Build the versioned local SWCraft artifacts.

    dist/
      python/     swcraft_tools-<v>-py3-none-any.whl  (+ .tar.gz)
                  swcraft_mcp-<v>-py3-none-any.whl    (+ .tar.gz)
                  swcraft_agent-<v>-py3-none-any.whl  (+ .tar.gz)
                  swcraft_specimens-<v>-py3-none-any.whl  (+ .tar.gz)
      vscode/     swcraft-vscode-<v>.vsix
      docs/      swcraft-guide-<v>.zip  (the manifest entry)
                 site/  the tree it was archived from (also what ``--docs-only`` leaves)
      manifest.json
      SHA256SUMS
      workstation/  swcraft-<v>-<platform>.zip + SHA256SUMS  (provisioning, not a manifest
                    entry -- `--workstation-archive`)

``build_all`` also copies the rendered guide into ``packages/tools/src/swcraft_tools/guide_site/``
(:func:`install_guide_site_payload`), so the ``swcraft-tools`` wheel and sdist carry it for
``swcraft docs``; a gitignored build product, rewritten by every run.

Nothing here publishes: the local artifact directory is only an installation source.
``--wheelhouse`` fills ``dist/wheelhouse/`` (platform- and interpreter-specific, so no manifest
entry) with SWCraft's four wheels and the third-party closure resolved under the lock, so the
clean-install acceptance (``verify_clean_install``) runs with ``--no-index``: populating may use the
network, asserting never does.

Stdlib only, no shell; every subprocess call goes through the injectable ``run`` callable.
``dist/python`` and ``dist/vscode`` are cleaned first, so a wheel of an earlier version never enters
the manifest, and the Python builds run with ``SOURCE_DATE_EPOCH`` pinned to the commit's timestamp
(:func:`reproducible_build_environment`), so two builds of one commit are byte-identical.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import email.message
import email.parser
import fnmatch
import gzip
import hashlib
import http.client
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
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The folder, relative to the repo root, holding the Python projects; each label below is its folder there.
PACKAGES_DIR = Path("packages")

#: Python projects built with ``python -m build``, in build order. The specimen generator is
#: last: it depends on none of the other three, and they on nothing of it.
PYTHON_PROJECTS = ("tools", "mcp", "agent", "specimens")

#: project folder (under :data:`PACKAGES_DIR`) -> the escaped distribution name its wheel/sdist filename carries.
PROJECT_DISTRIBUTIONS = {
    "tools": "swcraft_tools",
    "mcp": "swcraft_mcp",
    "agent": "swcraft_agent",
    "specimens": "swcraft_specimens",
}

#: SWCraft's four wheels the wheelhouse must always carry, in build order.
SWCRAFT_DISTRIBUTIONS = tuple(PROJECT_DISTRIBUTIONS[project] for project in PYTHON_PROJECTS)

#: The VS Code extension project, packaged through npm.
VSCODE_PROJECT = "vscode_extension"

#: The licence and the notice at the repository root, staged into each Python project's root before
#: its wheel is built (:func:`install_licence_payload`), where hatchling's default ``license-files``
#: patterns find them (PEP 639's ``.dist-info/licenses/``). None is declared, because a declared file
#: must exist at build time, editable installs included. The copies are gitignored.
LICENCE_FILENAMES = ("LICENSE", "NOTICE")

DIST_DIRNAME = "dist"
PYTHON_DIRNAME = "python"
VSCODE_DIRNAME = "vscode"
DOCS_DIRNAME = "docs"
WHEELHOUSE_DIRNAME = "wheelhouse"

#: ``dist/workstation/`` -- the archive a colleague unpacks and runs. Not a manifest entry: it is
#: built for one platform and one interpreter.
WORKSTATION_DIRNAME = "workstation"

#: Where the closure sits *inside* the archive; the installer's ``--source``
#: default and the directory its ``PLATFORM.json`` travels in.
WORKSTATION_WHEELS_DIRNAME = "wheels"

#: The archive directory the runtime payloads travel in, beside ``wheels/``; named apart from
#: :data:`RUNTIME_DIRNAME`, a build directory.
WORKSTATION_RUNTIME_DIRNAME = "runtime"

#: The container suffixes :func:`workstation_member_compression` stores rather than
#: deflates -- every payload file ``dist/runtime/`` holds under ``runtime/`` except ``RUNTIME.json``.
WORKSTATION_STORED_SUFFIXES = (".tar.xz", ".tar.gz", ".zip")

#: The archive directory the companion extensions travel in: ``extensions/<publisher>.<name>.vsix``,
#: no version and no target.
WORKSTATION_EXTENSIONS_DIRNAME = "extensions"

#: The files the archive ships by copying, never by a wheel: they run before any wheel is installed.
WORKSTATION_SCRIPTS_DIR = Path("scripts") / "workstation"

#: The one member whose bytes the archive changes -- see
#: :func:`workstation_member_bytes` -- and the one it makes executable.
WORKSTATION_BATCH_WRAPPER = "swcraft_install.cmd"
WORKSTATION_SHELL_WRAPPER = "swcraft_install.sh"

WORKSTATION_ARCHIVE_PREFIX = "swcraft-"
WORKSTATION_ARCHIVE_SUFFIX = ".zip"

#: The extension package's suffix, read wherever a filename is decided to be one.
VSIX_SUFFIX = ".vsix"
MANIFEST_FILENAME = "manifest.json"
SHA256SUMS_FILENAME = "SHA256SUMS"

#: MkDocs' configuration for the user guide. Its ``docs_dir`` and ``site_dir`` are declared there,
#: not passed, so ``mkdocs build`` run by hand writes where this script does.
MKDOCS_CONFIG_FILENAME = "mkdocs.yml"

#: The user guide's source tree -- MkDocs' ``docs_dir``.
GUIDE_DIR = Path("docs") / "guide"

#: The guide's corpus for the assistant's ``guide`` tool, written into the site's root, so every
#: channel that carries the site carries it. ``swcraft_tools.guide_corpus.CORPUS_FILENAME`` spelled
#: again (this script imports nothing it builds); a test holds the two equal.
GUIDE_CORPUS_FILENAME = "guide-corpus.json"

#: The producer of :data:`GUIDE_CORPUS_FILENAME`, run in its module form.
GUIDE_CORPUS_MODULE = "swcraft_tools.guide_corpus"

#: The guide page the build writes: the version the site describes. Gitignored and absent from
#: ``mkdocs.yml``'s ``nav``, so no committed page becomes another version lockstep site.
GUIDE_VERSION_PAGE = GUIDE_DIR / "version.md"

#: The repository's release notes, the source the guide's *Release notes* section is copied from.
RELEASE_NOTES_DIR = Path("docs") / "release-notes"

#: The guide's *Release notes* section, generated by :func:`write_guide_release_notes` before
#: mkdocs runs (``nav`` names its index). Gitignored: every note names its own version.
GUIDE_RELEASE_NOTES_DIR = GUIDE_DIR / "release-notes"
GUIDE_RELEASE_NOTES_INDEX = "index.md"

#: The oldest release note the guide always carries (``test_user_facing_surfaces.py``'s
#: ``OLDEST_SCANNED_RELEASE``, pinned equal): later notes are written for the user. An older note
#: joins only when a guide page links it (:func:`guide_release_notes`), so no link is dead.
GUIDE_RELEASE_NOTES_FLOOR = (0, 9, 0)

#: A relative link from a guide page into the section, capturing the version.
_GUIDE_RELEASE_NOTE_LINK = re.compile(r"\]\(\s*(?:\.\./)*release-notes/(\d+\.\d+\.\d+)\.md")

#: Where the rendered site lands, under ``dist/docs/``; the manifest records its archive.
DOCS_SITE_DIRNAME = "site"

#: The directory name the rendered guide is packaged under, inside the ``swcraft_tools`` package;
#: one name for :data:`IGNORED_SOURCE_DIRS` and :data:`PACKAGE_GUIDE_SITE_DIR`.
PACKAGE_GUIDE_SITE_DIRNAME = "guide_site"

#: Where :func:`install_guide_site_payload` builds the new payload: a sibling, so the move into place
#: is a rename, with a fixed name, so a killed run leaves one predictable directory the next removes.
PACKAGE_GUIDE_SITE_STAGING_DIRNAME = f"{PACKAGE_GUIDE_SITE_DIRNAME}.incoming"

#: Where :func:`install_guide_site_payload` moves the payload it replaces: a rename, never a removal
#: in place, because a failed ``rmtree`` could leave a truncated tree still holding ``index.html``
#: (the locator's only guard) for ``swcraft docs`` to serve.
PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME = f"{PACKAGE_GUIDE_SITE_DIRNAME}.outgoing"

#: The file whose presence makes a rendered tree a site worth packaging; pinned equal to
#: ``guide_site.py``'s ``SITE_INDEX_NAME`` by ``test_guide_site.py``.
SITE_INDEX_FILENAME = "index.html"

#: The rendered guide's home inside the ``swcraft-tools`` package tree: ``swcraft docs`` serves it
#: from the installed package (``importlib.resources``). A gitignored build product.
PACKAGE_GUIDE_SITE_DIR = PACKAGES_DIR / "tools" / "src" / "swcraft_tools" / PACKAGE_GUIDE_SITE_DIRNAME

#: The docs archive's filename parts: ``swcraft-guide-<version>.zip``. :func:`artifact_kind` classifies
#: on the prefix, so a stray ``swcraft_tools-0.1.0.zip`` still raises; zip because ``.tar.gz`` means sdist.
DOCS_ARCHIVE_PREFIX = "swcraft-guide-"
DOCS_ARCHIVE_SUFFIX = ".zip"

#: ``manifest.json`` key holding :func:`source_inventory`, the content fingerprint of the sources.
#: It catches what mtimes cannot: a source deleted after the build, or edited without its mtime moving.
SOURCES_KEY = "sources"

#: Written into ``dist/wheelhouse/`` so a stale or foreign-platform wheelhouse is
#: detected up front instead of surfacing as an opaque pip resolution error.
WHEELHOUSE_PLATFORM_FILENAME = "PLATFORM.json"

#: The clean-install acceptance installs only this requirement, so ``swcraft-mcp``/``swcraft-tools``
#: must arrive through the declared dependency graph.
CLEAN_INSTALL_REQUIREMENT = "swcraft-agent"

#: Relative path to the single source of truth for the version.
VERSION_MODULE = PACKAGES_DIR / "tools" / "src" / "swcraft_tools" / "_version.py"

#: The build inputs (directories walked recursively, and files) that decide whether ``dist/`` still
#: describes the sources (:func:`stale_artifact_source`); the manifest's ``version`` cannot, being
#: constant during development. The extension's packaged-only files are derived from ``.vscodeignore``
#: by :func:`packaged_vscode_files`, so a newly shipped file needs no edit here. A superset of
#: ``package-vsix.mjs``'s ``FRESHNESS_INPUT_*`` lists, because the ``.vsix`` also depends on the lock,
#: the TypeScript configuration and ``.vscodeignore``; the guide is here because ``dist/docs/`` is a
#: manifest entry.
ARTIFACT_SOURCE_DIRS = (
    *(f"{PACKAGES_DIR.as_posix()}/{project}/src" for project in PYTHON_PROJECTS),
    f"{VSCODE_PROJECT}/src",
    f"{VSCODE_PROJECT}/fixtures",
    f"{VSCODE_PROJECT}/scripts",
    GUIDE_DIR.as_posix(),
    # The guide carries a copy of the release notes (write_guide_release_notes), so a
    # note is a source of the guide archive and of the wheel's guide payload.
    RELEASE_NOTES_DIR.as_posix(),
)

ARTIFACT_SOURCE_FILES = (
    *(f"{PACKAGES_DIR.as_posix()}/{project}/pyproject.toml" for project in PYTHON_PROJECTS),
    f"{VSCODE_PROJECT}/package.json",
    f"{VSCODE_PROJECT}/package-lock.json",
    f"{VSCODE_PROJECT}/tsconfig.json",
    f"{VSCODE_PROJECT}/.vscodeignore",
    f"{VSCODE_PROJECT}/esbuild.mjs",
    MKDOCS_CONFIG_FILENAME,
    # Every wheel, sdist and the .vsix carry a copy of the root licence and notice.
    *LICENCE_FILENAMES,
    # The elkjs payload the swcraft-tools wheel carries is pinned here (the tree itself is excluded).
    "scripts/workstation/pins/runtime-pins.json",
)

#: Repo-relative files inside :data:`ARTIFACT_SOURCE_DIRS` that this build writes, so not sources:
#: counted, they would report ``dist/`` stale right after a green build, or fingerprint a tree
#: differently depending on whether the licence copies were staged.
GENERATED_SOURCE_FILES = frozenset(
    {
        GUIDE_VERSION_PAGE.as_posix(),
        *(f"{VSCODE_PROJECT}/{name}" for name in LICENCE_FILENAMES),
    }
)

#: Repo-relative directories this build writes, excluded for :data:`GENERATED_SOURCE_FILES`' reason.
#: A path, not a name: ``release-notes`` is also the name of the source the section is copied from.
GENERATED_SOURCE_DIRS = frozenset({GUIDE_RELEASE_NOTES_DIR.as_posix()})

#: vsce's exclusion list: everything under ``vscode_extension/`` it does not match ships in the ``.vsix``.
VSCODE_IGNORE_FILENAME = ".vscodeignore"

#: Directory names skipped while walking :data:`ARTIFACT_SOURCE_DIRS`: build output and caches, whose
#: mtimes move for reasons unrelated to the sources. The guide-site payload and its staging and
#: superseded siblings are this build's own output inside ``packages/tools/src``; counted, they would
#: report ``dist/`` stale right after a green build.
IGNORED_SOURCE_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "dist",
        "out",
        ".venv",
        ".mypy_cache",
        PACKAGE_GUIDE_SITE_DIRNAME,
        PACKAGE_GUIDE_SITE_STAGING_DIRNAME,
        PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME,
        # The elkjs payload and its sidecars, staged by provisioning; its pin file is the source.
        "elkjs",
        "elkjs.incoming",
        "elkjs.previous",
    }
)

#: The reproducible-builds variable hatchling stamps into archive members instead of the wall clock.
SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"

#: Used when the commit date cannot be read; hatchling's own default, so a fallback and an unpinned
#: build agree.
FALLBACK_SOURCE_DATE_EPOCH = "1580601600"

#: The npm invocations that produce the ``.vsix``, in order. ``package`` rebuilds the bundle itself,
#: so there is no separate build step; ``clean`` removes a bundle of an earlier version of the
#: sources, which the bundle-vs-source mtime check cannot see.
NPM_PACKAGE_SEQUENCE: tuple[tuple[str, ...], ...] = (
    ("ci",),
    ("run", "clean"),
    ("run", "package"),
)

#: ``run(argv, cwd)`` -- executes a subprocess, raising on a non-zero exit.
Runner = Callable[[Sequence[str], Path], None]


def _run(argv: Sequence[str], cwd: Path) -> None:
    subprocess.run(list(argv), cwd=str(cwd), check=True)


class BuildArtifactsError(RuntimeError):
    """A build step failed in a way this script can explain."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def artifact_kind(filename: str) -> str:
    """Map an artifact filename to its manifest ``kind``; ``ValueError`` for anything else.

    The docs archive keys off its prefix (:data:`DOCS_ARCHIVE_PREFIX`), so a stray ``.zip`` raises.
    """
    if filename.endswith(".whl"):
        return "wheel"
    if filename.endswith(".tar.gz"):
        return "sdist"
    if filename.endswith(VSIX_SUFFIX):
        return "vsix"
    if filename.startswith(DOCS_ARCHIVE_PREFIX) and filename.endswith(DOCS_ARCHIVE_SUFFIX):
        return "docs"
    raise ValueError(f"unrecognized artifact filename: {filename!r}")


def artifact_name(filename: str) -> str:
    """Map an artifact filename to the canonical hyphenated name of what it is a build of."""
    kind = artifact_kind(filename)
    if kind == "wheel":
        # {distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl
        stem = filename[: -len(".whl")]
        return stem.split("-")[0].replace("_", "-")
    if kind == "sdist":
        stem = filename[: -len(".tar.gz")]
    elif kind == "docs":
        stem = filename[: -len(DOCS_ARCHIVE_SUFFIX)]
    else:
        stem = filename[: -len(VSIX_SUFFIX)]
    return stem.rsplit("-", 1)[0].replace("_", "-")


#: Manifest ``kind`` -> the ``dist/`` subdirectory that kind is written to.
ARTIFACT_DIRECTORIES = {
    "wheel": PYTHON_DIRNAME,
    "sdist": PYTHON_DIRNAME,
    "vsix": VSCODE_DIRNAME,
    "docs": DOCS_DIRNAME,
}


def artifact_relative_path(entry: dict[str, object]) -> str:
    """``dist/``-relative POSIX path of a manifest entry; an unrecognized kind raises, never defaults."""
    kind = str(entry["kind"])
    directory = ARTIFACT_DIRECTORIES.get(kind)
    if directory is None:
        raise BuildArtifactsError(f"unrecognized artifact kind: {kind!r}")
    return f"{directory}/{entry['filename']}"


def parse_vscodeignore(text: str) -> list[tuple[str, bool]]:
    """``.vscodeignore``'s rules as ``(pattern, negated)`` pairs, in file order.

    Same shape as ``.gitignore``: blank lines and ``#`` comments are skipped and a
    leading ``!`` re-includes what an earlier pattern excluded.
    """
    rules: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        rules.append((pattern.strip("/"), negated))
    return rules


def _matches_glob(pattern_segments: Sequence[str], path_segments: Sequence[str]) -> bool:
    """Whether a ``/``-split glob matches a ``/``-split relative path.

    ``**`` spans any number of segments; ``*``/``?``/``[...]`` stay inside one.
    """
    if not pattern_segments:
        return not path_segments
    head, rest = pattern_segments[0], pattern_segments[1:]
    if head == "**":
        if not rest:
            return True
        return any(_matches_glob(rest, path_segments[index:]) for index in range(len(path_segments) + 1))
    if not path_segments or not fnmatch.fnmatchcase(path_segments[0], head):
        return False
    return _matches_glob(rest, path_segments[1:])


def is_packaged(relative_posix: str, rules: Sequence[tuple[str, bool]]) -> bool:
    """Whether *relative_posix* ends up inside the ``.vsix``, per parsed rules.

    The last matching rule decides (``.gitignore`` semantics), so a ``!`` line can
    re-include one file out of an excluded directory.
    """
    segments = relative_posix.split("/")
    packaged = True
    for pattern, negated in rules:
        if _matches_glob(pattern.split("/"), segments):
            packaged = negated
    return packaged


def packaged_vscode_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every source file that ships inside the ``.vsix``, derived from ``.vscodeignore``.

    A missing ``.vscodeignore`` excludes nothing (an over-broad set can only cause a rebuild).
    :data:`IGNORED_SOURCE_DIRS` still applies: ``dist/`` is packaged but generated from ``src/``.
    """
    project_root = repo_root / VSCODE_PROJECT
    if not project_root.is_dir():
        return []
    ignore_file = project_root / VSCODE_IGNORE_FILENAME
    rules = parse_vscodeignore(ignore_file.read_text(encoding="utf-8")) if ignore_file.is_file() else []

    files: list[Path] = []
    for entry in sorted((project_root.rglob("*")), key=lambda path: path.as_posix()):
        relative = entry.relative_to(project_root)
        if not entry.is_file() or IGNORED_SOURCE_DIRS.intersection(relative.parts):
            continue
        if is_packaged(relative.as_posix(), rules):
            files.append(entry)
    return files


def collect_artifact_sources(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every file the artifacts are built from or packaged from, under *repo_root*.

    Missing entries are skipped (a test's synthetic tree carries only part of the repository); files
    this build writes are not sources.
    """
    files: list[Path] = []
    for relative in ARTIFACT_SOURCE_DIRS:
        directory = repo_root / relative
        if not directory.is_dir():
            continue
        for entry in directory.rglob("*"):
            parts = entry.relative_to(directory).parts
            if not entry.is_file() or IGNORED_SOURCE_DIRS.intersection(parts):
                continue
            if any(part.endswith(".egg-info") for part in parts):
                continue
            repo_relative = entry.relative_to(repo_root).as_posix()
            if repo_relative in GENERATED_SOURCE_FILES:
                continue
            if any(repo_relative.startswith(f"{generated}/") for generated in GENERATED_SOURCE_DIRS):
                continue
            files.append(entry)
    for relative in ARTIFACT_SOURCE_FILES:
        candidate = repo_root / relative
        if candidate.is_file():
            files.append(candidate)
    files.extend(
        path
        for path in packaged_vscode_files(repo_root)
        if path.relative_to(repo_root).as_posix() not in GENERATED_SOURCE_FILES
    )
    return list(dict.fromkeys(files))


def source_inventory(repo_root: Path = REPO_ROOT) -> dict[str, object]:
    """Content fingerprint of what the artifacts were built and packaged from.

    ``{"count": <int>, "sha256": <digest over each sorted repo-relative path and that file's own
    SHA-256>}``: it moves for an addition, a deletion, a rename and an edit. Content is hashed because
    mtimes are not a truthful record of it (``cp -p``, a restore, a coarse filesystem).
    """
    entries = sorted((path.relative_to(repo_root).as_posix(), path) for path in collect_artifact_sources(repo_root))
    digest = hashlib.sha256()
    for relative, path in entries:
        digest.update(f"{relative}\0{sha256_file(path)}\n".encode())
    return {"count": len(entries), "sha256": digest.hexdigest()}


def stale_artifact_source(
    dist_root: Path,
    manifest: dict[str, object],
    repo_root: Path = REPO_ROOT,
) -> str | None:
    """Why the artifacts described by *manifest* no longer describe the sources.

    ``None`` means they are current. Otherwise: the repo-relative path of the newest source newer than
    the oldest artifact (ties count as fresh), or a sentence naming an inventory change mtimes cannot
    express. The mtime scan runs first because it can name the file; the recorded
    :func:`source_inventory` then catches deletions, renames and edits that kept the mtime. A manifest
    with no inventory is reported stale rather than assumed fresh.
    """
    entries = manifest.get("artifacts") or []
    if not isinstance(entries, list):
        raise BuildArtifactsError(f"manifest 'artifacts' must be a list, not {type(entries).__name__}")
    built = [dist_root / artifact_relative_path(entry) for entry in entries]
    present = [path for path in built if path.is_file()]
    if not present:
        return None

    oldest_artifact_mtime = min(path.stat().st_mtime for path in present)
    stalest: str | None = None
    newest_source_mtime = oldest_artifact_mtime
    for source in collect_artifact_sources(repo_root):
        mtime = source.stat().st_mtime
        if mtime > newest_source_mtime:
            stalest = source.relative_to(repo_root).as_posix()
            newest_source_mtime = mtime
    if stalest is not None:
        return stalest

    recorded = manifest.get(SOURCES_KEY)
    if not isinstance(recorded, dict):
        return f"{MANIFEST_FILENAME} records no packaged-source inventory"
    current = source_inventory(repo_root)
    if recorded.get("sha256") != current["sha256"]:
        return (
            "the packaged sources changed since the build without ageing anything "
            "(a deletion, a rename, or an edit that preserved the mtime) "
            f"-- {recorded.get('count')} files recorded, {current['count']} on disk"
        )
    return None


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so a large ``.vsix`` is not slurped."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entry(path: Path) -> dict[str, object]:
    """Describe one built artifact: ``{name, kind, filename, sha256, size}``."""
    return {
        "name": artifact_name(path.name),
        "kind": artifact_kind(path.name),
        "filename": path.name,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def utc_timestamp(moment: datetime) -> str:
    """UTC ISO-8601 to the second, ``Z``-suffixed -- the one timestamp format used."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_manifest(
    *,
    version: str,
    git_commit: str,
    built_at: datetime,
    artifact_paths: Iterable[Path],
    sources: dict[str, object],
) -> dict[str, object]:
    """Assemble ``dist/manifest.json``'s content from the built artifact files.

    Entries are sorted by filename, so two builds of one tree order the document alike. *sources* is
    :func:`source_inventory`, required because a manifest without it reads as stale.
    """
    entries = [manifest_entry(path) for path in sorted(artifact_paths, key=lambda p: p.name)]
    return {
        "version": version,
        "built_at": utc_timestamp(built_at),
        "git_commit": git_commit,
        "artifacts": entries,
        SOURCES_KEY: sources,
    }


def format_sha256sums(entries: Iterable[dict[str, object]]) -> str:
    """Render manifest entries in coreutils ``sha256sum`` format, sorted by path.

    ``sha256sum -c SHA256SUMS`` run from ``dist/`` verifies the release directly.
    """
    return format_sha256_lines((artifact_relative_path(entry), str(entry["sha256"])) for entry in entries)


def format_sha256_lines(entries: Iterable[tuple[str, str]]) -> str:
    """``<hex>  <path>`` per line, sorted by path: the format the installer's ``parse_sha256sums`` reads."""
    return "".join(f"{sha256}  {path}\n" for path, sha256 in sorted(entries))


def read_version(repo_root: Path = REPO_ROOT) -> str:
    """Read ``__version__`` out of the version module without importing it (nothing is installed yet)."""
    module_path = repo_root / VERSION_MODULE
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "__version__" in targets and isinstance(node.value, ast.Constant):
                return str(node.value.value)
    raise BuildArtifactsError(f"{module_path} does not assign a literal __version__")


#: The trailer ``scripts/public_snapshot.py`` writes on an assembled snapshot's one commit.
SOURCE_COMMIT_TRAILER = "Source-Commit"
#: A full lowercase SHA-1 or SHA-256 object id; ``public_snapshot.source_trailer`` applies the same pattern.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def read_git_commit(repo_root: Path = REPO_ROOT) -> str:
    """The commit the build is of, or ``"unknown"`` outside a git checkout.

    ``HEAD``'s SHA, unless ``HEAD``'s message carries a ``Source-Commit:`` trailer naming a full SHA:
    an assembled public snapshot (``scripts/public_snapshot.py``) names the commit it was taken from
    that way. Of several such trailers the last wins; one that is not a full lowercase SHA-1 or SHA-256
    is ignored.

    What this does not reach: the trailer is trusted, not verified; any ``HEAD`` carrying one is
    believed, whoever wrote it. Never raises: traceability metadata must not fail a build.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", f"--format=%H%n%(trailers:key={SOURCE_COMMIT_TRAILER},valueonly)", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    trailers = [line for line in lines[1:] if _COMMIT_SHA.fullmatch(line)]
    return trailers[-1] if trailers else lines[0]


def read_git_commit_epoch(repo_root: Path = REPO_ROOT) -> str | None:
    """Committer timestamp of ``HEAD`` as a Unix epoch, or ``None``.

    ``None`` when there is no commit to date the build from; the build then falls back to
    :data:`FALLBACK_SOURCE_DATE_EPOCH` rather than fail.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%ct"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    stamp = result.stdout.strip()
    return stamp if stamp.isdigit() else None


def source_date_epoch(repo_root: Path = REPO_ROOT, environ: dict[str, str] | None = None) -> str:
    """The timestamp the build backends stamp into the artifacts.

    An exported ``SOURCE_DATE_EPOCH`` wins (whoever set it is reproducing an older build); otherwise the
    commit's own date.
    """
    environ = os.environ if environ is None else environ
    existing = (environ.get(SOURCE_DATE_EPOCH_ENV) or "").strip()
    if existing:
        return existing
    return read_git_commit_epoch(repo_root) or FALLBACK_SOURCE_DATE_EPOCH


@contextlib.contextmanager
def reproducible_build_environment(repo_root: Path = REPO_ROOT, environ: dict[str, str] | None = None):
    """Export :func:`source_date_epoch` for the duration of the build steps.

    The environment is the channel because ``python -m build`` runs the backend in an isolated
    subprocess and ``run`` takes only ``(argv, cwd)``. The previous value is restored.
    """
    environ = os.environ if environ is None else environ
    previous = environ.get(SOURCE_DATE_EPOCH_ENV)
    stamp = source_date_epoch(repo_root, environ)
    environ[SOURCE_DATE_EPOCH_ENV] = stamp
    try:
        yield stamp
    finally:
        if previous is None:
            environ.pop(SOURCE_DATE_EPOCH_ENV, None)
        else:
            environ[SOURCE_DATE_EPOCH_ENV] = previous


# ---------------------------------------------------------------------------
# Wheelhouse (provisioning: network allowed here, nowhere downstream)
# ---------------------------------------------------------------------------


def requirement_name(requirement: str) -> str:
    """Canonical (PEP 503) distribution name of a requirement string."""
    head = re.split(r"[\[<>=!~;,\s@]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", head).lower()


def declared_requirements(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """``(project name, requirement)`` for every *third-party* direct dependency of the four projects.

    In the projects' dependency order and each ``pyproject.toml``'s own order.
    """
    declared: list[tuple[str, str]] = []
    for project in PYTHON_PROJECTS:
        pyproject = repo_root / PACKAGES_DIR / project / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        name = data.get("project", {}).get("name", project)
        for requirement in data.get("project", {}).get("dependencies", []):
            if requirement_name(requirement).startswith("swcraft-"):
                continue
            declared.append((name, requirement))
    return declared


def third_party_requirements(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every third-party direct dependency of the four Python projects, read from their ``pyproject.toml``.

    SWCraft's own distributions come from ``dist/python/``, never an index; pip resolves the
    transitive closure while downloading.
    """
    return sorted({requirement for _, requirement in declared_requirements(repo_root)})


# -- the lock's constraints -------------------------------------------------------------------------

#: The development lock, ``dev.py update-dev-lock``'s output: one universal resolution of every project's
#: dependencies; it alone pins the specimen generator's ``numpy`` and ``scipy`` (its metadata: ranges).
DEV_LOCK_FILE = Path("constraints") / "dev.txt"

#: One requirement line of the lock: ``name==version``, then an optional ``; marker``.
_LOCK_PIN = re.compile(r"(?P<pin>[A-Za-z0-9][A-Za-z0-9._-]*==\S+?)\s*(?:;\s*(?P<marker>.+?))?\s*")

#: One clause of a marker that names the interpreter's version, compared with a quoted release version.
_PYTHON_CLAUSE = re.compile(
    r"(?P<variable>python_version|python_full_version)\s*(?P<operator><=|>=|==|!=|<|>)\s*"
    r"(?P<quote>['\"])(?P<bound>\d+(?:\.\d+)*)(?P=quote)"
)


def _python_clause_holds(variable: str, operator: str, bound: str, python_version: str) -> bool:
    """Whether ``<variable> <operator> '<bound>'`` holds for the CPython *python_version* (``3.11.15``).

    Per PEP 508 (``X.Y`` or ``X.Y.Z``), compared as release segments padded with zeros.
    """
    parts = [int(part) for part in python_version.split(".")]
    candidate = parts[:2] if variable == "python_version" else parts
    limit = [int(part) for part in bound.split(".")]
    width = max(len(candidate), len(limit))
    left = tuple(candidate + [0] * (width - len(candidate)))
    right = tuple(limit + [0] * (width - len(limit)))
    return {
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
        "==": left == right,
        "!=": left != right,
    }[operator]


def lock_constraints_text(lock_text: str, python_version: str) -> str:
    """The lock's pins as a ``pip`` constraints file for the CPython *python_version* the wheelhouse is for.

    One ``name==version`` line per lock line whose marker holds, in the lock's order. Interpreter-version
    clauses of a conjunction are decided here (a failing one drops the line, holding ones are struck);
    other clauses, and markers with ``or`` or parentheses, stay for ``pip``. Any line that is not a pin,
    comment or blank is refused, so it cannot silently constrain nothing.

    What it does not reach: the lock resolves the development environment, so a runtime distribution
    the lock lacks is resolved unconstrained; nothing checks that none is.
    """
    lines = []
    for raw in lock_text.splitlines():
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        match = _LOCK_PIN.fullmatch(text)
        if match is None:
            raise BuildArtifactsError(
                f"{DEV_LOCK_FILE.as_posix()} holds {text!r}, which is not a `name==version [; marker]` pin."
            )
        pin, marker = match.group("pin"), match.group("marker")
        if marker is None:
            lines.append(pin)
            continue
        if " or " in marker or "(" in marker:
            lines.append(f"{pin} ; {marker}")
            continue
        kept = []
        holds = True
        for clause in (part.strip() for part in marker.split(" and ")):
            version_clause = _PYTHON_CLAUSE.fullmatch(clause)
            if version_clause is None:
                kept.append(clause)
            elif not _python_clause_holds(
                version_clause.group("variable"),
                version_clause.group("operator"),
                version_clause.group("bound"),
                python_version,
            ):
                holds = False
        if holds:
            lines.append(f"{pin} ; {' and '.join(kept)}" if kept else pin)
    return "".join(f"{line}\n" for line in lines)


def runtime_python_version(repo_root: Path = REPO_ROOT) -> str:
    """The CPython version the workstation runtime carries: the interpreter the wheelhouse is resolved for."""
    return str(read_runtime_pins(repo_root)["python"]["version"])


def lock_constraints(repo_root: Path = REPO_ROOT) -> str:
    """:func:`lock_constraints_text` of the committed lock, for :func:`runtime_python_version`."""
    try:
        text = (repo_root / DEV_LOCK_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BuildArtifactsError(
            f"{DEV_LOCK_FILE.as_posix()} cannot be read ({error}), and the wheelhouse is resolved under it."
        ) from None
    return lock_constraints_text(text, runtime_python_version(repo_root))


def _wheel_metadata(wheel: Path, purpose: str) -> tuple[email.message.Message, str, list[str]]:
    """*wheel*'s parsed ``METADATA`` headers, that member's name and every member's name.

    Decoded as UTF-8 with replacement, so a stray byte never refuses a wheel. A wheel that is not a
    readable zip or carries no single ``.dist-info/METADATA`` is a refusal ending in *purpose*.
    """
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            found = [name for name in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
            if len(found) != 1:
                raise BuildArtifactsError(f"{wheel.name} carries no single .dist-info/METADATA, so {purpose}.")
            text = archive.read(found[0]).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile) as error:
        raise BuildArtifactsError(f"{wheel.name} is not a readable wheel ({error}), so {purpose}.") from error
    return email.parser.Parser().parsestr(text, headersonly=True), found[0], names


def _major_minor(version: str) -> str:
    """``X.Y`` of a dotted version -- the part a wheelhouse, a stamp and a licence path key on."""
    return ".".join(version.split(".")[:2])


def wheelhouse_platform(now: datetime | None = None, python_executable: str | None = None) -> dict[str, str]:
    """The environment stamp recorded in (and checked against) ``PLATFORM.json``.

    Describes the interpreter the closure is resolved for: the running one, unless *python_executable*
    names another (the archive's Python), which is then asked (:func:`probe_interpreter`), because the
    host's interpreter can report another platform tag for the same release.
    """
    if python_executable is None or python_executable == sys.executable:
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_tag = sysconfig.get_platform()
    else:
        probed = probe_interpreter(python_executable)
        python_version = _major_minor(probed["python_version"])
        platform_tag = probed["platform_tag"]
    return {
        "python_version": python_version,
        "platform_tag": platform_tag,
        "created_at": utc_timestamp(now or datetime.now(UTC)),
    }


#: What :func:`probe_interpreter` asks another interpreter to print, as JSON (``%``-formatting so it
#: parses on any Python). ``install.py``'s ``_STAMP_PROGRAM`` plus the version's third field; a test
#: holds the two to one answer.
_INTERPRETER_STAMP_PROGRAM = (
    "import json, sys, sysconfig; "
    "print(json.dumps({'python_version': '%d.%d.%d' % sys.version_info[:3], "
    "'platform_tag': sysconfig.get_platform()}))"
)


def probe_interpreter(python_executable: str | Path) -> dict[str, str]:
    """``{"python_version": "X.Y.Z", "platform_tag": ...}`` as *python_executable* reports them.

    ``-I`` so no environment variable or user site changes the answer. Any failure to answer is a
    :class:`BuildArtifactsError` naming the interpreter.
    """
    try:
        completed = subprocess.run(
            [str(python_executable), "-I", "-c", _INTERPRETER_STAMP_PROGRAM],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            check=False,
        )
    except OSError as error:
        raise BuildArtifactsError(f"{python_executable} could not be started to ask what it is: {error}") from error
    try:
        answer = json.loads(completed.stdout) if completed.returncode == 0 else None
    except json.JSONDecodeError:
        answer = None
    if not isinstance(answer, dict) or not all(
        isinstance(answer.get(key), str) for key in ("python_version", "platform_tag")
    ):
        raise BuildArtifactsError(
            f"{python_executable} did not say which Python and platform it is "
            f"(exit {completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return {"python_version": answer["python_version"], "platform_tag": answer["platform_tag"]}


def platform_compatibility(tag: object) -> tuple[str, ...]:
    """The part of a ``sysconfig`` platform tag two interpreters have to agree on.

    Restates ``scripts/workstation/install.py``'s function of this name (a test holds the two equal).
    A macOS tag's middle field describes how the interpreter was built, not the host, so it is compared
    as ``(family, architecture)``; every other tag whole.
    """
    if not isinstance(tag, str):
        return (repr(tag),)
    if tag.startswith("macosx-"):
        fields = tag.split("-")
        if len(fields) >= 3:
            return (fields[0], fields[-1])
    return (tag,)


def check_wheelhouse_platform(wheelhouse: Path, interpreter: dict[str, str] | None = None) -> dict[str, str]:
    """Fail early, and actionably, on a missing or foreign-platform wheelhouse.

    Otherwise pip reports that it cannot find a distribution it can plainly see. Given *interpreter*
    (the archive's Python, from ``RUNTIME.json``), the comparison is exact: on macOS a closure for a
    newer deployment target can hold a wheel that Python cannot install. Absent, the running
    interpreter is compared through :func:`platform_compatibility`, as the installer compares it.
    ``python_version`` is compared exactly either way.
    """
    platform_path = wheelhouse / WHEELHOUSE_PLATFORM_FILENAME
    if not platform_path.is_file():
        raise BuildArtifactsError(
            f"{platform_path} is missing -- {wheelhouse} was never populated. "
            "Run `python scripts/build_artifacts.py --wheelhouse` first."
        )
    try:
        recorded = json.loads(platform_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BuildArtifactsError(f"{platform_path} is not readable JSON: {error}") from error

    if interpreter is None:
        current = wheelhouse_platform()
        whose = "this interpreter is"
        comparable = {"python_version": str, "platform_tag": platform_compatibility}
    else:
        current = interpreter
        whose = "the Python the archive carries is"
        comparable = {"python_version": str, "platform_tag": str}
    mismatches = [
        f"{key}: the wheelhouse was built for {recorded.get(key)!r}, {whose} {current[key]!r}"
        for key in ("python_version", "platform_tag")
        if comparable[key](recorded.get(key)) != comparable[key](current[key])
    ]
    if mismatches:
        raise BuildArtifactsError(
            f"{wheelhouse} was populated for a different environment ({'; '.join(mismatches)}). "
            "Repopulate it with `python scripts/build_artifacts.py --wheelhouse` -- which "
            "`python scripts/dev.py wheelhouse` runs, after `python scripts/dev.py runtime` -- so that "
            "it is resolved for the interpreter it will be installed into."
        )
    return recorded


def require_swcraft_wheels(directory: Path, version: str) -> list[Path]:
    """SWCraft's four wheels for ``version``, proven present and readable (so the message names the wheel)."""
    wheels = []
    for distribution in SWCRAFT_DISTRIBUTIONS:
        matches = sorted(directory.glob(f"{distribution}-{version}-*.whl"), key=lambda path: path.as_posix())
        if not matches:
            raise BuildArtifactsError(
                f"{distribution}-{version}-*.whl is missing from {directory} -- "
                "run `python scripts/build_artifacts.py` to rebuild the local artifacts."
            )
        wheel = matches[0]
        if not zipfile.is_zipfile(wheel):
            raise BuildArtifactsError(
                f"{wheel} is not a readable wheel (corrupt archive) -- "
                "run `python scripts/build_artifacts.py` to rebuild the local artifacts."
            )
        wheels.append(wheel)
    return wheels


def build_wheelhouse(
    repo_root: Path = REPO_ROOT,
    *,
    run: Runner = _run,
    python_executable: str = sys.executable,
    now: datetime | None = None,
) -> Path:
    """Populate ``dist/wheelhouse/`` so acceptance installs can run offline.

    Network is allowed here (provisioning). The third-party requirements are resolved in one ``pip
    download`` constrained by the lock's lines for the runtime's CPython (:func:`lock_constraints`),
    so what ships is what ``.venv`` resolved, the versions the specimens are pinned on; a conflict
    fails with a sentence naming the lock. The freshly built SWCraft wheels are copied in after.

    The closure is replaced, never edited in place: it is assembled in ``dist/wheelhouse.new/`` and
    swapped in by :func:`_swap_in_staged_directory` only once complete, so a dropped connection leaves
    the previous closure as it was.

    What this does not reach: a failure between the swap's two renames leaves the previous closure
    under ``dist/wheelhouse.previous`` (the sentence says so); the Windows rename behaviour is read
    from the documentation, not measured; and a ``pip download`` that exits 0 with an incomplete
    closure is swapped in like any other -- only ``dev.py test-artifacts``' clean-install
    acceptance, a later stage, installs out of it.
    """
    dist_root = repo_root / DIST_DIRNAME
    version = read_version(repo_root)
    built = require_swcraft_wheels(dist_root / PYTHON_DIRNAME, version)
    # Read before anything is emptied or downloaded: an unreadable lock is a sentence, not a half-run.
    lock = lock_constraints(repo_root)

    # Replaced whole, not pruned: a pruned directory keeps every third-party version it ever resolved,
    # and which one a `--find-links` install picks is then pip's ordering over a listing.
    wheelhouse = dist_root / WHEELHOUSE_DIRNAME
    # Beside the destination, so the move at the end is a rename on one filesystem.
    staging = dist_root / f"{WHEELHOUSE_DIRNAME}.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory() as scratch:
            constraints = Path(scratch) / "third-party-requirements.txt"
            constraints.write_text(
                "".join(f"{requirement}\n" for requirement in third_party_requirements(repo_root)),
                encoding="utf-8",
                newline="\n",
            )
            lock_constraints_file = Path(scratch) / "lock-constraints.txt"
            lock_constraints_file.write_text(lock, encoding="utf-8", newline="\n")
            try:
                run(
                    [
                        python_executable,
                        "-m",
                        "pip",
                        "download",
                        "--dest",
                        str(staging),
                        "-r",
                        str(constraints),
                        "-c",
                        str(lock_constraints_file),
                    ],
                    repo_root,
                )
            except subprocess.CalledProcessError as error:
                raise BuildArtifactsError(
                    f"pip could not resolve SWCraft's requirements under the lock's versions "
                    f"({DEV_LOCK_FILE.as_posix()}), exit {error.returncode} -- its output above names the "
                    "requirements that conflict."
                ) from None

        for wheel in built:
            shutil.copy2(wheel, staging / wheel.name)

        (staging / WHEELHOUSE_PLATFORM_FILENAME).write_text(
            json.dumps(wheelhouse_platform(now, python_executable), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _swap_in_staged_directory(staging, wheelhouse, "wheelhouse")
    return wheelhouse


# ---------------------------------------------------------------------------
# Clean-install acceptance (offline: no index, no editable install)
# ---------------------------------------------------------------------------


def venv_interpreter(venv_dir: Path, os_name: str = os.name) -> Path:
    """Interpreter path inside a venv, on either platform.

    Not named ``venv_python``: ``dev.py`` and ``bootstrap.py`` own a function of that name that takes a
    project root. ``os_name`` keeps both layouts testable from either platform.
    """
    if os_name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def clean_install_argv(python_executable: Path | str, wheelhouse: Path, version: str) -> list[str]:
    """The pip invocation the acceptance uses -- one requirement, no index.

    ``--no-index`` disables PyPI and any configured index, so the install resolves from
    ``--find-links`` alone.
    """
    return [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        f"{CLEAN_INSTALL_REQUIREMENT}=={version}",
    ]


#: Probes the installed environment from the inside, so the answers come from its own import machinery.
_ENVIRONMENT_PROBE = """
import importlib.metadata as metadata
import json
import pathlib
import sys
import sysconfig

purelib = pathlib.Path(sysconfig.get_paths()["purelib"])
prefix = pathlib.Path(sys.prefix)
report = {
    "executable": sys.executable,
    "prefix": str(prefix),
    "purelib": str(purelib),
    "distributions": {},
    "modules": {},
    "editable_markers": [],
}

for name in ("swcraft-tools", "swcraft-mcp", "swcraft-agent"):
    try:
        report["distributions"][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        report["distributions"][name] = None

for name in ("swcraft_tools", "swcraft_mcp", "swcraft_agent"):
    try:
        report["modules"][name] = __import__(name).__file__
    except Exception as error:  # noqa: BLE001 - reported, not raised
        report["modules"][name] = "ERROR: %s" % (error,)

if purelib.is_dir():
    for entry in sorted(purelib.iterdir()):
        if entry.name.startswith("__editable__"):
            report["editable_markers"].append(entry.name)
        elif entry.suffix == ".pth":
            for line in entry.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                target = pathlib.Path(line)
                if target.is_absolute() and prefix not in target.parents:
                    report["editable_markers"].append(entry.name + " -> " + line)

print(json.dumps(report))
"""


def probe_environment(python_executable: Path | str) -> dict[str, object]:
    """Ask an interpreter where it resolves the SWCraft packages from."""
    result = subprocess.run(
        [str(python_executable), "-c", _ENVIRONMENT_PROBE],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def verify_clean_install(
    repo_root: Path = REPO_ROOT,
    *,
    venv_dir: Path,
    run: Runner = _run,
    python_executable: str = sys.executable,
) -> dict[str, object]:
    """Install **only** ``swcraft-agent`` into a fresh venv, offline, and report.

    The report: the installed distributions and versions, where each module resolved from, and any
    editable-install marker in the new environment.
    """
    dist_root = repo_root / DIST_DIRNAME
    wheelhouse = dist_root / WHEELHOUSE_DIRNAME
    version = read_version(repo_root)

    check_wheelhouse_platform(wheelhouse)
    require_swcraft_wheels(wheelhouse, version)

    run([python_executable, "-m", "venv", str(venv_dir)], repo_root)
    interpreter = venv_interpreter(venv_dir)
    run(clean_install_argv(interpreter, wheelhouse, version), repo_root)

    report = probe_environment(interpreter)
    report["version"] = version
    return report


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------


def clean_output_dirs(dist_root: Path) -> None:
    """Empty ``dist/python``, ``dist/vscode`` and ``dist/docs``; drop any manifest.

    Otherwise an earlier version's wheel or guide archive would be recorded as this build's.
    ``dist/wheelhouse`` and ``dist/workstation`` are left alone: provisioning output, not in the
    manifest, each replaced by the step that writes it.
    """
    for name in (PYTHON_DIRNAME, VSCODE_DIRNAME, DOCS_DIRNAME):
        shutil.rmtree(dist_root / name, ignore_errors=True)
        (dist_root / name).mkdir(parents=True, exist_ok=True)
    for name in (MANIFEST_FILENAME, SHA256SUMS_FILENAME):
        (dist_root / name).unlink(missing_ok=True)


def build_python_artifacts(
    repo_root: Path,
    dist_root: Path,
    *,
    run: Runner = _run,
    python_executable: str = sys.executable,
) -> list[Path]:
    """Build a wheel + sdist for each Python project into ``dist/python``.

    Under :func:`reproducible_build_environment`, so a rebuild of one commit gives the same digests.
    """
    outdir = dist_root / PYTHON_DIRNAME
    with reproducible_build_environment(repo_root):
        for project in PYTHON_PROJECTS:
            run(
                [python_executable, "-m", "build", "--outdir", str(outdir), str(repo_root / PACKAGES_DIR / project)],
                repo_root,
            )
    return sorted((path for path in outdir.iterdir() if path.is_file()), key=lambda path: path.as_posix())


def require_npm(npm: str | None = None) -> str:
    """The npm executable, proven present, or an actionable error.

    ``require_``, not ``resolve_``: unlike ``dev.py``'s ``resolve_npm()`` it raises when npm is
    absent. ``shutil.which`` honours ``PATHEXT`` and returns ``npm.cmd`` on Windows without a shell.
    """
    if npm is not None:
        return npm
    resolved = shutil.which("npm")
    if resolved is None:
        raise BuildArtifactsError(
            "npm was not found on PATH -- the VS Code extension artifact cannot be built. "
            "Install Node.js 22 (see .prototools) and re-run."
        )
    return resolved


def build_vscode_artifact(
    repo_root: Path,
    dist_root: Path,
    *,
    run: Runner = _run,
    npm: str | None = None,
) -> Path:
    """Package the extension and move the ``.vsix`` into ``dist/vscode``."""
    project_root = repo_root / VSCODE_PROJECT
    npm_executable = require_npm(npm)

    # vsce writes the .vsix into the project root, which is gitignored: a leftover
    # from an earlier version would make the glob below ambiguous.
    for stale in project_root.glob(f"*{VSIX_SUFFIX}"):
        stale.unlink()

    for arguments in NPM_PACKAGE_SEQUENCE:
        run([npm_executable, *arguments], project_root)

    produced = sorted(project_root.glob(f"*{VSIX_SUFFIX}"), key=lambda path: path.as_posix())
    if not produced:
        raise BuildArtifactsError(f"`npm run package` produced no .vsix in {project_root}")
    if len(produced) > 1:
        names = ", ".join(path.name for path in produced)
        raise BuildArtifactsError(f"expected exactly one .vsix in {project_root}, found: {names}")

    destination = dist_root / VSCODE_DIRNAME / produced[0].name
    shutil.move(str(produced[0]), str(destination))
    return destination


def docs_site_dir(dist_root: Path) -> Path:
    """Where the rendered guide lands: ``dist/docs/site``."""
    return dist_root / DOCS_DIRNAME / DOCS_SITE_DIRNAME


def render_guide_version_page(version: str) -> str:
    """The Markdown of ``docs/guide/version.md`` for *version*.

    Deterministic (no timestamp, no host path), so two builds of one commit give identical pages. The
    only guide page naming the version; not committed.
    """
    return (
        "<!-- Generated by scripts/build_artifacts.py. Not committed: see .gitignore. -->\n"
        "\n"
        "# Version\n"
        "\n"
        f"This copy of the guide was built from SWCraft **{version}**.\n"
        "\n"
        "The installable files for a release carry the same version in their filenames,\n"
        "and the release notes for it say what changed.\n"
    )


def write_guide_version_page(repo_root: Path = REPO_ROOT) -> Path:
    """Write :data:`GUIDE_VERSION_PAGE`; returns the path written.

    The version comes from :func:`read_version` alone, the derivation the wheels' filenames use; there
    is no override.
    """
    page = repo_root / GUIDE_VERSION_PAGE
    if not page.parent.is_dir():
        raise BuildArtifactsError(f"the guide's source tree is missing: {page.parent}")
    page.write_text(render_guide_version_page(read_version(repo_root)), encoding="utf-8", newline="\n")
    return page


def release_note_version(path: Path) -> tuple[int, ...] | None:
    """``(0, 9, 0)`` for ``0.9.0.md``; ``None`` for a file that is not a release note."""
    if path.suffix != ".md":
        return None
    parts = path.stem.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def guide_release_notes(repo_root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """The release notes the guide's *Release notes* section carries, newest first.

    Every note from :data:`GUIDE_RELEASE_NOTES_FLOOR` on, plus every older note a committed guide page
    links relatively (``release-notes/<v>.md``). A link to an absent note selects nothing (the link
    gate reports it). The generated section is not read, so an earlier build's leftovers select nothing.
    """
    notes_dir = repo_root / RELEASE_NOTES_DIR
    if not notes_dir.is_dir():
        raise BuildArtifactsError(f"the release notes the guide carries are missing: {notes_dir}")
    guide_dir = repo_root / GUIDE_DIR
    generated = (repo_root / GUIDE_RELEASE_NOTES_DIR).as_posix()
    linked: set[tuple[int, ...]] = set()
    for page in sorted(guide_dir.rglob("*.md"), key=lambda path: path.as_posix()):
        if page.as_posix().startswith(f"{generated}/"):
            continue
        for version in _GUIDE_RELEASE_NOTE_LINK.findall(page.read_text(encoding="utf-8")):
            linked.add(tuple(int(part) for part in version.split(".")))
    selected: list[tuple[tuple[int, ...], Path]] = []
    for note in notes_dir.iterdir():
        version = release_note_version(note)
        if version is None or not note.is_file():
            continue
        if version >= GUIDE_RELEASE_NOTES_FLOOR or version in linked:
            selected.append((version, note))
    return tuple(note for _version, note in sorted(selected, key=lambda pair: pair[0], reverse=True))


def render_guide_release_notes_index(versions: Sequence[str]) -> str:
    """The Markdown of the section's index page for *versions*, given newest first.

    Deterministic, like :func:`render_guide_version_page`. An empty list is refused: a page listing
    nothing reads as a statement that there are none.
    """
    if not versions:
        raise BuildArtifactsError("the guide's release notes section would list no release note")
    lines = [
        "<!-- Generated by scripts/build_artifacts.py. Not committed: see .gitignore. -->",
        "",
        "# Release notes",
        "",
        "Every release of SWCraft has a note: what changed, what did not, and what an existing",
        "project can notice, with an upgrade checklist for that release. Read the note for the",
        "version you are moving to, and the notes of every version you are skipping over.",
        "",
        "The newest is first.",
        "",
        *(f"- [SWCraft {version}]({version}.md)" for version in versions),
        "",
    ]
    return "\n".join(lines)


def write_guide_release_notes(repo_root: Path = REPO_ROOT) -> Path:
    """Write the guide's *Release notes* section; returns its directory.

    Each note is copied byte for byte; the directory is replaced whole, so a note that left the
    selection does not stay behind.
    """
    notes = guide_release_notes(repo_root)
    section = repo_root / GUIDE_RELEASE_NOTES_DIR
    if not section.parent.is_dir():
        raise BuildArtifactsError(f"the guide's source tree is missing: {section.parent}")
    index = render_guide_release_notes_index([note.stem for note in notes])
    if section.exists():
        shutil.rmtree(section)
    section.mkdir()
    for note in notes:
        shutil.copyfile(note, section / note.name)
    (section / GUIDE_RELEASE_NOTES_INDEX).write_text(index, encoding="utf-8", newline="\n")
    return section


def build_docs_site(
    repo_root: Path = REPO_ROOT,
    *,
    run: Runner = _run,
    python_executable: str = sys.executable,
    site_dir: Path | None = None,
) -> Path:
    """Render the user guide into *site_dir*; returns that directory.

    *site_dir* defaults to ``<repo_root>/dist/docs/site``; a parameter so :func:`build_docs_archive`'s
    ``dist_root`` and a test's own render do not touch the live tree.

    The one place mkdocs is invoked, so ``dev.py docs`` and the release build use the same flags.
    ``--strict`` fails on a ``nav`` entry naming no page and a relative link resolving to nothing; it
    does not fail on an orphan page (the link-and-nav gate in ``packages/tools/tests/`` owns that) or
    check absolute URLs. The module form binds the build to this interpreter. Run under
    :func:`reproducible_build_environment` because MkDocs otherwise stamps today's date into
    ``sitemap.xml.gz``'s gzip header, the one byte that moves between two renders of one commit.

    :data:`GUIDE_VERSION_PAGE` and the *Release notes* section are generated into ``docs_dir`` first,
    so every render carries them. The corpus (``python -m swcraft_tools.guide_corpus``) is written
    after mkdocs, which cleans its site directory, into the site, so every channel carrying the site
    carries it; ``--mkdocs-config`` cuts it under the same Markdown extensions. A refusing producer
    fails the build. What this does not reach: whether the corpus's text matches the rendered pages
    beyond sharing their source and configuration.
    """
    config = repo_root / MKDOCS_CONFIG_FILENAME
    if not config.is_file():
        raise BuildArtifactsError(f"the guide's MkDocs configuration is missing: {config}")
    write_guide_version_page(repo_root)
    write_guide_release_notes(repo_root)
    site_dir = docs_site_dir(repo_root / DIST_DIRNAME) if site_dir is None else site_dir
    site_dir.parent.mkdir(parents=True, exist_ok=True)
    with reproducible_build_environment(repo_root):
        run(
            [
                python_executable,
                "-m",
                "mkdocs",
                "build",
                "--strict",
                "--config-file",
                str(config),
                "--site-dir",
                str(site_dir),
            ],
            repo_root,
        )
        run(
            [
                python_executable,
                "-m",
                GUIDE_CORPUS_MODULE,
                "--guide-dir",
                str(repo_root / GUIDE_DIR),
                "--mkdocs-config",
                str(config),
                "--out",
                str(site_dir / GUIDE_CORPUS_FILENAME),
            ],
            repo_root,
        )
    return site_dir


def docs_archive_filename(version: str) -> str:
    """``swcraft-guide-<version>.zip`` -- the name the manifest records."""
    return f"{DOCS_ARCHIVE_PREFIX}{version}{DOCS_ARCHIVE_SUFFIX}"


#: The ``create_system`` byte every archive member is written with (Unix, the pinned mode's
#: meaning); ``zipfile``'s default from ``sys.platform`` would make the bytes depend on the host.
_ZIP_CREATE_SYSTEM_UNIX = 3


def zip_timestamp(epoch: str) -> tuple[int, int, int, int, int, int]:
    """``SOURCE_DATE_EPOCH`` as a zip ``date_time`` tuple, in UTC.

    Zip's DOS timestamp cannot express a year before 1980, so an earlier epoch is clamped, not raised.
    """
    moment = datetime.fromtimestamp(int(epoch), UTC)
    if moment.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


#: The plain-text note at the root of the guide archive, saying the tree has to be served. ``.txt``
#: because a ``.md`` has no registered handler on Windows.
DOCS_ARCHIVE_README = "README.txt"


def render_docs_archive_readme(root: str) -> str:
    """The text of :data:`DOCS_ARCHIVE_README` for a tree unpacked as *root*/.

    Deterministic, so two builds of one commit archive to identical bytes. *root* is the archive's
    stem, so every command can be pasted as it stands.
    """
    return (
        "SWCraft user guide\n"
        "==================\n"
        "\n"
        f"This zip holds one directory, {root}/, which is the SWCraft user guide:\n"
        "the same pages `swcraft docs` serves from an installed toolchain. It is a\n"
        "static web site -- HTML, CSS and JavaScript, and nothing to install.\n"
        "\n"
        "SERVE IT -- DO NOT OPEN THE FILES DIRECTLY\n"
        "\n"
        "Opening index.html from the file system does not work, and fails quietly:\n"
        "every internal link is a directory URL, so a browser reading the files\n"
        "directly shows a directory listing instead of the page, and the search box\n"
        "finds nothing because the browser refuses to load the search index that way.\n"
        "Any static web server fixes both; the guide needs nothing on the server side.\n"
        "\n"
        "The shortest way, with Python 3 on the machine:\n"
        "\n"
        f"    cd {root}\n"
        "    python -m http.server\n"
        "\n"
        "then open the address it prints (http://localhost:8000/) in a browser.\n"
        "Stop the server with Ctrl+C when you are done.\n"
        "\n"
        "If you have the SWCraft toolchain installed, you do not need this zip at\n"
        "all: `swcraft docs` serves the guide that came with the installation and\n"
        "opens a browser on it.\n"
    )


def archive_docs_site(site_dir: Path, destination: Path, epoch: str) -> Path:
    """Zip *site_dir* to *destination* reproducibly; returns *destination*.

    Pinned against the host: member order (sorted by archive path), each member's mtime
    (:func:`zip_timestamp`), mode (``0o644``) and ``create_system`` byte (3, Unix; ``zipfile`` would
    write 0 on Windows). No directory entries are written. Members are rooted at ``<archive stem>/``,
    except :data:`DOCS_ARCHIVE_README` at the top level, outside the site so a served copy of the
    site does not publish it.

    The unpacked copy has to be served, not opened: over ``file://`` every directory URL
    (``use_directory_urls: true``) renders as a directory listing, and search fails (the index fetch
    and the Web Worker are refused, and the ``offline`` plugin that would emit a fallback is not
    enabled). Any static file server works.
    """
    if not site_dir.is_dir():
        raise BuildArtifactsError(f"the rendered site is missing: {site_dir}")
    root = destination.name[: -len(DOCS_ARCHIVE_SUFFIX)]
    members = sorted(
        (f"{root}/{path.relative_to(site_dir).as_posix()}", path) for path in site_dir.rglob("*") if path.is_file()
    )
    if not members:
        raise BuildArtifactsError(f"the rendered site is empty: {site_dir}")

    # The note is added after the emptiness check, so it cannot stand in for an empty site.
    payloads = sorted(
        [(arcname, path.read_bytes()) for arcname, path in members]
        + [(DOCS_ARCHIVE_README, render_docs_archive_readme(root).encode("utf-8"))]
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = zip_timestamp(epoch)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in payloads:
            info = zipfile.ZipInfo(arcname, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = _ZIP_CREATE_SYSTEM_UNIX
            archive.writestr(info, data)
    return destination


def build_docs_archive(
    repo_root: Path,
    dist_root: Path,
    *,
    run: Runner = _run,
    python_executable: str = sys.executable,
) -> Path:
    """Render the guide and archive it; returns ``dist/docs/swcraft-guide-<v>.zip``.

    The rendered ``site/`` is left beside the archive, as ``dev.py docs`` leaves it. Both are rooted at
    *dist_root*.
    """
    site_dir = build_docs_site(
        repo_root,
        run=run,
        python_executable=python_executable,
        site_dir=docs_site_dir(dist_root),
    )
    destination = dist_root / DOCS_DIRNAME / docs_archive_filename(read_version(repo_root))
    return archive_docs_site(site_dir, destination, source_date_epoch(repo_root))


#: The pauses between attempts of a directory rename or removal in the guide payload swap. On
#: Windows a directory cannot be renamed while a handle inside it is open (a scanner on the freshly
#: copied tree is the ordinary cause, and it clears on its own). About 1.5 s in total: enough for a
#: handle about to be released, too little to hide a permanent refusal. Not guarded by
#: ``sys.platform``, so the Windows path is the one every run executes.
GUIDE_TREE_RETRY_WAITS = (0.1, 0.2, 0.4, 0.8)


def _retry_guide_tree_operation(operation: Callable[[], None]) -> None:
    """Run *operation*, retrying an ``OSError`` over :data:`GUIDE_TREE_RETRY_WAITS`.

    The last attempt's exception propagates unchanged. What this does not reach: it cannot tell a
    transient failure from a permanent one, nor say why an attempt failed.
    """
    for wait in GUIDE_TREE_RETRY_WAITS:
        try:
            operation()
            return
        except OSError:
            time.sleep(wait)
    operation()


def _remove_guide_tree(path: Path, what: str) -> None:
    """Remove *path* if it is there, naming *what* and the reason when it cannot.

    Not ``ignore_errors=True``: a swallowed failure would surface one step later without its reason.
    Called only on the two sidecar trees, never on the live payload.
    """
    if not path.exists():
        return
    try:
        _retry_guide_tree_operation(lambda: shutil.rmtree(path))
    except OSError as exc:
        raise BuildArtifactsError(f"could not remove {what} at {path}: {exc}") from exc


def _discard_guide_tree(path: Path) -> None:
    """Drop a sidecar tree the build no longer needs; a removal that fails is ignored.

    On a failure path raising would replace the build's reason with the cleanup's; on the success path
    it would fail a build whose outputs are correct. A tree left behind is gitignored, excluded from
    the fingerprint and from the wheel and sdist (``packages/tools/pyproject.toml``'s ``exclude``), and
    removed by the next install.
    """
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def install_licence_payload(repo_root: Path) -> list[Path]:
    """Copy the root ``LICENSE`` and ``NOTICE`` into each Python project's root; returns the copies.

    Called before any wheel is built. Written straight onto the destination, with no temporary name,
    because hatchling's ``LICEN[CS]E*`` pattern would ship a leftover ``LICENSE.incoming``. A missing
    root file is refused by name. Not reached: the ``.vsix``, whose copies ``package-vsix.mjs`` stages.
    """
    missing = [name for name in LICENCE_FILENAMES if not (repo_root / name).is_file()]
    if missing:
        raise BuildArtifactsError(
            f"{', '.join(missing)} missing at the repository root, so the wheels would ship without it"
        )
    copies: list[Path] = []
    for project in PYTHON_PROJECTS:
        for name in LICENCE_FILENAMES:
            destination = repo_root / PACKAGES_DIR / project / name
            destination.write_bytes((repo_root / name).read_bytes())
            copies.append(destination)
    return copies


def install_guide_site_payload(repo_root: Path, site_dir: Path) -> Path:
    """Copy the rendered guide into the wheel's package tree; returns that directory.

    The destination is :data:`PACKAGE_GUIDE_SITE_DIR`, where ``swcraft docs`` resolves it through
    ``importlib.resources``.

    Nothing is written into or deleted out of the live payload: the tree is copied to a sibling, the
    old payload renamed aside, the copy renamed into place, and only then the old one removed. A
    partial write or removal would leave a truncated tree whose ``index.html`` satisfies the locator's
    guard, so every destructive step is a rename: a failed one leaves the previous payload whole under
    its own name (a failed second rename puts it back first; only a failed restoration leaves it under
    :data:`PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME`, and the message says which). Each rename is retried
    over :data:`GUIDE_TREE_RETRY_WAITS`, because on Windows a directory rename fails while any handle
    inside the tree is open. *site_dir* must hold ``index.html``, refused before anything is moved.

    What this does not check: whether the tree is a complete or current site (a presence check on
    one file, as the locator's). A process killed between the two renames leaves no payload, which
    the locator refuses; and a rename blocked for longer than the retries still fails, with the last
    attempt's error.
    """
    if not (site_dir / SITE_INDEX_FILENAME).is_file():
        raise BuildArtifactsError(f"the rendered guide has no {SITE_INDEX_FILENAME} to package: {site_dir}")
    destination = repo_root / PACKAGE_GUIDE_SITE_DIR
    staging = destination.parent / PACKAGE_GUIDE_SITE_STAGING_DIRNAME
    superseded = destination.parent / PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME
    _remove_guide_tree(staging, "the leftover guide payload staging directory")
    _remove_guide_tree(superseded, "the leftover superseded guide payload directory")
    try:
        shutil.copytree(site_dir, staging)
    except OSError as exc:
        # ``shutil.Error`` (a file ``copytree`` could not read) is an ``OSError`` too.
        _discard_guide_tree(staging)
        raise BuildArtifactsError(f"could not stage the guide payload from {site_dir}: {exc}") from exc
    moved_aside = destination.exists()
    if moved_aside:
        try:
            _retry_guide_tree_operation(lambda: destination.replace(superseded))
        except OSError as exc:
            _discard_guide_tree(staging)
            raise BuildArtifactsError(
                f"could not move the previous guide payload aside from {destination}: {exc}"
            ) from exc
    try:
        _retry_guide_tree_operation(lambda: staging.replace(destination))
    except OSError as exc:
        # Put the previous payload back under its own name before reporting.
        restored = True
        if moved_aside:
            try:
                _retry_guide_tree_operation(lambda: superseded.replace(destination))
            except OSError:
                restored = False
        _discard_guide_tree(staging)
        where = (
            f"the previous payload is unchanged at {destination}"
            if restored
            else f"the previous payload could not be put back and is at {superseded}"
        )
        raise BuildArtifactsError(
            f"could not move the staged guide payload into {destination}: {exc} -- {where}"
        ) from exc
    _discard_guide_tree(superseded)
    return destination


def write_manifest(dist_root: Path, manifest: dict[str, object]) -> Path:
    """Write ``manifest.json`` and ``SHA256SUMS``; returns the manifest path."""
    manifest_path = dist_root / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    entries = manifest["artifacts"]
    if not isinstance(entries, list):
        raise BuildArtifactsError(f"manifest 'artifacts' must be a list, not {type(entries).__name__}")
    # `sha256sum -c` reads a trailing `\r` as part of the file name, so a CRLF sums file fails.
    (dist_root / SHA256SUMS_FILENAME).write_text(format_sha256sums(entries), encoding="utf-8", newline="\n")
    return manifest_path


def build_all(
    repo_root: Path = REPO_ROOT,
    *,
    run: Runner = _run,
    python_executable: str = sys.executable,
    npm: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Run the whole artifact build; returns the written manifest path.

    The manifest is written last, so an aborted build leaves no manifest rather than a partial one.
    """
    # Before the wheels, so a missing npm does not waste the expensive half of the run.
    npm_executable = require_npm(npm)
    # Checked, never fetched: `artifacts` stays offline, so an absent or stale payload is refused
    # before anything in `dist/` is touched.
    require_elkjs_payload(repo_root)

    dist_root = repo_root / DIST_DIRNAME
    clean_output_dirs(dist_root)

    # Installed before any wheel is built, so the wheels describe the commit, not developer state;
    # copied, not re-rendered, so the archive and the payload are the same bytes.
    docs_archive = build_docs_archive(repo_root, dist_root, run=run, python_executable=python_executable)
    install_guide_site_payload(repo_root, docs_site_dir(dist_root))
    install_licence_payload(repo_root)

    artifacts = build_python_artifacts(repo_root, dist_root, run=run, python_executable=python_executable)
    artifacts.append(build_vscode_artifact(repo_root, dist_root, run=run, npm=npm_executable))
    artifacts.append(docs_archive)

    manifest = build_manifest(
        version=read_version(repo_root),
        git_commit=read_git_commit(repo_root),
        built_at=now or datetime.now(UTC),
        artifact_paths=artifacts,
        sources=source_inventory(repo_root),
    )
    return write_manifest(dist_root, manifest)


# ---------------------------------------------------------------------------
# The workstation archive
# ---------------------------------------------------------------------------


def workstation_platform_token(platform_tag: str | None = None) -> str:
    """``windows-x64`` / ``macos-arm64`` / ``linux-x64`` for a ``sysconfig`` tag.

    The token carries the architecture, not only the OS. Any other tag is refused by name, never
    guessed: a wrong guess surfaces offline as pip failing to find a distribution. The first of three
    platform checks, beside ``wheels/PLATFORM.json`` and the installer's ``check-platform``: build,
    archive and install are checked separately.
    """
    tag = sysconfig.get_platform() if platform_tag is None else platform_tag
    if tag == "win-amd64":
        return "windows-x64"
    if tag == "linux-x86_64":
        return "linux-x64"
    if tag.startswith("macosx-") and tag.endswith("-arm64"):
        return "macos-arm64"
    raise BuildArtifactsError(
        f"{tag!r} is not a platform the workstation archive is built for -- the three are "
        "win-amd64 (windows-x64), macosx-<version>-arm64 (macos-arm64) and linux-x86_64 (linux-x64). "
        "Build the archive on one of them; the closure inside it cannot be cross-resolved."
    )


def workstation_archive_filename(version: str, token: str) -> str:
    """``swcraft-<version>-<platform token>.zip``."""
    return f"{WORKSTATION_ARCHIVE_PREFIX}{version}-{token}{WORKSTATION_ARCHIVE_SUFFIX}"


def wheel_distribution_and_version(filename: str) -> tuple[str, str] | None:
    """``(canonical distribution, version)`` of a wheel filename, or ``None``.

    The distribution is canonicalized (PEP 503), so ``rpds_py`` and ``rpds-py`` are one.
    """
    if not filename.endswith(".whl"):
        return None
    fields = filename[: -len(".whl")].split("-")
    if len(fields) < 2:
        return None
    return re.sub(r"[-_.]+", "-", fields[0]).lower(), fields[1]


def duplicate_distributions(wheelhouse: Path) -> dict[str, list[str]]:
    """Distributions the wheelhouse holds in more than one version, by name (read from the filenames)."""
    versions: dict[str, set[str]] = {}
    for wheel in wheelhouse.glob("*.whl"):
        pair = wheel_distribution_and_version(wheel.name)
        if pair is not None:
            versions.setdefault(pair[0], set()).add(pair[1])
    return {name: sorted(found) for name, found in sorted(versions.items()) if len(found) > 1}


def require_single_version_wheelhouse(wheelhouse: Path) -> None:
    """Refuse a wheelhouse that has accreted, naming every offending distribution.

    Shipping duplicates would leave which version installs to pip's ordering over a listing.
    :func:`build_wheelhouse` replaces the directory whole, so this is rare; it guards a wheelhouse
    from before that rule and a wheel dropped in by hand between runs.
    """
    duplicates = duplicate_distributions(wheelhouse)
    if not duplicates:
        return
    named = "; ".join(f"{name} ({', '.join(versions)})" for name, versions in duplicates.items())
    raise BuildArtifactsError(
        f"{wheelhouse} holds more than one version of {len(duplicates)} distribution(s) -- {named}. "
        "An archive built from it would ship both and let pip's ordering decide which one installs. "
        "Repopulate it with `python scripts/dev.py wheelhouse`, which empties it first."
    )


def require_wheels_only(wheelhouse: Path) -> None:
    """Refuse a wheelhouse holding anything but wheels and ``PLATFORM.json``, naming every such file.

    ``pip download`` takes an sdist when no wheel exists for the platform; it would ship unlisted in
    the notices and be built on the colleague's machine.
    """
    others = sorted(
        path.name
        for path in wheelhouse.iterdir()
        if path.name != WHEELHOUSE_PLATFORM_FILENAME and not (path.is_file() and path.suffix == ".whl")
    )
    if others:
        raise BuildArtifactsError(
            f"{wheelhouse} holds {', '.join(others)}, which is not a wheel: the archive ships wheels alone, each "
            "listed in its notices. Find which requirement has no wheel for this platform, then repopulate it with "
            "`python scripts/dev.py wheelhouse`."
        )


def require_vscode_artifact(directory: Path, version: str) -> Path:
    """The ``.vsix`` for *version*, proven present -- named, not resolution-failed."""
    vsix = directory / f"swcraft-vscode-{version}{VSIX_SUFFIX}"
    if not vsix.is_file():
        raise BuildArtifactsError(
            f"{vsix.name} is missing from {directory} -- the workstation archive ships the extension, "
            "so run `python scripts/dev.py artifacts` first."
        )
    return vsix


def workstation_archive_members(repo_root: Path, dist_root: Path, vsix: Path, token: str) -> list[tuple[str, Path]]:
    """``(archive path, source file)`` for every member but ``SHA256SUMS``.

    Sorted by the archive path, POSIX-spelled, so directory order cannot reach the bytes. Flat by
    design: ``swcraft_install.cmd`` runs ``install.py`` beside itself, and the extension's Install
    Toolchain command accepts the unpacked directory. Beside the files of ``scripts/workstation/`` (no
    allow-list; its ``README.md`` is the archive's documentation) sit ``wheels/`` and ``runtime/``
    (:func:`runtime_archive_members`).
    """
    scripts = repo_root / WORKSTATION_SCRIPTS_DIR
    members = [(path.name, path) for path in scripts.iterdir() if path.is_file()]
    members.append((vsix.name, vsix))
    wheelhouse = dist_root / WHEELHOUSE_DIRNAME
    members.extend(
        (f"{WORKSTATION_WHEELS_DIRNAME}/{path.name}", path) for path in wheelhouse.iterdir() if path.is_file()
    )
    members.extend(runtime_archive_members(repo_root, dist_root, token))
    return sorted(members, key=lambda member: member[0])


def workstation_member_bytes(arcname: str, path: Path) -> bytes:
    """The bytes *arcname* is written with -- the file's own, with one exception.

    ``swcraft_install.cmd`` is converted to CRLF here: the repository is ``eol=lf`` and an LF-only
    batch file is a documented ``cmd.exe`` hazard. Deterministic, and the checkout is untouched. The
    shell wrapper is not converted: a CR would break its shebang.
    """
    data = path.read_bytes()
    if arcname == WORKSTATION_BATCH_WRAPPER:
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def workstation_member_mode(arcname: str) -> int:
    """The member's full ``st_mode``: a regular file, ``0o755`` for the shell wrapper.

    Derived from the archive name, not the checkout (a Windows clone records no exec bit).
    ``stat.S_IFREG`` is required: with a zero file-type nibble, ``ditto -x -k`` (Finder's double-click
    unpack) ignores the permissions and the wrapper arrives not executable.

    What this does not reach: Windows and Linux extractors other than ``unzip``; on Windows the mode
    does not make a file runnable anyway.
    """
    permissions = 0o755 if arcname == WORKSTATION_SHELL_WRAPPER else 0o644
    return stat.S_IFREG | permissions


def workstation_member_compression(arcname: str) -> int:
    """``ZIP_STORED`` for a payload that is already a compressed container, else deflate.

    The runtime payloads and the companion extensions are compressed already, so deflating them again
    costs tens of megabytes of work for a few kilobytes. Every other member keeps deflate, so the
    bytes of existing members do not move.
    """
    if arcname.startswith(f"{WORKSTATION_RUNTIME_DIRNAME}/") and arcname.endswith(WORKSTATION_STORED_SUFFIXES):
        return zipfile.ZIP_STORED
    if arcname.startswith(f"{WORKSTATION_EXTENSIONS_DIRNAME}/") and arcname.endswith(VSIX_SUFFIX):
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


#: A file at a ``.dist-info``'s root that is a licence file by its name, for a wheel that predates
#: PEP 639's ``licenses/`` directory (measured: ``scipy``, ``mdurl``, ``sniffio`` and others).
_DIST_INFO_LICENCE_FILE = re.compile(r"(licen[cs]e|copying|notice|authors)[^/]*", re.IGNORECASE)


def _single_line(value: str | None) -> str:
    """*value* stripped when it is one non-empty line, else ``""`` -- a ``License`` that is a text, not a name."""
    value = (value or "").strip()
    return value if value and "\n" not in value else ""


def read_wheel_notice(wheel: Path) -> dict[str, object]:
    """What *wheel*'s own ``.dist-info`` declares: ``name``, ``version``, ``licence``, its ``source`` and ``files``.

    The licence, from ``METADATA``: ``License-Expression``, else a one-line ``License`` (a multi-line
    one is a pasted text), else the ``License ::`` classifiers' last segments, else ``not declared``.
    The files: every member under ``<dist-info>/licenses/`` (PEP 639) and every licence-named file at
    the ``.dist-info``'s root. Not reached: a licence file outside the ``.dist-info``, and whether the
    declaration is complete.
    """
    metadata, member, names = _wheel_metadata(wheel, "its notice cannot be written")
    dist_info = member.split("/", 1)[0]
    classifiers = [
        value.split("::")[-1].strip()
        for value in metadata.get_all("Classifier") or []
        if value.strip().startswith("License ::")
    ]
    expression = _single_line(metadata.get("License-Expression"))
    named = _single_line(metadata.get("License"))
    if expression:
        licence, source = expression, "its `License-Expression` field"
    elif named:
        licence, source = named, "its `License` field"
    elif classifiers:
        licence, source = "; ".join(classifiers), "its `License ::` classifiers"
    else:
        licence, source = "not declared", ""
    files = sorted(
        name
        for name in names
        if not name.endswith("/")
        and (
            name.startswith(f"{dist_info}/licenses/")
            or (
                name.count("/") == 1
                and name.startswith(f"{dist_info}/")
                and _DIST_INFO_LICENCE_FILE.fullmatch(name.split("/")[1])
            )
        )
    )
    return {
        "name": str(metadata.get("Name") or wheel.name.split("-")[0]),
        "version": str(metadata.get("Version") or wheel.name.split("-")[1]),
        "licence": licence,
        "source": source,
        "files": files,
    }


def render_python_packages_notices(wheelhouse: Path) -> str:
    """The ``## Python packages`` section of the archive's notices: every third-party wheel of *wheelhouse*.

    Each wheel's :func:`read_wheel_notice`, in canonical-name order, so the section moves only when a
    wheel does. SWCraft's own wheels are not listed; a wheel declaring nothing is listed as ``not
    declared``, never skipped. The licence texts stay inside each wheel, at the paths listed.
    """
    own = {requirement_name(name) for name in SWCRAFT_DISTRIBUTIONS}
    wheels = []
    for wheel in wheelhouse.glob("*.whl"):
        pair = wheel_distribution_and_version(wheel.name)
        if pair is not None and pair[0] in own:
            continue
        wheels.append(((pair[0] if pair else wheel.name.lower()), wheel.name, wheel))
    lines = [
        "## Python packages",
        "",
        "The wheels under `wheels/`, which the installer installs into the toolchain's environment. Each",
        "third-party wheel is listed with the licence its own `METADATA` declares and the licence files its",
        "`.dist-info` carries, whose texts are inside the wheel.",
        "SWCraft's own wheels, the specimen generator's among them, are not third-party and are not listed.",
    ]
    for _key, filename, wheel in sorted(wheels):
        notice = read_wheel_notice(wheel)
        declared = notice["licence"] if not notice["source"] else f"{notice['licence']} ({notice['source']})"
        lines += [
            "",
            f"### `{notice['name']}` {notice['version']}",
            "",
            f"The `{WORKSTATION_WHEELS_DIRNAME}/{filename}` file.",
            "",
            f"Declared licence: {declared}",
            "",
        ]
        if notice["files"]:
            lines += ["Licence files inside it:", "", *(f"- `{name}`" for name in notice["files"])]
        else:
            lines.append("It carries no licence file in its `.dist-info`.")
    return "\n".join(lines) + "\n"


def workstation_notices(runtime_notices: Path, wheelhouse: Path) -> bytes:
    """The archive's ``THIRD-PARTY-NOTICES.md``: ``runtime``'s document as written, then the Python packages section."""
    head = runtime_notices.read_text(encoding="utf-8").rstrip("\n")
    return f"{head}\n\n{render_python_packages_notices(wheelhouse)}".encode()


def build_workstation_archive(repo_root: Path = REPO_ROOT) -> Path:
    """Compose ``dist/workstation/swcraft-<version>-<platform>.zip``; returns it.

    Downloads nothing and builds nothing: every input is on disk (``scripts/workstation/``, the
    ``.vsix``, ``dist/wheelhouse/`` and ``dist/runtime/``), which is why it follows ``artifacts``,
    ``runtime`` and ``wheelhouse`` and is not a network command. Not a manifest entry: like the
    wheelhouse it is built for one platform and one interpreter; its ``SHA256SUMS`` beside the zip
    records it.

    The wheel refusals below prevent the opaque pip resolution error a colleague would otherwise meet
    offline; the runtime refusals (:func:`runtime_archive_members`) prevent another platform's Node or
    a payload a pin bump left behind. All run before anything is written. The notices are
    ``runtime``'s document followed by the *Python packages* section (:func:`workstation_notices`); a
    third-party wheel without readable ``METADATA`` is refused by name.

    Every member's bytes are held in memory before the zip is written (the sums are over the final
    bytes): a few hundred MB, accepted on a runner that holds the full gate; streaming is the repair if
    one is ever needed.

    What this does not reach: beyond ``METADATA`` it does not open a third-party wheel, so a corrupt
    one whose ``METADATA`` still reads travels unremarked; it takes ``PLATFORM.json`` as the truth
    about the closure (an assertion about the interpreter that populated it, not every wheel); and it
    does not open a runtime payload, only compares digests, taking ``runtime``'s notices and
    ``RUNTIME.json`` as written.
    """
    dist_root = repo_root / DIST_DIRNAME
    version = read_version(repo_root)
    token = workstation_platform_token()

    # Refuse before anything is written: a half-composed dist/workstation/ could be handed on.
    require_swcraft_wheels(dist_root / PYTHON_DIRNAME, version)
    wheelhouse = dist_root / WHEELHOUSE_DIRNAME
    # Against the Python this archive carries (RUNTIME.json), never the one running this stage.
    check_wheelhouse_platform(wheelhouse, runtime_interpreter_stamp(dist_root / RUNTIME_DIRNAME))
    require_swcraft_wheels(wheelhouse, version)
    require_single_version_wheelhouse(wheelhouse)
    require_wheels_only(wheelhouse)
    vsix = require_vscode_artifact(dist_root / VSCODE_DIRNAME, version)

    members = workstation_archive_members(repo_root, dist_root, vsix, token)
    payloads = [
        (
            arcname,
            workstation_notices(path, wheelhouse)
            if arcname == THIRD_PARTY_NOTICES_FILENAME
            else workstation_member_bytes(arcname, path),
        )
        for arcname, path in members
    ]
    sums = format_sha256_lines((arcname, hashlib.sha256(data).hexdigest()) for arcname, data in payloads)
    payloads.append((SHA256SUMS_FILENAME, sums.encode("utf-8")))
    payloads.sort(key=lambda member: member[0])

    # Emptied so no previous version's archive sits beside the new one.
    workstation = dist_root / WORKSTATION_DIRNAME
    shutil.rmtree(workstation, ignore_errors=True)
    workstation.mkdir(parents=True, exist_ok=True)

    destination = workstation / workstation_archive_filename(version, token)
    timestamp = zip_timestamp(source_date_epoch(repo_root))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in payloads:
            info = zipfile.ZipInfo(arcname, date_time=timestamp)
            info.compress_type = workstation_member_compression(arcname)
            info.external_attr = workstation_member_mode(arcname) << 16
            info.create_system = _ZIP_CREATE_SYSTEM_UNIX
            archive.writestr(info, data)

    # `sha256sum -c` takes a trailing `\r` as part of the file name.
    (workstation / SHA256SUMS_FILENAME).write_text(
        format_sha256_lines([(destination.name, sha256_file(destination))]),
        encoding="utf-8",
        newline="\n",
    )
    return destination


# ---------------------------------------------------------------------------
# The runtime payloads: `dev.py runtime`
# ---------------------------------------------------------------------------

#: ``dist/runtime/`` -- the pinned payloads the workstation archive composes in beside ``wheels/``;
#: provisioning for one platform, not a manifest entry.
RUNTIME_DIRNAME = "runtime"

#: The shipped record of what one ``runtime`` run used; the pins file says what should be fetched.
RUNTIME_RECORD_FILENAME = "RUNTIME.json"

#: Every build-time pin and publisher digest, the ``elkjs`` payload's included. In a subdirectory,
#: because :func:`workstation_archive_members` ships every file directly in ``scripts/workstation/``.
RUNTIME_PINS_FILE = WORKSTATION_SCRIPTS_DIR / "pins" / "runtime-pins.json"

#: Where nodejs.org publishes a release: ``<this>/v<version>/<vendor file>``, beside the
#: release's own ``SHASUMS256.txt`` the pinned digests are transcribed from.
NODE_DIST_URL = "https://nodejs.org/dist"

#: The payload file names inside ``dist/runtime/``: no version and no platform token, so no path
#: changes with a bump. The Node file keeps the publisher's extension (:data:`RUNTIME_PLATFORMS`).
NODE_PAYLOAD_STEM = "node"
PYTHON_PAYLOAD_FILENAME = "python.tar.gz"

#: Where python-build-standalone publishes a release: ``<this>/<build tag>/<vendor file>``, beside
#: the ``SHA256SUMS`` asset the pinned digests are transcribed from.
PYTHON_RELEASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download"

#: The one leading directory of every ``install_only`` tarball, measured on all three
#: platforms: ``python/``. The installer strips it, and so does the build-time unpack.
PYTHON_ARCHIVE_ROOT = "python"

#: ``dist/runtime/python/`` -- the carried interpreter, unpacked at build time so ``dev.py
#: wheelhouse`` resolves the closure on it; never an archive member (that is the vendor's tarball).
RUNTIME_PYTHON_DIRNAME = "python"

#: The archive's platform token mapped to nodejs.org's spelling and file extension,
#: python-build-standalone's triple and where its interpreter and licence sit, and the Marketplace's
#: ``targetPlatform``: one table rather than four ``if`` chains. The extensions are the publishers'
#: (no ``.tar.xz`` of Node is published for macOS); ``{minor}`` is the pinned ``major.minor``.
RUNTIME_PLATFORMS: dict[str, dict[str, str]] = {
    "linux-x64": {
        "node_token": "linux-x64",
        "node_extension": ".tar.xz",
        "python_triple": "x86_64-unknown-linux-gnu",
        "python_executable": "bin/python3",
        "python_licence": "lib/python{minor}/LICENSE.txt",
        "vscode_target": "linux-x64",
    },
    "macos-arm64": {
        "node_token": "darwin-arm64",
        "node_extension": ".tar.gz",
        "python_triple": "aarch64-apple-darwin",
        "python_executable": "bin/python3",
        "python_licence": "lib/python{minor}/LICENSE.txt",
        "vscode_target": "darwin-arm64",
    },
    "windows-x64": {
        "node_token": "win-x64",
        "node_extension": ".zip",
        "python_triple": "x86_64-pc-windows-msvc",
        "python_executable": "python.exe",
        "python_licence": "LICENSE.txt",
        "vscode_target": "win32-x64",
    },
}

#: The Visual Studio Marketplace, with no account or token: ``POST <this>/extensionquery`` names
#: versions, ``GET <this>/publishers/<publisher>/vsextensions/<name>/<version>/vspackage`` serves one
#: package (``HEAD`` answers ``405``).
MARKETPLACE_GALLERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery"

#: The ``targetPlatform`` of a Marketplace build that declares none: one package for every
#: platform. Written into the pins file by this name, so a pin always says which build it is.
UNIVERSAL_TARGET = "universal"

#: A ``publisher.name`` extension identifier, the way VS Code resolves one from the gallery.
_EXTENSION_ID = re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*")

#: The notices document ``runtime`` writes and the archive carries at its root.
THIRD_PARTY_NOTICES_FILENAME = "THIRD-PARTY-NOTICES.md"

#: A pinned version is exact -- ``X.Y.Z`` and nothing else -- because a range pins nothing.
_EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}")
#: A python-build-standalone release tag: the eight digits of its date.
_BUILD_TAG = re.compile(r"\d{8}")

#: The ``User-Agent`` every request this module makes names itself with.
_USER_AGENT = "swcraft-build-artifacts"


def _is_exact(pattern: re.Pattern[str], value: object) -> bool:
    """Whether *value* is a string *pattern* matches whole -- the one test behind every pin shape."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _require_exact_version(value: object, where: str) -> None:
    """*value* an exact ``X.Y.Z`` version, or a refusal naming *where* (``<file>: <key>.version``)."""
    if not _is_exact(_EXACT_VERSION, value):
        raise BuildArtifactsError(f"{where} must be an exact X.Y.Z version, not {value!r}.")


def _require_every_platform(platforms: object, where: str) -> list[str]:
    """The tokens *platforms* pins, sorted, when they are exactly :data:`RUNTIME_PLATFORMS`'; else a refusal."""
    pinned = sorted(platforms) if isinstance(platforms, dict) else []
    if pinned != sorted(RUNTIME_PLATFORMS):
        raise BuildArtifactsError(
            f"{where} must pin exactly {', '.join(sorted(RUNTIME_PLATFORMS))}; "
            f"it pins {', '.join(pinned) or 'nothing'}."
        )
    return pinned


def _require_digest(value: object, where: str) -> None:
    """*value* a SHA-256 as the pins spell one, or a refusal naming *where* (``<...>.sha256``)."""
    if not _is_exact(_SHA256_DIGEST, value):
        raise BuildArtifactsError(f"{where} must be 64 lowercase hexadecimal digits, not {value!r}.")


#: The one sentence every refusal about ``dist/runtime/`` ends with.
_RUNTIME_RERUN = "Repopulate it with `python scripts/dev.py runtime`."

#: Seconds one read of a download may stall; the transfer is then retried over
#: :data:`DOWNLOAD_RETRY_WAITS`, and only then fails by name with ``dist/runtime/`` untouched.
DOWNLOAD_TIMEOUT_SECONDS = 60

#: Seconds waited before the second, third and fourth attempt of a download or Marketplace query that
#: failed in transport (:func:`_is_transport_failure`): a reset connection would otherwise cost a
#: whole CI run, since a run is re-run whole. A content refusal is never retried. Read at call time,
#: so a test patches it. Nothing is cached between runs.
DOWNLOAD_RETRY_WAITS = (5, 15, 45)

#: ``fetch(url, destination)`` -- writes the body at *url* to *destination*, raising on failure.
Fetcher = Callable[[str, Path], None]


def runtime_platform(token: str) -> dict[str, str]:
    """The :data:`RUNTIME_PLATFORMS` row for *token*, or a refusal naming it."""
    row = RUNTIME_PLATFORMS.get(token)
    if row is None:
        raise BuildArtifactsError(
            f"{token!r} is not a platform the runtime payloads are built for -- the three are "
            f"{', '.join(sorted(RUNTIME_PLATFORMS))}."
        )
    return row


def node_vendor_filename(version: str, token: str) -> str:
    """nodejs.org's own file name for *version* on *token*: ``node-v<version>-win-x64.zip``."""
    row = runtime_platform(token)
    return f"node-v{version}-{row['node_token']}{row['node_extension']}"


def node_download_url(version: str, token: str) -> str:
    """The URL nodejs.org serves :func:`node_vendor_filename` at."""
    return f"{NODE_DIST_URL}/v{version}/{node_vendor_filename(version, token)}"


def node_payload_filename(token: str) -> str:
    """``node.tar.xz`` / ``node.tar.gz`` / ``node.zip`` -- version-free, publisher's extension."""
    return f"{NODE_PAYLOAD_STEM}{runtime_platform(token)['node_extension']}"


def _read_json_object(path: Path, repo_root: Path) -> dict[str, object]:
    """*path* parsed as a JSON object, or a refusal naming it repository-relatively."""
    name = path.relative_to(repo_root).as_posix()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildArtifactsError(f"{name} is missing -- it is a committed pin, so restore it from git.") from None
    except (OSError, json.JSONDecodeError) as error:
        raise BuildArtifactsError(f"{name} is not readable JSON: {error}") from error
    if not isinstance(data, dict):
        raise BuildArtifactsError(f"{name} must hold a JSON object, not {type(data).__name__}.")
    return data


def read_runtime_pins(repo_root: Path = REPO_ROOT) -> dict[str, object]:
    """The committed pins file, proven complete for every platform, or a refusal.

    Checked whole, for all three tokens, before anything is fetched: a missing platform is a defect of
    the commit and fails on every host, not only on the one that builds it.
    """
    return check_runtime_pins(_read_json_object(repo_root / RUNTIME_PINS_FILE, repo_root))


def check_runtime_pins(pins: dict[str, object]) -> dict[str, object]:
    """*pins* proven complete, or a refusal naming the pins file and the key -- :func:`read_runtime_pins`' check.

    Also called by ``update-pins`` on what it minted, before it writes it.
    """
    name = RUNTIME_PINS_FILE.as_posix()
    for section in ("node", "python"):
        release = pins.get(section)
        if not isinstance(release, dict):
            raise BuildArtifactsError(f"{name} has no {section!r} object.")
        _require_exact_version(release.get("version"), f"{name}: {section}.version")
        platforms = release.get("platforms")
        for token in _require_every_platform(platforms, f"{name}: {section}.platforms"):
            entry = platforms[token]
            digest = entry.get("sha256") if isinstance(entry, dict) else None
            _require_digest(digest, f"{name}: {section}.platforms.{token}.sha256")
    build_tag = pins["python"].get("build_tag")
    if not _is_exact(_BUILD_TAG, build_tag):
        raise BuildArtifactsError(
            f"{name}: python.build_tag must be the python-build-standalone release tag, eight digits, "
            f"not {build_tag!r}."
        )
    elkjs = pins.get(ELKJS_PACKAGE)
    if not isinstance(elkjs, dict):
        raise BuildArtifactsError(f"{name} has no {ELKJS_PACKAGE!r} object.")
    _require_exact_version(elkjs.get("version"), f"{name}: {ELKJS_PACKAGE}.version")
    _require_digest(elkjs.get("sha256"), f"{name}: {ELKJS_PACKAGE}.sha256")
    _check_extension_pins(pins.get("extensions"), name)
    _refuse_the_retired_key(pins, name)
    return pins


def _check_extension_pins(extensions: object, name: str) -> None:
    """The ``extensions`` list: one entry per companion, in the pack's order, pinned for every token.

    Each entry names the extension, one exact version, and per platform the build it takes
    (:data:`UNIVERSAL_TARGET` or that platform's own ``vscode_target``, so no platform gets another's
    native build) with its SHA-256.
    """
    if not isinstance(extensions, list) or not extensions:
        raise BuildArtifactsError(f"{name} has no 'extensions' list naming the companion extensions.")
    seen: set[str] = set()
    for index, entry in enumerate(extensions):
        where = f"{name}: extensions[{index}]"
        ident = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(ident, str) or not _EXTENSION_ID.fullmatch(ident):
            raise BuildArtifactsError(f"{where}.id must be a lowercase publisher.name identifier, not {ident!r}.")
        if ident in seen:
            raise BuildArtifactsError(f"{where} pins {ident} a second time.")
        seen.add(ident)
        _require_exact_version(entry.get("version"), f"{where} ({ident}).version")
        platforms = entry.get("platforms")
        for token in _require_every_platform(platforms, f"{where} ({ident}).platforms"):
            build = platforms[token]
            target = build.get("target") if isinstance(build, dict) else None
            allowed = (UNIVERSAL_TARGET, RUNTIME_PLATFORMS[token]["vscode_target"])
            if target not in allowed:
                raise BuildArtifactsError(
                    f"{where} ({ident}).platforms.{token}.target must be one of {', '.join(allowed)}, not {target!r}."
                )
            _require_digest(build.get("sha256"), f"{where} ({ident}).platforms.{token}.sha256")


#: A retired pins key, refused by name, never ignored: the generator's wheel is built from source.
RETIRED_RUNTIME_PINS_KEY = "bundled"


def _refuse_the_retired_key(pins: Mapping[str, object], name: str) -> None:
    """Refuse a pins object that still carries :data:`RETIRED_RUNTIME_PINS_KEY`, naming the key."""
    if RETIRED_RUNTIME_PINS_KEY in pins:
        raise BuildArtifactsError(
            f"{name} carries {RETIRED_RUNTIME_PINS_KEY!r}, which is retired: the specimen generator's wheel is "
            "built from packages/specimens/ by `python scripts/dev.py artifacts`, so remove the key."
        )
    return None


class ExtensionPin(NamedTuple):
    """One companion extension as the pins name it for one platform: a tuple, so it still unpacks positionally."""

    id: str
    version: str
    target: str
    sha256: str


def extension_pins(pins: dict[str, object], token: str) -> list[ExtensionPin]:
    """``(id, version, target, sha256)`` of each companion extension for *token*, in the pins' order."""
    runtime_platform(token)
    return [
        ExtensionPin(
            entry["id"], entry["version"], entry["platforms"][token]["target"], entry["platforms"][token]["sha256"]
        )
        for entry in pins["extensions"]
    ]


def extension_payload_filename(ident: str) -> str:
    """``<publisher>.<name>.vsix`` -- no version and no target platform in the name."""
    return f"{ident}.vsix"


def extension_download_url(ident: str, version: str, target: str) -> str:
    """The Marketplace URL of one build: ``?targetPlatform=`` picks a platform build, its absence the universal one."""
    publisher, name = ident.split(".", 1)
    url = f"{MARKETPLACE_GALLERY_URL}/publishers/{publisher}/vsextensions/{name}/{version}/vspackage"
    if target != UNIVERSAL_TARGET:
        url += "?" + urllib.parse.urlencode({"targetPlatform": target})
    return url


def decode_marketplace_package(path: Path) -> Path:
    """Gunzip the Marketplace's body in place, so what is hashed and stored is the ``.vsix`` itself.

    The ``vspackage`` endpoint answers gzip-encoded even under ``Accept-Encoding: identity``, and
    :mod:`urllib` does not decode it. The gzip magic tells it from a zip; anything else is left as
    it came, for the pinned digest to refuse.
    """
    with path.open("rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        path.write_bytes(gzip.decompress(path.read_bytes()))
    return path


def node_pin(pins: dict[str, object], token: str) -> tuple[str, str]:
    """``(version, sha256)`` of the Node payload for *token*, from :func:`read_runtime_pins`' result."""
    runtime_platform(token)
    node = pins["node"]
    return node["version"], node["platforms"][token]["sha256"]


def python_pin(pins: dict[str, object], token: str) -> tuple[str, str, str]:
    """``(version, build tag, sha256)`` of the CPython payload for *token*.

    One CPython version is rebuilt under many python-build-standalone tags, so the pin is the pair.
    """
    runtime_platform(token)
    python = pins["python"]
    return python["version"], python["build_tag"], python["platforms"][token]["sha256"]


def python_vendor_filename(version: str, build_tag: str, token: str) -> str:
    """The release's own asset name: ``cpython-<version>+<tag>-<triple>-install_only.tar.gz``."""
    return f"cpython-{version}+{build_tag}-{runtime_platform(token)['python_triple']}-install_only.tar.gz"


def python_download_url(version: str, build_tag: str, token: str) -> str:
    """The URL the release serves :func:`python_vendor_filename` at, its ``+`` percent-encoded."""
    filename = urllib.parse.quote(python_vendor_filename(version, build_tag, token))
    return f"{PYTHON_RELEASE_URL}/{build_tag}/{filename}"


def python_executable_in(tree: Path, token: str) -> Path:
    """The interpreter inside an unpacked CPython *tree*: ``bin/python3``, or ``python.exe`` at its root on Windows."""
    return tree.joinpath(*runtime_platform(token)["python_executable"].split("/"))


def runtime_python(dist_root: Path, token: str) -> Path:
    """``dist/runtime/python/bin/python3`` -- ``python.exe`` at the tree's root on Windows."""
    return python_executable_in(dist_root / RUNTIME_DIRNAME / RUNTIME_PYTHON_DIRNAME, token)


def _fetch_pinned(
    fetch: Fetcher,
    url: str,
    destination: Path,
    *,
    pinned: str,
    described: str,
    runtime: Path,
    decode: Callable[[Path], Path] | None = None,
) -> Path:
    """Fetch *url* to *destination* and answer it when its SHA-256 is *pinned*; otherwise a refusal naming *described*.

    *decode*, when given, turns the body into the file that is hashed and kept. *runtime* is the live
    directory the refusal promises is unchanged.
    """
    fetch(url, destination)
    received = sha256_file(destination if decode is None else decode(destination))
    if received != pinned:
        raise BuildArtifactsError(
            f"{described} from {url} has SHA-256 {received}, but {RUNTIME_PINS_FILE.as_posix()} pins {pinned} "
            f"-- nothing was staged and {runtime} is unchanged."
        )
    return destination


def require_runtime_python(repo_root: Path = REPO_ROOT, token: str | None = None) -> Path:
    """The carried interpreter ``runtime`` unpacked, or a refusal naming the stage to run.

    ``dev.py wheelhouse`` resolves the closure on it, never on the build host's interpreter.
    """
    token = workstation_platform_token() if token is None else token
    interpreter = runtime_python(repo_root / DIST_DIRNAME, token)
    if not interpreter.is_file():
        raise BuildArtifactsError(
            f"{interpreter} is missing -- the wheelhouse is downloaded for the Python the workstation "
            "archive carries, so run `python scripts/dev.py runtime` first."
        )
    return interpreter


def unpack_python_payload(archive: Path, destination: Path) -> Path:
    """Unpack the CPython ``install_only`` tarball into *destination*, its ``python/`` stripped.

    The build-time twin of the installer's unpack, version-free for the same reason. Every member
    must sit under :data:`PYTHON_ARCHIVE_ROOT`; a hard link's target is rewritten with the same strip,
    and the rest is :mod:`tarfile`'s ``data`` filter. Delegated to the standard library on purpose,
    so this side is an oracle independent of ``install.py``'s hand-written unpacker (which cannot
    delegate: ``--python`` may name an interpreter without the filter). Without the filter this call
    raises rather than unpack unfiltered.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = []
            for member in bundle.getmembers():
                parts = [part for part in member.name.split("/") if part not in ("", ".")]
                if not parts or parts[0] != PYTHON_ARCHIVE_ROOT:
                    raise BuildArtifactsError(
                        f"{archive.name} holds {member.name!r} outside its one leading "
                        f"{PYTHON_ARCHIVE_ROOT}/ directory, so it was not unpacked."
                    )
                if len(parts) == 1:
                    continue
                member.name = "/".join(parts[1:])
                if member.islnk():
                    member.linkname = "/".join(part for part in member.linkname.split("/")[1:] if part)
                members.append(member)
            bundle.extractall(destination, members=members, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise BuildArtifactsError(f"{archive.name} could not be unpacked into {destination}: {error}") from error
    return destination


class _BodyCutOff(Exception):
    """A body short of the ``Content-Length`` it declares -- raised in :func:`_download`, retried as transport."""


def _is_transport_failure(error: BaseException) -> bool:
    """Whether *error* failed the transport, which :func:`_retry_transport` retries, rather than the content.

    Retried: a :class:`urllib.error.URLError` that is not an ``HTTPError``; any other :class:`OSError`
    (a connection reset, a timeout); an :class:`http.client.HTTPException` (``IncompleteRead``); the
    ``Content-Length`` cut-off; an ``HTTPError`` with a 5xx code or 429. Never retried: any other
    ``HTTPError`` (a wrong pin URL's 404) and a certificate that fails verification, bare or as a
    ``URLError``'s ``reason``. ``HTTPError`` is asked first because it is itself a ``URLError``.

    What this does not reach: a local write error (a full disk) is an ``OSError`` and is retried like
    a dropped connection, then refused with its own error.
    """
    if isinstance(error, urllib.error.HTTPError):
        return 500 <= error.code < 600 or error.code == 429
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, urllib.error.URLError):
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    return isinstance(error, (OSError, http.client.HTTPException, _BodyCutOff))


_Attempted = TypeVar("_Attempted")


def _retry_transport(operation: Callable[[], _Attempted], describe: str) -> _Attempted:
    """Answer *operation*'s result, retrying a transport failure over :data:`DOWNLOAD_RETRY_WAITS`.

    A failure that is not transport, or the last attempt's, is the refusal ``<describe>: <error> (<N>
    attempt[s])``. Any exception outside the three classified families propagates unchanged.
    """
    waits = DOWNLOAD_RETRY_WAITS
    attempts = 0
    while True:
        attempts += 1
        try:
            return operation()
        except (OSError, http.client.HTTPException, _BodyCutOff) as error:
            if attempts > len(waits) or not _is_transport_failure(error):
                counted = "1 attempt" if attempts == 1 else f"{attempts} attempts"
                raise BuildArtifactsError(f"{describe}: {error} ({counted})") from error
            if isinstance(error, urllib.error.HTTPError):
                error.close()  # a retried 5xx/429 holds its response open; release it before the wait
            time.sleep(waits[attempts - 1])


def _download(url: str, destination: Path) -> None:
    """Stream *url* into *destination*; a transport failure, or a body cut off part way, is retried, then refused.

    A chunked body cut off raises ``IncompleteRead``; one with a ``Content-Length`` raises nothing on
    3.11, so the bytes written are compared with the declared length. *destination* is opened once and
    truncated by every attempt, so a retry leaves only a whole body; a refused download may leave it
    partial (every caller writes into a directory it discards). Each attempt flushes before counting,
    so a held-back write error belongs to it; a failure to close afterwards is refused without an
    attempt count.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        sink = destination.open("wb")
    except OSError as error:
        raise BuildArtifactsError(f"could not download {url}: {destination} cannot be written: {error}") from error

    def attempt() -> None:
        sink.seek(0)
        sink.truncate()
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            shutil.copyfileobj(response, sink, 1024 * 1024)
            declared = response.headers.get("Content-Length")
        sink.flush()
        written = sink.tell()
        if declared is not None and declared.strip().isdigit() and int(declared) != written:
            raise _BodyCutOff(f"the body was cut off, {written} of the {declared.strip()} bytes it declares arrived")

    try:
        _retry_transport(attempt, f"could not download {url}")
    except BaseException:
        with contextlib.suppress(OSError):
            sink.close()  # the refusal already raised is the one to report
        raise
    try:
        sink.close()
    except OSError as error:
        raise BuildArtifactsError(f"could not download {url}: {destination} could not be closed: {error}") from error


def _is_root_licence(name: str) -> bool:
    """``<leading directory>/LICENSE`` -- nodejs.org's licence file, one level inside each of its archives."""
    parts = [part for part in name.split("/") if part]
    return len(parts) == 2 and parts[1] == "LICENSE"


def read_node_licence(payload: Path) -> str:
    """The text of the ``LICENSE`` directly inside nodejs.org's own archive file."""
    text = None
    try:
        if payload.name.endswith(".zip"):
            with zipfile.ZipFile(payload) as bundle:
                names = sorted(name for name in bundle.namelist() if _is_root_licence(name))
                if names:
                    text = bundle.read(names[0]).decode("utf-8", "replace")
        else:
            with tarfile.open(payload, "r:*") as bundle:
                for member in bundle:
                    if member.isfile() and _is_root_licence(member.name):
                        text = bundle.extractfile(member).read().decode("utf-8", "replace")
                        break
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as error:
        raise BuildArtifactsError(f"{payload.name} could not be read for its licence: {error}") from error
    if text is None:
        raise BuildArtifactsError(
            f"{payload.name} holds no LICENSE directly inside its leading directory, so its notice cannot be written."
        )
    return text


def read_python_licence(tree: Path, version: str, token: str) -> str:
    """The text of the unpacked CPython tree's own ``LICENSE.txt``, where :data:`RUNTIME_PLATFORMS` says it sits."""
    relative = runtime_platform(token)["python_licence"].format(minor=_major_minor(version))
    try:
        return tree.joinpath(*relative.split("/")).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise BuildArtifactsError(
            f"the CPython tree holds no readable {relative} ({error}), so its notice cannot be written."
        ) from error


#: Where ``vsce`` puts an extension's own ``package.json`` inside a ``.vsix``, and the package
#: descriptor whose ``<License>`` names the licence file.
VSIX_PACKAGE_MANIFEST = "extension/package.json"
VSIX_PACKAGE_DESCRIPTOR = "extension.vsixmanifest"
_VSIX_LICENSE_ASSET = re.compile(r"<License>\s*([^<]+?)\s*</License>")
_VSIX_LICENSE_FILE = re.compile(r"extension/licen[cs]e[^/]*", re.IGNORECASE)


def read_vsix_notice(package: Path) -> dict[str, str]:
    """What a ``.vsix`` declares about itself: ``version``, ``licence``, and the licence ``text`` and its ``source``.

    The licence is the ``license`` field of the extension's own ``package.json``; the text is the
    member its ``extension.vsixmanifest`` names as ``<License>``, else the one ``LICENSE*`` file at
    ``extension/``'s root. Nothing is transcribed, so a relicensing changes the answer.
    """
    try:
        with zipfile.ZipFile(package) as bundle:
            names = set(bundle.namelist())
            manifest = json.loads(bundle.read(VSIX_PACKAGE_MANIFEST).decode("utf-8"))
            descriptor = (
                bundle.read(VSIX_PACKAGE_DESCRIPTOR).decode("utf-8", "replace")
                if VSIX_PACKAGE_DESCRIPTOR in names
                else ""
            )
            named = _VSIX_LICENSE_ASSET.search(descriptor)
            if named and named.group(1) in names:
                source = named.group(1)
            else:
                source = next(iter(sorted(name for name in names if _VSIX_LICENSE_FILE.fullmatch(name))), "")
            text = bundle.read(source).decode("utf-8", "replace") if source else ""
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BuildArtifactsError(
            f"{package.name} is not a readable .vsix ({error}), so its notice cannot be written."
        ) from error
    manifest = manifest if isinstance(manifest, dict) else {}
    declared = manifest.get("license")
    return {
        "version": str(manifest.get("version") or ""),
        "licence": declared if isinstance(declared, str) and declared else "not declared",
        "text": text,
        "source": source,
    }


def _fenced(text: str) -> list[str]:
    """*text* as a fenced block whose fence no run of backticks inside it can close."""
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", *body.split("\n"), fence]


def render_third_party_notices(
    *,
    node_version: str,
    node_licence: str,
    python_version: str,
    python_build_tag: str,
    python_licence: str,
    extensions: Sequence[tuple[str, str, str, dict[str, str]]],
    elkjs: dict[str, str],
) -> str:
    """``THIRD-PARTY-NOTICES.md``: every payload with the licence its publisher declares, and the text it ships.

    Generated from what travels with the payloads (and *elkjs*, :func:`read_elkjs_notice` of the
    wheel's payload), so nothing is a transcription that could outlive a relicensing. Deterministic.
    It does not judge compatibility between the licences.
    """
    lines = [
        "# Third-party notices",
        "",
        "This archive carries software that SWCraft did not write, beside it and inside the `swcraft-tools`",
        "wheel it installs. Each part is listed here with the licence its publisher declares for it and,",
        "where the part ships one, its licence text as published. Nothing here says whether those licences",
        "suit your use.",
        "",
        f"## Node.js {node_version}",
        "",
        "The `runtime/node.*` file, as nodejs.org publishes it. Its licence text, from the `LICENSE` file",
        "inside it:",
        "",
        *_fenced(node_licence),
        "",
        f"## Python {python_version}",
        "",
        "The `runtime/python.tar.gz` file, as the python-build-standalone project publishes it (release",
        f"{python_build_tag}). Its licence text, from the `LICENSE.txt` file inside it:",
        "",
        *_fenced(python_licence),
        "",
        f"## elkjs {elkjs['version']}",
        "",
        "The layout engine the diagram generator runs, as the npm registry publishes it. It is not a file",
        "beside this notice: five of its files ride inside the `swcraft-tools` wheel, under",
        "`swcraft_tools/writer/diagrams/elkjs/`, so every installation of that wheel carries it.",
        "",
        f"Declared licence: {elkjs['licence']}",
        "",
        f"SWCraft distributes it under {ELKJS_DISTRIBUTED_LICENCE}. Its licence text, from the `LICENSE.md` file",
        "inside it:",
        "",
        *_fenced(elkjs["text"]),
        "",
        "## Visual Studio Code extensions",
    ]
    for ident, _pinned_version, target, notice in extensions:
        build = "the build for every platform" if target == UNIVERSAL_TARGET else f"its {target} build"
        lines += [
            "",
            f"### `{ident}` {notice['version']}",
            "",
            f"The `extensions/{extension_payload_filename(ident)}` file, as the Visual Studio Marketplace",
            f"publishes it: {build}.",
            "",
            f"Declared licence: {notice['licence']}",
            "",
        ]
        if notice["text"]:
            lines += [f"Its licence text, from `{notice['source']}` inside it:", "", *_fenced(notice["text"])]
        else:
            lines.append("The package carries no licence file.")
    return "\n".join(lines) + "\n"


def runtime_record(
    *,
    node_version: str,
    elkjs_version: str,
    python_version: str,
    python_build_tag: str,
    python_platform_tag: str,
    token: str,
    extensions: Sequence[dict[str, str]] = (),
    now: datetime | None = None,
) -> dict[str, object]:
    """What :data:`RUNTIME_RECORD_FILENAME` records -- :func:`wheelhouse_platform`'s pattern.

    ``python_platform_tag`` is what the carried interpreter reports (it differs from the host's
    ``platform_tag`` on macOS), read by :func:`runtime_interpreter_stamp`. ``extensions`` is each
    companion carried, as ``{id, version, target}``, compared with the pins. ``elkjs_version`` is the
    payload staged for the wheel, recorded because the notices beside the record describe it.
    ``python_triple``, ``platform_tag`` and ``created_at`` are a build log that no check reads.
    """
    return {
        "node_version": node_version,
        "elkjs_version": elkjs_version,
        "python_version": python_version,
        "python_build_tag": python_build_tag,
        "python_triple": runtime_platform(token)["python_triple"],
        "python_platform_tag": python_platform_tag,
        "extensions": [dict(entry) for entry in extensions],
        "platform_tag": sysconfig.get_platform(),
        "platform_token": token,
        "created_at": utc_timestamp(now or datetime.now(UTC)),
    }


def _swap_in_staged_directory(staging: Path, live: Path, what: str) -> None:
    """Move *staging* into *live*'s place, putting the previous *live* back on any failure.

    The previous directory is renamed aside to ``<live>.previous`` (never deleted in place, which can
    leave it half-deleted; on Windows a rename onto an existing path fails), the staged one takes its
    name, and only then is the old one dropped. Each failure is one sentence naming *live*, and
    *staging* is discarded: a failed move aside leaves *live* unchanged; a failed move into place puts
    the previous tree back, or names the ``.previous`` path it is left at. Both renames are retried
    by :func:`_retry_guide_tree_operation`.
    """
    superseded = live.with_name(f"{live.name}.previous")
    shutil.rmtree(superseded, ignore_errors=True)
    moved_aside = live.exists()
    if moved_aside:
        try:
            _retry_guide_tree_operation(lambda: live.replace(superseded))
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise BuildArtifactsError(
                f"could not move the previous {what} aside from {live}: {exc} -- "
                "it is unchanged and the staged one was discarded"
            ) from exc
    try:
        _retry_guide_tree_operation(lambda: staging.replace(live))
    except OSError as exc:
        restored = True
        if moved_aside:
            try:
                _retry_guide_tree_operation(lambda: superseded.replace(live))
            except OSError:
                restored = False
        shutil.rmtree(staging, ignore_errors=True)
        where = (
            f"the previous one is unchanged at {live}"
            if restored
            else f"the previous one could not be put back and is at {superseded}"
        )
        raise BuildArtifactsError(f"could not move the staged {what} into {live}: {exc} -- {where}") from exc
    shutil.rmtree(superseded, ignore_errors=True)


def build_runtime_payload(
    repo_root: Path = REPO_ROOT,
    *,
    token: str | None = None,
    fetch: Fetcher = _download,
    probe: Callable[[Path], dict[str, str]] = probe_interpreter,
    now: datetime | None = None,
) -> Path:
    """Populate ``dist/runtime/`` with the pinned Node.js, CPython and extension payloads; returns it.

    The only place in this module that fetches a payload, which is why it is a network stage of its
    own (``runtime``) and :func:`build_workstation_archive` downloads nothing. Every version and digest
    comes from :data:`RUNTIME_PINS_FILE`, checked whole before the first byte is fetched; nothing
    committed is written, and no process is started but the carried interpreter's probe.

    * Node -- nodejs.org's archive file, stored byte for byte as :func:`node_payload_filename`, refused
      unless its SHA-256 is the digest transcribed from the release's ``SHASUMS256.txt``.
    * CPython -- python-build-standalone's ``install_only`` tarball, stored byte for byte and refused
      unless its SHA-256 is the digest from the release's ``SHA256SUMS``; also unpacked into
      :data:`RUNTIME_PYTHON_DIRNAME` and asked what it is: another version is refused, and the platform
      tag it reports is recorded for the wheelhouse check.
    * The companion extensions -- each ``.vsix`` the pins name for *token*, from the Marketplace,
      gunzipped and refused unless its SHA-256 is the pinned one. That digest is this project's own
      record of what ``update-pins`` fetched: the Marketplace publishes none, so there is no
      independent oracle behind it.
    * elkjs -- :func:`install_elkjs_payload`, first, staged as ``swcraft-tools`` package data outside
      ``dist/runtime/``; only its notice and version land here.
    * :data:`THIRD_PARTY_NOTICES_FILENAME` and :data:`RUNTIME_RECORD_FILENAME`.

    Everything is assembled in ``dist/runtime.new/`` and swapped in whole
    (:func:`_swap_in_staged_directory`), so a dropped connection or a refused digest leaves the
    previous payload as it was.

    What this does not reach: Node.js and the extensions are run only where their platform runs them.
    """
    dist_root = repo_root / DIST_DIRNAME
    token = workstation_platform_token() if token is None else token
    runtime_platform(token)
    pins = read_runtime_pins(repo_root)
    node_version, node_sha256 = node_pin(pins, token)
    python_version, python_build_tag, python_sha256 = python_pin(pins, token)
    runtime = dist_root / RUNTIME_DIRNAME
    # Staged first: the notices below read the payload, and a refusal comes before any staging.
    elkjs_notice = read_elkjs_notice(install_elkjs_payload(repo_root, fetch=fetch))

    staging = dist_root / f"{RUNTIME_DIRNAME}.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        node_payload = _fetch_pinned(
            fetch,
            node_download_url(node_version, token),
            staging / node_payload_filename(token),
            pinned=node_sha256,
            described=node_vendor_filename(node_version, token),
            runtime=runtime,
        )

        python_payload = _fetch_pinned(
            fetch,
            python_download_url(python_version, python_build_tag, token),
            staging / PYTHON_PAYLOAD_FILENAME,
            pinned=python_sha256,
            described=python_vendor_filename(python_version, python_build_tag, token),
            runtime=runtime,
        )
        unpacked = unpack_python_payload(python_payload, staging / RUNTIME_PYTHON_DIRNAME)
        interpreter = python_executable_in(unpacked, token)
        reported = probe(interpreter)
        if reported["python_version"] != python_version:
            raise BuildArtifactsError(
                f"the interpreter in {python_vendor_filename(python_version, python_build_tag, token)} reports "
                f"Python {reported['python_version']}, but {RUNTIME_PINS_FILE.as_posix()} pins {python_version} "
                f"-- nothing was staged and {runtime} is unchanged."
            )

        extensions = []
        for pin in extension_pins(pins, token):
            package = _fetch_pinned(
                fetch,
                extension_download_url(pin.id, pin.version, pin.target),
                staging / extension_payload_filename(pin.id),
                pinned=pin.sha256,
                described=f"{pin.id} {pin.version} ({pin.target})",
                runtime=runtime,
                decode=decode_marketplace_package,
            )
            extensions.append((pin.id, pin.version, pin.target, read_vsix_notice(package)))

        notices = render_third_party_notices(
            node_version=node_version,
            node_licence=read_node_licence(node_payload),
            python_version=python_version,
            python_build_tag=python_build_tag,
            python_licence=read_python_licence(unpacked, python_version, token),
            extensions=extensions,
            elkjs=elkjs_notice,
        )
        (staging / THIRD_PARTY_NOTICES_FILENAME).write_text(notices, encoding="utf-8", newline="\n")

        record = runtime_record(
            node_version=node_version,
            elkjs_version=elkjs_notice["version"],
            python_version=python_version,
            python_build_tag=python_build_tag,
            python_platform_tag=reported["platform_tag"],
            token=token,
            extensions=[
                {"id": ident, "version": version, "target": target} for ident, version, target, _ in extensions
            ],
            now=now,
        )
        (staging / RUNTIME_RECORD_FILENAME).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _swap_in_staged_directory(staging, runtime, "runtime payload")
    return runtime


def read_runtime_record(runtime: Path) -> tuple[Path, dict[str, object]]:
    """``(path, contents)`` of ``dist/runtime/RUNTIME.json``, or a refusal naming the stage to run.

    Each of those failures means the ``runtime`` stage never finished, and the message says so.
    """
    if not runtime.is_dir():
        raise BuildArtifactsError(
            f"{runtime} is missing -- the workstation archive carries the Node.js, Python and editor "
            "extension payloads, so run `python scripts/dev.py runtime` first."
        )
    record_path = runtime / RUNTIME_RECORD_FILENAME
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildArtifactsError(
            f"{record_path} is missing, so {runtime} was never populated. {_RUNTIME_RERUN}"
        ) from None
    except (OSError, json.JSONDecodeError) as error:
        raise BuildArtifactsError(f"{record_path} is not readable JSON: {error}. {_RUNTIME_RERUN}") from error
    if not isinstance(record, dict):
        raise BuildArtifactsError(f"{record_path} must hold a JSON object. {_RUNTIME_RERUN}")
    return record_path, record


def runtime_interpreter_stamp(runtime: Path) -> dict[str, str]:
    """The ``PLATFORM.json`` keys of the Python the archive carries, as ``RUNTIME.json`` records them.

    ``python_version`` is the pin's ``major.minor``; ``platform_tag`` is what the carried interpreter
    reported. A record naming no interpreter is refused, so the wheelhouse is never checked against nothing.
    """
    record_path, record = read_runtime_record(runtime)
    version = record.get("python_version")
    tag = record.get("python_platform_tag")
    if not isinstance(version, str) or not _EXACT_VERSION.fullmatch(version) or not isinstance(tag, str) or not tag:
        raise BuildArtifactsError(
            f"{record_path} records no Python the archive carries (python_version {version!r}, "
            f"python_platform_tag {tag!r}), so the wheelhouse cannot be checked against it. {_RUNTIME_RERUN}"
        )
    return {"python_version": _major_minor(version), "platform_tag": tag}


def runtime_archive_members(repo_root: Path, dist_root: Path, token: str) -> list[tuple[str, Path]]:
    """``(archive path, source file)`` of the payloads ``runtime`` staged for *token*, or a refusal by name.

    Named, not listed: ``RUNTIME.json``, the Node file and :data:`PYTHON_PAYLOAD_FILENAME` under
    ``runtime/``, each pinned companion under ``extensions/``, and :data:`THIRD_PARTY_NOTICES_FILENAME`
    at the root -- never every file of ``dist/runtime/``, so a stray file cannot travel. Refused:

    * ``dist/runtime/`` absent, or ``RUNTIME.json`` missing or unreadable (:func:`read_runtime_record`);
    * ``RUNTIME.json``'s ``platform_token`` is not *token*;
    * its ``node_version``, ``python_version``, ``python_build_tag`` or ``elkjs_version`` (a record
      without one is stale) is not the committed pin, or its ``extensions`` are not exactly the pinned
      ids, versions and targets for *token*;
    * a named file is missing, or a Node, CPython or extension file's SHA-256 is not the committed one.

    What it does not reach: the notices are not opened; the elkjs payload itself is not compared here
    (it travels in the wheel, checked by :func:`require_elkjs_payload`); and ``platform_tag`` is not
    compared, because the payloads bind to the token, not to the build host's interpreter.
    """
    runtime = dist_root / RUNTIME_DIRNAME
    record_path, record = read_runtime_record(runtime)

    recorded_token = record.get("platform_token")
    if recorded_token != token:
        raise BuildArtifactsError(
            f"{runtime} was populated for {recorded_token!r}, and this archive is for {token!r}: its Node.js, "
            f"Python payloads would not run there. {_RUNTIME_RERUN}"
        )
    pins = read_runtime_pins(repo_root)
    node_version, node_sha256 = node_pin(pins, token)
    python_version, python_build_tag, python_sha256 = python_pin(pins, token)
    pinned = {
        "node_version": node_version,
        "python_version": python_version,
        "python_build_tag": python_build_tag,
        "elkjs_version": elkjs_pin(pins)[0],
    }
    stale = [
        f"{key} is {record.get(key)!r} and the committed pin is {value!r}"
        for key, value in pinned.items()
        if record.get(key) != value
    ]
    if stale:
        raise BuildArtifactsError(f"{runtime} is stale ({'; '.join(stale)}). {_RUNTIME_RERUN}")
    companions = extension_pins(pins, token)
    expected = [{"id": ident, "version": version, "target": target} for ident, version, target, _ in companions]
    if record.get("extensions") != expected:
        named = ", ".join(f"{entry['id']} {entry['version']} ({entry['target']})" for entry in expected)
        raise BuildArtifactsError(
            f"{runtime} is stale: it records the companion extensions {record.get('extensions')!r}, and the "
            f"committed pins name {named} for {token}. {_RUNTIME_RERUN}"
        )

    node_payload = runtime / node_payload_filename(token)
    python_payload = runtime / PYTHON_PAYLOAD_FILENAME
    packages = [(runtime / extension_payload_filename(ident), sha256) for ident, _, _, sha256 in companions]
    notices = runtime / THIRD_PARTY_NOTICES_FILENAME
    missing = [
        path.name
        for path in (node_payload, python_payload, *(package for package, _ in packages), notices)
        if not path.is_file()
    ]
    if missing:
        raise BuildArtifactsError(f"{runtime} holds no {' and no '.join(missing)}. {_RUNTIME_RERUN}")
    for payload, pinned_sha256 in ((node_payload, node_sha256), (python_payload, python_sha256), *packages):
        received = sha256_file(payload)
        if received != pinned_sha256:
            raise BuildArtifactsError(
                f"{payload} has SHA-256 {received}, but {RUNTIME_PINS_FILE.as_posix()} pins {pinned_sha256} for "
                f"{token}: the file changed after `runtime` verified it. {_RUNTIME_RERUN}"
            )
    return [
        *((f"{WORKSTATION_RUNTIME_DIRNAME}/{path.name}", path) for path in (record_path, node_payload, python_payload)),
        *((f"{WORKSTATION_EXTENSIONS_DIRNAME}/{package.name}", package) for package, _ in packages),
        (THIRD_PARTY_NOTICES_FILENAME, notices),
    ]


# ---------------------------------------------------------------------------
# The elkjs payload: `dev.py install` and `dev.py runtime` stage it
# ---------------------------------------------------------------------------

#: The npm package the diagram generator lays its graphs out with, and its pins key.
ELKJS_PACKAGE = "elkjs"

#: The npm registry: ``<this>/elkjs/<version>`` answers the version's metadata, and
#: ``<this>/elkjs/-/elkjs-<version>.tgz`` serves its tarball.
NPM_REGISTRY_URL = "https://registry.npmjs.org"

#: The one leading directory of every npm tarball.
NPM_TARBALL_ROOT = "package"

#: The files of the pinned tarball the layout needs: the entry point, the two files it requires, the
#: licence text and the manifest declaring it. ``main.js``'s ``require('web-worker')`` is guarded and
#: falls back to the bundled worker, so no dependency is needed. Sorted, as a tree walk lists them.
ELKJS_PAYLOAD_FILES = ("LICENSE.md", "lib/elk-api.js", "lib/elk-worker.min.js", "lib/main.js", "package.json")

#: Where the payload lives: package data of ``swcraft-tools``, so the generator finds it beside its
#: own module, editable installs included. A gitignored build product, rewritten whole.
PACKAGE_ELKJS_DIRNAME = ELKJS_PACKAGE
PACKAGE_ELKJS_DIR = PACKAGES_DIR / "tools" / "src" / "swcraft_tools" / "writer" / "diagrams" / PACKAGE_ELKJS_DIRNAME

#: The rewrite's two sidecars (the staged tree and the one renamed aside by
#: :func:`_swap_in_staged_directory`), excluded from the built archives and the source fingerprint.
PACKAGE_ELKJS_STAGING_DIRNAME = f"{PACKAGE_ELKJS_DIRNAME}.incoming"
PACKAGE_ELKJS_SUPERSEDED_DIRNAME = f"{PACKAGE_ELKJS_DIRNAME}.previous"

#: The licence SWCraft distributes elkjs under: of the declared ``EPL-2.0 OR GPL-3.0-or-later`` the
#: package ships the EPL-2.0 text. A package that stops offering it is refused, not relabelled.
ELKJS_DISTRIBUTED_LICENCE = "EPL-2.0"

#: The one sentence every refusal about the staged payload ends with.
_ELKJS_RERUN = "Stage it with `python scripts/dev.py install` (or `runtime`)."


def elkjs_tarball_filename(version: str) -> str:
    """The registry's own file name for *version*: ``elkjs-<version>.tgz``."""
    return f"{ELKJS_PACKAGE}-{version}.tgz"


def elkjs_tarball_url(version: str) -> str:
    """The URL the npm registry serves :func:`elkjs_tarball_filename` at."""
    return f"{NPM_REGISTRY_URL}/{ELKJS_PACKAGE}/-/{elkjs_tarball_filename(version)}"


def elkjs_metadata_url(version: str) -> str:
    """The URL of the registry's metadata for *version*, which names the tarball and its ``integrity``."""
    return f"{NPM_REGISTRY_URL}/{ELKJS_PACKAGE}/{version}"


def elkjs_pin(pins: dict[str, object]) -> tuple[str, str]:
    """``(version, sha256)`` of the elkjs tarball, from :func:`read_runtime_pins`' result -- one for every platform."""
    elkjs = pins[ELKJS_PACKAGE]
    return elkjs["version"], elkjs["sha256"]


def unpack_elkjs_payload(tarball: Path, destination: Path) -> Path:
    """Write exactly :data:`ELKJS_PAYLOAD_FILES` out of the npm *tarball* into *destination*; returns it.

    Each file is read by name and written as bytes, so no other name, link or mode of the tarball
    reaches the tree. A named file that is absent or not a regular file is refused by name.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, "r:gz") as bundle:
            members = {member.name: member for member in bundle.getmembers()}
            for relative in ELKJS_PAYLOAD_FILES:
                member = members.get(f"{NPM_TARBALL_ROOT}/{relative}")
                if member is None or not member.isfile():
                    raise BuildArtifactsError(
                        f"{tarball.name} holds no regular file {NPM_TARBALL_ROOT}/{relative}, "
                        "which the diagram layout needs."
                    )
                target = destination.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.extractfile(member).read())
    except (OSError, tarfile.TarError) as error:
        raise BuildArtifactsError(f"{tarball.name} could not be unpacked into {destination}: {error}") from error
    return destination


def _staged_elkjs_version(tree: Path) -> object:
    """The ``version`` the staged tree's ``package.json`` declares, or ``None`` when it cannot be read."""
    try:
        manifest = json.loads((tree / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return manifest.get("version") if isinstance(manifest, dict) else None


def install_elkjs_payload(repo_root: Path = REPO_ROOT, *, fetch: Fetcher = _download) -> Path:
    """Fetch the pinned elkjs tarball and stage its five files as package data; returns :data:`PACKAGE_ELKJS_DIR`.

    Uses the network; called only by ``dev.py install`` and ``dev.py runtime``, so ``artifacts`` stays
    offline (:func:`require_elkjs_payload`). The tarball is refused unless its SHA-256 is the
    committed one, the staged ``package.json`` must declare the pinned version, and the tree is
    swapped in whole, so a refusal leaves the previous payload. It fetches on every call: a tree that
    looks current is not proven to be the pinned bytes.
    """
    pins = read_runtime_pins(repo_root)
    version, sha256 = elkjs_pin(pins)
    live = repo_root / PACKAGE_ELKJS_DIR
    staging = live.with_name(PACKAGE_ELKJS_STAGING_DIRNAME)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            tarball = _fetch_pinned(
                fetch,
                elkjs_tarball_url(version),
                Path(scratch) / elkjs_tarball_filename(version),
                pinned=sha256,
                described=elkjs_tarball_filename(version),
                runtime=live,
            )
            unpack_elkjs_payload(tarball, staging)
        declared = _staged_elkjs_version(staging)
        if declared != version:
            raise BuildArtifactsError(
                f"{elkjs_tarball_filename(version)} declares version {declared!r}, but {RUNTIME_PINS_FILE.as_posix()} "
                f"pins {version} -- nothing was staged and {live} is unchanged."
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _swap_in_staged_directory(staging, live, "elkjs payload")
    return live


def require_elkjs_payload(repo_root: Path = REPO_ROOT) -> Path:
    """The staged elkjs payload, proven to be exactly the five files at the pinned version, or a refusal by name.

    Asked by ``artifacts`` before it builds a wheel, which carries whatever sits there. What this does
    not reach: the bytes of the ``lib/`` files (the digest pins the tarball, which is not kept), so a
    hand-edited file at the right version passes; :func:`install_elkjs_payload` repairs that.
    """
    version, _ = elkjs_pin(read_runtime_pins(repo_root))
    tree = repo_root / PACKAGE_ELKJS_DIR
    if not tree.is_dir():
        raise BuildArtifactsError(
            f"{tree} is missing, so the swcraft-tools wheel would carry no layout engine. {_ELKJS_RERUN}"
        )
    present = sorted(path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file())
    if present != list(ELKJS_PAYLOAD_FILES):
        raise BuildArtifactsError(
            f"{tree} holds {', '.join(present) or 'no file'}, not exactly {', '.join(ELKJS_PAYLOAD_FILES)}. "
            f"{_ELKJS_RERUN}"
        )
    declared = _staged_elkjs_version(tree)
    if declared != version:
        raise BuildArtifactsError(
            f"{tree} holds elkjs {declared!r}, but {RUNTIME_PINS_FILE.as_posix()} pins {version}. {_ELKJS_RERUN}"
        )
    return tree


def read_elkjs_notice(tree: Path) -> dict[str, str]:
    """What the staged payload declares about itself: ``version``, the declared ``licence`` and the licence ``text``.

    Read out of the payload's own ``package.json`` and ``LICENSE.md``. A declaration that does not offer
    :data:`ELKJS_DISTRIBUTED_LICENCE` is refused, because the notice states that choice.
    """
    try:
        manifest = json.loads((tree / "package.json").read_text(encoding="utf-8"))
        text = (tree / "LICENSE.md").read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildArtifactsError(
            f"the elkjs payload at {tree} is not readable ({error}), so its notice cannot be written. {_ELKJS_RERUN}"
        ) from error
    manifest = manifest if isinstance(manifest, dict) else {}
    declared = manifest.get("license")
    alternatives = [part.strip() for part in declared.strip("()").split(" OR ")] if isinstance(declared, str) else []
    if ELKJS_DISTRIBUTED_LICENCE not in alternatives:
        raise BuildArtifactsError(
            f"the elkjs payload declares the licence {declared!r}, which does not offer {ELKJS_DISTRIBUTED_LICENCE}, "
            "the licence SWCraft distributes it under -- decide the notice before shipping it."
        )
    return {"version": str(manifest.get("version") or ""), "licence": declared, "text": text}


# ---------------------------------------------------------------------------
# Minting the pins: `dev.py update-pins`
# ---------------------------------------------------------------------------

#: The pins file's ``description``; :func:`update_runtime_pins` writes the whole file, so a hand edit is undone.
RUNTIME_PINS_DESCRIPTION = (
    "Every build-time pin and digest of the workstation archive's runtime payloads, and of the elkjs payload "
    "the swcraft-tools wheel carries. scripts/build_artifacts.py reads "
    "this file and carries no digest or version of its own. "
    "`python scripts/dev.py update-pins` writes it: the Node.js, CPython and elkjs versions are the ones already "
    "here (edit them to bump). Each Node.js and CPython sha256 is transcribed from the publisher's own sums file "
    "(nodejs.org's SHASUMS256.txt, the python-build-standalone release's SHA256SUMS); the elkjs sha256 is "
    "computed from the npm registry's tarball once that tarball matches the sha512 integrity the registry "
    "publishes for the version. The companion "
    "extensions are the vscode_extension/package.json extensionPack, in its order, each at the newest "
    "release version the Visual Studio Marketplace publishes, with the build each platform takes; their "
    "sha256 is computed from the downloaded package, because the Marketplace publishes none."
)

#: The sums file each publisher puts beside a release.
NODE_SUMS_FILENAME = "SHASUMS256.txt"
PYTHON_SUMS_FILENAME = "SHA256SUMS"

#: The manifest whose ``extensionPack`` names the companions, and their order.
EXTENSION_PACK_MANIFEST = Path(VSCODE_PROJECT) / "package.json"

#: The version property that marks a Marketplace pre-release.
_PRE_RELEASE_PROPERTY = "Microsoft.VisualStudio.Code.PreRelease"

#: ``extensionquery``'s flags: ``IncludeVersions`` (``0x1``) and ``IncludeVersionProperties`` (``0x10``);
#: criterion ``7`` is an extension's ``publisher.name``.
_EXTENSION_QUERY_FLAGS = 0x1 | 0x10
_EXTENSION_NAME_CRITERION = 7

#: ``query(ids)`` -- the Marketplace's ``extensionquery`` answer for *ids*.
MarketplaceQuery = Callable[[Sequence[str]], dict[str, object]]


def query_marketplace(ids: Sequence[str]) -> dict[str, object]:
    """``POST`` one ``extensionquery`` for *ids* (no account, no token) and answer its JSON."""
    body = {
        "filters": [
            {
                "criteria": [{"filterType": _EXTENSION_NAME_CRITERION, "value": ident} for ident in ids],
                "pageNumber": 1,
                "pageSize": max(len(ids), 1),
                "sortBy": 0,
                "sortOrder": 0,
            }
        ],
        "flags": _EXTENSION_QUERY_FLAGS,
    }
    request = urllib.request.Request(
        f"{MARKETPLACE_GALLERY_URL}/extensionquery",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json;api-version=3.0-preview.1",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )

    # A cut-off answer (`IncompleteRead`) is retried as transport; a 200 whose body is not JSON is
    # content and refuses on the first attempt.
    def attempt() -> bytes:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read()

    endpoint = f"{MARKETPLACE_GALLERY_URL}/extensionquery"
    received = _retry_transport(attempt, f"could not query {endpoint}")
    try:
        answer = json.loads(received.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildArtifactsError(f"could not query {endpoint}: {error}") from error
    if not isinstance(answer, dict):
        raise BuildArtifactsError(f"{endpoint} answered something that is not an object.")
    return answer


def read_extension_pack(repo_root: Path = REPO_ROOT) -> list[str]:
    """The ``extensionPack`` of ``vscode_extension/package.json``, in its order -- the companions, and nothing else."""
    manifest = _read_json_object(repo_root / EXTENSION_PACK_MANIFEST, repo_root)
    pack = manifest.get("extensionPack")
    if (
        not isinstance(pack, list)
        or not pack
        or not all(isinstance(i, str) and _EXTENSION_ID.fullmatch(i) for i in pack)
    ):
        raise BuildArtifactsError(
            f"{EXTENSION_PACK_MANIFEST.as_posix()} declares no extensionPack of publisher.name identifiers, "
            "so there is no companion extension to pin."
        )
    return list(pack)


def published_digest(sums: str, filename: str, source: str) -> str:
    """The SHA-256 *sums* lists for *filename* -- one ``<digest>  <name>`` line -- or a refusal naming *source*."""
    for line in sums.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == filename and _SHA256_DIGEST.fullmatch(fields[0].lower()):
            return fields[0].lower()
    raise BuildArtifactsError(f"{source} lists no {filename}, so it cannot be pinned.")


def newest_release(extension: dict[str, object], ident: str) -> tuple[str, frozenset[str]]:
    """The newest version the Marketplace publishes for *extension* as a **release**, and the targets published at it.

    Pre-releases are skipped: VS Code installs one only for a user who asked. Newest is decided by the
    version's numbers, never by the answer's order.
    """
    releases: dict[str, set[str]] = {}
    for entry in extension.get("versions") or []:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if not isinstance(version, str) or not _EXACT_VERSION.fullmatch(version):
            continue
        properties = {
            item.get("key"): item.get("value") for item in entry.get("properties") or [] if isinstance(item, dict)
        }
        if str(properties.get(_PRE_RELEASE_PROPERTY, "")).lower() == "true":
            continue
        releases.setdefault(version, set()).add(entry.get("targetPlatform") or UNIVERSAL_TARGET)
    if not releases:
        raise BuildArtifactsError(f"the Visual Studio Marketplace publishes no release version of {ident}.")
    newest = max(releases, key=lambda version: tuple(int(part) for part in version.split(".")))
    return newest, frozenset(releases[newest])


def _published_extensions(answer: dict[str, object]) -> dict[str, dict[str, object]]:
    """``extensionquery``'s answer, keyed by lowercase ``publisher.name``."""
    try:
        extensions = answer["results"][0]["extensions"]
        return {
            f"{extension['publisher']['publisherName']}.{extension['extensionName']}".lower(): extension
            for extension in extensions
        }
    except (KeyError, IndexError, TypeError) as error:
        raise BuildArtifactsError(
            f"{MARKETPLACE_GALLERY_URL}/extensionquery answered in a shape this cannot read ({error!r})."
        ) from error


def elkjs_registry_digest(version: str, scratch: Path, fetch: Fetcher) -> str:
    """The SHA-256 of the elkjs *version* tarball, computed only once the registry's own sha512 vouches for it.

    The metadata's ``dist.tarball`` must be :func:`elkjs_tarball_url` (what ``install`` and ``runtime``
    fetch) and its ``dist.integrity`` a ``sha512-`` value the downloaded tarball must match.
    """
    metadata_url = elkjs_metadata_url(version)
    metadata_file = scratch / f"{ELKJS_PACKAGE}-{version}.json"
    fetch(metadata_url, metadata_file)
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        tarball_url = metadata["dist"]["tarball"]
        integrity = metadata["dist"]["integrity"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise BuildArtifactsError(
            f"{metadata_url} answered no readable dist.tarball and dist.integrity ({error!r})."
        ) from error
    expected_url = elkjs_tarball_url(version)
    if tarball_url != expected_url:
        raise BuildArtifactsError(
            f"{metadata_url} names the tarball {tarball_url!r}, not {expected_url}, which install and runtime fetch."
        )
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise BuildArtifactsError(f"{metadata_url} publishes no sha512 integrity for the tarball: {integrity!r}.")
    tarball = scratch / elkjs_tarball_filename(version)
    fetch(expected_url, tarball)
    received = "sha512-" + base64.b64encode(hashlib.sha512(tarball.read_bytes()).digest()).decode("ascii")
    if received != integrity:
        raise BuildArtifactsError(
            f"{elkjs_tarball_filename(version)} from {expected_url} has integrity {received}, but {metadata_url} "
            f"publishes {integrity} -- it cannot be pinned."
        )
    return sha256_file(tarball)


def update_runtime_pins(
    repo_root: Path = REPO_ROOT, *, query: MarketplaceQuery = query_marketplace, fetch: Fetcher = _download
) -> Path:
    """Rewrite :data:`RUNTIME_PINS_FILE` from the publishers; returns it.

    The one thing that mints a companion version, and not a ``verify`` stage: a gate that rewrote a
    committed file would dirty every checkout, so this is a maintainer's act whose output is a diff.

    * Node.js and CPython keep the committed versions (a bump edits them by hand); each platform's
      SHA-256 is transcribed from the publisher's own sums file.
    * elkjs keeps the committed version; npm publishes no SHA-256, so it is computed from the tarball,
      but only after the registry's sha512 ``integrity`` vouches for it (:func:`elkjs_registry_digest`).
    * The companion extensions are ``vscode_extension/package.json``'s ``extensionPack``, in order,
      each at its newest release (:func:`newest_release`). Each platform takes the build published for
      it when there is one, else the universal build: ``redhat.vscode-xml``'s platform build carries
      the native LemMinX binary, so it needs no Java and no download at first use. The SHA-256 is this
      project's own, since the Marketplace publishes none.

    The new file is checked with :func:`check_runtime_pins` before it is written; a refusal leaves the
    committed file untouched.
    """
    path = repo_root / RUNTIME_PINS_FILE
    name = RUNTIME_PINS_FILE.as_posix()
    committed = _read_json_object(path, repo_root)
    node = committed.get("node") if isinstance(committed.get("node"), dict) else {}
    python = committed.get("python") if isinstance(committed.get("python"), dict) else {}
    elkjs = committed.get(ELKJS_PACKAGE) if isinstance(committed.get(ELKJS_PACKAGE), dict) else {}
    node_version, python_version, build_tag = node.get("version"), python.get("version"), python.get("build_tag")
    elkjs_version = elkjs.get("version")
    if not (
        _is_exact(_EXACT_VERSION, node_version)
        and _is_exact(_EXACT_VERSION, python_version)
        and _is_exact(_BUILD_TAG, build_tag)
        and _is_exact(_EXACT_VERSION, elkjs_version)
    ):
        raise BuildArtifactsError(
            f"{name} must name node.version, python.version, python.build_tag and elkjs.version before update-pins "
            f"can transcribe their digests (it names {node_version!r}, {python_version!r}, {build_tag!r} and "
            f"{elkjs_version!r}): edit them by hand."
        )
    # Refused before anything is fetched, as the stages' reader refuses it.
    _refuse_the_retired_key(committed, name)
    ids = read_extension_pack(repo_root)
    tokens = sorted(RUNTIME_PLATFORMS)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        scratch = Path(directory)
        node_sums_url = f"{NODE_DIST_URL}/v{node_version}/{NODE_SUMS_FILENAME}"
        fetch(node_sums_url, scratch / NODE_SUMS_FILENAME)
        node_sums = (scratch / NODE_SUMS_FILENAME).read_text(encoding="utf-8")
        python_sums_url = f"{PYTHON_RELEASE_URL}/{build_tag}/{PYTHON_SUMS_FILENAME}"
        fetch(python_sums_url, scratch / PYTHON_SUMS_FILENAME)
        python_sums = (scratch / PYTHON_SUMS_FILENAME).read_text(encoding="utf-8")
        elkjs_sha256 = elkjs_registry_digest(elkjs_version, scratch, fetch)

        published = _published_extensions(query(ids))
        extensions = []
        for ident in ids:
            extension = published.get(ident)
            if extension is None:
                raise BuildArtifactsError(f"the Visual Studio Marketplace answered nothing for {ident}.")
            version, targets = newest_release(extension, ident)
            digests: dict[str, str] = {}
            platforms = {}
            for token in tokens:
                wanted = RUNTIME_PLATFORMS[token]["vscode_target"]
                target = wanted if wanted in targets else UNIVERSAL_TARGET if UNIVERSAL_TARGET in targets else None
                if target is None:
                    raise BuildArtifactsError(
                        f"{ident} {version} is published for {', '.join(sorted(targets))}: neither a {wanted} build "
                        f"nor a universal one, so the {token} archive has no build to carry."
                    )
                if target not in digests:
                    package = scratch / f"{ident}-{target}.vsix"
                    fetch(extension_download_url(ident, version, target), package)
                    digests[target] = sha256_file(decode_marketplace_package(package))
                platforms[token] = {"target": target, "sha256": digests[target]}
            extensions.append({"id": ident, "version": version, "platforms": platforms})

        pins = check_runtime_pins(
            {
                "description": RUNTIME_PINS_DESCRIPTION,
                "node": {
                    "version": node_version,
                    "platforms": {
                        token: {
                            "sha256": published_digest(
                                node_sums, node_vendor_filename(node_version, token), node_sums_url
                            )
                        }
                        for token in tokens
                    },
                },
                "python": {
                    "version": python_version,
                    "build_tag": build_tag,
                    "platforms": {
                        token: {
                            "sha256": published_digest(
                                python_sums,
                                python_vendor_filename(python_version, build_tag, token),
                                python_sums_url,
                            )
                        }
                        for token in tokens
                    },
                },
                ELKJS_PACKAGE: {"version": elkjs_version, "sha256": elkjs_sha256},
                "extensions": extensions,
            }
        )
    path.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the versioned local SWCraft artifacts.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to build from (default: this script's repository)",
    )
    parser.add_argument(
        "--wheelhouse",
        action="store_true",
        help=(
            "populate dist/wheelhouse/ (the built wheels plus their whole third-party "
            "closure) instead of building artifacts, resolved on the Python `--runtime` "
            "unpacked into dist/runtime/python/; uses the network, and is what lets "
            "the clean-install acceptance install with --no-index"
        ),
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help=(
            "render the user guide into dist/docs/site/ and stop, building no wheel and "
            "no .vsix; this is what `python scripts/dev.py docs` runs, and it needs no npm"
        ),
    )
    parser.add_argument(
        "--workstation-archive",
        action="store_true",
        help=(
            "compose dist/workstation/swcraft-<version>-<platform>.zip from what `artifacts`, "
            "`runtime` and `wheelhouse` already produced (the installer, the wrappers, the .vsix, "
            "the whole wheelhouse and the runtime payloads); downloads nothing, and is not a "
            "manifest entry"
        ),
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help=(
            "populate dist/runtime/ with the pinned Node.js and CPython releases and the companion editor "
            "extensions the workstation archive carries, with their notices, and restage the elkjs payload the "
            "swcraft-tools wheel carries; uses the network, needs no npm, reads the committed pins and edits none"
        ),
    )
    parser.add_argument(
        "--elkjs",
        action="store_true",
        help=(
            "stage the pinned elkjs payload -- five files of the npm registry's tarball, refused unless its "
            "SHA-256 is the committed one -- into packages/tools/src/swcraft_tools/writer/diagrams/elkjs/, where the "
            "swcraft-tools wheel carries it; uses the network, and is what `dev.py install` runs"
        ),
    )
    parser.add_argument(
        "--update-pins",
        action="store_true",
        help=(
            "rewrite scripts/workstation/pins/runtime-pins.json: transcribe the committed Node.js and CPython "
            "releases' published digests and mint the companion extensions at their newest Marketplace release; "
            "uses the network, and is a maintainer's act whose output is a diff to review, never a verify stage"
        ),
    )
    args = parser.parse_args(argv)
    modes = [args.wheelhouse, args.docs_only, args.workstation_archive, args.runtime, args.update_pins, args.elkjs]
    if sum(1 for mode in modes if mode) > 1:
        parser.error(
            "--wheelhouse, --docs-only, --workstation-archive, --runtime, --update-pins and --elkjs each replace "
            "the artifact build; pass at most one"
        )

    try:
        if args.elkjs:
            payload = install_elkjs_payload(args.repo_root.resolve())
            print(f"Staged {payload}")
            return 0
        if args.update_pins:
            pins = update_runtime_pins(args.repo_root.resolve())
            print(f"Wrote {pins}")
            return 0
        if args.runtime:
            runtime = build_runtime_payload(args.repo_root.resolve())
            print(f"Populated {runtime}")
            return 0
        if args.wheelhouse:
            # Resolved on the Python the archive carries, never the one running this script.
            repo_root = args.repo_root.resolve()
            wheelhouse = build_wheelhouse(repo_root, python_executable=str(require_runtime_python(repo_root)))
            print(f"Populated {wheelhouse}")
            return 0
        if args.docs_only:
            site_dir = build_docs_site(args.repo_root.resolve())
            print(f"Built {site_dir}")
            return 0
        if args.workstation_archive:
            archive = build_workstation_archive(args.repo_root.resolve())
            print(f"Wrote {archive}")
            return 0
        manifest_path = build_all(args.repo_root.resolve())
    except BuildArtifactsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        command = " ".join(str(part) for part in error.cmd)
        print(f"Error: build step failed (exit {error.returncode}): {command}", file=sys.stderr)
        return error.returncode or 1
    except OSError as error:
        # A full disk or an unwritable dist/: reported as a sentence, not a traceback.
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
