"""The real-export tier's access gate: ``SWCRAFT_REAL_MODELS``, its three states, and the cases it yields.

``SWCRAFT_REAL_MODELS`` names a **directory**: every regular file directly in it whose name ends
``.arxml`` in any case is one export (a symlink is followed; a subdirectory, a dotfile such as an
AppleDouble ``._<name>.arxml`` or any other file is not), labelled ``real model A``, ``B``, ... in
sorted POSIX-name order. An unreadable export is a case like any other and fails at its ``read`` step.
A relative value is resolved before any path reaches a subprocess. **The labels move** when an export
is added earlier in that order, so records are compared across runs by their counts first.

**No file or directory name is ever printed or written** -- not in a skip reason, a failure, a test
id or a ``repr``: the exports are private. Messages name the variable, never its value.

The three states:

* the variable is **unset** -> skip, naming the variable;
* the variable is **set** and names no readable directory holding at least one export -> **fail**;
* ``--require-real-models`` is passed and the variable is unset -> **fail**.

:func:`cases` yields one case per export, or the placeholder ``no-real-models``; the decision runs in
the test body (:func:`require_real_model`), because a ``skipif`` marker could never fail.

The module also holds the calibration chain's pure pieces -- *real model -> passport -> specimen ->
the same calls on both*: :func:`build_specimen` and its three outcomes (:class:`Specimen`),
:func:`run_swcraft`, :func:`count_check_model`, :func:`render_paired_check_model`,
:func:`calibration_verdict` and :data:`CALIBRATION_BLOCKING`. A difference is a property of the
chain, never a defect of the real model. Nothing compares an export against a digest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools.cli import MANIFEST_UNKNOWN_KEY_RULE
from swcraft_tools.passport import compute_passport
from swcraft_tools.validator import CONSISTENCY_RULES

#: The variable a host sets to the directory holding its real exports.
ENV_VAR = "SWCRAFT_REAL_MODELS"

#: The ``dev.py test-real-models`` / pytest flag that turns the unset skip into a failure.
FLAG = "--require-real-models"

#: The id of the one case collected when there is no export to run.
PLACEHOLDER_ID = "no-real-models"

#: The name the calibration's specimen is generated under (its manifest's): an import's *twin*.
TWIN_PASSPORT_NAME = "twin"


@dataclass(frozen=True)
class RealModel:
    """One export: its label, and its path kept out of every rendering."""

    label: str
    """``real model A``, ``real model B``, ... by the export's place in sorted POSIX-name order."""
    path: Path = field(repr=False)
    """The export. Never printed, never written: only the procedure opens it."""

    @property
    def case_id(self) -> str:
        """The test id: ``real-model-A``."""
        return self.label.replace(" ", "-")


def _is_export_name(name: str) -> bool:
    """A name ending ``.arxml`` in any case, and not a dotfile (AppleDouble metadata would shift every label)."""
    return name.lower().endswith(".arxml") and not name.startswith(".")


def _exports(directory: Path) -> list[Path] | None:
    """The exports directly in *directory*, absolute, in sorted POSIX-name order; ``None`` if it cannot be read.

    Every export, readable or not. Absolute, because the procedure runs ``swcraft`` in a temporary
    directory. **Any** exception is ``None``: this runs at collection, which
    :func:`withhold_exception_text` does not reach, so an escaping exception would print its text
    (``Path.resolve`` names the path on a symlink loop).
    """
    try:
        directory = directory.resolve()
        if not directory.is_dir():
            return None
        found = [path for path in directory.iterdir() if _is_export_name(path.name) and path.is_file()]
        return sorted(found, key=lambda path: path.as_posix())
    except Exception:  # noqa: BLE001 - collection-time: every exception is the names-free failure
        return None


def _labelled(configured: str) -> list[RealModel] | None:
    """The exports *configured* names, labelled; ``None`` when discovering or labelling them raised.

    Caught whole for :func:`_exports`' reason: discovery runs at collection.
    """
    exports = _exports(Path(configured))
    if exports is None:
        return None
    try:
        # Imported late: the tools tests are not on the path yet when the mcp conftest imports this.
        from conftest import measure_forms_module  # noqa: PLC0415

        letters = measure_forms_module().label_letters
        return [RealModel(label=f"real model {letters(index)}", path=path) for index, path in enumerate(exports)]
    except Exception:  # noqa: BLE001 - collection-time: every exception is the names-free failure
        return None


def real_models(environ: Mapping[str, str] | None = None) -> list[RealModel]:
    """The exports :data:`ENV_VAR` names, labelled; empty when it is unset or names nothing usable."""
    configured = (os.environ if environ is None else environ).get(ENV_VAR)
    if not configured:
        return []
    return _labelled(configured) or []


def cases() -> list[Any]:
    """The parametrization: one ``pytest.param`` per export, or the placeholder alone."""
    found = real_models()
    if not found:
        return [pytest.param(None, id=PLACEHOLDER_ID)]
    return [pytest.param(model, id=model.case_id) for model in found]


def require_real_model(case: RealModel | None, *, required: bool) -> RealModel:
    """*case* when it is an export; otherwise skip -- or **fail** -- by the three states.

    *required* is whether :data:`FLAG` was passed. Every message names :data:`ENV_VAR`, never its
    value, which is as private as the exports.
    """
    if case is not None:
        return case
    configured = os.environ.get(ENV_VAR)
    if configured:
        if _labelled(configured) is None:
            pytest.fail(
                f"{ENV_VAR} is set but names no readable directory, or discovering its exports raised "
                "(the exception withheld). This run asked for the "
                "real-export tier and could not make it, which is a failure and not an absence.",
                pytrace=False,
            )
        pytest.fail(
            f"{ENV_VAR} names a directory holding no *.arxml file directly in it. This run asked "
            "for the real-export tier and could not make it, which is a failure and not an absence.",
            pytrace=False,
        )
    reason = (
        f"{ENV_VAR} is not set -- this tier only runs against the real, machine-local exports "
        f"(never committed). Set {ENV_VAR} to the directory holding them to run it."
    )
    if required:
        pytest.fail(f"{FLAG} was passed, but this test cannot run: {reason}", pytrace=False)
    pytest.skip(reason)
    raise AssertionError("unreachable")  # pragma: no cover -- pytest.skip raises


def withhold_exception_text(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """Replace an uncaught exception of a ``real_models`` test by its class name, before it is reported.

    The ``-r`` short summary prints an unexpected exception's text even under ``--tb=no``, and that
    text may hold a name of the export. Called from ``pytest_runtest_makereport``; the exception
    becomes a ``pytest.fail`` naming its class and phase. A skip, an xfail and a ``pytest.fail`` are
    left alone. Does not reach a collection error, nor an exception that ends the session
    (``KeyboardInterrupt``, ``pytest.exit``).
    """
    excinfo = call.excinfo
    if excinfo is None or item.get_closest_marker("real_models") is None:
        return
    if excinfo.errisinstance((pytest.skip.Exception, pytest.fail.Exception, pytest.xfail.Exception)):
        return
    try:
        pytest.fail(f"raised {excinfo.typename} in {call.when}, its text withheld", pytrace=False)
    except pytest.fail.Exception:
        call.excinfo = pytest.ExceptionInfo.from_current()


# ---------------------------------------------------------------------------
# The calibration chain's pieces: the specimen, check-model counted, the verdict
# ---------------------------------------------------------------------------

#: How far a specimen's call may be from the real call in wire bytes, as a fraction of the real
#: call's, and still agree. **Unmeasured**. Not zero: a row's bytes move with texts the passport does
#: not record (names, descriptions, requirement titles) while its count does not.
TWIN_BYTE_TOLERANCE = 0.25

#: The rules of the unresolved references a clean import keeps (a path the export spelled and
#: declared nowhere). No passport carries one, so no specimen reproduces these findings: they are
#: counted apart (:attr:`CheckModelCounts.announced`) and never decide the verdict.
ANNOUNCED_RULES = frozenset(
    {
        "port.undeclared-interface",
        "composition.port-undeclared-interface",
        "mapping-set.unknown-application",
        "mapping-set.unknown-implementation",
    }
)

#: A rule outside the closed vocabulary, which may be any text.
UNRECOGNISED_RULE = "unrecognised-rule"

#: The rules a count keeps by name.
_RECOGNISED_RULES = frozenset(CONSISTENCY_RULES) | {MANIFEST_UNKNOWN_KEY_RULE}

#: The schema version of ``check-model --format json`` this reading knows.
CHECK_MODEL_SCHEMA_VERSION = 1

#: The verdict of an export whose specimen was built and agrees everywhere it is judged.
CHAIN_GREEN = "chain green"

#: Whether an export whose verdict is not :data:`CHAIN_GREEN` fails the stage. ``False`` (report-only,
#: failing on a defect of the chain alone) until the chain is green on every export.
CALIBRATION_BLOCKING = False


@dataclass(frozen=True)
class CheckModelCounts:
    """What ``check-model --format json`` said of one model, reduced to codes and counts.

    Counted by occurrence and rule, errors and warnings apart. ``problem`` is a closed sentence when
    the document could not be read (a defect of the chain). No field carries text of the model.
    """

    exit_code: int
    errors_by_rule: Mapping[str, int]
    warnings_by_rule: Mapping[str, int]
    problem: str | None = None

    @property
    def errors(self) -> int:
        return sum(self.errors_by_rule.values())

    @property
    def warnings(self) -> int:
        return sum(self.warnings_by_rule.values())

    @property
    def announced(self) -> int:
        """The findings of :data:`ANNOUNCED_RULES`, which no passport leaf carries."""
        return sum(
            count
            for by_rule in (self.errors_by_rule, self.warnings_by_rule)
            for rule, count in by_rule.items()
            if rule in ANNOUNCED_RULES
        )

    def summary(self) -> str:
        """The counts as one names-free line: the exit code, then the findings by severity and rule."""
        if self.problem is not None:
            return f"exit {self.exit_code}; {self.problem}"
        parts = [f"exit {self.exit_code}"]
        for noun, by_rule in (("error", self.errors_by_rule), ("warning", self.warnings_by_rule)):
            part = f"{sum(by_rule.values()):,} {noun} finding(s)"
            if by_rule:
                part += " (" + ", ".join(f"{rule} {count:,}" for rule, count in sorted(by_rule.items())) + ")"
            parts.append(part)
        return "; ".join(parts)


def _unreadable(exit_code: int, problem: str) -> CheckModelCounts:
    return CheckModelCounts(exit_code, {}, {}, problem=problem)


def count_check_model(findings_document: str, *, exit_code: int) -> CheckModelCounts:
    """``check-model --format json``'s stdout, counted by severity and rule.

    By occurrence, never deduplicated (the harness's ``finding_pairs`` would merge findings on one
    path). Every failure to read the document is a closed sentence, never the text.
    """
    try:
        document = json.loads(findings_document)
    except ValueError:
        return _unreadable(exit_code, "the check-model document is not JSON")
    if not isinstance(document, dict) or document.get("schemaVersion") != CHECK_MODEL_SCHEMA_VERSION:
        return _unreadable(exit_code, "the check-model document is not the schemaVersion 1 document")
    findings = document.get("findings")
    if not isinstance(findings, list) or not all(isinstance(finding, dict) for finding in findings):
        return _unreadable(exit_code, "the check-model document carries no findings list")
    counts: dict[str, Counter[str]] = {"error": Counter(), "warning": Counter()}
    for finding in findings:
        rule, severity = finding.get("rule"), finding.get("severity")
        if severity not in counts:
            return _unreadable(exit_code, "a check-model finding carries no known severity")
        counts[severity][rule if isinstance(rule, str) and rule in _RECOGNISED_RULES else UNRECOGNISED_RULE] += 1
    return CheckModelCounts(exit_code, dict(counts["error"]), dict(counts["warning"]))


def run_swcraft(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """``swcraft`` *argv* from this interpreter in *cwd*, its output captured and never echoed."""
    return subprocess.run(  # noqa: S603 - this interpreter, a fixed module, fixed arguments
        [sys.executable, "-m", "swcraft_tools.cli", *argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_check_model(manifest: Path) -> CheckModelCounts:
    """``swcraft check-model --format json`` on the project of *manifest*, from this interpreter, counted.

    Its output is captured and never echoed; only :func:`count_check_model`'s counts leave here.
    """
    checked = run_swcraft(manifest.parent, "check-model", "--project", str(manifest), "--format", "json")
    return count_check_model(checked.stdout, exit_code=checked.returncode)


def rule_rows(real: CheckModelCounts, specimen: CheckModelCounts | None) -> list[tuple[str, str, int, int | None]]:
    """``(severity, rule, real count, specimen count)`` for every rule either side reports, sorted.

    The specimen's count is ``None`` without a specimen, and ``0`` for a rule it does not report.
    """
    rows = []
    sides = (
        ("error", real.errors_by_rule, None if specimen is None else specimen.errors_by_rule),
        ("warning", real.warnings_by_rule, None if specimen is None else specimen.warnings_by_rule),
    )
    for severity, real_counts, specimen_counts in sides:
        for rule in sorted({*real_counts, *(specimen_counts or {})}):
            rows.append(
                (
                    severity,
                    rule,
                    real_counts.get(rule, 0),
                    None if specimen_counts is None else specimen_counts.get(rule, 0),
                )
            )
    return rows


def differing_rules(real: CheckModelCounts, specimen: CheckModelCounts) -> int:
    """How many ``(severity, rule)`` counts differ between the two, outside :data:`ANNOUNCED_RULES`."""
    return sum(
        1
        for _severity, rule, real_count, specimen_count in rule_rows(real, specimen)
        if rule not in ANNOUNCED_RULES and real_count != specimen_count
    )


def _no_specimen() -> str:
    """``measure_forms.NO_SPECIMEN``, the specimen column's cell when none was built."""
    from conftest import measure_forms_module  # noqa: PLC0415 - real_models()' reason

    return measure_forms_module().NO_SPECIMEN


def render_paired_check_model(real: CheckModelCounts, specimen: CheckModelCounts | None) -> str:
    """The paired ``check-model`` table: the counts by rule side by side, and the calibration finding.

    A rule of :data:`ANNOUNCED_RULES` is marked as outside the verdict.
    """
    no_specimen = _no_specimen()
    specimen_exit = no_specimen if specimen is None else f"exit {specimen.exit_code}"
    lines = [
        "## check-model, paired",
        "",
        "| severity | rule | real | specimen |",
        "|---|---|---|---|",
        f"| exit | | exit {real.exit_code} | {specimen_exit} |",
    ]
    for severity, rule, real_count, specimen_count in rule_rows(real, specimen):
        shown = f"{rule} (a kept unresolved reference, outside the verdict)" if rule in ANNOUNCED_RULES else rule
        specimen_cell = no_specimen if specimen_count is None else f"{specimen_count:,}"
        lines.append(f"| {severity} | {shown} | {real_count:,} | {specimen_cell} |")
    lines += [
        "",
        f"Calibration finding: {real.announced:,} findings of the unresolved-reference rules have no passport leaf, so "
        "no specimen reproduces them; they are excluded from the verdict.",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Specimen:
    """What the specimen step came to: one of three outcomes, figures and leaf paths only.

    Exactly one is set: ``manifest`` (built; ``leaves_differing`` the passport leaves it misses),
    ``rejected_lines`` (the generator's exit 2, its stderr counted, never read) or ``defect`` (not
    configured or any other answer; ``failure`` the stage's sentence).
    """

    manifest: Path | None = field(default=None, repr=False)
    leaves_differing: tuple[str, ...] = ()
    rejected_lines: int | None = None
    defect: str | None = None
    failure: str | None = None

    def __post_init__(self) -> None:
        if sum((self.manifest is not None, self.rejected_lines is not None, self.defect is not None)) != 1:
            raise ValueError("a specimen is built, rejected by the generator or a defect of the chain")
        if self.leaves_differing and self.manifest is None:
            raise ValueError("only a built specimen has leaves that differ")

    @property
    def built(self) -> bool:
        return self.manifest is not None

    @property
    def outcome(self) -> str:
        """``built``, ``built, <n> leaves differ``, ``rejected by the generator: <n> lines`` or the defect."""
        if self.rejected_lines is not None:
            return f"rejected by the generator: {self.rejected_lines} lines"
        if self.defect is not None:
            return self.defect
        if self.leaves_differing:
            return f"built, {len(self.leaves_differing)} leaves differ"
        return "built"

    def row(self) -> str:
        """The record's specimen row: the outcome, then the differing leaf paths of a built specimen."""
        if self.leaves_differing:
            return self.outcome + ": " + ", ".join(f"`{path}`" for path in self.leaves_differing)
        return self.outcome


#: What every not-configured sentence of the calibration says first, after the reason.
_NOT_CONFIGURED_COMMON = "the calibration builds each specimen with the specimen generator (passport in, project out)"

#: The sentence for each reason the generator is not configured. The variable's value is never in
#: it: it is a path, as private as the exports.
_NOT_CONFIGURED = {
    "unset": (
        "is not set",
        ", and .venv holds none; run `dev.py install`, or set the variable to the generator's absolute "
        "path and run `test-real-models` again.",
    ),
    "relative": (
        "is a relative path",
        ", started by its absolute path; set the variable to the generator's absolute path and run "
        "`test-real-models` again.",
    ),
    "missing": (
        "names no file",
        "; set the variable to the generator's absolute path, or unset it to use the one `dev.py install` "
        "installs, and run `test-real-models` again.",
    ),
    "batch-file": (
        "names a batch file",
        ", which is started without cmd.exe; set the variable to the generator's executable and run "
        "`test-real-models` again.",
    ),
}


def not_configured_sentence(reason: str) -> str:
    """The stage's sentence for a :class:`GeneratorNotConfigured` *reason*, naming the variable only."""
    head, tail = _NOT_CONFIGURED[reason]
    return f"{_generator().VARIABLE} {head}: {_NOT_CONFIGURED_COMMON}{tail}"


def _generator() -> Any:
    """``scripts/specimen_generator.py``, loaded by path (:func:`real_models`' reason for the late import)."""
    from conftest import specimen_generator_module  # noqa: PLC0415

    return specimen_generator_module()


def build_specimen(
    passport: dict[str, Any], root: Path, *, command: list[str] | None = None, repo_root: Path | None = None
) -> Specimen:
    """The specimen of *passport*, generated at *root* (absent or empty) by the specimen generator.

    *passport* has been validated. *command* is the tests' seam; by default the configured
    generator, else the ``.venv`` install of *repo_root* (this checkout when ``None``). Outcomes:

    - **built** -- the generated model's passport is compared with *passport* on every leaf it
      states and the differing leaf paths kept: the generator is compared, never trusted;
    - **rejected** -- exit 2, kept as a count of stderr lines, which carry a real model's figures;
    - **a defect** -- not configured, or any other answer (its exit code or exception class).

    No text of the generator is read. An exception raised here is the caller's: a defect of the chain.
    """
    from conftest import REPO_ROOT  # noqa: PLC0415 - real_models()' reason
    from passport_support import leaves_differing  # noqa: PLC0415 - real_models()' reason

    generator = _generator()
    try:
        command = generator.generator_command(command, None, REPO_ROOT if repo_root is None else repo_root)
    except generator.GeneratorNotConfigured as refused:
        return Specimen(defect=f"not configured: {generator.VARIABLE}", failure=not_configured_sentence(refused.reason))
    answered = generator.run_document(command, passport, root, TWIN_PASSPORT_NAME)
    if isinstance(answered, generator.Rejected):
        return Specimen(rejected_lines=answered.lines)
    if not isinstance(answered, generator.Built):
        cause = answered.cause
        return Specimen(
            defect=f"a defect of the generator: {cause}",
            failure=f"the specimen generator failed: {cause}",
        )
    computed = compute_passport(root / "model").passport
    differing = tuple(path for path, _stated, _computed in leaves_differing(passport, computed))
    return Specimen(manifest=root / "swcraft.yaml", leaves_differing=differing)


@dataclass(frozen=True)
class Verdict:
    """One export's verdict: no specimen, or the disagreeing pairs, differing rule counts and leaves.

    ``no_specimen`` is the specimen step's outcome when none was built; the counts are then unread.
    """

    no_specimen: str | None = None
    pairs_disagreeing: int = 0
    rules_differing: int = 0
    leaves_differing: int = 0

    @property
    def green(self) -> bool:
        """Whether the specimen was built, meets its passport and agrees everywhere it is judged."""
        return (
            self.no_specimen is None
            and self.pairs_disagreeing == 0
            and self.rules_differing == 0
            and self.leaves_differing == 0
        )

    def summary(self) -> str:
        """``no specimen: ...``, :data:`CHAIN_GREEN`, or ``report-only: ...``."""
        if self.no_specimen is not None:
            return f"no specimen: {self.no_specimen}"
        if self.green:
            return CHAIN_GREEN
        return (
            f"report-only: {self.pairs_disagreeing} pairs disagree, {self.rules_differing} rule counts differ, "
            f"{self.leaves_differing} leaves differ"
        )


def calibration_verdict(specimen: Specimen, *, pairs_disagreeing: int, rules_differing: int) -> Verdict:
    """One export's :class:`Verdict`.

    Neither count is read without a built specimen.
    """
    if not specimen.built:
        return Verdict(no_specimen=specimen.outcome)
    return Verdict(
        pairs_disagreeing=pairs_disagreeing,
        rules_differing=rules_differing,
        leaves_differing=len(specimen.leaves_differing),
    )
