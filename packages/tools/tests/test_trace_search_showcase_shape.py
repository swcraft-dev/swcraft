"""The trace link search over a synthetic, showcase-shaped model: every kind, both passes, determinism.

The pinned ``result.json`` (``fixtures/trace_search/showcase_shaped_result.json``)
is a **change detector, never a correctness proof**: the proofs are the
hand-computed cases of the evidence and passes suites. Regenerate it
deliberately, after reading the difference, with
``python packages/tools/tests/test_trace_search_showcase_shape.py`` -- never to make a red
run green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from swcraft_tools.project import require_project
from swcraft_tools.trace_search import TARGET_KINDS, TargetAddress, run_trace
from trace_models import showcase_shaped, write_project

GOLDEN = Path(__file__).parent / "fixtures" / "trace_search" / "showcase_shaped_result.json"

#: The keys of ``result.json`` a release or a dependency moves without the search changing:
#: the version and the model's digest (the fixture's bytes come from the YAML writer).
_VOLATILE = ("swcraft", "model")


def _project(tmp_path: Path, **keys: Any) -> tuple[Path, Path | None]:
    return write_project(showcase_shaped(), tmp_path / "project", **keys)


def _run(tmp_path: Path, **keys: Any):
    manifest, config = _project(tmp_path, **keys)
    return run_trace(require_project(str(manifest)), config)


def _pinned(document: dict[str, Any]) -> dict[str, Any]:
    pinned = json.loads(json.dumps(document))
    for key in _VOLATILE:
        pinned["fingerprint"].pop(key)
    return pinned


def _rows(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(entry["id"], candidate) for entry in document["requirements"] for candidate in entry["candidates"]]


class TestTheShowcaseShape:
    def test_a_review_run(self, tmp_path: Path) -> None:
        document = _run(tmp_path).document
        assert {entry["pass"] for entry in document["requirements"]} == {"review"}
        assert [row["document"] for row in document["documents"]] == ["Body Spec", "Brake Spec", "System Spec", None]
        assert document["summary"].startswith("trace: 19 requirements in scope in 3 documents and 2 without a document")

    def test_a_labelling_run_of_one_document(self, tmp_path: Path) -> None:
        document = _run(tmp_path, labelling=["System Spec"], labelling_share=0.5).document
        labelled = [entry for entry in document["requirements"] if entry["pass"] == "labelling"]
        # System Spec: SYS-1 to SYS-5 are linked (SYS-5 through the stub), so U = 3 and the budget 2.
        assert [record["budget"] for record in document["labelling"]] == [2]
        # every requirement of the document is listed, the budget's two first
        assert [bool(entry["chosenBecause"]) for entry in labelled] == [True, True] + [False] * 6
        assert {entry["id"] for entry in labelled[:2]} <= {"SYS-6", "SYS-7", "SYS-8"}
        assert [entry["id"] for entry in labelled[3:]] == ["SYS-1", "SYS-2", "SYS-3", "SYS-4", "SYS-5"]

    def test_every_kind_of_target_appears_as_a_row(self, tmp_path: Path) -> None:
        kinds = {candidate["target"]["kind"] for _identifier, candidate in _rows(_run(tmp_path).document)}
        assert kinds == set(TARGET_KINDS)

    def test_the_stub_is_never_a_row(self, tmp_path: Path) -> None:
        names = {
            candidate["target"]["owner"] or candidate["target"]["name"]
            for _id, candidate in _rows(_run(tmp_path).document)
        }
        assert "GatewayStub" not in names and "IExtBus" not in names

    def test_the_awaiting_link_is_no_seed_and_its_target_no_row(self, tmp_path: Path) -> None:
        document = _run(tmp_path).document
        entry = next(entry for entry in document["requirements"] if entry["id"] == "BRK-6")
        assert entry["linked"] == [
            {
                "target": TargetAddress("swc", None, "BrakeController").to_json(),
                "member": "BRK-6@1",
                "version": 1,
                "confirmed": False,
            }
        ]
        assert all(candidate["target"]["name"] != "BrakeController" for candidate in entry["candidates"])
        table = {row["document"]: row for row in document["documents"]}
        assert table["Brake Spec"]["awaitingOnly"] == 1

    def test_the_dangling_member_is_reported_and_refuses_nothing(self, tmp_path: Path) -> None:
        findings = _run(tmp_path).document["findings"]
        assert any(
            f["rule"] == "trace.link-unknown-requirement" and "GONE-1" in f["message"] for f in findings["trace"]
        )

    def test_every_written_fingerprint_is_the_one_apply_recomputes(self, tmp_path: Path) -> None:
        """A later Apply recomputes each fingerprint through ``target_fingerprint`` and ``requirement_fingerprint``."""
        from swcraft_tools.model_state import load_model_state
        from swcraft_tools.trace_search import requirement_fingerprint, target_fingerprint

        run = _run(tmp_path)
        state = load_model_state(tmp_path / "project" / "model")
        requirements = {requirement.id: requirement for requirement in state["requirements"]}
        rows = 0
        for entry in json.loads(run.proposals_path.read_text(encoding="utf-8"))["requirements"]:
            assert entry["fingerprint"] == requirement_fingerprint(requirements[entry["id"]])
            for candidate in entry["candidates"]:
                address = TargetAddress(**candidate["target"])
                assert candidate["fingerprint"] == target_fingerprint(state, address), address
                rows += 1
        assert rows > 50

    def test_the_golden(self, tmp_path: Path) -> None:
        """A change detector: regenerated deliberately (the module docstring), never to make a red run green."""
        assert _pinned(_run(tmp_path).document) == json.loads(GOLDEN.read_text(encoding="utf-8"))


class TestDeterminism:
    def test_two_runs_give_byte_equal_files(self, tmp_path: Path) -> None:
        manifest, _config = _project(tmp_path)
        texts = []
        for _ in range(2):
            run = run_trace(require_project(str(manifest)), None)
            texts.append((run.result_path.read_bytes(), run.proposals_path.read_bytes()))
        assert texts[0] == texts[1]

    def test_two_hash_seeds_give_byte_equal_files(self, tmp_path: Path) -> None:
        """Two hash seeds give byte-equal files.

        A set's or a hash's order reaching an output or a sum would differ between the two child runs: a labelling run,
        so both passes are covered, and all three files."""
        manifest, config = _project(tmp_path, labelling=["System Spec"], labelling_share=0.5)
        directory = tmp_path / "project" / "dist" / "experiments" / "trace"
        texts = []
        for seed in ("0", "1"):
            environment = {**os.environ, "PYTHONHASHSEED": seed}
            argv = ["experiment", "trace", "--project", str(manifest), "--config", str(config)]
            subprocess.run(
                [sys.executable, "-m", "swcraft_tools.cli", *argv],
                check=True,
                capture_output=True,
                env=environment,
                timeout=120,
            )
            texts.append(
                tuple((directory / name).read_bytes() for name in ("result.json", "proposals.json", "report.md"))
            )
        assert texts[0] == texts[1]


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        document = _pinned(_run(Path(scratch)).document)
    GOLDEN.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {GOLDEN}")
