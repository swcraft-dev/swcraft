"""``scripts/quality_ratchet.py``: the ratchets ``lint`` runs, over planted trees.

Each metric is shown failing on growth, passing on a decrease, lowered by ``update`` and never
raised by it; the zero check and the test-rule scanner are shown firing and staying silent where
the script's docstring says they do, its documented blind spots included; the mypy reader is shown
counting, filtering to the package's own files and refusing a run that did not finish.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import REPO_ROOT, quality_ratchet_module

_Q = quality_ratchet_module()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
    return path


def _measurement(**overrides):
    """A measurement; a metric of ``LIMITS`` given by name goes into ``limited``."""
    limited = {metric: overrides.pop(metric, {}) for metric in _Q.LIMITS}
    values = {
        "mypy": {"swcraft_tools": 10, "swcraft_mcp": 2, "swcraft_agent": 2},
        "line_citations": [],
        "test_rule": {},
        "private_crossings": {},
    }
    values.update(overrides)
    return _Q.Measurement(limited=limited, **values)


def _baseline(**overrides):
    baseline = _Q.initial_baseline(_measurement())
    for key, value in overrides.items():
        if key == "errors":
            baseline["mypy"]["errors"] = value
        elif key == "allowlist":
            baseline["test_rule"]["allowlist"] = value
        else:
            baseline[key] = value
    return baseline


# ---------------------------------------------------------------------------
# Size and complexity, measured
# ---------------------------------------------------------------------------


def _long_function(name: str, lines: int) -> str:
    body = "".join(f"    x{index} = {index}\n" for index in range(lines - 2))
    return f"def {name}():\n{body}    return 0\n"


def _complex_function(name: str, branches: int) -> str:
    body = "".join(f"    if value == {index}:\n        return {index}\n" for index in range(branches))
    return f"def {name}(value):\n{body}    return -1\n"


class TestTheSourcesAreMeasured:
    def test_a_package_module_is_keyed_by_import_name_and_a_script_by_path(self, tmp_path):
        _write(tmp_path / "packages/tools/src/swcraft_tools/sub/__init__.py", _long_function("f", 5))
        _write(
            tmp_path / "packages/tools/src/swcraft_tools/sub/mod.py", "class C:\n    def m(self):\n        return 1\n"
        )
        _write(tmp_path / "scripts/tool.py", _long_function("main", 3))
        _write(tmp_path / "scripts/tests/test_ignored.py", _long_function("test_x", 400))
        measured = _Q.measure_sources(tmp_path)
        assert measured["function_length"] == {
            "scripts/tool.py::main": 3,
            "swcraft_tools.sub::f": 5,
            "swcraft_tools.sub.mod::C.m": 2,
        }
        assert measured["module_lines"] == {"scripts/tool.py": 3, "swcraft_tools.sub": 5, "swcraft_tools.sub.mod": 3}

    def test_a_function_spans_its_closures_and_a_closure_is_an_entry_of_its_own(self, tmp_path):
        inner = textwrap.indent(_long_function("inner", 10), "    ")
        _write(tmp_path / "packages/tools/src/swcraft_tools/mod.py", f"def outer():\n{inner}    return inner\n")
        measured = _Q.measure_sources(tmp_path)
        assert measured["function_length"] == {
            "swcraft_tools.mod::outer": 12,
            "swcraft_tools.mod::outer.<locals>.inner": 10,
        }

    def test_a_trailing_closure_is_measured(self, tmp_path):
        # radon ends a function at its last statement that is not a nested definition, and never
        # adds a closure's complexity to its enclosing function's
        handler = textwrap.indent("@app.route\n" + _complex_function("handler", 31), "    ")
        method = textwrap.indent(f"def m(self, app):\n{handler}", "    ")
        _write(
            tmp_path / "packages/tools/src/swcraft_tools/mod.py", f"def register(app):\n{handler}\nclass C:\n{method}"
        )
        measured = _Q.measure_sources(tmp_path)
        assert measured["function_length"] == {
            "swcraft_tools.mod::register": 66,
            "swcraft_tools.mod::register.<locals>.handler": 64,
            "swcraft_tools.mod::C.m": 66,
            "swcraft_tools.mod::C.m.<locals>.handler": 64,
        }
        assert measured["complexity"] == {
            "swcraft_tools.mod::register": 1,
            "swcraft_tools.mod::register.<locals>.handler": 32,
            "swcraft_tools.mod::C.m": 1,
            "swcraft_tools.mod::C.m.<locals>.handler": 32,
        }

    def test_a_method_of_a_local_class_is_measured_and_the_function_spans_it(self, tmp_path):
        # radon drops a class defined inside a function: neither its methods nor its lines reach
        # the enclosing function
        method = textwrap.indent(_complex_function("m", 31).replace("(value)", "(self, value)"), "        ")
        _write(tmp_path / "packages/tools/src/swcraft_tools/mod.py", f"def make():\n    class Local:\n{method}")
        measured = _Q.measure_sources(tmp_path)
        assert measured["function_length"] == {
            "swcraft_tools.mod::make": 66,
            "swcraft_tools.mod::make.<locals>.Local.m": 64,
        }
        assert measured["complexity"] == {
            "swcraft_tools.mod::make": 1,
            "swcraft_tools.mod::make.<locals>.Local.m": 32,
        }

    def test_a_method_of_a_nested_class_is_measured_under_its_qualname(self, tmp_path):
        inner = textwrap.indent(_complex_function("deep", 31).replace("(value)", "(self, value)"), "            ")
        source = f"class Outer:\n    class Inner:\n        class Innermost:\n{inner}"
        _write(tmp_path / "packages/tools/src/swcraft_tools/mod.py", source)
        measured = _Q.measure_sources(tmp_path)
        assert measured["complexity"] == {"swcraft_tools.mod::Outer.Inner.Innermost.deep": 32}
        assert measured["function_length"] == {"swcraft_tools.mod::Outer.Inner.Innermost.deep": 64}

    def test_complexity_is_radons(self, tmp_path):
        _write(tmp_path / "packages/agent/src/swcraft_agent/mod.py", _complex_function("branchy", 31))
        assert _Q.measure_sources(tmp_path)["complexity"] == {"swcraft_agent.mod::branchy": 32}


# ---------------------------------------------------------------------------
# The ratchet rule, per metric
# ---------------------------------------------------------------------------


_LIMITED = [
    ("function_length", "swcraft_tools.mod::f", 250),
    ("complexity", "swcraft_tools.mod::f", 40),
    ("module_lines", "swcraft_tools.mod", 3500),
    ("test_file_lines", "packages/tools/tests/test_big.py", 3500),
    ("docstring_lines", "swcraft_tools.mod::f", 40),
]


@pytest.mark.parametrize(("metric", "key", "value"), _LIMITED)
class TestEachLimitedMetric:
    def test_growth_fails_and_names_the_key_the_baseline_and_the_new_value(self, metric, key, value):
        verdict = _Q.compare(_baseline(**{metric: {key: value}}), _measurement(**{metric: {key: value + 1}}))
        assert verdict.growth == [f"{metric} {key}: grew from the baseline's {value} to {value + 1}"]

    def test_a_decrease_passes_and_update_lowers_it(self, metric, key, value):
        baseline = _baseline(**{metric: {key: value}})
        measurement = _measurement(**{metric: {key: value - 1}})
        verdict = _Q.compare(baseline, measurement)
        assert (verdict.growth, verdict.stale) == ([], [])
        assert _Q.lowered_baseline(baseline, measurement)[metric] == {key: value - 1}

    def test_update_never_raises_a_value(self, metric, key, value):
        baseline = _baseline(**{metric: {key: value}})
        assert _Q.lowered_baseline(baseline, _measurement(**{metric: {key: value + 5}}))[metric] == {key: value}

    def test_an_unlisted_entry_may_not_cross_the_limit(self, metric, key, value):
        limit = _Q.LIMITS[metric]
        assert _Q.compare(_baseline(), _measurement(**{metric: {key: limit}})).growth == []
        growth = _Q.compare(_baseline(), _measurement(**{metric: {key: limit + 1}})).growth
        assert growth == [f"{metric} {key}: new entry, baseline none, now {limit + 1} over the limit {limit}"]

    def test_a_stale_entry_fails_check_and_update_drops_it(self, metric, key, value):
        limit = _Q.LIMITS[metric]
        baseline = _baseline(**{metric: {key: value}})
        for measured in ({}, {key: limit}):
            measurement = _measurement(**{metric: measured})
            verdict = _Q.compare(baseline, measurement)
            assert len(verdict.stale) == 1 and key in verdict.stale[0] and str(value) in verdict.stale[0]
            assert _Q.lowered_baseline(baseline, measurement)[metric] == {}


def _lines(count: int) -> str:
    return "".join(f"x{index} = {index}\n" for index in range(count))


def _docstring(lines: int, indent: str = "    ") -> str:
    """A docstring of *lines* physical lines, indented for a body at *indent*."""
    inner = "".join(f"{indent}line {index}\n" for index in range(lines - 2))
    return f'{indent}"""Summary.\n{inner}{indent}"""\n'


class TestTheDocstrings:
    """The docstring ratchet: what it measures, how it keys an entry, which trees it reads."""

    def test_a_module_a_class_and_a_function_docstring_are_measured_and_an_attribute_string_is_not(
        self, tmp_path: Path
    ) -> None:
        source = (
            '"""Module."""\n'
            "class C:\n" + _docstring(3) + "    x = 1\n" + '    """An attribute string."""\n'
            "    def m(self):\n" + _docstring(4, " " * 8) + "        return 1\n"
            "def f():\n" + _docstring(5) + "    return 0\n"
            "def g():\n    return 0\n"
        )
        _write(tmp_path / "packages/tools/src/swcraft_tools/mod.py", source)
        assert _Q.measure_docstrings(tmp_path) == {
            "swcraft_tools.mod::": 1,
            "swcraft_tools.mod::C": 3,
            "swcraft_tools.mod::C.m": 4,
            "swcraft_tools.mod::f": 5,
        }

    def test_a_test_tree_is_keyed_by_its_path_and_the_closed_tree_is_not_measured(self, tmp_path: Path) -> None:
        body = "def test_x():\n" + _docstring(3) + "    assert True\n"
        _write(tmp_path / "packages/tools/tests/test_a.py", body)
        _write(tmp_path / "scripts/tests/test_b.py", body)
        for tree in _Q.CLOSED_TEST_TREES:
            _write(tmp_path / tree / "test_c.py", body)
        _write(tmp_path / "packages/specimens/tests/scenarios/runner.py", body)
        _write(tmp_path / "vscode_extension/src/core.test.ts", "a;\n")
        assert _Q.measure_docstrings(tmp_path) == {
            "packages/specimens/tests/scenarios/runner.py::test_x": 3,
            "packages/tools/tests/test_a.py::test_x": 3,
            "scripts/tests/test_b.py::test_x": 3,
        }

    def test_the_docstring_key_and_the_function_length_key_are_built_alike(self, tmp_path: Path) -> None:
        source = """
            def outer():
                '''Outer.'''
                def inner():
                    '''Inner.'''
                    return 1
                class Local:
                    '''Local.'''
                    def method(self):
                        '''Method.'''
                        return 2
                return inner, Local
            class Top:
                '''Top.'''
                class Nested:
                    '''Nested.'''
                    async def deep(self):
                        '''Deep.'''
                        return 3
            if True:
                def conditional():
                    '''Conditional.'''
                    return 4
            """
        _write(tmp_path / "scripts/tool.py", source)
        functions = set(_Q.measure_sources(tmp_path)["function_length"])
        docstrings = set(_Q.measure_docstrings(tmp_path))
        classes = {"scripts/tool.py::outer.<locals>.Local", "scripts/tool.py::Top", "scripts/tool.py::Top.Nested"}
        assert docstrings - classes == functions
        assert classes <= docstrings

    def test_two_definitions_of_one_qualname_keep_the_larger(self, tmp_path: Path) -> None:
        source = "def f():\n" + _docstring(3) + "    return 1\n" + "def f():\n" + _docstring(6) + "    return 2\n"
        _write(tmp_path / "scripts/tool.py", source)
        assert _Q.measure_docstrings(tmp_path) == {"scripts/tool.py::f": 6}

    def test_a_planted_long_docstring_fails_check_and_a_cut_one_is_lowered_by_update(self, tmp_path: Path) -> None:
        module = tmp_path / "packages/tools/src/swcraft_tools/mod.py"
        _write(module, "def f():\n" + _docstring(35) + "    return 0\n")
        (tmp_path / "scripts").mkdir()

        def run(command: list[str]) -> int:
            return _Q.run(
                [*command, "--repo-root", str(tmp_path)],
                measure_tree=lambda root: _measurement(docstring_lines=_Q.measure_docstrings(root)),
            )

        def listed() -> dict[str, int]:
            return _Q.read_baseline(tmp_path / _Q.BASELINE)["docstring_lines"]

        assert run(["update", "--initial"]) == 0
        assert listed() == {"swcraft_tools.mod::f": 35}
        _write(module, "def f():\n" + _docstring(36) + "    return 0\n")
        assert run(["check"]) == 1
        _write(module, "def f():\n" + _docstring(32) + "    return 0\n")
        assert run(["check"]) == 0
        assert run(["update"]) == 0
        assert listed() == {"swcraft_tools.mod::f": 32}
        _write(tmp_path / "scripts/tool.py", "def g():\n" + _docstring(31) + "    return 0\n")
        assert run(["check"]) == 1


class TestTheTestFiles:
    """The test-file ratchet, measured over planted trees and run through the command line."""

    def test_every_test_tree_is_measured_and_keyed_by_its_path(self, tmp_path):
        _write(tmp_path / "packages/tools/tests/test_a.py", _lines(4))
        _write(tmp_path / "packages/tools/tests/support/helper.py", _lines(2))
        _write(tmp_path / "packages/newpkg/tests/conftest.py", _lines(1))
        _write(tmp_path / "scripts/tests/test_b.py", _lines(3))
        _write(tmp_path / "internal/tests/test_c.py", _lines(5))
        _write(tmp_path / "vscode_extension/src/core.test.ts", "a;\nb;\n")
        _write(tmp_path / "vscode_extension/src/core.ts", _lines(9))
        _write(tmp_path / "packages/tools/tests/__pycache__/test_a.py", _lines(9))
        _write(tmp_path / "packages/specimens/tests/scenarios/runner.py", _lines(9))
        assert _Q.measure_test_files(tmp_path) == {
            "internal/tests/test_c.py": 5,
            "packages/newpkg/tests/conftest.py": 1,
            "packages/tools/tests/support/helper.py": 2,
            "packages/tools/tests/test_a.py": 4,
            "scripts/tests/test_b.py": 3,
            "vscode_extension/src/core.test.ts": 2,
        }
        assert _Q.measure_sources(tmp_path)["module_lines"] == {"packages/specimens/tests/scenarios/runner.py": 9}

    @staticmethod
    def _run(tmp_path, command):
        (tmp_path / "scripts").mkdir(exist_ok=True)
        return _Q.run(
            [*command, "--repo-root", str(tmp_path)],
            measure_tree=lambda root: _measurement(test_file_lines=_Q.measure_test_files(root)),
        )

    @staticmethod
    def _listed(tmp_path):
        return json.loads((tmp_path / _Q.BASELINE).read_text(encoding="utf-8"))["test_file_lines"]

    def test_a_planted_growth_fails_check_a_shrink_passes_and_update_lowers(self, tmp_path):
        listed = _write(tmp_path / "packages/tools/tests/test_big.py", _lines(3005))
        assert self._run(tmp_path, ["update", "--initial"]) == 0
        assert self._listed(tmp_path) == {"packages/tools/tests/test_big.py": 3005}
        _write(listed, _lines(3006))
        assert self._run(tmp_path, ["check"]) == 1
        assert self._run(tmp_path, ["update"]) == 1
        assert self._listed(tmp_path) == {"packages/tools/tests/test_big.py": 3005}
        _write(listed, _lines(3002))
        assert self._run(tmp_path, ["check"]) == 0
        assert self._run(tmp_path, ["update"]) == 0
        assert self._listed(tmp_path) == {"packages/tools/tests/test_big.py": 3002}
        _write(listed, _lines(3000))
        assert self._run(tmp_path, ["check"]) == 1
        assert self._run(tmp_path, ["update"]) == 0
        assert self._listed(tmp_path) == {}

    def test_a_planted_new_file_over_the_limit_fails_check_and_update_refuses_it(self, tmp_path):
        assert self._run(tmp_path, ["update", "--initial"]) == 0
        _write(tmp_path / "vscode_extension/src/fresh.test.ts", _lines(3000))
        assert self._run(tmp_path, ["check"]) == 0
        _write(tmp_path / "vscode_extension/src/fresh.test.ts", _lines(3001))
        assert self._run(tmp_path, ["check"]) == 1
        assert self._run(tmp_path, ["update"]) == 1
        assert self._listed(tmp_path) == {}

    def test_an_entry_of_an_absent_closed_tree_is_kept_unmeasured(self):
        key = "internal/tests/test_closed.py"
        baseline = _baseline(test_file_lines={key: 3200, "scripts/tests/test_gone.py": 3100})
        measurement = _measurement(absent_trees=("internal/tests",))
        verdict = _Q.compare(baseline, measurement)
        assert verdict.growth == [] and len(verdict.stale) == 1 and "test_gone" in verdict.stale[0]
        assert _Q.lowered_baseline(baseline, measurement)["test_file_lines"] == {key: 3200}
        assert len(_Q.compare(baseline, _measurement()).stale) == 2

    @pytest.mark.parametrize("held", [True, False])
    def test_measure_names_a_closed_tree_absent_exactly_where_the_checkout_lacks_it(self, tmp_path, held):
        if held:
            _write(tmp_path / "internal/tests/test_closed.py", _lines(1))
        measurement = _Q.measure(
            tmp_path,
            mypy_runner=lambda root, package: (0, "Success: no issues found in 1 source file\n"),
            citation_files=lambda root: ([], set()),
        )
        assert measurement.absent_trees == (() if held else ("internal/tests",))


def test_a_rename_over_the_limit_fails_as_a_new_entry_and_names_the_vanished_key():
    baseline = _baseline(function_length={"swcraft_tools.mod::old_name": 300})
    verdict = _Q.compare(baseline, _measurement(function_length={"swcraft_tools.mod::new_name": 300}))
    assert len(verdict.growth) == 1
    assert "swcraft_tools.mod::new_name: new entry" in verdict.growth[0]
    assert "swcraft_tools.mod::old_name vanished" in verdict.growth[0]
    assert any("swcraft_tools.mod::old_name" in item for item in verdict.stale)


class TestTheMypyCount:
    def test_growth_fails_a_decrease_passes_and_update_lowers_but_never_raises(self):
        baseline = _baseline(errors={"swcraft_tools": 10, "swcraft_mcp": 2, "swcraft_agent": 2})
        grown = _measurement(mypy={"swcraft_tools": 11, "swcraft_mcp": 2, "swcraft_agent": 2})
        assert _Q.compare(baseline, grown).growth == ["mypy swcraft_tools: grew from the baseline's 10 errors to 11"]
        assert _Q.lowered_baseline(baseline, grown)["mypy"]["errors"]["swcraft_tools"] == 10
        fewer = _measurement(mypy={"swcraft_tools": 9, "swcraft_mcp": 2, "swcraft_agent": 1})
        assert _Q.compare(baseline, fewer).growth == []
        assert _Q.lowered_baseline(baseline, fewer)["mypy"]["errors"] == {
            "swcraft_tools": 9,
            "swcraft_mcp": 2,
            "swcraft_agent": 1,
        }

    def test_a_baseline_taken_with_other_arguments_is_refused(self):
        baseline = _baseline()
        baseline["mypy"]["arguments"] = ["--strict"]
        with pytest.raises(_Q.RatchetError, match="not comparable"):
            _Q.compare(baseline, _measurement())

    def test_the_run_pins_the_package_resolution_to_the_source_directories(self, tmp_path, monkeypatch):
        assert _Q.MYPY_PATH == (
            "packages/tools/src",
            "packages/mcp/src",
            "packages/agent/src",
            "packages/specimens/src",
        )
        assert _Q.MYPY_ARGUMENTS[0] == "--config-file="
        seen = {}

        def fake_run(argv, **options):
            cache = Path(argv[argv.index("--cache-dir") + 1])
            seen.update(argv=tuple(argv), cache=cache, **options)
            return subprocess.CompletedProcess(argv, 0, stdout="out\n", stderr="err\n")

        monkeypatch.setattr(_Q.subprocess, "run", fake_run)
        assert _Q.run_mypy(tmp_path, "swcraft_mcp") == (0, "out\nerr\n")
        cache = seen["cache"]
        assert seen["argv"] == (
            sys.executable,
            "-m",
            "mypy",
            *_Q.MYPY_ARGUMENTS,
            "--cache-dir",
            str(cache),
            "-p",
            "swcraft_mcp",
        )
        assert seen["cwd"] == tmp_path
        assert seen["env"]["MYPYPATH"] == os.pathsep.join((tmp_path / entry).as_posix() for entry in _Q.MYPY_PATH)

    def test_the_run_is_never_coloured(self, tmp_path, monkeypatch):
        """A host's FORCE_COLOR would put escape codes into the lines the reader parses: colour is off."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("MYPY_FORCE_COLOR", "1")
        seen = {}

        def fake_run(argv, **options):
            seen.update(argv=tuple(argv), env=options["env"])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_Q.subprocess, "run", fake_run)
        _Q.run_mypy(tmp_path, "swcraft_tools")
        assert seen["env"]["MYPY_FORCE_COLOR"] == "0"
        assert seen["argv"][3 : 3 + len(_Q.MYPY_ARGUMENTS)] == tuple(_Q.MYPY_ARGUMENTS)

    def test_the_run_reuses_no_cache(self, tmp_path, monkeypatch):
        """No incremental read, and a fresh cache directory outside the tree, removed after the run."""
        assert "--no-incremental" in _Q.MYPY_ARGUMENTS
        assert "--cache-dir" not in _Q.MYPY_ARGUMENTS
        seen = {}

        def fake_run(argv, **options):
            cache = Path(argv[argv.index("--cache-dir") + 1])
            seen.update(cache=cache, empty=cache.is_dir() and not any(cache.iterdir()))
            (cache / "stale.json").write_text("{}", encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_Q.subprocess, "run", fake_run)
        _Q.run_mypy(tmp_path, "swcraft_tools")
        first = seen["cache"]
        assert seen["empty"]
        assert tmp_path not in first.parents
        assert not first.exists()
        _Q.run_mypy(tmp_path, "swcraft_tools")
        assert seen["empty"]

    def test_a_baseline_counting_other_packages_is_refused(self):
        baseline = _baseline(errors={"swcraft_tools": 10, "swcraft_mcp": 2, "swcraft_agent": 2, "gone": 1})
        with pytest.raises(_Q.RatchetError, match="reviewed baseline edit"):
            _Q.compare(baseline, _measurement())
        with pytest.raises(_Q.RatchetError, match="reviewed baseline edit"):
            _Q.compare(_baseline(errors={"swcraft_tools": 10}), _measurement())


def test_a_baseline_with_other_limits_is_refused():
    baseline = _baseline(limits={**_Q.LIMITS, "complexity": 40})
    with pytest.raises(_Q.RatchetError, match="limits"):
        _Q.compare(baseline, _measurement())


class TestTheMypyReader:
    def test_it_reads_success_as_zero(self):
        assert _Q.parse_mypy("swcraft_mcp", "packages/mcp/src", 0, "Success: no issues found in 4 source files\n") == 0

    def test_it_reads_found_n_errors(self):
        output = (
            "packages/tools/src/swcraft_tools/a.py:3: error: Bad  [arg-type]\n"
            "packages/tools/src/swcraft_tools/a.py:3: note: See here\n"
            "packages/tools/src/swcraft_tools/b.py:9:5: error: Worse  [union-attr]\n"
            "Found 2 errors in 2 files (checked 149 source files)\n"
        )
        assert _Q.parse_mypy("swcraft_tools", "packages/tools/src", 1, output) == 2

    def test_it_counts_only_the_packages_own_files(self):
        output = (
            "packages/mcp/src/swcraft_mcp/server.py:1: error: Own  [arg-type]\n"
            "packages/tools/src/swcraft_tools/a.py:3: error: Followed import  [arg-type]\n"
            "packages\\mcp\\src\\swcraft_mcp\\tool.py:2: error: Own, Windows spelling  [arg-type]\n"
            "Found 3 errors in 3 files (checked 4 source files)\n"
        )
        assert _Q.parse_mypy("swcraft_mcp", "packages/mcp/src", 1, output) == 2

    def test_it_counts_an_own_file_printed_as_an_absolute_path(self):
        output = (
            "/work/repo/packages/mcp/src/swcraft_mcp/server.py:1: error: Own, POSIX  [arg-type]\n"
            "C:\\Work\\Repo\\Packages\\MCP\\src\\swcraft_mcp\\tool.py:2:3: error: Own, drive  [arg-type]\n"
            "C:\\Work\\Repo\\packages\\tools\\src\\swcraft_tools\\a.py:3: error: Followed import  [arg-type]\n"
            "Found 3 errors in 3 files (checked 4 source files)\n"
        )
        assert _Q.parse_mypy("swcraft_mcp", "packages/mcp/src", 1, output) == 2

    @pytest.mark.parametrize(
        "output",
        [
            "packages/tools/src/swcraft_tools/schema.py:1055: error: Invalid syntax  [syntax]\n"
            "Found 1 error in 1 file (errors prevented further checking)\n",
            "Package 'swcraft_tools' cannot be type checked due to missing py.typed marker. See https://x\n",
        ],
        ids=["errors-prevented-further-checking", "missing-py-typed-marker"],
    )
    def test_it_refuses_a_run_that_did_not_finish(self, output):
        for status in (1, 2):
            with pytest.raises(_Q.RatchetError, match="did not finish"):
                _Q.parse_mypy("swcraft_tools", "packages/tools/src", status, output)

    def test_it_refuses_a_crash_without_a_summary(self):
        with pytest.raises(_Q.RatchetError, match="without a summary line"):
            _Q.parse_mypy("swcraft_tools", "packages/tools/src", 2, "Traceback (most recent call last):\n")

    def test_measure_runs_each_package_once(self, tmp_path):
        calls = []

        def runner(root, package):
            calls.append((root, package))
            return 0, "Success: no issues found in 1 source file\n"

        assert _Q.measure_mypy(tmp_path, runner) == {"swcraft_tools": 0, "swcraft_mcp": 0, "swcraft_agent": 0}
        assert calls == [(tmp_path, "swcraft_tools"), (tmp_path, "swcraft_mcp"), (tmp_path, "swcraft_agent")]


# ---------------------------------------------------------------------------
# The line-citation zero check
# ---------------------------------------------------------------------------

#: The retired grammar, assembled at run time so this module carries no line citation.
_CITED = "validator" + ".py" + ":" + "12"


class TestTheLineCitationZeroCheck:
    _MODULES = {"packages/tools/src/swcraft_tools/validator.py"}

    def test_a_line_citation_in_an_open_tree_fails(self, tmp_path):
        planted = _write(tmp_path / "packages/tools/src/swcraft_tools/mod.py", f'"""See {_CITED}."""\n')
        assert _Q.measure_line_citations(tmp_path, [planted], self._MODULES) == [
            f"packages/tools/src/swcraft_tools/mod.py: {_CITED}"
        ]

    def test_a_scanner_diagnostic_does_not(self):
        assert _Q.line_citations(f"x.py:2: error\n{_CITED}: refused\n", self._MODULES | {"x.py"}) == []

    def test_an_orphan_beside_a_module_on_the_same_or_previous_line_fails(self):
        orphan = "`" + ":" + "40" + "`"
        assert _Q.line_citations(f"see `validator.py` and {orphan}", self._MODULES) == [orphan]
        assert _Q.line_citations(f"see `validator.py`\nand {orphan}", self._MODULES) == [orphan]

    def test_an_orphan_into_a_standard_document_does_not(self):
        orphan = "`" + ":" + "25318" + "`"
        assert _Q.line_citations(f"SWCT R4.2.2 {orphan}", self._MODULES) == []
        assert _Q.line_citations(f"`validator.py`\n\n\nSWCT {orphan}", self._MODULES) == []

    def test_a_module_that_names_no_tracked_file_does_not(self):
        assert _Q.line_citations("no" + "_such" + ".py" + ":" + "3", self._MODULES) == []

    def test_a_range_citation_fails(self):
        cited_range = _CITED + "-" + "40"
        assert _Q.line_citations(f"see {cited_range}", self._MODULES) == [cited_range]

    def test_the_files_read_are_the_tracked_files_of_the_citation_trees(self, tmp_path):
        _write(tmp_path / "packages/tools/src/swcraft_tools/validator.py", "x = 1\n")
        _write(tmp_path / "docs/guide/page.md", "page\n")
        _write(tmp_path / "docs/concepts/closed.md", "closed\n")
        _write(tmp_path / "packages/tools/untracked.md", "untracked\n")
        git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid")
        subprocess.run((*git, "init", "-q"), cwd=tmp_path, check=True)
        subprocess.run(
            (*git, "add", "packages/tools/src", "docs/guide/page.md", "docs/concepts/closed.md"),
            cwd=tmp_path,
            check=True,
        )
        files, modules = _Q.tracked_citation_files(tmp_path)
        assert [path.relative_to(tmp_path).as_posix() for path in files] == [
            "docs/guide/page.md",
            "packages/tools/src/swcraft_tools/validator.py",
        ]
        assert modules == {"packages/tools/src/swcraft_tools/validator.py"}

    def test_a_failed_git_listing_is_refused(self, tmp_path):
        with pytest.raises(_Q.RatchetError, match="git ls-files failed"):
            _Q.tracked_citation_files(tmp_path)

    def test_a_binary_file_is_skipped(self, tmp_path):
        planted = tmp_path / "examples/blob.bin"
        planted.parent.mkdir(parents=True)
        planted.write_bytes(b"\0" + _CITED.encode("utf-8"))
        assert _Q.measure_line_citations(tmp_path, [planted], self._MODULES) == []


# ---------------------------------------------------------------------------
# The test-rule scanner
# ---------------------------------------------------------------------------

_PREAMBLE = """\
import ast
import inspect
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONCEPT = REPO / "docs" / "concepts" / "x.md"
"""


def _scan(tmp_path: Path, body: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    _write(tmp_path / "packages/tools/tests/test_planted.py", _PREAMBLE + textwrap.dedent(body))
    for relative, text in (extra or {}).items():
        _write(tmp_path / relative, text)
    return _Q.measure_test_rule(tmp_path)


_KEY = "packages/tools/tests/test_planted.py::test_it"


class TestTheScannerFires:
    def test_on_a_phrase_of_documentation_prose(self, tmp_path):
        found = _scan(tmp_path, 'def test_it():\n    assert "the rule" in CONCEPT.read_text(encoding="utf-8")\n')
        assert list(found) == [_KEY] and found[_KEY].startswith("docs,")

    def test_on_a_phrase_of_a_source_read_as_text(self, tmp_path):
        found = _scan(
            tmp_path,
            """\
            def test_it():
                text = (REPO / "packages" / "tools" / "src" / "swcraft_tools" / "cli.py").read_text(encoding="utf-8")
                assert text.count("noqa") == 3
            """,
        )
        assert found[_KEY].startswith("source,")

    def test_on_a_phrase_of_a_docstring(self, tmp_path):
        found = _scan(tmp_path, 'def test_it():\n    assert (Path.__doc__ or "").startswith("Path")\n')
        assert found[_KEY].startswith("docstring,")

    def test_on_a_phrase_of_inspected_source(self, tmp_path):
        found = _scan(tmp_path, 'def test_it():\n    assert re.search(r"def \\w+", inspect.getsource(Path))\n')
        assert found[_KEY].startswith("inspect,")

    def test_on_a_failure_guarded_by_the_negated_requirement(self, tmp_path):
        body = 'def test_it():\n    if "rule" not in CONCEPT.read_text(encoding="utf-8"):\n        pytest.fail("x")\n'
        assert list(_scan(tmp_path, body)) == [_KEY]

    def test_through_a_helper_the_test_imports_by_name(self, tmp_path):
        found = _scan(
            tmp_path,
            """\
            from prose_helpers import concept_text

            def test_it():
                assert "the rule" in concept_text("x.md")
            """,
            extra={
                "packages/tools/tests/prose_helpers.py": (
                    "from pathlib import Path\n\nDOCS = Path(__file__).resolve().parents[3] / 'docs' / 'concepts'\n\n"
                    "def concept_text(name):\n    return (DOCS / name).read_text(encoding='utf-8')\n"
                )
            },
        )
        assert list(found) == [_KEY]

    @pytest.mark.parametrize(
        "body",
        [
            'def test_it():\n    unittest.TestCase().assertIn("rule", CONCEPT.read_text(encoding="utf-8"))\n',
            'def test_it():\n    if "rule" not in CONCEPT.read_text(encoding="utf-8"):\n        raise AssertionError\n',
            'def test_it():\n    assert CONCEPT.read_text(encoding="utf-8").endswith("the end\\n")\n',
            'def test_it():\n    assert CONCEPT.read_text(encoding="utf-8") == "the whole page"\n',
            '@pytest.mark.parametrize("phrase", ["the rule", "the other rule"])\n'
            "def test_it(phrase):\n"
            '    assert phrase in CONCEPT.read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    for phrase in ("the rule", "the other rule"):\n'
            '        assert phrase in CONCEPT.read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    with open(REPO / "docs" / "concepts" / "x.md", encoding="utf-8") as handle:\n'
            '        assert "the rule" in handle.read()\n',
            'def test_it():\n    assert "Path" in inspect.getdoc(Path)\n',
            "def test_it():\n"
            '    text = CONCEPT.read_text(encoding="utf-8")\n'
            '    assert all(phrase in text for phrase in ("the rule", "the other rule"))\n',
            'def test_it():\n    assert "SWCraft" in (REPO / "README.md").read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    text = CONCEPT.read_text(encoding="utf-8")\n'
            "    while text:\n"
            '        assert "the rule" in text\n'
            "        text = text[1:]\n",
            "def test_it():\n"
            '    text = CONCEPT.read_text(encoding="utf-8")\n'
            "    match text:\n"
            "        case _:\n"
            '            assert "the rule" in text\n',
            "def test_it(flag):\n"
            "    if flag:\n"
            '        text = CONCEPT.read_text(encoding="utf-8")\n'
            "    else:\n"
            '        text = "a literal"\n'
            '    assert text == "the whole page"\n',
            "def test_it():\n"
            '    if CONCEPT.read_text(encoding="utf-8") != "the whole page":\n'
            "        raise AssertionError\n",
            "def test_it():\n"
            '    if CONCEPT.read_text(encoding="utf-8").count("rule") == 0:\n'
            '        pytest.fail("x")\n',
            'def test_it():\n    assert CONCEPT.read_text(encoding="utf-8").count("rule") != 0\n',
            "def test_it():\n"
            '    assert "def main" in Path("packages/tools/src/swcraft_tools/cli.py").read_text(encoding="utf-8")\n',
            'def test_it():\n    assert CONCEPT.read_text(encoding="utf-8").count("rule")\n',
            'def test_it():\n    assert len(re.findall("rule", CONCEPT.read_text(encoding="utf-8")))\n',
            "def test_it(tmp_path, flag):\n"
            "    if flag:\n"
            '        path = tmp_path / "x.md"\n'
            "    else:\n"
            "        path = CONCEPT\n"
            '    assert "the rule" in path.read_text(encoding="utf-8")\n',
            '@pytest.mark.parametrize("name", ["docs/guide/page.md", "docs/concepts/x.md"])\n'
            "def test_it(name):\n"
            '    assert "the rule" in (REPO / name).read_text(encoding="utf-8")\n',
            '@pytest.mark.parametrize(("name", "phrase"), '
            '[("docs/guide/page.md", "a"), ("packages/tools/src/m.py", "b")])\n'
            "def test_it(name, phrase):\n"
            '    assert phrase in (REPO / name).read_text(encoding="utf-8")\n',
            "def test_it(tmp_path, flag):\n"
            "    if flag:\n"
            "        base = tmp_path\n"
            "    else:\n"
            "        base = REPO\n"
            '    assert "the rule" in (base / "docs/concepts/x.md").read_text(encoding="utf-8")\n',
            '@pytest.mark.parametrize("base", ["docs/guide", "docs/concepts"])\n'
            '@pytest.mark.parametrize("name", ["x.json", "x.md"])\n'
            "def test_it(base, name):\n"
            '    assert "the rule" in (REPO / base / name).read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    for name in ("docs/concepts/x.md",):\n'
            '        assert "the rule" in (REPO / name).read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    for name in ("docs/guide/page.md", "docs/concepts/x.md"):\n'
            '        assert "the rule" in (REPO / name).read_text(encoding="utf-8")\n',
        ],
        ids=[
            "assertIn",
            "raise-under-negated-guard",
            "endswith",
            "literal-equality",
            "parametrized-phrase",
            "for-over-a-literal-tuple",
            "with-open",
            "inspect-getdoc",
            "all-over-a-generator",
            "readme-outside-docs",
            "inside-a-while-loop",
            "inside-a-match-case",
            "a-source-read-in-one-branch-a-literal-in-the-other",
            "inequality-under-a-failure-guard",
            "zero-count-under-a-failure-guard",
            "nonzero-count",
            "a-repository-relative-source-path",
            "a-truthy-count",
            "a-truthy-findall-length",
            "a-source-path-chosen-in-the-second-branch",
            "a-source-path-in-a-later-parametrize-row",
            "a-source-path-in-a-later-row-of-a-table",
            "a-repository-directory-chosen-in-the-second-branch",
            "stacked-parametrize-tables-in-their-later-rows",
            "a-path-bound-by-a-loop-over-a-literal-tuple",
            "a-source-path-in-a-later-loop-value",
        ],
    )
    def test_on_each_requirement_form(self, tmp_path, body):
        assert list(_scan(tmp_path, body)) == [_KEY]

    def test_on_a_repository_relative_readme(self, tmp_path):
        body = 'def test_it():\n    assert "SWCraft" in Path("README.md").read_text(encoding="utf-8")\n'
        assert list(_scan(tmp_path, body, extra={"README.md": "SWCraft\n"})) == [_KEY]

    def test_in_a_nested_test_class(self, tmp_path):
        body = (
            "class TestOuter:\n"
            "    class TestInner:\n"
            "        def test_it(self):\n"
            '            assert "rule" in CONCEPT.read_text(encoding="utf-8")\n'
        )
        assert list(_scan(tmp_path, body)) == ["packages/tools/tests/test_planted.py::TestOuter::TestInner::test_it"]

    def test_under_a_class_level_parametrize(self, tmp_path):
        body = (
            '@pytest.mark.parametrize("name", ["docs/concepts/x.md"])\n'
            "class TestX:\n"
            "    def test_it(self, name):\n"
            '        assert "rule" in (REPO / name).read_text(encoding="utf-8")\n'
        )
        assert list(_scan(tmp_path, body)) == ["packages/tools/tests/test_planted.py::TestX::test_it"]

    def test_a_method_of_a_test_class_is_keyed_with_its_class(self, tmp_path):
        body = 'class TestX:\n    def test_it(self):\n        assert "rule" in CONCEPT.read_text(encoding="utf-8")\n'
        assert list(_scan(tmp_path, body)) == ["packages/tools/tests/test_planted.py::TestX::test_it"]


class TestTheScannerStaysSilent:
    @pytest.mark.parametrize(
        "body",
        [
            'def test_it():\n    assert "FIXME" not in CONCEPT.read_text(encoding="utf-8")\n',
            'def test_it():\n    assert re.findall(r"FIXME \\d", CONCEPT.read_text(encoding="utf-8")) == []\n',
            "def test_it():\n"
            '    tree = ast.parse((REPO / "packages/tools/src/swcraft_tools/cli.py").read_text(encoding="utf-8"))\n'
            '    assert "main" in [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]\n',
            "def test_it():\n"
            '    data = json.loads((REPO / "docs/concepts/x.md").read_text(encoding="utf-8"))\n'
            '    assert "key" in data\n',
            "def test_it():\n"
            '    committed = (REPO / "packages/tools/src/swcraft_tools/gen.py").read_text(encoding="utf-8")\n'
            "    assert committed == generate()\n",
            "def test_it(tmp_path):\n"
            '    out = (tmp_path / "README.md").read_text(encoding="utf-8")\n'
            '    assert "SWCraft" in out\n',
            'def test_it():\n    assert "rule" in (REPO / "docs/guide/page.md").read_text(encoding="utf-8")\n',
            'def test_it():\n    assert "x" in (REPO / "docs/contracts/data.json").read_text(encoding="utf-8")\n',
            "def test_it():\n"
            '    assert "x" in (REPO / "packages/tools/tests/fixtures/a.md").read_text(encoding="utf-8")\n',
            'def test_it():\n    assert CONCEPT.read_text(encoding="utf-8").count("obsolete") == 0\n',
            'def test_it():\n    assert len(re.findall("old", CONCEPT.read_text(encoding="utf-8"))) == 0\n',
            'def test_it():\n    assert 1 > CONCEPT.read_text(encoding="utf-8").count("obsolete")\n',
            "def test_it():\n"
            '    if CONCEPT.read_text(encoding="utf-8").count("obsolete") > 0:\n'
            '        pytest.fail("x")\n',
            'def test_it():\n    assert "SWCraft" in Path("out.md").read_text(encoding="utf-8")\n',
            'def test_it():\n    assert not CONCEPT.read_text(encoding="utf-8").count("obsolete")\n',
            '@pytest.mark.parametrize("name", ["docs/guide/page.md", "docs/guide/other.md"])\n'
            "def test_it(name):\n"
            '    assert "the rule" in (REPO / name).read_text(encoding="utf-8")\n',
        ],
        ids=[
            "token-ban",
            "empty-findall",
            "ast-parse",
            "json-loads",
            "generated-equals-committed",
            "tmp-path-output",
            "user-guide",
            "json-data",
            "a-fixture-file",
            "a-zero-count",
            "a-zero-findall-length",
            "a-count-below-one",
            "a-positive-count-under-a-failure-guard",
            "a-relative-path-the-repository-does-not-hold",
            "a-negated-truthy-count",
            "every-parametrize-row-in-the-user-guide",
        ],
    )
    def test_on_what_the_rule_allows(self, tmp_path, body):
        assert _scan(tmp_path, body) == {}


class TestTheDocumentedBlindSpots:
    """Each blind spot the script's docstring names, shown not reached."""

    @pytest.mark.parametrize(
        "body",
        [
            # a text that arrives through a pytest fixture
            "@pytest.fixture\n"
            "def concept():\n"
            '    return CONCEPT.read_text(encoding="utf-8")\n\n'
            "def test_it(concept):\n"
            '    assert "the rule" in concept\n',
            # an assertion made inside a helper
            "def _require(phrase):\n"
            '    assert phrase in CONCEPT.read_text(encoding="utf-8")\n\n'
            "def test_it():\n"
            '    _require("the rule")\n',
            # a phrase built at run time
            'def test_it():\n    phrase = "the" + " rule"\n    assert phrase in CONCEPT.read_text(encoding="utf-8")\n',
            # a disguised positive written as a negative
            'def test_it():\n    assert "the old rule" not in CONCEPT.read_text(encoding="utf-8")\n',
            # a line number pinned as an integer
            "def test_it():\n    assert inspect.getsourcelines(Path)[1] == 42\n",
            # a parametrize given through pytestmark
            "class TestX:\n"
            '    pytestmark = pytest.mark.parametrize("name", ["docs/concepts/x.md"])\n\n'
            "    def test_it(self, name):\n"
            '        assert "rule" in (REPO / name).read_text(encoding="utf-8")\n',
        ],
        ids=[
            "fixture",
            "assertion-in-helper",
            "phrase-built-at-run-time",
            "disguised-negative",
            "integer-line-pin",
            "pytestmark-parametrize",
        ],
    )
    def test_is_not_reached(self, tmp_path, body):
        assert _scan(tmp_path, body) == {}

    def test_a_relative_path_is_judged_by_the_repository_not_by_the_working_directory(self, tmp_path):
        body = (
            "def test_it(tmp_path, monkeypatch):\n"
            "    monkeypatch.chdir(tmp_path)\n"
            '    assert "SWCraft" in Path("README.md").read_text(encoding="utf-8")\n'
        )
        assert list(_scan(tmp_path, body, extra={"README.md": "SWCraft\n"})) == [_KEY]

    def test_a_helper_called_through_its_module_is_not_followed(self, tmp_path):
        found = _scan(
            tmp_path,
            """\
            import prose_helpers

            def test_it():
                assert "the rule" in prose_helpers.concept_text("x.md")
            """,
            extra={
                "packages/tools/tests/prose_helpers.py": (
                    "from pathlib import Path\n\nDOCS = Path(__file__).resolve().parents[3] / 'docs' / 'concepts'\n\n"
                    "def concept_text(name):\n    return (DOCS / name).read_text(encoding='utf-8')\n"
                )
            },
        )
        assert found == {}


class TestTheTestRuleRatchet:
    def test_a_new_violator_fails_a_listed_one_passes(self):
        baseline = _baseline(allowlist=["a.py::test_old"])
        measurement = _measurement(test_rule={"a.py::test_old": "docs", "a.py::test_new": "docs"})
        assert [item.split(":", 1)[0] for item in _Q.compare(baseline, measurement).growth] == ["test_rule a.py"]
        assert "a.py::test_new" in _Q.compare(baseline, measurement).growth[0]

    def test_the_allowlist_only_shrinks(self):
        baseline = _baseline(allowlist=["a.py::test_fixed", "a.py::test_old"])
        measurement = _measurement(test_rule={"a.py::test_old": "docs"})
        assert ["a.py::test_fixed" in item for item in _Q.compare(baseline, measurement).stale] == [True]
        assert _Q.lowered_baseline(baseline, measurement)["test_rule"]["allowlist"] == ["a.py::test_old"]

    def test_a_listed_exception_passes_and_is_kept(self):
        baseline = _baseline()
        baseline["test_rule"]["exceptions"] = {"a.py::test_kept": "his word"}
        measurement = _measurement(test_rule={"a.py::test_kept": "docs"})
        assert _Q.compare(baseline, measurement).growth == []
        assert _Q.lowered_baseline(baseline, measurement)["test_rule"]["exceptions"] == {"a.py::test_kept": "his word"}


# ---------------------------------------------------------------------------
# The private crossings
# ---------------------------------------------------------------------------

_SRC = "packages/tools/src/pkg"

_BASE_TREE = {
    f"{_SRC}/__init__.py": "",
    f"{_SRC}/a.py": "_x = 1\n_y = 2\n_z = 3\npublic = 4\n",
    f"{_SRC}/_sub.py": "",
    f"{_SRC}/_version.py": "__version__ = '1'\n",
    f"{_SRC}/sub/__init__.py": "",
}


def _crossings(tmp_path: Path, relative: str, text: str) -> dict[str, str]:
    for path, body in {**_BASE_TREE, relative: text}.items():
        _write(tmp_path / path, body)
    return _Q.measure_private_crossings(tmp_path)


class TestThePrivateCrossingsScanner:
    @pytest.mark.parametrize(
        ("relative", "text", "name", "module"),
        [
            pytest.param(f"{_SRC}/b.py", "from pkg.a import _x\n", "_x", "pkg.a", id="absolute"),
            pytest.param(f"{_SRC}/b.py", "from .a import _y\n", "_y", "pkg.a", id="relative-one-dot"),
            pytest.param(f"{_SRC}/sub/c.py", "from ..a import _z\n", "_z", "pkg.a", id="relative-two-dots"),
            pytest.param(f"{_SRC}/b.py", "from pkg import _x\n", "_x", "pkg", id="through-a-package-init"),
            pytest.param(f"{_SRC}/b.py", "def f():\n    from pkg.a import _x\n", "_x", "pkg.a", id="inside-a-function"),
            pytest.param(f"{_SRC}/__init__.py", "from . import _sub\n", "_sub", "pkg", id="init-own-submodule"),
            pytest.param(f"{_SRC}/__init__.py", "from .a import _x as _x\n", "_x", "pkg.a", id="init-re-export"),
            pytest.param(
                f"{_SRC}/__init__.py", "from pkg.a import public as _public\n", "_public", "pkg.a", id="init-alias"
            ),
            pytest.param(f"{_SRC}/__init__.py", "import pkg.a as _a\n", "_a", "pkg.a", id="init-module-alias"),
            pytest.param(f"{_SRC}/b.py", "from . import _sub\n", "_sub", "pkg", id="private-submodule-via-package"),
            pytest.param(f"{_SRC}/sub/__init__.py", "from .. import _x\n", "_x", "pkg", id="sub-package-init-two-dots"),
        ],
    )
    def test_a_crossing_is_counted_under_its_importer_and_name(self, tmp_path, relative, text, name, module):
        assert _crossings(tmp_path, relative, text) == {f"{relative}::{name}": module}

    @pytest.mark.parametrize(
        ("relative", "text"),
        [
            pytest.param(f"{_SRC}/b.py", "from pkg._version import __version__\n", id="dunder"),
            pytest.param(f"{_SRC}/__init__.py", "from pkg._version import __version__\n", id="init-dunder"),
            pytest.param(f"{_SRC}/__init__.py", "from . import _nothing\n", id="init-own-name-not-a-submodule"),
            pytest.param(f"{_SRC}/b.py", "from pkg.a import public as _public\n", id="non-init-private-alias"),
            pytest.param(f"{_SRC}/b.py", "from pkg import a\n\nVALUE = a._x\n", id="attribute-read-not-reached"),
            pytest.param(f"{_SRC}/b.py", "from os import _exit\n", id="not-a-product-module"),
            pytest.param("packages/tools/tests/test_b.py", "from pkg.a import _x\n", id="product-test-importer"),
            pytest.param("scripts/b.py", "from pkg.a import _x\n", id="script-importer"),
            pytest.param(f"{_SRC}/a.py", "from pkg.a import _x\n", id="own-name-imported-absolutely"),
            pytest.param(f"{_SRC}/__init__.py", "import pkg.a as a\n", id="init-public-module-alias"),
            pytest.param(f"{_SRC}/__init__.py", "import os as _os\n", id="init-alias-of-a-foreign-module"),
            pytest.param(f"{_SRC}/__init__.py", "import pkg._sub\n", id="private-submodule-import-not-reached"),
            pytest.param(f"{_SRC}/__init__.py", "from pkg.a import *\n", id="star-import-not-reached"),
            pytest.param(f"{_SRC}/__init__.py", "_own = 1\n__all__ = ['_own']\n", id="own-definition-not-reached"),
        ],
    )
    def test_no_crossing_is_counted(self, tmp_path, relative, text):
        assert _crossings(tmp_path, relative, text) == {}

    def test_two_imports_of_one_name_keep_one_entry_naming_the_last_module(self, tmp_path):
        text = "from pkg.a import _x\nfrom pkg import _x\n"
        assert _crossings(tmp_path, f"{_SRC}/b.py", text) == {f"{_SRC}/b.py::_x": "pkg"}


class TestThePrivateCrossingsRatchet:
    _OLD = {"p/b.py::_x": "pkg.a", "p/c.py::_y": "pkg.a"}

    def test_a_new_crossing_fails_and_is_named(self):
        baseline = _baseline(private_crossings=dict(self._OLD))
        measurement = _measurement(private_crossings={**self._OLD, "p/d.py::_z": "pkg.a"})
        growth = _Q.compare(baseline, measurement).growth
        assert len(growth) == 1 and "p/d.py::_z" in growth[0]

    def test_an_unchanged_list_passes(self):
        verdict = _Q.compare(_baseline(private_crossings=dict(self._OLD)), _measurement(private_crossings=self._OLD))
        assert (verdict.growth, verdict.stale) == ([], [])

    def test_the_list_only_shrinks(self):
        baseline = _baseline(private_crossings=dict(self._OLD))
        measurement = _measurement(private_crossings={"p/b.py::_x": "pkg.a"})
        verdict = _Q.compare(baseline, measurement)
        assert verdict.growth == []
        assert len(verdict.stale) == 1 and "p/c.py::_y" in verdict.stale[0]
        assert _Q.lowered_baseline(baseline, measurement)["private_crossings"] == {"p/b.py::_x": "pkg.a"}

    def test_the_first_baseline_records_every_crossing(self):
        assert _Q.initial_baseline(_measurement(private_crossings=self._OLD))["private_crossings"] == self._OLD

    def test_the_command_line_refuses_a_new_crossing_and_counts_the_list(self, tmp_path, capsys):
        (tmp_path / "scripts").mkdir()

        def run(command, measurement):
            return _Q.run([*command, "--repo-root", str(tmp_path)], measure_tree=lambda root: measurement)

        run(["update", "--initial"], _measurement())
        grown = _measurement(private_crossings={"p/d.py::_z": "pkg.a"})
        assert run(["check"], grown) == 1
        assert run(["update"], grown) == 1
        assert _Q.read_baseline(tmp_path / _Q.BASELINE)["private_crossings"] == {}
        capsys.readouterr()
        assert run(["check"], _measurement()) == 0
        assert "0 private crossings" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


class TestTheCommandLine:
    @staticmethod
    def _run(tmp_path, command, measurement):
        return _Q.run([*command, "--repo-root", str(tmp_path)], measure_tree=lambda root: measurement)

    def _read(self, tmp_path):
        return json.loads((tmp_path / _Q.BASELINE).read_text(encoding="utf-8"))

    def test_initial_writes_the_first_baseline_and_only_the_first(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        measurement = _measurement(function_length={"m::f": 300, "m::g": 10})
        assert self._run(tmp_path, ["update", "--initial"], measurement) == 0
        assert self._read(tmp_path)["function_length"] == {"m::f": 300}
        assert self._run(tmp_path, ["update", "--initial"], measurement) == 1

    def test_check_fails_on_growth_and_update_refuses_it(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement(function_length={"m::f": 300}))
        grown = _measurement(function_length={"m::f": 301})
        assert self._run(tmp_path, ["check"], grown) == 1
        assert self._run(tmp_path, ["update"], grown) == 1
        assert self._read(tmp_path)["function_length"] == {"m::f": 300}

    def test_update_lowers_and_then_check_passes(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement(function_length={"m::f": 300}))
        lower = _measurement(function_length={"m::f": 250})
        assert self._run(tmp_path, ["update"], lower) == 0
        assert self._read(tmp_path)["function_length"] == {"m::f": 250}
        assert self._run(tmp_path, ["check"], lower) == 0

    def test_a_line_citation_fails_check(self, tmp_path, capsys):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement())
        assert self._run(tmp_path, ["check"], _measurement(line_citations=[f"x.md: {_CITED}"])) == 1
        assert _CITED in capsys.readouterr().out

    def test_check_without_a_baseline_fails(self, tmp_path):
        assert self._run(tmp_path, ["check"], _measurement()) == 1

    def test_check_names_a_lowered_value_and_passes(self, tmp_path, capsys):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement(function_length={"m::f": 300}))
        capsys.readouterr()
        assert self._run(tmp_path, ["check"], _measurement(function_length={"m::f": 250})) == 0
        assert "m::f: 300 -> 250" in capsys.readouterr().out
        assert self._read(tmp_path)["function_length"] == {"m::f": 300}

    def test_initial_goes_with_update_alone(self, tmp_path):
        with pytest.raises(SystemExit) as raised:
            self._run(tmp_path, ["check", "--initial"], _measurement())
        assert raised.value.code == 2

    def test_initial_refuses_while_a_line_citation_exists(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        assert self._run(tmp_path, ["update", "--initial"], _measurement(line_citations=[f"x.md: {_CITED}"])) == 1
        assert not (tmp_path / _Q.BASELINE).exists()

    def test_update_refuses_a_grown_mypy_count(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement())
        grown = _measurement(mypy={"swcraft_tools": 11, "swcraft_mcp": 2, "swcraft_agent": 2})
        assert self._run(tmp_path, ["update"], grown) == 1
        assert self._read(tmp_path)["mypy"]["errors"]["swcraft_tools"] == 10

    def test_an_incomparable_baseline_exits_one(self, tmp_path, capsys):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement())
        baseline = self._read(tmp_path)
        baseline["limits"]["complexity"] = 40
        _Q.write_baseline(tmp_path / _Q.BASELINE, baseline)
        assert self._run(tmp_path, ["check"], _measurement()) == 1
        assert "limits" in capsys.readouterr().err


class TestInit:
    """``init <key>`` creates an absent measured key and never re-records a present one."""

    @staticmethod
    def _run(tmp_path, command, measurement):
        return _Q.run([*command, "--repo-root", str(tmp_path)], measure_tree=lambda root: measurement)

    @staticmethod
    def _without(tmp_path, key):
        """A first baseline at *tmp_path* with *key* removed."""
        (tmp_path / "scripts").mkdir()
        baseline = _Q.initial_baseline(_measurement(function_length={"m::f": 300}, test_file_lines={"t.py": 3100}))
        del baseline[key]
        _Q.write_baseline(tmp_path / _Q.BASELINE, baseline)
        return baseline

    def test_it_writes_exactly_the_absent_key_from_the_tree(self, tmp_path):
        before = self._without(tmp_path, "test_file_lines")
        tree = _measurement(function_length={"m::f": 301}, test_file_lines={"t.py": 3200, "u.py": 10})
        assert self._run(tmp_path, ["init", "test_file_lines"], tree) == 0
        after = json.loads((tmp_path / _Q.BASELINE).read_text(encoding="utf-8"))
        assert after["test_file_lines"] == {"t.py": 3200}
        assert {key: value for key, value in after.items() if key != "test_file_lines"} == before
        assert list(after) == list(_Q.initial_baseline(tree))
        assert self._run(tmp_path, ["check"], tree) == 1  # the grown m::f is still held at 300

    @pytest.mark.parametrize("key", ["function_length", "docstring_lines", "mypy", "test_rule", "private_crossings"])
    def test_every_measured_kind_of_key_is_written_from_the_tree(self, tmp_path, key):
        before = self._without(tmp_path, key)
        if key in _Q.LIMITS:
            del before["limits"][key]
            _Q.write_baseline(tmp_path / _Q.BASELINE, before)
        tree = _measurement(
            function_length={"m::f": 301, "m::g": 5},
            test_file_lines={"t.py": 3100},
            mypy={"swcraft_tools": 7, "swcraft_mcp": 0, "swcraft_agent": 1},
            test_rule={"tests/test_x.py::test_y": "a phrase"},
            private_crossings={"p/b.py::_x": "pkg.a"},
        )
        assert self._run(tmp_path, ["init", key], tree) == 0
        after = _Q.read_baseline(tmp_path / _Q.BASELINE)
        assert after[key] == _Q.initial_baseline(tree)[key]
        assert after["limits"] == _Q.LIMITS
        assert list(after) == list(_Q.initial_baseline(tree))

    def test_a_baseline_without_a_limits_table_is_refused_with_exit_1(self, tmp_path, capsys):
        baseline = self._without(tmp_path, "test_file_lines")
        del baseline["limits"]
        _Q.write_baseline(tmp_path / _Q.BASELINE, baseline)
        written = (tmp_path / _Q.BASELINE).read_bytes()
        assert self._run(tmp_path, ["init", "test_file_lines"], _measurement()) == 1
        assert "no limits table" in capsys.readouterr().err
        assert (tmp_path / _Q.BASELINE).read_bytes() == written

    def test_init_without_a_baseline_is_refused_with_exit_1(self, tmp_path, capsys):
        (tmp_path / "scripts").mkdir()
        assert self._run(tmp_path, ["init", "test_file_lines"], _measurement()) == 1
        assert "no baseline" in capsys.readouterr().err
        assert not (tmp_path / _Q.BASELINE).exists()

    @pytest.mark.parametrize(
        "key", ["test_file_lines", "function_length", "docstring_lines", "mypy", "test_rule", "private_crossings"]
    )
    def test_a_present_key_is_refused_and_nothing_changes(self, tmp_path, capsys, key):
        (tmp_path / "scripts").mkdir()
        self._run(tmp_path, ["update", "--initial"], _measurement(test_file_lines={"t.py": 3100}))
        written = (tmp_path / _Q.BASELINE).read_bytes()
        grown = _measurement(
            test_file_lines={"t.py": 3900}, mypy={"swcraft_tools": 99, "swcraft_mcp": 2, "swcraft_agent": 2}
        )
        assert self._run(tmp_path, ["init", key], grown) == 1
        assert key in capsys.readouterr().err
        assert (tmp_path / _Q.BASELINE).read_bytes() == written

    @pytest.mark.parametrize("key", ["limits", "line_citations", "no_such_key"])
    def test_an_unknown_key_is_refused(self, tmp_path, capsys, key):
        self._without(tmp_path, "test_file_lines")
        written = (tmp_path / _Q.BASELINE).read_bytes()
        assert self._run(tmp_path, ["init", key], _measurement()) == 1
        assert ", ".join(_Q.MEASURED_KEYS) in capsys.readouterr().err
        assert (tmp_path / _Q.BASELINE).read_bytes() == written

    @pytest.mark.parametrize("command", [["check"], ["update"]])
    def test_check_and_update_refuse_an_absent_key_and_name_init(self, tmp_path, capsys, command):
        self._without(tmp_path, "test_file_lines")
        written = (tmp_path / _Q.BASELINE).read_bytes()
        assert self._run(tmp_path, command, _measurement(function_length={"m::f": 300})) == 1
        error = capsys.readouterr().err
        assert "test_file_lines" in error and "init <key>" in error
        assert (tmp_path / _Q.BASELINE).read_bytes() == written

    def test_a_limit_the_baseline_lacks_is_added_with_the_key(self, tmp_path):
        self._without(tmp_path, "test_file_lines")
        baseline = _Q.read_baseline(tmp_path / _Q.BASELINE)
        del baseline["limits"]["test_file_lines"]
        _Q.write_baseline(tmp_path / _Q.BASELINE, baseline)
        assert self._run(tmp_path, ["init", "test_file_lines"], _measurement()) == 0
        assert _Q.read_baseline(tmp_path / _Q.BASELINE)["limits"] == _Q.LIMITS

    @pytest.mark.parametrize("command", [["init"], ["check", "mypy"], ["update", "mypy"]])
    def test_a_key_goes_with_init_alone(self, tmp_path, command):
        with pytest.raises(SystemExit) as raised:
            self._run(tmp_path, command, _measurement())
        assert raised.value.code == 2


def test_the_committed_baseline_was_written_by_this_script():
    """The baseline's limits and mypy arguments are the script's: a count is compared like with like."""
    baseline = _Q.read_baseline(REPO_ROOT / _Q.BASELINE)
    assert baseline["limits"] == _Q.LIMITS
    assert baseline["mypy"]["arguments"] == list(_Q.MYPY_ARGUMENTS)
    assert baseline["mypy"]["mypypath"] == list(_Q.MYPY_PATH)
    assert set(baseline["mypy"]["errors"]) == set(_Q.MYPY_PACKAGES)


def test_every_measured_root_is_a_directory_of_this_checkout_holding_python() -> None:
    """A missing root is skipped by the measurement, so a wrong one would shrink it and still pass."""
    roots = {*_Q.SOURCE_ROOTS, *_Q.PACKAGE_ROOTS, *_Q.MYPY_PACKAGES.values(), *_Q.MYPY_PATH}
    roots |= {*_Q.CITATION_TREES, *_Q.TEST_TREES, *_Q._PROJECTS}
    for root in sorted(roots):
        assert (REPO_ROOT / root).is_dir(), root
    for root in sorted({*_Q.SOURCE_ROOTS, *_Q.TEST_TREES}):
        assert next((REPO_ROOT / root).rglob("*.py"), None) is not None, root
    open_test_trees = [tree for tree in _Q.TEST_FILE_TREES if tree not in _Q.CLOSED_TEST_TREES]
    for pattern in open_test_trees:
        assert any(base.is_dir() for base in REPO_ROOT.glob(pattern)), pattern
    assert next((REPO_ROOT / _Q.EXTENSION_TEST_ROOT).rglob("*.test.ts"), None) is not None
