"""Tests for the ``model_diff`` and ``impact_of`` MCP tools.

Both wrap ``swcraft_tools.snapshot``/``differ``/``impact`` and reuse their renderers; neither writes an audit entry.
``impact_of`` answers text, so a refusal above the response budget is an ``ERROR:`` sentence, not an
``errorResult``; with ``form``, ``direction``, ``depth`` and the ``limit``/``after`` page it offers a refusal chain,
walked from both ends on the wide model in the last section. ``model_diff`` answers a bounded summary by default and
the ``format_diff`` text with ``form="full"``; its tests git-init the project themselves.

**What this module does not reach.** Most cases call the tools as Python functions, a narrower surface than
``tools/call``: FastMCP validates arguments against the declared types first, so an argument handed to the body
directly may be refused by the transport before the body runs (as ``null`` is for ``form`` and ``direction``; the
wire tests state that half).
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
import scale_model
import yaml
from conftest import model_element_path, write_model_element
from fastmcp.exceptions import ToolError
from protocol_client import call
from scratch_git import git as _git
from swcraft_mcp.server import _text_wire_bytes, impact_of, model_diff
from swcraft_tools import differ, snapshot
from swcraft_tools.editing import read as read_mod


def _project_root(tmp_path: Path) -> Path:
    # _project_env writes swcraft.yaml directly into tmp_path.
    return tmp_path


# ---------------------------------------------------------------------------
# model_diff
# ---------------------------------------------------------------------------


def test_model_diff_default_head_vs_working_tree(model_root, tmp_path):
    """model_diff() with no args diffs HEAD vs the current working tree."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    # Uncommitted working-tree change to an existing SWC.
    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_path.write_text(
        swc_path.read_text(encoding="utf-8").replace("ports:", "description: modified sensor\nports:"), encoding="utf-8"
    )

    result = model_diff(form="full")

    assert "~ SpeedSensorSWC" in result
    assert "modified sensor" in result


def test_model_diff_two_named_revisions(model_root, tmp_path):
    """model_diff(rev_a=X, rev_b=Y) diffs two named revisions, ignoring the working tree."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)
    first_rev = _git("rev-parse", "HEAD", cwd=root).stdout.strip()

    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_path.write_text(
        swc_path.read_text(encoding="utf-8").replace("ports:", "description: modified sensor\nports:"), encoding="utf-8"
    )
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "modify SpeedSensorSWC", cwd=root)
    second_rev = _git("rev-parse", "HEAD", cwd=root).stdout.strip()

    # A further uncommitted change must not leak into a two-rev diff.
    swc_path.write_text(swc_path.read_text(encoding="utf-8") + "\n# stray uncommitted comment\n", encoding="utf-8")

    result = model_diff(rev_a=first_rev, rev_b=second_rev, form="full")

    assert "~ SpeedSensorSWC" in result
    assert "modified sensor" in result
    assert "stray uncommitted comment" not in result


def test_model_diff_no_changes(model_root, tmp_path):
    """Diffing a revision against an identical working tree reports no changes."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    assert model_diff(form="full") == "No changes."
    assert model_diff() == {
        "response": "diff",
        "rev_a": "HEAD",
        "rev_b": "",
        "layers": {},
        "totals": {"added": 0, "removed": 0, "changed": 0},
    }


def _committed_then_changed(model_root, tmp_path) -> None:
    """The project committed once, then one component changed and one interface added in the working tree."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)
    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_path.write_text(
        swc_path.read_text(encoding="utf-8").replace("ports:", "description: modified sensor\nports:"),
        encoding="utf-8",
        newline="\n",
    )
    interfaces = Path(model_root) / "interfaces"
    added = (interfaces / "ISpeed.yaml").read_text(encoding="utf-8").replace("name: ISpeed", "name: ISpeedCopy", 1)
    (interfaces / "ISpeedCopy.yaml").write_text(added, encoding="utf-8", newline="\n")


def test_model_diff_summarises_by_default(model_root, tmp_path):
    """The default answer is the per-layer summary: counts and identities, no field values."""
    _committed_then_changed(model_root, tmp_path)

    result = model_diff()

    assert result["response"] == "diff"
    assert result["layers"]["swc"]["changed"] == {"count": 1, "identities": ["SpeedSensorSWC"]}
    assert result["layers"]["interfaces"]["added"] == {"count": 1, "identities": ["ISpeedCopy"]}
    assert result["totals"] == {"added": 1, "removed": 0, "changed": 1}
    assert "modified sensor" not in json.dumps(result)


def test_model_diff_full_is_the_text_format_diff_renders(model_root, tmp_path):
    """`form="full"` is the CLI's text, byte for byte."""
    _committed_then_changed(model_root, tmp_path)
    old = snapshot.load_snapshot(model_root, rev="HEAD")
    new = snapshot.load_snapshot(model_root)

    assert model_diff(form="full") == differ.format_diff(differ.diff_snapshots(old, new))


def test_model_diff_full_over_the_budget_is_refused_naming_the_summary(model_root, tmp_path, monkeypatch):
    """Above the budget the text is refused, never cut: the summary is the hint, the CLI the whole answer."""
    _committed_then_changed(model_root, tmp_path)
    monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 10)

    result = model_diff(form="full")

    assert result.startswith("ERROR: this answer measures ")
    assert "above the server's 10-byte budget" in result
    assert 'Ask for it with form="summary".' in result
    assert "`swcraft diff HEAD --project PATH` answers it with no response budget" in result


def test_model_diff_summary_over_the_budget_is_refused_naming_the_cli(model_root, tmp_path, monkeypatch):
    """The summary bounds its names, not their bytes: above the budget it is refused too, never cut."""
    _committed_then_changed(model_root, tmp_path)
    summary = model_diff()
    assert isinstance(summary, dict)
    monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", read_mod.measure(summary) - 1)

    result = model_diff()

    assert result == (
        f"ERROR: this answer measures {read_mod.measure(summary)} wire bytes, above the server's "
        f"{read_mod.measure(summary) - 1}-byte budget; it is refused rather than truncated. "
        "`swcraft diff HEAD --project PATH` answers it with no response budget (PATH is the project's "
        "`swcraft.yaml`)."
    )
    monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", read_mod.measure(summary))
    assert model_diff() == summary


def test_model_diff_full_refusal_names_both_revisions(model_root, tmp_path, monkeypatch):
    """Two named revisions are carried into the CLI command the refusal names."""
    _committed_then_changed(model_root, tmp_path)
    root = _project_root(tmp_path)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "second", cwd=root)
    monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 10)

    result = model_diff(rev_a="HEAD~1", rev_b="HEAD", form="full")

    assert "`swcraft diff 'HEAD~1' HEAD --project PATH`" in result  # shell-quoted, as `shlex.quote` spells it


@pytest.mark.parametrize("form", ["count", "index", "", "FULL"])
def test_model_diff_refuses_an_unknown_form(model_root, form):
    """Two forms only; anything else is refused naming them."""
    result = model_diff(form=form)

    assert result == f"ERROR: unknown form {form!r}; expected one of: summary, full."


def test_model_diff_invalid_revision_returns_error_string(model_root, tmp_path):
    """An unresolvable revision surfaces as an 'ERROR: ...' string, not a raised exception."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    result = model_diff(rev_a="not-a-real-revision")

    assert result.startswith("ERROR:")


def test_model_diff_duplicate_identity_key_returns_error_string(model_root, tmp_path):
    """A duplicate identity key raised by diff_snapshots() surfaces as an 'ERROR: ...' string."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    (swc_path.parent / "SpeedSensorSWC_dup.yaml").write_text(swc_path.read_text(encoding="utf-8"), encoding="utf-8")

    result = model_diff()

    assert result.startswith("ERROR:")
    assert "duplicate key" in result


def test_model_diff_malformed_yaml_returns_error_string(model_root, tmp_path):
    """Malformed YAML is answered as an 'ERROR: ...' string.

    The parse error arrives wrapped as ElementParseError, a ValueError, the one exception the tool catches; a raw
    yaml.YAMLError that leaked would propagate and fail this case."""
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_path.write_text("name: [unclosed\n", encoding="utf-8")

    result = model_diff()

    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# impact_of
# ---------------------------------------------------------------------------


def test_impact_of_full_returns_expected_grouped_report(model_root):
    """impact_of('ISpeed', form="full") reports the SWCs whose ports use it, as the CLI's text, details included."""
    result = impact_of("ISpeed", form="full")

    assert "Impact of 'ISpeed':" in result
    assert "interface:" in result
    assert "SpeedSensorSWC" in result
    assert "SpeedCtrlSWC" in result
    assert "via port" in result


def test_impact_of_defaults_to_the_summary_form(model_root):
    """The default is the summary form, and its header says so.

    The same dependents, grouped by layer with a count, without the ``detail`` strings.
    """
    result = impact_of("ISpeed")

    assert "Impact of 'ISpeed' (summary):" in result
    assert "SpeedSensorSWC" in result
    assert "SpeedCtrlSWC" in result
    assert "via port" not in result
    assert len(result) < len(impact_of("ISpeed", form="full"))


def test_impact_of_no_match_returns_not_found_message(model_root):
    """Both forms share the one line there is nothing to narrow about."""
    assert impact_of("NoSuchElement") == "Element 'NoSuchElement' not found in any layer."
    assert impact_of("NoSuchElement", form="full") == "Element 'NoSuchElement' not found in any layer."


def test_impact_of_unknown_form_is_refused_and_names_the_three(model_root):
    """A plain-text tool refuses in text: ``impact_of`` is no editing-protocol operation, unlike ``read``."""
    result = impact_of("ISpeed", form="index")

    # The one wording model_diff refuses an unknown form in, over impact_of's three.
    assert result == "ERROR: unknown form 'index'; expected one of: count, summary, full."


def test_impact_of_full_over_the_budget_refuses_and_offers_the_summary(model_root, monkeypatch):
    """Refused, never truncated -- and the refusal carries the request that fits.

    The budget is lowered rather than the model widened: lowering the wire constant moves what is enforced and what
    the refusal reports together.
    """
    answered = impact_of("ISpeed", form="full")
    dependents = len([line for line in answered.splitlines() if line.startswith("  ")])
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 200)

    result = impact_of("ISpeed", form="full")

    assert result.startswith("ERROR:")
    assert "200" in result  # the budget, as measured against
    assert "wire bytes" in result
    # The count is of the answer that was not sent, not of the refusal.
    assert f"over {dependents} dependent(s)" in result
    assert 'form="summary"' in result
    assert "SpeedSensorSWC" not in result  # nothing of the answer leaks through


def test_impact_of_summary_over_the_budget_offers_the_bounded_walk(model_root, monkeypatch):
    """The chain's second step: a summary over the budget offers the depth-1 request.

    The width is already narrowest, so the next reduction is how far the walk goes. The step carries no page: a
    summary with no ``limit`` is already paged at twenty. The budget is the summary's size minus one, since a tiny
    budget would refuse everything and not tell a summary-specific refusal apart.
    """
    fits = _text_wire_bytes(impact_of("ISpeed"))
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", fits - 1)

    result = impact_of("ISpeed")

    assert result.startswith("ERROR:")
    assert "wire bytes" in result
    assert 'form="summary"' not in result  # already the narrowest width
    assert "Ask for it with depth=1." in result
    assert "limit=" not in result
    assert "SpeedSensorSWC" not in result


def test_the_not_found_line_is_held_to_the_same_budget_in_both_forms(model_root, monkeypatch):
    """A miss is a report too, and a lowered budget refuses it -- over zero dependents.

    The count comes from the answer, here a sentence: a renderer that counted the message's lines would report one.
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 10)

    for form in ("full", "summary"):
        result = impact_of("NoSuchElement", form=form)
        assert result.startswith("ERROR:"), form
        assert "over 0 dependent(s)" in result, form


def test_impact_of_at_its_declared_default_answers_the_summary(model_root):
    """The omitted argument answers the summary; an explicit ``null`` is refused, because ``null`` is no omission."""
    assert impact_of("ISpeed") == impact_of("ISpeed", form="summary")
    assert impact_of("ISpeed", form=None).startswith("ERROR:")
    assert "summary" in impact_of("ISpeed", form=None)


def test_a_null_form_never_reaches_this_tools_body_over_the_wire(model_root):
    """Over ``tools/call`` a ``null`` form is a FastMCP tool error, not the body's ``ERROR:`` line.

    The ``ERROR:`` line is what a Python caller sees; ``form`` is declared ``str``, so the transport refuses first.
    """
    with pytest.raises(ToolError, match="form"):
        call("impact_of", name="ISpeed", form=None)
    # And the argument the tool really does take at the wire answers.
    assert call("impact_of", name="ISpeed", form="summary").texts[0].startswith("Impact of 'ISpeed' (summary):")


def test_impact_of_under_the_budget_answers_in_both_forms(model_root):
    """The admitted side of the same bound, on the shipped budget."""
    assert impact_of("ISpeed", form="full").startswith("Impact of 'ISpeed':")
    assert impact_of("ISpeed").startswith("Impact of 'ISpeed' (summary):")


def test_impact_of_duplicate_identity_key_returns_error_string(model_root):
    """A duplicate identity key raised by build_reference_graph() surfaces as an 'ERROR: ...' string."""
    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    (swc_path.parent / "SpeedSensorSWC_dup.yaml").write_text(swc_path.read_text(encoding="utf-8"), encoding="utf-8")

    result = impact_of("SpeedSensorSWC")

    assert result.startswith("ERROR:")
    assert "duplicate key" in result


def test_impact_of_malformed_yaml_returns_error_string(model_root):
    """Malformed YAML is answered as an 'ERROR: ...' string (an ElementParseError, a ValueError)."""
    swc_path = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_path.write_text("name: [unclosed\n", encoding="utf-8")

    result = impact_of("SpeedSensorSWC")

    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Read-only convention: neither tool writes an audit entry
# ---------------------------------------------------------------------------


def test_neither_tool_writes_audit_entry(model_root, tmp_path):
    root = _project_root(tmp_path)
    _git("init", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "initial commit", cwd=root)

    from swcraft_tools.auditor import AuditLog

    # The project's real log: the server holds no auditor of its own.
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    before = len(auditor.get_entries())

    model_diff()
    impact_of("ISpeed")

    after = len(auditor.get_entries())
    assert after == before == 0


# ---------------------------------------------------------------------------
# Direction, depth, page, and the refusal chain
#
# The walks and the page are tested in `packages/tools/tests/`; here: the tool's four arguments, its text
# refusals, and the hub report.
# ---------------------------------------------------------------------------


@pytest.fixture
def wide_project(tmp_path: Path) -> Path:
    """The shared wide model (``scale_model``), written into the autouse fixture's own project.

    ``ISig`` is named by all twenty components, each named by twenty-five connectors: a hub interface.
    """
    scale_model.build_wide_model(tmp_path / "model")
    return tmp_path


def test_impact_of_walks_the_forward_direction_when_asked(model_root):
    """The same edges the other way: what ``SpeedSensorSWC`` needs, not what needs it."""
    result = impact_of("SpeedSensorSWC", direction="dependencies", form="full")

    assert "Impact of 'SpeedSensorSWC':" in result
    assert "ISpeed" in result
    assert "via port" in result
    # The footer names the direction, because the header cannot.
    assert result.endswith("(direction: dependencies)")


def test_impact_of_defaults_to_the_dependents_direction(model_root):
    """The unnamed call walks the dependents and prints no footer."""
    assert impact_of("ISpeed") == impact_of("ISpeed", direction="dependents")
    assert "\n\n(" not in impact_of("ISpeed")


def test_impact_of_unknown_direction_is_refused_and_names_the_two(model_root):
    """A plain-text tool refuses in text, and the two names come off the engine."""
    result = impact_of("ISpeed", direction="sideways")

    assert result.startswith("ERROR:")
    assert "'sideways'" in result
    assert '"dependents" (the default)' in result
    assert '"dependencies"' in result


def test_impact_of_at_a_bounded_depth_answers_the_first_level_and_says_so(model_root):
    """``depth`` bounds the walk; the footer echoes it, as ``form`` echoes in the header.

    The negatives are the assertion: the footer is rendered from ``depth`` without asking the walk, so a walk that
    ignored ``depth`` would pass the positives. The two rows below are second-hop rows.
    """
    bounded = impact_of("ISpeed", depth=1)
    unbounded = impact_of("ISpeed")

    assert bounded.startswith("Impact of 'ISpeed' (summary):")
    assert bounded.endswith("(depth: 1)")
    assert "SpeedSensorSWC" in bounded

    assert "Speed_to_Ctrl" in unbounded and "Speed_to_Ctrl" not in bounded
    assert "PowertrainCtrl" in unbounded and "PowertrainCtrl" not in bounded


def test_impact_of_a_requirement_lists_the_elements_that_satisfy_it(model_root):
    """A requirement is depended upon by the components that satisfy it.

    Changing `REQ_001` reaches `SpeedSensorSWC` and, through it, its connector and composition; `ISpeed`'s impact
    names no requirement.
    """
    report = impact_of("REQ_001")

    assert report.startswith("Impact of 'REQ_001' (summary):")
    assert "requirement:" in report
    assert "SpeedSensorSWC" in report
    assert "REQ_001" not in impact_of("ISpeed")


@pytest.mark.parametrize(("name", "depth"), [("ISpeed", None), ("ISpeed", 1), ("ISpeed", 2), ("NoSuchElement", 1)])
def test_swcraft_impact_prints_this_tools_full_form_byte_for_byte(
    model_root, tmp_path, monkeypatch, capsys, name, depth
):
    """``swcraft impact [--depth N]`` prints ``impact_of(depth=N, form="full")`` plus a newline, footer included.

    A name no layer carries is one text too: the not-found sentence, then the depth footer.
    """
    from swcraft_tools.cli import main as cli_main

    bound = [] if depth is None else ["--depth", str(depth)]
    monkeypatch.setattr("sys.argv", ["swcraft", "impact", name, "--project", str(tmp_path / "swcraft.yaml"), *bound])
    cli_main()
    printed = capsys.readouterr().out

    answered = impact_of(name, depth=depth, form="full")
    assert not answered.startswith("ERROR"), answered
    assert printed == answered + "\n"


def test_impact_of_at_a_depth_below_one_is_refused_by_the_engine(model_root):
    """``0`` can only be a mistake, and the engine's ValueError is the tool's ERROR line."""
    result = impact_of("ISpeed", depth=0)

    assert result.startswith("ERROR:")
    assert "depth must be at least 1" in result


def test_impact_of_refuses_a_depth_below_one_for_a_name_no_layer_carries(model_root):
    """The refusal does not depend on the model containing the name.

    The defect: the depth check ran inside the per-match walk, so an unknown name was answered as not found with a
    ``(depth: 0)`` footer instead of refused.
    """
    result = impact_of("NoSuchElementAnywhere", depth=0)

    assert result.startswith("ERROR:")
    assert "depth must be at least 1" in result

    found = impact_of("NoSuchElementAnywhere", depth=1)
    assert found.startswith("Element 'NoSuchElementAnywhere' not found in any layer.")


def test_impact_of_with_a_limit_reports_the_whole_set_and_the_cursor(model_root):
    """The page block a text tool can carry: ``<returned> of <total>`` and ``next``.

    ``total`` is read off the unpaged report: a window of one out of four differs from one out of one.
    """
    whole = impact_of("ISpeed")
    paged = impact_of("ISpeed", limit=1)

    total = sum(len(line.split(":", 1)[1].split(",")) for line in whole.splitlines() if line.startswith("  "))
    assert total > 1, whole
    assert f"page: 1 of {total}" in paged
    assert 'next: after="' in paged
    assert "truncated" in paged
    # Paging bounds rows, not characters: on a report this small one row plus the totals outweighs the summary.
    rows = [line for line in paged.splitlines() if line.startswith("  ")]
    assert len(rows) == 1, paged
    assert sum(len(row.split(":", 1)[1].split(",")) for row in rows) == 1, paged
    whole_counts = [
        f"{line.strip().split(' (', 1)[0]} {line.split('(', 1)[1].split(')', 1)[0]}"
        for line in whole.splitlines()
        if line.startswith("  ")
    ]
    assert f"in all: {', '.join(whole_counts)}; " in paged, (whole, paged)

    last = impact_of("ISpeed", limit=total)
    assert f"page: {total} of {total}, complete" in last
    assert "next: after=" not in last


@pytest.mark.parametrize("form", ["summary", "full"])
def test_a_truncated_impact_of_page_ends_with_the_whole_report_s_count_per_kind(model_root, form):
    """Both forms carry the one footer, and its counts sum to the page's total."""
    paged = impact_of("ISpeed", form=form, limit=1)

    footer = paged.splitlines()[-1]
    match = re.fullmatch(r'\(page: 1 of (\d+), truncated; in all: ([^;]+); next: after="[^"]+"\)', footer)
    assert match, footer
    counts = [int(part.rsplit(" ", 1)[1]) for part in match.group(2).split(", ")]
    assert sum(counts) == int(match.group(1))

    complete = impact_of("ISpeed", form=form, limit=int(match.group(1)))
    assert "in all:" not in complete


def test_impact_of_pages_forward_through_the_cursor_it_published(model_root):
    """A keyset walk: the cursor is replayed verbatim and the window moves."""
    first = impact_of("ISpeed", limit=1)
    cursor = _next_cursor(first)

    second = impact_of("ISpeed", limit=1, after=cursor)

    assert second.startswith("Impact of 'ISpeed' (summary):")
    assert cursor.split(":", 1)[1] not in second


def test_impact_of_refuses_a_cursor_no_row_carries(model_root):
    """The row was edited away; a keyset says so instead of restarting silently."""
    result = impact_of("ISpeed", limit=1, after="interface/swc:Gone")

    assert result.startswith("ERROR:")
    assert "interface/swc:Gone" in result


def test_impact_of_refuses_after_without_a_limit_on_full_and_answers_it_on_summary(model_root):
    """ "Everything after X" with no bound is the unbounded report the page replaces — on ``full``.

    On ``summary`` the cursor continues the default page of twenty, so a default page's ``next`` replays as printed.
    """
    refused = impact_of("ISpeed", form="full", after="interface/swc:SpeedSensorSWC")

    assert refused.startswith("ERROR:")
    assert "limit" in refused

    first = impact_of("ISpeed", limit=1)
    cursor = _next_cursor(first)
    answered = impact_of("ISpeed", after=cursor)

    assert answered.startswith("Impact of 'ISpeed' (summary):")
    assert answered == impact_of("ISpeed", limit=20, after=cursor)


def test_impact_of_refuses_a_limit_below_one(model_root):
    result = impact_of("ISpeed", limit=0)

    assert result.startswith("ERROR:")
    assert "limit must be at least 1" in result


def test_a_report_that_matched_nothing_is_not_paged(model_root):
    """A name no layer carries answers with the name, ``limit`` or no ``limit``.

    A ``page: 0 of 0`` footer would describe a listing of no element.
    """
    unpaged = impact_of("NoSuchElement")

    assert impact_of("NoSuchElement", limit=5) == unpaged
    assert "page:" not in unpaged


def test_a_cursor_against_a_report_that_matched_nothing_is_answered_not_refused(model_root):
    """The one exception to refusing a cursor no row carries: there is no listing it could point into.

    The not-found line answers on both forms. On ``full``, ``after`` without ``limit`` is still refused: that is
    decided before the model is read.
    """
    unpaged = impact_of("NoSuchElement")
    unpaged_full = impact_of("NoSuchElement", form="full")

    assert impact_of("NoSuchElement", limit=5, after="interface/swc:Gone") == unpaged
    assert impact_of("NoSuchElement", after="interface/swc:Gone") == unpaged
    assert impact_of("NoSuchElement", form="full", limit=5, after="interface/swc:Gone") == unpaged_full
    assert impact_of("NoSuchElement", form="full", after="interface/swc:Gone").startswith("ERROR:")


def test_the_hub_report_that_had_no_answer_now_answers_its_first_page_and_then_at_depth_one(
    wide_project: Path, monkeypatch
) -> None:
    """A hub interface answers its first page, and under a lowered budget the refusal names the way out.

    The default summary of the hub is its first page of twenty. The budget is then lowered to the size of the answer
    the chain offers: everything wider than ``depth=1`` is refused, and that request itself fits.
    """
    default_page = impact_of("ISig")

    assert not default_page.startswith("ERROR:")
    assert "page: 20 of 520, truncated; in all: swc 20, connector 500; next: after=" in default_page

    fits = _text_wire_bytes(impact_of("ISig", depth=1))
    assert fits < _text_wire_bytes(default_page)
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", fits)

    full = impact_of("ISig", form="full")
    summary = impact_of("ISig")
    answered = impact_of("ISig", depth=1)

    assert full.startswith("ERROR:")
    assert 'Ask for it with form="summary".' in full
    assert summary.startswith("ERROR:")
    assert "Ask for it with depth=1." in summary
    assert "limit=" not in summary.split("`", 1)[0]
    assert not answered.startswith("ERROR:")
    assert answered.endswith("(depth: 1)")  # twenty rows: answered whole, no page footer
    assert "S0" in answered


def test_the_chain_offers_the_count_once_the_depth_is_already_bounded(wide_project: Path, monkeypatch) -> None:
    """Cumulative, one step at a time: a bounded summary that still does not fit is offered the count.

    A summary is already paged at twenty, so a smaller page would be a sample; the count is the whole.
    """
    fits = _text_wire_bytes(impact_of("ISig", depth=1, limit=1))
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", fits)

    result = impact_of("ISig", depth=1)

    assert result.startswith("ERROR:")
    assert 'Ask for it with form="count", limit=null, after=null.' in result
    assert "depth=1" not in result  # already bounded; the step is not offered twice


def test_the_chain_ends_in_the_count_when_the_report_is_already_summary_bounded_and_paged(
    wide_project: Path, monkeypatch
) -> None:
    """The end of the chain is an answer: the count, not a dead end.

    The caller can lower its own ``limit``, so the server does not search for a smaller one; it offers the count with
    the page keys set to ``null``, because the chain is cumulative and ``count`` refuses a page. The budget is one
    byte under this call's measure, since a literal would stop refusing when the fixture or renderer moves.
    """
    answered = impact_of("ISig", depth=1, limit=1)
    assert not answered.startswith("ERROR:"), answered
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", _text_wire_bytes(answered) - 1)

    result = impact_of("ISig", depth=1, limit=1)

    assert result.startswith("ERROR:")
    assert 'Ask for it with form="count", limit=null, after=null.' in result
    assert "There is no narrower form" not in result


def test_the_refused_chain_never_offers_a_step_the_request_already_carries():
    """The negative of the three steps: each narrowing is offered at most once.

    It asks the sentence builder directly, with no model. The third argument is the refused request's ``after``: the
    depth step drops it (``after=null``), since it may name a row the depth-1 walk does not carry.
    """
    from swcraft_mcp.server import _narrower_impact_request

    assert _narrower_impact_request("full", None, None) == ' Ask for it with form="summary".'
    assert _narrower_impact_request("full", 1, "interface/swc:S0") == ' Ask for it with form="summary".'
    assert _narrower_impact_request("summary", None, None) == " Ask for it with depth=1."
    assert _narrower_impact_request("summary", None, "x") == " Ask for it with depth=1, after=null."
    count = ' Ask for it with form="count", limit=null, after=null.'
    assert _narrower_impact_request("summary", 1, None) == count
    assert _narrower_impact_request("summary", 1, "x") == count


def test_the_walked_chain_ends_in_a_count_answer_and_its_last_hint_carries_no_page_key(
    wide_project: Path, monkeypatch
) -> None:
    """Walked from ``full`` the way an agent walks it — each hint merged onto the refused request.

    Under a budget below every rendered report of the hub, the three steps are taken in order and the last request
    answers: the count, with no page key left (a ``null`` clause drops the key). The merge is written here, not
    borrowed from ``scripts/measure_mcp_scale.py``, so the server's sentence is read by a second parser.
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 40)
    request: dict[str, object] = {"name": "ISig", "form": "full"}
    steps = []
    answer = impact_of(**request)
    while isinstance(answer, str):
        assert answer.startswith("ERROR:"), answer
        (clauses,) = re.findall(r"Ask for it with ([^.]+)\.", answer)[:1]
        for clause in clauses.split(", "):
            key, _, value = clause.partition("=")
            request[key] = None if value == "null" else value.strip('"') if value.startswith('"') else int(value)
        request = {key: value for key, value in request.items() if value is not None}
        steps.append(dict(request))
        answer = impact_of(**request)

    assert steps[-1] == {"name": "ISig", "form": "count", "depth": 1}
    assert [step["form"] for step in steps] == ["summary", "summary", "count"]
    assert answer["response"] == "impact"
    assert answer["matched"]["interface"]["total"] == 20


# ---------------------------------------------------------------------------
# The default page of a summary: a walk of more than twenty rows answers its first twenty; a walk of twenty or
# fewer is answered whole.
# ---------------------------------------------------------------------------


def _uint8_and_interfaces(root: Path, names: list[str]) -> None:
    """The base type ``uint8`` and one sender-receiver interface over it per name in *names*."""
    write_model_element(root, {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})
    for name in names:
        write_model_element(
            root,
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": "uint8"}],
            },
        )


def _write_swc(root: Path, group: str, payload: dict) -> None:
    """Write the component *payload* as ``swc/<group>/<name>.yaml`` under *root*."""
    path = root / "swc" / group / f"{payload['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")


def _hub(tmp_path: Path, components: int) -> None:
    """An interface ``IHub`` named by *components* components and nothing else.

    No connector and no composition, so the dependents walk of ``IHub`` is exactly *components* rows, one hop deep.
    """
    root = tmp_path / "model"
    _uint8_and_interfaces(root, ["IHub"])
    port = {"name": "pOut", "direction": "provide", "interface": "IHub"}
    for i in range(components):
        _write_swc(root, "hub", {"name": f"H{i:02d}", "ports": [dict(port)]})


def _rows(text: str) -> int:
    """The dependents a summary text lists -- the identities after each kind's colon.

    Splitting on ``,`` is safe: the fixtures' identifiers carry no comma.
    """
    return sum(len(line.split(":", 1)[1].split(",")) for line in text.splitlines() if line.startswith("  "))


def _next_cursor(text: str) -> str:
    """The cursor a truncated page's footer prints as ``next: after="…"``."""
    match = re.search(r'next: after="([^"]+)"', text)
    assert match, text
    return match.group(1)


def _wide_component(tmp_path: Path, interfaces: int) -> None:
    """A component ``W`` with one port on each of *interfaces* interfaces, all over one base type.

    Its dependencies walk is the interfaces and the one base type they share:
    *interfaces* + 1 rows, so the forward direction is paged at the same size.
    """
    root = tmp_path / "model"
    _uint8_and_interfaces(root, [f"I{i:02d}" for i in range(interfaces)])
    ports = [{"name": f"p{i:02d}", "direction": "provide", "interface": f"I{i:02d}"} for i in range(interfaces)]
    _write_swc(root, "wide", {"name": "W", "ports": ports})


def test_a_summary_of_twenty_one_rows_answers_its_first_twenty_with_the_whole_walk_s_totals(tmp_path: Path) -> None:
    """Twenty rows, the ``page`` figures, the ``in all`` totals of the whole walk and ``next``."""
    _hub(tmp_path, 21)

    answer = impact_of("IHub")
    counted = impact_of("IHub", form="count")

    assert _rows(answer) == 20, answer
    footer = answer.splitlines()[-1]
    match = re.fullmatch(r'\(page: 20 of 21, truncated; in all: ([^;]+); next: after="([^"]+)"\)', footer)
    assert match, footer
    by_kind: dict[str, int] = {}
    for figures in counted["matched"].values():
        for kind, count in figures["by_kind"].items():
            by_kind[kind] = by_kind.get(kind, 0) + count
    assert match.group(1) == ", ".join(f"{kind} {count}" for kind, count in by_kind.items())
    # The default page is the explicit page of the same size, byte for byte.
    assert answer == impact_of("IHub", limit=20)

    # Replaying `after` alone continues to the last row.
    rest = impact_of("IHub", after=match.group(2))
    assert _rows(rest) == 1, rest
    assert rest.splitlines()[-1].startswith("(page: 1 of 21, truncated; in all: ")
    assert "next: after=" not in rest


def test_a_summary_of_twenty_rows_is_answered_whole_with_no_footer(tmp_path: Path) -> None:
    """A walk of one page or less is answered whole: every row, and no ``page:`` line."""
    _hub(tmp_path, 20)

    answer = impact_of("IHub")

    assert _rows(answer) == 20, answer
    assert "page:" not in answer
    assert "\n\n(" not in answer
    assert answer == impact_of("IHub", limit=20).rsplit("\n\n(", 1)[0]


def test_the_default_page_honours_and_echoes_the_depth(tmp_path: Path) -> None:
    _hub(tmp_path, 21)

    answer = impact_of("IHub", depth=1)

    assert _rows(answer) == 20, answer
    assert answer.splitlines()[-1].startswith("(depth: 1; page: 20 of 21, truncated; ")
    assert answer == impact_of("IHub", depth=1, limit=20)


def test_full_with_no_limit_is_the_whole_walk_and_refuses_after_alone(tmp_path: Path) -> None:
    """``full`` is not paged by default: its reader asked for every edge's detail."""
    _hub(tmp_path, 21)

    full = impact_of("IHub", form="full")
    cursor = _next_cursor(impact_of("IHub"))

    assert "page:" not in full
    assert sum(1 for line in full.splitlines() if "via port" in line) == 21, full
    assert impact_of("IHub", form="full", after=cursor).startswith("ERROR:")


def test_an_explicit_limit_beats_the_default_page_both_ways(tmp_path: Path) -> None:
    """A client ``limit`` above twenty answers that many rows; below twenty, that few."""
    _hub(tmp_path, 21)

    wide = impact_of("IHub", limit=50)
    narrow = impact_of("IHub", limit=5)

    assert _rows(wide) == 21, wide
    assert wide.splitlines()[-1] == "(page: 21 of 21, complete)"
    assert _rows(narrow) == 5, narrow
    assert narrow.splitlines()[-1].startswith("(page: 5 of 21, truncated; ")


def test_a_stale_cursor_sent_alone_on_summary_is_refused_and_named(model_root) -> None:
    """``after`` alone continues a default page, so a cursor no row carries is refused there too."""
    result = impact_of("ISpeed", after="interface/swc:Gone")

    assert result.startswith("ERROR:")
    assert "interface/swc:Gone" in result


def test_the_default_page_holds_in_the_dependencies_direction(tmp_path: Path) -> None:
    """The forward walk is paged at twenty the same way, and ``after`` alone continues it."""
    _wide_component(tmp_path, 21)
    counted = impact_of("W", form="count", direction="dependencies")
    total = sum(figures["total"] for figures in counted["matched"].values())
    assert total > 20, counted

    first = impact_of("W", direction="dependencies")

    assert _rows(first) == 20, first
    footer = first.splitlines()[-1]
    match = re.search(rf'page: 20 of {total}, truncated; in all: [^;]+; next: after="([^"]+)"\)$', footer)
    assert match, footer
    rest = impact_of("W", direction="dependencies", after=match.group(1))
    assert _rows(rest) == total - 20, rest
    assert "next: after=" not in rest


def test_a_default_page_refused_at_a_bounded_depth_is_offered_the_count_over_its_page(
    tmp_path: Path, monkeypatch
) -> None:
    """At a depth the chain's next step is the count, and the refusal counts the page's rows."""
    _hub(tmp_path, 21)
    answered = impact_of("IHub", depth=1)
    assert answered == impact_of("IHub", depth=1, limit=20)
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", _text_wire_bytes(answered) - 1)

    result = impact_of("IHub", depth=1)

    assert result.startswith("ERROR:")
    assert " over 20 dependent(s)" in result
    assert 'Ask for it with form="count", limit=null, after=null.' in result


def test_a_refused_continued_page_is_offered_depth_one_without_its_cursor(wide_project: Path, monkeypatch) -> None:
    """A continued default page (``after`` alone) refused at no depth: the hint drops the cursor.

    The second page's cursor names a connector two hops from the hub, which the depth-1 walk does not carry, so the
    hint is ``depth=1, after=null``.
    """
    first = impact_of("ISig")
    second = impact_of("ISig", after=_next_cursor(first))
    cursor = _next_cursor(second)
    assert impact_of("ISig", depth=1, after=cursor).startswith("ERROR:")  # why the cursor is dropped

    merged = impact_of("ISig", depth=1)
    assert not merged.startswith("ERROR:"), merged

    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", _text_wire_bytes(second) - 1)
    refused = impact_of("ISig", after=_next_cursor(first))

    assert refused.startswith("ERROR:"), refused
    assert "Ask for it with depth=1, after=null." in refused


def test_the_count_answers_per_matched_layer_what_the_summary_lists(model_root) -> None:
    """The structured count, held to the summary's own rows on the same walk."""
    summary = impact_of("ISpeed")
    rows = [line.strip() for line in summary.splitlines() if line.startswith("  ")]

    counted = impact_of("ISpeed", form="count")

    assert counted == {
        "response": "impact",
        "name": "ISpeed",
        "direction": "dependents",
        "depth": None,
        "matched": {
            "interface": {
                "total": sum(int(row.split(" (", 1)[1].split(")", 1)[0]) for row in rows),
                "by_kind": {row.split(" (", 1)[0]: int(row.split(" (", 1)[1].split(")", 1)[0]) for row in rows},
            }
        },
    }


def test_the_count_respects_the_depth_and_the_direction(model_root) -> None:
    for direction in ("dependents", "dependencies"):
        for depth in (None, 1):
            counted = impact_of("SpeedSensorSWC", form="count", direction=direction, depth=depth)
            listed = impact_of("SpeedSensorSWC", direction=direction, depth=depth)
            assert counted["direction"] == direction and counted["depth"] == depth
            rows = [
                line
                for line in listed.splitlines()
                if line.startswith("  ")
                and " (" in line
                and line.strip() not in {"(no dependents)", "(no dependencies)"}
            ]
            total = sum(int(row.strip().split(" (", 1)[1].split(")", 1)[0]) for row in rows)
            assert sum(figures["total"] for figures in counted["matched"].values()) == total, (direction, depth)


def test_the_count_of_a_name_no_layer_declares_matches_nothing(model_root) -> None:
    assert impact_of("NoSuchElement", form="count")["matched"] == {}


@pytest.mark.parametrize("page", [{"limit": 5}, {"after": "interface/swc:S0"}, {"limit": 5, "after": "x"}])
def test_the_count_is_never_paged(model_root, page) -> None:
    refused = impact_of("ISpeed", form="count", **page)

    assert isinstance(refused, str) and refused.startswith("ERROR:")
    assert "never paged" in refused


def test_the_count_is_never_refused_for_size(model_root, monkeypatch) -> None:
    """Under a one-byte budget every rendered form refuses and the count still answers."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 1)

    assert impact_of("ISpeed").startswith("ERROR:")
    assert impact_of("ISpeed", form="count")["matched"]["interface"]["total"] >= 2


def test_the_count_goes_on_the_wire_as_one_json_document(model_root) -> None:
    answered = call("impact_of", name="ISpeed", form="count")

    assert len(answered.texts) == 1
    assert json.loads(answered.texts[0]) == impact_of("ISpeed", form="count")


def test_the_four_new_arguments_read_null_the_way_each_one_declares_it(model_root):
    """``direction`` refuses ``null``; for ``depth``, ``limit`` and ``after`` ``null`` is the omission.

    ``direction: str`` refuses ``null`` as an unknown value; the other three default to ``null``.
    """
    unnamed = impact_of("ISpeed")

    assert impact_of("ISpeed", depth=None, limit=None, after=None) == unnamed
    assert impact_of("ISpeed", direction=None).startswith("ERROR:")
    assert "None" in impact_of("ISpeed", direction=None)


def test_the_four_new_arguments_read_null_the_way_each_one_declares_it_at_the_wire(model_root):
    """The same split over ``tools/call``.

    A ``null`` ``direction`` is a transport refusal; the other three answer the unnamed call's bytes.
    """
    unnamed = call("impact_of", name="ISpeed").texts[0]

    with pytest.raises(ToolError, match="direction"):
        call("impact_of", name="ISpeed", direction=None)
    assert call("impact_of", name="ISpeed", depth=None, limit=None, after=None).texts[0] == unnamed


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_the_transport_coerces_a_non_integer_depth_or_limit_before_this_body_runs(model_root, value):
    """FastMCP coerces ``true``, ``1.0`` and ``"1"`` to ``1`` before the body runs.

    ``depth`` and ``limit`` are ``int | None`` and pydantic's lax mode coerces, so no ``TypeError`` can escape the
    body here; the library's whole-number guard serves a Python caller.
    """
    one_hop = call("impact_of", name="ISpeed", depth=1).texts[0]
    one_row = call("impact_of", name="ISpeed", limit=1).texts[0]

    assert call("impact_of", name="ISpeed", depth=value).texts[0] == one_hop
    assert call("impact_of", name="ISpeed", limit=value).texts[0] == one_row


@pytest.mark.parametrize("key", ["depth", "limit"])
def test_a_fractional_depth_or_limit_is_a_transport_refusal_and_never_an_answer(model_root, key):
    """Lax mode coerces but does not accept anything: ``1.5`` is a tool error and the body never runs.

    What neither test reaches: a host that forwards an argument without validating it against the published schema;
    the library guard answers one.
    """
    with pytest.raises(ToolError, match=key):
        call("impact_of", name="ISpeed", **{key: 1.5})


# ---------------------------------------------------------------------------
# The `full` form names every detail of the reaching edges: every port of a component reached through several,
# and a connector's endpoints. `summary` carries no detail and does not move.
# ---------------------------------------------------------------------------


def _write_three_port_model(model_root: Path) -> None:
    """Three provide ports on ``IBrakeRequest``, the second connected, so the endpoint detail is visible."""
    write = scale_model._write
    write(model_root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    write(
        model_element_path(model_root, "uint8_base", kind="base_type"),
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
    )
    # A data element's `implementation_type` names an implementation type, never a base type.
    write(
        model_element_path(model_root, "uint8", kind="value_type"),
        {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
    )
    write(
        model_root / "interfaces" / "IBrakeRequest.yaml",
        {
            "name": "IBrakeRequest",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint8"}],
        },
    )
    write(
        model_root / "swc" / "brake" / "BrakeController.yaml",
        {
            "name": "BrakeController",
            "ports": [
                {"name": "pBrakeRequest", "direction": "provide", "interface": "IBrakeRequest"},
                {"name": "pBrakeRequestAux", "direction": "provide", "interface": "IBrakeRequest"},
                {"name": "pBrakeRequestDiag", "direction": "provide", "interface": "IBrakeRequest"},
            ],
        },
    )
    write(
        model_root / "swc" / "brake" / "BrakeConsumer.yaml",
        {
            "name": "BrakeConsumer",
            "ports": [{"name": "rBrakeRequest", "direction": "require", "interface": "IBrakeRequest"}],
        },
    )
    write(
        model_root / "connectors" / "Brake_link.yaml",
        {
            "name": "Brake_link",
            "from": {"swc": "BrakeController", "port": "pBrakeRequestAux"},
            "to": {"swc": "BrakeConsumer", "port": "rBrakeRequest"},
        },
    )


@pytest.fixture
def three_port_project(tmp_path: Path) -> Path:
    _write_three_port_model(tmp_path / "model")
    return tmp_path


#: `impact_of("IBrakeRequest")` at its default form: identities and counts only, so no detail may reach it.
_THREE_PORT_SUMMARY = (
    "Impact of 'IBrakeRequest' (summary):\n\n"
    "interface:\n"
    "  swc (2): BrakeConsumer, BrakeController\n"
    "  connector (1): Brake_link"
)


def test_impact_of_full_names_every_port_of_the_reaching_edge(three_port_project: Path) -> None:
    lines = impact_of("IBrakeRequest", form="full").splitlines()

    assert "  swc BrakeController (via ports pBrakeRequest, pBrakeRequestAux, pBrakeRequestDiag)" in lines
    assert "  swc BrakeConsumer (via port rBrakeRequest)" in lines, "one edge still renders its singular bytes"
    assert [line for line in lines if line.startswith("  swc BrakeController")] == [
        "  swc BrakeController (via ports pBrakeRequest, pBrakeRequestAux, pBrakeRequestDiag)"
    ], "one line per dependent, however many edges reached it"


def test_impact_of_full_names_the_port_a_connector_was_reached_through(three_port_project: Path) -> None:
    assert (
        "  connector Brake_link (via port BrakeController.pBrakeRequestAux)"
        in impact_of("BrakeController", form="full").splitlines()
    )
    assert (
        "  connector Brake_link (via port BrakeConsumer.rBrakeRequest)"
        in impact_of("BrakeConsumer", form="full").splitlines()
    )
    # From the interface both owners are reached, so the line names both endpoints.
    assert (
        "  connector Brake_link (via ports BrakeController.pBrakeRequestAux, BrakeConsumer.rBrakeRequest)"
        in impact_of("IBrakeRequest", form="full").splitlines()
    )


def test_a_paged_full_form_keeps_every_detail_of_its_rows(three_port_project: Path) -> None:
    """The page windows rows and never narrows one: the plural and the endpoints survive it."""
    first = impact_of("IBrakeRequest", form="full", limit=2)
    assert "  swc BrakeController (via ports pBrakeRequest, pBrakeRequestAux, pBrakeRequestDiag)" in first.splitlines()
    cursor = _next_cursor(first)
    second = impact_of("IBrakeRequest", form="full", limit=2, after=cursor)
    assert (
        "  connector Brake_link (via ports BrakeController.pBrakeRequestAux, BrakeConsumer.rBrakeRequest)"
        in second.splitlines()
    )


def test_impact_of_summary_does_not_move_by_a_byte(three_port_project: Path) -> None:
    assert impact_of("IBrakeRequest") == _THREE_PORT_SUMMARY
    assert impact_of("IBrakeRequest", form="summary") == _THREE_PORT_SUMMARY


def test_the_wide_hubs_full_form_names_every_port_and_still_fits_the_budget(wide_project: Path) -> None:
    """The wider ``full`` on the wide model names every port and both endpoints, and still fits the budget.

    ``ISig`` is named by all fifty ports of each of twenty components and reached by five hundred connectors. The
    byte figure is not pinned: the answer is held to the budget like every other, never truncated to fit.
    """
    full = impact_of("ISig", form="full")
    summary = impact_of("ISig")
    lines = full.splitlines()

    assert full.startswith("Impact of 'ISig':"), "the wider full form is still admitted on the wide model"
    assert _text_wire_bytes(full) <= read_mod.RESPONSE_BUDGET_WIRE_BYTES
    ports = ", ".join(f"p{j}, r{j}" for j in range(scale_model.WIDE_PORTS_PER_SWC // 2))
    for i in range(scale_model.WIDE_SWCS):
        assert f"  swc S{i} (via ports {ports})" in lines
    connector_lines = [line for line in lines if line.startswith("  connector ")]
    assert len(connector_lines) == scale_model.WIDE_CONNECTORS
    assert all(line.endswith(")") and " (via ports S" in line for line in connector_lines)
    assert "via port" not in summary
    assert _text_wire_bytes(summary) < _text_wire_bytes(full)


# ---------------------------------------------------------------------------
# The refusal names the CLI, by direction
# ---------------------------------------------------------------------------
#
# Every size refusal ends with the CLI command that answers the same walk with no response budget. It comes after
# the narrower sentence, so a walker's parse of the first "Ask for it with ..." is untouched.

_CLI_COMMAND = re.compile(
    r"`(swcraft [^`]+)` answers it with no response budget \(PATH is the project's `swcraft\.yaml`\)\.$"
)


def _refused(name: str, monkeypatch, **request) -> str:
    """``impact_of``'s refusal of *request*, with the budget at ten bytes so every report is over it."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 10)
    result = impact_of(name, **request)
    assert result.startswith("ERROR:"), result
    return result


def _hinted_command(refusal: str) -> list[str]:
    """The argv the refusal's last sentence names, split the way a POSIX shell would split it."""
    match = _CLI_COMMAND.search(refusal)
    assert match, refusal
    return shlex.split(match.group(1))


@pytest.mark.parametrize("form", ["full", "summary"])
def test_a_dependents_refusal_names_swcraft_impact_with_the_project(model_root, monkeypatch, form) -> None:
    refusal = _refused("ISpeed", monkeypatch, form=form)

    assert _hinted_command(refusal) == ["swcraft", "impact", "ISpeed", "--project", "PATH"]
    # After the narrower sentence, which stays the first thing a walker reads.
    assert refusal.index("Ask for it with") < refusal.index("`swcraft impact")


def test_a_dependencies_refusal_names_query_deps_because_impact_walks_dependents_only(model_root, monkeypatch) -> None:
    refusal = _refused("SpeedSensorSWC", monkeypatch, direction="dependencies")

    assert _hinted_command(refusal) == ["swcraft", "query", "--deps", "SpeedSensorSWC", "--project", "PATH"]
    assert "swcraft impact" not in refusal


@pytest.mark.parametrize("direction", ["dependents", "dependencies"])
def test_the_hinted_command_carries_the_depth_and_never_the_page(model_root, monkeypatch, direction) -> None:
    refusal = _refused("ISpeed", monkeypatch, direction=direction, depth=2, limit=1)

    command = _hinted_command(refusal)
    assert command[-2:] == ["--depth", "2"]
    assert "--limit" not in command
    assert 'Ask for it with form="count", limit=null, after=null.' in refusal


def test_an_answer_within_the_budget_names_no_command(model_root) -> None:
    answered = impact_of("ISpeed")

    assert not answered.startswith("ERROR:")
    assert "swcraft impact" not in answered
    assert "--project" not in answered


def test_the_hint_names_the_placeholder_and_never_where_the_project_is_stored(model_root, tmp_path, monkeypatch):
    refusal = _refused("ISpeed", monkeypatch)

    assert str(tmp_path) not in refusal
    assert tmp_path.as_posix() not in refusal


def test_a_name_that_is_not_one_shell_word_stays_one_argument(model_root, monkeypatch) -> None:
    """A name no layer carries is still a report (the not-found line), and its hint still splits right."""
    refusal = _refused("No Such Element", monkeypatch)

    assert _hinted_command(refusal)[2] == "No Such Element"


@pytest.mark.parametrize(
    ("name", "direction", "depth"),
    [
        ("ISpeed", "dependents", None),
        ("ISpeed", "dependents", 1),
        ("SpeedSensorSWC", "dependencies", None),
        ("SpeedSensorSWC", "dependencies", 1),
    ],
)
def test_the_hinted_command_run_as_written_answers_the_refused_walk(
    model_root, tmp_path, monkeypatch, capsys, name, direction, depth
) -> None:
    """What the sentence promises: the command, with a real manifest for ``PATH``, answers the same walk whole.

    The budget stays at ten bytes while the CLI runs, which has none. ``impact`` is held to ``impact_of``'s full text
    byte for byte; ``query --deps`` to the walk ``impact_of`` reports, row for row, through its JSON document.
    """
    from swcraft_tools.cli import main as cli_main

    refusal = _refused(name, monkeypatch, direction=direction, depth=depth)
    manifest = str(tmp_path / "swcraft.yaml")
    argv = [manifest if token == "PATH" else token for token in _hinted_command(refusal)]

    if direction == "dependents":
        monkeypatch.setattr("sys.argv", argv)
        cli_main()
        printed = capsys.readouterr().out
        monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 10**9)
        assert printed == impact_of(name, depth=depth, form="full") + "\n"
        return

    monkeypatch.setattr("sys.argv", [*argv, "--format", "json"])
    cli_main()
    document = json.loads(capsys.readouterr().out)
    assert (document["name"], document["direction"], document["depth"]) == (name, direction, depth)
    reached = {
        layer: sorted((row["layer"], row["name"]) for row in rows) for layer, rows in document["matched"].items()
    }
    assert any(reached.values()), "the comparison is vacuous on an empty walk"
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 10**9)
    full = impact_of(name, direction=direction, depth=depth, form="full")
    # Both ways, so a `query --deps` that dropped a row, added one or ignored `--depth` fails here.
    assert reached == _impact_rows(full), (reached, full)


def _impact_rows(full: str) -> dict[str, list[tuple[str, str]]]:
    """``format_impact``'s report as ``{matched layer: sorted [(layer, name)]}``, a row per dependent line."""
    rows: dict[str, list[tuple[str, str]]] = {}
    section: list[tuple[str, str]] = []
    for line in full.splitlines()[1:]:
        if line.endswith(":") and not line.startswith(" "):  # the scope footer, `(direction: …)`, is no section
            section = rows.setdefault(line.removesuffix(":"), [])
        elif line.startswith("  ") and line.strip() != "(no dependents)":
            layer, name = line.split()[:2]
            section.append((layer, name))
    return {layer: sorted(found) for layer, found in rows.items()}
