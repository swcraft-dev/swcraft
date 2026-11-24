"""Capture what the component exchange publishes, and compare a later run against that capture.

A regression check for a no-change refactor of the component exchange. It is **not** an
independent oracle: every output it records is produced by the implementation under test, so it
proves *unchanged*, never *correct*.

Commands::

    .venv/bin/python scripts/exchange_output_oracle.py --capture tmp/oracle/baseline.json
    .venv/bin/python scripts/exchange_output_oracle.py --compare tmp/oracle/baseline.json

It runs pytest in-process over :data:`TEST_FILES` and records, per test node id in call order, the
component and whole-model ARXML bytes, every ``export-summary.json``, the reports and planned writes
(or the refusal) of ``plan_component_import`` and ``import_component``, every ``projected_swc``
result, and every ``generate arxml`` / ``import component`` CLI run (exit code, stdout, stderr, the
``--report`` file); then it exports a copy of ``examples/basic`` whole, per component and
``--all-components``, and each run in :data:`EXPECTED_EXAMPLE_SUCCESS` must exit 0 and write at
least one new or changed file, so a comparison cannot pass over a CLI error both sides share. Paths
are normalised (``<tmp>``, ``<repo>``, ``<mkdtemp>``), so a baseline from another worktree compares.

``--compare`` fails on any differing entry (naming the node and the entry), on a differing node
set, on a listed file that contributed no entry, and on failing tests (the record is kept, so
every moved node is still named). ``--capture`` writes no baseline over a failing run, an empty
listed file, or an expected example run that failed or wrote nothing.

Not reached: output of a subprocess a test starts, CLI commands other than ``_EXCHANGE_COMMANDS``,
and product paths none of :data:`TEST_FILES` exercises (the MCP server's tests and ``scripts/tests/``
among them).
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_ROOT = REPO_ROOT / "packages" / "tools"

#: The ``tools`` test files that exercise the component export or import, the marker suites and the
#: projections' defaults, each contributing at least one entry. Only ``packages/tools/tests``: the
#: in-process run collects from that root alone.
TEST_FILES: tuple[str, ...] = (
    "tests/test_access_points_acceptance.py",
    "tests/test_arxml_access_writer.py",
    "tests/test_arxml_components_writer.py",
    "tests/test_arxml_golden.py",
    "tests/test_arxml_import_cli.py",
    "tests/test_arxml_import_interfaces.py",
    "tests/test_arxml_import_merges.py",
    "tests/test_arxml_import_types.py",
    "tests/test_arxml_import_unresolved.py",
    "tests/test_arxml_reader.py",
    "tests/test_arxml_reader_markers.py",
    "tests/test_arxml_symbol_and_port_init_writer.py",
    "tests/test_arxml_xsd_conformance.py",
    "tests/test_behaviour_losses_import.py",
    "tests/test_behavioural_emission.py",
    "tests/test_behavioural_import.py",
    "tests/test_capability_carry_combinations.py",
    "tests/test_cli.py",
    "tests/test_com_spec_silent_spelling.py",
    "tests/test_component_data_description.py",
    "tests/test_component_dependencies.py",
    "tests/test_component_exchange_acceptance.py",
    "tests/test_component_exchange_storage.py",
    "tests/test_component_export.py",
    "tests/test_component_import.py",
    "tests/test_component_import_init_values.py",
    "tests/test_component_same_name_pair.py",
    "tests/test_data_description_import.py",
    "tests/test_data_description_schema.py",
    "tests/test_data_event_exchange.py",
    "tests/test_description_emission.py",
    "tests/test_description_import.py",
    "tests/test_encoding_round_trip.py",
    "tests/test_exact_bounds.py",
    "tests/test_exact_repeats_per_package.py",
    "tests/test_exclusive_area_exchange.py",
    "tests/test_handle_termination_and_restart.py",
    "tests/test_identifier_coverage.py",
    "tests/test_impl_composites_exchange.py",
    "tests/test_import_refused_combinations.py",
    "tests/test_init_values_acceptance.py",
    "tests/test_inter_runnable_variable_exchange.py",
    "tests/test_member_descriptions.py",
    "tests/test_name_space_collisions.py",
    "tests/test_operation_event_editing.py",
    "tests/test_operation_event_exchange.py",
    "tests/test_package_field.py",
    "tests/test_package_placement_import.py",
    "tests/test_package_placement_writer.py",
    "tests/test_port_api_option_import.py",
    "tests/test_port_init_import.py",
    "tests/test_port_init_rules.py",
    "tests/test_reader.py",
    "tests/test_reader_behaviour_losses.py",
    "tests/test_reserved_device_names.py",
    "tests/test_reserved_name_policies.py",
    "tests/test_runnable_event_name_space.py",
    "tests/test_runnable_symbol_import.py",
    "tests/test_runnable_symbol_rules.py",
    "tests/test_single_read.py",
    "tests/test_skip_categories.py",
    "tests/test_small_fidelity_fields.py",
    "tests/test_stated_names_import.py",
    "tests/test_stub_ports_arxml.py",
    "tests/test_swc_timing_import.py",
    "tests/test_swc_timing_writer.py",
    "tests/test_termination_required.py",
    "tests/test_uncarried_losses.py",
    "tests/test_unit_round_trip.py",
    "tests/test_value_type_range.py",
    "tests/test_walk_reports_byte_identity.py",
    "tests/test_writer_defaults.py",
)

FAST_DESELECT = (
    "not benchmark and not slow and not artifact and not integration and not large_bench and not real_models"
)

EXAMPLE_FILE = "examples/basic"

#: The ``examples/basic`` runs that must exit 0 and write at least one file (every one of the three).
EXPECTED_EXAMPLE_SUCCESS: tuple[str, ...] = ("whole", "component-ExampleSWC", "all-components")

#: The exception attributes that carry a refusal's lines, recorded where the class has them.
_LINE_ATTRIBUTES = ("violations", "conflicts", "skip_lines", "duplicate_lines", "lines", "missing")


class _Tee:
    """A text stream that writes to *inner* and keeps a copy."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return self._inner.write(text)

    def flush(self) -> None:
        self._inner.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Recorder:
    """The pytest plugin: patches the watched functions and records their outputs per node."""

    def __init__(self) -> None:
        self.nodes: dict[str, list[dict[str, Any]]] = {}
        self.current: str | None = None
        self.tmp_patterns: list[tuple[str, str]] = []
        self.patched: dict[str, int] = {}

    # -- normalisation -------------------------------------------------------------------

    def set_tmp(self, base: Path) -> None:
        self.tmp_patterns = [(_spelled(base), "<tmp>")]
        # So a baseline taken in another worktree compares with this one's.
        self.tmp_patterns.append((_spelled(REPO_ROOT), "<repo>"))
        # A test's own ``tempfile.mkdtemp()`` directory; the import report's Markdown may escape it.
        system = Path(tempfile.gettempdir())
        self.tmp_patterns.append((_spelled(system) + r"(\\?/)tmp(?:\\?[A-Za-z0-9_]){8}", r"<mkdtemp>"))

    def norm(self, text: str) -> str:
        for pattern, replacement in self.tmp_patterns:
            text = re.sub(pattern, replacement, text)
        # A test that mocks the writer prints the mock, whose ``id`` is an address.
        return re.sub(r"(<MagicMock name='[^']*') id='\d+'>", r"\1>", text)

    def add(self, entry: dict[str, Any]) -> None:
        node = self.current if self.current is not None else "<outside-a-test>"
        self.nodes.setdefault(node, []).append(entry)

    # -- the wrappers -------------------------------------------------------------------

    def _file_text(self, path: Any) -> str:
        return self.norm(Path(path).read_bytes().decode("utf-8", errors="surrogateescape"))

    def wrap_generate(self, kind: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = original(*args, **kwargs)
            except Exception as error:
                self.add({"kind": kind, "error": type(error).__name__, "text": self.norm(str(error))})
                raise
            self.add({"kind": kind, "bytes": self._file_text(result)})
            return result

        return wrapper

    def wrap_summary(self, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.add({"kind": "export-summary", "text": self.norm(result)})
            return result

        return wrapper

    def _error_entry(self, error: BaseException) -> dict[str, Any]:
        entry: dict[str, Any] = {"kind": "import-error", "type": type(error).__name__, "text": self.norm(str(error))}
        for name in _LINE_ATTRIBUTES:
            if hasattr(error, name):
                value = getattr(error, name)
                items = sorted(value) if isinstance(value, set | frozenset) else list(value)
                entry[name] = [self.norm(str(item)) for item in items]
        if hasattr(error, "update_resolves"):
            entry["update_resolves"] = bool(error.update_resolves)
        return entry

    def _relative(self, path: Any, model_root: Any) -> str:
        target = Path(path).resolve()
        with contextlib.suppress(ValueError):
            return target.relative_to(Path(model_root).resolve()).as_posix()
        return self.norm(Path(path).as_posix())

    def _rendered(self, render: Callable[[], str]) -> str:
        """The rendered report; a test that injects a failing renderer gets the failure recorded."""
        try:
            return self.norm(render())
        except Exception as error:
            return f"<render raised {type(error).__name__}: {self.norm(str(error))}>"

    def wrap_plan(self, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            model_root = args[1] if len(args) > 1 else kwargs.get("model_root")
            try:
                plan = original(*args, **kwargs)
            except Exception as error:
                self.add(self._error_entry(error))
                raise
            writes = [
                [self._relative(path, model_root), self.norm(_canonical(payload))] for path, payload in plan.writes
            ]
            self.add(
                {
                    "kind": "import-plan",
                    "render": self._rendered(plan.report.render),
                    "markdown": self._rendered(plan.report.render_markdown),
                    "writes": writes,
                }
            )
            return plan

        return wrapper

    def wrap_import(self, original: Callable[..., Any]) -> Callable[..., Any]:
        """``import_component``: the report as the apply leaves it, ``written_paths`` filled."""

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            model_root = args[1] if len(args) > 1 else kwargs.get("model_root")
            try:
                report = original(*args, **kwargs)
            except Exception as error:
                self.add(self._error_entry(error))
                raise
            self.add(
                {
                    "kind": "import-report",
                    "render": self._rendered(report.render),
                    "markdown": self._rendered(report.render_markdown),
                    "written_paths": [self._relative(path, model_root) for path in report.written_paths],
                }
            )
            return report

        return wrapper

    def wrap_projection(self, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.add({"kind": "projected-swc", "swc": self.norm(_canonical(result.model_dump(mode="json")))})
            return result

        return wrapper

    def wrap_main(self, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            out, err = _Tee(sys.stdout), _Tee(sys.stderr)
            saved = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            code: Any = 0
            try:
                result = original(*args, **kwargs)
                code = 0 if result is None else result
                return result
            except SystemExit as stop:
                code = 0 if stop.code is None else stop.code
                raise
            except BaseException as error:
                code = f"raised {type(error).__name__}"
                raise
            finally:
                sys.stdout, sys.stderr = saved
                if _is_exchange_command(sys.argv[1:]):
                    self.add(
                        {
                            "kind": "cli",
                            "argv": [self.norm(str(item)) for item in sys.argv[1:]],
                            "code": code if isinstance(code, int | str) else str(code),
                            "stdout": self.norm("".join(out.parts)),
                            "stderr": self.norm("".join(err.parts)),
                        }
                    )
                    report = _report_argument(sys.argv[1:])
                    if report is not None and Path(report).is_file():
                        self.add(
                            {
                                "kind": "cli-report-file",
                                "path": self.norm(Path(report).as_posix()),
                                "text": self._file_text(report),
                            }
                        )

        return wrapper

    # -- patching -----------------------------------------------------------------------

    def patch(self) -> None:
        from swcraft_tools import cli
        from swcraft_tools.component_exchange import divergence, import_, report
        from swcraft_tools.writer import arxml_generation

        originals = {
            "generate_component_arxml": (
                arxml_generation.generate_component_arxml,
                self.wrap_generate("component-arxml", arxml_generation.generate_component_arxml),
            ),
            "generate_arxml": (
                arxml_generation.generate_arxml,
                self.wrap_generate("model-arxml", arxml_generation.generate_arxml),
            ),
            "render_export_summary": (report.render_export_summary, self.wrap_summary(report.render_export_summary)),
            "plan_component_import": (import_.plan_component_import, self.wrap_plan(import_.plan_component_import)),
            "import_component": (import_.import_component, self.wrap_import(import_.import_component)),
            "main": (cli.main, self.wrap_main(cli.main)),
            "projected_swc": (divergence.projected_swc, self.wrap_projection(divergence.projected_swc)),
        }
        for module in list(sys.modules.values()):
            namespace = getattr(module, "__dict__", None)
            if not isinstance(namespace, dict):
                continue
            for attribute, value in list(namespace.items()):
                for name, (original, wrapper) in originals.items():
                    if value is original:
                        namespace[attribute] = wrapper
                        self.patched[name] = self.patched.get(name, 0) + 1

    # -- the pytest hooks ----------------------------------------------------------------

    def pytest_collection_finish(self, session: Any) -> None:
        self.set_tmp(session.config._tmp_path_factory.getbasetemp())
        self.patch()

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.current = nodeid

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self.current = None


def _spelled(path: Path) -> str:
    """A pattern for every spelling of *path*: as given and resolved, each character optionally escaped."""
    spellings = {str(path), path.as_posix(), str(path.resolve()), path.resolve().as_posix()}
    alternatives = ["".join(r"\\?" + re.escape(char) for char in spelling) for spelling in spellings]
    return "(?:" + "|".join(sorted(alternatives, key=len, reverse=True)) + ")"


#: The CLI commands whose runs are recorded. Other commands print ports, timings, random directories
#: or commit ids, none of which the component exchange decides.
_EXCHANGE_COMMANDS = (("generate", "arxml"), ("import", "component"))


def _is_exchange_command(argv: list[str]) -> bool:
    words = [str(item) for item in argv]
    return any(tuple(words[i : i + 2]) in _EXCHANGE_COMMANDS for i in range(len(words) - 1))


def _report_argument(argv: list[str]) -> str | None:
    """The ``--report`` PATH of a CLI run (``--report PATH`` or ``--report=PATH``), or ``None``."""
    words = [str(item) for item in argv]
    for index, word in enumerate(words):
        if word == "--report" and index + 1 < len(words):
            return words[index + 1]
        if word.startswith("--report="):
            return word.split("=", 1)[1]
    return None


def _posix_key(value: Any) -> str:
    """A sort key spelling a path in POSIX form on every platform, anything else as ``str``."""
    return value.as_posix() if isinstance(value, PurePath) else str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set | frozenset):
        return sorted(value, key=_posix_key)
    return str(value)


def _canonical(value: Any) -> str:
    # ASCII-escaped: a stream a test fed undecodable bytes carries lone surrogates, which UTF-8 cannot encode.
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=_json_default)


def _example_files(recorder: Recorder, base: Path, roots: tuple[Path, ...]) -> dict[str, str]:
    """Every file under *roots*: its *base*-relative POSIX path and its (normalised) text."""
    files: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=_posix_key):
            if path.is_file():
                files[path.relative_to(base).as_posix()] = recorder._file_text(path)
    return files


def _run_example(recorder: Recorder, base: Path) -> list[str]:
    """Export ``examples/basic`` whole, per component and ``--all-components`` through the CLI.

    Returns the runs of :data:`EXPECTED_EXAMPLE_SUCCESS` that exited non-zero or wrote no file.
    """
    from swcraft_tools import cli

    target = base / "examples-basic"
    batch = base / "examples-basic-batch"
    shutil.copytree(REPO_ROOT / EXAMPLE_FILE, target, ignore=shutil.ignore_patterns("dist", "views"))
    manifest = (target / "swcraft.yaml").as_posix()
    runs = {
        "whole": ["generate", "arxml", "--project", manifest],
        "component-ExampleSWC": ["generate", "arxml", "--project", manifest, "--component", "ExampleSWC"],
        "all-components": [
            "generate",
            "arxml",
            "--project",
            manifest,
            "--all-components",
            "--output-dir",
            batch.as_posix(),
        ],
    }
    roots = (target / "dist", batch)
    failures: list[str] = []
    saved_argv = sys.argv
    try:
        for label, argv in runs.items():
            recorder.current = f"{EXAMPLE_FILE}::{label}"
            sys.argv = ["swcraft", *argv]
            before = _example_files(recorder, base, roots)
            code: Any = 0
            sink_out, sink_err = _Sink(), _Sink()
            with contextlib.redirect_stdout(sink_out), contextlib.redirect_stderr(sink_err):
                try:
                    result = cli.main()
                    code = 0 if result is None else result
                except SystemExit as stop:
                    code = 0 if stop.code is None else stop.code
                except Exception as error:
                    # A regression that makes the CLI raise is an entry, so it shows as a DIFF.
                    code = f"raised {type(error).__name__}"
                    recorder.add(
                        {"kind": "example-error", "type": type(error).__name__, "text": recorder.norm(str(error))}
                    )
            after = _example_files(recorder, base, roots)
            recorder.add({"kind": "example-files", "files": after})
            if label in EXPECTED_EXAMPLE_SUCCESS:
                written = [path for path, text in after.items() if before.get(path) != text]
                if code != 0:
                    failures.append(f"{EXAMPLE_FILE}::{label} exited {code}")
                if not written:
                    failures.append(f"{EXAMPLE_FILE}::{label} wrote no file")
    finally:
        sys.argv = saved_argv
        recorder.current = None
    return failures


class _Sink:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def _file_of(node: str) -> str:
    return node.split("::", 1)[0]


def capture(files: tuple[str, ...], marker: str, extra: list[str], base: Path) -> dict[str, Any]:
    import pytest

    recorder = Recorder()
    argv = [
        *files,
        "-m",
        marker,
        "-p",
        "no:cacheprovider",
        "-q",
        f"--basetemp={(base / 'pytest').as_posix()}",
        *extra,
    ]
    previous = Path.cwd()
    os.chdir(TOOLS_ROOT)
    try:
        status = pytest.main(argv, plugins=[recorder])
    finally:
        os.chdir(previous)
    # The record is kept when a test fails: `--compare` must still name every node whose output moved.
    if status != 0:
        print(f"FAIL: pytest exited {int(status)}")
    recorder.set_tmp(base)
    example_failures = _run_example(recorder, base)
    return {
        "patched": dict(sorted(recorder.patched.items())),
        "nodes": dict(sorted(recorder.nodes.items())),
        "pytest_status": int(status),
        "example_failures": example_failures,
    }


def _summary(record: dict[str, Any], files: tuple[str, ...]) -> tuple[str, dict[str, int], list[str]]:
    digest = hashlib.sha256(_canonical(record["nodes"]).encode("utf-8")).hexdigest()
    per_file: dict[str, int] = {name: 0 for name in (*files, EXAMPLE_FILE)}
    for node, entries in record["nodes"].items():
        key = _file_of(node)
        key = key if key in per_file else (EXAMPLE_FILE if key.startswith(EXAMPLE_FILE) else key)
        per_file[key] = per_file.get(key, 0) + len(entries)
    empty = [name for name, count in per_file.items() if count == 0]
    return digest, per_file, empty


def _print_summary(record: dict[str, Any], files: tuple[str, ...]) -> list[str]:
    digest, per_file, empty = _summary(record, files)
    nodes = record["nodes"]
    print(f"sha256 {digest}")
    print(f"nodes {len(nodes)} entries {sum(len(entries) for entries in nodes.values())}")
    print(f"patched bindings {record['patched']}")
    for name, count in per_file.items():
        print(f"  {count:6d}  {name}")
    for name in empty:
        print(f"FAIL: {name} contributed no entry")
    return empty


def _describe(a: dict[str, Any], b: dict[str, Any]) -> str:
    lines = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            left = json.dumps(a.get(key), indent=1, ensure_ascii=True).splitlines()
            right = json.dumps(b.get(key), indent=1, ensure_ascii=True).splitlines()
            diff = list(difflib.unified_diff(left, right, "baseline", "now", lineterm="", n=1))
            lines.append(f"    field {key!r}:")
            lines.extend(f"      {line}" for line in diff[:40])
    return "\n".join(lines)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    old, new = baseline["nodes"], current["nodes"]
    for node in sorted(set(old) - set(new)):
        failures.append(f"node missing now: {node}")
    for node in sorted(set(new) - set(old)):
        failures.append(f"node new now: {node}")
    for node in sorted(set(old) & set(new)):
        a, b = old[node], new[node]
        if a == b:
            continue
        if len(a) != len(b):
            failures.append(f"{node}: {len(a)} entries in the baseline, {len(b)} now")
        for index, (left, right) in enumerate(zip(a, b, strict=False)):
            if left != right:
                failures.append(f"{node}: entry {index} ({left.get('kind')}) differs\n{_describe(left, right)}")
    return failures


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(record) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--capture", metavar="OUT", help="write the record to OUT")
    action.add_argument("--compare", metavar="BASELINE", help="capture afresh and compare with BASELINE")
    parser.add_argument(
        "--files", nargs="+", help="override the test file list (exploration only; never for a baseline)"
    )
    args = parser.parse_args()

    files = tuple(args.files) if args.files else TEST_FILES
    marker, extra = FAST_DESELECT, []

    # Outside every git work tree (a test asserts "not in a git repository"), one per run so concurrent
    # runs never meet; the prefix keeps it out of the `<mkdtemp>` pattern.
    base = Path(tempfile.mkdtemp(prefix="swcraft-exchange-oracle-"))
    try:
        record = capture(files, marker, extra, base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    empty = _print_summary(record, files)
    # Which tree's code ran: a baseline taken in another worktree must name that worktree here.
    print(f"swcraft_tools from {Path(sys.modules['swcraft_tools'].__file__).parent.as_posix()}")

    tests_failed = record.pop("pytest_status") != 0
    example_failures = record.pop("example_failures")
    for failure in example_failures:
        print(f"FAIL: {failure}")
    if args.capture:
        if tests_failed or empty or example_failures:
            print(
                "no baseline written: a baseline needs a passing run in which every listed file contributed "
                "and every expected example run succeeded"
            )
            return 1
        _write(Path(args.capture), record)
        return 0

    baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
    failures = compare(baseline, record)
    for failure in failures:
        print(f"DIFF: {failure}")
    broken = failures or empty or tests_failed or example_failures
    print(f"{len(failures)} difference(s)" if broken else "identical")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
