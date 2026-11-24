"""The shared external gate helper, run against a stand-in executable (no SWCraft needed).

`check-model --format json` prints one JSON document whose findings must be empty; every other
`check-model` run (`--warnings-as-errors` in text format) prints a human-readable success line,
which the helper must not parse as JSON: its exit code is the verdict.

The real-passport test (integration) runs the pinned release on a specimen of every passport under
`SWCRAFT_SPECIMENS_REAL_PASSPORTS`, a directory outside the repository; it is skipped, naming the
variable, when that is unset.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.specimen.leaves import stated_leaves
from tests.oracle.generator_start import generator_command
from tests.oracle.swcraft_gate import (
    SUBPROCESS_TIMEOUT,
    assert_swcraft_clean,
    check_commands,
    ordered_findings,
)

ROOT = Path(__file__).parents[1]

STAND_IN = """\
import sys

args = sys.argv[1:]
json_format = "--format=json" in args or any(
    a == "--format" and b == "json" for a, b in zip(args, args[1:])
)
if args[0] == "check-model" and json_format:
    sys.stdout.write({json_document!r})
elif args[0] == "check-model":
    sys.stdout.write("Validation passed (no artifacts generated).\\n")
    if {text_exit} and "--warnings-as-errors" in args:
        sys.stdout.write("1 warning(s)\\n")
        sys.exit({text_exit})
else:
    sys.stdout.write("ok\\n")
"""


def _stand_in(tmp_path: Path, json_document: str, text_exit: int = 0) -> str:
    script = tmp_path / "swcraft_stand_in.py"
    script.write_text(
        STAND_IN.format(json_document=json_document, text_exit=text_exit),
        encoding="utf-8",
        newline="\n",
    )
    launcher = tmp_path / "swcraft"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    return str(launcher)


COMMANDS = [("check-model", "--format", "json"), ("check-model", "--warnings-as-errors")]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
def test_a_text_format_check_model_is_judged_by_its_exit_code(tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, '{"findings": []}\n')
    assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", COMMANDS)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
def test_a_json_finding_still_fails_the_gate(tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, '{"findings": [{"rule": "x"}]}\n')
    with pytest.raises(AssertionError, match="check-model findings"):
        assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", COMMANDS)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
def test_the_equals_spelling_of_the_json_format_is_parsed(tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, '{"findings": [{"rule": "x"}]}\n')
    with pytest.raises(AssertionError, match="check-model findings"):
        assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", [("check-model", "--format=json")])
    clean = _stand_in(tmp_path, '{"findings": []}\n')
    assert_swcraft_clean(clean, tmp_path / "swcraft.yaml", [("check-model", "--format=json")])


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
def test_a_text_format_check_model_exiting_non_zero_fails_the_gate(tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, '{"findings": []}\n', text_exit=1)
    with pytest.raises(AssertionError, match="warning"):
        assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", COMMANDS)


def test_a_text_format_check_model_without_warnings_as_errors_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="passes on warnings"):
        assert_swcraft_clean("unused", tmp_path / "swcraft.yaml", [("check-model",)])


# --- the findings a passport orders ---------------------------------------------------------------

UNALLOCATED_RULE = "trace.requirement-unallocated"
NEEDS_CONFIRMATION_RULE = "trace.link-needs-confirmation"
#: The multiset `large-bench` orders: N = 4 unallocated requirements, V = 2 stale links.
ORDERED = {UNALLOCATED_RULE: 4, NEEDS_CONFIRMATION_RULE: 2}
ORDERED_FINDINGS = [(rule, "warning") for rule, count in ORDERED.items() for _ in range(count)]


def _findings(*findings: tuple[str, str]) -> str:
    """A `check-model --format json` document holding the findings `(rule, severity)`."""
    return json.dumps(
        {
            "schemaVersion": 1,
            "findings": [
                {"rule": rule, "severity": severity, "message": "m", "subjects": [], "target": None}
                for rule, severity in findings
            ],
        }
    )


JSON_ONLY = [("check-model", "--format", "json")]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
def test_exactly_the_ordered_warnings_pass_the_gate(tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, _findings(*reversed(ORDERED_FINDINGS)))
    assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", JSON_ONLY, ordered=ORDERED)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
@pytest.mark.parametrize(
    "findings",
    [
        pytest.param(ORDERED_FINDINGS[1:], id="one warning fewer"),
        pytest.param([*ORDERED_FINDINGS, (UNALLOCATED_RULE, "warning")], id="one warning more"),
        pytest.param([*ORDERED_FINDINGS[1:], ("hardware.missing-budget", "warning")], id="another rule"),
        pytest.param([(UNALLOCATED_RULE, "error"), *ORDERED_FINDINGS[1:]], id="the rule as an error"),
    ],
)
def test_any_other_multiset_fails_the_gate_naming_it(findings: list[tuple[str, str]], tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, _findings(*findings))
    with pytest.raises(AssertionError) as raised:
        assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", JSON_ONLY, ordered=ORDERED)
    message = str(raised.value)
    assert "ordered" in message and "reported" in message
    assert f"{UNALLOCATED_RULE} (warning): 4" in message
    assert f"{NEEDS_CONFIRMATION_RULE} (warning): 2" in message


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a POSIX shell script")
@pytest.mark.parametrize("severity", ["warning", "error"])
def test_without_an_order_any_finding_still_fails_the_gate(severity: str, tmp_path: Path) -> None:
    swcraft = _stand_in(tmp_path, _findings((UNALLOCATED_RULE, severity)))
    with pytest.raises(AssertionError, match="check-model findings"):
        assert_swcraft_clean(swcraft, tmp_path / "swcraft.yaml", JSON_ONLY)


def test_a_passport_orders_its_n_and_v_and_only_an_empty_order_adds_warnings_as_errors() -> None:
    ordered = ordered_findings({"requirements": {"unallocated": 4, "stale_links": 2}})
    assert ordered == ORDERED
    assert check_commands(ordered) == [("check-model", "--format", "json"), ("validate",)]
    empty = ordered_findings({"layers": {}})
    assert empty == {UNALLOCATED_RULE: 0, NEEDS_CONFIRMATION_RULE: 0}
    assert check_commands(empty) == [
        ("check-model", "--format", "json"),
        ("check-model", "--warnings-as-errors"),
        ("validate",),
    ]


# --- the real passports ---------------------------------------------------------------------------

SWCRAFT = os.environ.get("SWCRAFT_BIN")
#: A directory of real model passports (`*.json`) outside the repository. They are not the
#: generator's to publish, so the test names them by sorted position only and reports figures, never a file name.
REAL_PASSPORTS = os.environ.get("SWCRAFT_SPECIMENS_REAL_PASSPORTS")


def _real_passports() -> list[Path]:
    return sorted(Path(REAL_PASSPORTS).glob("*.json"), key=lambda path: path.as_posix()) if REAL_PASSPORTS else []


def _differences(stated: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """The leaves where the live passport differs from the stated one, as figures only."""
    expected, actual = stated_leaves(stated), stated_leaves(live)
    return [
        f"{path}: stated {json.dumps(expected.get(path))}, live {json.dumps(actual.get(path))}"
        for path in sorted(set(expected) | set(actual))
        if json.dumps(expected.get(path)) != json.dumps(actual.get(path))
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "position",
    range(1, len(_real_passports()) + 1) if _real_passports() else [0],
    ids=lambda position: f"passport {position}" if position else "no passports",
)
def test_a_real_passport_builds_a_specimen_whose_live_passport_equals_it(position: int, tmp_path: Path) -> None:
    """A specimen of each real passport builds clean and meets the real passport leaf for leaf.

    It is built with no refusal; `check-model` reports nothing and `validate` passes; and
    `swcraft passport` on the specimen equals the real passport leaf for leaf (and as canonical
    text).
    """
    if not REAL_PASSPORTS:
        pytest.skip("SWCRAFT_SPECIMENS_REAL_PASSPORTS is not set: no real passport is read")
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    assert position, "SWCRAFT_SPECIMENS_REAL_PASSPORTS names a directory with no *.json passport"
    # A copy under a neutral name, so no output of either program carries the file's own name.
    passport = tmp_path / "passport.json"
    passport.write_bytes(_real_passports()[position - 1].read_bytes())
    stated = json.loads(passport.read_text(encoding="utf-8"))
    out = tmp_path / "specimen"
    started = time.perf_counter()
    built = subprocess.run(
        [*generator_command(), "specimen", str(passport), "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT,
    )
    seconds = time.perf_counter() - started
    assert built.returncode == 0, f"passport {position} refused: {built.stderr}"
    # A passport stating N or V orders that many warnings; the gate reads them from it.
    ordered = ordered_findings(stated)
    assert_swcraft_clean(SWCRAFT, out / "swcraft.yaml", check_commands(ordered), cwd=ROOT, ordered=ordered)
    completed = subprocess.run(
        [SWCRAFT, "passport", "--project", str(out / "swcraft.yaml")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    live = json.loads(completed.stdout)
    differences = _differences(stated, live)
    assert differences == [], f"passport {position}: " + "; ".join(differences)
    assert json.dumps(live) == json.dumps(stated), f"passport {position}: key order or number type"
    print(f"passport {position}: {len(stated_leaves(stated))} leaves compared, 0 differences, built in {seconds:.1f} s")
