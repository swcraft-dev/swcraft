"""The ``compositions`` layer through the editing protocol, end to end.

The layer is projected generically, so this asserts that the general machinery answers for
it as for every other layer, in five scenarios a layer-specific shortcut would need:
discovery read from the live engine; a ``create`` of a composition with a member and a
delegation; a member's ``type`` retargeted under an ordinary scope with an empty safety
verdict (``safety_decision: not-applicable``; the classifier's half is
``test_editing_authorization.py::TestTheCompositionEditsAreNotWeakenings``); a removal
refused as ``unauthorized-delta`` beside its positive control; and an over-budget read
answering its hint chain, nothing truncated.

**What this module does not reach.** It never crosses a wire (``packages/mcp/tests``). It
asserts nothing about the sizes of the forms on a real model. And it says nothing about
whether a composition is valid: the layer's rules are ``test_validator_compositions.py``'s,
and pass D reaches them only where a scenario stages a sound result.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.audit import AUDIT_FILENAME, CHANGE_SET_APPROVED, SAFETY_NOT_ASKED
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.protocol import ERROR_CODES
from swcraft_tools.editing.read import RESPONSE_BUDGET_WIRE_BYTES
from swcraft_tools.editing.safety_guard import is_safety_relevant

# Fixtures and helpers

#: Boundary ports of the over-budget composition: wide enough that even the collection
#: ``summary`` (the binding form) refuses with room to spare, while the collection
#: ``index`` and the element's ``summary`` stay under the budget, which makes the hint
#: chain a chain. A change to what a composition port publishes, or to the budget, moves it.
WIDE_COMPOSITION_PORTS = 1500


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real project on disk, the vector harness's base model, which satisfies every composition rule."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _model_root(project_root: Path) -> Path:
    return project_root / "model"


def _tree_state(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _baseline(project_root: Path) -> str:
    from swcraft_tools.editing.staging import baseline_token

    return baseline_token(str(_model_root(project_root)))


def _change_set(project_root: Path, *operations: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "change_set",
        "baseline": _baseline(project_root),
        "intent": "a composition change set",
        "operations": list(operations),
    }


def _approver(
    approved: bool = True, *, seen: list[ChangeSetProposal] | None = None
) -> Callable[[ChangeSetProposal], ApprovalDecision]:
    def review(proposal: ChangeSetProposal) -> ApprovalDecision:
        if seen is not None:
            seen.append(proposal)
        return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=approved)

    return review


def _typed(address: dict[str, Any]) -> Address:
    """The wire address as the classifier's own dataclass, through ``Address.from_json``.

    The assertion is put to the production predicate over the production parse.
    """
    return Address.from_json(address)


def _audit(project_root: Path) -> list[dict[str, Any]]:
    log = project_root / "dist" / AUDIT_FILENAME
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


#: The address of the member whose ``type`` scenario 3 retargets.
_ALT_MEMBER_TYPE = {
    "layer": "compositions",
    "element": "Alt",
    "path": [{"field": "members"}, {"member": "alt"}, {"field": "type"}],
}

#: A whole composition: a boundary port, a member and the delegation that wires them,
#: one ``create`` reaching all three member kinds.
_SENSING = {
    "operation": "create",
    "address": {"layer": "compositions", "element": "Sensing"},
    "value": {
        "name": "Sensing",
        "title": "Sensing subsystem",
        "description": "Wraps the example provider behind a boundary port.",
        "ports": [{"name": "pOut", "direction": "provide", "interface": "IExample"}],
        "members": [{"name": "ExampleSWC", "type": "ExampleSWC"}],
        "delegations": [{"name": "dOut", "port": "pOut", "member": "ExampleSWC", "member_port": "pOut"}],
    },
}

#: A second composition holding one member under a name of its own, which scenario 3
#: retargets and scenario 4 removes. ``rule:composition.member-type-repeated`` refuses a
#: type twice in one composition, never across two.
_ALT = {
    "operation": "create",
    "address": {"layer": "compositions", "element": "Alt"},
    "value": {
        "name": "Alt",
        "title": "Alternative grouping",
        "members": [{"name": "alt", "type": "ExampleSWC"}],
    },
}


def _commit(project_root: Path, *operations: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Submit *operations* under an approving boundary and assert the commit."""
    response = handle_request(
        _change_set(project_root, *operations), project=project_root, review=_approver(True), **kwargs
    )
    assert response["status"] == "committed", response
    return response


# 1. Discovery publishes the layer, its four kinds and the address vocabulary


class TestDiscoveryPublishesTheLayer:
    """Asked of the live engine: a vector and its replay agree by construction."""

    @pytest.fixture
    def layer(self, project: Path) -> dict[str, Any]:
        response = handle_request({"operation": "discover", "layer": "compositions"}, project=project)
        assert response["response"] == "discover", response
        assert [entry["layer"] for entry in response["layers"]] == ["compositions"]
        return response["layers"][0]

    def test_the_layer_is_published_as_a_mandatory_present_layer(self, layer: dict[str, Any]) -> None:
        assert layer["identity_field"] == "name"
        # The default `summary` omits `optional: false`: absent means `false`.
        assert "optional" not in layer
        assert layer["present"] is True

    def test_the_four_kinds_are_published(self, layer: dict[str, Any]) -> None:
        """The element kind and the three member kinds, in the contract's order.

        The member kinds carry ``member_of`` and ``member_only``, which makes an address
        writable from the descriptor alone.
        """
        assert [kind["kind"] for kind in layer["element_kinds"]] == [
            "composition",
            "composition_port",
            "composition_member",
            "delegation",
        ]
        by_kind = {kind["kind"]: kind for kind in layer["element_kinds"]}
        assert [field["name"] for field in by_kind["composition_port"]["fields"]] == [
            "name",
            "direction",
            "interface",
            "description",
        ]
        assert [field["name"] for field in by_kind["composition_member"]["fields"]] == [
            "name",
            "type",
            "composition",
            "description",
        ]
        assert [field["name"] for field in by_kind["delegation"]["fields"]] == [
            "name",
            "port",
            "member",
            "member_port",
            "description",
        ]
        for kind in ("composition_port", "composition_member", "delegation"):
            assert by_kind[kind]["member_only"] is True
            assert by_kind[kind]["member_of"], kind

    def test_the_three_collections_publish_how_their_members_are_addressed(self, layer: dict[str, Any]) -> None:
        """The other half of the vocabulary: a member step needs an identity field."""
        fields = {field["name"]: field for field in layer["element_kinds"][0]["fields"]}
        assert [fields[name]["collection"]["member_kinds"] for name in ("ports", "members", "delegations")] == [
            ["composition_port"],
            ["composition_member"],
            ["delegation"],
        ]
        for name in ("ports", "members", "delegations"):
            assert fields[name]["collection"]["member_identity"] == "field"
            assert fields[name]["collection"]["identity_field"] == "name"

    def test_the_layers_three_references_are_published_with_their_targets(self, layer: dict[str, Any]) -> None:
        """The members' ``type`` and ``composition`` and the boundary port's interface, with their targets.

        ``type`` resolves into ``swc`` and ``composition`` into ``compositions``: a wrong
        layer would put a false node in every ``impact`` answer.
        """
        published: dict[tuple[str, str], Any] = {}
        for kind in layer["element_kinds"]:
            for field in kind["fields"]:
                if "reference" in field:
                    published[(kind["kind"], field["name"])] = field["reference"]
        assert published == {
            ("composition_port", "interface"): {"layer": "interfaces", "identity_field": "name"},
            ("composition_member", "type"): {"layer": "swc", "identity_field": "name"},
            ("composition_member", "composition"): {"layer": "compositions", "identity_field": "name"},
        }

    def test_every_published_kind_is_really_addressable(self, project: Path) -> None:
        """One address per published kind, built from the descriptor alone, is answered rather than refused."""
        _commit(project, _SENSING)
        addresses = {
            "composition": {"layer": "compositions", "element": "Sensing"},
            "composition_port": {
                "layer": "compositions",
                "element": "Sensing",
                "path": [{"field": "ports"}, {"member": "pOut"}],
            },
            "composition_member": {
                "layer": "compositions",
                "element": "Sensing",
                "path": [{"field": "members"}, {"member": "ExampleSWC"}],
            },
            "delegation": {
                "layer": "compositions",
                "element": "Sensing",
                "path": [{"field": "delegations"}, {"member": "dOut"}],
            },
        }
        for kind, address in addresses.items():
            answered = handle_request({"operation": "read", "address": address}, project=project)
            assert answered["response"] == "read", (kind, answered)
            assert answered["address"] == address, (kind, answered)


# 2 and 3. The change sets that commit


class TestTheCompositionChangeSetsThatCommit:
    def test_creating_a_composition_with_a_member_and_a_delegation_commits(self, project: Path) -> None:
        response = _commit(project, _SENSING)

        assert [entry["summary"] for entry in response["delta"]] == ["compositions: + Sensing"]
        written = yaml.safe_load((_model_root(project) / "compositions" / "Sensing.yaml").read_text(encoding="utf-8"))
        # Each member carries the fields its kind declares; the delegation's
        # wiring is what pass D checked.
        assert written["ports"] == [{"name": "pOut", "direction": "provide", "interface": "IExample"}]
        assert written["members"] == [{"name": "ExampleSWC", "type": "ExampleSWC"}]
        assert written["delegations"] == [
            {"name": "dOut", "port": "pOut", "member": "ExampleSWC", "member_port": "pOut"}
        ]
        # `root` is omitted at its default: only the root carries the key.
        assert "root" not in written

    def test_retargeting_a_members_type_commits_under_an_ordinary_scope(self, project: Path) -> None:
        """Retargeting a member's type is an ordinary edit: one scope entry, an empty safety verdict.

        "The guard had nothing to say", not "the guard let it through".
        """
        _commit(project, _ALT)
        seen: list[ChangeSetProposal] = []
        scope = [{"operation": "update", "address": _ALT_MEMBER_TYPE}]

        response = handle_request(
            _change_set(project, {"operation": "update", "address": _ALT_MEMBER_TYPE, "value": "ExampleConsumerSWC"}),
            project=project,
            review=_approver(True, seen=seen),
            approved_scope=scope,
            scope_id="turn-1",
        )

        assert response["status"] == "committed", response
        assert [entry["before"] for entry in response["delta"]] == ["ExampleSWC"]
        assert [entry["after"] for entry in response["delta"]] == ["ExampleConsumerSWC"]
        assert not is_safety_relevant("update", _typed(_ALT_MEMBER_TYPE))
        assert seen[0].safety.changes == ()
        approved = [entry for entry in _audit(project) if entry["operation"] == CHANGE_SET_APPROVED]
        assert approved[-1]["args"]["safety_decision"] == SAFETY_NOT_ASKED

    def test_the_retarget_is_visible_where_the_model_is_read_back(self, project: Path) -> None:
        _commit(project, _ALT)
        _commit(
            project,
            {"operation": "update", "address": _ALT_MEMBER_TYPE, "value": "ExampleConsumerSWC"},
            approved_scope=[{"operation": "update", "address": _ALT_MEMBER_TYPE}],
            scope_id="turn-1",
        )
        answered = handle_request({"operation": "read", "address": _ALT_MEMBER_TYPE}, project=project)
        assert answered["value"] == "ExampleConsumerSWC"


# 4. Removing a composition needs the authority to remove it


class TestRemovingACompositionNeedsDeleteAuthority:
    """The refusal and its positive control: the control makes it a statement about the scope."""

    def test_a_scope_without_the_removal_refuses_it(self, project: Path) -> None:
        _commit(project, _ALT)
        before = _tree_state(_model_root(project))

        response = handle_request(
            _change_set(project, {"operation": "remove", "address": {"layer": "compositions", "element": "Alt"}}),
            project=project,
            review=_approver(True),
            # Grants the create at the same address and not the remove: the
            # authority is per operation, not per element.
            approved_scope=[{"operation": "create", "address": {"layer": "compositions", "element": "Alt"}}],
            scope_id="turn-1",
        )

        assert response["response"] == "error"
        assert response["error"] == "unauthorized-delta"
        assert response["failure_class"] == "unauthorized"
        assert response["addresses"] == [{"layer": "compositions", "element": "Alt"}]
        assert _tree_state(_model_root(project)) == before

    def test_an_empty_scope_refuses_it_too(self, project: Path) -> None:
        """An empty scope is a statement, not an omission: nothing is approved."""
        _commit(project, _ALT)

        response = handle_request(
            _change_set(project, {"operation": "remove", "address": {"layer": "compositions", "element": "Alt"}}),
            project=project,
            review=_approver(True),
            approved_scope=[],
            scope_id="turn-1",
        )

        assert response["error"] == "unauthorized-delta", response

    def test_the_scope_that_covers_it_commits(self, project: Path) -> None:
        _commit(project, _ALT)
        removal = {"operation": "remove", "address": {"layer": "compositions", "element": "Alt"}}

        response = handle_request(
            _change_set(project, removal),
            project=project,
            review=_approver(True),
            approved_scope=[{"operation": "remove", "address": {"layer": "compositions", "element": "Alt"}}],
            scope_id="turn-1",
        )

        assert response["status"] == "committed", response
        assert not (_model_root(project) / "compositions" / "Alt.yaml").exists()
        # Not a safety act: a composition declares none of the fields the classifier compares.
        assert not is_safety_relevant("remove", _typed({"layer": "compositions", "element": "Alt"}))


# 5. The over-budget read, and the chain that answers instead


def _wide_composition(count: int = WIDE_COMPOSITION_PORTS) -> dict[str, Any]:
    """One composition that is merely very wide, every boundary port on the base's ``IExample``."""
    return {
        "name": "Wide",
        "title": "A composition wide enough that a full read does not fit in one response",
        "ports": [
            {
                "name": f"p{index:03d}",
                "direction": "provide" if index % 2 == 0 else "require",
                "interface": "IExample",
            }
            for index in range(count)
        ],
    }


@pytest.fixture
def wide(project: Path) -> Path:
    path = _model_root(project) / "compositions" / "Wide.yaml"
    path.write_text(yaml.safe_dump(_wide_composition(), sort_keys=False), encoding="utf-8", newline="\n")
    return project


class TestTheOverBudgetReadOfALargeComposition:
    """A read above the budget is refused with a chain, never truncated."""

    def test_the_closed_error_enum_still_has_eleven_members(self) -> None:
        """No twelfth code: a big answer gets the code the protocol already has."""
        assert len(ERROR_CODES) == 11
        assert "over-budget" in ERROR_CODES

    def test_the_element_read_is_refused_with_measured_and_one_hint(self, wide: Path) -> None:
        address = {"layer": "compositions", "element": "Wide"}

        refused = handle_request({"operation": "read", "address": address}, project=wide)

        assert refused["error"] == "over-budget"
        assert refused["failure_class"] == "invalid"
        assert refused["addresses"] == [address]
        assert refused["measured"]["budget"] == RESPONSE_BUDGET_WIRE_BYTES
        assert refused["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
        # An element address answers `full` by default, so the chain's one step
        # is the narrower form.
        assert refused["hints"] == [{"operation": "read", "address": address, "form": "summary"}]

    def test_the_hint_the_element_read_offers_answers(self, wide: Path) -> None:
        """A chain that does not answer is a dead end dressed as a chain."""
        address = {"layer": "compositions", "element": "Wide"}
        refused = handle_request({"operation": "read", "address": address}, project=wide)

        answered = handle_request(refused["hints"][0], project=wide)

        assert answered["response"] == "read", answered
        assert answered["form"] == "summary"

    def test_the_collection_read_offers_the_three_step_chain(self, wide: Path) -> None:
        """Less prose, less shape, then the first page, in narrowing order."""
        address = {"layer": "compositions", "element": "Wide", "path": [{"field": "ports"}]}

        refused = handle_request({"operation": "read", "address": address, "form": "full"}, project=wide)

        assert refused["error"] == "over-budget"
        assert refused["measured"]["elements"] == WIDE_COMPOSITION_PORTS
        assert [hint.get("form") for hint in refused["hints"]] == ["summary", "index", "full"]
        assert [hint.get("limit") for hint in refused["hints"]] == [None, None, 50]
        assert all(hint["address"] == address for hint in refused["hints"])

    def test_the_chain_is_walked_until_one_step_answers(self, wide: Path) -> None:
        """A hint is a form the address admits, not a promise that it fits.

        Here the first step is itself over the budget; its refusal carries a strictly
        shorter chain, so a client that walks reaches an answer.
        """
        address = {"layer": "compositions", "element": "Wide", "path": [{"field": "ports"}]}
        request: dict[str, Any] = {"operation": "read", "address": address, "form": "full"}

        walked: list[dict[str, Any]] = []
        for _ in range(4):
            answer = handle_request(request, project=wide)
            walked.append(answer)
            if answer["response"] != "error":
                break
            assert answer["error"] == "over-budget", answer
            assert answer["hints"], "a refusal with no chain is a dead end"
            request = answer["hints"][0]

        assert [answer["response"] for answer in walked] == ["error", "error", "read"], walked
        # Each chain is strictly shorter than the one before, so the walk terminates.
        assert len(walked[0]["hints"]) > len(walked[1]["hints"])
        assert walked[-1]["form"] == "index"

    def test_the_page_hint_answers_a_first_page_of_the_published_size(self, wide: Path) -> None:
        address = {"layer": "compositions", "element": "Wide", "path": [{"field": "ports"}]}
        refused = handle_request({"operation": "read", "address": address, "form": "full"}, project=wide)

        paged = handle_request(refused["hints"][-1], project=wide)

        assert paged["response"] == "read", paged
        assert paged["page"]["total"] == WIDE_COMPOSITION_PORTS
        assert paged["page"]["returned"] == 50
        assert paged["page"]["truncated"] is True
        assert paged["page"]["next"]

    def test_nothing_is_truncated(self, wide: Path) -> None:
        """The refusal carries no partial answer: no ``value``, no ``members``."""
        refused = handle_request(
            {"operation": "read", "address": {"layer": "compositions", "element": "Wide"}}, project=wide
        )
        assert refused["response"] == "error"
        assert "value" not in refused and "members" not in refused and "page" not in refused
