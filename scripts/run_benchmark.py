"""Run pytest-benchmark and preserve every JSON result under a unique name.

pytest-benchmark opens its ``--benchmark-json`` path during argument parsing, before any
fixture runs, so it fails when the gitignored ``.benchmarks/`` directory is absent. This
wrapper creates the directory, appends a unique ``--benchmark-json`` path and runs pytest
without a shell.

Set ``SWCRAFT_BENCHMARK_LABEL`` to a stable label (``pre-refactor``) when two runs will be
compared; without one, a UTC timestamp makes the file name unique.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

BENCHMARK_LABEL_ENV_VAR = "SWCRAFT_BENCHMARK_LABEL"
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def benchmark_output_path(label: str | None = None, *, now: datetime | None = None) -> Path:
    """Return a unique, filesystem-safe benchmark result path."""
    if label is None:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        label = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    if not _SAFE_LABEL.fullmatch(label):
        raise ValueError(
            f"{BENCHMARK_LABEL_ENV_VAR} must contain only letters, digits, '.', '_' or '-' "
            "and must start with a letter or digit"
        )
    return Path(".benchmarks") / f"benchmark-results-{label}.json"


def main(args: list[str]) -> int:
    if any(arg == "--benchmark-json" or arg.startswith("--benchmark-json=") for arg in args):
        print("run_benchmark.py manages --benchmark-json; do not pass it explicitly", file=sys.stderr)
        return 2

    try:
        output_path = benchmark_output_path(os.environ.get(BENCHMARK_LABEL_ENV_VAR))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        print(
            f"Benchmark JSON already exists: {output_path}; choose a new {BENCHMARK_LABEL_ENV_VAR}",
            file=sys.stderr,
        )
        return 2
    print(f"Benchmark JSON: {output_path}")
    result = subprocess.run([sys.executable, "-m", "pytest", *args, f"--benchmark-json={output_path}"])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
