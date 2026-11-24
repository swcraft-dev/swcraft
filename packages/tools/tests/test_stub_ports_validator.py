"""Contract ``7.6.0``: ports on an external **component** stub, at ``check-model``.

A component stub (``external: true``) may declare ports by name and direction; they never carry an
``interface`` and may carry an ``observed_interface``, a hint no rule reads. The validator is asked
over the compositions fixture wired by ``external_stubs_support.add_component_stub_ports``, broken
one way per test:

* the restated ``swc.external-with-ports`` -- ports allowed, ``interface`` or ``measures`` on one
  refused under the subject address and message prefix it had before;
* the reworded ``port.undeclared-interface`` -- skips a stub's port, reports a declared port
  naming no interface;
* one finding per cause -- the safety rules leave a stub port's ``measures`` to
  ``swc.external-with-ports``, and every judging reader of ``Port.interface`` abstains on ``None``;
* the pre-flight answers *not judged* for a stub's port, after the direction;
* connections resolve a stub's port by name, direction still judges, the hint is never compared;
* the five identifier guards (``check_identifier_conformance``, ``_check_component_names``,
  ``identifier_violations``, ``emitted_identifier_violations``, ``_check_swc_names``) each hold a
  stub's port name, the last segment of every end on it, to the Identifier rule;
* pass D -- each restated finding reaches the protocol under its code.

What it does **not** reach: the writer's stub check and the ARXML emission
(``test_stub_ports_arxml.py``), the protocol's writes of a stub's port
(``test_stub_ports_editing.py``), the importer's minting and the combined foreign-name
test (``test_stub_ports_import.py``), and any model shape outside the ones written
below: this is example-based evidence, not a proof over every stub-bearing model.
"""

from __future__ import annotations

from pathlib import Path

import external_stubs_support as stubs
import pytest
import swcraft_tools.editing.registry as reg
from conftest import model_element_path
from swcraft_tools import validator
from swcraft_tools.arxml_import.reconcile import ReconciledModel, identifier_violations
from swcraft_tools.arxml_import.swc_mapping import ImportedSwc
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    diagnose,
    emitted_identifier_violations,
)
from swcraft_tools.reader import read_connectors
from swcraft_tools.schema import Port, SWComponent
from swcraft_tools.validator import (
    check_cross_layer_consistency,
    check_identifier_conformance,
    check_port_compatibility,
    model_view,
)
from swcraft_tools.writer.arxml_generation import _check_component_names
from swcraft_tools.writer.elements import write_element

STUB = "swc/external/ExternalStub.yaml"
BETA = "swc/core/Beta.yaml"

#: A port name one character over the 128 the AUTOSAR ``Identifier`` admits. A
#: stub's port is emitted as no ``SHORT-NAME``, but as the last segment of every
#: end on it, and a reference path segment is held to the same 128.
FOREIGN_PORT_NAME = "p" * 129

#: Names a stub's port may not carry: a slash would retarget the emitted end at
#: another element, a leading digit and a space make the reference schema-invalid,
#: and the fourth is over-long.
BAD_STUB_PORT_NAMES = ("x/y", "1x", "a b", FOREIGN_PORT_NAME)

#: The message prefix the rule carried before contract ``7.6.0`` for a stub
#: declaring ports, which the restated rule keeps for a stub port with an
#: interface, so the restatement is no breaking change to the finding.
PORTS_PREFIX = "SWC 'ExternalStub' declares 'external: true' and also ports;"


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    return stubs.component_stub_ports_model_root(tmp_path / "model")


def _records(model_root: Path) -> list[validator.ConsistencyIssue]:
    return list(check_cross_layer_consistency(str(model_root)).records)


def _of(model_root: Path, rule: str) -> list[validator.ConsistencyIssue]:
    return [record for record in _records(model_root) if record.rule == rule]


# ---------------------------------------------------------------------------
# The fixture, and the restated rule
# ---------------------------------------------------------------------------


class TestAStubMayDeclarePorts:
    """The positive half of the restated ``swc.external-with-ports``."""

    def test_the_wired_fixture_is_clean(self, model_root: Path) -> None:
        """Ports by name and direction, a hint on one, two connectors and a delegation: no finding."""
        assert stubs.rules(model_root) == []

    def test_a_hint_naming_nothing_declared_is_no_finding(self, model_root: Path) -> None:
        """No rule reads the hint, so one that no longer resolves is not a finding."""
        stubs.set_port(model_root, STUB, "pExt", observed_interface="INoLongerDeclared")

        assert stubs.rules(model_root) == []

    def test_a_stub_without_ports_is_still_clean(self, model_root: Path) -> None:
        """The portless stub the rule accepted before it was restated."""
        stubs.change(model_root, "swc/external/OtherStub.yaml", lambda d: d.update(ports=[]))
        stubs.change(
            model_root, "connectors/stub_link.yaml", lambda d: d.update(to={"swc": "Beta", "port": "rFromExt"})
        )

        assert "swc.external-with-ports" not in stubs.rules(model_root)


class TestAStubsPortMayNotDeclareAnInterfaceOrMeasures:
    """The negative half: what a stub's port may not carry, one finding per cause."""

    def test_a_declared_interface_keeps_the_subject_and_the_prefix(self, model_root: Path) -> None:
        """The same subject address and message prefix as before the restatement."""
        stubs.set_port(model_root, STUB, "pExt", interface="ITrigger")

        (record,) = _of(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("ports"),)),)
        assert record.message.startswith(PORTS_PREFIX)
        assert "port 'pExt' declares an interface" in record.message
        assert stubs.rules(model_root) == ["swc.external-with-ports"]

    def test_an_undeclared_interface_on_a_stubs_port_is_one_finding(self, model_root: Path) -> None:
        """``port.undeclared-interface`` skips a stub's port; before ``7.6.0`` both rules reported it."""
        stubs.set_port(model_root, STUB, "rExt", interface="IAbsent")

        assert stubs.rules(model_root) == ["swc.external-with-ports"]

    def test_measures_on_a_stubs_provide_port_is_one_finding(self, model_root: Path) -> None:
        """``safety.measure-on-provide-port`` leaves a stub's port to ``swc.external-with-ports``."""
        stubs.set_port(model_root, STUB, "pExt", measures={"trigger": "an argument nothing reads"})

        (record,) = _of(model_root, "swc.external-with-ports")
        assert record.message.startswith(PORTS_PREFIX)
        assert "port 'pExt' declares measures" in record.message
        assert stubs.rules(model_root) == ["swc.external-with-ports"]

    def test_measures_on_a_stubs_require_port_is_one_finding(self, model_root: Path) -> None:
        """``safety.measure-unknown-element`` likewise, on a require port."""
        stubs.set_port(model_root, STUB, "rExt", measures={"trigger": "an argument nothing reads"})

        assert stubs.rules(model_root) == ["swc.external-with-ports"]

    def test_both_on_one_port_and_on_two_are_one_finding_naming_each(self, model_root: Path) -> None:
        stubs.set_port(model_root, STUB, "pExt", interface="ITrigger", measures={"trigger": "m"})
        stubs.set_port(model_root, STUB, "rExt", interface="ITrigger")

        (record,) = _of(model_root, "swc.external-with-ports")
        assert "port 'pExt' declares an interface and measures" in record.message
        assert "port 'rExt' declares an interface" in record.message
        assert stubs.rules(model_root) == ["swc.external-with-ports"]

    def test_a_runnable_is_still_reported_and_ports_are_not_named(self, model_root: Path) -> None:
        stubs.change(model_root, STUB, lambda d: d.update(runnables=[{"name": "Run"}]))

        (record,) = _of(model_root, "swc.external-with-ports")
        assert record.subjects == (Subject("swc", "ExternalStub", (field_step("runnables"),)),)
        assert "ports" not in record.message.split(";")[0]


# ---------------------------------------------------------------------------
# port.undeclared-interface, reworded
# ---------------------------------------------------------------------------


class TestADeclaredComponentsPortStillNeedsAnInterface:
    """``interface`` is optional at load; ``check-model`` is where a declared port without one is refused."""

    def test_a_missing_interface_is_the_existing_rule_reworded(self, model_root: Path) -> None:
        stubs.set_port(model_root, BETA, "pOut", interface=None)

        (record,) = _of(model_root, "port.undeclared-interface")
        assert record.subjects == (
            Subject("swc", "Beta", (field_step("ports"), member_step("pOut"), field_step("interface"))),
        )
        assert record.message.startswith("SWC 'Beta' port 'pOut' declares no interface")
        assert record.target is None

    def test_an_undeclared_interface_keeps_its_message(self, model_root: Path) -> None:
        stubs.set_port(model_root, BETA, "pOut", interface="IAbsent")

        (record,) = _of(model_root, "port.undeclared-interface")
        assert record.message.startswith("SWC 'Beta' port 'pOut' references interface 'IAbsent'")
        assert record.target == "IAbsent"

    def test_a_hint_on_a_declared_port_does_not_stand_in_for_the_interface(self, model_root: Path) -> None:
        """On a declared component the hint is inert; the finding stays."""
        stubs.set_port(model_root, BETA, "pOut", interface=None, observed_interface="ISignal")

        assert [record.rule for record in _of(model_root, "port.undeclared-interface")] == ["port.undeclared-interface"]

    def test_every_judging_reader_abstains_on_a_missing_interface(self, model_root: Path) -> None:
        """One finding per cause: the connector, the delegation, the access and the safety rules add none."""
        for port in ("pOut", "pInner", "rFromExt"):
            stubs.set_port(model_root, BETA, port, interface=None)
        stubs.set_port(model_root, BETA, "rFromExt", measures={"trigger": "m"})
        stubs.change(
            model_root,
            BETA,
            lambda d: d.update(runnables=[{"name": "Run", "reads": [{"port": "rFromExt", "element": "trigger"}]}]),
        )

        assert stubs.rules(model_root) == ["port.undeclared-interface"] * 3


# ---------------------------------------------------------------------------
# Connections to a stub's port
# ---------------------------------------------------------------------------


class TestConnectionsToAStubsPort:
    """Resolved by name and direction; the interface abstains, the hint is never compared."""

    def test_an_unknown_stub_port_is_an_unknown_port(self, model_root: Path) -> None:
        stubs.change(model_root, "connectors/ext_link.yaml", lambda d: d["from"].update(port="pAbsent"))

        assert stubs.rules(model_root) == ["connector.endpoint-unknown-port"]

    def test_direction_still_judges_an_assembly_connector(self, model_root: Path) -> None:
        stubs.set_port(model_root, "swc/external/OtherStub.yaml", "rOther", direction="provide")

        assert stubs.rules(model_root) == ["connector.to-not-require"]

    def test_direction_still_judges_a_delegation(self, model_root: Path) -> None:
        stubs.set_port(model_root, STUB, "rExt", direction="provide")

        assert stubs.rules(model_root) == ["composition.delegation-port-direction-mismatch"]

    def test_an_unknown_stub_port_in_a_delegation(self, model_root: Path) -> None:
        stubs.change(model_root, "compositions/Sub.yaml", lambda d: d["delegations"][-1].update(member_port="rAbsent"))

        assert stubs.rules(model_root) == ["composition.delegation-unknown-port"]

    def test_a_hint_disagreeing_with_the_neighbour_is_never_compared(self, model_root: Path) -> None:
        """``pExt``'s hint says ``ISignal``; its neighbours declare ``ITrigger``. No interface finding."""
        stubs.set_port(model_root, STUB, "pExt", observed_interface="ISignal")

        assert stubs.rules(model_root) == []

    def test_a_stubs_port_reads_no_interface_layer_for_an_answer_that_always_abstains(
        self, model_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` on either side abstains before any layer read, on the connector and the delegation path."""

        def not_expected(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("an interface layer was read for a port without an interface")

        connectors = {connector.name: connector for connector in read_connectors(str(model_root))}
        monkeypatch.setattr(model_view, "read_interfaces", not_expected)
        monkeypatch.setattr(model_view, "read_data_description", not_expected)

        for name in ("ext_link", "stub_link"):
            assert validator.check_connector_compatibility(str(model_root), connectors[name]).compatible is True
        assert validator.check_composition_consistency(str(model_root)).records == ()
        assert (
            validator.interface_compatibility(None, "ITrigger", context="subset", interfaces={}, data_types={}) is None
        )


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------


class TestThePreflightDoesNotJudgeAStubsPort:
    def test_a_stub_port_against_a_declared_one_is_not_judged(self, model_root: Path) -> None:
        result = check_port_compatibility(str(model_root), "ExternalStub", "pExt", "Beta", "rFromExt")

        assert result.compatible is True
        assert result.reason.startswith("not judged: external component 'ExternalStub' port 'pExt'")

    def test_the_stub_is_named_whichever_side_it_is_on(self, model_root: Path) -> None:
        result = check_port_compatibility(str(model_root), "Beta", "rFromExt", "ExternalStub", "pExt")

        assert result.reason.startswith("not judged: external component 'ExternalStub' port 'pExt'")

    def test_stub_to_stub_is_not_judged(self, model_root: Path) -> None:
        result = check_port_compatibility(str(model_root), "ExternalStub", "pExt", "OtherStub", "rOther")

        assert result.compatible is True
        assert result.reason.startswith("not judged: external component 'ExternalStub' port 'pExt'")

    def test_the_hint_does_not_make_a_judgement(self, model_root: Path) -> None:
        """A hint equal to the neighbour's interface is still no declaration."""
        result = check_port_compatibility(str(model_root), "ExternalStub", "pExt", "Beta", "rFromExt")

        assert "ITrigger" not in result.reason

    def test_direction_is_judged_first(self, model_root: Path) -> None:
        result = check_port_compatibility(str(model_root), "ExternalStub", "rExt", "OtherStub", "rOther")

        assert result.compatible is False
        assert "one must be 'provide' and the other 'require'" in result.reason

    def test_two_declared_ports_are_answered_as_before(self, model_root: Path) -> None:
        result = check_port_compatibility(str(model_root), "Beta", "pInner", "Beta", "rFromExt")

        assert (result.compatible, result.reason) == (True, "")

    def test_a_port_without_an_interface_reads_no_data_type_layer(self, model_root: Path) -> None:
        """A ``None`` side abstains, so an unrelated malformed data type cannot fail the answer."""
        stubs.set_port(model_root, BETA, "pInner", interface=None)
        model_element_path(model_root, "uint8", layer="implementation_types").write_text(
            "name: uint8\nkind: no_such_kind\n", encoding="utf-8", newline="\n"
        )

        for pair in (("Beta", "pInner", "Beta", "rFromExt"), ("Beta", "rFromExt", "Beta", "pInner")):
            result = check_port_compatibility(str(model_root), *pair)
            assert (result.compatible, result.reason) == (True, "")

    def test_a_port_without_an_interface_against_an_interface_stub_is_still_not_judged(self, model_root: Path) -> None:
        stubs.interface_stub(model_root, "IStub", "/Other/Pkg/IStub", "SENDER-RECEIVER-INTERFACE")
        stubs.set_port(model_root, BETA, "pInner", interface=None)
        stubs.set_port(model_root, BETA, "rFromExt", interface="IStub")

        result = check_port_compatibility(str(model_root), "Beta", "pInner", "Beta", "rFromExt")

        assert result.compatible is True
        assert result.reason.startswith("not judged: external interface 'IStub'")


# ---------------------------------------------------------------------------
# The five identifier guards, each on its own
# ---------------------------------------------------------------------------


def _component(external: bool, port_name: str = FOREIGN_PORT_NAME) -> SWComponent:
    """A component carrying *port_name*, as a stub or as a declared component."""
    if external:
        return SWComponent(
            name="Foreign",
            external=True,
            external_ref="/Foreign/Pkg/Foreign",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
            ports=[Port(name=port_name, direction="provide")],
        )
    return SWComponent(name="Foreign", ports=[Port(name=port_name, direction="provide", interface="ISignal")])


def _staged(component: SWComponent) -> StagedModel:
    return StagedModel(registry=reg.current(), elements={"swc": [component.model_dump(exclude_defaults=True)]})


class TestAStubsPortNameIsAReferencePathSegment:
    """Every guard holds a stub's port name to the ``Identifier`` rule, each asked on its own.

    No ``SHORT-NAME`` carries it, but every end on the port is written as ``external_ref`` +
    ``/`` + the name, and a reference path segment is an ``Identifier``.
    """

    @pytest.mark.parametrize("bad", BAD_STUB_PORT_NAMES)
    def test_check_identifier_conformance(self, model_root: Path, bad: str) -> None:
        stubs.set_port(model_root, STUB, "pExt", name=bad)
        stubs.change(model_root, "connectors/ext_link.yaml", lambda d: d["from"].update(port=bad))
        stubs.change(model_root, "connectors/stub_link.yaml", lambda d: d["from"].update(port=bad))

        (record,) = check_identifier_conformance(str(model_root)).records
        assert record.rule == "identifier.declared"
        assert record.subjects == (
            Subject("swc", "ExternalStub", (field_step("ports"), member_step(bad), field_step("name"))),
        )

    @pytest.mark.parametrize("external", [True, False])
    @pytest.mark.parametrize("bad", BAD_STUB_PORT_NAMES)
    def test_the_exporters_component_guard(self, external: bool, bad: str) -> None:
        with pytest.raises(ValueError, match="port name") as caught:
            _check_component_names([_component(external, bad)])
        assert ("reference path segment" in str(caught.value)) is external

    @pytest.mark.parametrize("external", [True, False])
    def test_the_importers_guard(self, external: bool) -> None:
        reconciled = ReconciledModel(
            data_types=[],
            interfaces=[],
            swcs=[ImportedSwc(swc=_component(external), group="external", path="/Foreign/Pkg/Foreign")],
            connectors=[],
        )
        (line,) = identifier_violations(reconciled, [])
        assert "port" in line

    @pytest.mark.parametrize("bad", BAD_STUB_PORT_NAMES)
    def test_the_protocols_pre_materialization_guard(self, bad: str) -> None:
        for external in (True, False):
            (violation,) = emitted_identifier_violations(_staged(_component(external, bad)))
            assert [address.to_json() for address in violation.addresses] == [
                {"layer": "swc", "element": "Foreign", "path": [{"field": "ports"}, {"member": bad}, {"field": "name"}]}
            ]
            # The sentence names the emission that happens: a SHORT-NAME for a
            # declared component's port, a reference path segment for a stub's.
            assert ("last segment of an AUTOSAR reference path" in violation.message) is external
            assert ("emitted as an AUTOSAR SHORT-NAME" in violation.message) is not external

    def test_a_conforming_stub_port_name_passes_every_guard(self, tmp_path: Path) -> None:
        component = _component(True, "pConforming")
        _check_component_names([component])
        assert emitted_identifier_violations(_staged(component)) == ()
        (tmp_path / "swc").mkdir()
        write_element(str(tmp_path), "swc/external/Foreign.yaml", component, create=True)

    @pytest.mark.parametrize("external", [True, False])
    def test_the_element_writers_guard(self, tmp_path: Path, external: bool) -> None:
        (tmp_path / "swc").mkdir()
        relative = f"swc/{'external' if external else 'core'}/Foreign.yaml"
        with pytest.raises(ValueError, match="port name"):
            write_element(str(tmp_path), relative, _component(external), create=True)
        assert not (tmp_path / relative).exists()


# ---------------------------------------------------------------------------
# Pass D
# ---------------------------------------------------------------------------


class TestPassDAnswersEachRestatedFinding:
    """Each restated finding reaches the protocol, under its rule's code and its own address."""

    def test_a_stub_port_with_an_interface(self, model_root: Path) -> None:
        stubs.set_port(model_root, STUB, "pExt", interface="ITrigger")

        (violation,) = diagnose(check_cross_layer_consistency(str(model_root)))
        assert violation.error == CONSISTENCY_VIOLATION
        assert [address.to_json() for address in violation.addresses] == [
            {"layer": "swc", "element": "ExternalStub", "path": [{"field": "ports"}]}
        ]

    def test_a_stub_port_with_measures(self, model_root: Path) -> None:
        stubs.set_port(model_root, STUB, "pExt", measures={"trigger": "m"})

        (violation,) = diagnose(check_cross_layer_consistency(str(model_root)))
        assert violation.error == CONSISTENCY_VIOLATION

    def test_a_declared_port_without_an_interface(self, model_root: Path) -> None:
        """Two ports without one are two violations: with no target, nothing groups them."""
        stubs.set_port(model_root, BETA, "pOut", interface=None)
        stubs.set_port(model_root, BETA, "pInner", interface=None)

        violations = diagnose(check_cross_layer_consistency(str(model_root)))
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE, BROKEN_REFERENCE]
        assert [address.to_json() for violation in violations for address in violation.addresses] == [
            {
                "layer": "swc",
                "element": "Beta",
                "path": [{"field": "ports"}, {"member": port}, {"field": "interface"}],
            }
            for port in ("pOut", "pInner")
        ]

    def test_the_clean_fixture_is_answered_by_nothing(self, model_root: Path) -> None:
        assert diagnose(check_cross_layer_consistency(str(model_root))) == ()
