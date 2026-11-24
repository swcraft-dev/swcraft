#!/usr/bin/env python3
"""Run the versioned SWCraft scenario contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn

import yaml

ROOT = Path(__file__).resolve().parent
#: The directory every subprocess runs in, the package root: a relative `--swcraft-bin` or
#: `--specimens-bin` path resolves against it.
PACKAGE_ROOT = ROOT.parents[1]
SCENARIO_SCHEMA_VERSION = "1.0"
RESULTS_SCHEMA_VERSION = "2.0"
ALLOWED_OPS = {
    "equals",
    "count_equals",
    "contains",
    "non_empty",
    "stdout_contains",
    "file_non_empty",
    "resource_totals",
    "elements_from_generation",
    "status",
}
PLACEHOLDER = re.compile(r"^\{([a-z][a-z0-9_.-]*)\}$")
ALLOWED_PLACEHOLDERS = {"project", "output", "arxml", "diagrams", "preset.name", "preset.seed"}
ALLOWED_PREFIXES = ("landmarks.", "project.", "architecture.")
SPECIMENS_BIN_VARIABLE = "SWCRAFT_SPECIMENS_BIN"


class ScenarioError(Exception):
    pass


def specimens_bin(option: str | None, environ: Mapping[str, str]) -> str:
    """The `swcraft-specimens` executable: *option* (`--specimens-bin`), else `SWCRAFT_SPECIMENS_BIN`.

    `PATH` is never searched: a bare lookup can resolve to another install's generator, which
    :func:`generator_provenance` would refuse. Without either the run is refused.
    """
    generator = option or environ.get(SPECIMENS_BIN_VARIABLE)
    if not generator:
        _fail(f"the specimen generator is not named; use --specimens-bin or {SPECIMENS_BIN_VARIABLE}")
    return generator


def _fail(message: str) -> NoReturn:
    raise ScenarioError(message)


def generator_provenance(generation: Mapping[str, Any], preset: str, version: str) -> None:
    """Refuse a generation whose sidecar names another generator than the one the results record.

    The results record *version*, the `swcraft-specimens` distribution beside this interpreter; the
    sidecar names the generator that ran, so a passed run never carries a version that did not run.
    The preset needs no check: `run_suite` hands the generator the file whose SHA-256 it records.
    What this does not reach: two installations of the same version whose code differs.
    """
    expected = {"name": "swcraft-specimens", "version": version}
    named = generation.get("generator")
    if named != expected:
        _fail(
            f"{preset}: the generator that ran is {named!r}, not the {expected['name']} {version} beside this "
            "runner whose version the results record; run the runner with that "
            "generator's interpreter"
        )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a mapping")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be a list")
    return value


def _check_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{label}: unknown field(s): {', '.join(unknown)}")


def _validate_placeholder_scalar(value: Any, label: str) -> None:
    if not isinstance(value, str) or ("{" not in value and "}" not in value):
        return
    matches = list(re.finditer(r"\{([a-z][a-z0-9_.-]*)\}", value))
    embedded_allowed = (
        value.startswith("group=")
        and len(matches) == 1
        and matches[0].start() == len("group=")
        and matches[0].end() == len(value)
    )
    if not PLACEHOLDER.fullmatch(value) and not embedded_allowed:
        _fail(f"{label}: placeholder must occupy the whole scalar")
    for match in matches:
        name = match.group(1)
        if name not in ALLOWED_PLACEHOLDERS and not name.startswith(ALLOWED_PREFIXES):
            _fail(f"{label}: unknown placeholder")


def _ref_path(path: str, document: dict[str, Any]) -> Any:
    parts = path.split(".")
    value: Any = document
    for part in parts:
        if isinstance(value, list):
            if not part.isdigit():
                _fail(f"invalid reference path: {path}")
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            _fail(f"unknown reference path: {path}")
    return value


def resolve_reference(
    value: Any, generation: dict[str, Any], preset: str, seed: int, output: Path, project: Path
) -> Any:
    if not isinstance(value, str):
        return value
    match = PLACEHOLDER.fullmatch(value)
    if not match:
        if "{" in value or "}" in value:

            def replace(match: re.Match[str]) -> str:
                return str(resolve_reference("{" + match.group(1) + "}", generation, preset, seed, output, project))

            return re.sub(r"\{([a-z][a-z0-9_.-]*)\}", replace, value)
        return value
    name = match.group(1)
    if name == "project":
        return str(project)
    if name == "output":
        return str(output)
    if name == "arxml":
        return str(output / "dist" / "model.arxml")
    if name == "diagrams":
        return str(output / "views" / "report.txt")
    if name == "preset.name":
        return preset
    if name == "preset.seed":
        return seed
    if not (name in ALLOWED_PLACEHOLDERS or name.startswith(ALLOWED_PREFIXES)):
        _fail(f"unknown placeholder: {{{name}}}")
    return _ref_path(name, generation)


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _fail(f"cannot read scenarios: {exc}")
    root = _mapping(raw, "scenario document")
    _check_keys(root, {"schema_version", "scenarios"}, "scenario document")
    if root.get("schema_version") != SCENARIO_SCHEMA_VERSION:
        _fail(f"scenario schema_version must be {SCENARIO_SCHEMA_VERSION}")
    scenarios = _list(root.get("scenarios"), "scenarios")
    seen: set[str] = set()
    for index, raw_scenario in enumerate(scenarios):
        s = _mapping(raw_scenario, f"scenarios[{index}]")
        _check_keys(
            s,
            {
                "id",
                "description",
                "presets",
                "command",
                "expected_exit_code",
                "format",
                "assertions",
            },
            f"scenario {index}",
        )
        sid = s.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9-]+", sid):
            _fail(f"scenario {index}: invalid id")
        if sid in seen:
            _fail(f"duplicate scenario id: {sid}")
        seen.add(sid)
        if not isinstance(s.get("description"), str) or not s["description"]:
            _fail(f"scenario {sid}: description is required")
        presets = _list(s.get("presets"), f"scenario {sid}.presets")
        if not presets or any(p not in {"small", "medium", "large"} for p in presets):
            _fail(f"scenario {sid}: invalid preset reference")
        command = _list(s.get("command"), f"scenario {sid}.command")
        if not command or any(not isinstance(x, str) or not x for x in command):
            _fail(f"scenario {sid}: command must be a non-empty list of strings")
        if command[0] != "swcraft" or any(x in {"sh", "bash", "zsh", "cmd", "powershell"} for x in command):
            _fail(f"scenario {sid}: forbidden command")
        for ci, token in enumerate(command):
            _validate_placeholder_scalar(token, f"scenario {sid}.command[{ci}]")
        if not isinstance(s.get("expected_exit_code"), int) or s["expected_exit_code"] < 0:
            _fail(f"scenario {sid}: expected_exit_code must be a non-negative integer")
        if s.get("format") not in {"text", "json"}:
            _fail(f"scenario {sid}: format must be text or json")
        assertions = _list(s.get("assertions"), f"scenario {sid}.assertions")
        if not assertions:
            _fail(f"scenario {sid}: at least one assertion is required")
        assertion_ids: set[str] = set()
        for ai, raw_assertion in enumerate(assertions):
            a = _mapping(raw_assertion, f"scenario {sid}.assertions[{ai}]")
            _check_keys(
                a,
                {"id", "op", "path", "expected", "source", "identity", "where", "contains"},
                f"assertion {sid}[{ai}]",
            )
            aid = a.get("id")
            if not isinstance(aid, str) or aid in assertion_ids:
                _fail(f"scenario {sid}: invalid or duplicate assertion id")
            assertion_ids.add(aid)
            if a.get("op") not in ALLOWED_OPS:
                _fail(f"scenario {sid}.{aid}: unknown assertion operator")
            for key in ("path", "expected", "source"):
                if key in a:
                    _validate_placeholder_scalar(a[key], f"scenario {sid}.{aid}.{key}")
            if a["op"] not in {
                "resource_totals",
                "file_non_empty",
                "stdout_contains",
            } and not isinstance(a.get("path"), str):
                _fail(f"scenario {sid}.{aid}: path is required")
    return scenarios


def _assertion(
    actual: Any,
    assertion: dict[str, Any],
    generation: dict[str, Any],
    output: Path,
    stdout: str,
    preset: str,
    seed: int,
    project: Path,
) -> dict[str, Any]:
    op = assertion["op"]
    expected = resolve_reference(assertion.get("expected"), generation, preset, seed, output, project)
    path = assertion.get("path")
    observed: Any = actual
    if path and op not in {"file_non_empty"}:
        observed = _ref_path(path, actual) if isinstance(actual, dict) else None
    ok = False
    detail: dict[str, Any] = {"id": assertion["id"], "operator": op, "path": path}
    if op == "equals":
        ok = observed == expected
    elif op == "count_equals":
        ok = isinstance(observed, list) and len(observed) == expected
        detail["observed_count"] = len(observed) if isinstance(observed, list) else None
    elif op == "contains":
        ok = expected in observed
    elif op == "non_empty":
        ok = bool(observed)
    elif op == "status":
        ok = observed == expected
    elif op == "stdout_contains":
        ok = str(expected) in stdout
    elif op == "file_non_empty":
        file_path = Path(resolve_reference(assertion["path"], generation, preset, seed, output, project))
        ok = file_path.is_file() and file_path.stat().st_size > 0
        detail["file"] = (
            file_path.relative_to(output).as_posix() if file_path.is_relative_to(output) else "outside-output"
        )
    elif op == "resource_totals":
        ok = "FINDINGS 0 error(s), 0 informational" in stdout
        for memory in generation.get("resources", {}).get("memory", []):
            ok = (
                ok
                and f"RESOURCE {memory['memory']}" in stdout
                and f"CONSUMPTION {memory['consumption_bytes']} bytes" in stdout
            )
        detail["checked_memory_totals"] = len(generation.get("resources", {}).get("memory", []))
    elif op == "elements_from_generation":
        source = _ref_path(assertion["source"], generation)
        where = assertion.get("where", {})
        if not isinstance(source, list) or not isinstance(where, dict):
            _fail(f"{assertion['id']}: invalid elements_from_generation definition")
        selected = [
            x[assertion["identity"]]
            for x in source
            if all(
                x.get(k) == resolve_reference(v, generation, preset, seed, output, project) for k, v in where.items()
            )
        ]
        # list/query output is canonicalized by identity: compare the selection as a set, keep the
        # sorted list as evidence.
        selected = sorted(selected)
        ok = sorted(observed) == selected
        detail["expected_elements"] = selected
    if not ok:
        detail["status"] = "failed"
        detail["expected"] = expected
        detail["observed"] = observed
        raise ScenarioError(
            f"{preset}/{assertion.get('_scenario', '?')}: assertion {assertion['id']} failed: "
            f"expected {expected!r}, observed {observed!r}"
        )
    detail["status"] = "passed"
    if op in {
        "equals",
        "count_equals",
        "contains",
        "non_empty",
        "status",
        "elements_from_generation",
    }:
        detail["observed"] = observed
    return detail


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", shell=False, check=False)


def parse_json_response(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"damaged JSON response: {exc}") from None


def _normalise_arguments(arguments: list[str], output: Path, project: Path) -> list[str]:
    """Keep machine-local paths out of the provenance document."""
    values = []
    for argument in arguments:
        if argument == str(project):
            values.append("{project}")
        elif argument == str(output):
            values.append("{output}")
        else:
            values.append(argument)
    return values


def run_suite(
    presets: list[str], swcraft: str, build_root: Path, results_path: Path, generator: Sequence[str]
) -> dict[str, Any]:
    """Generate each preset with *generator* (the command that starts `swcraft-specimens`) and run the suite.

    The runner never spells its own start, so it runs no generator its caller did not name.
    """
    scenarios = load_scenarios(ROOT / "production.yaml")
    build_root = build_root.resolve()
    results_path = results_path.resolve()
    # Generated projects stay below build_root; the results file may sit beside that directory.
    if not results_path.is_relative_to(build_root.parent):
        _fail("results path must be inside the build-root parent")
    build_root.mkdir(parents=True, exist_ok=True)
    version = _run([swcraft, "--version"], PACKAGE_ROOT)
    if version.returncode != 0:
        _fail(f"cannot execute SWCraft: {version.stderr.strip()}")
    # Any SWCraft runs the suite: the generator pins no release. Its version is the installed
    # distribution's, never read from a source tree beside the script.
    try:
        swcraft_specimens_version = metadata.version("swcraft-specimens")
    except metadata.PackageNotFoundError:
        _fail("cannot determine specimen generator version: the swcraft-specimens distribution is not installed")
    # Imported here: the script's `--help` runs without the package installed.
    from swcraft_specimens.packaged import preset_path as packaged_preset

    results: dict[str, Any] = {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "swcraft_specimens_version": swcraft_specimens_version,
        "generator": {"name": "swcraft-specimens-scenarios", "version": "1.0"},
        "swcraft_version": version.stdout.strip(),
        "presets": [],
    }
    for preset in presets:
        # The installed package's preset, handed to the generator by path, so the recorded digest is
        # the input it read even when `--specimens-bin` names another installation.
        preset_path = packaged_preset(preset)
        preset_out = build_root / preset
        if preset_out.exists():
            shutil.rmtree(preset_out)
        generation_cmd = [
            *generator,
            "generate",
            str(preset_path),
            "--out",
            str(preset_out),
        ]
        generated = _run(generation_cmd, PACKAGE_ROOT)
        if generated.returncode != 0:
            _fail(f"{preset}: generation failed: {generated.stderr.strip()}")
        generation = json.loads((preset_out / "generation.json").read_text(encoding="utf-8"))
        generator_provenance(generation, preset, swcraft_specimens_version)
        preset_result: dict[str, Any] = {
            "preset": preset,
            "seed": generation["preset"]["seed"],
            "preset_sha256": hashlib.sha256(preset_path.read_bytes()).hexdigest(),
            "project_tree_sha256": generation["project"]["project_tree_sha256"],
            "scenarios": [],
        }
        for scenario in scenarios:
            if preset not in scenario["presets"]:
                continue
            start = time.perf_counter()
            command = [swcraft if token == "swcraft" else token for token in scenario["command"]]
            command = [
                str(
                    resolve_reference(
                        token,
                        generation,
                        preset,
                        generation["preset"]["seed"],
                        preset_out,
                        preset_out / "swcraft.yaml",
                    )
                )
                for token in command
            ]
            completed = _run(
                command
                + (
                    ["--format", scenario["format"]]
                    if scenario["format"] == "json" and "--format" not in command
                    else []
                ),
                PACKAGE_ROOT,
            )
            actual: Any = {}
            if scenario["format"] == "json":
                actual = parse_json_response(completed.stdout)
            scenario_result: dict[str, Any] = {
                "id": scenario["id"],
                "description": scenario["description"],
                "arguments": _normalise_arguments(command[1:], preset_out, preset_out / "swcraft.yaml"),
                "expected_exit_code": scenario["expected_exit_code"],
                "actual_exit_code": completed.returncode,
                "duration_seconds": round(time.perf_counter() - start, 6),
                "status": "passed",
                "assertions": [],
            }
            try:
                if completed.returncode != scenario["expected_exit_code"]:
                    raise ScenarioError(
                        f"{preset}/{scenario['id']}: "
                        f"expected exit {scenario['expected_exit_code']}, "
                        f"got {completed.returncode}: {completed.stderr.strip()}"
                    )
                for assertion in scenario["assertions"]:
                    a = dict(assertion)
                    a["_scenario"] = scenario["id"]
                    scenario_result["assertions"].append(
                        _assertion(
                            actual,
                            a,
                            generation,
                            preset_out,
                            completed.stdout,
                            preset,
                            generation["preset"]["seed"],
                            preset_out / "swcraft.yaml",
                        )
                    )
            except ScenarioError:
                scenario_result["status"] = "failed"
                scenario_result["stderr"] = completed.stderr.strip()[:500]
                preset_result["scenarios"].append(scenario_result)
                raise
            preset_result["scenarios"].append(scenario_result)
        results["presets"].append(preset_result)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preset", action="append", choices=("small", "medium", "large"), dest="presets")
    group.add_argument("--all-presets", action="store_true")
    parser.add_argument("--swcraft-bin")
    parser.add_argument(
        "--specimens-bin", help=f"the swcraft-specimens executable (default: ${SPECIMENS_BIN_VARIABLE})"
    )
    parser.add_argument("--build-root", type=Path, default=Path("build/scenarios"))
    parser.add_argument("--results", type=Path, default=Path("build/results/scenario-results.json"))
    args = parser.parse_args(argv)
    try:
        swcraft = args.swcraft_bin or os.environ.get("SWCRAFT_BIN") or shutil.which("swcraft")
        if not swcraft:
            _fail("SWCraft not found; use --swcraft-bin or SWCRAFT_BIN")
        generator = specimens_bin(args.specimens_bin, os.environ)
        presets = ["small", "medium", "large"] if args.all_presets else args.presets
        run_suite(presets, swcraft, args.build_root, args.results, [generator])
    except (OSError, ScenarioError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"scenario suite passed: {', '.join(presets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
