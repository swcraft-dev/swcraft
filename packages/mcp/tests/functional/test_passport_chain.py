"""The calibration chain's pieces, proven on a committed pair: specimen, paired calls, paired check-model, verdict.

``examples/basic`` stands in for a real export and the ``basic-twin`` specimen, built from its
committed passport, is its twin. The real generator is never started: every ``build_specimen`` call
gets a stub generator through its ``command`` seam, or a variable that names no generator. Held here:
the specimen step's three outcomes and its not-configured sentences; the paired calls and the
agreement rule (each condition failing alone, at ``TWIN_BYTE_TOLERANCE``); name- and text-dependent
calls recorded, never judged; the paired ``check-model`` counts with ``ANNOUNCED_RULES`` outside the
verdict; the verdict forms; the stage's record and its defect branches; and that no rendering of the
sentinel model carries a sentinel or ``zqsen``.

Does not reach: the pair's verdict, which is a measurement and never asserted green; a real export;
the real generator's answer to a real passport; the tolerance itself, unmeasured until the first
real specimen; and data accesses, which the passport does not state, so a model whose runnables
access less than its specimen's is no twin of it in the replies that carry accesses.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import measure_forms_module, specimen_generator_module, take_specimen
from stub_generator import stub_command, write_stub
from swcraft_tools.model_registry import current_registry
from swcraft_tools.passport import compute_passport
from test_passport import NAME_SENTINEL, TEXT_SENTINEL, sentinel_root  # noqa: F401 - the fixture

from tests.functional import real_models
from tests.functional import test_real_models as stage
from tests.functional.conftest import REPO_ROOT
from tests.functional.real_models import (
    ANNOUNCED_RULES,
    CHAIN_GREEN,
    CheckModelCounts,
    Specimen,
    build_specimen,
    calibration_verdict,
    differing_rules,
    not_configured_sentence,
    render_paired_check_model,
    run_check_model,
)

measure_forms = measure_forms_module()
generator = specimen_generator_module()

#: The lowercase prefix both sentinels share.
SENTINEL_PREFIX = "zqsen"

#: The pair's real side, and its twin's name in the pin file.
REAL_SIDE = REPO_ROOT / "examples" / "basic"
TWIN = "basic-twin"

#: The stub rejection's line count.
REJECTED_LINES = 3

#: The name-dependent calls the measurement asks of the pair's real side, in its order.
DEPENDENT_ON_THE_PAIR = [
    "Elements without a description, 1 of 1",
    "Requirements about a topic, 1 of 1",
    "Elements that could realise a requirement, 1 of 1",
    "find 4 names (exact, lower, prefix, misspelt)",
    "find 16 names (the 16 largest SWCs)",
    "find text (two words of the largest SWC's name)",
]


@dataclasses.dataclass(frozen=True)
class Chain:
    """One model through the chain: its passport, specimen, forms and ``check-model`` counts."""

    passport: dict[str, Any]
    specimen: Specimen
    forms: Any
    specimen_forms: Any
    counts: CheckModelCounts
    specimen_counts: CheckModelCounts | None


def _chain(manifest: Path, work: Path, command: list[str]) -> Chain:
    passport = compute_passport(manifest.parent / "model").passport
    specimen = build_specimen(passport, work / "specimen", command=command)
    built = specimen.manifest
    return Chain(
        passport=passport,
        specimen=specimen,
        forms=measure_forms.measure(manifest),
        specimen_forms=None if built is None else measure_forms.measure(built),
        counts=run_check_model(manifest),
        specimen_counts=None if built is None else run_check_model(built),
    )


@pytest.fixture(scope="module")
def stub(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_stub(tmp_path_factory.mktemp("stub"))


def _copies_the_twin(stub: Path) -> list[str]:
    """A stub command that answers exit 0 with a copy of the taken twin specimen."""
    return stub_command(stub, "copy", os.fspath(take_specimen(TWIN)))


@pytest.fixture(scope="module")
def twin(stub: Path, tmp_path_factory: pytest.TempPathFactory) -> Chain:
    """``examples/basic`` (copied, so no step writes the repository) through the chain, its twin built by a stub."""
    work = tmp_path_factory.mktemp("chain")
    shutil.copytree(REAL_SIDE, work / "real", ignore=shutil.ignore_patterns("dist", "views"))
    return _chain(work / "real" / "swcraft.yaml", work, _copies_the_twin(stub))


@pytest.fixture(scope="module")
def sentinel(sentinel_root: Path, stub: Path, tmp_path_factory: pytest.TempPathFactory) -> Chain:  # noqa: F811 - the fixture
    """The sentinel model through the chain, rejected by a stub whose every stderr line carries the sentinel."""
    manifest = sentinel_root.parent / "swcraft.yaml"
    manifest.write_text(generator.manifest_text("sentinel"), encoding="utf-8", newline="\n")
    command = stub_command(stub, "reject", str(REJECTED_LINES), f"{NAME_SENTINEL} {TEXT_SENTINEL}")
    return _chain(manifest, tmp_path_factory.mktemp("sentinel-chain"), command)


def _call(label: str = "read swc count", **overrides: Any) -> Any:
    fields = {"status": measure_forms.OK, "wire_bytes": 1000, "seconds": 0.01, "outcome": "ok (count 3)"}
    fields |= {"code": "", "figures": "count 3"} | overrides
    return measure_forms.Call(label, **fields)


def _names_free(text: str) -> None:
    assert NAME_SENTINEL not in text
    assert TEXT_SENTINEL not in text
    assert SENTINEL_PREFIX not in text.lower()


class TestTheSpecimenStep:
    def test_the_twin_is_built_with_swcrafts_manifest_and_meets_its_passport(self, twin: Chain) -> None:
        assert twin.specimen.built and twin.specimen.defect is None and twin.specimen.rejected_lines is None
        assert twin.specimen.manifest.read_text(encoding="utf-8") == generator.manifest_text("twin")
        assert generator.digest(twin.specimen.manifest.parent / "model") == generator.digest(
            take_specimen(TWIN) / "model"
        )
        # The real side's passport is the twin's source passport, so nothing differs. A source passport
        # stays at its contract through a minor, so `contract_version` is compared by major; the running
        # passport carries the running contract's version, which the real-model stage requires.
        source = json.loads(generator.passport_path(REPO_ROOT, TWIN).read_text(encoding="utf-8"))
        assert twin.passport["contract_version"] == current_registry().contract_version
        assert twin.passport["contract_version"].split(".")[0] == source["contract_version"].split(".")[0]
        assert {k: v for k, v in twin.passport.items() if k != "contract_version"} == {
            k: v for k, v in source.items() if k != "contract_version"
        }
        assert twin.specimen.leaves_differing == ()
        assert twin.specimen.outcome == "built" and twin.specimen.row() == "built"

    @pytest.mark.parametrize(
        "path",
        ["identifiers.interfaces.mean_length", "components.by_declared_level.QM"],
        ids=["a-leaf", "an-upper-case-leaf"],
    )
    def test_a_leaf_the_specimen_misses_is_kept_by_its_path(
        self, twin: Chain, stub: Path, tmp_path: Path, path: str
    ) -> None:
        passport = copy.deepcopy(twin.passport)
        *parents, last = path.split(".")
        node = passport
        for key in parents:
            node = node[key]
        node[last] += 1
        specimen = build_specimen(passport, tmp_path / "specimen", command=_copies_the_twin(stub))
        assert specimen.built and specimen.leaves_differing == (path,)
        assert specimen.outcome == "built, 1 leaves differ"
        assert specimen.row() == f"built, 1 leaves differ: `{path}`"
        verdict = calibration_verdict(specimen, pairs_disagreeing=0, rules_differing=0)
        assert not verdict.green
        assert verdict.summary() == "report-only: 0 pairs disagree, 0 rule counts differ, 1 leaves differ"

    def test_a_rejection_is_counted_by_its_lines_and_never_read(self, twin: Chain, stub: Path, tmp_path: Path) -> None:
        command = stub_command(stub, "reject", str(REJECTED_LINES), f"{NAME_SENTINEL} {TEXT_SENTINEL}")
        specimen = build_specimen(twin.passport, tmp_path / "specimen", command=command)
        assert specimen.rejected_lines == REJECTED_LINES and not specimen.built
        assert specimen.outcome == specimen.row() == f"rejected by the generator: {REJECTED_LINES} lines"
        assert calibration_verdict(specimen, pairs_disagreeing=0, rules_differing=0).summary() == (
            f"no specimen: rejected by the generator: {REJECTED_LINES} lines"
        )
        _names_free(repr(specimen))

    @pytest.mark.parametrize(
        ("arguments", "cause"),
        [(("exit", "1"), "exit 1"), (("empty",), "exit 0 without model/")],
        ids=["exit-1", "exit-0-without-model"],
    )
    def test_any_other_answer_is_a_defect_of_the_chain(
        self, twin: Chain, stub: Path, tmp_path: Path, arguments: tuple[str, ...], cause: str
    ) -> None:
        specimen = build_specimen(twin.passport, tmp_path / "specimen", command=stub_command(stub, *arguments))
        assert not specimen.built and specimen.rejected_lines is None
        assert specimen.defect == specimen.outcome == f"a defect of the generator: {cause}"
        assert specimen.failure == f"the specimen generator failed: {cause}"

    def test_an_executable_that_cannot_start_is_a_defect_named_by_the_exception_s_class(
        self, twin: Chain, tmp_path: Path
    ) -> None:
        absent = tmp_path / f"{SENTINEL_PREFIX}-generator" / "absent"
        specimen = build_specimen(twin.passport, tmp_path / "specimen", command=[os.fspath(absent)])
        assert specimen.defect == "a defect of the generator: FileNotFoundError"
        _names_free(f"{specimen.defect} {specimen.failure}")


class TestNotConfigured:
    """The generator ``specimen_generator.configured`` cannot find: a defect, one sentence naming the variable."""

    @pytest.mark.parametrize(
        ("value", "reason", "said"),
        [
            (f"{SENTINEL_PREFIX}/generator", "relative", "is a relative path"),
            ("ABSOLUTE/absent", "missing", "names no file"),
            ("ABSOLUTE/generator.cmd", "batch-file", "names a batch file"),
        ],
        ids=["relative", "names-no-file", "a-batch-file"],
    )
    def test_a_set_variable_the_generator_cannot_be_started_from(
        self, twin: Chain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, reason: str, said: str
    ) -> None:
        value = value.replace("ABSOLUTE", os.fspath(tmp_path / f"{SENTINEL_PREFIX}Generators"))
        monkeypatch.setenv(generator.VARIABLE, value)
        specimen = build_specimen(twin.passport, tmp_path / "specimen")
        assert specimen.defect == f"not configured: {generator.VARIABLE}"
        assert specimen.failure == not_configured_sentence(reason)
        assert specimen.failure.startswith(f"{generator.VARIABLE} {said}: the calibration builds each specimen")
        assert "run `test-real-models` again." in specimen.failure
        _names_free(f"{specimen.outcome} {specimen.failure} {specimen!r}")
        assert not (tmp_path / "specimen").exists(), "no generator was started"

    def test_unset_and_not_installed(self, twin: Chain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A repository root holding the pin file and no ``.venv``: the unset sentence, verbatim."""
        (tmp_path / generator.PIN_FILE).parent.mkdir(parents=True)
        shutil.copyfile(REPO_ROOT / generator.PIN_FILE, tmp_path / generator.PIN_FILE)
        monkeypatch.delenv(generator.VARIABLE, raising=False)
        specimen = build_specimen(twin.passport, tmp_path / "specimen", repo_root=tmp_path)
        assert specimen.failure == (
            f"{generator.VARIABLE} is not set: the calibration builds each specimen with the specimen "
            "generator (passport in, project out), and .venv holds none; run `dev.py install`, or set the "
            "variable to the generator's absolute path and run `test-real-models` again."
        )

    def test_the_stage_fails_the_export_at_its_specimen_step_with_the_sentence(
        self, twin: Chain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(generator.VARIABLE, os.fspath(tmp_path / f"{SENTINEL_PREFIX}Generator"))
        outcome = stage.Outcome("real model A")
        with pytest.raises(pytest.fail.Exception) as failed:
            stage._specimen_step(outcome, twin.passport, tmp_path)
        assert failed.value.pytrace is False
        assert outcome.stopped == f"red at specimen: {not_configured_sentence('missing')}"
        assert str(failed.value) == f"real model A: {outcome.stopped}"
        assert outcome.specimen is None
        _names_free(str(failed.value))

    def test_a_defect_of_the_generator_fails_the_export_too(
        self, twin: Chain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        defect = Specimen(defect="a defect of the generator: exit 1", failure="the specimen generator failed: exit 1")
        monkeypatch.setattr(stage, "build_specimen", lambda _passport, _root: defect)
        outcome = stage.Outcome("real model A")
        with pytest.raises(pytest.fail.Exception):
            stage._specimen_step(outcome, twin.passport, tmp_path)
        assert outcome.stopped == "red at specimen: the specimen generator failed: exit 1"


class TestThePairedCalls:
    def test_every_call_is_a_pair_in_the_real_model_s_order(self, twin: Chain) -> None:
        recipes, references = measure_forms.paired_calls(
            twin.forms, twin.specimen_forms, tolerance=real_models.TWIN_BYTE_TOLERANCE
        )
        assert [pair.label for pair in recipes] == [call.label for call in twin.forms.recipe_calls]
        assert [pair.label for pair in references] == [call.label for call in twin.forms.reference_calls]
        record = measure_forms.render_paired_record(
            twin.forms, twin.specimen_forms, tolerance=real_models.TWIN_BYTE_TOLERANCE
        )
        rows = [line for line in record.splitlines() if line.startswith("| ") and not line.startswith("| call |")]
        assert [row.split(" | ")[0].removeprefix("| ") for row in rows] == [call.label for call in twin.forms.calls]
        assert "## Recipe forms, paired" in record and "## Reference calls, paired" in record
        assert "The bytes agree within 25% of the real call's." in record

    def test_without_a_specimen_every_specimen_cell_says_so(self, twin: Chain) -> None:
        record = measure_forms.render_paired_record(twin.forms, None, tolerance=real_models.TWIN_BYTE_TOLERANCE)
        rows = [line for line in record.splitlines() if line.startswith("| ") and not line.startswith("| call |")]
        assert len(rows) == len(twin.forms.calls)
        assert all(row.endswith(f" | {measure_forms.NO_SPECIMEN} | not judged: no specimen |") for row in rows)
        assert "Pairs judged" not in record

    def test_a_specimen_asked_other_calls_is_a_defect(self, twin: Chain) -> None:
        other = dataclasses.replace(twin.specimen_forms, reference_calls=twin.specimen_forms.reference_calls[1:])
        with pytest.raises(ValueError, match="not asked the same calls"):
            measure_forms.paired_calls(twin.forms, other, tolerance=real_models.TWIN_BYTE_TOLERANCE)


class TestTheAgreementRule:
    TOLERANCE = real_models.TWIN_BYTE_TOLERANCE

    def test_the_tolerance_is_the_module_s_unmeasured_quarter(self) -> None:
        assert real_models.TWIN_BYTE_TOLERANCE == 0.25

    def test_equal_outcomes_figures_and_near_bytes_agree(self) -> None:
        assert measure_forms.pair_agrees(_call(), _call(wire_bytes=1250), tolerance=self.TOLERANCE)
        assert measure_forms.pair_agrees(_call(), _call(wire_bytes=750), tolerance=self.TOLERANCE)

    @pytest.mark.parametrize(
        "specimen",
        [
            {"status": measure_forms.REFUSED, "code": "over-budget"},
            {"figures": "count 4"},
            {"wire_bytes": 1251},
            {"wire_bytes": 749},
        ],
        ids=["the-outcome", "the-figures", "the-bytes-above", "the-bytes-below"],
    )
    def test_each_condition_failing_alone_disagrees(self, specimen: dict[str, Any]) -> None:
        assert not measure_forms.pair_agrees(_call(), _call(**specimen), tolerance=self.TOLERANCE)

    def test_another_refusal_code_is_another_outcome(self) -> None:
        refused = {"status": measure_forms.REFUSED, "figures": "", "code": "over-budget"}
        assert measure_forms.pair_agrees(_call(**refused), _call(**refused), tolerance=self.TOLERANCE)
        assert not measure_forms.pair_agrees(
            _call(**refused), _call(**refused | {"code": "text-refusal"}), tolerance=self.TOLERANCE
        )

    def test_a_real_call_of_no_bytes_agrees_only_with_none(self) -> None:
        assert measure_forms.pair_agrees(_call(wire_bytes=0), _call(wire_bytes=0), tolerance=self.TOLERANCE)
        assert not measure_forms.pair_agrees(_call(wire_bytes=0), _call(wire_bytes=1), tolerance=self.TOLERANCE)

    def test_both_not_asked_agree(self) -> None:
        not_asked = {"status": measure_forms.NOT_ASKED, "wire_bytes": 0, "figures": "", "outcome": "not asked"}
        assert measure_forms.pair_agrees(_call(**not_asked), _call(**not_asked), tolerance=self.TOLERANCE)

    def test_a_measured_call_carries_its_code_and_figures(self, twin: Chain) -> None:
        for call in twin.forms.calls:
            assert bool(call.code) == (call.status == measure_forms.REFUSED), call.label
            assert not call.figures or call.status == measure_forms.OK, call.label


class TestNameDependentCalls:
    def test_the_list_names_calls_the_measurement_asks(self, twin: Chain) -> None:
        labels = {call.label for call in twin.forms.reference_calls}
        assert labels >= measure_forms.NAME_DEPENDENT_CALLS
        dependent = [call.label for call in twin.forms.calls if measure_forms.name_dependent(call.label)]
        assert dependent == DEPENDENT_ON_THE_PAIR

    def test_every_recipe_asking_by_text_is_never_judged(self) -> None:
        """Read from each recipe's requests, so a new recipe cannot slip past the rule."""
        for recipe in measure_forms.all_recipes():
            asks_by_text = any(
                measure_forms.TEXT_ARGUMENTS & set(request)
                or any(entry.get("field") in measure_forms.TEXT_FIELDS for entry in request.get("where", ()))
                for request in recipe.requests
            )
            assert (recipe.title in measure_forms.text_dependent_recipes()) is asks_by_text, recipe.title
        assert {"Requirements about a topic", "Elements that could realise a requirement"} <= (
            measure_forms.text_dependent_recipes()
        )

    @pytest.mark.parametrize(
        ("request_", "expected"),
        [
            ({"text": "<TOPIC>"}, True),
            ({"for_requirement": "<REQUIREMENT>", "candidates": 3}, True),
            ({"address": {"layer": "swc"}, "where": [{"field": "description", "exists": False}]}, True),
            ({"address": {"layer": "swc"}, "join": [{"with": {"where": [{"field": "title", "equals": "x"}]}}]}, True),
            ({"address": {"layer": "swc"}, "where": [{"field": "name", "equals": "<COMPONENT>"}]}, False),
            ({"name": "<ELEMENT>", "form": "count"}, False),
        ],
        ids=["text", "for-requirement", "description", "joined-title", "name", "impact"],
    )
    def test_follows_text_reads_every_depth(self, request_: dict[str, Any], expected: bool) -> None:
        assert measure_forms.follows_text(request_) is expected

    def test_a_name_dependent_pair_is_recorded_and_never_judged(self, twin: Chain) -> None:
        label = "find 16 names (the 16 largest SWCs)"
        refused = tuple(
            dataclasses.replace(call, status=measure_forms.REFUSED, code="over-budget") if call.label == label else call
            for call in twin.specimen_forms.reference_calls
        )
        specimen = dataclasses.replace(twin.specimen_forms, reference_calls=refused)
        _recipes, references = measure_forms.paired_calls(
            twin.forms, specimen, tolerance=real_models.TWIN_BYTE_TOLERANCE
        )
        (pair,) = [pair for pair in references if pair.label == label]
        assert pair.agrees is None and not pair.judged
        assert pair.agreement() == "recorded, not judged: follows the names' spelling"
        record = measure_forms.render_paired_record(twin.forms, specimen, tolerance=real_models.TWIN_BYTE_TOLERANCE)
        assert "refused `over-budget`" in record


class TestThePairedCheckModel:
    REAL = CheckModelCounts(
        1,
        {"port.undeclared-interface": 3, "mapping-set.unknown-application": 1, "swc.access.unknown-port": 2},
        {"trace.requirement-unallocated": 1},
    )

    def test_the_counts_by_rule_side_by_side(self) -> None:
        specimen = CheckModelCounts(1, {"swc.access.unknown-port": 2}, {"trace.requirement-unallocated": 1})
        table = render_paired_check_model(self.REAL, specimen)
        assert table.splitlines()[2:9] == [
            "| severity | rule | real | specimen |",
            "|---|---|---|---|",
            "| exit | | exit 1 | exit 1 |",
            "| error | mapping-set.unknown-application (a kept unresolved reference, outside the verdict) | 1 | 0 |",
            "| error | port.undeclared-interface (a kept unresolved reference, outside the verdict) | 3 | 0 |",
            "| error | swc.access.unknown-port | 2 | 2 |",
            "| warning | trace.requirement-unallocated | 1 | 1 |",
        ]
        assert "Calibration finding: 4 findings of the unresolved-reference rules have no passport leaf" in table
        assert differing_rules(self.REAL, specimen) == 0

    def test_a_count_outside_the_announced_rules_differs_by_severity_and_rule(self) -> None:
        specimen = CheckModelCounts(0, {}, {"trace.requirement-unallocated": 2, "swc.access.unknown-port": 1})
        # errors swc.access.unknown-port 2 vs 0, warnings swc.access.unknown-port 0 vs 1, and
        # trace.requirement-unallocated 1 vs 2: three rows; the unresolved-reference rows are not among them.
        assert differing_rules(self.REAL, specimen) == 3
        assert set(ANNOUNCED_RULES) & {"port.undeclared-interface", "mapping-set.unknown-application"}

    def test_without_a_specimen_its_column_says_so(self) -> None:
        table = render_paired_check_model(self.REAL, None)
        rows = [line for line in table.splitlines() if line.startswith(("| error", "| warning", "| exit"))]
        assert all(row.endswith(" | not run: no specimen |") for row in rows)

    def test_both_sides_of_the_pair_are_read(self, twin: Chain) -> None:
        """Both documents are read; their difference, if any, is the pair's measurement, never asserted."""
        assert twin.counts.problem is None and twin.specimen_counts is not None
        assert twin.specimen_counts.problem is None


class TestTheVerdict:
    def test_no_specimen_names_the_step_s_outcome(self) -> None:
        verdict = calibration_verdict(Specimen(rejected_lines=2), pairs_disagreeing=0, rules_differing=0)
        assert not verdict.green
        assert verdict.summary() == "no specimen: rejected by the generator: 2 lines"

    def test_a_specimen_is_exactly_one_of_the_three_outcomes(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="a specimen is built"):
            Specimen()
        with pytest.raises(ValueError, match="a specimen is built"):
            Specimen(rejected_lines=1, manifest=tmp_path / "swcraft.yaml")
        with pytest.raises(ValueError, match="a specimen is built"):
            Specimen(rejected_lines=1, defect="a defect of the generator: exit 1")
        with pytest.raises(ValueError, match="only a built specimen"):
            Specimen(rejected_lines=1, leaves_differing=("layers.swc",))

    def test_report_only_counts_the_pairs_the_rules_and_the_leaves(self, twin: Chain) -> None:
        verdict = calibration_verdict(twin.specimen, pairs_disagreeing=3, rules_differing=1)
        assert not verdict.green
        assert verdict.summary() == "report-only: 3 pairs disagree, 1 rule counts differ, 0 leaves differ"
        assert (
            calibration_verdict(twin.specimen, pairs_disagreeing=0, rules_differing=2)
            .summary()
            .startswith("report-only:")
        )

    def test_green_needs_every_count_at_zero(self) -> None:
        assert real_models.Verdict().green and real_models.Verdict().summary() == CHAIN_GREEN
        assert not real_models.Verdict(leaves_differing=1).green

    def test_the_pair_s_verdict_is_one_of_the_forms_and_never_asserted_green(self, twin: Chain) -> None:
        """The pair's verdict is a measurement; only its form is held."""
        verdict = _stage_outcome("real model A", twin).verdict
        assert verdict.no_specimen is None
        assert verdict.summary() == CHAIN_GREEN or verdict.summary().startswith("report-only: ")


def test_no_rendering_of_the_sentinel_model_carries_a_sentinel(sentinel: Chain, twin: Chain) -> None:
    """Every rendering of the chain on the sentinel model, with no specimen and with a foreign one.

    The sentinel's specimen is rejected by a stub whose every stderr line carries both sentinels:
    the rejection is counted, never read, and the paired renderings are taken against the twin's
    specimen as well.
    """
    assert sentinel.specimen.outcome == f"rejected by the generator: {REJECTED_LINES} lines"
    assert sentinel.specimen_forms is None and sentinel.specimen_counts is None
    tolerance = real_models.TWIN_BYTE_TOLERANCE
    renderings = [
        repr(sentinel.specimen),
        sentinel.specimen.row(),
        measure_forms.render_paired_record(sentinel.forms, sentinel.specimen_forms, tolerance=tolerance),
        measure_forms.render_paired_record(sentinel.forms, twin.specimen_forms, tolerance=tolerance),
        render_paired_check_model(sentinel.counts, sentinel.specimen_counts),
        render_paired_check_model(sentinel.counts, twin.counts),
        sentinel.counts.summary(),
        calibration_verdict(sentinel.specimen, pairs_disagreeing=1, rules_differing=1).summary(),
    ]
    for rendering in renderings:
        _names_free(rendering)


def _stage_outcome(label: str, chain: Chain) -> Any:
    """*chain* as the stage's ``Outcome`` of a finished export, its verdict the chain's own."""
    pairs = rules = 0
    if chain.specimen_forms is not None and chain.specimen_counts is not None:
        recipes, references = measure_forms.paired_calls(
            chain.forms, chain.specimen_forms, tolerance=real_models.TWIN_BYTE_TOLERANCE
        )
        pairs = measure_forms.disagreeing([*recipes, *references])
        rules = differing_rules(chain.counts, chain.specimen_counts)
    return stage.Outcome(
        label,
        specimen=chain.specimen,
        forms=chain.forms,
        specimen_forms=chain.specimen_forms,
        counts=chain.counts,
        specimen_counts=chain.specimen_counts,
        verdict=calibration_verdict(chain.specimen, pairs_disagreeing=pairs, rules_differing=rules),
    )


class TestTheRecord:
    """The stage's record (``test_real_models.render_record``), rendered here with a built specimen."""

    def test_a_built_specimen_and_a_rejected_one_render_the_whole_record(self, twin: Chain, sentinel: Chain) -> None:
        built = _stage_outcome("real model A", twin)
        record = stage.render_record([built, _stage_outcome("real model B", sentinel)], "2026-10-24")
        lines = record.splitlines()
        assert lines[0] == "# The passport chain's calibration on 2 real models — 2026-10-24"
        assert lines[2] == stage.RECORD_OPENING
        for heading in ("## Counts", "## real model A", "## real model B", "## Verdict per export"):
            assert heading in lines, heading
        assert lines.count("### Recipe forms, paired") == 2 and lines.count("### check-model, paired") == 2
        (specimen_counts,) = [line for line in lines if line.startswith("| check-model on the specimen |")]
        assert specimen_counts == (
            f"| check-model on the specimen | {twin.specimen_counts.summary()} | not run: no specimen |"
        )
        section_a = record.split("## real model A")[1].split("## real model B")[0]
        assert f"Specimen: {twin.specimen.row()}" in section_a
        assert f"Verdict: {built.verdict.summary()}" in section_a and "Pairs judged: " in section_a
        assert f"- real model A: {built.verdict.summary()}" in lines
        assert f"- real model B: no specimen: rejected by the generator: {REJECTED_LINES} lines" in lines
        section_b = record.split("## real model B")[1]
        assert f"Specimen: rejected by the generator: {REJECTED_LINES} lines" in section_b
        _names_free(record)

    def test_a_run_where_no_export_finished_says_where_each_stopped(self) -> None:
        stopped = stage.Outcome("real model A", stopped="red at import: swcraft import model exited 1")
        record = stage.render_record([stopped, stage.Outcome("real model B")], "2026-10-24")
        assert "No export reached its verdict; the lines below say where each one stopped." in record
        assert "## Counts" not in record and "## real model A" not in record
        assert record.endswith(
            "- real model A: red at import: swcraft import model exited 1\n- real model B: not finished\n"
        )

    def test_a_blocked_verdict_line_says_it_failed(self) -> None:
        outcome = stage.Outcome("real model A", verdict=real_models.Verdict(pairs_disagreeing=1))
        outcome.blocked = True
        assert outcome.line() == (
            "report-only: 1 pairs disagree, 0 rule counts differ, 0 leaves differ; failed: the calibration is blocking"
        )


def _failure_of(action: Any) -> str:
    with pytest.raises(pytest.fail.Exception) as failed:
        action()
    assert failed.value.pytrace is False
    return str(failed.value)


class TestTheStageSteps:
    """The stage's steps' defect branches, each a names-free red at its step."""

    @staticmethod
    def _passport_step(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: object, *, schema: str | None = None
    ) -> tuple[Any, str]:
        def writes(_cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(json.dumps(document), encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(list(argv), 0, "", "")

        monkeypatch.setattr(stage, "run_swcraft", writes)
        if schema is not None:
            monkeypatch.setattr(stage, "schema_text", lambda: schema)
        outcome = stage.Outcome("real model A")
        message = _failure_of(lambda: stage._passport(outcome, tmp_path / "swcraft.yaml", tmp_path))
        assert outcome.stopped is not None and message == f"real model A: {outcome.stopped}"
        return outcome, message

    def test_a_passport_the_schema_refuses(self, twin: Chain, monkeypatch, tmp_path) -> None:
        document = {key: value for key, value in twin.passport.items() if key != "layers"}
        _outcome, message = self._passport_step(monkeypatch, tmp_path, document)
        assert message == "real model A: red at passport: the passport does not validate against its schema"

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"swc": NAME_SENTINEL}, "3 string leaves, where the two versions are the only ones"),
            ({"swc": True}, "a leaf other than the two versions is not a number"),
        ],
        ids=["a-string-leaf", "a-bool-leaf"],
    )
    def test_a_leaf_that_is_not_a_number(
        self, twin: Chain, monkeypatch, tmp_path, change: dict[str, Any], reason: str
    ) -> None:
        """The schema forbids both; a permissive one shows the stage's own check under it."""
        document = copy.deepcopy(twin.passport)
        document["layers"] |= change
        _outcome, message = self._passport_step(monkeypatch, tmp_path, document, schema="{}")
        assert message == f"real model A: red at passport: {reason}"
        _names_free(message)

    def test_a_foreign_contract(self, twin: Chain, monkeypatch, tmp_path) -> None:
        document = copy.deepcopy(twin.passport) | {"contract_version": "0.0.1"}
        _outcome, message = self._passport_step(monkeypatch, tmp_path, document, schema="{}")
        assert message == "real model A: red at passport: its contract_version is not the running contract's"

    def test_a_call_answering_an_error_that_is_no_refusal(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(measure_forms, "measure", lambda _manifest: SimpleNamespace(errors=("read swc count",)))
        outcome = stage.Outcome("real model A")
        message = _failure_of(lambda: stage._measured(outcome, tmp_path / "swcraft.yaml", "the import"))
        assert message == (
            "real model A: red at forms: the import: 1 call(s) answered an error, not a refusal: read swc count"
        )

    def test_an_exception_inside_a_step_is_named_by_its_class_with_no_context(self) -> None:
        def raises() -> None:
            raise RuntimeError(f"{NAME_SENTINEL} {TEXT_SENTINEL}")

        outcome = stage.Outcome("real model A")
        with pytest.raises(pytest.fail.Exception) as failed:
            stage._step(outcome, "specimen", raises)
        assert str(failed.value) == "real model A: red at specimen: raised RuntimeError"
        assert failed.value.__context__ is None, "the red carries no exception context"
        assert outcome.stopped == "red at specimen: raised RuntimeError"
