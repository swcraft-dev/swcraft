"""The model contract's wiring in code, and the readings its closed half shares.

Four checks run here. No module outside ``packages/tools/src/`` names a ``reader.py`` wrapper (a textual
scan, so an aliased call is caught too; the extension has no Python and is outside the scan). No
``_NOT_A_FIGURE`` exemption matches a plain count, so the figure sweep cannot be switched off by a
pattern broad enough to exempt everything. The bump procedure's guard test exists in
``test_model_contract.py`` and is callable (it is not run here). And the ``cli`` package names
``check_cross_layer_consistency`` and none of the checks it composes, except the one allowance pinned
to the function that spends it, while still calling the composer.

The module also defines the readings and tables the closed half of this check imports
(``read_repo_file``, ``_NOT_A_FIGURE``, the wrapper scans). What it does not reach: no document is
read here -- the documents' figures, the documented wrapper list and the agent rule files'
``paths:`` globs are checked only by the closed half, under ``dev.py test-internal``; and the agent
guide's "Read first" table is checked by neither half, so a row there can go missing or stale
without a failure.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest
import test_model_contract
import validator_package
from conftest import REPO_ROOT, element_path
from emission_scan import constructs
from swcraft_tools.cli import main as cli_main
from swcraft_tools.validator import check_cross_layer_consistency


# Public: the closed half of this check imports it.
def read_repo_file(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"missing repository file: {relative}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The reader wrappers and the figure sweep
# ---------------------------------------------------------------------------


#: ``reader.py`` relative to :data:`REPO_ROOT`, for the wrapper measurement below.
_READER_MODULE = "packages/tools/src/swcraft_tools/reader.py"

#: The ``validator`` package, the heaviest caller, pinned by name rather than recomputed as
#: "the largest", which would move silently. Its figure is the sum over the package's modules.
_VALIDATOR_PACKAGE = "packages/tools/src/swcraft_tools/validator/"

#: The two generic entry points every wrapper delegates to: they name no layer, so they are
#: excluded by name rather than by a prefix rule that would have to guess.
_GENERIC_READER_ENTRY_POINTS = frozenset({"read_layer", "find_element_file"})

#: A public helper that names no layer read: it derives an SWC's placement from its file's location.
_READER_HELPERS = frozenset({"swc_placement"})


def _reader_wrappers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The public ``read_*`` and ``find_*_file`` wrappers, read off ``reader.py``."""
    public = [
        node.name
        for node in ast.parse(read_repo_file(_READER_MODULE)).body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and node.name not in _GENERIC_READER_ENTRY_POINTS | _READER_HELPERS
    ]
    reads = tuple(name for name in public if name.startswith("read_"))
    finds = tuple(name for name in public if name.startswith("find_") and name.endswith("_file"))
    assert sorted(reads + finds) == sorted(public), (
        f"a public reader function is neither a `read_*` nor a `find_*_file` wrapper: {public}. "
        "Classify it here before the count below can mean anything."
    )
    return reads, finds


def _wrapper_call_sites() -> list[tuple[str, str, str]]:
    """``(relative module path, construct, wrapper)`` of every call to a wrapper, over the package minus ``reader.py``.

    *construct* is the qualified name of the ``def`` or ``class`` the call is evaluated in (``""`` at module level):
    a decorator, default, annotation or base belongs to the enclosing one. Counted as :class:`ast.Call` nodes,
    because the claim is about what a module *does* and an import proves only what it names; a call is matched by
    a bare name or an attribute spelled as a wrapper's name.
    """
    reads, finds = _reader_wrappers()
    wrappers = frozenset(reads + finds)
    sites: list[tuple[str, str, str]] = []
    for path in sorted(
        (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools").rglob("*.py"), key=lambda path: path.as_posix()
    ):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == _READER_MODULE:
            continue
        for node, construct in constructs(ast.parse(path.read_text(encoding="utf-8"))).items():
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called in wrappers:
                sites.append((relative, construct, called))
    return sites


def _reader_wrapper_calls() -> Counter:
    """``module::construct -> calls`` to a wrapper (:func:`_wrapper_call_sites`)."""
    return Counter(f"{module}::{construct}" for module, construct, _wrapper in _wrapper_call_sites())


#: The wrapper whose caller count is stated on its own, because it was once documented as
#: having none.
_PLACEMENT_WRAPPER = "read_swc_model_with_placement"


def _reader_wrapper_calls_by_wrapper() -> dict[str, Counter]:
    """``wrapper -> (module::construct -> calls)``, the same scan keyed by wrapper.

    Answers "does *this* wrapper have a production caller", which the per-module count cannot.
    """
    reads, finds = _reader_wrappers()
    per_wrapper: dict[str, Counter] = {name: Counter() for name in reads + finds}
    for module, construct, wrapper in _wrapper_call_sites():
        per_wrapper[wrapper][f"{module}::{construct}"] += 1
    return per_wrapper


#: The three source trees the textual basis is measured over. `scripts/` is out because it is
#: not shipped and the claim is about the packages' Python API; the extension has no Python.
_MENTION_ROOTS = ("packages/tools/src", "packages/mcp/src", "packages/agent/src")


def _reader_wrapper_mentions() -> Counter:
    """``relative module path -> word-boundary mentions`` of a wrapper name.

    A different basis from :func:`_reader_wrapper_calls`: a module that only names a wrapper in
    a docstring counts here and not there. Regex over the source text rather than AST, because an
    AST walk would drop the comments and docstrings.

    **What it does not reach:** it counts a name, not a use -- a wrapper quoted in a string or
    named in a comment counts either way. It serves the token ban below, not a count.
    """
    reads, finds = _reader_wrappers()
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in sorted(reads + finds)) + r")\b")
    mentions: Counter = Counter()
    for root in _MENTION_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py"), key=lambda path: path.as_posix()):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == _READER_MODULE:
                continue
            found = len(pattern.findall(path.read_text(encoding="utf-8")))
            if found:
                mentions[relative] = found
    return mentions


def test_nothing_outside_tools_names_a_reader_wrapper() -> None:
    """Nothing in ``packages/mcp/`` or ``packages/agent/`` names a reader wrapper.

    Textual, the stronger basis: no call can hide behind an alias the AST scan would miss. The
    extension has no Python, so it is out of the scan by construction.
    """
    outside = sorted(
        relative for relative in _reader_wrapper_mentions() if not relative.startswith("packages/tools/src/")
    )
    assert outside == [], f"a module outside packages/tools/src names a reader wrapper: {outside}"
    assert any((REPO_ROOT / root).is_dir() for root in _MENTION_ROOTS[1:]), "the outside trees are missing"


#: Digit forms that are references rather than counts, with the reason each is not a figure;
#: every other digit run the closed half's sweep reads must sit inside a phrase it pins.
#:
#: This list is the whole exemption surface. Patterns rather than an allow-list of numbers: a
#: list of numbers would quietly exempt a real count that happened to equal an exempted one. An
#: exemption says a form is not a count, not that the reference is right.
_NOT_A_FIGURE: tuple[tuple[str, str], ...] = (
    (
        r"```[^`]*```",
        "inside a fenced code block: an example document or a listing, never prose. Its own "
        "exemption rather than a side effect of the inline-span rule, which is how it used to be "
        "covered",
    ),
    (
        r"`\d+`|(`[^`]*`)",
        "inside an inline-code span, with ONE exception, which is the capture group: a span holding "
        "nothing but digits (`12`) is not exempt, because that is exactly how a count would be "
        "written in a document that backticks almost every noun. The digit-only alternative is "
        "still MATCHED so that the scan stays paired on the backticks around it \u2014 it just "
        "contributes no exempt span. A range (`61\u201366`, `2111\u20132113`) or any letter makes the span "
        "a citation, a code fragment or an identifier",
    ),
    (r"\bADR \d+", "an ADR number"),
    (r"\bdecisions? \d+(?:\s*\([^)]*\))?(?: and \d+)?", "an ADR decision number"),
    (r"\bPhases? \d+(?:\.\d+)*", "a phase number"),
    (r"\bTasks? \d+", "a plan task number"),
    (
        r"\bchecks? \d+(?:\s*[–-]\s*\d+)?",
        "a named check of a gate module — `check 5`, `checks 1-5`. Its own reference, like an ADR "
        "or a decision number: `test_editing_registry_injection.py`'s checks are numbered in its "
        "docstring and each is a name, not a count. The COUNT of them is spelled in words (“six "
        "checks”), which this sweep does not walk at all",
    ),
    (
        r"\b\d+\.\d+(?:\.\d+)?\b",
        "a bare sub-phase or version number. The one pattern here that is NOT context-narrowed, and "
        "so the sweep's one remaining swallow: a count written with a decimal point would be "
        "exempted by it. Nothing separates `22.2` from `13.0` \u2014 both are two integers around a dot "
        "in a document about phases \u2014 and an allow-list of the phase numbers in use would exempt a "
        "real count that happened to equal one",
    ),
    (r"\bDraft \d+-\d+", "a JSON Schema draft"),
    (
        r"\bISO 26262(?:-\d+)?",
        "a standard's number and part, like an ADR number: `ISO 26262`, `ISO 26262-1`. The "
        "safety fields' `description`s quote it and the prose beside them names it, and no "
        "clause number follows — the ADR that owns those fields forbids one",
    ),
)

#: A count, written the way one would be written into the documents; no exemption may match it.
#: The figure must be one no artifact states, or a probe would read like a pinned real figure
#: and measure the document instead of itself; this one is deliberately none of the contract's
#: counts.
_A_BARE_COUNT = "The registry declares 31 collections in the model."


def test_no_not_a_figure_exemption_swallows_a_bare_count() -> None:
    """The breadth guard: no exemption may match a count written as a count.

    Without it, appending `(r"\\d+", "everything")` to `_NOT_A_FIGURE` would switch the sweep off.
    """
    swallowed = [reason for pattern, reason in _NOT_A_FIGURE if re.search(pattern, _A_BARE_COUNT)]
    assert swallowed == [], (
        f"_NOT_A_FIGURE entries match a plain count ({_A_BARE_COUNT!r}), so they exempt figures "
        f"rather than references: {swallowed}"
    )


def test_the_named_guard_test_exists() -> None:
    """The guard the bump procedure names is a real, callable test function.

    Resolved on the imported module, not grepped as text. It is not invoked: one version mismatch
    would fail twice under two names, and the call breaks once the guard takes a fixture.
    """
    guard = getattr(test_model_contract, "test_the_contract_records_the_product_version_it_was_transcribed_from", None)
    assert callable(guard), "the bump procedure names a guard test_model_contract.py does not define"


# ---------------------------------------------------------------------------
# The CLI names the composer and nothing it composes
# ---------------------------------------------------------------------------
#
# A hand-maintained list of the checks `check_cross_layer_consistency` runs, restated in the CLI,
# drifts silently (one once named four of nine); naming no composed check leaves nothing to keep
# in step.

#: The CLI package; every module of it is read, so a command's code cannot leave the scan by moving.
_CLI_SOURCE = "packages/tools/src/swcraft_tools/cli"

#: The one validator gate the CLI is allowed to name (the computed-value names below are no gate).
_COMPOSER = "check_cross_layer_consistency"

#: The one *composed* check the CLI may name, and the single function that may name it:
#: `resource-report` renders the hardware topology beside the budgets, and the renderer imports
#: no gate, so its composition root computes it. One check on a reporting path is not the
#: restated list on the gating path the rule keeps out.
_REPORT_COMPOSED = "check_hardware_topology"
_REPORT_COMPOSER = "cmd_resource_report"

#: The computed-value vocabulary and the one function that computes it, which `cmd_query` hands
#: to `query.answer_request` for `--request` (the `query` package imports no owner of a computed
#: value). Neither is a check, so naming them restates no part of the composer's list.
_COMPUTED_VALUE_NAMES = frozenset({"DERIVED", "derive_rows"})

#: The composer's result type, which the `cli` package's gate helpers take as their parameter.
#: A type, not a check, so admitting it restates no part of the composer's list.
_COMPOSER_RESULT_TYPE = "ConsistencyResult"

#: The manifest's one finding: `cli/build_commands.py` builds the `manifest.unknown-key` records
#: as the validator's record type and puts them ahead of the composer's result with
#: `with_records`. A record type and a result rebuilder, not checks.
_MANIFEST_FINDING_NAMES = frozenset({"ConsistencyIssue", "with_records"})

#: The restated list as the CLI once carried it, the positive control for the scan below.
_THE_DEFECT = (
    "check_cross_layer_consistency already calls check_connector_consistency, "
    "check_data_type_references, check_data_type_cycles, and check_task_consistency internally."
)


def _composed_checks() -> tuple[str, ...]:
    """The checks the composer runs, read from the composer's own body and the phases it calls.

    Derived, never restated (:func:`validator_package.composed_checks`), so this test is no
    second copy of the list.
    """
    return validator_package.composed_checks()


def _cli_modules() -> list[ast.Module]:
    """Every module of the CLI package, parsed, in POSIX path order."""
    paths = sorted((REPO_ROOT / _CLI_SOURCE).rglob("*.py"), key=lambda path: path.as_posix())
    assert len(paths) > 1, f"{_CLI_SOURCE}: the scan reads every module of the package"
    return [ast.parse(path.read_text(encoding="utf-8")) for path in paths]


def test_the_composed_checks_are_read_from_the_composer_itself() -> None:
    """The derivation, before anything is asserted with it."""
    checks = _composed_checks()
    assert len(checks) >= 4, "the composer composes fewer checks than the recorded defect miscounted"
    assert len(set(checks)) == len(checks), f"the composer names a check twice: {checks}"
    assert all(name.startswith("check_") for name in checks), checks
    assert _COMPOSER not in checks, "the composer would be composing itself"


def test_the_one_composed_check_the_cli_names_is_named_only_where_it_composes() -> None:
    """The allowance is spent exactly once, by the function it is pinned to.

    What it does **not** reach: prose. It reads the AST, because the question is which
    *function* names the check and text has no scopes.
    """
    modules = _cli_modules()
    composed = set(_composed_checks())

    assert _REPORT_COMPOSED in composed, (
        f"`{_REPORT_COMPOSED}` is no longer one of the checks `{_COMPOSER}` composes, "
        "so the allowance above guards nothing"
    )

    naming: list[tuple[str, set[str]]] = []
    # Every outermost function at any depth, each once; a nested function is walked as part of the one holding it.
    pending: list[ast.AST] = list(modules)
    while pending:
        node = pending.pop(0)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            pending[:0] = ast.iter_child_nodes(node)
            continue
        named = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name) and inner.id in composed}
        if named:
            naming.append((node.name, named))

    assert naming == [(_REPORT_COMPOSER, {_REPORT_COMPOSED})], (
        f"{_CLI_SOURCE}: only `{_REPORT_COMPOSER}` may name a composed check, and only "
        f"`{_REPORT_COMPOSED}`; found {naming}"
    )

    imported = {
        alias.name
        for module in modules
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.validator"
        for alias in node.names
    }

    allowance = {_COMPOSER, _REPORT_COMPOSED, _COMPOSER_RESULT_TYPE} | _COMPUTED_VALUE_NAMES
    assert imported == allowance | _MANIFEST_FINDING_NAMES, (
        f"{_CLI_SOURCE} imports {sorted(imported)} from the validator; the composer, the one "
        "composed check the report renders, the composer's result type the gate helpers are typed "
        "with, the computed-value vocabulary `--request` hands on and the manifest finding's record "
        "type and rebuilder are the whole allowance"
    )
    assert not _COMPUTED_VALUE_NAMES & composed, "a computed-value name became a composed check"
    assert not _MANIFEST_FINDING_NAMES & composed, "a manifest finding name became a composed check"


def test_the_cli_still_calls_the_composer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check-model` and `validate` still run the composer: each refuses its finding, exit 1.

    The model breaks a rule the ARXML writer does not refuse, so only the composer can fail it.
    What this does not reach: that the composer is handed the manifest's AUTOSAR version, and any
    rule other than the one this model breaks.
    """
    project = tmp_path / "project"
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
    cli_main()
    model_root = project / "model"
    component = element_path(model_root, "swc", "LinkedSWC", group="linked")
    component.parent.mkdir(parents=True)
    component.write_text(
        "name: LinkedSWC\ntype: application\nhandle_termination_and_restart: no_support\n"
        "satisfies:\n- REQ_NOBODY_DECLARES@1\n",
        encoding="utf-8",
        newline="\n",
    )
    expected = check_cross_layer_consistency(str(model_root)).records
    assert {record.rule for record in expected} == {"trace.link-unknown-requirement"}
    capsys.readouterr()

    for command in ("check-model", "validate"):
        monkeypatch.setattr("sys.argv", ["swcraft", command, "--project", str(project / "swcraft.yaml")])
        with pytest.raises(SystemExit) as exit_:
            cli_main()

        assert exit_.value.code == 1, command
        printed = capsys.readouterr()
        for record in expected:
            assert record.message in printed.out + printed.err, (command, record.rule)
    # `check-model` writes nothing in any case, so only `validate`'s refusal is observable here.
    assert not (project / "dist").exists(), "validate wrote an artifact over a model the composer refuses"


def test_the_scan_catches_the_comment_it_was_written_for() -> None:
    """The positive control: the restated list trips the scan."""
    caught = sorted(name for name in _composed_checks() if name in _THE_DEFECT)
    assert caught == [
        "check_connector_consistency",
        "check_data_type_cycles",
        "check_data_type_references",
        "check_task_consistency",
    ]
    assert len(caught) < len(_composed_checks()), "the recorded defect was that the list was short; it must still be"
