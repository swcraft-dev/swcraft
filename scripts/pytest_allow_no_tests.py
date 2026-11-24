"""Run pytest and treat "no tests collected" as success."""

from __future__ import annotations

import subprocess
import sys


def main(args: list[str]) -> int:
    result = subprocess.run([sys.executable, "-m", "pytest", *args])
    if result.returncode in (0, 5):
        return 0
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
