"""The validator's *not judged* predicate, asked one pair at a time over in-memory interfaces.

``not_judged_component``, ``not_judged_stub`` and ``not_judged_reason`` are the two halves (and their
sum) of what :func:`check_port_compatibility` answers as *not judged*. Covered: the four *not judged*
cases (a stub component's port at either end, two different interface stubs, a declared interface
against a different stub, an unset or undeclared interface against a stub); the judged cases that
answer ``None``; and that two declared interfaces are answered without asking the compatibility
predicate.

The oracle that the pre-flight's answers did not change is the pre-flight's own suites
(``test_stub_ports_validator.py``, ``test_external_stubs_*``, the ``mcp_server`` pre-flight modules).
What this module does not reach: the diagram extractor's use of the predicate, and any interface
shape beyond the ones built below.
"""

from __future__ import annotations

import pytest
import validator_package
from swcraft_tools import validator
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import CSInterface, ExternalInterface, Interface, SRInterface
from swcraft_tools.validator import PortEnd, not_judged_component, not_judged_reason, not_judged_stub

#: Declared interfaces are built without members (``model_construct``): neither half
#: of the predicate reads a declared interface's members.
INTERFACES: dict[str, Interface] = {
    "ISpeed": SRInterface.model_construct(name="ISpeed", kind="sender_receiver", data_elements=[]),
    "IOther": SRInterface.model_construct(name="IOther", kind="sender_receiver", data_elements=[]),
    "ICall": CSInterface.model_construct(name="ICall", kind="client_server", operations=[]),
    "IStubSr": ExternalInterface(
        name="IStubSr",
        kind="external_interface",
        external_ref="/Ext/IStubSr",
        external_dest="SENDER-RECEIVER-INTERFACE",
    ),
    "IStubSr2": ExternalInterface(
        name="IStubSr2",
        kind="external_interface",
        external_ref="/Ext/IStubSr2",
        external_dest="SENDER-RECEIVER-INTERFACE",
    ),
    "IStubCs": ExternalInterface(
        name="IStubCs", kind="external_interface", external_ref="/Ext/IStubCs", external_dest="CLIENT-SERVER-INTERFACE"
    ),
    "IStubAbstract": ExternalInterface(
        name="IStubAbstract",
        kind="external_interface",
        external_ref="/Ext/IStubAbstract",
        external_dest="PORT-INTERFACE",
    ),
}

#: The predicate's sentence for ``IStubSr``, whose kind agrees with its counterpart.
STUB_SR_REASON = (
    "not judged: external interface 'IStubSr' is a stub whose content is outside the project; its kind"
    " (SENDER-RECEIVER-INTERFACE) agrees, and nothing beyond an external interface's kind can be compared."
)

DECLARED_END = PortEnd("Beta", False, "rIn")
STUB_END = PortEnd("ExternalStub", True, "pExt")
OTHER_STUB_END = PortEnd("OtherStub", True, "rOther")


def _stub(*, required: str | None, provided: str | None) -> str | None:
    return not_judged_stub(required, provided, context="subset", interfaces=INTERFACES)


class TestTheComponentHalf:
    @pytest.mark.parametrize("ends", [(STUB_END, DECLARED_END), (DECLARED_END, STUB_END)])
    def test_a_stub_at_either_end_is_not_judged(self, ends: tuple[PortEnd, ...]) -> None:
        reason = not_judged_component(ends)

        assert reason is not None
        assert reason.startswith("not judged: external component 'ExternalStub' port 'pExt'")

    @pytest.mark.parametrize(
        ("ends", "named"),
        [
            ((STUB_END, OTHER_STUB_END), "not judged: external component 'ExternalStub' port 'pExt'"),
            ((OTHER_STUB_END, STUB_END), "not judged: external component 'OtherStub' port 'rOther'"),
        ],
    )
    def test_two_stub_ends_name_the_first_in_caller_order(self, ends: tuple[PortEnd, ...], named: str) -> None:
        reason = not_judged_component(ends)

        assert reason is not None
        assert reason.startswith(named)

    def test_two_declared_owners_are_judged(self) -> None:
        assert not_judged_component((DECLARED_END, PortEnd("Alpha", False, "pOut"))) is None


class TestTheStubHalf:
    def test_two_different_stubs_are_not_judged_naming_the_required_first(self) -> None:
        reason = _stub(required="IStubSr", provided="IStubSr2")

        assert reason is not None
        assert reason.startswith("not judged: external interface 'IStubSr'")

    @pytest.mark.parametrize(("required", "provided"), [("ISpeed", "IStubSr"), ("IStubSr", "ISpeed")])
    def test_a_declared_interface_against_a_different_stub_is_not_judged(self, required: str, provided: str) -> None:
        reason = _stub(required=required, provided=provided)

        assert reason is not None
        assert reason.startswith("not judged: external interface 'IStubSr'")

    def test_a_stub_whose_dest_tells_no_kind_is_not_judged_and_says_so(self) -> None:
        reason = _stub(required="ICall", provided="IStubAbstract")

        assert reason is not None
        assert "does not tell its interface kind" in reason

    @pytest.mark.parametrize(("required", "provided"), [(None, "IStubSr"), ("IStubSr", None)])
    def test_an_unset_interface_against_a_stub_is_not_judged(self, required: str | None, provided: str | None) -> None:
        reason = _stub(required=required, provided=provided)

        assert reason is not None
        assert reason.startswith("not judged: external interface 'IStubSr'")

    @pytest.mark.parametrize(("required", "provided"), [("INoSuch", "IStubSr"), ("IStubSr", "INoSuch")])
    def test_an_interface_that_names_nothing_against_a_stub_is_not_judged(self, required: str, provided: str) -> None:
        """Both names set, one declared nowhere: the compatibility predicate abstains on the lookup."""
        assert (
            validator.interface_compatibility(
                required, provided, context="subset", interfaces=INTERFACES, data_types=DataDescription(())
            )
            is None
        )
        assert _stub(required=required, provided=provided) == STUB_SR_REASON

    def test_one_stub_on_both_ends_is_judged(self) -> None:
        assert _stub(required="IStubSr", provided="IStubSr") is None

    @pytest.mark.parametrize(
        ("required", "provided"), [("ISpeed", "ISpeed"), ("ISpeed", "IOther"), ("ISpeed", "ICall")]
    )
    def test_two_declared_interfaces_are_judged(self, required: str, provided: str) -> None:
        assert _stub(required=required, provided=provided) is None

    @pytest.mark.parametrize(("required", "provided"), [(None, "ISpeed"), ("ISpeed", None), (None, None)])
    def test_an_unset_interface_against_a_declared_one_is_judged(
        self, required: str | None, provided: str | None
    ) -> None:
        assert _stub(required=required, provided=provided) is None

    @pytest.mark.parametrize(("required", "provided"), [("ISpeed", "IStubCs"), ("IStubCs", "IStubSr")])
    def test_a_kind_mismatch_across_a_stub_is_a_finding_and_not_not_judged(self, required: str, provided: str) -> None:
        assert (
            validator.interface_compatibility(
                required, provided, context="subset", interfaces=INTERFACES, data_types=DataDescription(())
            )
            is not None
        )
        assert _stub(required=required, provided=provided) is None

    def test_declared_interfaces_never_reach_the_compatibility_predicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def not_expected(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("interface_compatibility was asked about two declared interfaces")

        validator_package.patch_everywhere(monkeypatch, "interface_compatibility", not_expected)

        assert _stub(required="ISpeed", provided="ICall") is None


class TestTheSum:
    def test_the_component_half_answers_first(self) -> None:
        reason = not_judged_reason(
            (STUB_END, DECLARED_END), "ISpeed", "IStubSr", context="subset", interfaces=INTERFACES
        )

        assert reason is not None
        assert reason.startswith("not judged: external component 'ExternalStub'")

    def test_the_stub_half_answers_when_no_owner_is_a_stub(self) -> None:
        reason = not_judged_reason(
            (DECLARED_END, PortEnd("Alpha", False, "pOut")),
            "ISpeed",
            "IStubSr",
            context="subset",
            interfaces=INTERFACES,
        )

        assert reason is not None
        assert reason.startswith("not judged: external interface 'IStubSr'")

    def test_a_judged_pair_answers_none(self) -> None:
        assert (
            not_judged_reason(
                (DECLARED_END, PortEnd("Alpha", False, "pOut")),
                "ISpeed",
                "IOther",
                context="subset",
                interfaces=INTERFACES,
            )
            is None
        )

    @pytest.mark.parametrize("outer_direction", ["provide", "require"])
    def test_a_delegation_clause_gives_the_connector_answer_across_a_stub(self, outer_direction: str) -> None:
        """Across a stub only the kind is compared, so neither delegation clause moves the answer."""
        context = validator.delegation_context(outer_direction)

        assert (
            not_judged_reason((DECLARED_END,), "ISpeed", "IStubSr", context=context, interfaces=INTERFACES)
            == STUB_SR_REASON
        )
