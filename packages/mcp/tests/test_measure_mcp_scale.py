"""The acceptance oracle's own test: the sweep and the film replay run, and write nothing.

``scripts/measure_mcp_scale.py`` reports the MCP surface's figures, so its own failure (a sweep that
measured summaries under a "layer" heading, a replay whose ``change_set`` committed) would produce a
table that looks correct. Both modes run end to end on a copy of ``examples/basic``. The module lives
here because the script drives an in-memory FastMCP client and ``packages/tools/`` may not import MCP.

The refusal half runs by lowering the budget (``read`` reads ``RESPONSE_BUDGET_WIRE_BYTES`` at the
call), which drives the hint-walking loop, the refused sweep cells and the exit gate's per-refusal
listing. The chain shapes that need a wide model (a paged read hint, a discover hint, ``impact_of``'s
cumulative sentence) are driven against a scripted client instead.

What it does not reach: absolute sizes on a real model (the wire unit is held by
``test_response_budget.py``). "Writes nothing" is checked over the model tree: a staged change set
still appends its lifecycle records to the audit log, as it should.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scale_model
from conftest import measure_mcp_scale_module
from swcraft_mcp.server import _narrower_impact_request
from swcraft_tools.editing.protocol import DISCOVER_FORMS
from swcraft_tools.editing.read import RESPONSE_BUDGET_WIRE_BYTES, compact_json
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import RetiredStorageError, load_model_state

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The script under test, through the shared cache: one module object with ``test_response_budget.py``.
measure_mcp_scale = measure_mcp_scale_module()


def _model_digest(model_root: Path) -> list[tuple[str, str]]:
    """Every model file's POSIX path and SHA-256, sorted — the "wrote nothing" oracle."""
    return [
        (
            path.relative_to(model_root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(model_root.rglob("*.yaml"), key=lambda p: p.as_posix())
    ]


@pytest.fixture
def basic_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of ``examples/basic`` (a staged change set still appends to its audit log), and the host channels.

    Both variables are set through ``monkeypatch`` so the script's own plain assignments are reverted.
    """
    project = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project)
    manifest = project / "swcraft.yaml"
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))
    monkeypatch.setenv("SWCRAFT_REVIEW", "stage-only")
    return manifest


#: Small enough that even a two-element layer's ``index`` does not fit, so a chain runs to its end.
TINY_BUDGET = 120


def _run(manifest: Path, *, replay: bool = False) -> None:
    asyncio.run(measure_mcp_scale.run(manifest, "basic", replay))


def test_the_sweep_runs_and_names_the_form_of_every_layer_row(
    basic_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(basic_project)
    printed = capsys.readouterr().out

    assert "One call per read-only tool, each at its DEFAULT form" in printed
    assert "### The three forms per layer" in printed
    # The heading names all three forms, so a row cannot be read as "the layer".
    assert "| layer | n | full | summary | index |" in printed

    # The rows are the contract's layers, in its order, read off the live registry rather than the
    # directories under `model/`. Both ends of the slice are asserted first, so a reworded line fails
    # by name rather than slicing the wrong text.
    assert "\n`x` marks" in printed
    table = printed.split("### The three forms per layer", 1)[1].split("\n`x` marks", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| layer |")]
    cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
    assert [row[0] for row in cells] == [layer.name for layer in current_registry().layers]
    # At the production budget nothing on `examples/basic` is refused: every cell is numeric.
    for row in cells:
        assert len(row) == 2 + len(measure_mcp_scale.FORMS), row
        for cell in row[1:]:
            assert cell.replace(",", "").isdigit(), row


def test_the_film_replay_runs_both_prompts_and_the_hub_section(
    basic_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(basic_project, replay=True)
    printed = capsys.readouterr().out

    assert "### prompt 1 — " in printed
    assert "### prompt 2 — " in printed
    assert "wire bytes of tool output in total" in printed
    assert "The hubs in every form" in printed
    assert "### The exit gate" in printed
    # Every tool of the replayed sequence appears in a row.
    for tool in ("discover", "read", "change_set", "impact_of", "resource_report"):
        assert f"| {tool} |" in printed


def test_the_exit_gate_states_the_published_budget_and_the_clause_it_gates(
    basic_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate's two load-bearing lines, asserted rather than its heading.

    ``budget`` is read off ``discover``'s published ``reads`` block, and the first clause is guarded by
    ``if budget and ...``: a renamed block would make the budget ``0`` and the clause vacuously true.
    """
    _run(basic_project, replay=True)
    printed = capsys.readouterr().out

    assert f"* budget, as `discover` publishes it: {RESPONSE_BUDGET_WIRE_BYTES:,} wire bytes" in printed
    assert "* answered responses ABOVE the budget: 0 — the gate's first clause" in printed


def test_a_lowered_budget_drives_the_refusal_half_of_the_replay(
    basic_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of the script nothing else executes: refusals, and the chain walked.

    ``examples/basic`` refuses nothing at the shipped budget; the server reads the constant at the
    call, so lowering it is enough.
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", TINY_BUDGET)
    _run(basic_project, replay=True)
    printed = capsys.readouterr().out

    # The gate publishes the lowered budget: one knob, end to end.
    assert f"* budget, as `discover` publishes it: {TINY_BUDGET} wire bytes" in printed
    assert "refused `over-budget`" in printed

    # The gate's first clause bites: the unbounded tools are over the lowered budget and are counted.
    above = next(line for line in printed.splitlines() if "answered responses ABOVE the budget" in line)
    assert not above.startswith("* answered responses ABOVE the budget: 0 "), above

    # The chain was walked: the swc `read` refused at `summary` is followed by its replayed hint.
    rows = [line for line in printed.splitlines() if line.startswith("| read |")]
    swc_rows = [row for row in rows if '{"layer": "swc"}' in row]
    assert len(swc_rows) >= 2, swc_rows
    assert '"form": "index"' in swc_rows[-1], "the last step of the walked chain is the narrowest form"

    assert "chain exhausted" in printed


def test_a_lowered_budget_marks_the_sweeps_refused_cells(
    basic_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's other half: a refused cell carries the refusal's own figure, marked."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", TINY_BUDGET)
    _run(basic_project)
    printed = capsys.readouterr().out

    swc_row = next(line for line in printed.splitlines() if line.startswith("| swc | "))
    assert "** x" in swc_row, swc_row
    # The element count comes off the refusal's `measured`.
    assert "| swc | 2 |" in swc_row


def test_the_film_replay_writes_nothing_to_the_model(basic_project: Path) -> None:
    model_root = basic_project.parent / "model"
    before = _model_digest(model_root)
    _run(basic_project, replay=True)
    assert _model_digest(model_root) == before, (
        "the replay changed the model. It runs under SWCRAFT_REVIEW=stage-only, where a change "
        "set is staged and validated in a private copy and nothing is ever applied."
    )


def test_the_replays_change_set_is_staged_rather_than_refused(
    basic_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Keeps the test above from passing vacuously: a refused change set also leaves the model unchanged.
    _run(basic_project, replay=True)
    printed = capsys.readouterr().out
    change_set_rows = [line for line in printed.splitlines() if line.startswith("| change_set |")]
    assert len(change_set_rows) == 1
    assert change_set_rows[0].endswith("| ok |"), change_set_rows[0]


class _Result:
    """One tool result in the one-copy shape, as much of it as the script reads.

    A shape, not a measurement: the tests using it assert what the script does with a refusal.
    """

    def __init__(self, body: dict[str, Any] | str) -> None:
        text = body if isinstance(body, str) else compact_json(body)
        self.structured_content = None
        self.meta = None
        self.content: list[Any] = [SimpleNamespace(type="text", text=text)]


class _FakeClient:
    """A client that answers a scripted list of results and records what it was asked.

    Drives ``Session.walk`` with refusal shapes that only a wide model would produce.
    """

    def __init__(self, results: list[_Result]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool: str, args: dict[str, Any], raise_on_error: bool = False) -> _Result:
        self.calls.append((tool, args))
        return self.results.pop(0)


def _walked(results: list[_Result], tool: str, **args: Any) -> list[tuple[str, dict[str, Any]]]:
    """``Session.walk`` over *results*, returning the calls it made."""
    client = _FakeClient(results)
    session = measure_mcp_scale.Session(client)
    asyncio.run(session.walk(tool, "section", **args))
    return client.calls


def test_a_refusal_is_reported_with_its_chain_in_the_order_it_offers_it() -> None:
    # `outcome_of` is every refused cell's text. The two "not a budget refusal" rows are held here alone:
    # the script issues no invalid request, so nothing else holds both branches to one reading of
    # `chain_of`'s `None`.
    with_chain = _Result(
        {
            "response": "error",
            "error": "over-budget",
            "measured": {"wire_bytes": 700_000, "budget": 80_000, "elements": 90},
            "hints": [{"form": "summary"}, {"form": "index"}],
        }
    )
    exhausted = _Result(
        {
            "response": "error",
            "error": "over-budget",
            "measured": {"wire_bytes": 120_000, "budget": 80_000, "elements": 900},
        }
    )
    # Different outcomes: `impact_of`'s `full` refusal names a live one-step chain, not an exhausted one.
    text_with_a_chain = _Result('ERROR: this answer measures 160000 wire bytes ... Ask for it with form="summary".')
    text_ending_in_the_count = _Result(
        "ERROR: this answer measures 110000 wire bytes over 900 dependent(s), above the "
        "server's 80000-byte budget; it is refused rather than truncated. Ask for it with "
        'form="count", limit=null, after=null.'
    )
    text_not_a_budget_refusal = _Result('ERROR: unknown form \'index\'; use "summary" or "full"')
    # A refusal whose code is not `over-budget` carries no `hints`; read as `()` it would print as an
    # exhausted chain that the exit gate's `chain == ()` count disagrees with.
    protocol_not_a_budget_refusal = _Result(
        {"response": "error", "error": "unknown-field", "message": "no field 'colour' on kind 'base_type'"}
    )

    assert measure_mcp_scale.outcome_of(with_chain) == (
        "refused `over-budget`, would be 700,000 B, hints [summary, index]"
    )
    assert measure_mcp_scale.outcome_of(exhausted) == (
        "refused `over-budget`, would be 120,000 B, hints [] — chain exhausted"
    )
    assert measure_mcp_scale.outcome_of(text_with_a_chain) == "refused (text), next request [summary]"
    assert measure_mcp_scale.outcome_of(text_ending_in_the_count) == "refused (text), next request [count]"
    assert measure_mcp_scale.outcome_of(text_not_a_budget_refusal).startswith("refused (text), not a budget refusal")
    assert measure_mcp_scale.outcome_of(protocol_not_a_budget_refusal) == (
        "refused `unknown-field`, not a budget refusal"
    )
    assert measure_mcp_scale.outcome_of(_Result({"response": "read", "value": []})) == "ok"

    # Only the genuinely terminal ones end a chain.
    assert measure_mcp_scale.chain_of(with_chain) == ("summary", "index")
    assert measure_mcp_scale.chain_of(exhausted) == ()
    assert measure_mcp_scale.chain_of(text_with_a_chain) == ("summary",)
    assert measure_mcp_scale.chain_of(text_ending_in_the_count) == ("count",)
    assert measure_mcp_scale.chain_of(text_not_a_budget_refusal) is None
    assert measure_mcp_scale.chain_of(protocol_not_a_budget_refusal) is None
    # The cell and the count are read from the same distinction.
    for row in (protocol_not_a_budget_refusal, text_not_a_budget_refusal):
        assert "chain exhausted" not in measure_mcp_scale.outcome_of(row)
    for row in (exhausted,):
        assert measure_mcp_scale.chain_of(row) == ()
        assert "chain exhausted" in measure_mcp_scale.outcome_of(row)


def test_a_hint_is_labelled_by_what_makes_it_narrower_and_not_by_its_form_alone() -> None:
    """A hint is labelled by what makes it narrower, not by its form alone.

    A paged hint carries the form it was already served in, a member slice and a filter ride along
    unchanged, and a discover hint has no address: each would print as the form the request had.
    """
    label = measure_mcp_scale.hint_label
    assert label({"operation": "read", "address": {"layer": "swc"}, "form": "summary"}) == "summary"
    assert label({"operation": "read", "address": {"layer": "connectors"}, "form": "full", "limit": 50}) == (
        "full+limit=50"
    )
    assert label({"operation": "read", "address": {"layer": "swc"}, "form": "index", "limit": 50, "after": "S9"}) == (
        "index+limit=50+after=S9"
    )
    assert label({"operation": "discover", "form": "summary"}) == "discover project:summary"
    assert label({"operation": "discover", "layer": "swc", "form": "full"}) == "discover swc:full"
    # The text tool's parsed hint, and a hint carrying nothing this script can name.
    assert label({"depth": 1, "limit": 50}) == "depth=1+limit=50"
    assert label({}) == "?"


def test_the_text_chains_next_request_is_read_out_of_the_sentence() -> None:
    """The sentences ``server._narrower_impact_request`` builds, parsed by shape rather than keyed.

    The shape is `key=value` clauses inside "Ask for it with ..."; the last step asks for the count
    with the page keys as ``null``, which the parse reads as keys to drop.
    """
    parse = measure_mcp_scale.text_hint
    # The live sentences, built by the server: literals here would stay green if the wording changed.
    assert parse(_narrower_impact_request("full", None, None)) == {"form": "summary"}
    assert parse(_narrower_impact_request("summary", None, None)) == {"depth": 1}
    # A refused request that carried a cursor drops it with the depth step.
    assert parse(_narrower_impact_request("summary", None, "x")) == {"depth": 1, "after": None}

    # And the last: the count, with the page dropped, at a depth with or without a cursor.
    count = {"form": "count", "limit": None, "after": None}
    assert parse(_narrower_impact_request("summary", 1, None)) == count
    assert parse(_narrower_impact_request("summary", 1, "x")) == count
    assert parse("... truncated. There is no narrower form of this report.") is None
    assert parse('ERROR: unknown form \'index\'; use "summary" or "full"') is None


@pytest.mark.parametrize(
    ("form", "depth", "after", "expected"),
    [
        ("full", None, None, {"form": "summary"}),
        ("summary", None, None, {"depth": 1}),
        ("summary", None, "x", {"depth": 1, "after": None}),
        ("summary", 1, None, {"form": "count", "limit": None, "after": None}),
    ],
)
@pytest.mark.parametrize("direction", ["dependents", "dependencies"])
def test_the_parse_still_holds_on_the_whole_refusal_with_the_cli_sentence(form, depth, after, expected, direction):
    """The refusal ends with the CLI command; the parse still reads the first ``Ask for it with ...``.

    Built by the server's own ``_within_budget``, so the message is the one a walker receives.
    """
    from swcraft_mcp.server import _within_budget

    text = "x" * (RESPONSE_BUDGET_WIRE_BYTES + 1)
    message = _within_budget(text, form, 3, name="ISig", direction=direction, depth=depth, after=after)
    assert "--project PATH" in message, message

    assert measure_mcp_scale.text_hint(message) == expected


def test_the_walk_replays_a_read_hint_whole_and_not_its_form_alone() -> None:
    """A hint is a complete ``readRequest``, and every key of it is part of the request.

    A member-slice refusal hints the same slice (filter, collection and cursor replayed); forwarding
    only ``address`` and ``form`` would walk to a different question.
    """
    hint = {
        "operation": "read",
        "address": {"layer": "swc"},
        "members": "ports",
        "form": "summary",
        "where": [{"field": "interface", "equals": "ISpeed"}],
        "limit": 50,
        "after": {"element": "S0", "member": "r24"},
    }
    refused = _Result({"response": "error", "error": "over-budget", "hints": [hint]})
    answered = _Result({"response": "read", "form": "summary", "value": []})

    calls = _walked([refused, answered], "read", address={"layer": "swc"}, members="ports")

    assert [tool for tool, _args in calls] == ["read", "read"]
    assert calls[1][1] == {key: value for key, value in hint.items() if key != "operation"}


def test_the_walk_follows_a_discover_hint_to_the_other_tool() -> None:
    """``discover`` is budgeted too, so the session's first call can refuse; its hint goes to ``discover``."""
    refused = _Result(
        {
            "response": "error",
            "error": "over-budget",
            "measured": {"wire_bytes": 90_000, "budget": 80_000, "elements": 10},
            "hints": [{"operation": "discover", "form": "summary"}],
        }
    )
    answered = _Result({"response": "discover", "form": "summary", "layers": []})

    calls = _walked([refused, answered], "discover")

    assert calls == [("discover", {}), ("discover", {"form": "summary"})]


def test_the_text_chain_is_cumulative_because_the_sentence_names_only_what_changes() -> None:
    """``impact_of``'s refusal names only the narrowing to add, so the walker keeps the rest of the request."""
    ask_for_summary = _Result('ERROR: ... truncated. Ask for it with form="summary".')
    ask_for_a_bounded_page = _Result("ERROR: ... truncated. Ask for it with depth=1, limit=50.")
    answered = _Result("Impact of 'ISig' (summary, depth 1):")

    calls = _walked(
        [ask_for_summary, ask_for_a_bounded_page, answered],
        "impact_of",
        name="ISig",
        direction="dependencies",
        form="full",
    )

    assert calls[1][1] == {"name": "ISig", "direction": "dependencies", "form": "summary"}
    assert calls[2][1] == {"name": "ISig", "direction": "dependencies", "form": "summary", "depth": 1, "limit": 50}


def test_the_text_chains_last_hint_drops_the_page_keys_it_names_as_null() -> None:
    """The count refuses a page, so the walk must not carry the one the step before it added."""
    ask_for_the_count = _Result('ERROR: ... truncated. Ask for it with form="count", limit=null, after=null.')
    counted = _Result({"response": "impact", "name": "ISig", "matched": {"interface": {"total": 20, "by_kind": {}}}})

    calls = _walked(
        [ask_for_the_count, counted], "impact_of", name="ISig", form="summary", depth=1, limit=50, after="x"
    )

    assert calls[1][1] == {"name": "ISig", "form": "count", "depth": 1}
    assert measure_mcp_scale.carried(counted) == "counted: interface 20"


def test_the_discover_form_order_is_the_protocols_set() -> None:
    """``DISCOVER_FORM_ORDER`` places every form the protocol declares, once, and no other."""
    order = measure_mcp_scale.DISCOVER_FORM_ORDER
    assert sorted(order) == sorted(DISCOVER_FORMS)
    assert len(set(order)) == len(order)


def test_the_sweep_measures_discover_in_both_forms(basic_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``discover``'s size is a function of the contract; both forms show what the default replaced."""
    _run(basic_project)
    printed = capsys.readouterr().out

    assert "### `discover` in both forms" in printed
    rows = [line for line in printed.splitlines() if line.startswith("| discover |")]
    forms = [json.loads(row.split("|")[2].strip().strip("`"))["form"] for row in rows if '"form"' in row]
    # Widest first, so a before/after table reads down from the column that moved.
    assert forms == list(measure_mcp_scale.DISCOVER_FORM_ORDER) == ["full", "summary", "baseline"]


def test_the_replay_answers_the_exit_gates_four_questions_and_the_two_dead_ends(
    basic_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit gate as calls: the member slice, the forward walk, the projection, the page.

    ``examples/basic`` answers each in one call, so this asserts each question was asked in the
    gate's shape and the projection's client-side half was computed; not the figures.
    """
    _run(basic_project, replay=True)
    printed = capsys.readouterr().out

    assert "The exit gate's four questions, each walked to an answer" in printed
    rows = [line for line in printed.splitlines() if line.startswith("| read |") or line.startswith("| impact_of |")]
    asked = [row for row in rows if '"members": "ports"' in row]
    assert asked, "question 1 is the member slice, filtered at the members"
    assert [row for row in rows if '"direction": "dependencies"' in row], "question 2 is the forward direction"
    assert [row for row in rows if '"layer": "tasks"' in row and '"period"' in row], "question 3 is the projection"
    assert [row for row in rows if '"depth": 1' in row], "question 4 is the bounded, paged report"
    assert [row for row in rows if '"layer": "connectors"' in row], "and the first known dead end"

    # What each answer carried: a count and a truncation flag, which no byte figure states.
    # `examples/basic` has tasks, so question 3 is witnessed non-empty.
    assert "* Q1 — every port on `IExample`, with its owner: 2 row(s), unpaged" in printed
    assert '* Q3 — the `period` projection: {"total": 1, "returned": 1, "truncated": false}, of which 1 of 1' in printed
    assert "have `period` at most 0.01 s" in printed
    assert "* Q4 — the bounded dependents of `IExample`: (depth: 1; page: 2 of 2, complete)" in printed
    assert "* the exit gate's four questions and the two known dead ends: " in printed


def test_the_hubs_are_chosen_by_counting_ports(basic_project: Path) -> None:
    # `hubs` puts the two largest components in the acceptance table, and must stay a measurement.
    model_root = basic_project.parent / "model"
    rows = measure_mcp_scale.hubs(load_model_state(model_root), 2)
    # One port each, so the tie-break decides: the table must name the same hubs on every run.
    assert rows == [("ExampleConsumerSWC", 1), ("ExampleSWC", 1)]


def test_the_hubs_rank_by_port_count_before_the_name(tmp_path: Path) -> None:
    """On a model where counts differ, the ranking is by port count first.

    The widest component's name sorts last, so a name-only sort cannot produce the expected answer.
    """
    model_root = tmp_path / "model"
    scale_model.build_wide_model(model_root)
    (model_root / "swc" / "Narrow.yaml").write_text(
        "name: Narrow\ntype: application\nports:\n  - name: pOnly\n    direction: provide\n    interface: ISig\n",
        encoding="utf-8",
        newline="\n",
    )

    rows = measure_mcp_scale.hubs(load_model_state(model_root), 3)

    counts = [ports for _name, ports in rows]
    assert counts == sorted(counts, reverse=True), rows
    assert counts[0] == scale_model.WIDE_PORTS_PER_SWC
    assert ("Narrow", 1) not in rows, "a one-port component must not outrank a fifty-port one"


def test_the_counting_helpers_answer_literals_on_the_example(basic_project: Path) -> None:
    """Literals read once off ``examples/basic``: recomputing them would repeat the script's own counting."""
    state = load_model_state(basic_project.parent / "model")

    assert measure_mcp_scale.heaviest(state) == (("IExample", 2), ("ExampleConsumerSWC", 1))


def test_a_stub_port_counts_for_its_component_and_for_no_interface(tmp_path: Path) -> None:
    """An external stub's port has no ``interface``; ``None`` must not rank as the heaviest interface."""
    model_root = tmp_path / "model"
    (model_root / "swc" / "stubs").mkdir(parents=True)
    (model_root / "swc" / "stubs" / "Stub.yaml").write_text(
        "name: Stub\ntype: application\nports:\n"
        "  - name: pA\n    direction: provide\n"
        "  - name: pB\n    direction: provide\n"
        "  - name: rC\n    direction: require\n"
        "  - name: pSig\n    direction: provide\n    interface: ISig\n",
        encoding="utf-8",
        newline="\n",
    )
    state = load_model_state(model_root)

    assert measure_mcp_scale.heaviest(state) == (("ISig", 1), ("Stub", 4))


def test_a_model_with_no_interface_or_no_swc_is_refused_by_name(tmp_path: Path) -> None:
    """Nothing to name is a refusal that says so, never a bare ``IndexError`` or an invented key."""
    stub_only = tmp_path / "stub_only"
    (stub_only / "swc").mkdir(parents=True)
    (stub_only / "swc" / "Stub.yaml").write_text(
        "name: Stub\ntype: application\nports:\n  - name: pA\n    direction: provide\n",
        encoding="utf-8",
        newline="\n",
    )
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError, match="no port of the model names an interface"):
        measure_mcp_scale.heaviest(load_model_state(stub_only))
    with pytest.raises(ValueError, match="the model declares no SWC"):
        measure_mcp_scale.heaviest(load_model_state(empty))


def test_a_retired_layout_is_refused_before_any_tool_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader's ``RetiredStorageError`` passes through before the client is built."""
    project = tmp_path / "retired"
    (project / "model" / "data_types").mkdir(parents=True)
    (project / "model" / "data_types" / "Speed.yaml").write_text(
        "name: Speed\ncategory: VALUE\n", encoding="utf-8", newline="\n"
    )
    manifest = project / "swcraft.yaml"
    manifest.write_text("name: retired\n", encoding="utf-8", newline="\n")
    # `run` assigns `os.environ` directly; monkeypatch first so nothing leaks into a later test.
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))
    monkeypatch.setenv("SWCRAFT_REVIEW", "stage-only")

    def no_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the client was built for a project the loader refuses")

    monkeypatch.setattr("fastmcp.Client", no_client)

    with pytest.raises(RetiredStorageError):
        asyncio.run(measure_mcp_scale.run(manifest, "retired", False))


def test_a_structured_half_is_refused_rather_than_counted() -> None:
    two_copies = _Result({"response": "discover"})
    two_copies.structured_content = {"response": "discover"}

    with pytest.raises(ValueError, match="two copies"):
        measure_mcp_scale.wire_bytes(two_copies)


def test_the_wire_size_is_each_block_escaped_once() -> None:
    result = _Result('a "quoted"\nline')
    result.content.append(SimpleNamespace(type="text", text="note"))

    assert measure_mcp_scale.wire_bytes(result) == len('"a \\"quoted\\"\\nline"') + len('"note"')


@pytest.mark.parametrize("text", ["[1]", "42", '"a string"', "not json", ""])
def test_a_first_block_that_is_no_json_object_has_no_body(text: str) -> None:
    result = _Result(text)

    assert measure_mcp_scale.body_of(result) is None
    assert measure_mcp_scale.text_of(result) == text


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Impact of 'X' (summary):\n  a\n(page 1 of 3, 50 of 120 shown)", "(page 1 of 3, 50 of 120 shown)"),
        ("Impact of 'X' (full):\n  a", "answered whole (no page, no bound)"),
        ("", ""),
        ("  \n ", ""),
    ],
    ids=["footer", "no-footer", "empty", "blank"],
)
def test_what_a_plain_text_report_carried_is_its_footer(report: str, expected: str) -> None:
    assert measure_mcp_scale.carried(_Result(report)) == expected


def test_what_a_mapping_body_carried_is_its_page_or_its_row_count() -> None:
    page = {"total": 900, "returned": 50, "truncated": True}

    assert measure_mcp_scale.carried(_Result({"response": "read", "page": page})) == json.dumps(page)
    assert measure_mcp_scale.carried(_Result({"response": "read", "value": [1, 2]})) == "2 row(s), unpaged"
    assert measure_mcp_scale.carried(_Result({"response": "read"})) == ""
