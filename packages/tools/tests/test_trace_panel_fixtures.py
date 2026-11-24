"""The VS Code panel's shared fixtures: what the Python side writes, the TypeScript side reads.

Each file under ``fixtures/trace_panel/`` is one half of a cross-language oracle: Python generates or
checks it here, and ``vscode_extension/src/traceCore.test.ts`` reads it, so the two sides cannot drift
unseen.

- ``canonical_proposals.json`` -- a proposals file written by the search's own dump
  (:func:`~swcraft_tools.trace_search.proposals_json`) from a synthetic run with a non-ASCII title, every
  evidence kind, a ``chosen`` entry, an awaiting link and a target holding two members of one requirement;
  the panel must serialise it to the same bytes;
- ``pending_vectors.json`` -- proposals files and their pending counts, held to
  :func:`~swcraft_tools.trace_records.pending`;
- ``evidence_sentences.json`` -- evidence records and the sentence ``report.md`` renders for them;
- ``cli_answers.json`` -- the CLI's own output the panel parses (a ``--documents`` answer, a search's text,
  an Apply dry run's and an applied run's text, a dry run with nothing to do), each with its exit code; the
  scratch project directory is written as ``/work/project`` with ``/`` separators, the same on every platform.

``ticked_proposals.json`` runs the other way: ``traceCore.test.ts`` writes it once
(``UPDATE_TRACE_FIXTURES=1 npm run test``) from the canonical file and a fixed list of edits, compares its own
output with it on every run, and the strict reader reads it here.

The generated files are **change detectors, never correctness proofs**: regenerate them deliberately, after
reading the difference, with ``python packages/tools/tests/test_trace_panel_fixtures.py`` -- never to make a
red run green. The generation fixes ``model.digest`` and ``model.swcraftVersion``, so neither a release nor
the YAML writer's bytes move them. Every model is synthetic.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from swcraft_tools import trace_records
from swcraft_tools.project import require_project
from swcraft_tools.trace_search import _evidence_text, proposals_json, run_trace
from trace_models import OUT, TOP, ApplyProject, apply_model, confirming, showcase_shaped, write_project

FIXTURES = Path(__file__).parent / "fixtures" / "trace_panel"
CANONICAL = FIXTURES / "canonical_proposals.json"
PENDING = FIXTURES / "pending_vectors.json"
EVIDENCE = FIXTURES / "evidence_sentences.json"
ANSWERS = FIXTURES / "cli_answers.json"

#: Where the scratch project directory is said to be in ``cli_answers.json``.
WORK = "/work/project"

#: The fixed values of the generated file's ``model`` record.
FIXTURE_VERSION = "0.0.0+fixture"
FIXTURE_DIGEST = "sha256:" + "0" * 64

#: The requirement whose title is non-ASCII (and not Cyrillic, by the language gate).
TITLED = ("BRK-7", "Pad temperature above 600 °C")

#: A requirement whose title holds the characters JSON escapes, or a writer could spell differently.
ESCAPED = ("BRK-8", 'Say "stop" \\ now\tthen\u2028next \U0001f697')

#: The target the canonical file chooses for ``BRK-5`` outside its rows, and a second one the vectors choose.
CHOSEN = {"kind": "memory", "owner": None, "name": "Ram"}
CHOSEN_TOO = {"kind": "processor", "owner": None, "name": "Mcu"}


def _panel_model():
    """The showcase-shaped model, plus a non-ASCII title and a second member of ``BRK-6`` on one target."""
    model = showcase_shaped()
    identifier, title = TITLED
    model.requirement(
        identifier, title, "The brake pad temperature shall be monitored above 600 °C.", document="Brake Spec"
    )
    model.link("swc/app/BrakeController.yaml", "BRK-6")
    # Every escape the two writers must agree on: a quote, a backslash, a tab, U+2028 and an astral character.
    model.requirement(ESCAPED[0], ESCAPED[1], "The brake shall stop on request.", document="Brake Spec")
    return model


def _entry(document: dict[str, Any], identifier: str) -> dict[str, Any]:
    return next(entry for entry in document["requirements"] if entry["id"] == identifier)


def canonical_document() -> dict[str, Any]:
    """The canonical proposals document.

    A labelling and a review document, two candidates per requirement, one ``chosen`` entry, ``model`` fixed."""
    with tempfile.TemporaryDirectory() as scratch:
        manifest, config = write_project(
            _panel_model(), Path(scratch) / "project", labelling=["System Spec"], labelling_share=0.5, candidates=2
        )
        run = run_trace(require_project(str(manifest)), config)
        document = json.loads(run.proposals_path.read_text(encoding="utf-8"))
    document["model"] = {**document["model"], "digest": FIXTURE_DIGEST, "swcraftVersion": FIXTURE_VERSION}
    entry = _entry(document, "BRK-5")
    assert all(row["target"] not in (CHOSEN, CHOSEN_TOO) for row in entry["candidates"])
    entry["chosen"].append({"target": dict(CHOSEN)})
    return document


# ---------------------------------------------------------------------------
# The pending vectors
# ---------------------------------------------------------------------------


def _vector_base(canonical: dict[str, Any]) -> dict[str, Any]:
    """Two entries of the canonical file, each cut to its first row.

    ``BRK-6`` (two awaiting links) and ``BRK-5``, no choice."""
    base = copy.deepcopy(canonical)
    base["requirements"] = [copy.deepcopy(_entry(canonical, "BRK-6")), copy.deepcopy(_entry(canonical, "BRK-5"))]
    for entry in base["requirements"]:
        entry["candidates"] = entry["candidates"][:1]
    base["requirements"][1]["chosen"] = []
    return base


def _row(document: dict[str, Any], **slots: Any) -> None:
    document["requirements"][0]["candidates"][0].update(slots)


def _second_row(document: dict[str, Any], **slots: Any) -> None:
    document["requirements"][1]["candidates"][0].update(slots)


def _link(document: dict[str, Any], **slots: Any) -> None:
    document["requirements"][0]["linked"][0].update(slots)


def _choice(document: dict[str, Any], target: dict[str, Any] = CHOSEN, **slots: Any) -> None:
    document["requirements"][1]["chosen"].append({"target": dict(target), **slots})


_MARKS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("", {}),
    (", applied", {"applied": True}),
    (", stale", {"stale": "gone"}),
)


def _vectors() -> Iterator[tuple[str, Callable[[dict[str, Any]], None], int]]:
    yield "nothing decided", lambda d: None, 0
    for suffix, marks in _MARKS:
        open_ = 0 if marks else 1
        yield f"a row accepted{suffix}", lambda d, m=marks: _row(d, decision="accept", **m), open_
        yield f"a row rejected{suffix}", lambda d, m=marks: _row(d, decision="reject", **m), open_
        yield (
            f"a row accepted from a preselection{suffix}",
            lambda d, m=marks: _row(d, decision="accept", preselected=True, **m),
            open_,
        )
        yield f"an awaiting link confirmed{suffix}", lambda d, m=marks: _link(d, decision="confirm", **m), open_
        yield f"a chosen entry{suffix}", lambda d, m=marks: _choice(d, **m), open_

    def only_done(document: dict[str, Any]) -> None:
        _row(document, decision="accept", applied=True)
        _second_row(document, decision="reject", stale="gone")
        _link(document, decision="confirm", applied=True)
        _choice(document, stale="gone")

    yield "only applied and stale items", only_done, 0

    def mixed(document: dict[str, Any]) -> None:
        _row(document, decision="accept")
        _second_row(document, decision="reject")
        _link(document, decision="confirm")
        _choice(document)
        _choice(document, CHOSEN_TOO, applied=True)
        document["requirements"][0]["linked"][1].update(decision="confirm", stale="gone")

    yield "mixed", mixed, 4


def pending_vectors(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    """Each vector: a name, a whole proposals document and its pending count."""
    vectors = []
    for name, edit, count in _vectors():
        document = _vector_base(canonical)
        edit(document)
        vectors.append({"name": name, "pending": count, "proposals": document})
    return vectors


# ---------------------------------------------------------------------------
# The evidence sentences
# ---------------------------------------------------------------------------


def _shape(record: dict[str, Any]) -> tuple[Any, ...]:
    """What changes a record's sentence: its kind, whether it has examples, and a singular count."""
    kind = record["kind"]
    return (
        kind,
        bool(record.get("examples")),
        record.get("of") == 1 if kind == "neighbours" else record.get("count") == 1,
    )


def evidence_sentences(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    """One record of each shape the canonical file holds, then crafted shapes it lacks, then a list of two and none."""
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in canonical["requirements"]:
        for row in entry["candidates"]:
            for record in (*row["group"]["evidence"], *row["evidence"]):
                found.setdefault(_shape(record), record)
    crafted = (
        {"kind": "neighbours", "share": 1, "seeded": 1, "of": 1, "examples": []},
        {"kind": "neighbours", "share": {"n": 2, "d": 3}, "seeded": 2, "of": 3, "examples": ["SR-1", "SR-2"]},
        {"kind": "similar", "score": {"n": 1500000, "d": 1000000}, "count": 1, "examples": ["SR-9"]},
        {"kind": "similar", "score": {"n": 250000, "d": 1000000}, "count": 4, "examples": []},
        {"kind": "name", "phrase": "Pad temperature"},
        {"kind": "text", "terms": ["pad", "temperature", "°c"]},
    )
    for record in crafted:
        found.setdefault(_shape(record), record)
    cases = [
        {"records": [record], "text": _evidence_text([record])}
        for _shape_, record in sorted(found.items(), key=lambda item: repr(item[0]))
    ]
    pair = [cases[0]["records"][0], cases[-1]["records"][0]]
    cases.append({"records": pair, "text": _evidence_text(pair)})
    cases.append({"records": [], "text": _evidence_text([])})
    return cases


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# The CLI's answers
# ---------------------------------------------------------------------------


def _cli(project_dir: Path, *argv: str) -> dict[str, Any]:
    """One run of the CLI in *project_dir*: its exit code and stdout, the directory written as :data:`WORK`."""
    done = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "experiment", "trace", *argv],
        cwd=project_dir,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    text = done.stdout.decode("utf-8").replace("\r\n", "\n")
    for spelling in (str(project_dir.resolve()), str(project_dir)):  # macOS's /var is /private/var resolved
        text = text.replace(spelling, WORK)
    return {"exit": done.returncode, "stdout": text.replace("\\", "/")}


def cli_answers() -> dict[str, Any]:
    """The CLI's own answers on two synthetic projects: the panel model's search, and Apply's project."""
    answers: dict[str, Any] = {"cwd": WORK}
    with tempfile.TemporaryDirectory() as scratch:
        project_dir = Path(scratch) / "project"
        manifest, config = write_project(_panel_model(), project_dir, labelling=["System Spec"])
        project = ["--project", str(manifest), "--config", str(config)]
        answers["search"] = _cli(project_dir, *project)
        answers["documents"] = _cli(project_dir, "--documents", *project, "--format", "json")
    with tempfile.TemporaryDirectory() as scratch:
        apply = ApplyProject(Path(scratch), apply_model())
        apply.write(confirming(apply, {"SR-2": [(OUT, "accept"), (TOP, "reject")]}))
        argv = ["--apply", str(apply.proposals), "--project", str(apply.manifest)]
        answers["applyDryRun"] = _cli(apply.dir, *argv, "--dry-run")
        digest = next(line for line in answers["applyDryRun"]["stdout"].splitlines() if line.startswith("Plan: "))
        answers["applyApplied"] = _cli(apply.dir, *argv, "--expect-plan", digest.removeprefix("Plan: "))
        answers["applyNoChange"] = _cli(apply.dir, *argv, "--dry-run")
    return answers


def generated() -> dict[Path, str]:
    """Every generated fixture's path and text."""
    canonical = canonical_document()
    return {
        CANONICAL: proposals_json(canonical),
        PENDING: _json_text(pending_vectors(canonical)),
        EVIDENCE: _json_text(evidence_sentences(canonical)),
        ANSWERS: _json_text(cli_answers()),
    }


@pytest.fixture(scope="module")
def fresh() -> dict[Path, str]:
    return generated()


class TestTheGeneratedFixtures:
    @pytest.mark.parametrize("path", [CANONICAL, PENDING, EVIDENCE, ANSWERS], ids=lambda path: path.name)
    def test_each_equals_a_fresh_generation(self, fresh: dict[Path, str], path: Path) -> None:
        """A change detector: regenerated deliberately (the module docstring), never to make a red run green."""
        assert path.read_text(encoding="utf-8") == fresh[path]


class TestTheCanonicalFile:
    def test_the_reader_and_the_schema_accept_it(self) -> None:
        data = CANONICAL.read_bytes()
        trace_records.parse_proposals(data, CANONICAL.name)
        schema = json.loads(
            (resource_files("swcraft_tools") / trace_records.SCHEMA_RESOURCE).read_text(encoding="utf-8")
        )
        assert list(Draft202012Validator(schema).iter_errors(json.loads(data))) == []

    def test_it_holds_what_the_panel_tests_need(self) -> None:
        document = json.loads(CANONICAL.read_text(encoding="utf-8"))
        assert document["model"]["swcraftVersion"] == FIXTURE_VERSION
        assert _entry(document, TITLED[0])["title"] == TITLED[1]
        assert _entry(document, ESCAPED[0])["title"] == ESCAPED[1]
        assert "°" in CANONICAL.read_text(encoding="utf-8")  # written as is, not escaped
        assert {entry["pass"] for entry in document["requirements"]} == {"labelling", "review"}
        assert any(entry["document"] is None for entry in document["requirements"])
        assert _entry(document, "BRK-5")["chosen"] == [{"target": CHOSEN}]
        members = [(link["target"]["name"], link["member"]) for link in _entry(document, "BRK-6")["linked"]]
        assert members == [("BrakeController", "BRK-6@1"), ("BrakeController", "BRK-6")]
        kinds = {
            record["kind"]
            for entry in document["requirements"]
            for row in entry["candidates"]
            for record in (*row["group"]["evidence"], *row["evidence"])
        }
        assert kinds == {"name", "neighbours", "similar", "text"}


class TestTheTitle:
    def test_it_is_the_model_title_in_both_files(self, tmp_path: Path) -> None:
        manifest, _config = write_project(_panel_model(), tmp_path / "project")
        run = run_trace(require_project(str(manifest)), None)
        titles = {
            "BRK-1": "Brake command",
            TITLED[0]: TITLED[1],
            "N-2": "Unrelated",
        }
        for path in (run.result_path, run.proposals_path):
            entries = {entry["id"]: entry for entry in json.loads(path.read_text(encoding="utf-8"))["requirements"]}
            assert {identifier: entries[identifier]["title"] for identifier in titles} == titles, path.name


class TestThePendingVectors:
    def test_each_count_is_the_reader_s(self) -> None:
        vectors = json.loads(PENDING.read_text(encoding="utf-8"))
        assert len(vectors) == len(list(_vectors()))
        for vector in vectors:
            proposals = trace_records.parse_proposals(proposals_json(vector["proposals"]).encode("utf-8"), "vector")
            assert trace_records.pending(proposals.model) == vector["pending"], vector["name"]


class TestTheEvidenceSentences:
    def test_each_sentence_is_the_report_s(self) -> None:
        cases = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        shapes = {_shape(case["records"][0]) for case in cases if len(case["records"]) == 1}
        assert {shape[0] for shape in shapes} == {"name", "neighbours", "similar", "text"}
        for case in cases:
            assert _evidence_text(case["records"]) == case["text"]


class TestTheFileThePanelWrote:
    """``ticked_proposals.json``: written by ``traceCore.test.ts``, read here by Apply's strict reader.

    The panel's test writes it from the canonical file and a fixed list of edits (a tick, a reject, *Mark strong*, a
    choice, a confirm)."""

    def test_the_strict_reader_and_the_schema_accept_it(self) -> None:
        path = FIXTURES / "ticked_proposals.json"
        proposals = trace_records.read_proposals(path)
        schema = json.loads(
            (resource_files("swcraft_tools") / trace_records.SCHEMA_RESOURCE).read_text(encoding="utf-8")
        )
        assert list(Draft202012Validator(schema).iter_errors(proposals.document)) == []
        decisions = [
            (trace_records.decision_of(item), getattr(item, "preselected", None))
            for *_, item in trace_records.entry_items(proposals.model)
            if trace_records.decision_of(item) is not None
        ]
        assert ("accept", None) in decisions and ("reject", None) in decisions
        assert ("accept", True) in decisions and ("confirm", None) in decisions
        assert trace_records.pending(proposals.model) == len(decisions)


class TestTheCliAnswers:
    def test_they_hold_what_the_panel_parses(self) -> None:
        answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
        assert f"Generated proposals: {WORK}/dist/experiments/trace/proposals.json" in answers["search"]["stdout"]
        documents = json.loads(answers["documents"]["stdout"])
        assert documents["proposals"] == "dist/experiments/trace/proposals.json"
        assert [row["pass"] for row in documents["documents"]][:3] == ["review", "review", "labelling"]
        dry = answers["applyDryRun"]["stdout"].splitlines()
        assert "Dry run: nothing was written." in dry and "No change." not in dry
        assert sum(line.startswith("Summary: ") for line in dry) == 1
        assert sum(line.startswith("Plan: sha256:") for line in dry) == 1
        applied = answers["applyApplied"]["stdout"].splitlines()
        assert "Dry run: nothing was written." not in applied
        assert (
            "Summary: 1 written, 1 confirmed, 1 rejected, 0 already done, 0 stale, 0 not written, 0 undecided; 1 files"
            in applied
        )
        assert "No change." in answers["applyNoChange"]["stdout"].splitlines()


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path, text in generated().items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
