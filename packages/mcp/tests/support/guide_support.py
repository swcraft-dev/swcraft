"""The guide sites the ``guide`` tool's tests install, and how a test installs one.

The tool reads the installed corpus alone (``guide_site.package_site_dir()``, then
``guide_site.bundle_site_dir()``, never ``SWCRAFT_GUIDE_SITE``), so a test installs a site by pointing
those two steps at a temporary directory. The suite's autouse ``_project_env`` points both at nothing
and forgets the loaded corpus, so a test that installs no site gets ``guide-missing``.

Two corpora, built in-process once per session: :func:`fixture_corpus` (the producer's small fixture
guide, with a *Release notes* section the producer leaves out) and :func:`real_corpus` (``docs/guide``
with the selected release notes written into a temporary copy by ``write_guide_release_notes``).

What this does not reach: ``version.md``, the one page the site build generates that the in-process
corpus lacks, and the built site's own file, which only ``dev.py docs`` or the artifact stage writes.
"""

from __future__ import annotations

import copy
import functools
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, build_artifacts_module
from swcraft_tools import guide_search, guide_site
from swcraft_tools.guide_corpus import build_corpus

#: The producer's small fixture guide, with its own ``mkdocs.yml`` one directory above it.
FIXTURE_GUIDE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "guide_corpus" / "basic" / "guide"


@functools.cache
def _fixture_corpus() -> dict[str, Any]:
    return build_corpus(FIXTURE_GUIDE)


@functools.cache
def _real_corpus() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="real-guide-") as scratch:
        root = Path(scratch)
        guide = (REPO_ROOT / "docs" / "guide").resolve()
        shutil.copytree(
            guide,
            root / "docs" / "guide",
            ignore=lambda directory, names: [
                name for name in names if Path(directory).resolve() == guide and name in ("release-notes", "version.md")
            ],
        )
        shutil.copytree(REPO_ROOT / "docs" / "release-notes", root / "docs" / "release-notes")
        build_artifacts_module().write_guide_release_notes(root)
        return build_corpus(root / "docs" / "guide", mkdocs_config=REPO_ROOT / "mkdocs.yml")


def fixture_corpus() -> dict[str, Any]:
    """The fixture guide's corpus, a copy a test may change."""
    return copy.deepcopy(_fixture_corpus())


def real_corpus() -> dict[str, Any]:
    """The real guide's corpus, built beside the selected release notes it leaves out; a copy a test may change."""
    return copy.deepcopy(_real_corpus())


def write_site(directory: Path, corpus: dict[str, Any] | str | None) -> Path:
    """A site in *directory*: an ``index.html`` and, unless *corpus* is ``None``, the corpus file.

    *corpus* is a mapping, written as JSON, or raw text, written as it is (a malformed file).
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<html></html>\n", encoding="utf-8", newline="\n")
    if corpus is not None:
        text = corpus if isinstance(corpus, str) else json.dumps(corpus, ensure_ascii=False)
        (directory / guide_search.CORPUS_FILENAME).write_text(text, encoding="utf-8", newline="\n")
    return directory


def install_site(monkeypatch: pytest.MonkeyPatch, package: Path, bundle: Path | None = None) -> None:
    """Point the reader's two steps at *package* and *bundle* (``None``: no bundle); forget the loaded corpus."""
    monkeypatch.setattr(guide_site, "package_site_dir", lambda: package)
    monkeypatch.setattr(guide_site, "bundle_site_dir", lambda: bundle)
    guide_search.reset_cache()


def install_no_site(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    """Point the reader's two steps at nothing: the package step (always a path) at a directory of *scratch*.

    *scratch* is the test's own temporary directory, so no other test can have made a site there.
    """
    nowhere = scratch / "no-guide-site-here"
    assert not nowhere.exists(), "the no-site path must hold nothing"
    install_site(monkeypatch, nowhere)


def install_corpus(monkeypatch: pytest.MonkeyPatch, where: Path, corpus: dict[str, Any] | str | None) -> Path:
    """Write a site holding *corpus* at *where* and install it as the package payload."""
    site = write_site(where, corpus)
    install_site(monkeypatch, site)
    return site
