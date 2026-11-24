"""The read-only commands over a component stub's ports (contract ``7.6.0``).

``list``, ``query`` and ``impact`` are total consumers: they read each layer's declared
identity, fields and references, never a fixed set of kinds, so a stub's ports needed no code in
any of the three. This module holds the evidence for what two of them do with the hint:

* **``query`` reads ``observed_interface`` as a field.** ``--where`` and ``--exists`` select
  on it like on any declared scalar, and a filter on ``interface`` never selects a stub's port
  by its hint — the hint is not the declared interface;
* **``impact`` draws no dependency edge from it**, and none from a stub port's ``None``
  interface: a stub depends on no interface, an interface's dependents never include a stub
  whose hint names it, and the walk names no interface called ``None`` or ``""``. The control
  is a declared component, whose ``interface`` IS an edge.

``list`` names the two stubs by identity, like any other component, whatever ports they carry.

Everything runs through ``swcraft``'s ``main()`` on the compositions fixture wired by
``external_stubs_support.add_component_stub_ports`` (``ExternalStub.pExt`` provides with the hint
``ITrigger``, ``ExternalStub.rExt`` and ``OtherStub.rOther`` require with none), so the exit code
is the command's own. The MCP ``read``/``impact_of`` answers and the pre-flight are
``packages/mcp/tests/test_stub_ports_mcp.py``'s.

What this module does not reach: a model whose stub ports carry hints that disagree with every
neighbour (the hint is a literal to both commands, so nothing here could tell), and the diagram,
which ignores the hint (``test_stub_ports_diagrams.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
import yaml
from swcraft_tools.cli import main


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    """The wired compositions model as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "stub-ports-project"
    root.mkdir()
    document = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    path = root / "swcraft.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    stubs.component_stub_ports_model_root(root / "model")
    return path


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run ``swcraft`` with *argv*; return ``(exit code, stdout)``."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    else:
        code = 0
    return code, capsys.readouterr().out


def _json(argv: list[str], manifest: Path, monkeypatch, capsys) -> dict[str, Any]:
    code, out = _run([*argv, "--project", str(manifest), "--format", "json"], monkeypatch, capsys)
    assert code == 0, out
    return json.loads(out)


def _port_rows(document: dict[str, Any]) -> list[tuple[str, str]]:
    return [(row["element"], row["member"]) for row in document["elements"]]


def _names_in(document: Any) -> set[str]:
    """Every string anywhere in a JSON document."""
    if isinstance(document, str):
        return {document}
    if isinstance(document, dict):
        return set().union(*(_names_in(key) | _names_in(value) for key, value in document.items()))
    if isinstance(document, list):
        return set().union(*(_names_in(item) for item in document))
    return set()


class TestList:
    def test_both_stubs_are_listed_whatever_ports_they_carry(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["list", "swc", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        listed = {line.removeprefix("swc: ") for line in out.splitlines() if line.startswith("swc: ")}
        assert {"ExternalStub", "OtherStub", "Beta"} <= listed


class TestQueryReadsTheHintAsAField:
    def test_where_selects_the_port_whose_hint_names_the_interface(self, manifest: Path, monkeypatch, capsys) -> None:
        document = _json(
            ["query", "swc", "--members", "ports", "--where", "observed_interface=ITrigger"],
            manifest,
            monkeypatch,
            capsys,
        )

        assert _port_rows(document) == [("ExternalStub", "pExt")]
        assert document["where"] == [{"field": "observed_interface", "equals": "ITrigger"}]

    def test_exists_selects_the_ports_that_carry_a_hint(self, manifest: Path, monkeypatch, capsys) -> None:
        """``rExt`` and ``rOther`` carry none, and no declared component's port carries one."""
        document = _json(
            ["query", "swc", "--members", "ports", "--exists", "observed_interface"], manifest, monkeypatch, capsys
        )

        assert _port_rows(document) == [("ExternalStub", "pExt")]

    def test_the_text_form_names_the_port_beside_its_owner(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(
            [
                "query",
                "swc",
                "--members",
                "ports",
                "--where",
                "observed_interface=ITrigger",
                "--project",
                str(manifest),
            ],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert "swc: ExternalStub.pExt" in out.splitlines()

    def test_a_filter_on_interface_never_selects_a_stubs_port_by_its_hint(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        document = _json(
            ["query", "swc", "--members", "ports", "--where", "interface=ITrigger"], manifest, monkeypatch, capsys
        )

        rows = _port_rows(document)
        assert ("Beta", "rFromExt") in rows  # the control: a declared port on ITrigger is selected
        assert not [row for row in rows if row[0] in {"ExternalStub", "OtherStub"}], rows

    def test_no_stubs_port_carries_an_interface(self, manifest: Path, monkeypatch, capsys) -> None:
        document = _json(["query", "swc", "--members", "ports", "--exists", "interface"], manifest, monkeypatch, capsys)

        owners = {element for element, _port in _port_rows(document)}
        assert "Beta" in owners
        assert not owners & {"ExternalStub", "OtherStub"}


class TestImpactDrawsNoEdgeFromTheHint:
    def test_an_interface_s_dependents_never_include_the_stub_its_hint_names(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        code, out = _run(["impact", "ITrigger", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        assert "swc Beta (via ports" in out  # the control: a declared port's interface is an edge
        assert "ExternalStub" not in out, out

    def test_the_first_level_is_the_declared_ports_alone(self, manifest: Path, monkeypatch, capsys) -> None:
        document = _json(["query", "--dependents", "ITrigger", "--depth", "1"], manifest, monkeypatch, capsys)

        names = _names_in(document)
        assert "Beta" in names
        assert not names & {"ExternalStub", "OtherStub"}, document

    @pytest.mark.parametrize("stub", ["ExternalStub", "OtherStub"])
    def test_a_stub_depends_on_no_interface(self, manifest: Path, monkeypatch, capsys, stub: str) -> None:
        """Neither the hint (``ExternalStub.pExt``) nor a ``None`` interface is an edge."""
        document = _json(["query", "--deps", stub], manifest, monkeypatch, capsys)

        # Read off the matched rows, not every string of the document: a stub that satisfies
        # a requirement depends on it, and that component-level edge carries an empty `detail`.
        rows = [row for matched in document["matched"].values() for row in matched]
        assert not {row["name"] for row in rows} & {"ITrigger", "ISignal", "None", ""}, document
        assert not [row for row in rows if row["layer"] == "interface"], document
        code, out = _run(["query", "--deps", stub, "--project", str(manifest)], monkeypatch, capsys)
        assert code == 0
        assert "interface" not in out, out

    def test_the_control_a_declared_component_depends_on_its_interfaces(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        document = _json(["query", "--deps", "Beta"], manifest, monkeypatch, capsys)

        assert "ITrigger" in _names_in(document)

    def test_the_stub_s_dependents_are_what_is_wired_to_it(self, manifest: Path, monkeypatch, capsys) -> None:
        """The connectors and the composition naming the stub ARE its dependents: its ports resolve by name."""
        code, out = _run(["impact", "ExternalStub", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        assert "connector ext_link (via port ExternalStub.pExt)" in out
        assert "connector stub_link (via port ExternalStub.pExt)" in out
        assert "composition Sub (member Ext)" in out
