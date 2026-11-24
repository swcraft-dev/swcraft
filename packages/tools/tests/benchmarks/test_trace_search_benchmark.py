"""The trace link search at the design scale: report-only, with no assertion on time.

``python scripts/dev.py benchmark`` runs it (``-m benchmark``, deselected from
the fast gate). The model is ``trace_models.scale_model``'s: 6 000 requirements
over 20 documents, a third of them linked, and 150 components of 10 ports and 3
runnables each -- synthetic and deterministic, never a real project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from swcraft_tools.project import require_project
from swcraft_tools.trace_search import run_trace
from trace_models import scale_model, write_project


@pytest.mark.benchmark(group="trace_search")
def test_benchmark_trace_search_at_the_design_scale(benchmark: Any, tmp_path: Path) -> None:
    manifest, _config = write_project(scale_model(), tmp_path / "project")
    project = require_project(str(manifest))

    run = benchmark.pedantic(run_trace, args=(project, None), rounds=1, iterations=1)

    document = run.document
    assert len(document["requirements"]) == 6000
    print("\n--- Trace link search at the design scale ---")
    print(document["summary"])
    print(f"seconds = {benchmark.stats.stats.max:.1f}")
