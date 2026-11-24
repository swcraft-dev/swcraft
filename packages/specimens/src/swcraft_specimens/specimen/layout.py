"""The entry planner: the layout of one configuration as an integer program.

Notation as in `connectors.py` and `delegations.py`: n components, the first k the hubs; U and u1
the unconnected ports, R the reached ones; A the connectors, L the links, D the delegations, C the
compositions; F the SR + CS fan-outs (the stubs carry no connector). A **configuration** is one
port placement and one fan-out tail (`Configuration`). A **layout** (`Layout`) decides per component
its unconnected, connector-reached and delegation-only ports, the connector blocks and the stars.

:func:`plan` answers a layout or none. Two programs describe the layouts: the exact one
(:func:`exact_program`, the star placement itself) and a relaxed one (:func:`relaxed_program`) that
every exact layout satisfies, so the relaxed "no" is the exact one's. Their rows, all linear with
constant big-Ms, are the ports, the unconnected ports, the connectors, the blocks with their zero
diagonal, the links, the stars, the delegations, and symmetry rows between identical components.

The solve is SciPy's `milp` (HiGHS) with a zero objective and no time limit; scipy and numpy are
imported inside the solve alone. Status 0 is feasible, 2 infeasible, anything else a
`GenerationError`. A feasible verdict is certified row by row in Python integers
(:func:`check_solution`). An infeasible verdict is certified when integer bound propagation
(:func:`propagate`) empties a domain, otherwise it is HiGHS's floating-point verdict (`Tally`
counts both). The chosen layout is the lexicographically largest feasible vector in one fixed order
(:func:`canonical`). Every "yes" is an integer fact on every platform, but an uncertified "no" can
differ between platforms and lower the chosen value there (guarded by layouts recorded on one platform
and asserted on another).

What this does not reach: the exact program's exactness is held by an exhaustive oracle on every
configuration of at most six ports, not proved beyond it, where an uncertified "no" is HiGHS's
floating-point verdict, not a certificate; the exact program's time at the count
ceiling is unmeasured, and the star search is exhaustive, so its time is not bounded either.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import GenerationError
from ..model import Project
from ..passport import Refusal
from .common import hub_count
from .connectors import (
    FULL_SEARCH,
    ConnectorSolution,
    ConnectorSpec,
    solution_connectors,
    solve_open,
    star_solution,
)
from .vocabulary import DELEGATION, NO_LAYOUT

# --- the constraint families ------------------------------------------------------------------

PORTS = "L1"
UNCONNECTED = "L2"
CONNECTORS = "L3"
BLOCKS = "L4"
LINKS = "L5"
STARS = "L6"
DELEGATIONS = "L7"
SYMMETRY = "symmetry"

#: The bound propagation's passes over every row before a "no" stays the solver's alone.
PROPAGATION_PASSES = 16

#: A layout's sources: the connector search's ascending layout (the preferred candidate), the
#: planner's, or none for D = 0. A record only: what differs with no delegation reads the
#: delegations, never the source.
PREFERRED = "preferred"
SOLVER = "solver"
NO_DELEGATION = "none"

#: A layout's step: none for the preferred candidate and D = 0, step 2's builders, step 3's exact program.
UNPLANNED_STEP = 0
BUILDER_STEP = 2
EXACT_STEP = 3

#: Step 2's builders, as `Trace.builder` records them.
SOLVE_OPEN = "solve-open"
FIRST_FIT = "first-fit"

#: One debug record per configuration planned, with its trace and tally; not configured here, so
#: nothing is printed unless a caller asks.
LOG = logging.getLogger(__name__)

#: Who is told when step 3 starts: a callable taking the notice's text, or `None`. Set by
#: `build.build_specimen_files` around the feasibility check and the build, so the notice reaches
#: the shape computation wherever it first runs.
NOTICE: ContextVar[Callable[[str], None] | None] = ContextVar("NOTICE", default=None)

#: The notice's text; `build_specimen_files` prefixes it with the program and the preset.
EXACT_NOTICE = (
    "the layout needs the specimen generator's exact program, which may take long at this size; stating "
    "components.ports.total and interfaces.fan_out.top shortens it"
)


@dataclass(frozen=True)
class Configuration:
    """One configuration: port counts (descending), SR + CS fan-outs and the stated figures.

    `connectors` (A) is `None` when unstated.
    """

    counts: tuple[int, ...]
    fan_outs: tuple[int, ...]
    unconnected: int
    max_on_one: int
    connectors: int | None
    links: int
    delegations: int
    compositions: int

    @classmethod
    def of_spec(
        cls,
        spec: ConnectorSpec,
        counts: Sequence[int],
        connectable_fan_outs: Sequence[int],
        delegations: int,
        compositions: int,
    ) -> Configuration:
        """The configuration of these connector leaves at these counts and SR + CS fan-outs."""
        return cls(
            tuple(counts),
            tuple(connectable_fan_outs),
            spec.unconnected,
            spec.max_on_one,
            spec.connectors,
            spec.links,
            delegations,
            compositions,
        )

    @property
    def hubs(self) -> int:
        return hub_count(self.counts)

    @property
    def ports(self) -> int:
        return sum(self.counts)

    @property
    def reached(self) -> int:
        """R = P - U."""
        return self.ports - self.unconnected

    @property
    def phi(self) -> int:
        """Phi, the ports on the SR + CS interfaces."""
        return sum(self.fan_outs)

    @property
    def most_connectors(self) -> int:
        """A-bar: A when stated, else min(R, Phi)."""
        if self.connectors is not None:
            return self.connectors
        return max(0, min(self.reached, self.phi))

    def open_units(self, connected: Sequence[int]) -> tuple[int, ...]:
        """Per component its ports no connector end reaches, at these connector-reached ports."""
        return tuple(p - c for p, c in zip(self.counts, connected, strict=True))


# --- the programs -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """low <= sum(coefficient x variable) <= high (`None` unbounded), of one family."""

    terms: tuple[tuple[int, int], ...]
    low: int | None
    high: int | None
    family: str


@dataclass(frozen=True)
class Program:
    """One integer program: its variables, its rows and where each layout variable sits."""

    exact: bool
    names: tuple[str, ...]
    lower: tuple[int, ...]
    upper: tuple[int, ...]
    rows: tuple[Row, ...]
    unconnected: tuple[int, ...]
    connected: tuple[int, ...]
    providers: tuple[int, ...]
    extras: tuple[int, ...]
    #: C_oo, OH, HO, HH.
    blocks: tuple[int, int, int, int]
    #: R_c, the ports a connector end reaches.
    connector_reached: int
    connectors: int
    #: The solver's arrays per column order, built once.
    cache: dict[bool, Any] = field(default_factory=dict, compare=False, repr=False)


class _Builder:
    def __init__(self, omit: frozenset[str]) -> None:
        self.names: list[str] = []
        self.lower: list[int] = []
        self.upper: list[int] = []
        self.rows: list[Row] = []
        self.omit = omit

    def var(self, name: str, low: int, high: int) -> int:
        self.names.append(name)
        self.lower.append(low)
        self.upper.append(high)
        return len(self.names) - 1

    def row(
        self,
        family: str,
        terms: Sequence[tuple[int, int]],
        low: int | None = None,
        high: int | None = None,
    ) -> None:
        if family in self.omit:
            return
        merged: dict[int, int] = {}
        for variable, coefficient in terms:
            merged[variable] = merged.get(variable, 0) + coefficient
        self.rows.append(
            Row(
                tuple((v, c) for v, c in sorted(merged.items()) if c),
                low,
                high,
                family,
            )
        )

    def equal(self, family: str, terms: Sequence[tuple[int, int]], value: int) -> None:
        self.row(family, terms, value, value)


def relaxed_program(configuration: Configuration, omit: frozenset[str] = frozenset()) -> Program:
    """Step 1's program: L6 by aggregates per interface. `omit` leaves families out."""
    return _program(configuration, exact=False, omit=omit)


def exact_program(configuration: Configuration, omit: frozenset[str] = frozenset()) -> Program:
    """Step 3's program: L6 as the star placement. `omit` leaves families out."""
    return _program(configuration, exact=True, omit=omit)


@dataclass(frozen=True)
class _Columns:
    """Where each variable of one program sits.

    `centred` maps (component, interface) to the exact program's (s_ai, o_ai); `aggregates` maps an
    interface to the relaxed program's (x_i, S_i).
    """

    u: tuple[int, ...]
    c: tuple[int, ...]
    inn: tuple[int, ...]
    pr: tuple[int, ...]
    ex: tuple[int, ...]
    z: tuple[int, ...]
    dd: tuple[int, ...]
    h: tuple[int, ...]
    centred: dict[tuple[int, int], tuple[int, int]]
    aggregates: dict[int, tuple[int, int]]
    coo: int
    oh: int
    ho: int
    hh: int
    rc: int
    connectors: int


def _program(configuration: Configuration, *, exact: bool, omit: frozenset[str]) -> Program:
    b = _Builder(omit)
    v = _columns(b, configuration, exact)
    _port_rows(b, configuration, v)
    _unconnected_rows(b, configuration, v)
    _connector_rows(b, v)
    _block_rows(b, configuration, v)
    _link_rows(b, configuration, v)
    if exact:
        _exact_star_rows(b, configuration, v)
    else:
        _relaxed_star_rows(b, configuration, v)
    _delegation_rows(b, configuration, v)
    _symmetry_rows(b, configuration, v)
    return Program(
        exact,
        tuple(b.names),
        tuple(b.lower),
        tuple(b.upper),
        tuple(b.rows),
        v.u,
        v.c,
        v.pr,
        v.ex,
        (v.coo, v.oh, v.ho, v.hh),
        v.rc,
        v.connectors,
    )


def _stars_on(configuration: Configuration) -> list[int]:
    """The interfaces a star fits on (fan-out 2 or more)."""
    return [i for i, fan_out in enumerate(configuration.fan_outs) if fan_out >= 2]


def _columns(b: _Builder, configuration: Configuration, exact: bool) -> _Columns:
    """The variables: per component in index order, then the stars', the blocks, R_c and A."""
    counts, fan_outs = configuration.counts, configuration.fan_outs
    u1, bar = configuration.max_on_one, configuration.most_connectors
    stars_on = _stars_on(configuration)
    per: dict[str, list[int]] = {name: [] for name in ("u", "c", "in", "pr", "ex", "z", "dd", "h")}
    for a, p in enumerate(counts):
        for name, high in (
            ("u", min(p, u1)),
            ("c", p),
            ("in", p),
            ("pr", p),
            ("ex", bar),
            ("z", 1),
            ("dd", p),
            ("h", 1 if p >= u1 else 0),
        ):
            per[name].append(b.var(f"{name}{a}", 0, high))
    centred: dict[tuple[int, int], tuple[int, int]] = {}
    aggregates: dict[int, tuple[int, int]] = {}
    if exact:
        for a, p in enumerate(counts):
            if p < 1:
                continue
            for i in stars_on:
                f = fan_outs[i]
                centred[a, i] = (
                    b.var(f"s{a}_{i}", 0, min(p, f // 2)),
                    b.var(f"o{a}_{i}", 0, f - 1),
                )
    else:
        for i in stars_on:
            f = fan_outs[i]
            aggregates[i] = (b.var(f"x{i}", 0, f), b.var(f"S{i}", 0, f // 2))
    coo = b.var("C_oo", 0, bar)
    oh = b.var("OH", 0, bar)
    ho = b.var("HO", 0, bar)
    hh = b.var("HH", 0, bar)
    rc = b.var("R_c", 0, max(0, configuration.reached))
    if configuration.connectors is not None:
        connectors = b.var("A", configuration.connectors, configuration.connectors)
    else:
        connectors = b.var("A", 0, bar)
    return _Columns(
        tuple(per["u"]),
        tuple(per["c"]),
        tuple(per["in"]),
        tuple(per["pr"]),
        tuple(per["ex"]),
        tuple(per["z"]),
        tuple(per["dd"]),
        tuple(per["h"]),
        centred,
        aggregates,
        coo,
        oh,
        ho,
        hh,
        rc,
        connectors,
    )


def _port_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L1 ports."""
    for a, p in enumerate(configuration.counts):
        b.row(PORTS, [(v.u[a], 1), (v.c[a], 1)], high=p)
        b.equal(PORTS, [(v.c[a], 1), (v.pr[a], -1), (v.inn[a], -1)], 0)
    b.equal(PORTS, [*((c, 1) for c in v.c), (v.rc, -1)], 0)


def _unconnected_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L2 unconnected."""
    b.equal(UNCONNECTED, [(u, 1) for u in v.u], configuration.unconnected)
    for a in range(len(configuration.counts)):
        b.row(UNCONNECTED, [(v.u[a], 1), (v.h[a], -configuration.max_on_one)], low=0)
    if configuration.unconnected > 0:
        b.row(UNCONNECTED, [(h, 1) for h in v.h], low=1)


def _connector_rows(b: _Builder, v: _Columns) -> None:
    """L3 connectors."""
    b.equal(CONNECTORS, [*((x, 1) for x in v.inn), (v.connectors, -1)], 0)
    b.equal(CONNECTORS, [*((x, 1) for x in v.pr), (v.rc, -1), (v.connectors, 1)], 0)
    b.equal(CONNECTORS, [*((x, 1) for x in v.pr), *((x, 1) for x in v.ex), (v.connectors, -1)], 0)


def _block_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L4 the blocks and the zero diagonal."""
    n, hubs = len(configuration.counts), configuration.hubs
    b.equal(BLOCKS, [*((v.inn[a], 1) for a in range(hubs, n)), (v.coo, -1), (v.ho, -1)], 0)
    b.equal(
        BLOCKS,
        [
            *((v.pr[a], 1) for a in range(hubs, n)),
            *((v.ex[a], 1) for a in range(hubs, n)),
            (v.coo, -1),
            (v.oh, -1),
        ],
        0,
    )
    b.equal(BLOCKS, [(v.coo, 1), (v.oh, 1), (v.ho, 1), (v.hh, 1), (v.connectors, -1)], 0)
    for a in range(n):
        other = v.coo if a < hubs else v.hh
        b.row(
            BLOCKS,
            [(v.inn[a], 1), (v.pr[a], 1), (v.ex[a], 1), (other, 1), (v.connectors, -1)],
            high=0,
        )


def _link_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L5 links."""
    hubs = configuration.hubs
    ordinary = len(configuration.counts) - hubs
    if configuration.links > 0:
        b.equal(LINKS, [(v.coo, 1)], ordinary // 2 + configuration.links)
    else:
        b.row(LINKS, [(v.coo, 1)], high=ordinary // 2)
    if hubs == 0:
        b.equal(LINKS, [(v.coo, 1), (v.connectors, -1)], 0)


def _exact_star_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L6 the stars, exact: the star placement per component and interface."""
    fan_outs, n = configuration.fan_outs, len(configuration.counts)
    stars_on = _stars_on(configuration)
    for (_, i), (s, o) in v.centred.items():
        b.row(STARS, [(s, 1), (o, -1)], high=0)
        b.row(STARS, [(o, 1), (s, -(fan_outs[i] - 1))], high=0)
    on_interface: dict[int, list[int]] = {i: [] for i in stars_on}
    of_component: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    for (a, i), (s, o) in v.centred.items():
        on_interface[i].extend((s, o))
        of_component[a].append((s, o))
    for i in stars_on:
        b.row(STARS, [(x, 1) for x in on_interface[i]], high=fan_outs[i])
    for a in range(n):
        mine = of_component[a]
        b.equal(STARS, [*((s, 1) for s, _ in mine), (v.pr[a], -1)], 0)
        b.equal(STARS, [*((o, 1) for _, o in mine), (v.pr[a], -1), (v.ex[a], -1)], 0)


def _relaxed_star_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L6 the stars, relaxed: aggregates per interface with the tightening row."""
    fan_outs, bar = configuration.fan_outs, configuration.most_connectors
    for i, (x, s) in v.aggregates.items():
        b.row(STARS, [(s, 2), (x, -1)], high=0)
        b.row(STARS, [(x, 1), (s, -fan_outs[i])], high=0)
    b.equal(STARS, [*((x, 1) for x, _ in v.aggregates.values()), (v.rc, -1)], 0)
    b.equal(
        STARS,
        [*((s, 1) for _, s in v.aggregates.values()), (v.rc, -1), (v.connectors, 1)],
        0,
    )
    largest = max(fan_outs, default=0)
    for a in range(len(configuration.counts)):
        b.row(STARS, [(v.ex[a], 1), (v.pr[a], -bar)], high=0)
        b.row(STARS, [(v.ex[a], 1), (v.pr[a], -(largest - 2))], high=0)


def _delegation_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """L7 membership and the delegations."""
    reached, delegations = configuration.reached, configuration.delegations
    for a, p in enumerate(configuration.counts):
        b.row(DELEGATIONS, [(v.c[a], 1), (v.z[a], p)], high=p)
        b.row(DELEGATIONS, [(v.dd[a], 1), (v.z[a], -p)], high=0)
        b.row(DELEGATIONS, [(v.dd[a], 1), (v.u[a], 1), (v.c[a], 1)], high=p)
    b.row(DELEGATIONS, [(v.rc, 1)], low=reached - delegations)
    b.row(
        DELEGATIONS,
        [(v.connectors, 1), *((dd, -(configuration.compositions - 1)) for dd in v.dd)],
        high=reached - delegations,
    )


def _symmetry_rows(b: _Builder, configuration: Configuration, v: _Columns) -> None:
    """The symmetry of identical components (same count, same category)."""
    counts, hubs = configuration.counts, configuration.hubs
    for a in range(len(counts) - 1):
        p = counts[a]
        if p and counts[a + 1] == p and (a < hubs) == (a + 1 < hubs):
            b.row(SYMMETRY, [(v.c[a], 1), (v.c[a + 1], -1)], low=0)
            b.row(SYMMETRY, [(v.c[a], p), (v.c[a + 1], -p), (v.u[a], 1), (v.u[a + 1], -1)], low=0)


# --- the solve ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SolverOptions:
    """How a program is handed to the solver: presolve, and the columns in order or reversed."""

    presolve: bool = True
    reversed_columns: bool = False


@dataclass
class Tally:
    """The solves of one layout.

    The feasible ones, and the "no"s certified by propagation and those that rest on the solver
    alone.
    """

    feasible: int = 0
    certified: int = 0
    uncertified: int = 0

    @property
    def solves(self) -> int:
        return self.feasible + self.certified + self.uncertified


Bounds = Mapping[int, tuple[int, int]]


def solve(
    program: Program,
    bounds: Bounds | None = None,
    options: SolverOptions = SolverOptions(),
    tally: Tally | None = None,
) -> tuple[int, ...] | None:
    """A solution of `program` with these bounds replaced, certified in integers, or `None`."""
    tally = Tally() if tally is None else tally
    lower, upper = list(program.lower), list(program.upper)
    for variable, (low, high) in (bounds or {}).items():
        lower[variable], upper[variable] = low, high
    if any(low > high for low, high in zip(lower, upper, strict=True)):
        tally.certified += 1
        return None
    status, message, found = _milp(program, lower, upper, options)
    if status == 0 and found is not None:
        values = tuple(round(value) for value in found)
        check_solution(program, values, lower, upper)
        tally.feasible += 1
        return values
    if status == 2:
        if propagate(program, lower, upper):
            tally.certified += 1
        else:
            tally.uncertified += 1
        return None
    raise GenerationError(f"the layout solver ended with status {status}: {message}")


def _milp(
    program: Program, lower: Sequence[int], upper: Sequence[int], options: SolverOptions
) -> tuple[int, str, list[float] | None]:
    """One HiGHS run; scipy and numpy are imported here alone."""
    import numpy
    import scipy.optimize
    from scipy.sparse import csr_array

    size = len(program.names)
    order = list(range(size))
    if options.reversed_columns:
        order.reverse()
    arrays = program.cache.get(options.reversed_columns)
    if arrays is None:
        column = {variable: index for index, variable in enumerate(order)}
        data: list[int] = []
        rows: list[int] = []
        columns: list[int] = []
        for index, row in enumerate(program.rows):
            for variable, coefficient in row.terms:
                rows.append(index)
                columns.append(column[variable])
                data.append(coefficient)
        matrix = csr_array(
            (numpy.array(data, dtype=float), (numpy.array(rows), numpy.array(columns))),
            shape=(len(program.rows), size),
        )
        low = numpy.array([-numpy.inf if r.low is None else r.low for r in program.rows])
        high = numpy.array([numpy.inf if r.high is None else r.high for r in program.rows])
        arrays = (matrix, low, high)
        program.cache[options.reversed_columns] = arrays
    matrix, low, high = arrays
    constraints = [scipy.optimize.LinearConstraint(matrix, low, high)] if len(program.rows) else []
    result = _on_the_solver_stack(
        lambda: scipy.optimize.milp(
            numpy.zeros(size),
            integrality=numpy.ones(size),
            bounds=scipy.optimize.Bounds(
                numpy.array([lower[v] for v in order], dtype=float),
                numpy.array([upper[v] for v in order], dtype=float),
            ),
            constraints=constraints,
            options={"presolve": options.presolve, "mip_rel_gap": 0.0, "disp": False},
        )
    )
    found: list[float] | None = None
    if result.x is not None:
        found = [0.0] * size
        for index, variable in enumerate(order):
            found[variable] = float(result.x[index])
    return int(result.status), str(result.message), found


#: The stack reserve of the thread a solve runs on, in bytes. HiGHS recurses deeply on a large
#: program: a relaxed program at the size ceiling overflowed a Windows process's main-thread stack,
#: which ends the interpreter with a fatal "stack overflow" rather than an exception. A thread of
#: this reserve holds it, on every platform alike (a reserve, not memory committed up front). Below
#: 256 MiB, which CPython's Windows threads refuse (`ValueError: size not valid`).
SOLVER_STACK_BYTES = 128 * 1024 * 1024
#: The name prefix of the solver thread (`ThreadPoolExecutor` appends "_0").
SOLVER_THREAD_NAME = "swcraft-specimens-solve"


#: The one long-lived thread every solve of the process runs on, started on first use.
_SOLVER: ThreadPoolExecutor | None = None
_SOLVER_LOCK = threading.Lock()


def _solver() -> ThreadPoolExecutor:
    """The process's solver thread: one thread of :data:`SOLVER_STACK_BYTES`, started once and kept.

    Never one per solve: HiGHS keeps its task executor per calling thread, and on Windows a fresh
    thread per call ended worker processes with an access violation inside HiGHS.
    `threading.stack_size` is process-wide, so it is set for this thread's start alone.
    """
    global _SOLVER
    with _SOLVER_LOCK:
        if _SOLVER is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=SOLVER_THREAD_NAME)
            previous = threading.stack_size(SOLVER_STACK_BYTES)
            try:
                executor.submit(threading.get_ident).result()
            finally:
                threading.stack_size(previous)
            _SOLVER = executor
        return _SOLVER


def _on_the_solver_stack(call: Callable[[], Any]) -> Any:
    """`call()` on the solver thread, its result returned or its exception raised.

    What it does not reach: a thread another caller starts meanwhile takes the same reserve
    (harmless); a program needing more than the reserve still overflows; a `call` that waits on the
    solver thread would wait forever (no solve does).
    """
    return _solver().submit(call).result()


def check_solution(
    program: Program,
    values: Sequence[int],
    lower: Sequence[int] | None = None,
    upper: Sequence[int] | None = None,
) -> None:
    """Re-check every bound and row in Python integers; a failure is a `GenerationError`."""
    lower = program.lower if lower is None else lower
    upper = program.upper if upper is None else upper
    if len(values) != len(program.names):
        raise GenerationError("the layout solver's solution fails the integer check (its size)")
    for variable, value in enumerate(values):
        if not lower[variable] <= value <= upper[variable]:
            raise GenerationError(
                f"the layout solver's solution fails the integer check ({program.names[variable]} = {value})"
            )
    for row in program.rows:
        total = sum(coefficient * values[variable] for variable, coefficient in row.terms)
        if (row.low is not None and total < row.low) or (row.high is not None and total > row.high):
            raise GenerationError(f"the layout solver's solution fails the integer check (a row of {row.family})")


def propagate(program: Program, lower: Sequence[int], upper: Sequence[int]) -> bool:
    """Whether integer bound propagation empties a domain within `PROPAGATION_PASSES`: a certified "no"."""
    low, high = list(lower), list(upper)
    for _ in range(PROPAGATION_PASSES):
        changed = False
        for row in program.rows:
            least = sum(c * (low[v] if c > 0 else high[v]) for v, c in row.terms)
            most = sum(c * (high[v] if c > 0 else low[v]) for v, c in row.terms)
            if (row.high is not None and least > row.high) or (row.low is not None and most < row.low):
                return True
            for v, c in row.terms:
                if row.high is not None:
                    room = row.high - (least - c * (low[v] if c > 0 else high[v]))
                    if c > 0:
                        if room // c < high[v]:
                            high[v], changed = room // c, True
                    elif -(room // -c) > low[v]:
                        low[v], changed = -(room // -c), True
                if row.low is not None:
                    need = row.low - (most - c * (high[v] if c > 0 else low[v]))
                    if c > 0:
                        if -(-need // c) > low[v]:
                            low[v], changed = -(-need // c), True
                    elif need // c < high[v]:
                        high[v], changed = need // c, True
                if low[v] > high[v]:
                    return True
        if not changed:
            return False
    return False


# --- the canonical sequence ---------------------------------------------------------------------


@dataclass(frozen=True)
class Canonical:
    """A canonical layout; the star fields are set only when found `with stars`.

    `pieces` (exact program only) are (component, interface, connectors); `values` is the certified
    solution with every canonical value fixed.
    """

    connectors: int
    connected: tuple[int, ...]
    unconnected: tuple[int, ...]
    providers: tuple[int, ...] | None = None
    extras: tuple[int, ...] | None = None
    blocks: tuple[int, int, int, int] | None = None
    pieces: tuple[tuple[int, int, int], ...] | None = None
    values: tuple[int, ...] = field(default=(), compare=False, repr=False)

    def starred(self) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, int, int, int]]:
        """The stars, the extras and the blocks; a `GenerationError` when found without them."""
        if self.providers is None or self.extras is None or self.blocks is None:
            raise GenerationError("the canonical layout was found without its stars")
        return self.providers, self.extras, self.blocks


class _Sequence:
    """The feasibility questions of one canonical sequence, every value fixed as it is found."""

    def __init__(self, program: Program, options: SolverOptions, tally: Tally) -> None:
        self.program = program
        self.options = options
        self.tally = tally
        self.fixed: dict[int, tuple[int, int]] = {}

    def solve(self, variable: int, low: int, high: int) -> tuple[int, ...] | None:
        bounds = dict(self.fixed)
        bounds[variable] = (low, high)
        return solve(self.program, bounds, self.options, self.tally)

    def upper(self, variable: int) -> int:
        return self.fixed.get(variable, (0, self.program.upper[variable]))[1]

    def largest(self, variable: int, witness: tuple[int, ...]) -> tuple[int, ...]:
        """The largest feasible value of `variable`, by bisection from the witness's; fixed."""
        low, top = witness[variable], self.upper(variable)
        while low < top:
            middle = (low + top + 1) // 2
            found = self.solve(variable, middle, top)
            if found is None:
                top = middle - 1
            else:
                witness, low = found, found[variable]
        self.fixed[variable] = (low, low)
        return witness

    def smallest_admitted(self, choosable: Sequence[int]) -> tuple[int, ...] | None:
        """The smallest feasible count of `choosable`, fixed; `None` when none is."""
        variable = self.program.connectors
        admitted = set(choosable)
        top_bound = self.program.upper[variable]
        members = sorted(admitted)
        if not members:
            return None
        floor = members[0]
        while True:
            witness = self.solve(variable, floor, top_bound)
            if witness is None:
                return None
            low, top = floor, witness[variable]
            while low < top:
                middle = (low + top) // 2
                found = self.solve(variable, floor, middle)
                if found is None:
                    low = middle + 1
                else:
                    witness, top = found, found[variable]
            if top in admitted:
                self.fixed[variable] = (top, top)
                return witness
            above = [member for member in members if member > top]
            if not above:
                return None
            floor = above[0]


def canonical(
    program: Program,
    configuration: Configuration,
    choosable: Sequence[int] | None = None,
    *,
    stars: bool = False,
    options: SolverOptions = SolverOptions(),
    tally: Tally | None = None,
) -> Canonical | None:
    """The canonical layout of `program`, or `None` when it has none.

    With A unstated the count is the smallest feasible of `choosable` (every count up to A-bar when
    `None`); with `stars` the sequence goes on to the stars, the extras, the blocks and, on the exact
    program, the star placement. `tally` counts every solve, a "no" included.
    """
    begun = _begin(program, configuration, choosable, options, Tally() if tally is None else tally)
    if begun is None:
        return None
    sequence, witness = begun
    if not stars:
        return _counts(program, witness)
    return _with_stars(sequence, witness, configuration)


def _begin(
    program: Program,
    configuration: Configuration,
    choosable: Sequence[int] | None,
    options: SolverOptions,
    tally: Tally,
) -> tuple[_Sequence, tuple[int, ...]] | None:
    """Fix A (when unstated) and (c, u) per component; `None` when the program has no solution."""
    sequence = _Sequence(program, options, tally)
    if configuration.connectors is None:
        if choosable is None:
            choosable = range(program.upper[program.connectors] + 1)
        witness = sequence.smallest_admitted(choosable)
    else:
        witness = solve(program, None, options, tally)
    if witness is None:
        return None
    for a in range(len(configuration.counts)):
        witness = sequence.largest(program.connected[a], witness)
        witness = sequence.largest(program.unconnected[a], witness)
    return sequence, witness


def _counts(program: Program, witness: tuple[int, ...]) -> Canonical:
    return Canonical(
        witness[program.connectors],
        tuple(witness[v] for v in program.connected),
        tuple(witness[v] for v in program.unconnected),
        values=witness,
    )


def _with_stars(sequence: _Sequence, witness: tuple[int, ...], configuration: Configuration) -> Canonical:
    """Continue a fixed sequence: pr_a, ex_a, then C_oo, OH, HO, each the largest; then the placement."""
    program = sequence.program
    for a in range(len(configuration.counts)):
        witness = sequence.largest(program.providers[a], witness)
        witness = sequence.largest(program.extras[a], witness)
    for variable in program.blocks[:3]:
        witness = sequence.largest(variable, witness)
    providers = tuple(witness[v] for v in program.providers)
    extras = tuple(witness[v] for v in program.extras)
    coo, oh, ho, hh = (witness[v] for v in program.blocks)
    pieces = None
    if program.exact:
        outs = [p + e for p, e in zip(providers, extras, strict=True)]
        placement = star_search(configuration.fan_outs, providers, outs)
        if placement is None:
            raise GenerationError("the star search finds no placement the exact program certified")
        pieces = star_pieces(placement)
    found = _counts(program, witness)
    return replace(found, providers=providers, extras=extras, blocks=(coo, oh, ho, hh), pieces=pieces)


# --- the builders of the stars -------------------------------------------------------------------

#: A star placement: (component, interface, stars, connectors), each with stars > 0.
Placement = tuple[tuple[int, int, int, int], ...]
#: Stars as (component, interface, connectors).
Pieces = tuple[tuple[int, int, int], ...]


def star_search(fan_outs: Sequence[int], providers: Sequence[int], connectors: Sequence[int]) -> Placement | None:
    """The deterministic depth-first star search of step 3; the first complete placement, or `None`.

    Components in index order, interfaces in fan-out order, stars and connectors from the most down.
    Component a centres `providers[a]` stars carrying `connectors[a]` connectors; a star of o
    connectors takes 1 + o ports of its interface.
    """
    n = len(providers)
    if any(out < stars or (stars == 0) != (out == 0) for stars, out in zip(providers, connectors, strict=True)):
        return None
    usable = [i for i, fan_out in enumerate(fan_outs) if fan_out >= 2]
    slots = [(a, i) for a in range(n) if providers[a] for i in usable]
    if not slots:
        # No star at all, or stars with no interface of fan-out 2 or more to take them.
        return None if any(providers) else ()
    rooms = list(fan_outs)
    left_stars, left_out = list(providers), list(connectors)

    def options(position: int) -> Iterator[tuple[int, int]]:
        a, i = slots[position]
        last = position + 1 == len(slots) or slots[position + 1][0] != a
        f, room = fan_outs[i], rooms[i]
        stars_left, out_left = left_stars[a], left_out[a]
        for stars in range(min(stars_left, room // 2), -1, -1):
            if last and stars != stars_left:
                continue
            if stars == 0:
                if not last or out_left == 0:
                    yield 0, 0
                continue
            top = min(out_left, (f - 1) * stars, room - stars)
            least = out_left if last else stars
            for out in range(top, least - 1, -1):
                if out_left - out >= stars_left - stars:
                    yield stars, out

    def apply(position: int, option: tuple[int, int], sign: int) -> None:
        a, i = slots[position]
        stars, out = option
        rooms[i] -= sign * (stars + out)
        left_stars[a] -= sign * stars
        left_out[a] -= sign * out

    stack = [options(0)]
    applied: list[tuple[int, int]] = []
    while stack:
        if len(applied) == len(stack):
            apply(len(stack) - 1, applied.pop(), -1)
        option = next(stack[-1], None)
        if option is None:
            stack.pop()
            continue
        apply(len(stack) - 1, option, 1)
        applied.append(option)
        if len(stack) == len(slots):
            return tuple((a, i, stars, out) for (a, i), (stars, out) in zip(slots, applied, strict=True) if stars)
        stack.append(options(len(stack)))
    return None


def star_pieces(placement: Placement) -> Pieces:
    """Each (component, interface)'s connectors split evenly over its stars, the rest on the first."""
    pieces: list[tuple[int, int, int]] = []
    for a, i, stars, out in placement:
        share, extra = divmod(out, stars)
        pieces.extend((a, i, share + (1 if k < extra else 0)) for k in range(stars))
    return tuple(pieces)


def first_fit(fan_outs: Sequence[int], providers: Sequence[int], extras: Sequence[int]) -> Pieces | None:
    """Step 2's second builder: each star, largest first, into the tightest interface that holds it.

    Extras spread evenly over a component's stars; ties by component index, then fan-out order.
    `None` when a star fits nowhere.
    """
    stars: list[tuple[int, int]] = []
    for a, (count, extra) in enumerate(zip(providers, extras, strict=True)):
        if count == 0:
            if extra:
                return None
            continue
        share, rest = divmod(extra, count)
        stars.extend((a, share + (1 if k < rest else 0)) for k in range(count))
    stars.sort(key=lambda star: (-star[1], star[0]))
    rooms = list(fan_outs)
    pieces: list[tuple[int, int, int]] = []
    for a, extra in stars:
        size = 2 + extra
        fitting = [i for i, room in enumerate(rooms) if room >= size]
        if not fitting:
            return None
        i = min(fitting, key=lambda j: (rooms[j], j))
        rooms[i] -= size
        pieces.append((a, i, 1 + extra))
    return tuple(pieces)


# --- the bounds and the refusal -----------------------------------------------------------------


def layout_bounds(
    reached: int,
    connectors: int | None,
    compositions: int,
    phi: int,
    choosable: Sequence[int] = (),
) -> tuple[int, int]:
    """The least and most delegations some layout may have, checked before any solve.

    With A unstated, `choosable` gives the counts: the least is R - min(R, Phi), and the most C x R
    when 0 is choosable, else at the least positive count.
    """
    if connectors == 0:
        return reached, compositions * reached
    if connectors is not None:
        return (
            reached - min(2 * connectors, reached, phi),
            compositions * (reached - connectors) - (compositions - 1),
        )
    positive = [count for count in choosable if count > 0]
    if not positive:
        # A = 0 alone, or no count at all (the bounds empty).
        return (reached, compositions * reached) if 0 in choosable else (reached, reached - 1)
    least = reached - min(reached, phi)
    if 0 in choosable:
        return least, compositions * reached
    first = min(positive)
    return least, compositions * (reached - first) - (compositions - 1)


def no_layout_refusal(configuration: Configuration, tried: int) -> Refusal:
    """`no-layout` at `connectors.delegation`: no configuration tried has a layout."""
    c = configuration
    connectors = f"{c.connectors} connectors" if c.connectors is not None else "the connectors chosen"
    return Refusal(
        DELEGATION,
        NO_LAYOUT,
        f"states {c.delegations}; with {c.ports} ports on {len(c.counts)} components "
        f"({c.hubs} hubs), {c.unconnected} unconnected (at most {c.max_on_one} on one), "
        f"{connectors}, {c.links} links, {c.compositions} compositions and {c.phi} "
        "ports on the sender-receiver and client-server interfaces, no layout of the connectors "
        "and the delegations exists in the specimen generator's chain, for the fan-out tails and counts "
        f"beyond top that the specimen generator derives ({tried} tried)",
    )


def derived_total_clause(budget: int) -> str:
    """What `derived` adds to `no-layout` when `components.ports.total` is derived."""
    return (
        f"the derivation tried up to {budget} totals, each with its own layout program (at the "
        "largest sizes up to about an hour each); stating components.ports.total makes it one"
    )


# --- the layout and the three steps ---------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """The layout of one configuration.

    Per component: `providers` its stars, `requires` its connector-reached require ports, `extras`
    its connectors beyond one per star. A component is a root member when a connector end reaches
    it, or when it has no reached port and R_c > 0; the others are deep. `reach`, `steps` and
    `repeats` are the delegations' three sources; `source` and `step` record where it came from.
    """

    unconnected: tuple[int, ...]
    connected: tuple[int, ...]
    delegated: tuple[int, ...]
    providers: tuple[int, ...]
    requires: tuple[int, ...]
    extras: tuple[int, ...]
    root: tuple[int, ...]
    deep: tuple[int, ...]
    reach: int
    steps: int
    repeats: int
    connectors: int
    blocks: tuple[int, int, int, int]
    pieces: Pieces
    source: str
    step: int

    @property
    def connector_reached(self) -> int:
        """R_c."""
        return sum(self.connected)

    @property
    def deep_delegated(self) -> int:
        """D_deep: the deep components' delegation-only ports."""
        return sum(self.delegated[a] for a in self.deep)

    @property
    def root_delegated(self) -> int:
        """D_root: the root members' delegation-only ports."""
        return sum(self.delegated[a] for a in self.root)

    @property
    def delegations(self) -> int:
        """D, the three sources together."""
        return self.reach + self.steps + self.repeats

    @property
    def open_units(self) -> tuple[int, ...]:
        """Per component its ports no connector end reaches: unconnected or delegation-only."""
        return tuple(u + d for u, d in zip(self.unconnected, self.delegated, strict=True))

    @classmethod
    def of_solution(
        cls,
        counts: Sequence[int],
        unconnected: Sequence[int],
        solution: ConnectorSolution,
        delegations: int,
        compositions: int,
        *,
        source: str,
        step: int,
    ) -> Layout:
        """The layout a construction lays with `unconnected` of its open ports unconnected.

        With no delegation every component is a root member. A mismatch is a `GenerationError`.
        """
        open_units = solution.unconnected
        n = len(counts)
        if not (len(unconnected) == len(open_units) == n):
            raise GenerationError("the layout does not give every component")
        connected = tuple(p - o for p, o in zip(counts, open_units, strict=True))
        delegated = tuple(o - u for o, u in zip(open_units, unconnected, strict=True))
        requires = tuple(c - s for c, s in zip(connected, solution.providers, strict=True))
        if min((*connected, *delegated, *requires, *unconnected), default=0) < 0:
            raise GenerationError("the layout's ports do not add up")
        reached_by_connectors = sum(connected)
        if delegations == 0:
            root = tuple(range(n))
        else:
            root = tuple(
                a for a in range(n) if connected[a] > 0 or (counts[a] == unconnected[a] and reached_by_connectors > 0)
            )
        members = set(root)
        deep = tuple(a for a in range(n) if a not in members)
        reach = sum(delegated)
        deep_delegated = sum(delegated[a] for a in deep)
        steps = min(max(0, delegations - reach), (compositions - 1) * deep_delegated)
        repeats = delegations - reach - steps
        connectors = solution_connectors(solution)
        if reach > delegations or not 0 <= repeats <= sum(solution.providers):
            raise GenerationError("the layout's delegations do not reproduce D")
        return cls(
            tuple(unconnected),
            connected,
            delegated,
            tuple(solution.providers),
            requires,
            tuple(solution.extras),
            root,
            deep,
            reach,
            steps,
            repeats,
            connectors,
            (solution.ordinary, solution.to_hubs, solution.from_hubs, solution.hub_to_hub),
            tuple((piece.node, piece.room, piece.edges) for piece in solution.pieces),
            source,
            step,
        )

    def solution(self, counts: Sequence[int], fan_outs: Sequence[int]) -> ConnectorSolution:
        """The construction this layout decides, for `connectors.build_connectors(..., solution=...)`."""
        return _laid(counts, fan_outs, self.open_units, self.providers, self.extras, self.pieces, self.blocks)


def _laid(
    counts: Sequence[int],
    fan_outs: Sequence[int],
    open_units: Sequence[int],
    providers: Sequence[int],
    extras: Sequence[int],
    pieces: Pieces,
    blocks: tuple[int, int, int, int],
) -> ConnectorSolution:
    """`connectors.star_solution` of these stars and blocks (C_oo, OH, HO, HH)."""
    ordinary, to_hubs, from_hubs, hub_to_hub = blocks
    return star_solution(
        counts,
        fan_outs,
        open_units,
        providers,
        extras,
        pieces,
        ordinary=ordinary,
        to_hubs=to_hubs,
        from_hubs=from_hubs,
        hub_to_hub=hub_to_hub,
    )


def check_layout(project: Project, layout: Layout) -> None:
    """Re-read the layout from a built project; a difference is a `GenerationError`.

    `project.components` must be in the layout's order. Compares the number of components, then per
    component its unconnected, connector-reached and delegation-only ports, which composition holds
    each component (the root or the deepest), the counts of each kind of delegation source and the
    number of connectors, naming the first that differs: the construction executes the layout, it
    never decides one of its own.

    What this does not reach: which ports the connectors join, which port carries which role, the
    stars' interfaces and the blocks; the connector leaves are `connectors.build_connectors`'
    post-check's, the stated leaves the self-check's.
    """
    names = [component.name for component in project.components]
    if len(names) != len(layout.unconnected):
        raise GenerationError("the built project differs from its layout (the components)")
    index = {name: a for a, name in enumerate(names)}
    ends = {(end.swc, end.port) for connector in project.connectors for end in (connector.source, connector.target)}
    deepest = len(project.compositions) - 1
    holders: dict[int, list[int]] = {}
    alone = [0] * len(names)
    reach = steps = repeats = 0
    for depth, composition in enumerate(project.compositions):
        types = {member.name: member.type for member in composition.members}
        for member in composition.members:
            if member.type is not None:
                holders.setdefault(index[member.type], []).append(depth)
        for delegation in composition.delegations:
            component = types.get(delegation.member)
            if component is None:
                steps += 1
            elif (component, delegation.member_port) in ends:
                repeats += 1
            else:
                reach += 1
                alone[index[component]] += 1
    connected = tuple(
        sum(1 for port in component.ports if (component.name, port.name) in ends) for component in project.components
    )
    unconnected = tuple(
        len(component.ports) - c - d for component, c, d in zip(project.components, connected, alone, strict=True)
    )
    root = set(layout.root)
    expected_holders = {a: [0 if a in root else deepest] for a in range(len(names))}
    differences = [
        what
        for what, built, decided in (
            ("the unconnected ports", unconnected, layout.unconnected),
            ("the connector-reached ports", connected, layout.connected),
            ("the delegation-only ports", tuple(alone), layout.delegated),
            ("the membership", holders, expected_holders),
            ("the sources", (reach, steps, repeats), (layout.reach, layout.steps, layout.repeats)),
            ("the connectors", len(project.connectors), layout.connectors),
        )
        if built != decided
    ]
    if differences:
        raise GenerationError(f"the built project differs from its layout ({differences[0]})")


@dataclass(frozen=True)
class Trace:
    """What the planner's steps answered for one configuration.

    `relaxed` is step 1's verdict; `builder` is the step-2 builder that built (`SOLVE_OPEN` or
    `FIRST_FIT`), `None` when neither did or step 2 was not reached; `exact` is step 3's verdict,
    `None` when step 3 was not reached.
    """

    relaxed: bool
    builder: str | None = None
    exact: bool | None = None


@dataclass(frozen=True)
class Planned:
    """The planner's answer: the layout (`None` when none exists), the trace and the solve tally."""

    layout: Layout | None
    trace: Trace
    tally: Tally = field(default_factory=Tally, compare=False)


def _realised(configuration: Configuration, found: Canonical, pieces: Pieces, step: int) -> Layout:
    """The layout of a canonical solution with its stars, blocks and pieces.

    On the first-fit path `star_solution` cannot refuse: the relaxed program holds L1-L4 and
    `first_fit` lays every star within its interface's room. A refusal there is a planner defect
    and a `GenerationError`, never a fallback to step 3.
    """
    providers, extras, blocks = found.starred()
    solution = _laid(
        configuration.counts,
        configuration.fan_outs,
        configuration.open_units(found.connected),
        providers,
        extras,
        pieces,
        blocks,
    )
    return Layout.of_solution(
        configuration.counts,
        found.unconnected,
        solution,
        configuration.delegations,
        configuration.compositions,
        source=SOLVER,
        step=step,
    )


def _answer(configuration: Configuration, layout: Layout | None, trace: Trace, tally: Tally) -> Planned:
    LOG.debug(
        "layout of %s: %s, %s; %d solves: %d feasible, %d certified no, %d uncertified no",
        configuration,
        "no layout" if layout is None else f"step {layout.step}",
        trace,
        tally.solves,
        tally.feasible,
        tally.certified,
        tally.uncertified,
    )
    return Planned(layout, trace, tally)


def plan(configuration: Configuration, choosable: Sequence[int] | None = None) -> Planned:
    """The planner's three steps on one configuration with D > 0, after the preferred candidate failed.

    Step 1, the relaxed program (infeasible: no layout); step 2, its canonical A and (c, u) built by
    `connectors.solve_open`, else continued to its stars and built by `first_fit`; step 3, the exact
    program (infeasible: no layout). With A unstated the count is the smallest feasible of
    `choosable` (every count up to the program's bound when `None`); with A stated `choosable` is
    not read. When step 3 starts, `NOTICE` is told. Every answer is logged (`LOG`).
    """
    tally = Tally()
    relaxed = relaxed_program(configuration)
    begun = _begin(relaxed, configuration, choosable, SolverOptions(), tally)
    if begun is None:
        return _answer(configuration, None, Trace(relaxed=False), tally)
    built = _step_two(configuration, *begun)
    if built is not None:
        layout, builder = built
        return _answer(configuration, layout, Trace(relaxed=True, builder=builder), tally)
    notice = NOTICE.get()
    if notice is not None:
        notice(EXACT_NOTICE)
    exact = canonical(exact_program(configuration), configuration, choosable, stars=True, tally=tally)
    if exact is None:
        return _answer(configuration, None, Trace(relaxed=True, exact=False), tally)
    if exact.pieces is None:
        raise GenerationError("the exact program's canonical layout carries no star placement")
    layout = _realised(configuration, exact, exact.pieces, EXACT_STEP)
    return _answer(configuration, layout, Trace(relaxed=True, exact=True), tally)


def _step_two(configuration: Configuration, sequence: _Sequence, witness: tuple[int, ...]) -> tuple[Layout, str] | None:
    """Step 2: the layout and its builder (`SOLVE_OPEN`, else `FIRST_FIT`), or `None` when neither builds."""
    found = _counts(sequence.program, witness)
    spec = ConnectorSpec(
        unconnected=configuration.unconnected,
        max_on_one=configuration.max_on_one,
        assembly=found.connectors,
        layer=None,
        links=configuration.links,
    )
    solution = solve_open(
        spec,
        configuration.counts,
        configuration.open_units(found.connected),
        configuration.fan_outs,
        FULL_SEARCH,
    )
    if solution is not None and solution_connectors(solution) == found.connectors:
        layout = Layout.of_solution(
            configuration.counts,
            found.unconnected,
            solution,
            configuration.delegations,
            configuration.compositions,
            source=SOLVER,
            step=BUILDER_STEP,
        )
        return layout, SOLVE_OPEN
    # A and (c, u) stay as they were fixed.
    stars = _with_stars(sequence, witness, configuration)
    providers, extras, _ = stars.starred()
    pieces = first_fit(configuration.fan_outs, providers, extras)
    if pieces is None:
        return None
    return _realised(configuration, stars, pieces, BUILDER_STEP), FIRST_FIT
