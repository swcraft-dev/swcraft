"""The read-only commands over the external stubs.

``list``, ``query`` and ``impact`` are total consumers: they read each layer's declared
identity, fields and references, never a fixed set of kinds. This module is the evidence that the claim holds for them:

* ``list`` names every stub in its layer, by identity;
* ``query LAYER --where kind=external_interface`` (and ``kind=external_type``) selects
  exactly the stubs, and ``--where external_ref=...`` filters on the path as a literal;
* ``impact`` of a stub lists every element whose port names it and the connectors drawn
  across those ports;
* ``external_ref`` is a literal, never a reference edge: a stub whose path ends in a
  declared element's name is not its dependent.

Everything runs through ``swcraft``'s ``main()`` on ``external_stubs_support``'s stub model,
so the exit code is the command's own.

What this module does not reach: the MCP ``read`` and ``impact_of`` tools (the same
``query`` engine, pinned by their own suites); and whether the path in ``external_ref``
exists anywhere, which nothing in SWCraft can know.
"""

from __future__ import annotations

import json
from pathlib import Path

import external_stubs_support as stubs
import pytest
from swcraft_tools.cli import main

_INTERFACE_STUB_NAMES = sorted(name for name, _, _ in stubs.INTERFACE_STUBS)
_TYPE_STUB_NAME = stubs.TYPE_STUB[0]


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    """The stub model's manifest."""
    model_root = stubs.external_stubs_model_root(tmp_path / "project")
    return model_root.parent / "swcraft.yaml"


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


def _lines(out: str, layer: str) -> list[str]:
    prefix = f"{layer}: "
    return [line.removeprefix(prefix) for line in out.splitlines() if line.startswith(prefix)]


class TestList:
    def test_every_interface_stub_is_listed_in_its_layer(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["list", "interfaces", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        listed = _lines(out, "interfaces")
        assert set(_INTERFACE_STUB_NAMES) <= set(listed)
        assert {"IExample", "IDiagnosticService"} <= set(listed)

    def test_the_type_stub_is_listed_in_its_layer(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["list", "implementation_types", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        assert _TYPE_STUB_NAME in _lines(out, "implementation_types")

    def test_the_json_listing_names_the_stubs(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["list", "--project", str(manifest), "--format", "json"], monkeypatch, capsys)

        assert code == 0
        document = json.loads(out)
        text = json.dumps(document)
        for name in (*_INTERFACE_STUB_NAMES, _TYPE_STUB_NAME):
            assert f'"{name}"' in text, name


class TestQuery:
    def test_kind_external_interface_selects_exactly_the_interface_stubs(
        self, manifest: Path, monkeypatch, capsys
    ) -> None:
        code, out = _run(
            ["query", "interfaces", "--where", "kind=external_interface", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert sorted(_lines(out, "interfaces")) == _INTERFACE_STUB_NAMES

    def test_kind_external_type_selects_exactly_the_type_stub(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(
            ["query", "implementation_types", "--where", "kind=external_type", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert _lines(out, "implementation_types") == [_TYPE_STUB_NAME]

    def test_the_json_slice_carries_the_kind_filter_and_the_stubs(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(
            [
                "query",
                "interfaces",
                "--where",
                "kind=external_interface",
                "--project",
                str(manifest),
                "--format",
                "json",
            ],
            monkeypatch,
            capsys,
        )

        assert code == 0
        document = json.loads(out)
        assert document["where"] == [{"field": "kind", "equals": "external_interface"}]
        assert sorted(document["elements"]) == _INTERFACE_STUB_NAMES

    def test_the_stub_kind_is_not_a_kind_of_the_other_layer(self, manifest: Path, monkeypatch, capsys) -> None:
        """``external_type`` is a data type kind: no interface has it, and the answer is empty, not a refusal."""
        code, out = _run(
            ["query", "interfaces", "--where", "kind=external_type", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert _lines(out, "interfaces") == []

    def test_external_ref_filters_as_a_literal(self, manifest: Path, monkeypatch, capsys) -> None:
        name, ref, _dest = stubs.INTERFACE_STUBS[0]

        code, out = _run(
            ["query", "interfaces", "--where", f"external_ref={ref}", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert _lines(out, "interfaces") == [name]

    def test_external_dest_filters_the_stubs_by_the_source_s_dest(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(
            ["query", "interfaces", "--where", "external_dest=PORT-INTERFACE", "--project", str(manifest)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert _lines(out, "interfaces") == ["IForeignAny"]


class TestImpact:
    @pytest.mark.parametrize(("name", "ref", "dest"), stubs.INTERFACE_STUBS)
    def test_impact_of_an_interface_stub_lists_every_port_naming_it(
        self, manifest: Path, monkeypatch, capsys, name: str, ref: str, dest: str
    ) -> None:
        code, out = _run(["impact", name, "--project", str(manifest), "--format", "json"], monkeypatch, capsys)

        assert code == 0
        rows = json.loads(out)["matched"]["interface"]
        ports = {
            (row["layer"], row["name"], detail["value"])
            for row in rows
            for detail in row["details"]
            if detail["kind"] == "port"
        }
        expected = {
            ("swc", owner, port) for owner, port, _direction, interface in stubs.STUB_PORTS if interface == name
        }
        expected |= {
            ("composition", "Root", port) for port, _direction, interface in stubs.BOUNDARY_PORTS if interface == name
        }
        assert expected, name
        assert ports == expected

    def test_impact_names_the_connector_drawn_across_a_stub_port(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["impact", "IForeignCS", "--project", str(manifest)], monkeypatch, capsys)

        assert code == 0
        assert "swc ExampleSWC (via port pCs)" in out
        assert "swc ExampleConsumerSWC (via port rCs)" in out
        assert "composition Root (via port rExtCs" in out
        assert "connector Cs_across_stub (via ports ExampleSWC.pCs, ExampleConsumerSWC.rCs)" in out

    def test_impact_of_the_type_stub_lists_what_is_typed_by_it(self, manifest: Path, monkeypatch, capsys) -> None:
        code, out = _run(["impact", _TYPE_STUB_NAME, "--project", str(manifest), "--depth", "1"], monkeypatch, capsys)

        assert code == 0
        assert "interface IExample" in out
        assert "interface IDiagnosticService" in out
        assert "implementation_type RefForeign_T" in out

    def test_query_dependents_is_the_same_walk(self, manifest: Path, monkeypatch, capsys) -> None:
        _code, impact_json = _run(
            ["impact", "IForeignSR", "--project", str(manifest), "--format", "json"], monkeypatch, capsys
        )
        code, query_json = _run(
            ["query", "--dependents", "IForeignSR", "--project", str(manifest), "--format", "json"],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert query_json == impact_json


class TestExternalRefIsALiteral:
    """The path a stub carries names nothing in the model, even when its last segment does."""

    @pytest.fixture()
    def aliased(self, manifest: Path) -> Path:
        model_root = manifest.parent / "model"
        stubs.interface_stub(model_root, "IAlias", "/Foreign/PortInterfaces/IExample", "SENDER-RECEIVER-INTERFACE")
        stubs.type_stub(model_root, "TAlias", "/Foreign/ImplementationDataTypes/float32", "IMPLEMENTATION-DATA-TYPE")
        return manifest

    @pytest.mark.parametrize(("stub", "leaf"), [("IAlias", "IExample"), ("TAlias", "float32")])
    def test_a_stub_is_not_a_dependent_of_the_element_its_path_ends_in(
        self, aliased: Path, monkeypatch, capsys, stub: str, leaf: str
    ) -> None:
        code, out = _run(["impact", leaf, "--project", str(aliased), "--format", "json"], monkeypatch, capsys)

        assert code == 0
        names = {row["name"] for rows in json.loads(out)["matched"].values() for row in rows}
        assert names, leaf
        assert stub not in names

    @pytest.mark.parametrize("stub", ["IAlias", "TAlias", *_INTERFACE_STUB_NAMES, _TYPE_STUB_NAME])
    def test_a_stub_depends_on_nothing(self, aliased: Path, monkeypatch, capsys, stub: str) -> None:
        code, out = _run(["query", "--deps", stub, "--project", str(aliased), "--format", "json"], monkeypatch, capsys)

        assert code == 0
        matched = json.loads(out)["matched"]
        assert matched, stub
        assert all(rows == [] for rows in matched.values()), matched

    def test_a_stub_s_path_is_not_a_name_impact_resolves(self, aliased: Path, monkeypatch, capsys) -> None:
        code, out = _run(
            ["impact", "/Foreign/PortInterfaces/IExample", "--project", str(aliased)],
            monkeypatch,
            capsys,
        )

        assert code == 0
        assert "'/Foreign/PortInterfaces/IExample' not found in any layer" in out
