"""The integration tests' one start helper (`tests/oracle/generator_start.py`) and the scan that holds them to it.

The scan reads a test function as `integration` when its own decorators, its class's or its module's
`pytestmark` name the marker, and follows its calls into the same module's top-level helpers
(transitively). A module outside :data:`MODULE_STARTERS` may spell no `-m swcraft_specimens` start at all.

What the scan does not reach: in a module of :data:`MODULE_STARTERS`, a start built in a fixture an
integration case requests, a helper imported from another module, and a command spelled through a
variable. `tests/scenarios/run_scenarios.py` is scanned whole.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from tests.oracle import generator_start
from tests.oracle.generator_start import BIN_VARIABLE, START_LOG_VARIABLE, generator_command

TESTS = Path(__file__).parent

SCENARIO_RUNNER = TESTS / "scenarios" / "run_scenarios.py"

#: The modules whose fast tests start `python -m swcraft_specimens` on purpose (its `--help`,
#: `--version`, hash-seed determinism, the CLI's refusals), and the helper's own tests. Their
#: integration cases are still held to the helper.
MODULE_STARTERS = frozenset(
    {
        "test_cli.py",
        "test_generator_start.py",
        "test_tiny_model.py",
        "test_production_preset_golden.py",
        "test_toolchain_version.py",
    }
)


def test_unset_is_this_interpreters_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BIN_VARIABLE, raising=False)
    monkeypatch.delenv(START_LOG_VARIABLE, raising=False)
    assert generator_command() == [sys.executable, "-m", "swcraft_specimens"]
    assert generator_command(["/x/swcraft-specimens"]) == ["/x/swcraft-specimens"]


def test_the_variable_wins_over_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BIN_VARIABLE, "/archive/bin/swcraft-specimens")
    monkeypatch.delenv(START_LOG_VARIABLE, raising=False)
    assert generator_command() == ["/archive/bin/swcraft-specimens"]
    assert generator_command(["/x/swcraft-specimens"]) == ["/archive/bin/swcraft-specimens"]


def test_each_start_is_logged_with_the_test_and_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "starts.jsonl"
    monkeypatch.setenv(START_LOG_VARIABLE, str(log))
    monkeypatch.delenv(BIN_VARIABLE, raising=False)
    generator_command()
    monkeypatch.setenv(BIN_VARIABLE, "/archive/bin/swcraft-specimens")
    generator_command()
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["command"] for line in lines] == [
        [sys.executable, "-m", "swcraft_specimens"],
        ["/archive/bin/swcraft-specimens"],
    ]
    assert all("test_each_start_is_logged" in line["test"] for line in lines)


def test_nothing_is_logged_without_the_log_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(START_LOG_VARIABLE, raising=False)
    monkeypatch.chdir(tmp_path)
    generator_command()
    assert list(tmp_path.iterdir()) == []


def _marks_integration(expression: ast.expr) -> bool:
    """*expression* holds a `<...>.integration` attribute, called or not, or inside a list; a string does not count."""
    return any(isinstance(node, ast.Attribute) and node.attr == "integration" for node in ast.walk(expression))


def _names_integration(decorators: list[ast.expr]) -> bool:
    return any(_marks_integration(decorator) for decorator in decorators)


def _is_integration(function: ast.FunctionDef) -> bool:
    return _names_integration(function.decorator_list)


def _module_is_integration(tree: ast.Module) -> bool:
    """The module's `pytestmark` names `integration` (a mark, or a list holding one)."""
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets)
        and _marks_integration(node.value)
        for node in tree.body
    )


def _integration_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """Every test function the `integration` marker reaches: by its decorators, its class's, the module's."""
    marked_module = _module_is_integration(tree)
    found: list[ast.FunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (marked_module or _is_integration(node)):
            found.append(node)
        elif isinstance(node, ast.ClassDef):
            marked_class = marked_module or _names_integration(node.decorator_list)
            found.extend(
                member
                for member in node.body
                if isinstance(member, ast.FunctionDef) and (marked_class or _is_integration(member))
            )
    return found


def _reached(tree: ast.Module, roots: list[ast.FunctionDef]) -> list[ast.FunctionDef]:
    """*roots* and every top-level function of *tree* they call by name, transitively."""
    helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    reached: dict[int, ast.FunctionDef] = {id(root): root for root in roots}
    pending = list(roots)
    while pending:
        for node in ast.walk(pending.pop()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
                helper = helpers[node.func.id]
                if id(helper) not in reached:
                    reached[id(helper)] = helper
                    pending.append(helper)
    return list(reached.values())


def _starts_beside_the_helper(name: str, tree: ast.Module) -> list[int]:
    """The lines of *tree* that start the module itself where the helper is required."""
    if name not in MODULE_STARTERS:
        return _module_starts(tree)
    return sorted(
        {line for function in _reached(tree, _integration_functions(tree)) for line in _module_starts(function)}
    )


def _module_starts(function: ast.AST) -> list[int]:
    """The lines of *function* (or any tree) holding a sequence literal `..., "-m", "swcraft_specimens", ...`."""
    lines = []
    for node in ast.walk(function):
        if isinstance(node, ast.List | ast.Tuple):
            values = [element.value if isinstance(element, ast.Constant) else None for element in node.elts]
            pairs = zip(values, values[1:], strict=False)
            if any(pair == ("-m", "swcraft_specimens") for pair in pairs):
                lines.append(node.lineno)
    return lines


def test_no_integration_test_starts_the_module_itself() -> None:
    """Every integration case starts the generator through the helper, so `SWCRAFT_SPECIMENS_BIN` reaches it."""
    found: list[str] = []
    for path in sorted(TESTS.glob("test_*.py"), key=lambda candidate: candidate.as_posix()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(f"{path.name}:{line}" for line in _starts_beside_the_helper(path.name, tree))
    assert found == []


def test_every_module_starter_still_starts_the_module() -> None:
    """An allow-list entry whose module no longer spells the start is stale, and is removed."""
    stale = sorted(
        name for name in MODULE_STARTERS if not _module_starts(ast.parse((TESTS / name).read_text(encoding="utf-8")))
    )
    assert stale == []


def test_the_scenario_runner_spells_no_start_of_its_own() -> None:
    """One start site is the point of the helper: the runner's integration case passes the helper's command."""
    tree = ast.parse(SCENARIO_RUNNER.read_text(encoding="utf-8"))
    assert _module_starts(tree) == []


def test_the_scan_sees_a_planted_start() -> None:
    planted = ast.parse(
        "@pytest.mark.integration\ndef test_x():\n    run([PYTHON, '-m', 'swcraft_specimens', 'generate'])\n"
    )
    (function,) = [node for node in ast.walk(planted) if isinstance(node, ast.FunctionDef)]
    assert _is_integration(function) and _module_starts(function) == [3]


_PLANTED_START = "run([PYTHON, '-m', 'swcraft_specimens', 'generate'])"


@pytest.mark.parametrize(
    "source",
    [
        f"pytestmark = pytest.mark.integration\ndef test_x():\n    {_PLANTED_START}\n",
        f"pytestmark = [pytest.mark.slow, pytest.mark.integration]\ndef test_x():\n    {_PLANTED_START}\n",
        f"@pytest.mark.integration\nclass TestX:\n    def test_x(self):\n        {_PLANTED_START}\n",
        f"def _start():\n    {_PLANTED_START}\ndef _via():\n    _start()\n"
        "@pytest.mark.integration\ndef test_x():\n    _via()\n",
    ],
    ids=["module-mark", "module-mark-list", "class-mark", "helper-of-a-helper"],
)
def test_the_scan_sees_a_start_the_marker_reaches_indirectly(source: str) -> None:
    assert _starts_beside_the_helper("test_cli.py", ast.parse(source)) != []


def test_a_fast_start_is_allowed_only_in_a_module_starter() -> None:
    source = f"def test_x():\n    {_PLANTED_START}\n"
    assert _starts_beside_the_helper("test_cli.py", ast.parse(source)) == []
    assert _starts_beside_the_helper("test_new.py", ast.parse(source)) == [2]


def test_the_module_command_is_this_interpreters() -> None:
    assert generator_start.MODULE_COMMAND == (sys.executable, "-m", "swcraft_specimens")
