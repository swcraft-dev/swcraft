"""``filesystem_support.case_sensitive_under`` answers, and answers consistently.

The probe is what every test of ``rule:storage.filename-case-collision`` branches
on: it answers on every host rather than skipping, and leaves nothing behind.

**Not reached:** which answer is *right* for a host (a case-sensitive APFS volume
is a legal development host); the second test checks consistency between two
spellings of one question, not an oracle.
"""

from __future__ import annotations

from pathlib import Path

import filesystem_support


def test_the_case_probe_answers_and_leaves_nothing_behind(tmp_path: Path) -> None:
    answer = filesystem_support.case_sensitive_under(tmp_path)

    assert isinstance(answer, bool)
    assert list(tmp_path.iterdir()) == [], "the probe removes its own directory"


def test_the_case_probe_agrees_with_the_swapped_spelling_of_one_name(tmp_path: Path) -> None:
    (tmp_path / "probe.yaml").write_text("x\n", encoding="utf-8", newline="\n")
    folds_case = (tmp_path / "PROBE.yaml").exists()

    assert filesystem_support.case_sensitive_under(tmp_path) is (not folds_case)
