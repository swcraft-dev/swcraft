"""The public files hold no development-record marker but the reasoned ``KEPT`` literals.

``scripts/history_markers.py`` scans the expansion of ``public-paths.txt`` at ``HEAD``, the same files
``test_public_tree_hygiene.py`` reads, so the answer is the same in the full checkout and in the
assembled public tree. The script's docstring says what a marker is and what the scan does not reach;
its own cases are ``scripts/tests/test_history_markers.py``.
"""

from __future__ import annotations

from conftest import REPO_ROOT, history_markers_module, public_snapshot_module

_MARKERS = history_markers_module()


def test_no_open_file_holds_a_counted_marker_and_every_kept_entry_is_used_and_reasoned() -> None:
    problems = _MARKERS.check(REPO_ROOT, public_snapshot_module().expansion(REPO_ROOT, "HEAD"))
    assert problems == [], "\n".join([*problems, _MARKERS.FAILURE_HELP])


def test_the_gate_exempts_exactly_its_own_three_files_and_scans_none_of_them() -> None:
    own = {
        "scripts/history_markers.py",
        "scripts/tests/test_history_markers.py",
        "packages/tools/tests/test_public_tree_history.py",
    }
    assert own == _MARKERS.EXEMPT
    assert any(_MARKERS.scan_text(path, (REPO_ROOT / path).read_text(encoding="utf-8")) for path in own)
    assert _MARKERS.scan(REPO_ROOT, sorted(own)) == []
