"""Contract `7.6.0`: a port on an external component stub, at the declaration, the loader and the serializer.

A component stub (``SWComponent.external``) may declare ports: a stub's port carries a
**name** and a **direction**, the two facts a connector end states, and **no**
``interface`` -- the export declares none, and ``interface`` means the *declared*
interface. So ``field:swc.port.interface`` is optional (default ``null``), and a separate
``field:swc.port.observed_interface`` -- a plain string, never a reference --
carries the interface of the port's one known neighbour as a hint no rule reads.

This module asks three questions, and only three:

* **what the contract declares** -- the two port fields' shape and order, the
  restated ``rule:swc.external-with-ports`` (``appliesTo`` keeps ``ports`` and
  gains ``port.interface`` and ``port.measures``, an addition and not a removal),
  and the two rule summaries the validator repeats;
* **what loads** -- a port without an ``interface`` loads with ``None``, through
  the class and through the reader, and a port carrying ``observed_interface``
  loads with it;
* **what is written** -- ``yaml_documents.swc_to_dict`` omits a ``None`` interface
  and writes the hint, in the same key order the derived serializer
  (``elements.element_to_dict``) produces, so the importer's write path and
  every mutation keep the hint.

What this module does **not** reach: no rule is exercised here. Whether ``check-model``
accepts a stub's port without an interface, refuses one with an interface or ``measures``, and
reports a declared component's port without an interface
(``port.undeclared-interface``) are the validator's questions (the rule tests);
nothing here emits ARXML, mints a port from an import, or draws a diagram.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from model_contract_fixtures import load_contract
from swcraft_tools import reader
from swcraft_tools.schema import Port, SWComponent
from swcraft_tools.validator import CONSISTENCY_RULES
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import swc_to_dict


def _port_fields() -> dict[str, dict[str, Any]]:
    kinds = {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}
    return {field["name"]: field for field in kinds["kind:swc.port"]["fields"]}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


def _stub(**port: Any) -> SWComponent:
    """A component stub carrying one port built from *port*."""
    return SWComponent.model_validate(
        {
            "name": "Dcm",
            "type": "application",
            "external": True,
            "external_ref": "/Foreign/Dcm",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "ports": [port],
            "runnables": [],
        }
    )


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


class TestTheContractDeclaresTheStubPortFields:
    def test_the_contract_version_is_the_minor_that_admits_them(self) -> None:
        """``7.6.0`` admitted the fields as a minor: one added, one optional, one ``appliesTo`` widened."""
        # The shipped contract version; every later contract bump moves it.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_port_declares_its_fields_in_order(self) -> None:
        assert list(_port_fields()) == [
            "name",
            "direction",
            "interface",
            "observed_interface",
            "description",
            "measures",
            # Contract 7.11.0: the com-spec entries, last.
            "com_specs",
            # Contract 11.0.0: the trace link, appended after them.
            "satisfies",
        ]

    def test_interface_is_optional_with_a_null_default_and_still_a_reference(self) -> None:
        interface = _port_fields()["interface"]

        assert (interface["required"], interface["default"]) == (False, None)
        assert interface["reference"] == {"layer": "interfaces", "identityField": "name"}

    def test_observed_interface_is_an_optional_string_and_never_a_reference(self) -> None:
        """A hint and not a declaration: no ``reference`` and no ``relation``.

        So the traversal draws no impact edge from it and no pass holds it."""
        hint = _port_fields()["observed_interface"]

        assert hint["id"] == "field:swc.port.observed_interface"
        assert (hint["type"], hint["required"], hint["default"], hint["immutable"]) == ("string", False, None, False)
        assert "reference" not in hint
        assert "relation" not in hint

    def test_external_with_ports_keeps_ports_and_gains_the_two_port_fields(self) -> None:
        rule = _rules()["rule:swc.external-with-ports"]

        assert rule["appliesTo"] == [
            "field:swc.swc.external",
            "field:swc.swc.ports",
            "field:swc.port.interface",
            "field:swc.port.measures",
            # Contract 7.11.0: a stub's port states no com-spec either.
            "field:swc.port.com_specs",
            "field:swc.swc.runnables",
            "field:swc.swc.memory",
            "field:swc.swc.data_type_mappings",
            "field:swc.swc.handle_termination_and_restart",
            "field:swc.swc.package",
            # Contract 8.0.0: a stub emits no behaviour to carry a stated name.
            "field:swc.swc.behavior_name",
            # Contract 12.1.0: a stub emits no behaviour to carry variables either.
            "field:swc.swc.variables",
            # Contract 12.2.0: nor exclusive areas.
            "field:swc.swc.exclusive_areas",
        ]

    def test_the_restated_rule_summaries_are_the_validators(self) -> None:
        for rule_id in ("rule:swc.external-with-ports", "rule:port.undeclared-interface"):
            assert _rules()[rule_id]["summary"] == CONSISTENCY_RULES[rule_id.removeprefix("rule:")], rule_id

    def test_the_external_with_ports_summary_allows_a_bare_port(self) -> None:
        summary = CONSISTENCY_RULES["swc.external-with-ports"]

        # Contract 7.11.0 added com-specs to what a stub's port may not carry.
        assert "a port with an interface, measures or com-specs" in summary


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


class TestAStubPortLoads:
    def test_a_port_without_an_interface_loads_with_none(self) -> None:
        port = Port.model_validate({"name": "GetCurrentTime", "direction": "provide"})

        assert port.interface is None
        assert port.observed_interface is None

    def test_a_port_with_an_observed_interface_loads_with_it(self) -> None:
        port = Port.model_validate({"name": "Did", "direction": "require", "observed_interface": "IDid"})

        assert (port.interface, port.observed_interface) == (None, "IDid")

    def test_a_declared_port_still_loads_as_before(self) -> None:
        port = Port.model_validate({"name": "pOut", "direction": "provide", "interface": "IExample"})

        assert (port.interface, port.observed_interface) == ("IExample", None)

    def test_a_stub_with_bare_ports_loads_through_the_reader(self, tmp_path: Path) -> None:
        document = {
            "name": "Dcm",
            "type": "application",
            "external": True,
            "external_ref": "/Foreign/Dcm",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "ports": [
                {"name": "Did", "direction": "require", "observed_interface": "IDid"},
                {"name": "GetCurrentTime", "direction": "provide"},
            ],
            "runnables": [],
        }
        path = tmp_path / "swc" / "external" / "Dcm.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        (loaded,) = reader.read_swc_model(str(tmp_path))

        assert [(p.name, p.direction, p.interface, p.observed_interface) for p in loaded.ports] == [
            ("Did", "require", None, "IDid"),
            ("GetCurrentTime", "provide", None, None),
        ]


# ---------------------------------------------------------------------------
# The serializer
# ---------------------------------------------------------------------------


class TestTheSerializerKeepsTheHint:
    def test_a_none_interface_is_omitted_and_the_hint_written(self) -> None:
        swc = _stub(name="Did", direction="require", observed_interface="IDid")

        assert swc_to_dict(swc)["ports"] == [{"name": "Did", "direction": "require", "observed_interface": "IDid"}]

    def test_a_bare_port_writes_name_and_direction_only(self) -> None:
        swc = _stub(name="GetCurrentTime", direction="provide")

        assert swc_to_dict(swc)["ports"] == [{"name": "GetCurrentTime", "direction": "provide"}]

    def test_a_declared_port_is_written_as_before(self) -> None:
        swc = _stub(name="pOut", direction="provide", interface="IExample", description="d")

        assert swc_to_dict(swc)["ports"] == [
            {"name": "pOut", "direction": "provide", "interface": "IExample", "description": "d"}
        ]

    def test_the_hand_written_and_the_derived_serializers_agree(self) -> None:
        """Key order included, as ``schema.Port`` declares it: a mutation and ``write_element`` write one file."""
        swc = _stub(name="Did", direction="require", interface="IDid", observed_interface="IDid")

        assert yaml.safe_dump(swc_to_dict(swc), sort_keys=False) == yaml.safe_dump(
            element_to_dict(swc), sort_keys=False
        )

    def test_the_written_port_round_trips(self) -> None:
        swc = _stub(name="Did", direction="require", observed_interface="IDid")

        assert SWComponent.model_validate(swc_to_dict(swc)) == swc
