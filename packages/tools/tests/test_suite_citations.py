"""A test class cited beside a suite path must be declared in that file.

Wherever a repository-relative ``…/test_*.py`` path is followed by a ``Test…`` class name with only
punctuation and whitespace between them, at most :data:`_MAX_GAP` characters (``path``'s ``TestX``,
``path::TestX``, or a JSDoc citation split over two lines), the file must declare that class. Every root of
:data:`_ROOTS` must exist and yield at least one file, so a renamed root fails instead of being read as empty.

What this does not reach: a class is paired with the nearest preceding suite path, so a paragraph naming two
suites can pair the wrong one (it then fails, never passes); it proves the class exists, not that it asserts
what the sentence claims; it reads only :data:`_ROOTS` (``vscode_extension/src``'s ``*.test.ts`` among them, not
``scripts/tests/``) and the unreleased release notes, so the Python test trees, ``examples/``, ``templates/``,
the plans and the released notes (history, which keeps the paths it was written with) are unchecked; a citation
of a path outside :data:`SUITE_PREFIXES` is skipped; a pre-move path (:data:`_PRE_MOVE_PREFIXES`) is reported
as not existing; a citation without the ``.py`` path is invisible.

Another module extends :data:`_ROOTS` and :data:`SUITE_PREFIXES` and calls :func:`_check_roots` and
:func:`_check` over its own trees, so these four names and their signatures are shared, not private to this file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import REPO_ROOT

#: `(path, suffixes)` -- the trees and files whose prose cites the suites. A file entry is read as itself; a
#: directory entry is walked.
_ROOTS = (
    ("docs/guide", (".md",)),
    ("vscode_extension/src", (".ts",)),
    ("packages/tools/src", (".py",)),
    ("packages/mcp/src", (".py",)),
    ("packages/agent/src", (".py",)),
    ("scripts", (".py",)),
)

#: Test code inside a root of :data:`_ROOTS`, relative to the repository; its planted citations are data.
_NOT_READ = "scripts/tests"

#: The most non-word characters between a suite path and its class; the gap absorbs a JSDoc ``*`` and a line break.
_MAX_GAP = 120

#: A suite path, then a class name after at most :data:`_MAX_GAP` non-word characters.
_CITATION = re.compile(rf"((?:[\w.-]+/)*test_[\w.]+\.py)[`'\"]*(?:'s|::)?[^\w]{{0,{_MAX_GAP}}}?\b(Test[A-Za-z0-9_]+)\b")

#: The marker an unreleased release note carries; a note without it has shipped.
_UNRELEASED_MARKER = "**Not yet released.**"

#: Where the three Python projects lived before they moved under ``packages/``.
_PRE_MOVE_PREFIXES: tuple[str, ...] = ("tools/", "mcp_server/", "agent/")

#: The trees a cited suite path is resolved under; a citation outside them is skipped. The pre-move prefixes
#: stay, so a stale citation is reported, never skipped.
SUITE_PREFIXES: tuple[str, ...] = (
    "packages/tools/",
    "packages/mcp/",
    "packages/agent/",
    *_PRE_MOVE_PREFIXES,
    "vscode_extension/",
    "scripts/",
)


def _unreleased_release_notes() -> Iterator[Path]:
    """Yield the release notes still marked unreleased: the only ones whose citations are resolved."""
    for path in sorted((REPO_ROOT / "docs" / "release-notes").glob("*.md"), key=lambda item: item.as_posix()):
        if _UNRELEASED_MARKER in path.read_text(encoding="utf-8"):
            yield path


def _files() -> Iterator[Path]:
    """Yield the unreleased release notes, then every file of :data:`_ROOTS` with its root's suffix."""
    not_read = REPO_ROOT / _NOT_READ
    yield from _unreleased_release_notes()
    for relative, suffixes in _ROOTS:
        root = REPO_ROOT / relative
        if root.is_file():
            yield root
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file() and path.suffix in suffixes and not path.is_relative_to(not_read):
                yield path


def _flattened(path: Path) -> str:
    """Return *path*'s text with comment prefixes removed and lines joined, so a two-line JSDoc citation is one."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        lines.append(re.sub(r"^\s*(?:\*|//|#)\s?", "", line))
    return " ".join(" ".join(lines).split())


def _classes_of(suite: str) -> set[str] | None:
    """Return the `class Test…` names declared in *suite*, or ``None`` if it does not exist."""
    path = REPO_ROOT / suite
    if not path.is_file():
        return None
    return set(re.findall(r"^class (Test[A-Za-z0-9_]+)", path.read_text(encoding="utf-8"), flags=re.MULTILINE))


def _check() -> int:
    """Check every citation in the files :func:`_files` yields.

    Returns:
        The number of citations whose suite exists.

    Raises:
        AssertionError: naming every citation of a missing suite or an undeclared class.
    """
    offences: list[str] = []
    checked = 0
    for path in _files():
        text = _flattened(path)
        for suite, name in _CITATION.findall(text):
            if not suite.startswith(SUITE_PREFIXES):
                # A bare `test_x.py` names no file, and a tree outside SUITE_PREFIXES has no suite here.
                continue
            declared = _classes_of(suite)
            where = path.relative_to(REPO_ROOT).as_posix()
            if declared is None:
                moved = (
                    ", a pre-move path (the projects are under packages/)"
                    if suite.startswith(_PRE_MOVE_PREFIXES)
                    else ""
                )
                offences.append(f"{where}: cites {suite}{moved}, which does not exist")
                continue
            checked += 1
            if name not in declared:
                offences.append(f"{where}: cites {suite}'s `{name}`, which is not declared there")
    assert not offences, (
        "a citation that names the wrong file is the defect this repository's evidence "
        "discipline exists to prevent:\n" + "\n".join(offences)
    )
    return checked


def _check_roots() -> None:
    """Check that every root of :data:`_ROOTS` exists and yields a file :func:`_files` reads.

    Raises:
        AssertionError: naming every root that is missing or yields no file.
    """
    read = list(_files())
    unread = []
    for relative, suffixes in _ROOTS:
        root = REPO_ROOT / relative
        if not (root.is_file() or root.is_dir()):
            unread.append(f"{relative}: does not exist")
        elif not any(path.is_relative_to(root) for path in read):
            unread.append(f"{relative}: holds no {'/'.join(suffixes)} file")
    assert not unread, (
        "a root this check reads is missing or empty, so its citations would go unchecked:\n" + "\n".join(unread)
    )


def test_every_cited_test_class_is_in_the_suite_it_is_cited_at() -> None:
    # No minimum count of citations: the product prose may cite no suite at all. The roots check keeps the
    # real sweep from reading nothing; the planted trees below prove the matcher.
    _check_roots()
    _check()


def _planted_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citation: str, page: str = "docs/guide/page.md"
) -> None:
    """Plant a tree with one suite declaring ``TestReal`` and *page* holding *citation*, *page*'s folder the root."""
    suite = tmp_path / "packages" / "tools" / "tests" / "test_planted.py"
    suite.parent.mkdir(parents=True)
    suite.write_text("class TestReal:\n    pass\n", encoding="utf-8", newline="\n")
    page_path = tmp_path / page
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(citation, encoding="utf-8", newline="\n")
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        f"{__name__}._ROOTS", ((page_path.parent.relative_to(tmp_path).as_posix(), (page_path.suffix,)),)
    )


@pytest.mark.parametrize(
    ("citation", "page"),
    [
        ("`packages/tools/tests/test_planted.py::TestReal`\n", "docs/guide/page.md"),
        ("`packages/tools/tests/test_planted.py`'s `TestReal`\n", "docs/guide/page.md"),
        (
            "/**\n * See packages/tools/tests/test_planted.py\n * `TestReal` for the rule.\n */\n",
            "vscode_extension/src/page.ts",
        ),
    ],
    ids=["double-colon", "possessive", "two-line-jsdoc"],
)
def test_a_valid_citation_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citation: str, page: str) -> None:
    _planted_tree(tmp_path, monkeypatch, citation, page)
    _check_roots()
    assert _check() == 1


def test_a_citation_of_a_missing_suite_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_tree(tmp_path, monkeypatch, "`packages/tools/tests/test_gone.py::TestReal`\n")
    with pytest.raises(AssertionError, match=r"cites packages/tools/tests/test_gone\.py, which does not exist"):
        _check()


def test_a_citation_outside_the_suite_trees_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_tree(tmp_path, monkeypatch, "`examples/tests/test_gone.py::TestReal`\n")
    assert _check() == 0


def test_the_scripts_test_tree_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_tree(tmp_path, monkeypatch, "# packages/tools/tests/test_planted.py::TestMissing\n", "scripts/tests/x.py")
    (tmp_path / "scripts" / "tool.py").write_text(
        "# packages/tools/tests/test_planted.py::TestReal\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(f"{__name__}._ROOTS", (("scripts", (".py",)),))
    assert _check() == 1


def test_a_missing_or_empty_root_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_tree(tmp_path, monkeypatch, "no citation\n")
    monkeypatch.setattr(f"{__name__}._ROOTS", (("docs/renamed", (".md",)), ("docs/guide", (".ts",))))
    with pytest.raises(AssertionError, match=r"docs/renamed: does not exist\n\s*docs/guide: holds no \.ts file"):
        _check_roots()


def test_a_citation_of_an_undeclared_class_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_tree(tmp_path, monkeypatch, "`packages/tools/tests/test_planted.py::TestMissing`\n")
    with pytest.raises(
        AssertionError, match=r"docs/guide/page\.md: cites packages/tools/tests/test_planted\.py's `TestMissing`"
    ):
        _check()


def _stale_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a tree citing a pre-move path in a released note and in a guide page, beside an unreleased note."""
    notes = tmp_path / "docs" / "release-notes"
    notes.mkdir(parents=True)
    stale = "`tools/tests/test_cli.py::TestOld`\n"
    (notes / "0.1.0.md").write_text(stale, encoding="utf-8", newline="\n")
    (notes / "0.2.0.md").write_text(f"{_UNRELEASED_MARKER}\n", encoding="utf-8", newline="\n")
    (tmp_path / "docs" / "guide").mkdir()
    (tmp_path / "docs" / "guide" / "page.md").write_text(stale, encoding="utf-8", newline="\n")
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setattr(f"{__name__}._ROOTS", (("docs/guide", (".md",)),))


def test_a_pre_move_citation_in_a_living_file_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stale_tree(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match=r"docs/guide/page\.md: cites tools/tests/test_cli\.py, a pre-move path"):
        _check()


def test_a_released_note_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stale_tree(tmp_path, monkeypatch)
    assert [path.name for path in _unreleased_release_notes()] == ["0.2.0.md"]
    with pytest.raises(AssertionError) as raised:
        _check()
    assert "0.1.0.md" not in str(raised.value)
