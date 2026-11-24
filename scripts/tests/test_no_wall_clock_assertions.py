"""No test asserts absolute wall-clock speed, and no scratch git repository starts background maintenance.

Two static rules over ``scripts/quality_ratchet.py``'s ``TEST_TREES``.

**The wall-clock rule.** Inside one function, an *elapsed-time expression* is a subtraction with a call to
``time.perf_counter``, ``time.monotonic`` or ``time.time`` (or an ``_ns`` form) as an operand; a subtraction
of two names both assigned from such a call; a name assigned from either; or the second target of a tuple
unpacking of a call to ``timed`` or ``_timed``. An ``assert`` ordering (``<``, ``<=``, ``>``, ``>=``) such an
expression is flagged unless the other side is a name or attribute ending in ``_HANG_SECONDS`` (a hang guard
sized against the hang) or ``_SPEC_SECONDS`` (a specification's figure, citing its clause). Each comparison
of a chain is judged alone.

**The scratch-git rule.** In a module that runs a maintenance-triggering git command (``commit``,
``merge``, ``rebase``, ``gc``, ``fetch``, ``am``, ``pull``, ``cherry-pick``, ``stash``, ``push``), every
``init``, ``clone`` or ``worktree`` command must be followed in the same function by a ``quiet_maintenance``
call, or carry ``NO_AUTO_MAINTENANCE``. A ``git``/``_git`` helper whose own body calls ``quiet_maintenance``
(``scratch_git.git``) satisfies the rule only for ``init`` with literal options, and only when the name
resolves to that helper by a top-level definition or ``from X import name``.

Both are static heuristics over literal shapes: a duration or argv built at run time, passed through a
helper or another module, and the extension's TypeScript tests are not reached.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from conftest import REPO_ROOT, quality_ratchet_module

#: The clock functions of ``time`` whose difference is an elapsed time.
CLOCKS = frozenset({"perf_counter", "monotonic", "time", "perf_counter_ns", "monotonic_ns", "time_ns"})

#: Helpers whose call returns ``(result, seconds)``.
TIMED_HELPERS = frozenset({"timed", "_timed"})

#: The suffixes of a named bound the wall-clock rule accepts.
BOUND_SUFFIXES = ("_HANG_SECONDS", "_SPEC_SECONDS")

#: The comparisons that bound a duration.
ORDERINGS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

#: The expressions of an assert that bind names of their own, whose comparisons the wall-clock rule does not read.
INNER_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

#: The git subcommands that start background maintenance when they finish (``push`` on its receiving side).
TRIGGERS = frozenset({"commit", "merge", "rebase", "gc", "fetch", "am", "pull", "cherry-pick", "stash", "push"})

#: The git subcommands that create a repository (or a worktree of one).
CREATORS = frozenset({"init", "clone", "worktree"})

#: The functions whose call is a git command by name.
GIT_HELPERS = frozenset({"git", "_git"})

#: The ``init`` option whose value is the git directory, not a work tree the quieting helper misses.
SEPARATE_GIT_DIR = "--separate-git-dir"

#: The call that quiets a scratch repository, and the arguments that quiet one command.
QUIET = "quiet_maintenance"
CARRIED = "NO_AUTO_MAINTENANCE"

#: A key of the quieting-helper index: the defining module's last dotted component and the function's name.
HelperKey = tuple[str, str]


@dataclass(frozen=True)
class Finding:
    """One flagged site: the file, the line and the rule (``wall-clock`` or ``scratch-git``)."""

    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def _name(node: ast.AST) -> str | None:
    """The identifier a ``Name`` or the last one an ``Attribute`` spells, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _called(node: ast.AST) -> str | None:
    """The name a call calls, by :func:`_name`."""
    return _name(node.func) if isinstance(node, ast.Call) else None


def _is_clock_call(node: ast.AST) -> bool:
    """``time.<clock>()`` for a clock of :data:`CLOCKS`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
        and node.func.attr in CLOCKS
    )


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node of *scope* outside its nested functions and lambdas, which are scopes of their own."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _scopes(tree: ast.Module) -> Iterator[ast.AST]:
    """The module and every function in it, nested ones and methods included."""
    yield tree
    yield from (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _in_order(nodes: Iterable[ast.AST]) -> list[ast.AST]:
    """*nodes* that carry a position, in source order."""
    placed = [node for node in nodes if hasattr(node, "lineno")]
    return sorted(placed, key=lambda node: (node.lineno, node.col_offset))


@dataclass
class _Clocks:
    """The names of one scope assigned from a clock call, and those holding an elapsed time, at one point."""

    clocks: set[str] = field(default_factory=set)
    elapsed: set[str] = field(default_factory=set)

    def subtraction(self, node: ast.AST) -> bool:
        """*node* is a subtraction against a clock call, or of two clock-assigned names."""
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
            return False
        if _is_clock_call(node.left) or _is_clock_call(node.right):
            return True
        return (
            isinstance(node.left, ast.Name)
            and isinstance(node.right, ast.Name)
            and {node.left.id, node.right.id} <= self.clocks
        )

    def is_elapsed(self, node: ast.AST) -> bool:
        """*node* is an elapsed-time expression."""
        return self.subtraction(node) or (isinstance(node, ast.Name) and node.id in self.elapsed)

    def forget(self, target: ast.AST) -> None:
        """Every name *target* binds no longer holds what it held."""
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                self.clocks.discard(node.id)
                self.elapsed.discard(node.id)

    def bind(self, target: ast.AST, value: ast.AST | None) -> None:
        """*target* is assigned *value*: a clock call, a subtraction, a ``timed`` unpacking, or anything else."""
        clock = value is not None and _is_clock_call(value)
        elapsed = value is not None and self.subtraction(value)
        self.forget(target)
        if isinstance(target, ast.Name):
            if clock:
                self.clocks.add(target.id)
            elif elapsed:
                self.elapsed.add(target.id)
        elif (
            isinstance(target, ast.Tuple)
            and len(target.elts) >= 2
            and isinstance(target.elts[1], ast.Name)
            and _called(value) in TIMED_HELPERS
        ):
            self.elapsed.add(target.elts[1].id)


def _is_named_bound(node: ast.AST) -> bool:
    """*node* is a name or attribute whose last identifier ends in a suffix of :data:`BOUND_SUFFIXES`."""
    name = _name(node)
    return name is not None and name.endswith(BOUND_SUFFIXES)


def _outside_inner_scopes(node: ast.AST) -> Iterator[ast.AST]:
    """*node* and its descendants, stopping at a lambda or a comprehension, which bind names of their own."""
    yield node
    if not isinstance(node, INNER_SCOPES):
        for child in ast.iter_child_nodes(node):
            yield from _outside_inner_scopes(child)


def _asserts_a_duration(test: ast.AST, state: _Clocks) -> bool:
    """*test* orders an elapsed-time expression against anything but a named bound."""
    for compare in (node for node in _outside_inner_scopes(test) if isinstance(node, ast.Compare)):
        operands = [compare.left, *compare.comparators]
        for index, operator in enumerate(compare.ops):
            if not isinstance(operator, ORDERINGS):
                continue
            left, right = operands[index], operands[index + 1]
            if (state.is_elapsed(left) and not _is_named_bound(right)) or (
                state.is_elapsed(right) and not _is_named_bound(left)
            ):
                return True
    return False


def _bindings(node: ast.AST) -> Iterator[tuple[ast.AST, ast.AST | None]]:
    """The ``(target, value)`` pairs *node* binds; ``None`` for a value the rule does not read."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield target, node.value
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        yield node.target, node.value
    elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
        yield node.target, None
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        yield node.optional_vars, None


def _wall_clock_lines(scope: ast.AST) -> Iterator[int]:
    """The lines of *scope*'s asserts that bound an elapsed time by anything but a named bound."""
    state = _Clocks()
    # A ``with`` item carries no position of its own: its targets are bound at the ``with`` statement's.
    for node in _in_order(_own_nodes(scope)):
        if isinstance(node, ast.Assert) and _asserts_a_duration(node.test, state):
            yield node.lineno
        for target, value in _bindings(node):
            state.bind(target, value)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                for target, value in _bindings(item):
                    state.bind(target, value)


def _subcommand(words: Iterable[ast.AST]) -> str | None:
    """The first string literal of *words* that is neither an option nor the value of ``-c``/``-C``."""
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if not (isinstance(word, ast.Constant) and isinstance(word.value, str)):
            continue
        if word.value in ("-c", "-C"):
            skip = True
        elif not word.value.startswith("-"):
            return word.value
    return None


def _git_subcommand(call: ast.Call) -> str | None:
    """The subcommand *call* runs, when it is a git command."""
    if _called(call) in GIT_HELPERS:
        return _subcommand(call.args)
    for argument in call.args:
        if (
            isinstance(argument, (ast.List, ast.Tuple))
            and argument.elts
            and isinstance(argument.elts[0], ast.Constant)
            and argument.elts[0].value == "git"
        ):
            return _subcommand(argument.elts[1:])
    return None


def _quiets(function: ast.AST) -> bool:
    """*function*'s own body calls ``quiet_maintenance``, outside any function or lambda it defines."""
    return any(_called(node) == QUIET for node in _own_nodes(function))


def helper_index(modules: Iterable[tuple[str, str]]) -> dict[HelperKey, bool]:
    """The ``git``/``_git`` helpers *modules* (``(module stem, source)`` pairs) define, each with whether it quiets.

    A key two modules define (two ``conftest.py`` files) quiets only when both definitions do.
    """
    index: dict[HelperKey, bool] = {}
    for stem, source in modules:
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in GIT_HELPERS:
                key = (stem, node.name)
                index[key] = index.get(key, True) and _quiets(node)
    return index


def _quieting_names(tree: ast.Module, helpers: Mapping[HelperKey, bool]) -> set[str]:
    """The names a call in *tree* can use to reach a helper that quiets inside itself.

    Only the module's top level binds such a name: a helper name a function defines or imports locally is
    judged unquieted throughout the module.
    """
    own = {
        node.name: _quiets(node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in GIT_HELPERS
    }
    names = {name for name, quiets in own.items() if quiets}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            stem = node.module.rsplit(".", 1)[-1]
            for alias in node.names:
                local = alias.asname or alias.name
                if local not in own and helpers.get((stem, alias.name), False):
                    names.add(local)
    top_level = set(tree.body)
    for node in ast.walk(tree):
        if node in top_level:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.discard(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.difference_update((alias.asname or alias.name).rsplit(".", 1)[-1] for alias in node.names)
    return names


def _inits_in_place(call: ast.Call) -> bool:
    """*call*'s first argument is ``"init"`` and the others are options: the form a quieting helper quiets."""
    first, *rest = call.args or [None]
    if not (isinstance(first, ast.Constant) and first.value == "init"):
        return False
    value_of_an_option = False
    for word in rest:
        if value_of_an_option:
            value_of_an_option = False
        elif isinstance(word, ast.Constant) and word.value == SEPARATE_GIT_DIR:
            value_of_an_option = True
        elif not (isinstance(word, ast.Constant) and isinstance(word.value, str) and word.value.startswith("-")):
            return False
    return True


def _scratch_git_lines(tree: ast.Module, helpers: Mapping[HelperKey, bool]) -> Iterator[int]:
    """The lines of *tree*'s repository creations that start no quieting, when the module triggers maintenance."""
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    if not any(_git_subcommand(call) in TRIGGERS for call in calls):
        return
    quieting = _quieting_names(tree, helpers)
    for scope in _scopes(tree):
        own = _in_order(node for node in _own_nodes(scope) if isinstance(node, ast.Call))
        for position, call in enumerate(own):
            if _git_subcommand(call) not in CREATORS:
                continue
            if isinstance(call.func, ast.Name) and call.func.id in quieting and _inits_in_place(call):
                continue
            if any(_name(node) == CARRIED for node in ast.walk(call)):
                continue
            if any(_called(later) == QUIET for later in own[position + 1 :]):
                continue
            yield call.lineno


def scan_source(source: str, path: str = "<memory>", helpers: Mapping[HelperKey, bool] | None = None) -> list[Finding]:
    """Both rules over one module's *source*; *helpers* is the quieting-helper index of :func:`helper_index`."""
    tree = ast.parse(source)
    lines = {(line, "wall-clock") for scope in _scopes(tree) for line in _wall_clock_lines(scope)}
    lines |= {(line, "scratch-git") for line in _scratch_git_lines(tree, helpers or {})}
    return [Finding(path, line, rule) for line, rule in sorted(lines)]


def python_files(roots: Iterable[str], base: Path = REPO_ROOT) -> list[Path]:
    """Every ``*.py`` file under *roots* (relative to *base*, the repository by default), in POSIX order."""
    return sorted(
        (path for root in roots for path in (base / root).rglob("*.py")),
        key=lambda path: path.relative_to(base).as_posix(),
    )


def scan_trees(roots: Iterable[str], helper_roots: Iterable[str], base: Path = REPO_ROOT) -> list[Finding]:
    """Both rules over every module under *roots*; quieting helpers are indexed over *roots* and *helper_roots*."""
    scanned = python_files(roots, base)
    sources = {path: path.read_text(encoding="utf-8") for path in {*scanned, *python_files(helper_roots, base)}}
    helpers = helper_index((path.stem, source) for path, source in sources.items())
    return [
        finding
        for path in scanned
        for finding in scan_source(sources[path], path.relative_to(base).as_posix(), helpers)
    ]


def open_test_trees() -> tuple[str, ...]:
    """The open test trees, as ``scripts/quality_ratchet.py`` lists them."""
    return tuple(quality_ratchet_module().TEST_TREES)


def scanned(source: str, helpers: Mapping[HelperKey, bool] | None = None) -> list[str]:
    """The rules :func:`scan_source` flags in *source*, dedented, in line order."""
    return [finding.rule for finding in scan_source(textwrap.dedent(source), helpers=helpers)]


class TestTheWallClockRuleFlags:
    """An ordering assert on an elapsed time, against anything but a named bound."""

    def test_a_literal_bound(self) -> None:
        assert scanned(
            """
            def test_fast():
                started = time.perf_counter()
                work()
                assert time.perf_counter() - started < 2.0
            """
        ) == ["wall-clock"]

    def test_a_bound_computed_in_place(self) -> None:
        assert scanned(
            """
            def test_fast():
                started = time.monotonic()
                work()
                assert time.monotonic() - started < bound + POLL_SECONDS + 1.0
            """
        ) == ["wall-clock"]

    def test_a_name_or_attribute_without_the_suffix(self) -> None:
        assert scanned(
            """
            def test_fast():
                started = time.monotonic()
                work()
                elapsed = time.monotonic() - started
                assert elapsed < LIMIT
                assert elapsed < module.TIMEOUT_SECONDS
            """
        ) == ["wall-clock", "wall-clock"]

    def test_a_timed_unpacking(self) -> None:
        assert scanned(
            """
            def test_fast():
                _, seconds = _timed(lambda: work())
                assert seconds < 2.0
            """
        ) == ["wall-clock"]

    def test_an_ns_clock(self) -> None:
        assert scanned(
            """
            def test_fast():
                started = time.perf_counter_ns()
                work()
                assert 2_000_000_000 > time.perf_counter_ns() - started
            """
        ) == ["wall-clock"]

    def test_the_difference_of_two_clock_names(self) -> None:
        assert scanned(
            """
            def test_fast():
                start = time.time()
                work()
                end = time.time()
                assert end - start <= 2
            """
        ) == ["wall-clock"]

    def test_each_comparison_of_a_chain_on_its_own(self) -> None:
        assert scanned(
            """
            def test_no_hang():
                started = time.monotonic()
                close()
                assert 0 <= time.monotonic() - started < CLOSE_HANG_SECONDS
            """
        ) == ["wall-clock"]

    def test_a_clause_of_a_conjunction(self) -> None:
        assert scanned(
            """
            def test_fast():
                allow, seconds = timed(lambda: decide())
                assert allow and seconds < 2.0
            """
        ) == ["wall-clock"]


class TestTheWallClockRuleIsSilent:
    """The named bounds, and the timings that bound nothing."""

    def test_a_hang_bound_by_name_and_by_attribute(self) -> None:
        assert (
            scanned(
                """
                def test_no_hang():
                    started = time.monotonic()
                    close()
                    assert time.monotonic() - started < CLOSE_HANG_SECONDS
                    assert time.monotonic() - started < module.CLOSE_HANG_SECONDS
                """
            )
            == []
        )

    def test_a_named_bound_on_the_left(self) -> None:
        assert (
            scanned(
                """
                def test_no_hang():
                    started = time.monotonic()
                    close()
                    assert CLOSE_HANG_SECONDS > time.monotonic() - started
                """
            )
            == []
        )

    def test_a_specification_bound(self) -> None:
        assert (
            scanned(
                """
                def test_large_preset():
                    started = time.perf_counter()
                    generate("large")
                    assert time.perf_counter() - started < LARGE_PRESET_SPEC_SECONDS
                """
            )
            == []
        )

    def test_a_deadline_passed_as_an_argument(self) -> None:
        assert (
            scanned(
                """
                def test_wait():
                    deadline = time.monotonic() + 30
                    assert wait_until(ready, deadline=deadline - time.monotonic())
                    assert process.wait(timeout=10) == 0
                """
            )
            == []
        )

    def test_a_printed_or_recorded_duration(self) -> None:
        assert (
            scanned(
                """
                def test_record():
                    started = time.perf_counter()
                    check()
                    elapsed = time.perf_counter() - started
                    print(f"took {elapsed:.2f} s")
                    record["seconds"] = elapsed
                """
            )
            == []
        )

    def test_a_file_age(self) -> None:
        assert (
            scanned(
                """
                def test_sweep(path):
                    an_hour_ago = time.time() - SWEEP_AGE_SECONDS - 60
                    os.utime(path, (an_hour_ago, an_hour_ago))
                    assert sweep() == [path]
                """
            )
            == []
        )


class TestTheAssignmentTracking:
    """A name counts from its assignment on, a reassignment ends it, a nested function is its own scope."""

    def test_a_use_before_the_assignment_is_not_elapsed(self) -> None:
        assert (
            scanned(
                """
                def test_order():
                    elapsed = 3
                    assert elapsed < 5
                    started = time.monotonic()
                    elapsed = time.monotonic() - started
                """
            )
            == []
        )

    def test_a_reassignment_ends_it(self) -> None:
        assert (
            scanned(
                """
                def test_rebound():
                    started = time.monotonic()
                    elapsed = time.monotonic() - started
                    elapsed = len(items)
                    assert elapsed < 5
                """
            )
            == []
        )

    def test_a_nested_function_is_a_scope_of_its_own(self) -> None:
        assert scanned(
            """
            def test_outer():
                started = time.monotonic()
                elapsed = time.monotonic() - started

                def inner(elapsed):
                    assert elapsed < 5

                assert elapsed < 5
            """
        ) == ["wall-clock"]


class TestWhatTheWallClockRuleDoesNotReach:
    """Each limit of the module docstring, unreached, beside the reached form it differs from."""

    def test_a_duration_returned_by_a_helper(self) -> None:
        returned = """
            def took():
                started = time.monotonic()
                work()
                return time.monotonic() - started

            def test_fast():
                assert took() < 2
        """
        inline = """
            def test_fast():
                started = time.monotonic()
                work()
                assert time.monotonic() - started < 2
        """
        assert (scanned(returned), scanned(inline)) == ([], ["wall-clock"])

    def test_a_duration_in_a_list_or_a_dictionary(self) -> None:
        kept = """
            def test_fast():
                started = time.monotonic()
                durations = [time.monotonic() - started]
                table = {"run": time.monotonic() - started}
                assert durations[0] < 2
                assert table["run"] < 2
        """
        named = """
            def test_fast():
                started = time.monotonic()
                duration = time.monotonic() - started
                assert duration < 2
        """
        assert (scanned(kept), scanned(named)) == ([], ["wall-clock"])

    def test_a_clock_read_through_another_module(self) -> None:
        scripted = """
            def test_send():
                returned = clock.monotonic()
                assert returned - written[0] < 1.0
        """
        direct = """
            def test_send():
                returned = time.monotonic()
                written = time.monotonic()
                assert returned - written < 1.0
        """
        assert (scanned(scripted), scanned(direct)) == ([], ["wall-clock"])

    def test_a_pytest_fail_guarded_by_a_duration(self) -> None:
        guarded = """
            def test_fast():
                started = time.monotonic()
                if time.monotonic() - started > 2:
                    pytest.fail("slow")
        """
        asserted = """
            def test_fast():
                started = time.monotonic()
                assert time.monotonic() - started <= 2
        """
        assert (scanned(guarded), scanned(asserted)) == ([], ["wall-clock"])

    def test_assert_less_and_an_equality(self) -> None:
        unreached = """
            def test_fast(self):
                started = time.monotonic()
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2)
                assert elapsed == 2
                assert elapsed != 2
        """
        ordered = """
            def test_fast(self):
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert elapsed >= 2
        """
        assert (scanned(unreached), scanned(ordered)) == ([], ["wall-clock"])

    def test_a_clock_imported_or_renamed(self) -> None:
        renamed = """
            from time import perf_counter

            def test_fast():
                clock = time.monotonic
                started = perf_counter()
                assert perf_counter() - started < 2
                begun = clock()
                assert clock() - begun < 2
        """
        spelled = """
            def test_fast():
                started = time.perf_counter()
                assert time.perf_counter() - started < 2
        """
        assert (scanned(renamed), scanned(spelled)) == ([], ["wall-clock"])

    def test_a_duration_transformed_or_aliased(self) -> None:
        transformed = """
            def test_fast():
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert elapsed * 1000 < 2000
                alias = elapsed
                assert alias < 2
        """
        plain = """
            def test_fast():
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert elapsed < 2
        """
        assert (scanned(transformed), scanned(plain)) == ([], ["wall-clock"])

    def test_a_duration_bound_inside_the_assert(self) -> None:
        inside = """
            def test_fast():
                started = time.monotonic()
                assert (elapsed := time.monotonic() - started) < 2
        """
        before = """
            def test_fast():
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert elapsed < 2
        """
        assert (scanned(inside), scanned(before)) == ([], ["wall-clock"])

    def test_a_helper_named_timed_is_trusted_by_its_name(self) -> None:
        named_timed = """
            def test_count():
                _, calls = timed(lambda: work())
                assert calls < 3
        """
        named_otherwise = """
            def test_count():
                _, seconds = measure(lambda: work())
                assert seconds < 3
        """
        assert (scanned(named_timed), scanned(named_otherwise)) == (["wall-clock"], [])

    def test_a_duration_a_later_branch_rebinds(self) -> None:
        rebound = """
            def test_fast(fast):
                started = time.monotonic()
                if fast:
                    elapsed = time.monotonic() - started
                else:
                    elapsed = 0
                assert elapsed < 2
        """
        one_branch = """
            def test_fast(fast):
                started = time.monotonic()
                if fast:
                    elapsed = time.monotonic() - started
                assert elapsed < 2
        """
        assert (scanned(rebound), scanned(one_branch)) == ([], ["wall-clock"])

    def test_a_comparison_inside_a_lambda_or_a_comprehension(self) -> None:
        inner = """
            def test_fast(samples):
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert (lambda elapsed: elapsed < 5)(3)
                assert all(elapsed < 5 for elapsed in samples)
                assert all(elapsed < 5 for _ in samples)
        """
        outer = """
            def test_fast(samples):
                started = time.monotonic()
                elapsed = time.monotonic() - started
                assert all(samples) and elapsed < 5
        """
        assert (scanned(inner), scanned(outer)) == ([], ["wall-clock"])

    def test_only_python_files_are_read(self, tmp_path: Path) -> None:
        body = "def test_fast():\n    started = time.monotonic()\n    assert time.monotonic() - started < 2\n"
        (tmp_path / "tree").mkdir()
        (tmp_path / "tree" / "test_speed.py").write_text(body, encoding="utf-8", newline="\n")
        (tmp_path / "tree" / "speed.test.ts").write_text(body, encoding="utf-8", newline="\n")
        assert [finding.path for finding in scan_trees(("tree",), (), tmp_path)] == ["tree/test_speed.py"]


class TestTheScratchGitRule:
    """A repository created in a module that triggers maintenance is quieted in the creating function."""

    def test_an_init_with_no_quieting_is_flagged(self) -> None:
        assert scanned(
            """
            def test_history(tmp_path):
                subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
                subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "x"], check=True)
            """
        ) == ["scratch-git"]

    def test_an_init_followed_by_quiet_maintenance_is_silent(self) -> None:
        assert (
            scanned(
                """
                def test_history(tmp_path):
                    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
                    quiet_maintenance(tmp_path)
                    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "x"], check=True)
                """
            )
            == []
        )

    def test_quieting_before_the_init_does_not_count(self) -> None:
        assert scanned(
            """
            def test_history(tmp_path):
                quiet_maintenance(tmp_path)
                _git(tmp_path, "init", "-q")
                _git(tmp_path, "commit", "-m", "x")
            """
        ) == ["scratch-git"]

    def test_a_worktree_command_carrying_the_arguments_is_silent(self) -> None:
        assert (
            scanned(
                """
                def worktree(path):
                    subprocess.run(["git", *NO_AUTO_MAINTENANCE, "worktree", "add", str(path), "HEAD"])
                    subprocess.run(["git", "fetch"])
                """
            )
            == []
        )

    def test_a_module_that_triggers_no_maintenance_is_outside_the_rule(self) -> None:
        assert (
            scanned(
                """
                def test_tracked(tmp_path):
                    _git(tmp_path, "init", "-q")
                    _git(tmp_path, "add", ".")
                """
            )
            == []
        )

    def test_an_option_value_is_not_the_subcommand(self) -> None:
        assert scanned(
            """
            def test_history(tmp_path):
                subprocess.run(["git", "-c", "init.defaultBranch=main", "-C", "init", "init"])
                subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", "init"])
            """
        ) == ["scratch-git"]

    def test_a_helper_that_quiets_inside_itself_exempts_its_callers(self) -> None:
        own = """
            def _git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd)
                if args[:1] == ("init",):
                    quiet_maintenance(cwd)

            def test_history(tmp_path):
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        imported = """
            from conftest import _git

            def test_history(tmp_path):
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        helpers = helper_index([("conftest", textwrap.dedent(own))])
        assert (scanned(own), scanned(imported, helpers), scanned(imported)) == ([], [], ["scratch-git"])

    def test_a_push_triggers_the_rule(self) -> None:
        assert scanned(
            """
            def test_publish(tmp_path):
                subprocess.run(["git", "init", "--bare", str(tmp_path / "remote.git")])
                subprocess.run(["git", "-C", str(tmp_path / "work"), "push", "origin", "main"])
            """
        ) == ["scratch-git"]

    def test_a_quieting_helper_exempts_only_an_init_in_its_own_directory(self) -> None:
        helper = """
            def _git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd)
                if args[:1] == ("init",):
                    quiet_maintenance(cwd)
        """
        in_place = """
            def test_history(tmp_path, git_directory):
                _git("init", "-q", cwd=tmp_path)
                _git("init", "--separate-git-dir", str(git_directory), cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        elsewhere = """
            def test_history(tmp_path, other):
                _git("clone", str(tmp_path), str(other), cwd=tmp_path)
                _git("worktree", "add", str(other), cwd=tmp_path)
                _git("init", "-q", str(other), cwd=tmp_path)
                _git("-c", "init.defaultBranch=main", "init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        helpers = helper_index([("conftest", textwrap.dedent(helper))])
        imported = "from conftest import _git\n"
        assert scanned(imported + textwrap.dedent(in_place), helpers) == []
        assert scanned(imported + textwrap.dedent(elsewhere), helpers) == ["scratch-git"] * 4

    def test_a_helper_two_modules_define_quiets_only_when_both_do(self) -> None:
        quieting = "def _git(*args, cwd):\n    quiet_maintenance(cwd)\n"
        loud = "def _git(*args, cwd):\n    subprocess.run(['git', *args], cwd=cwd)\n"
        assert helper_index([("conftest", quieting), ("conftest", loud)]) == {("conftest", "_git"): False}
        assert helper_index([("conftest", quieting), ("conftest", quieting)]) == {("conftest", "_git"): True}

    def test_a_helper_that_does_not_quiet_exempts_nothing(self) -> None:
        assert scanned(
            """
            def _git(project, *args):
                subprocess.run(["git", "-C", str(project), *args])

            def test_history(tmp_path):
                _git(tmp_path, "init", "-q")
                _git(tmp_path, "commit", "-m", "x")
            """
        ) == ["scratch-git"]


class TestWhatTheScratchGitRuleDoesNotReach:
    """The limits of the module docstring, unreached, beside the reached form."""

    def test_a_starred_or_variable_argv(self) -> None:
        built = """
            def test_history(tmp_path):
                subprocess.run([*GIT, "init", str(tmp_path)])
                for args in (["init"], ["commit", "-m", "x"]):
                    subprocess.run(["git", *args])
                argv = ["git", "init"]
                subprocess.run(argv)
                subprocess.run(["git", "commit", "-m", "x"])
        """
        literal = """
            def test_history(tmp_path):
                subprocess.run(["git", "init", str(tmp_path)])
                subprocess.run(["git", "commit", "-m", "x"])
        """
        assert (scanned(built), scanned(literal)) == ([], ["scratch-git"])

    def test_a_repository_created_by_product_code(self) -> None:
        product = """
            def test_bench(tmp_path):
                agent_task_run._commit_bench(tmp_path)
                subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "x"])
        """
        spelled = """
            def test_bench(tmp_path):
                subprocess.run(["git", "init", str(tmp_path)])
                subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "x"])
        """
        assert (scanned(product), scanned(spelled)) == ([], ["scratch-git"])

    def test_a_quieting_call_elsewhere_in_the_function_names_any_repository(self) -> None:
        other = """
            def test_two(tmp_path):
                _git(tmp_path / "a", "init")
                quiet_maintenance(tmp_path / "b")
                _git(tmp_path / "a", "commit", "-m", "x")
        """
        none = """
            def test_two(tmp_path):
                _git(tmp_path / "a", "init")
                _git(tmp_path / "a", "commit", "-m", "x")
        """
        assert (scanned(other), scanned(none)) == ([], ["scratch-git"])

    def test_a_repository_created_in_one_function_and_committed_in_another(self) -> None:
        apart = """
            def make_repo(tmp_path):
                subprocess.run(["git", "init", str(tmp_path)])

            def test_history(tmp_path):
                make_repo(tmp_path)
                quiet_maintenance(tmp_path)
                subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "x"])
        """
        together = """
            def make_repo(tmp_path):
                subprocess.run(["git", "init", str(tmp_path)])
                quiet_maintenance(tmp_path)

            def test_history(tmp_path):
                make_repo(tmp_path)
                subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "x"])
        """
        assert (scanned(apart), scanned(together)) == (["scratch-git"], [])

    def test_a_quieting_helper_reached_as_an_attribute(self) -> None:
        helper = """
            def _git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd)
                if args[:1] == ("init",):
                    quiet_maintenance(cwd)
        """
        body = """
            def test_history(tmp_path):
                {call}("init", cwd=tmp_path)
                {call}("commit", "-m", "x", cwd=tmp_path)
        """
        helpers = helper_index([("conftest", textwrap.dedent(helper))])
        attribute = "import conftest\n" + textwrap.dedent(body).format(call="conftest._git")
        imported = "from conftest import _git\n" + textwrap.dedent(body).format(call="_git")
        assert (scanned(attribute, helpers), scanned(imported, helpers)) == (["scratch-git"], [])

    def test_a_quieting_helper_imported_inside_a_function(self) -> None:
        helper = """
            def _git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd)
                if args[:1] == ("init",):
                    quiet_maintenance(cwd)
        """
        local = """
            def test_quieted(tmp_path):
                from conftest import _git
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        top_level = """
            from conftest import _git

            def test_quieted(tmp_path):
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        helpers = helper_index([("conftest", textwrap.dedent(helper))])
        assert (scanned(local, helpers), scanned(top_level, helpers)) == (["scratch-git"], [])

    def test_a_helper_name_a_function_rebinds_loses_the_exemption_everywhere(self) -> None:
        shadowed = """
            from conftest import _git

            def test_quieted(tmp_path):
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)

            def test_loud(tmp_path):
                def _git(*args, cwd):
                    subprocess.run(["git", *args], cwd=cwd)
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        helper = "def _git(*args, cwd):\n    quiet_maintenance(cwd)\n"
        helpers = helper_index([("conftest", helper)])
        assert scanned(shadowed, helpers) == ["scratch-git"] * 2

    def test_a_helper_that_quiets_only_through_a_function_it_defines(self) -> None:
        body = """
            def _git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd)
                {quieting}

            def test_history(tmp_path):
                _git("init", cwd=tmp_path)
                _git("commit", "-m", "x", cwd=tmp_path)
        """
        callback = "def later():\n                    quiet_maintenance(cwd)"
        direct = "quiet_maintenance(cwd)"
        assert (scanned(body.format(quieting=callback)), scanned(body.format(quieting=direct))) == (
            ["scratch-git"],
            [],
        )


class TestTheTrees:
    """The scan over the open trees: clean, and reading what it claims to read."""

    def test_the_trees_are_read(self) -> None:
        trees = open_test_trees()
        assert {"scripts/tests", "packages/tools/tests"} <= set(trees)
        assert len(python_files(trees)) > 100, "the scan reads too few files; the tree list or the glob stopped working"

    def test_the_shared_git_helper_is_a_quieting_helper(self) -> None:
        shared = REPO_ROOT / "packages" / "tools" / "tests" / "support" / "scratch_git.py"
        assert helper_index([("scratch_git", shared.read_text(encoding="utf-8"))]) == {("scratch_git", "git"): True}

    def test_no_open_test_asserts_wall_clock_speed_or_leaves_a_repository_loud(self) -> None:
        findings = scan_trees(open_test_trees(), ())
        assert not findings, (
            "a test bounds an elapsed time by an unnamed figure, or creates a scratch repository that can start "
            "background maintenance:\n" + "\n".join(map(str, findings))
        )
