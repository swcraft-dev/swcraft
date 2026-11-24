"""The relaxed program's raw solutions at the size ceiling, recorded once (`tests/fixtures/layout/ceiling.json`).

HiGHS takes 20 s to 7 min per feasible ceiling case, so the fast test reads them recorded: it
re-checks each in Python integers against the program built today (`check_solution`), whose digest
must equal the recorded one, and builds step 2 from it. One case is still solved in a `slow` test.
Re-record after a change of the relaxed program or of the pinned SciPy, from `packages/specimens/`:

    python -m tests.oracle.ceiling_solutions

What it does not reach: a recorded solution is the one HiGHS returned on the recording platform;
that the solver returns a first-fit-buildable one today is held only for the case the `slow` test solves.
"""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

from swcraft_specimens.specimen.layout import Configuration, Program, relaxed_program, solve

CEILING_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "layout" / "ceiling.json"

#: The ceiling: 5 hubs of 1,000 ports and 4,995 one-port components; 99 SR interfaces of fan-out
#: 100 and one of 95; U = 3,000, at most 1,000 on one.
CEILING_COUNTS = (1000,) * 5 + (1,) * 4995
CEILING_FAN_OUTS = (100,) * 99 + (95,)

#: The five feasible ceiling cases, as (A, D, C, L).
CEILING_CASES = (
    (3000, 4000, 4, 0),
    (3500, 100, 4, 0),
    (3000, 4000, 4, 5),
    (4990, 3000, 4, 0),
    (2000, 9000, 4, 0),
)


def ceiling_configuration(connectors: int, delegations: int, compositions: int, links: int) -> Configuration:
    return Configuration(CEILING_COUNTS, CEILING_FAN_OUTS, 3000, 1000, connectors, links, delegations, compositions)


def program_digest(program: Program) -> str:
    """SHA-256 over the program's variables, bounds and rows: a recorded solution belongs to it."""
    text = json.dumps(
        [
            list(program.names),
            list(program.lower),
            list(program.upper),
            [[list(map(list, row.terms)), row.low, row.high, row.family] for row in program.rows],
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_key(case: tuple[int, int, int, int]) -> str:
    return "A={}, D={}, C={}, L={}".format(*case)


def record() -> None:
    cases = []
    for case in CEILING_CASES:
        program = relaxed_program(ceiling_configuration(*case))
        raw = solve(program)
        assert raw is not None, case
        cases.append(
            {
                "case": case_key(case),
                "program_sha256": program_digest(program),
                "nonzero": [[index, value] for index, value in enumerate(raw) if value],
            }
        )
        print(f"recorded {case_key(case)}", flush=True)
    document = {"recorded_on": f"{platform.system()} {platform.machine()}", "cases": cases}
    CEILING_FIXTURE.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    record()
