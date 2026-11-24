"""Every project file is in English.

A quotation is rendered in English and marked as a translation; no listed text file carries
Cyrillic (a file git tracks or does not ignore, with one of :data:`TEXT_SUFFIXES`).

What it does not reach: other non-Latin scripts; Cyrillic outside the basic block U+0400-U+04FF
(the Supplement and Extended-A/B/C blocks, letters no Russian word needs); the files git ignores
(`.venv`, `dist/`, `.vscode-test/`); a listed file that is not UTF-8, skipped without a word; and
listed files outside the suffix list (the one `.png`, the empty `.gitkeep`s, and dotfiles and
one-off suffixes nobody writes prose in). Three listed sites carry Cyrillic as **test data** and
are allowed by name below; a fourth needs a reason here, not a wider allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from conftest import REPO_ROOT
from tracked_files_support import listed_files

#: The basic Cyrillic block, built from its code points: this file carries no Cyrillic of its own.
CYRILLIC = re.compile("[" + chr(0x0400) + "-" + chr(0x04FF) + "]")

#: Every suffix a listed file carries prose or text under; a new suffix joins here, not a wider
#: wildcard, so the docstring's list of what is left out stays true.
TEXT_SUFFIXES = (
    ".md",
    ".py",
    ".ts",
    ".mjs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".rst",
    ".cmd",
    ".sh",
    ".html",
    ".svg",
    ".arxml",
    ".c4",
    ".conf",
)

#: A floor on the listed text files read, well under the tree's count: a tree that shrinks does not
#: fail this, a listing that has gone blind does.
TEXT_FILES_FLOOR = 1_000

#: Listed files whose Cyrillic is test data, not prose: a non-ASCII identifier the Identifier
#: rule must refuse, a requirement id that sorts after ASCII ones, and a measured one-copy escape.
ALLOWED_TEST_DATA = frozenset(
    {
        "packages/tools/tests/test_identifier.py",
        "packages/tools/tests/test_query.py",
        "docs/plans/completed/20261009-one-copy-on-the-wire.md",
    }
)

#: The :data:`ALLOWED_TEST_DATA` entries under a closed path: the public tree does not carry them,
#: so a closed half checks they still carry Cyrillic. Allowed here so a full checkout's scan passes.
CLOSED_TEST_DATA = frozenset({"docs/plans/completed/20261009-one-copy-on-the-wire.md"})


def text_files() -> list[str]:
    """Return the listed text files the gate reads, by POSIX name, the allowed test data left out."""
    return [name for name in listed_files(suffixes=TEXT_SUFFIXES) if name not in ALLOWED_TEST_DATA]


def cyrillic_offenders(names: Iterable[str], *, root: Path = REPO_ROOT) -> list[str]:
    """Return ``name:line`` of the first Cyrillic line of each file of *names* under *root* that has one.

    A name that is no longer a file, or not UTF-8, is skipped.
    """
    offenders: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if CYRILLIC.search(line):
                offenders.append(f"{name}:{number}")
                break
    return offenders


def test_no_listed_text_file_carries_cyrillic() -> None:
    names = text_files()
    assert len(names) >= TEXT_FILES_FLOOR, f"only {len(names)} text files listed; the listing has gone blind"
    offenders = cyrillic_offenders(names)
    message = "Russian in a project file -- render the words in English and mark the translation: "
    assert offenders == [], message + ", ".join(offenders)


def test_the_allowed_test_data_still_exists_and_still_carries_it() -> None:
    """Each allowed file is still reported, so a file that lost its Cyrillic leaves the list."""
    open_data = sorted(ALLOWED_TEST_DATA - CLOSED_TEST_DATA)
    reported = cyrillic_offenders(open_data)
    assert [entry.rsplit(":", 1)[0] for entry in reported] == open_data, (
        "a file of ALLOWED_TEST_DATA carries no Cyrillic any more, or is gone -- remove it from the list"
    )


def test_a_planted_cyrillic_line_is_reported_at_its_line(tmp_path: Path) -> None:
    """A planted Cyrillic letter on line three is reported once as ``name:3``; a clean file is not."""
    (tmp_path / "clean.md").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    pasted = "one\ntwo\na pasted quote " + chr(0x0434) + chr(0x0430) + "\nand " + chr(0x043D) + "\n"
    (tmp_path / "pasted.md").write_text(pasted, encoding="utf-8", newline="\n")
    assert cyrillic_offenders(["clean.md", "pasted.md", "absent.md"], root=tmp_path) == ["pasted.md:3"]
