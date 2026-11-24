"""The MCP tools over a component stub's ports, asked over the wire on the stub-ports compositions fixture.

* ``read`` publishes a stub's port as the file holds it -- ``interface: null`` and the hint in
  ``observed_interface`` -- in the ``full`` form; ``where`` selects on the hint, never on ``interface``;
* a port collection's default summary carries a set ``interface`` (summaries keep scalar references) and omits an
  unset one, never ``null``; ``observed_interface`` is ``full``-only;
* ``impact_of`` draws no edge from the hint, and ``change_set`` stages a connector at a stub's port -- its ends
  *not judged* -- without naming the hint as if it were compared.

No reply here names where the project is stored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
import swcraft_mcp.server as _server
import yaml
from protocol_client import call


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at the wired compositions model; return the project root."""
    root = tmp_path / "stub-ports-project"
    root.mkdir()
    document = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    stubs.component_stub_ports_model_root(root / "model")
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(root / "swcraft.yaml"))
    _server._PROJECT = None
    return root


def _read(**arguments: Any) -> dict[str, Any]:
    structured, _texts = call("read", **arguments)
    assert "error" not in structured, structured
    return structured


def _ports(component: str) -> dict[str, Any]:
    return {"layer": "swc", "element": component, "path": [{"field": "ports"}]}


class TestReadPublishesTheStubsPort:
    def test_the_element_read_carries_no_interface_and_the_hint(self, project: Path) -> None:
        answer = _read(address={"layer": "swc", "element": "ExternalStub"})

        assert answer["form"] == "full"
        ports = {port["name"]: port for port in answer["value"]["ports"]}
        assert ports["pExt"]["interface"] is None
        assert ports["pExt"]["observed_interface"] == "ITrigger"
        assert ports["rExt"]["interface"] is None
        assert ports["rExt"]["observed_interface"] is None

    def test_where_selects_on_the_hint_and_interface_never_matches_it(self, project: Path) -> None:
        by_hint = _read(
            address={"layer": "swc"}, members="ports", where=[{"field": "observed_interface", "equals": "ITrigger"}]
        )
        by_interface = _read(
            address={"layer": "swc"}, members="ports", where=[{"field": "interface", "equals": "ITrigger"}]
        )

        assert [(row["element"], row["member"]["name"]) for row in by_hint["value"]] == [("ExternalStub", "pExt")]
        owners = {row["element"] for row in by_interface["value"]}
        assert "Beta" in owners
        assert not owners & {"ExternalStub", "OtherStub"}


class TestThePortSummaryCarriesTheInterface:
    """The reference clause over the wire: set means published, unset means absent."""

    def test_a_port_collection_summary_carries_every_interface_on_a_declared_component(self, project: Path) -> None:
        answer = _read(address=_ports("Beta"))

        assert answer["form"] == "summary"
        assert answer["value"], answer
        for port in answer["value"]:
            # Every collection field is published as its count.
            assert set(port) == {"name", "direction", "interface", "measures", "com_specs", "satisfies"}, port
            assert port["interface"], port
        assert {port["name"]: port["interface"] for port in answer["value"]}["rFromExt"] == "ITrigger"

    def test_a_stub_s_port_summary_omits_the_unset_interface_and_never_carries_the_hint(self, project: Path) -> None:
        answer = _read(address=_ports("ExternalStub"))

        assert answer["form"] == "summary"
        assert answer["value"], answer
        for port in answer["value"]:
            assert set(port) == {"name", "direction", "measures", "com_specs", "satisfies"}, port

    def test_a_ports_slice_summary_tells_declared_from_stub_by_the_key_alone(self, project: Path) -> None:
        answer = _read(address={"layer": "swc"}, members="ports")

        assert answer["form"] == "summary"
        rows = {(row["element"], row["member"]["name"]): row["member"] for row in answer["value"]}
        assert rows[("Beta", "rFromExt")]["interface"] == "ITrigger"
        assert "interface" not in rows[("ExternalStub", "pExt")]
        assert all("observed_interface" not in member for member in rows.values())

    def test_the_full_form_of_the_same_collection_carries_it(self, project: Path) -> None:
        answer = _read(address=_ports("Beta"), form="full")

        interfaces = {port["name"]: port["interface"] for port in answer["value"]}
        assert interfaces["rFromExt"] == "ITrigger"
        assert all(interfaces.values()), interfaces

    def test_the_element_address_still_answers_full_and_carries_every_interface(self, project: Path) -> None:
        answer = _read(address={"layer": "swc", "element": "Beta"})

        assert answer["form"] == "full"
        assert all(port["interface"] for port in answer["value"]["ports"]), answer["value"]["ports"]

    def test_where_filters_on_interface_in_the_summary(self, project: Path) -> None:
        answer = _read(address=_ports("Beta"), where=[{"field": "interface", "equals": "ITrigger"}])

        assert answer["form"] == "summary"
        assert "rFromExt" in [port["name"] for port in answer["value"]]


class TestImpactOf:
    def test_an_interface_s_dependents_never_include_the_stub_its_hint_names(self, project: Path) -> None:
        _structured, texts = call("impact_of", name="ITrigger", form="full")
        text = "\n".join(texts)

        assert "Beta" in text  # the control: a declared port's interface is an edge
        assert "ExternalStub" not in text, text

    def test_a_stub_depends_on_no_interface(self, project: Path) -> None:
        _structured, texts = call("impact_of", name="ExternalStub", form="full", direction="dependencies")
        text = "\n".join(texts)

        assert "ExternalStub" in text  # the walk was answered, not refused
        assert not text.startswith("ERROR"), text
        assert "ITrigger" not in text, text
        assert "interface" not in text, text


class TestAConnectorAtAStubPortIsStaged:
    """The connector's two ends are judged when the change set is staged (pass D).

    Each case re-draws one of the fixture's connectors under a new name -- the remove keeps the link from being a
    duplicate -- so pass D judges exactly that pair, in ``stage-only`` mode, which commits nothing.
    """

    def _redraw(self, project: Path, old: str, requirer: tuple[str, str]) -> dict[str, Any]:
        structured, _texts = call(
            "change_set",
            baseline=fixtures.current_baseline_token(project),
            intent=f"Re-draw {old} from ExternalStub.pExt to {requirer[0]}.{requirer[1]}",
            operations=[
                {"operation": "remove", "address": {"layer": "connectors", "element": old}},
                {
                    "operation": "create",
                    "address": {"layer": "connectors", "element": "redrawn_link"},
                    "value": {
                        "name": "redrawn_link",
                        "composition": "Sub",
                        "from": {"swc": "ExternalStub", "port": "pExt"},
                        "to": {"swc": requirer[0], "port": requirer[1]},
                    },
                },
            ],
        )
        assert isinstance(structured, dict), structured
        return structured

    @pytest.mark.parametrize(
        ("old", "requirer"),
        [("ext_link", ("Beta", "rFromExt")), ("stub_link", ("OtherStub", "rOther"))],
        ids=["stub-to-declared", "stub-to-stub"],
    )
    def test_a_stubs_port_is_not_judged_so_the_connector_is_staged(self, project: Path, old, requirer) -> None:
        answer = self._redraw(project, old, requirer)

        assert answer["response"] == "change_set", answer
        assert answer["status"] == "staged", answer
        assert "ITrigger" not in repr(answer)  # the hint is never what was compared

    def test_no_answer_names_where_the_project_is_stored(self, project: Path) -> None:
        text = repr(self._redraw(project, "ext_link", ("Beta", "rFromExt")))

        assert str(project) not in text
        assert project.as_posix() not in text
        assert ".yaml" not in text
