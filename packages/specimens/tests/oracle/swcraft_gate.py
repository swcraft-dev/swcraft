"""The external SWCraft gate, shared by every integration test that runs the pinned release.

The oracle is SWCraft itself, run as an external program: every command exits 0, ``check-model
--format json`` reports no finding at all (a text-format ``check-model`` must name
``--warnings-as-errors`` and is judged by its exit code), and ``resource-report`` ends its findings with the full line
``tests/scenarios/run_scenarios.py`` requires.

A passport that orders findings (N unallocated requirements and V stale links) passes ``ordered``,
the exact ``{rule: count}`` multiset ``check-model --format json`` must report: every finding a
warning, each rule exactly its count, no other rule and no error. Without ``ordered`` no finding at
all is allowed. ``ordered_findings`` reads a passport's multiset and ``check_commands`` the commands
that judge it (``--warnings-as-errors`` only where it is empty).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from swcraft_specimens.passport import CONTRACT_MAJOR

# The whole line, so an informational finding fails the gate exactly as it fails the scenarios.
CLEAN_RESOURCE_REPORT = re.compile(r"^FINDINGS 0 error\(s\), 0 informational$", re.MULTILINE)
#: Seconds any one subprocess may run before the test fails rather than hangs.
SUBPROCESS_TIMEOUT = 600


def run_checked(
    command: Sequence[str],
    cwd: Path | None,
    env: Mapping[str, str] | None = None,
    what: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run one command under `SUBPROCESS_TIMEOUT` and require exit 0.

    Its output is captured as UTF-8 text; a failure names `what` (when given) and carries the
    stdout and stderr.
    """
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT,
    )
    prefix = f"{what}: " if what else ""
    assert completed.returncode == 0, f"{prefix}{completed.stdout}\n{completed.stderr}"
    return completed


def _json_format(command: Sequence[str]) -> bool:
    """Whether a command asks for the JSON format, the only one whose stdout is a document."""
    return any(
        argument == "--format=json" or (argument == "--format" and following == "json")
        for argument, following in zip(command, [*command[1:], ""])
    )


def assert_live_passport_equals_sidecar(live: Mapping[str, Any], sidecar: Mapping[str, Any]) -> None:
    """The live `swcraft passport` equals the sidecar's whole passport, `contract_version` by major.

    A contract minor renames and removes nothing, so the generator's `MODEL_CONTRACT_VERSION` and
    SWCraft's own contract version must share the major (`CONTRACT_MAJOR`); the rest is compared as
    canonical text (key order and int/float both count).
    """
    live_version = str(live["contract_version"])
    sidecar_version = str(sidecar["contract_version"])
    assert live_version.split(".", 1)[0] == str(CONTRACT_MAJOR), (
        f"the live passport states contract {live_version}; the specimen generator writes major {CONTRACT_MAJOR}"
    )
    assert sidecar_version.split(".", 1)[0] == str(CONTRACT_MAJOR), sidecar_version

    def rest(document: Mapping[str, Any]) -> str:
        kept = {key: value for key, value in document.items() if key != "contract_version"}
        return json.dumps(kept, ensure_ascii=False)

    assert rest(live) == rest(sidecar)


#: The two rules a passport orders: N unallocated requirements and V stale links.
UNALLOCATED_RULE = "trace.requirement-unallocated"
NEEDS_CONFIRMATION_RULE = "trace.link-needs-confirmation"


def ordered_findings(passport: Mapping[str, Any]) -> dict[str, int]:
    """The warnings a passport document orders: N unallocated requirements and V stale links.

    Each is 0 when the passport states no requirement.
    """
    requirements = passport.get("requirements", {})
    return {
        UNALLOCATED_RULE: int(requirements.get("unallocated", 0)),
        NEEDS_CONFIRMATION_RULE: int(requirements.get("stale_links", 0)),
    }


def check_commands(ordered: Mapping[str, int]) -> list[tuple[str, ...]]:
    """The JSON ``check-model`` and ``validate`` commands.

    ``check-model`` runs with ``--warnings-as-errors`` where ``ordered`` is empty (a text
    ``check-model`` fails on any warning).
    """
    commands: list[tuple[str, ...]] = [("check-model", "--format", "json")]
    if not any(ordered.values()):
        commands.append(("check-model", "--warnings-as-errors"))
    commands.append(("validate",))
    return commands


def _multiset(counts: Mapping[tuple[str, str], int]) -> str:
    """A multiset of `(rule, severity)` as sorted text, `rule (severity): count`."""
    return "{" + ", ".join(f"{r} ({s}): {n}" for (r, s), n in sorted(counts.items()) if n) + "}"


def _assert_ordered_findings(findings: list[dict[str, Any]], ordered: Mapping[str, int]) -> None:
    # A finding lacking a field is still counted (as "None"), so it fails the gate by name.
    reported = Counter((str(finding.get("rule")), str(finding.get("severity"))) for finding in findings)
    expected = Counter({(rule, "warning"): count for rule, count in ordered.items() if count})
    assert reported == expected, f"check-model findings: ordered {_multiset(expected)}, reported {_multiset(reported)}"


def assert_swcraft_clean(
    swcraft: str,
    project: Path,
    commands: Sequence[Sequence[str]],
    cwd: Path | None = None,
    ordered: Mapping[str, int] | None = None,
) -> None:
    """Run each SWCraft ``command`` on ``project`` and require it clean.

    With ``ordered``, a JSON ``check-model`` reports exactly that multiset of warnings.
    """
    for command in commands:
        if command[0] == "check-model" and not _json_format(command):
            assert "--warnings-as-errors" in command, (
                f"{command}: a text-format check-model passes on warnings; the oracle needs "
                "--format json or --warnings-as-errors"
            )
        completed = run_checked([swcraft, *command, "--project", str(project)], cwd, what=str(command))
        if command[0] == "check-model" and _json_format(command):
            findings = json.loads(completed.stdout)["findings"]
            _assert_ordered_findings(findings, ordered or {})
        if command[0] == "resource-report":
            assert CLEAN_RESOURCE_REPORT.search(completed.stdout), completed.stdout
