"""``scripts/measure_forms.py``: every recipe form and the reference calls, figures only.

The script is the real-export tier's measurement, so its own failure would read as a figure: a recipe
dropped, a reference call out of order, a cell carrying a name of the model. It runs here on copies of
small specimens and is held to:

- every recipe a row, each request in order; a placeholder the model cannot fill is ``not asked``;
- the reference calls in their listed order and labels; the record's layout;
- a refusal's cell is its closed code, stated size and hint keys from the closed ``HINT_KEYS``; a text
  refusal's cell is its code alone, even when a model name reads as a hint with a planted key and number;
- ``impact_of``'s ``count`` form keyed by the kind tokens it emits, so different walks differ;
- no sentinel name or text in any cell, record, stdout, stderr or server log (the last when every tool
  raises with the sentinels in its text); no number the model holds in any cell, record, stdout or
  stderr; an ``impact_of`` summary is its line count alone, never parsed for a number;
- the names-free fallbacks (``unrecognised-code``, non-integer figures left out) and ``main``'s labels
  past ``model Z`` and its write-failure sentence.

It lives here because the script drives an in-memory FastMCP client, which ``packages/tools/`` may not
import. What it does not reach: a numeric model value in a reply shape the planted numbers do not reach
(held only by the script's figures audit), and the figures of a real export.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import swcraft_mcp.server as server
from conftest import copy_specimen, load_repo_script, measure_forms_module
from fastmcp import Client
from swcraft_tools.impact import ImpactPage, format_impact_scope, format_impact_summary
from swcraft_tools.passport import compute_passport
from test_agent_recipes import RECIPE_TITLES
from test_passport import NAME_SENTINEL, TEXT_SENTINEL, sentinel_root  # noqa: F401 - the fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "measure_forms.py"
measure_forms = measure_forms_module()
BENCH_MODULE = load_repo_script("agent_task_bench", "scripts/agent_task_bench.py")

#: The lowercase prefix both sentinels share: a lowered or truncated derivative still carries it.
SENTINEL_PREFIX = "zqsen"

#: The small specimen the forms are measured on: memories, budgets, cores and tasks.
SMALL = "passport-small"

#: A specimen with no memory, so the two memory recipes and reference calls are not asked.
NO_MEMORY = "no-memory"

#: The reference calls, in order, spelled out so the script's list is compared with an independent one.
REFERENCE_LABELS = [
    "discover baseline (default)",
    "discover summary",
    "discover full",
    "discover swc full",
    "discover interfaces full",
    "discover connectors full",
    "read map (no address)",
    *(
        f"read {layer} {form}"
        for layer in ("swc", "interfaces", "connectors", "application_types", "implementation_types", "compositions")
        for form in ("count", "index, first page", "summary, first page")
    ),
    "read largest SWC full",
    "read largest SWC summary",
    "read 16 largest SWCs summary (batch)",
    "read 16 largest SWCs full (batch)",
    "find 4 names (exact, lower, prefix, misspelt)",
    "find 16 names (the 16 largest SWCs)",
    "find text (two words of the largest SWC's name)",
    "derived unconnected, whole swc layer, first page",
    "derived unconnected count, whole swc layer",
    "derived partitions, swc index",
    "derived effective_level count rating B",
    "derived effective_level, swc index",
    "derived crossing_ends, connectors index first page",
    "derived connection_ends, connectors count",
    "impact_of heaviest interface, count",
    "impact_of heaviest interface, summary (default)",
    "impact_of heaviest interface, summary depth 1 limit 20",
    "impact_of largest SWC, count",
    "impact_of largest SWC, summary",
    "impact_of largest SWC, dependencies count",
    "impact_of largest SWC, dependencies",
    "resource_report overview",
    "resource_report core",
    "resource_report memory",
    "resource_report fits 40 KB in memory",
    "derived core, tasks index",
    "derived realised_by, requirements index",
]

#: One hint's cell: keys joined by ``+``, a value only for ``limit`` (an integer) and
#: ``form`` (a closed form).
_HINT = r"[a-z_-]+(?:=(?:\d+|full|summary|index|count|baseline))?"
#: A refusal's whole cell.
REFUSAL_CELL = re.compile(
    rf"refused `[a-z][a-z-]*`(?:, would be [\d,]+ B)?(?:, hints \[(?:{_HINT}(?:\+{_HINT})*(?:, )?)*\])?"
)


def _project(root: Path, specimen: str = SMALL) -> Path:
    """A copy of *specimen* at *root* (which must not exist): the test's to write; its manifest."""
    return copy_specimen(specimen, root) / "swcraft.yaml"


@pytest.fixture(scope="module")
def small(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return measure_forms.measure(_project(tmp_path_factory.mktemp("forms") / SMALL))


@pytest.fixture(scope="module")
def no_memory(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return measure_forms.measure(_project(tmp_path_factory.mktemp("forms") / NO_MEMORY, NO_MEMORY))


@pytest.fixture(scope="module")
def sentinel_manifest(sentinel_root: Path) -> Path:  # noqa: F811 - the imported fixture
    manifest = sentinel_root.parent / "swcraft.yaml"
    manifest.write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    return manifest


def _names_free(text: str) -> None:
    assert NAME_SENTINEL not in text
    assert TEXT_SENTINEL not in text
    assert SENTINEL_PREFIX not in text.lower()


def test_every_recipe_is_a_row_each_request_in_order(small: Any) -> None:
    recipes = measure_forms.all_recipes()
    assert tuple(recipe.title for recipe in recipes) == RECIPE_TITLES
    expected = [
        f"{recipe.title}, {index} of {len(recipe.requests)}"
        for recipe in recipes
        for index in range(1, len(recipe.requests) + 1)
    ]
    assert [call.label for call in small.recipe_calls] == expected
    # The small model holds every kind a placeholder names, so every recipe is asked.
    assert all(call.status != measure_forms.NOT_ASKED for call in small.recipe_calls)
    assert small.errors == ()


def test_a_kind_the_model_lacks_is_not_asked_naming_the_kind(no_memory: Any) -> None:
    not_asked = {call.label: call.outcome for call in no_memory.calls if call.status == measure_forms.NOT_ASKED}
    assert not_asked == {
        "Whether a memory holds a new component, 1 of 1": "not asked: the model has no memory",
        "The largest consumer of a memory, 1 of 1": "not asked: the model has no memory",
        "resource_report memory": "not asked: the model has no memory",
        "resource_report fits 40 KB in memory": "not asked: the model has no memory",
    }
    assert no_memory.errors == ()


def test_the_reference_calls_are_the_list_in_order(small: Any, no_memory: Any) -> None:
    assert [call.label for call in small.reference_calls] == REFERENCE_LABELS
    # A call the model has nothing for keeps its row, so two records always compare row by row.
    assert [call.label for call in no_memory.reference_calls] == REFERENCE_LABELS


def test_the_record_has_its_layout(small: Any, no_memory: Any) -> None:
    record = measure_forms.render_record([small, no_memory], ["model A", "model B"], date="2026-10-24")
    lines = record.splitlines()
    assert lines[0] == "# The question forms on 2 models — 2026-10-24"
    assert "No LLM was run" in lines[2]
    assert lines[4] == "|  | model A | model B |"
    passport_rows = [
        "Components / interfaces / connectors",
        "Data types (application / implementation / base)",
        "Compositions, hardware",
        "Tasks, requirements",
        "Largest component, by ports",
        "Heaviest interface, by ports naming it",
        "Ports no connector reaches",
    ]
    assert [line.split(" | ")[0].removeprefix("| ") for line in lines[6:13]] == passport_rows
    assert lines[13] == ""
    # The real tier's further rows go below the passport's, one cell per label.
    table = measure_forms.counts_table(
        [small, no_memory], ["model A", "model B"], [("Import, check-model", ["exit 0", "exit 1"])]
    )
    assert [line.split(" | ")[0].removeprefix("| ") for line in table[2:]] == [*passport_rows, "Import, check-model"]
    assert table[-1] == "| Import, check-model | exit 0 | exit 1 |"
    assert "## Recipe forms" in lines and "## Reference calls" in lines
    recipes = lines[lines.index("## Recipe forms") + 2 : lines.index("## Reference calls") - 1]
    assert recipes[0] == "| call | model A | model B |"
    assert len(recipes) == 2 + len(small.recipe_calls)
    # The loader's order: the profile's recipes, then the How-to pages'.
    (largest,) = [row for row in recipes if row.startswith("| The largest consumer of a memory, 1 of 1 | ")]
    assert largest.endswith(" | not asked: the model has no memory |")
    assert recipes[-1].startswith("| What a change affects, 2 of 2 | ")
    summary = [line for line in lines if line.startswith(("model A: ", "model B: "))]
    assert len(summary) == 2
    assert re.fullmatch(r"model B: answers over 8,192 bytes: \d+ of \d+; refused: \d+; not asked: 4\.", summary[1])


def test_a_refusal_is_its_code_size_and_hint_keys_only(
    sentinel_manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lowered budget makes the batch and the impact summary refuse; their hints carry sentinel names.
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 500)
    measured = measure_forms.measure(sentinel_manifest)
    by_label = {call.label: call for call in measured.calls}
    batch = by_label["read 16 largest SWCs full (batch)"]
    assert batch.status == measure_forms.REFUSED
    assert batch.outcome.startswith("refused `over-budget`, would be ")
    assert ", hints [" in batch.outcome
    impact = by_label["impact_of heaviest interface, summary (default)"]
    assert impact.status == measure_forms.REFUSED
    assert impact.outcome == "refused `text-refusal`", "a text refusal's hint is parsed from text"
    refused = [call for call in measured.calls if call.status == measure_forms.REFUSED]
    assert refused
    for call in refused:
        assert REFUSAL_CELL.fullmatch(call.outcome), call.outcome
        _names_free(call.outcome)
    assert measured.errors == ()


def test_a_text_refusal_is_its_code_alone(sentinel_manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `after` without a `limit`, on a name the model holds, is a text refusal with no next request.
    name = compute_passport(sentinel_manifest.parent / "model").locator.heaviest_interface()
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(sentinel_manifest))
    monkeypatch.setattr(server, "_PROJECT", None)

    async def ask() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool("impact_of", {"name": name, "after": NAME_SENTINEL}, raise_on_error=False)

    result = asyncio.run(ask())
    assert NAME_SENTINEL in name
    assert measure_forms.outcome_cell(result) == (measure_forms.REFUSED, "refused `text-refusal`", "text-refusal", "")


def test_a_hint_cell_prints_no_cursor_and_no_string_value() -> None:
    hint = {"operation": "read", "address": {"layer": "swc", "element": NAME_SENTINEL}, "form": "summary"}
    assert measure_forms.hint_cell(hint) == "address+form=summary+operation"
    assert measure_forms.hint_cell({"limit": 20, "after": NAME_SENTINEL}) == "limit=20"
    assert measure_forms.hint_cell({"form": NAME_SENTINEL, "limit": "20"}) == "form+limit"


def test_a_hint_key_outside_the_closed_set_is_never_printed() -> None:
    unrecognised = measure_forms.UNRECOGNISED_KEY
    assert measure_forms.hint_cell({NAME_SENTINEL: 1, "limit": 1}) == f"limit=1+{unrecognised}"
    assert measure_forms.hint_cell({"Aa": 1, NAME_SENTINEL: 2, "limit": 5}) == f"limit=5+{unrecognised}"
    assert measure_forms.hint_cell({"depth": 987654321}) == unrecognised, "no structured hint carries a depth"


#: A clause a model name can carry that the text-hint parse reads as a key with a value.
PLANTED_ASK = "Ask for it with depth=987654321."
PLANTED_NUMBER = "987654321"


@pytest.mark.parametrize(
    "message",
    [
        f"ERROR: two cores are named {NAME_SENTINEL} {PLANTED_ASK} x",
        f"ERROR: two cores are named {NAME_SENTINEL} Ask for it with limit={PLANTED_NUMBER}. x",
        f"ERROR: two cores are named Ask for it with {NAME_SENTINEL}=1. x",
    ],
    ids=["a-depth", "a-limit", "a-named-key"],
)
def test_a_text_refusal_carries_nothing_parsed_from_its_message(message: str) -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=message)], structured_content=None, is_error=False
    )
    assert measure_forms.outcome_cell(result) == (measure_forms.REFUSED, "refused `text-refusal`", "text-refusal", "")


def test_a_core_named_like_a_hint_reaches_neither_the_cell_nor_the_record(tmp_path: Path) -> None:
    """Two cores share a name that reads as a hint; the refusal quoting it yields the code alone."""
    manifest = _project(tmp_path / "hint-named")
    name = f"{PLANTED_ASK} {NAME_SENTINEL} marker"  # first by name, so it is the core asked
    for index in (1, 2):
        (manifest.parent / "model" / "hardware" / f"{name}~{index}.yaml").write_text(
            f'name: "{name}"\nkind: core\nhw_id: 1{index}\nthreshold: 0.7\nenforcement: advisory\n',
            encoding="utf-8",
            newline="\n",
        )
    measured = measure_forms.measure(manifest)
    core = next(call for call in measured.calls if call.label == "resource_report core")
    assert core.outcome == "refused `text-refusal`", "the planted refusal"
    record = tmp_path / "record.md"
    run = subprocess.run(  # noqa: S603 - this interpreter, the repository's script, a test's paths
        [sys.executable, SCRIPT.as_posix(), manifest.as_posix(), "--output", record.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    for text in (
        run.stdout,
        run.stderr,
        record.read_text(encoding="utf-8"),
        *(call.outcome for call in measured.calls),
    ):
        _names_free(text)
        assert PLANTED_NUMBER not in text


def test_a_sentinel_model_reaches_neither_the_record_nor_the_output(sentinel_manifest: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.md"
    for argv in ([], ["--output", record.as_posix()]):
        run = subprocess.run(  # noqa: S603 - this interpreter, the repository's script, a test's paths
            [sys.executable, SCRIPT.as_posix(), sentinel_manifest.as_posix(), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        _names_free(run.stdout)
        _names_free(run.stderr)
    text = record.read_text(encoding="utf-8")
    assert text.startswith("# The question forms on 1 model — ")
    assert "| call | model A |" in text
    _names_free(text)


def test_a_project_that_cannot_be_read_is_one_names_free_sentence(tmp_path: Path) -> None:
    missing = tmp_path / NAME_SENTINEL / "swcraft.yaml"
    run = subprocess.run(  # noqa: S603 - this interpreter, the repository's script, a test's path
        [sys.executable, SCRIPT.as_posix(), missing.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert run.returncode == 1
    assert run.stdout == ""
    assert run.stderr == "measure_forms.py: model A could not be measured: ManifestNotFoundError\n"


def test_a_tool_that_raises_logs_nothing_of_its_text(
    sentinel_manifest: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """FastMCP logs an unexpected tool exception on stderr; the script silences it, then re-enables the logger."""

    def raises() -> Any:
        raise RuntimeError(f"{NAME_SENTINEL} {TEXT_SENTINEL}")

    monkeypatch.setattr(server, "_get_project", raises)
    capfd.readouterr()
    measured = measure_forms.measure(sentinel_manifest)
    out, err = capfd.readouterr()
    assert any(call.status == measure_forms.ERROR for call in measured.calls), "no tool raised"
    _names_free(out)
    _names_free(err)
    assert not logging.getLogger("fastmcp").disabled


def _result(body: dict[str, Any]) -> Any:
    """A tool result carrying *body* as its one text block, the shape the server sends."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(body))], structured_content=None, is_error=False
    )


def test_a_refusal_code_outside_the_closed_shape_is_unrecognised() -> None:
    result = _result({"response": "error", "error": f"{NAME_SENTINEL} text"})
    assert measure_forms.refusal_code(result) == measure_forms.UNRECOGNISED_CODE
    assert measure_forms.outcome_cell(result) == (
        measure_forms.REFUSED,
        "refused `unrecognised-code`",
        measure_forms.UNRECOGNISED_CODE,
        "",
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"count": NAME_SENTINEL}, ""),
        ({"count": True}, ""),
        ({"page": {"total": NAME_SENTINEL, "returned": 1, "truncated": False}}, ""),
        ({"page": {"total": 2, "returned": 1, "truncated": "no"}}, ""),
        (
            {"count": 3, "page": {"total": 2, "returned": 1, "truncated": False}},
            "count 3; page total 2 returned 1 truncated False",
        ),
    ],
    ids=["a-string-count", "a-bool-count", "a-named-total", "a-string-truncated", "integers"],
)  # fmt: skip
def test_figures_of_keeps_integers_and_booleans_alone(body: dict[str, Any], expected: str) -> None:
    assert measure_forms.figures_of(_result(body)) == expected


def _impact_count(matched: dict[str, Any]) -> Any:
    return _result(
        {"response": "impact", "name": NAME_SENTINEL, "direction": "dependents", "depth": None, "matched": matched}
    )


@pytest.mark.parametrize(
    ("matched", "expected"),
    [
        ({}, "matched 0"),
        ({"interface": {"total": 3, "by_kind": {"swc": 3}}}, "matched 1: interface total 3 (swc 3)"),
        (
            {"interface": {"total": 9, "by_kind": {"swc": 6, "connector": 1, "task": 2}}},
            "matched 1: interface total 9 (connector 1, swc 6, task 2)",
        ),
        (
            {NAME_SENTINEL: {"total": 1, "by_kind": {NAME_SENTINEL: 1}}},
            "matched 1: unrecognised-kind total 1 (unrecognised-kind 1)",
        ),
        (
            {"interfaces": {"total": 1, "by_kind": {"connectors": 1}}},
            "matched 1: unrecognised-kind total 1 (unrecognised-kind 1)",
        ),
        ({"swc": {"total": NAME_SENTINEL, "by_kind": {"swc": "1"}}}, "matched 1: swc"),
    ],
    ids=["nothing-matched", "a-walk-of-3", "a-walk-of-9", "named-keys", "layer-names-are-not-kinds", "named-values"],
)  # fmt: skip
def test_figures_of_reads_impact_of_counts(matched: dict[str, Any], expected: str) -> None:
    assert measure_forms.figures_of(_impact_count(matched)) == expected


def _impact_call(by_kind: dict[str, int]) -> Any:
    matched = {"interface": {"total": sum(by_kind.values()), "by_kind": by_kind}}
    outcome = measure_forms.outcome_cell(_impact_count(matched))
    return measure_forms.Call("impact_of", outcome.status, 100, 0.0, outcome.cell, figures=outcome.figures)


def test_two_impact_counts_that_differ_do_not_agree() -> None:
    assert measure_forms.pair_agrees(_impact_call({"swc": 3}), _impact_call({"swc": 3}), tolerance=0.25)
    assert not measure_forms.pair_agrees(_impact_call({"swc": 3}), _impact_call({"swc": 9}), tolerance=0.25)


def test_three_and_four_interface_dependencies_do_not_agree() -> None:
    """The paired ``form="count"`` dependencies call tells apart what the line count cannot."""

    def dependencies(interfaces: int) -> Any:
        matched = {"swc": {"total": interfaces, "by_kind": {"interface": interfaces}}}
        result = _result(
            {
                "response": "impact",
                "name": NAME_SENTINEL,
                "direction": "dependencies",
                "depth": None,
                "matched": matched,
            }
        )
        outcome = measure_forms.outcome_cell(result)
        return measure_forms.Call("impact_of", outcome.status, 100, 0.0, outcome.cell, figures=outcome.figures)

    three, four = dependencies(3), dependencies(4)
    assert three.figures == "matched 1: swc total 3 (interface 3)"
    assert NAME_SENTINEL not in three.figures + four.figures
    assert not measure_forms.pair_agrees(three, four, tolerance=0.25)


def test_the_reference_calls_answer_and_the_dependencies_count_pairs_its_summary(small: Any) -> None:
    """No reference call is refused as malformed, and the dependencies summary has its count beside it."""
    by_label = {call.label: call for call in small.reference_calls}
    refused = [call.label for call in small.reference_calls if call.code == "schema-violation"]
    assert refused == []
    count = by_label["impact_of largest SWC, dependencies count"]
    assert count.status == measure_forms.OK, count.outcome
    assert count.figures.startswith("matched "), count.figures
    assert "interface" in count.figures, count.figures


def test_one_total_split_differently_between_kinds_does_not_agree() -> None:
    """The kinds ``impact_of`` emits are singular tokens; read as layer names they all collapsed."""
    first = _impact_call({"connector": 1, "task": 2, "interface": 1})
    second = _impact_call({"connector": 2, "task": 1, "interface": 1})
    assert first.figures != second.figures
    assert not measure_forms.pair_agrees(first, second, tolerance=0.25)


_ADDRESS = {"layer": "swc", "element": NAME_SENTINEL}
_IDENTITY = {"element": NAME_SENTINEL, "derived": {"effective_level": {"level": NAME_SENTINEL}}}
_ELEMENT = {"name": NAME_SENTINEL, "ports": [], "memory": {}, "wcet_us": 987654321}


def _element(rows: int) -> dict[str, Any]:
    return {
        **_ELEMENT,
        "ports": [{"name": NAME_SENTINEL}] * rows,
        "memory": {f"{NAME_SENTINEL}{i}": 1 for i in range(rows)},
    }


def _layer(rows: int) -> dict[str, Any]:
    return {"layer": NAME_SENTINEL, "present": True, "element_kinds": [{"kind": NAME_SENTINEL, "fields": [1] * rows}]}


#: Every reply shape a recipe form or a reference call answers with, as a body of *rows* rows.
_SHAPES = {
    "read-count": lambda rows: {"response": "read", "address": _ADDRESS, "count": rows},
    "read-index-unpaged": lambda rows: {
        "response": "read",
        "form": "index",
        "value": {"count": rows, "identities": [NAME_SENTINEL] * rows},
    },
    "read-index-derived": lambda rows: {
        "response": "read",
        "derived": [NAME_SENTINEL],
        "value": {"count": rows, "identities": [_IDENTITY] * rows},
    },
    "read-index-paged": lambda rows: {
        "response": "read",
        "members": NAME_SENTINEL,
        "page": {"total": 9, "returned": 2, "truncated": True},
        "value": {"count": rows, "identities": [_IDENTITY] * rows},
    },
    "read-summary-unpaged": lambda rows: {"response": "read", "value": [{"name": NAME_SENTINEL}] * rows},
    "read-summary-paged": lambda rows: {
        "response": "read",
        "page": {"total": 9, "returned": 2, "truncated": True},
        "value": [{"name": NAME_SENTINEL}] * rows,
    },
    "read-one-element": lambda rows: {"response": "read", "address": _ADDRESS, "value": _element(rows)},
    "read-one-element-summary": lambda rows: {
        "response": "read",
        "address": _ADDRESS,
        "form": "summary",
        "value": {**_ELEMENT, "ports": rows, "memory": rows, "runnables": 2},
    },
    "read-batch-summary": lambda rows: {
        "response": "batch",
        "answers": [
            {
                "response": "read",
                "address": _ADDRESS,
                "form": "summary",
                "value": {**_ELEMENT, "ports": rows, "memory": 1, "runnables": 2},
            },
        ],
    },
    "read-map": lambda rows: {
        "response": "map",
        "counts": {"swc": rows, NAME_SENTINEL: 1},
        "layers": {"swc": [NAME_SENTINEL] * rows},
    },
    "read-batch": lambda rows: {
        "response": "batch",
        "answers": [
            {"response": "read", "address": _ADDRESS, "value": _element(rows)},
            {"response": "error", "error": "not-found"},
        ],
    },
    "find-names": lambda rows: {
        "response": "find",
        "results": [{"name": NAME_SENTINEL, "candidates": [{"identity": NAME_SENTINEL}] * rows}],
    },
    "find-text": lambda rows: {
        "response": "find",
        "text": NAME_SENTINEL,
        "candidates": [{"identity": NAME_SENTINEL}] * rows,
    },
    "trace-candidates": lambda rows: {
        "response": "trace",
        "for_requirement": NAME_SENTINEL,
        "neighbours": 0,
        "existing_links": [{"member": NAME_SENTINEL}] * rows,
    },
    "impact-count": lambda rows: {
        "response": "impact",
        "name": NAME_SENTINEL,
        "matched": {"swc": {"total": rows, "by_kind": {"task": rows}}},
    },
    "resource-overview": lambda rows: {
        "response": "resources",
        "cores": [{"name": NAME_SENTINEL}] * rows,
        "memories": [],
        "findings": [],
        "topology": [],
    },
    "resource-core": lambda rows: {
        "response": "core",
        "resource": {"name": NAME_SENTINEL},
        "tasks": [{"name": NAME_SENTINEL}] * rows,
        "undeclared": 0,
    },
    "resource-memory": lambda rows: {
        "response": "memory",
        "resource": {"name": NAME_SENTINEL, "size": 987654321, "limit": "987654321 B"},
        "consumers": [{"name": NAME_SENTINEL}] * rows,
    },
    "resource-fits": lambda rows: {
        "response": "fits",
        "resource": NAME_SENTINEL,
        "size": 987654321,
        "asked": "987654321 B",
        "fits": rows < 4,
        "within_threshold": {"fits": rows < 4, "limit": 987654321, "headroom": 987654321},
        "within_size": {"fits": True, "limit": 987654321, "headroom": 987654321},
        "answer": f"fits physically, breaks the threshold by {NAME_SENTINEL}",
    },
    "discover": lambda rows: {
        "response": "discover",
        "project": {"name": NAME_SENTINEL},
        "layers": [_layer(rows)] * rows,
    },
}


def _text(text: str) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], structured_content=None, is_error=False)


def _text_result(lines: int) -> Any:
    return _text("\n".join(f"{NAME_SENTINEL} line {index}" for index in range(lines)))


def _dependent(layer: str, index: int) -> Any:
    return SimpleNamespace(ref=SimpleNamespace(layer=layer, name=f"{NAME_SENTINEL}{index}"))


def _impact_summary(dependents: int, *, page: ImpactPage | None = None) -> Any:
    """``impact_of``'s summary as the tracked renderer writes it: *dependents* components and a task."""
    result = {"interface": [*(_dependent("swc", index) for index in range(dependents)), _dependent("task", 0)]}
    text = format_impact_summary(NAME_SENTINEL, result)  # type: ignore[arg-type] - the renderer reads .ref alone
    footer = format_impact_scope(page=page)
    return _text(text + ("\n\n" + footer if footer else ""))


def _shape_result(shape: str, rows: int) -> Any:
    if shape == "plain-text":
        return _text_result(rows)
    return _result(_SHAPES[shape](rows))


def _shape_call(shape: str, rows: int) -> Any:
    outcome = measure_forms.outcome_cell(_shape_result(shape, rows))
    assert outcome.status == measure_forms.OK
    return measure_forms.Call(shape, outcome.status, 100, 0.0, outcome.cell, figures=outcome.figures)


@pytest.mark.parametrize("shape", [*_SHAPES, "plain-text"])
def test_every_reply_shape_with_3_rows_differs_from_its_4(shape: str) -> None:
    """A 3-row and a 4-row answer of one shape never agree, and neither prints a name or a string value."""
    three, four = _shape_call(shape, 3), _shape_call(shape, 4)
    assert three.figures and three.figures != four.figures
    assert measure_forms.pair_agrees(three, _shape_call(shape, 3), tolerance=0.25)
    assert not measure_forms.pair_agrees(three, four, tolerance=0.25)
    for call in (three, four):
        assert NAME_SENTINEL not in str(call)
        assert "987654321" not in str(call)
        assert re.fullmatch(r"[a-z0-9_ ,;:|()-]+", call.figures.replace("True", "").replace("False", "")), call.figures


def test_a_summary_prints_its_collection_counts_and_withholds_every_other_integer() -> None:
    """A summary answers a collection by its count: kept at a field the contract declares one, alone."""
    summary = {"response": "read", "address": _ADDRESS, "form": "summary"}
    value = {**_ELEMENT, "ports": 3, "memory": 4, "runnables": 2, "category": 987654321}
    assert measure_forms.figures_of(_result({**summary, "value": value})) == "element -,4,-,3,2,-"
    assert measure_forms.summary_collection_fields(summary) >= {"ports", "memory", "runnables", "satisfies"}
    assert "wcet_us" not in measure_forms.summary_collection_fields(summary)
    for withheld in (
        {**summary, "form": "full"},
        {"response": "read", "address": _ADDRESS},
        {**summary, "address": {**_ADDRESS, "path": [{"field": "ports"}]}},
        {**summary, "address": {"layer": "swc"}},
        {**summary, "address": {"layer": NAME_SENTINEL, "element": NAME_SENTINEL}},
        {**summary, "address": NAME_SENTINEL},
    ):
        figures = measure_forms.figures_of(_result({**withheld, "value": {**value, "ports": 987654321}}))
        assert figures == "element -,-,-,-,-,-", withheld
    assert "987654321" not in measure_forms.figures_of(_result({**summary, "value": value}))


def test_a_real_summary_carries_the_collection_counts_its_full_form_lists(small: Any) -> None:
    """The server's own summary: each collection's count is the length the full form prints for it."""
    by_label = {call.label: call for call in small.calls}

    def collections(label: str) -> list[str]:
        figures = by_label[label].figures
        assert figures.startswith("element "), figures
        return sorted(size for size in figures.removeprefix("element ").split(",") if size != "-")

    summary = collections("read largest SWC summary")
    assert summary, "a summary's collection counts were withheld"
    assert summary == collections("read largest SWC full")


def test_the_figures_print_closed_keys_and_integers_alone() -> None:
    assert measure_forms.figures_of(_result(_SHAPES["read-index-unpaged"](3))) == "rows 3 identities 3"
    assert (
        measure_forms.figures_of(_result(_SHAPES["read-map"](3)))
        == "counts swc 3, unrecognised-layer 1; layers 1 names 3"
    )
    assert measure_forms.figures_of(_result(_SHAPES["read-one-element"](3))) == "element 3,-,3,-"
    assert (
        measure_forms.figures_of(_result(_SHAPES["read-batch"](3))) == "answers 2: element 3,-,3,- | refused not-found"
    )
    assert measure_forms.figures_of(_result(_SHAPES["find-names"](3))) == "results 1 (candidates 3)"
    assert measure_forms.figures_of(_result(_SHAPES["discover"](3))) == "layers 3 present 3 kinds 3 fields 9"
    assert measure_forms.figures_of(_text_result(3)) == "lines 3"
    assert measure_forms.figures_of(_result({"value": {"count": NAME_SENTINEL}, "size": "1 KB"})) == "element -"
    assert measure_forms.figures_of(_result({"value": {"count": 987654321}})) == "element -"
    assert measure_forms.figures_of(_result(_SHAPES["resource-memory"](3))) == "consumers 3"
    assert measure_forms.figures_of(_result(_SHAPES["resource-fits"](3))) == "fits yes"
    assert measure_forms.figures_of(_impact_summary(3)).startswith("lines ")


@pytest.mark.parametrize(("verdict", "token"), [(True, "yes"), (False, "no"), (None, "unknown")])
def test_fits_is_a_closed_token_and_no_amount_of_the_model(verdict: bool | None, token: str) -> None:
    body = {"response": "fits", "resource": NAME_SENTINEL, "kind": "memory", "size": 987654321}
    body |= {"asked": "987654321 B", "used": 987654321, "answer": f"does not fit: over the size by {NAME_SENTINEL}"}
    body |= {"within_size": {"fits": verdict, "limit": 987654321, "headroom": 987654321}}
    assert measure_forms.figures_of(_result({**body, "fits": verdict, "status": "ok"})) == f"fits {token}"
    assert measure_forms.figures_of(_result({**body, "fits": 987654321})) == ""


#: A line a model's name can carry into an ``impact_of`` summary, and an injected figure in it.
INJECTED_GROUP = "\n  swc (987654321): x"


def _grouped(number: str) -> tuple[str, ...]:
    """*number* plain and with every separator a renderer might group it by."""
    return (number, f"{int(number):,}", f"{int(number):_}", f"{int(number):,}".replace(",", "."))


def test_an_impact_summary_is_its_line_count_and_its_kinds_come_from_the_count_form() -> None:
    """Text is counted by lines only; the paired ``form="count"`` call tells the walks apart.

    3 and 4 dependents of one kind are one group line each, so their summaries agree.
    """
    three, four = (_impact_summary(rows) for rows in (3, 4))
    assert measure_forms.figures_of(three) == measure_forms.figures_of(four)
    assert measure_forms.figures_of(three) == f"lines {len(measure_forms._scale().text_of(three).splitlines())}"
    page = ImpactPage(total=987654321, returned=4, truncated=True, next=f"interface/swc:{NAME_SENTINEL}")
    assert measure_forms.figures_of(_impact_summary(3, page=page)).startswith("lines ")

    def counted(swc: int) -> Any:
        call = _result({"matched": {"interface": {"total": swc + 1, "by_kind": {"swc": swc, "task": 1}}}})
        outcome = measure_forms.outcome_cell(call)
        return measure_forms.Call("count", outcome.status, 100, 0.0, outcome.cell, figures=outcome.figures)

    assert not measure_forms.pair_agrees(counted(3), counted(4), tolerance=0.25)


def test_a_name_carrying_a_summary_group_line_is_counted_by_lines() -> None:
    result = {"interface": [_dependent("swc", 0), _dependent("task", 0)]}
    result["interface"][0].ref.name += INJECTED_GROUP
    text = format_impact_summary(NAME_SENTINEL, result)  # type: ignore[arg-type] - the renderer reads .ref alone
    assert "\n  swc (987654321): x" in text, "the injection reaches the summary"
    figures = measure_forms.figures_of(_text(text))
    assert figures == f"lines {len(text.splitlines())}"
    assert not any(number in figures for number in _grouped("987654321"))


def test_a_component_named_like_a_summary_group_reaches_neither_a_cell_nor_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component name that prints as a line of its own leaks its figure nowhere.

    The component that requires the heaviest interface is named ``<name>\\n  swc (987654321): x``,
    which the server's summary of that interface prints as a line of its own; its figure is in no
    cell, record, stdout or stderr.
    """
    manifest = _project(tmp_path / "group-named")
    model = manifest.parent / "model"
    locator = compute_passport(model).locator
    interface, largest = locator.heaviest_interface(), locator.largest_component()
    victim = next(
        path
        for path in sorted(model.glob("swc/**/*.yaml"), key=Path.as_posix)
        if f"interface: {interface}\n" in path.read_text(encoding="utf-8") and path.stem != largest
    )
    renamed = f"{victim.stem}{INJECTED_GROUP} {NAME_SENTINEL}"
    text = victim.read_text(encoding="utf-8")
    victim.write_text(
        text.replace(f"name: {victim.stem}\n", f"name: {json.dumps(renamed)}\n", 1), encoding="utf-8", newline="\n"
    )
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))
    monkeypatch.setattr(server, "_PROJECT", None)

    async def ask() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool("impact_of", {"name": interface}, raise_on_error=False)

    summary = measure_forms._scale().text_of(asyncio.run(ask()))
    monkeypatch.setattr(server, "_PROJECT", None)
    assert "\n  swc (987654321): x" in summary, "the injected line must reach the summary it is held to"
    measured = measure_forms.measure(manifest)
    asked = next(call for call in measured.calls if call.label == "impact_of heaviest interface, summary (default)")
    assert asked.status == measure_forms.OK and asked.figures.startswith("lines "), asked.outcome
    record = tmp_path / "record.md"
    run = subprocess.run(  # noqa: S603 - this interpreter, the repository's script, a test's paths
        [sys.executable, SCRIPT.as_posix(), manifest.as_posix(), "--output", record.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    for rendered in (run.stdout, run.stderr, record.read_text(encoding="utf-8"), *map(str, measured.calls)):
        _names_free(rendered)
        assert not any(number in rendered for number in _grouped("987654321"))


#: A number the model holds, planted in every memory's size and every non-zero memory budget.
PLANTED_AMOUNT = 987654321


def test_no_amount_of_the_model_reaches_a_cell_the_record_or_the_output(tmp_path: Path) -> None:
    manifest = _project(tmp_path / "amounts")
    model = manifest.parent / "model"
    planted = 0
    # The memories by the names the specimen's hardware files declare, never a generator's naming rule.
    memories = sorted(
        re.search(r"(?m)^name: (\S+)$", text).group(1)
        for text in (path.read_text(encoding="utf-8") for path in model.glob("hardware/*.yaml"))
        if "kind: memory" in text
    )
    assert memories, "the specimen declares no memory"
    budget = re.compile(rf"(?m)^(  (?:{'|'.join(map(re.escape, memories))})): [1-9]\d*$")
    for path in sorted((*model.glob("hardware/*.yaml"), *model.glob("swc/**/*.yaml")), key=Path.as_posix):
        text = path.read_text(encoding="utf-8")
        if "kind: memory" in text:
            text = re.sub(r"(?m)^size: \d+$", f"size: {PLANTED_AMOUNT}", text)
        text = budget.sub(rf"\1: {PLANTED_AMOUNT}", text)
        planted += text.count(str(PLANTED_AMOUNT))
        path.write_text(text, encoding="utf-8", newline="\n")
    assert planted > 2, "nothing planted"
    measured = measure_forms.measure(manifest)
    memory = next(call for call in measured.calls if call.label == "resource_report memory")
    assert memory.status == measure_forms.OK, memory.outcome
    record = tmp_path / "record.md"
    run = subprocess.run(  # noqa: S603 - this interpreter, the repository's script, a test's paths
        [sys.executable, SCRIPT.as_posix(), manifest.as_posix(), "--output", record.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    for text in (run.stdout, run.stderr, record.read_text(encoding="utf-8"), *map(str, measured.calls)):
        assert str(PLANTED_AMOUNT) not in text
        assert f"{PLANTED_AMOUNT:,}" not in text


def test_the_element_kinds_are_the_tokens_impact_of_emits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key of the server's ``impact_of`` ``count`` answer, over every interface, is a declared kind."""
    manifest = _project(tmp_path / "kinds")
    interfaces = sorted(path.stem for path in (manifest.parent / "model" / "interfaces").glob("*.yaml"))
    assert interfaces
    kinds = measure_forms.element_kinds()
    assert {"interface", "connector", "task", "swc"} <= kinds
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))
    monkeypatch.setattr(server, "_PROJECT", None)

    async def ask() -> list[Any]:
        async with Client(server.mcp) as client:
            return [
                await client.call_tool("impact_of", {"name": name, "form": "count"}, raise_on_error=False)
                for name in interfaces
            ]

    seen: set[str] = set()
    for reply in asyncio.run(ask()):
        matched = json.loads(measure_forms._scale().text_of(reply))["matched"]
        for layer, counts in matched.items():
            seen |= {layer, *counts["by_kind"]}
    monkeypatch.setattr(server, "_PROJECT", None)
    assert seen and seen <= kinds, seen - kinds


def test_main_labels_past_z_and_says_why_a_record_could_not_be_written(
    small: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(measure_forms, "measure", lambda _manifest: small)
    projects = [f"project-{index}/swcraft.yaml" for index in range(27)]
    assert measure_forms.main(["measure_forms.py", *projects]) == 0
    out = capsys.readouterr().out
    assert "| model Z | model AA |" in out

    missing = tmp_path / NAME_SENTINEL / "record.md"
    assert measure_forms.main(["measure_forms.py", projects[0], "--output", missing.as_posix()]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "measure_forms.py: the record could not be written: FileNotFoundError\n"
