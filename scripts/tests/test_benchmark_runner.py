"""Tests for the cross-platform benchmark runner."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "run_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("run_benchmark", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_benchmark_output_path_uses_explicit_label() -> None:
    assert _MODULE.benchmark_output_path("pre-refactor") == Path(".benchmarks/benchmark-results-pre-refactor.json")


def test_benchmark_output_path_uses_utc_timestamp_by_default() -> None:
    now = datetime(2026, 7, 14, 12, 34, 56, 789, tzinfo=UTC)

    assert _MODULE.benchmark_output_path(now=now) == Path(".benchmarks/benchmark-results-20260714T123456000789Z.json")


@pytest.mark.parametrize("label", ["", "../escape", "contains space", "-starts-with-dash"])
def test_benchmark_output_path_rejects_unsafe_label(label: str) -> None:
    with pytest.raises(ValueError, match="SWCRAFT_BENCHMARK_LABEL"):
        _MODULE.benchmark_output_path(label)


def test_main_refuses_to_overwrite_a_named_result(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWCRAFT_BENCHMARK_LABEL", "existing")
    output_path = tmp_path / ".benchmarks" / "benchmark-results-existing.json"
    output_path.parent.mkdir()
    output_path.write_text("original", encoding="utf-8")

    assert _MODULE.main([]) == 2
    assert output_path.read_text(encoding="utf-8") == "original"
    assert "already exists" in capsys.readouterr().err
