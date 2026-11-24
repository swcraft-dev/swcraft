"""The root ``THIRD-PARTY-NOTICES.md`` against the pins a release archive is built from.

The file is written by hand. Its second part names what a release archive
bundles -- Node.js, CPython as python-build-standalone publishes it, elkjs and the companion VS Code
extensions -- each with the version ``scripts/workstation/pins/runtime-pins.json`` pins. A pin moved by
``dev.py update-pins`` or by hand and not carried into the notices fails here, by name.

What this does **not** reach: the first part (third-party content inside the repository), because no
test can know what is "third-party content"; the declared licences in the table, which only a person
reading each publisher's declaration can check; and the Python wheel closure's individual packages,
which the generated archive notices list and this file deliberately does not.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import REPO_ROOT, build_artifacts_module

NOTICES = REPO_ROOT / "THIRD-PARTY-NOTICES.md"
#: The pins file the archive build reads, from the build itself: a moved file moves here too.
RUNTIME_PINS = REPO_ROOT / build_artifacts_module().RUNTIME_PINS_FILE

#: The heading of the part this test holds; the table under it is read row by row.
BUNDLED_HEADING = "## 2. What a release archive bundles"

#: The part names the table uses for the three single payloads. The extensions are named by their
#: Marketplace identifier in backticks, read from the pins rather than listed here.
NODE_PART = "Node.js"
PYTHON_PART = "CPython, as python-build-standalone publishes it"
ELKJS_PART = "elkjs"
#: The one row that names no pinned payload: its packages are listed in each archive's own notices.
WHEEL_CLOSURE_PART = "The Python wheel closure"


def bundled_rows(text: str) -> dict[str, str]:
    """``{part: version}`` of every table row under :data:`BUNDLED_HEADING`; refuses a duplicate part."""
    _, found, after = text.partition(f"\n{BUNDLED_HEADING}\n")
    if not found:
        raise ValueError(f"THIRD-PARTY-NOTICES.md has no {BUNDLED_HEADING!r} section")
    section = re.split(r"\n## ", after, maxsplit=1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Part":
            continue
        if cells[0] in rows:
            raise ValueError(f"the part {cells[0]!r} has two rows")
        rows[cells[0]] = cells[1]
    return rows


def expected_rows(pins: dict) -> dict[str, str]:
    """The rows the pins require: each payload with its pinned version, in the table's spelling."""
    python = pins["python"]
    rows = {
        NODE_PART: pins["node"]["version"],
        PYTHON_PART: f"{python['version']} (release {python['build_tag']})",
        ELKJS_PART: pins["elkjs"]["version"],
    }
    for extension in pins["extensions"]:
        rows[f"`{extension['id']}`"] = extension["version"]
    return rows


def _notices_text() -> str:
    return NOTICES.read_text(encoding="utf-8")


def _pins() -> dict:
    return json.loads(RUNTIME_PINS.read_text(encoding="utf-8"))


def drift(rows: dict[str, str], expected: dict[str, str]) -> dict[str, tuple[str | None, str]]:
    """``{part: (notices, pins)}`` of every expected part whose row is missing or at another version."""
    return {part: (rows.get(part), version) for part, version in expected.items() if rows.get(part) != version}


def test_every_pinned_payload_has_its_row_at_its_pinned_version() -> None:
    wrong = drift(bundled_rows(_notices_text()), expected_rows(_pins()))
    assert not wrong, f"THIRD-PARTY-NOTICES.md disagrees with runtime-pins.json ((notices, pins) by part): {wrong}"


def test_the_table_names_no_part_the_pins_do_not() -> None:
    """A payload removed from the archive leaves the notices with it; only the wheel closure row is unpinned."""
    rows = bundled_rows(_notices_text())
    extra = set(rows) - set(expected_rows(_pins())) - {WHEEL_CLOSURE_PART}
    assert not extra, f"THIRD-PARTY-NOTICES.md names parts runtime-pins.json does not pin: {sorted(extra)}"
    assert WHEEL_CLOSURE_PART in rows


def test_the_notices_state_the_licences_the_repository_carries() -> None:
    """The first part's three entries and the pointer to the archive's own generated notices stay."""
    text = _notices_text()
    for needle in ("LikeC4", "MIT", "WATERS 2015", "AUTOSAR", "no AUTOSAR specification text"):
        assert needle in text, f"THIRD-PARTY-NOTICES.md no longer says {needle!r}"


# --- the reader itself ---------------------------------------------------------------------------


_SAMPLE = """# Third-party notices

## 1. In the repository

| Part | Version | Declared licence |
|---|---|---|
| Ignored | 9 | X |

## 2. What a release archive bundles

| Part | Version | Declared licence |
|---|---|---|
| Node.js | 1.2.3 | MIT |
| `a.b` | 4.5.6 | MIT |
"""


def test_the_reader_takes_the_rows_of_the_second_part_only() -> None:
    assert bundled_rows(_SAMPLE) == {"Node.js": "1.2.3", "`a.b`": "4.5.6"}


def test_the_reader_refuses_a_duplicate_row() -> None:
    with pytest.raises(ValueError, match="two rows"):
        bundled_rows(_SAMPLE + "| Node.js | 1.2.4 | MIT |\n")


def test_the_reader_refuses_a_file_without_the_section() -> None:
    with pytest.raises(ValueError, match="has no"):
        bundled_rows("# Third-party notices\n")


def test_a_moved_pin_is_the_one_difference_reported() -> None:
    """On a sample, the moved Node.js pin is the one difference, the Python and extension spellings included."""
    sample = _SAMPLE + f"| {PYTHON_PART} | 3.11.0 (release 1) | PSF |\n| {ELKJS_PART} | 0 | EPL |\n"
    pins = {
        "node": {"version": "1.2.4"},
        "python": {"version": "3.11.0", "build_tag": "1"},
        "elkjs": {"version": "0"},
        "extensions": [{"id": "a.b", "version": "4.5.6"}],
    }
    assert drift(bundled_rows(sample), expected_rows(pins)) == {NODE_PART: ("1.2.3", "1.2.4")}
