"""An authorized change of the ARXML goldens, asserted rather than eyeballed.

An authorized golden change costs an expected-difference list committed as data, hand-derived before
the writer runs, and this test asserting the observed difference equals it. The before-copies under
``fixtures/arxml_golden/`` are committed for this purpose alone: a difference cannot be observed against
a baseline that exists only in history.

Each document is flattened into one line per element, ``{tag}|{path}|{DEST}|{text}``, and three axes are
compared: a declared rename, the multiset of the remaining lines, and the child order of every parent
whose child set is unchanged.

**What this does not reach:** attributes other than ``DEST``, and whitespace; ``test_arxml_golden.py``'s
byte comparison against a fresh generation is the authority.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from conftest import GOLDEN_DIR, REPO_ROOT

AR_NS = "{http://autosar.org/schema/r4.0}"

BEFORE_DIR = GOLDEN_DIR / "differences" / "before"
#: The goldens as the recorded change left them, pinned by digest: the live goldens differ from them.
AFTER_DIR = GOLDEN_DIR / "differences" / "after"
DIFFERENCES_PATH = GOLDEN_DIR / "differences.json"

DIFFERENCES = json.loads(DIFFERENCES_PATH.read_text(encoding="utf-8"))["goldens"]


def _local(tag: str) -> str:
    """The tag without the AUTOSAR namespace."""
    return tag[len(AR_NS) :] if tag.startswith(AR_NS) else tag


def _short_name_of(element: ET.Element) -> str | None:
    """The element's own ``SHORT-NAME`` text, or None if it carries none."""
    child = element.find(f"{AR_NS}SHORT-NAME")
    return child.text if child is not None and child.text else None


def _flatten(root: ET.Element) -> tuple[list[str], dict[str, list[str]]]:
    """Flatten a parsed document into structural lines and a named-child index.

    Returns:
        ``(lines, children)``: one ``{tag}|{path}|{DEST}|{text}`` line per element in document order,
        ``path`` being the nearest ``SHORT-NAME`` chain; and each named element's path mapped to the
        ordered names of the named elements below it (wrappers carry no path segment, as in
        ``ShortNameScope``).
    """
    lines: list[str] = []
    children: dict[str, list[str]] = {}

    def walk(element: ET.Element, path: str) -> None:
        own = _short_name_of(element)
        if own is not None:
            children.setdefault(path, []).append(own)
            path = f"{path}/{own}"
            children.setdefault(path, [])
        dest = element.get("DEST", "")
        text = (element.text or "").strip()
        lines.append(f"{_local(element.tag)}|{path}|{dest}|{text}")
        for child in element:
            walk(child, path)

    walk(root, "")
    return lines, children


def _document(text: str) -> tuple[list[str], dict[str, list[str]]]:
    return _flatten(ET.fromstring(text))


def _rename(lines: list[str], renames: list[list[str]]) -> list[str]:
    """Apply the declared substitutions to every line, in declaration order."""
    renamed = []
    for line in lines:
        for old, new in renames:
            line = line.replace(old, new)
        renamed.append(line)
    return renamed


def _multiset_difference(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    """``(removed, added)`` as sorted lists, with multiplicity."""
    before_counts = Counter(before)
    after_counts = Counter(after)
    removed = sorted((before_counts - after_counts).elements())
    added = sorted((after_counts - before_counts).elements())
    return removed, added


def _order_changes(
    before: dict[str, list[str]],
    after: dict[str, list[str]],
) -> list[dict[str, object]]:
    """Every parent whose child SET is unchanged and whose child ORDER is not.

    A parent whose set changed is excluded, its children being in the multisets already; so a reorder
    under a renamed parent shows only once the ``renamed`` declaration folds the rename away.
    """
    changes: list[dict[str, object]] = []
    for parent in sorted(set(before) & set(after)):
        if set(before[parent]) == set(after[parent]) and before[parent] != after[parent]:
            changes.append({"parent": parent, "before": before[parent], "after": after[parent]})
    return changes


def _observed(name: str, entry: dict) -> dict[str, object]:
    """The observed difference between the before-copy and the after-copy."""
    before_lines, before_children = _document((BEFORE_DIR / f"{name}.arxml").read_text(encoding="utf-8"))
    after_lines, after_children = _document((AFTER_DIR / f"{name}.arxml").read_text(encoding="utf-8"))

    renames = [list(pair) for pair in entry["renamed"]]
    before_lines = _rename(before_lines, renames)
    before_children = {
        _rename([parent], renames)[0]: _rename(names, renames) for parent, names in before_children.items()
    }

    removed, added = _multiset_difference(before_lines, after_lines)
    return {
        "structure_removed": removed,
        "structure_added": added,
        "order_changes": _order_changes(before_children, after_children),
    }


class TestTheCommittedDifferenceListIsTheObservedDifference:
    """The assertion, one test per golden plus the two that hold the data honest."""

    @pytest.mark.parametrize("name", sorted(DIFFERENCES))
    def test_the_observed_difference_equals_the_committed_list(self, name: str) -> None:
        entry = DIFFERENCES[name]
        observed = _observed(name, entry)
        expected = {
            "structure_removed": entry["structure_removed"],
            "structure_added": entry["structure_added"],
            "order_changes": entry["order_changes"],
        }
        assert observed == expected, (
            f"golden '{name}' differs from its pre-Phase-23 copy in a way "
            f"`fixtures/arxml_golden/differences.json` does not declare. Either the writer "
            f"is wrong or the design changed; a golden is not regenerated to make this pass."
        )

    @pytest.mark.parametrize("name", sorted(DIFFERENCES))
    def test_a_golden_declared_byte_identical_really_is(self, name: str) -> None:
        entry = DIFFERENCES[name]
        before = (BEFORE_DIR / f"{name}.arxml").read_text(encoding="utf-8")
        after = (AFTER_DIR / f"{name}.arxml").read_text(encoding="utf-8")
        if entry["bytes_identical"]:
            assert after == before, f"golden '{name}' is declared byte-identical to its pre-Phase-23 copy and is not"
        else:
            assert after != before, f"golden '{name}' is declared to change and did not; the difference list is stale"

    def test_the_list_covers_every_committed_golden(self) -> None:
        """Three goldens, three entries — including ``empty-4.2.2``, which ``GOLDEN_MODEL_ROOTS`` omits."""
        committed = sorted(path.stem for path in GOLDEN_DIR.glob("*.arxml"))
        assert committed == sorted(DIFFERENCES), (
            "every committed golden owes an entry in differences.json, even one whose entry is 'no difference'"
        )

    def test_every_before_copy_is_present(self) -> None:
        """The baseline exists for every entry — a missing one is an unasserted golden."""
        for name in DIFFERENCES:
            assert (BEFORE_DIR / f"{name}.arxml").is_file(), f"no pre-Phase-23 copy of golden '{name}'"

    @pytest.mark.parametrize("name", sorted(DIFFERENCES))
    def test_the_before_copy_is_not_git_ignored(self, name: str) -> None:
        """The before-copies are tracked: ``.gitignore`` ignores ``*.arxml`` and its negations are non-recursive.

        Without a negation ``git add`` skips them silently and a fresh clone lacks the oracle's baseline.
        """
        path = (BEFORE_DIR / f"{name}.arxml").relative_to(REPO_ROOT)
        # --no-index so the answer comes from the ignore rules alone, and no
        # --verbose so the exit code stays the verdict (a matched *negation*
        # makes --verbose exit 0 too).
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode != 0, (
            f"{path} is git-ignored — add a negation to .gitignore, or the baseline "
            "the differences assertion rests on is silently never committed."
        )

    def test_the_before_copies_are_the_branch_point_digests(self) -> None:
        """The baselines are the branch point's bytes: a regenerated copy would compare the change to itself."""
        expected = {
            "basic-4.2.2": "a6ef540773660d7a3169870114f94b616824258db24039487665d00bad5fd14c",
            "composite-4.2.2": "2b2fde13f71d4d946044b987d37407f3d401c62e26a859b86a311f295fbaec2c",
            "empty-4.2.2": "5ffcd165c2de3d0632d55e0cf4e307e785dda73a84e2ff8b6221b7f4565560ba",
        }
        for name, digest in expected.items():
            data = (BEFORE_DIR / f"{name}.arxml").read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest, (
                f"pre-Phase-23 copy of '{name}' is not the branch point's"
            )

    def test_every_after_copy_is_present(self) -> None:
        """The after-state exists for every entry, as the before-copy does."""
        for name in DIFFERENCES:
            assert (AFTER_DIR / f"{name}.arxml").is_file(), f"no after-copy of golden '{name}'"

    def test_the_after_copies_are_the_recorded_after_digests(self) -> None:
        """The after-side is the bytes the recorded change committed (the golden README's "After" digests).

        A copy regenerated by the current writer would fold a later re-freeze's delta into the recorded difference.
        """
        expected = {
            "basic-4.2.2": "0a2b711432c87657514c4cfca71022917216da439ba4bda609a3e828c07d623c",
            "composite-4.2.2": "5564f9866c202e50dfafeb6b10b51f18730e1d898f5cf47db9638bbdc4b8cb4a",
            "empty-4.2.2": "5ffcd165c2de3d0632d55e0cf4e307e785dda73a84e2ff8b6221b7f4565560ba",
        }
        for name, digest in expected.items():
            data = (AFTER_DIR / f"{name}.arxml").read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest, f"the after-copy of '{name}' is not the recorded golden"

    @pytest.mark.parametrize("name", sorted(DIFFERENCES))
    def test_the_after_copy_is_not_git_ignored(self, name: str) -> None:
        """The same non-recursive-negation trap as the before-copies, one directory over."""
        path = (AFTER_DIR / f"{name}.arxml").relative_to(REPO_ROOT)
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode != 0, (
            f"{path} is git-ignored — add a negation to .gitignore, or the after-side "
            "the differences assertion rests on is silently never committed."
        )


class TestTheComparisonItselfBites:
    """A mutated copy of a golden stops matching the committed difference."""

    def test_an_unexpected_extra_element_is_a_reported_difference(self, tmp_path: Path) -> None:
        before_lines, _ = _document((BEFORE_DIR / "empty-4.2.2.arxml").read_text(encoding="utf-8"))
        mutated = before_lines + ["SW-COMPONENT-PROTOTYPE|/ComponentTypes/Root/Ghost||"]
        removed, added = _multiset_difference(before_lines, mutated)
        assert removed == []
        assert added == ["SW-COMPONENT-PROTOTYPE|/ComponentTypes/Root/Ghost||"]

    def test_a_reorder_under_an_unchanged_parent_is_a_reported_difference(self) -> None:
        before = {"/ComponentTypes/Root": ["A", "B"]}
        after = {"/ComponentTypes/Root": ["B", "A"]}
        assert _order_changes(before, after) == [
            {"parent": "/ComponentTypes/Root", "before": ["A", "B"], "after": ["B", "A"]}
        ]

    def test_a_reorder_under_a_parent_whose_children_changed_is_not_double_reported(self) -> None:
        """The stated limit of ``_order_changes``, asserted rather than described."""
        before = {"/ComponentTypes/Root": ["A", "B"]}
        after = {"/ComponentTypes/Root": ["B", "C"]}
        assert _order_changes(before, after) == []
