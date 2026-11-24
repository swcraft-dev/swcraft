"""The Impact view's recorded documents: what the CLI writes, the TypeScript side reads.

Each file under ``vscode_extension/src/fixtures/impact/`` is, or is shaped from, the stdout of
``swcraft impact --format json -- <name>`` on ``examples/basic`` with no ``--depth`` (the run the
Impact view makes); ``navigationCore.test.ts`` and the view's tests parse it. Every test here runs
the CLI afresh and holds the committed file equal to its output.

Recorded: ``IExample.json`` (six dependents in five layers), ``REQ_EXAMPLE_001.json`` (two direct
dependents, four transitive), ``NoSuchElement.json`` (an empty ``matched``), ``Root.json`` (no
dependents). Shaped from a fresh run: ``two-roots.json`` (a second matched layer, a name two layers
declare) and ``unknown-token.json`` (a row of a layer token no ``references.json`` layer carries,
and a matched token no layer carries: a toolchain newer than the extension).

They are **change detectors, never correctness proofs**: regenerate them deliberately, after
reading the difference, with
``.venv/bin/python packages/tools/tests/test_impact_panel_fixtures.py``, never to make a red run green.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
from collections.abc import Callable
from typing import Any

import pytest
from conftest import REPO_ROOT
from swcraft_tools.cli import main

FIXTURES = REPO_ROOT / "vscode_extension" / "src" / "fixtures" / "impact"
BASIC_MANIFEST = REPO_ROOT / "examples" / "basic" / "swcraft.yaml"

#: The layer token the shaped ``unknown-token.json`` uses: no layer carries it.
UNKNOWN_TOKEN = "future_layer"


def _cli_json(name: str) -> str:
    """Return ``swcraft impact --format json -- <name>``'s stdout on ``examples/basic``, run in this process."""
    argv = ["swcraft", "impact", "--project", str(BASIC_MANIFEST), "--format", "json", "--", name]
    out = io.StringIO()
    saved = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main()
    finally:
        sys.argv = saved
    return out.getvalue()


def _dump(document: dict[str, Any]) -> str:
    """Render a shaped document in the CLI's own layout (two-space indent, one closing newline)."""
    return json.dumps(document, indent=2) + "\n"


def _two_roots() -> str:
    document = json.loads(_cli_json("IExample"))
    rows = document["matched"]["interface"]
    composition = next(row for row in rows if row["layer"] == "composition")
    document["matched"]["swc"] = [copy.deepcopy(composition)]
    return _dump(document)


def _unknown_token() -> str:
    document = json.loads(_cli_json("IExample"))
    rows = document["matched"]["interface"]
    component = next(row for row in rows if row["layer"] == "swc")
    rows.append(
        {
            "layer": UNKNOWN_TOKEN,
            "name": "FutureElement",
            "detail": "via port pOut",
            "details": [{"kind": "port", "value": "pOut"}],
        }
    )
    document["matched"][UNKNOWN_TOKEN] = [copy.deepcopy(component)]
    return _dump(document)


#: Each fixture and the function that writes it today.
CASES: dict[str, Callable[[], str]] = {
    "IExample.json": lambda: _cli_json("IExample"),
    "REQ_EXAMPLE_001.json": lambda: _cli_json("REQ_EXAMPLE_001"),
    "NoSuchElement.json": lambda: _cli_json("NoSuchElement"),
    "Root.json": lambda: _cli_json("Root"),
    "two-roots.json": _two_roots,
    "unknown-token.json": _unknown_token,
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_committed_document_is_the_cli_s(name: str) -> None:
    assert (FIXTURES / name).read_text(encoding="utf-8") == CASES[name]()


def test_the_documents_hold_what_the_panel_tests_need() -> None:
    """Each case shows what its name says, so a model edit cannot hollow one out."""
    read = {name: json.loads((FIXTURES / name).read_text(encoding="utf-8")) for name in CASES}
    for document in read.values():
        assert document["depth"] is None, "the view's run is the whole walk"
    interface = read["IExample.json"]["matched"]["interface"]
    assert [row["layer"] for row in interface] == ["swc", "swc", "connector", "composition", "task", "sequence"]
    assert all(row["detail"] for row in interface)
    requirement = read["REQ_EXAMPLE_001.json"]["matched"]["requirement"]
    # The two components link it directly (no detail); the rest are reached through them.
    assert [row["detail"] for row in requirement[:2]] == ["", ""]
    assert {row["layer"] for row in requirement[2:]} == {"connector", "composition", "task", "sequence"}
    assert read["NoSuchElement.json"]["matched"] == {}
    assert read["Root.json"]["matched"] == {"composition": []}
    assert list(read["two-roots.json"]["matched"]) == ["interface", "swc"]
    unknown = read["unknown-token.json"]["matched"]
    assert list(unknown) == ["interface", UNKNOWN_TOKEN]
    assert unknown["interface"][-1]["layer"] == UNKNOWN_TOKEN


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for fixture in sorted(CASES):
        (FIXTURES / fixture).write_text(CASES[fixture](), encoding="utf-8", newline="\n")
