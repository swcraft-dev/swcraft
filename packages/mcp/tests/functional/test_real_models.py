"""The real-export tier: the passport chain's calibration, one case per export ``SWCRAFT_REAL_MODELS`` names.

Every case is ``real_models``-marked; its one stage is ``dev.py test-real-models``, in no CI job,
since the exports are machine-local. The cases and the gate's three states are ``real_models.py``'s.
The stage is report-only until the chain is green on every export (a defect of the chain still
fails an export); ``real_models.CALIBRATION_BLOCKING`` is the switch.

Each export runs, in a temporary directory, after a first **read** step (an unreadable export is a
red at ``read``, never left out):

1. **import** -- ``swcraft init`` and ``swcraft import model``; only their exit codes are kept.
2. **passport** -- ``swcraft passport``: it validates against the schema, every leaf but the two
   versions is a number, and ``contract_version`` is the running contract's.
3. **specimen** -- ``real_models.build_specimen``: built, rejected, or a defect that fails the export.
4. **forms** -- ``measure_forms.measure`` on the import, and on the specimen when built; a call
   answering an error that is not a refusal fails.
5. **check-model** -- counted by severity and rule; an unreadable document fails.
6. **the verdict** -- ``no specimen: ...``, ``chain green`` or ``report-only: ...``; under
   ``CALIBRATION_BLOCKING`` a verdict that is not green fails the export.

A session finalizer writes the figures-only record, ``dist/real-models/record.md`` (or
``SWCRAFT_REAL_MODELS_RECORD``), replaced whole by every run that reached an export.
No name is ever printed: every subprocess runs captured, every check is reduced to a boolean or a
count, a red's message carries the label, the step, counts, rule codes and leaf paths only, every
exception is reported by its class alone, a warning is swallowed and counted, and no ``assert``
compares a value that may hold a name. A hang has no timeout but the generator's own ceiling.
A hang has no timeout but the generator's own ceiling.
"""

from __future__ import annotations

import datetime
import json
import os
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import pytest
from conftest import measure_forms_module
from jsonschema import Draft202012Validator
from swcraft_tools import __version__
from swcraft_tools.model_registry import current_registry
from swcraft_tools.passport import schema_text

from tests.functional import real_models
from tests.functional.conftest import REPO_ROOT
from tests.functional.real_models import (
    TWIN_BYTE_TOLERANCE,
    CheckModelCounts,
    RealModel,
    Specimen,
    Verdict,
    build_specimen,
    calibration_verdict,
    cases,
    differing_rules,
    render_paired_check_model,
    require_real_model,
    run_check_model,
    run_swcraft,
)

pytestmark = pytest.mark.real_models

#: The variable that moves the record out of the repository's ``dist/`` (the privacy suite's).
RECORD_VAR = "SWCRAFT_REAL_MODELS_RECORD"

#: Where the record goes by default: gitignored, replaced whole by every run that reached an export.
DEFAULT_RECORD = REPO_ROOT / "dist" / "real-models" / "record.md"

#: The record's first sentence, verbatim.
RECORD_OPENING = (
    "The real models are a calibration set for the passport chain, not references: a difference below "
    "is a property of the chain to be explained, never a defect of the real model to be allowed for. "
    "A specimen is a model generated from a passport, whose properties are known exactly."
)

#: **Test-only.** ``<point>:<text>`` makes the procedure raise or warn with *text* at *point* --
#: ``fixture`` (set-up), ``finalizer`` (teardown, after the record is written), ``specimen`` (the
#: specimen step) or ``warning`` (the forms step) -- or, at ``blocking``, takes
#: ``CALIBRATION_BLOCKING`` as ``True``: the privacy suite's monkeypatch of a stage run in a
#: subprocess. Inert when unset; never set by ``dev.py``.
FAULT_VAR = "SWCRAFT_REAL_MODELS_FAULT"

_T = TypeVar("_T")


@dataclass
class Outcome:
    """What one export came to: filled step by step, read by the finalizer."""

    label: str
    stopped: str | None = None
    """``red at <step>: <reason>`` (the label not repeated), once a defect of the chain stopped it."""
    specimen: Specimen | None = None
    forms: Any = None
    specimen_forms: Any = None
    counts: CheckModelCounts | None = None
    specimen_counts: CheckModelCounts | None = None
    verdict: Verdict | None = None
    blocked: bool = False
    """Whether the verdict failed the export, the calibration being blocking."""
    warnings: int = 0

    @property
    def built_specimen(self) -> Path | None:
        """The built specimen's manifest; ``None`` before step 3 or when no specimen was built."""
        return None if self.specimen is None else self.specimen.manifest

    def line(self) -> str:
        """The record's verdict line: the red, the verdict, or ``not finished`` for an interrupted run."""
        if self.stopped is not None:
            text = self.stopped
        elif self.verdict is not None:
            text = self.verdict.summary() + ("; failed: the calibration is blocking" if self.blocked else "")
        else:
            text = "not finished"
        return text + (f"; {self.warnings:,} warnings, not shown" if self.warnings else "")


def _fault_point() -> tuple[str, str]:
    """:data:`FAULT_VAR` as ``(point, text)``; two empty strings when it is unset."""
    where, _, text = os.environ.get(FAULT_VAR, "").partition(":")
    return where, text


def _fault(point: str) -> None:
    """Raise, or warn, with :data:`FAULT_VAR`'s text when it names *point*; nothing when it is unset."""
    where, text = _fault_point()
    if where != point:
        return
    if point == "warning":
        warnings.warn(text, stacklevel=2)
    else:
        raise RuntimeError(text)


def _stop(outcome: Outcome, step: str, reason: str) -> NoReturn:
    outcome.stopped = f"red at {step}: {reason}"
    pytest.fail(f"{outcome.label}: {outcome.stopped}", pytrace=False)


def _step(outcome: Outcome, step: str, action: Callable[[], _T]) -> _T:
    """*action*'s value; an exception is a red at *step* named by its class, a warning is counted.

    The red is raised outside the ``except`` clause, so the failure carries no exception context.
    """
    raised: str | None = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            value = action()
        except Exception as exc:  # noqa: BLE001 - reported by its class alone, which names nothing
            raised = type(exc).__name__
    outcome.warnings += len(caught)
    if raised is not None:
        _stop(outcome, step, f"raised {raised}")
    return value


def _read(outcome: Outcome, model: RealModel) -> None:
    """The first step: the export opens for reading, or the case is a red at ``read`` by the exception's class."""

    def opened() -> None:
        with model.path.open("rb") as export:
            export.read(1)

    _step(outcome, "read", opened)


def _import(outcome: Outcome, model: RealModel, work: Path) -> Path:
    """Step 1: a fresh project and the whole export imported into it; its manifest, the exit codes alone kept."""
    project = work / "project"
    initialised = _step(outcome, "import", lambda: run_swcraft(work, "init", str(project)))
    if initialised.returncode != 0:
        _stop(outcome, "import", f"swcraft init exited {initialised.returncode}")
    manifest = project / "swcraft.yaml"
    imported = _step(
        outcome,
        "import",
        lambda: run_swcraft(work, "import", "model", "--source", str(model.path), "--project", str(manifest)),
    )
    if imported.returncode != 0:
        _stop(outcome, "import", f"swcraft import model exited {imported.returncode}")
    return manifest


def _leaves(document: Any, at: str = "") -> list[tuple[str, Any]]:
    if isinstance(document, dict):
        return [leaf for key, value in document.items() for leaf in _leaves(value, f"{at}/{key}")]
    if isinstance(document, list):
        return [leaf for index, value in enumerate(document) for leaf in _leaves(value, f"{at}/{index}")]
    return [(at, document)]


def _passport(outcome: Outcome, manifest: Path, work: Path) -> dict[str, Any]:
    """Step 2: ``swcraft passport``, its document checked against the schema and the numeric rule."""
    output = work / "passport.json"
    made = _step(
        outcome,
        "passport",
        lambda: run_swcraft(manifest.parent, "passport", "--project", str(manifest), "--output", str(output)),
    )
    if made.returncode != 0:
        _stop(outcome, "passport", f"swcraft passport exited {made.returncode}")
    passport = _step(outcome, "passport", lambda: json.loads(output.read_text(encoding="utf-8")))
    validator = Draft202012Validator(json.loads(schema_text()))
    if not isinstance(passport, dict) or not validator.is_valid(passport):
        _stop(outcome, "passport", "the passport does not validate against its schema")
    leaves = _leaves(passport)
    strings = sorted(at for at, value in leaves if isinstance(value, str))
    if strings != ["/contract_version", "/passport_version"]:
        _stop(outcome, "passport", f"{len(strings)} string leaves, where the two versions are the only ones")
    numeric = (value for at, value in leaves if at not in strings)
    if not all(isinstance(value, int | float) and not isinstance(value, bool) for value in numeric):
        _stop(outcome, "passport", "a leaf other than the two versions is not a number")
    if passport["contract_version"] != current_registry().contract_version:
        _stop(outcome, "passport", "its contract_version is not the running contract's")
    return passport


def _blocking() -> bool:
    """``real_models.CALIBRATION_BLOCKING``, or ``True`` under :data:`FAULT_VAR`'s ``blocking`` point."""
    return real_models.CALIBRATION_BLOCKING or _fault_point()[0] == "blocking"


def _specimen(passport: dict[str, Any], work: Path) -> Specimen:
    """Step 3's action: the specimen of the passport, or the rejection; any exception is a defect."""
    _fault("specimen")
    return build_specimen(passport, work / "specimen")


def _specimen_step(outcome: Outcome, passport: dict[str, Any], work: Path) -> Specimen:
    """Step 3: the specimen, or a red at ``specimen`` with the specimen's closed sentence on a defect."""
    specimen = _step(outcome, "specimen", lambda: _specimen(passport, work))
    if specimen.defect is not None:
        _stop(outcome, "specimen", specimen.failure or specimen.defect)
    return specimen


def _measured(outcome: Outcome, manifest: Path, whose: str) -> Any:
    """Every recipe form and reference call on *manifest*; an error that is not a refusal is a defect."""

    def measured() -> Any:
        _fault("warning")
        return measure_forms_module().measure(manifest)

    measurement = _step(outcome, "forms", measured)
    if measurement.errors:
        labels = "; ".join(measurement.errors)
        _stop(
            outcome, "forms", f"{whose}: {len(measurement.errors)} call(s) answered an error, not a refusal: {labels}"
        )
    return measurement


def _forms(outcome: Outcome, manifest: Path) -> None:
    """Step 4: the forms on the import always, on the specimen when it was built."""
    outcome.forms = _measured(outcome, manifest, "the import")
    if outcome.built_specimen is not None:
        outcome.specimen_forms = _measured(outcome, outcome.built_specimen, "the specimen")


def _counted(outcome: Outcome, manifest: Path, whose: str) -> CheckModelCounts:
    counts = _step(outcome, "check-model", lambda: run_check_model(manifest))
    if counts.problem is not None:
        _stop(outcome, "check-model", f"{whose}: {counts.summary()}")
    return counts


def _check_model(outcome: Outcome, manifest: Path) -> None:
    """Step 5: ``check-model`` on the import always, on the specimen when it was built; counted only."""
    outcome.counts = _counted(outcome, manifest, "the import")
    if outcome.built_specimen is not None:
        outcome.specimen_counts = _counted(outcome, outcome.built_specimen, "the specimen")


def _verdict(outcome: Outcome) -> None:
    """Step 6: the export's verdict; a failure only when the calibration is blocking."""
    specimen = outcome.specimen
    assert specimen is not None and outcome.counts is not None  # set by steps 3 and 5
    pairs, rules = 0, 0
    if outcome.specimen_forms is not None and outcome.specimen_counts is not None:
        measure_forms = measure_forms_module()
        recipes, references = _step(
            outcome,
            "verdict",
            lambda: measure_forms.paired_calls(outcome.forms, outcome.specimen_forms, tolerance=TWIN_BYTE_TOLERANCE),
        )
        pairs = measure_forms.disagreeing([*recipes, *references])
        rules = differing_rules(outcome.counts, outcome.specimen_counts)
    outcome.verdict = calibration_verdict(specimen, pairs_disagreeing=pairs, rules_differing=rules)
    if _blocking() and not outcome.verdict.green:
        outcome.blocked = True
        pytest.fail(f"{outcome.label}: {outcome.verdict.summary()}; the calibration is blocking", pytrace=False)


def _demoted(section: str) -> list[str]:
    """*section*'s lines, each ``## `` heading one level down, under its export's heading."""
    return [f"#{line}" if line.startswith("## ") else line for line in section.rstrip("\n").splitlines()]


def _export_section(outcome: Outcome) -> list[str]:
    measure_forms = measure_forms_module()
    assert outcome.specimen is not None and outcome.counts is not None and outcome.verdict is not None
    forms = measure_forms.render_paired_record(outcome.forms, outcome.specimen_forms, tolerance=TWIN_BYTE_TOLERANCE)
    return [
        f"## {outcome.label}",
        "",
        f"Specimen: {outcome.specimen.row()}",
        "",
        *_demoted(forms),
        "",
        *_demoted(render_paired_check_model(outcome.counts, outcome.specimen_counts)),
        "",
        f"Verdict: {outcome.verdict.summary()}",
        "",
    ]


def render_record(outcomes: list[Outcome], date: str) -> str:
    """The calibration record: the counts, each finished export's pairs and verdict, every verdict line.

    Figures, closed codes, leaf paths and this repository's text only.
    """
    finished = [outcome for outcome in outcomes if outcome.stopped is None and outcome.verdict is not None]
    noun = "real model" if len(outcomes) == 1 else "real models"
    lines = [
        f"# The passport chain's calibration on {len(outcomes)} {noun} — {date}",
        "",
        RECORD_OPENING,
        "",
        f"Built with SWCraft {__version__}. Figures only: counts, byte sizes, seconds, closed codes and "
        "passport leaf paths, and no element name, no value and no text of a model. No LLM was run. The "
        "labels follow the sorted order of the export file names, and move when an export is added "
        "before another, so a record is compared with another by its counts first. A call pair agrees "
        f"on the same outcome and figures with bytes within {TWIN_BYTE_TOLERANCE:.0%} of the real "
        "call's, an unmeasured tolerance; seconds are one call's, which is not a measurement.",
        "",
    ]
    if finished:
        labels = [outcome.label for outcome in finished]
        facts = [
            ("check-model on the import", [outcome.counts.summary() for outcome in finished]),
            (
                "check-model on the specimen",
                [
                    measure_forms_module().NO_SPECIMEN
                    if outcome.specimen_counts is None
                    else outcome.specimen_counts.summary()
                    for outcome in finished
                ],
            ),
            ("Specimen", [outcome.specimen.outcome for outcome in finished]),
        ]
        lines += [
            "## Counts",
            "",
            *measure_forms_module().counts_table([outcome.forms for outcome in finished], labels, facts),
            "",
        ]
        for outcome in finished:
            lines += _export_section(outcome)
    else:
        lines += ["No export reached its verdict; the lines below say where each one stopped.", ""]
    lines += ["## Verdict per export", "", *(f"- {outcome.label}: {outcome.line()}" for outcome in outcomes)]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="session")
def real_models_record() -> Iterator[list[Outcome]]:
    """The outcomes of this run's exports, written as the record when the session ends, if any ran.

    A run that reached no export -- the unset skip -- writes nothing, so it never replaces a record.
    """
    _fault("fixture")
    outcomes: list[Outcome] = []
    yield outcomes
    if not outcomes:
        return
    failed: str | None = None
    try:
        path = Path(os.environ.get(RECORD_VAR) or DEFAULT_RECORD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_record(outcomes, datetime.date.today().isoformat()), encoding="utf-8", newline="\n")
    except Exception as exc:  # noqa: BLE001 - reported by its class alone, which names nothing
        failed = type(exc).__name__
    if failed is not None:
        pytest.fail(f"the real-models record could not be written: raised {failed}", pytrace=False)
    _fault("finalizer")


@pytest.mark.parametrize("case", cases())
def test_real_model(
    case: RealModel | None, tmp_path: Path, real_models_record: list[Outcome], pytestconfig: pytest.Config
) -> None:
    model = require_real_model(case, required=bool(pytestconfig.getoption(real_models.FLAG)))
    outcome = Outcome(model.label)
    real_models_record.append(outcome)
    _read(outcome, model)
    manifest = _import(outcome, model, tmp_path)
    passport = _passport(outcome, manifest, tmp_path)
    outcome.specimen = _specimen_step(outcome, passport, tmp_path)
    _forms(outcome, manifest)
    _check_model(outcome, manifest)
    _verdict(outcome)
