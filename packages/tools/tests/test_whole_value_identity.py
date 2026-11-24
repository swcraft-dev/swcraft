"""A whole-value member's identity leaves out every field at its contract default.

A ``whole_value`` list member *is* its identity, and a dump carries its
defaults, so without a rule a field such as the timing event's ``disabled_modes``
(default ``[]``) would make ``{"type": "timing", "period": 0.01}`` stop naming its
event. The rule is ``editing.address.reduce_whole_value``: a key whose value equals the
contract's declared default for that key in the member kind is not part of the
identity. It applies to identities only — a change's address, a scope entry's,
a member match and a delta's member step — and never to a value: a ``read`` and
a delta's ``before`` / ``after`` keep the full form.

What this module holds:

* an event matched with and without an explicit ``disabled_modes: []``, through a
  change's address and through a scope entry's, both ways round;
* every whole-value address a committed editing vector spells, and the deepest
  address the agent's scope-proposal tests spell, unchanged by the reduction —
  but for a composite member step that leaves out an optional identity field,
  which the same pass spells out as ``null``;
* the full form of every value;
* the address depth still five with ``disabled_modes`` declared, and an address
  below a whole-value member refused by the resolver.

What it does not reach: a nested reduction. The rule is one level deep, because
no shipped whole-value kind holds a nested whole-value kind with an optional
field; a disabled mode's three fields are all required.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    AddressError,
    FieldStep,
    MemberStep,
    ScopeEntry,
    collection_at,
    covers,
    is_address_prefix,
    member_identity_of,
    member_matches,
    reduce_address,
    reduce_whole_value,
    resolve,
)
from swcraft_tools.editing.read import read_value
from swcraft_tools.schema import InitEvent, OperationEvent, TimingEvent
from swcraft_tools.snapshot import load_snapshot

PLAIN = {"type": "timing", "period": 0.01}
EXPLICIT = {"type": "timing", "period": 0.01, "disabled_modes": []}
#: The full value of the same event, every field at its default included (`name` too),
#: which is what a read and a delta's halves carry.
FULL = {**EXPLICIT, "name": None}
MODE = {"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"}

VECTOR_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol"


def _events() -> reg.CollectionDescriptor:
    collection = reg.REGISTRY.element_kind("runnable").get_field("events").collection
    assert collection is not None
    return collection


def _events_address(member: Any, *tail: Any) -> Address:
    return Address(
        layer="swc",
        element="ExampleSWC",
        path=(FieldStep("runnables"), MemberStep("ExampleRun"), FieldStep("events"), MemberStep(member), *tail),
    )


# ---------------------------------------------------------------------------
# The reduction itself
# ---------------------------------------------------------------------------


class TestTheReduction:
    def test_a_default_valued_field_is_left_out_of_the_identity(self) -> None:
        assert reduce_whole_value(EXPLICIT, _events()) == PLAIN
        assert reduce_whole_value(PLAIN, _events()) == PLAIN
        assert reduce_whole_value(TimingEvent(type="timing", period=0.01), _events()) == PLAIN

    def test_a_stated_value_stays_in_it(self) -> None:
        stated = {**PLAIN, "disabled_modes": [MODE]}
        assert reduce_whole_value(stated, _events()) == stated

    def test_the_other_event_kinds_reduce_to_themselves(self) -> None:
        # Every other field of an init event and of an operation event is
        # required, and their `name` is left out at its `None` default.
        assert reduce_whole_value(InitEvent(type="init"), _events()) == {"type": "init"}
        operation = {"type": "operation", "port": "pServe", "operation": "Get"}
        assert reduce_whole_value(OperationEvent(**operation), _events()) == operation

    def test_the_defaults_are_the_contract_s(self) -> None:
        timing = next(literal for literal, _ in _events().member_defaults if literal == "timing")
        assert dict(dict(_events().member_defaults)[timing]) == {"disabled_modes": [], "name": None}
        assert dict(dict(_events().member_defaults)["init"]) == {"name": None}

    def test_a_string_member_and_a_field_identity_are_untouched(self) -> None:
        # `satisfies` is a string-member collection.
        links = reg.REGISTRY.element_kind("swc").get_field("satisfies").collection
        ports = reg.REGISTRY.element_kind("swc").get_field("ports").collection
        assert reduce_whole_value("REQ_EXAMPLE_001@1", links) == "REQ_EXAMPLE_001@1"
        assert reduce_whole_value({"name": "pOut", "com_specs": []}, ports) == {"name": "pOut", "com_specs": []}

    def test_member_matching_compares_reduced_identities_both_ways(self) -> None:
        event = TimingEvent(type="timing", period=0.01)
        assert member_matches(event, _events(), PLAIN)
        assert member_matches(event, _events(), EXPLICIT)
        assert not member_matches(event, _events(), {**PLAIN, "disabled_modes": [MODE]})
        assert member_identity_of(event, _events()) == PLAIN


class TestAScopeAndAChangeCompareReducedIdentities:
    def test_the_two_spellings_reduce_to_one_address(self) -> None:
        assert reduce_address(_events_address(EXPLICIT)) == reduce_address(_events_address(PLAIN))
        assert reduce_address(_events_address(EXPLICIT)).to_json() == _events_address(PLAIN).to_json()

    @pytest.mark.parametrize(("approved", "changed"), [(PLAIN, EXPLICIT), (EXPLICIT, PLAIN)])
    def test_a_scope_entry_covers_the_change_whichever_side_spells_the_default(
        self, approved: dict[str, Any], changed: dict[str, Any]
    ) -> None:
        entry = ScopeEntry(operation="remove", address=reduce_address(_events_address(approved)))
        change = reduce_address(_events_address(changed))
        assert covers(entry, "remove", change)
        assert is_address_prefix(entry.address, change)
        # Unreduced, the two spellings are two identities: the reduction is what
        # makes them one, and it is applied where an address is parsed.
        assert not is_address_prefix(_events_address(approved), _events_address(changed))

    @pytest.mark.parametrize(
        ("approved", "changed"),
        [(PLAIN, EXPLICIT), (EXPLICIT, PLAIN), (EXPLICIT, EXPLICIT)],
        ids=["scope-plain", "change-plain", "both-explicit"],
    )
    def test_the_engine_stages_a_change_its_scope_covers_under_either_spelling(
        self, tmp_path: Path, approved: dict[str, Any], changed: dict[str, Any]
    ) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        address = {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [{"field": "runnables"}, {"member": "ExampleRun"}, {"field": "events"}, {"member": changed}],
        }
        scope_address = {**address, "path": [*address["path"][:-1], {"member": approved}]}
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": fixtures.current_baseline_token(project),
                "intent": "Remove the timing event, addressed with and without its default field.",
                "operations": [{"operation": "remove", "address": address}],
            },
            project=project,
            approved_scope=[{"operation": "remove", "address": scope_address}],
        )
        assert response["status"] == "staged", response
        (entry,) = response["delta"]
        # The delta's member step is the reduced identity, its value the full one.
        assert entry["address"]["path"][-1] == {"member": PLAIN}
        assert entry["before"] == FULL


# ---------------------------------------------------------------------------
# No committed address changes
# ---------------------------------------------------------------------------


def _addresses(document: Any) -> list[dict[str, Any]]:
    """Every object of *document* shaped like ``$defs/address`` with a path."""
    found: list[dict[str, Any]] = []
    if isinstance(document, dict):
        if set(document) <= {"layer", "element", "path"} and isinstance(document.get("path"), list):
            found.append(document)
        for value in document.values():
            found.extend(_addresses(value))
    elif isinstance(document, list):
        for item in document:
            found.extend(_addresses(item))
    return found


def _spelled_out_selectors(original: dict[str, Any], reduced: dict[str, Any]) -> int:
    """How many member steps of *reduced* differ from *original* by an absent selector spelled ``null``.

    The same pass that reduces a whole-value identity spells out, as ``null``, an
    optional identity field a composite member step leaves out. Some vectors use that
    two-key spelling on purpose, so those steps are the one sanctioned difference;
    anything else a reduction changes still fails.
    """
    assert len(original.get("path", [])) == len(reduced.get("path", [])), (original, reduced)
    spelled = 0
    path = original.get("path", [])
    for index, (before, after) in enumerate(zip(path, reduced.get("path", []))):
        if before == after:
            continue
        member, canonical = before.get("member"), after.get("member")
        assert isinstance(member, dict) and isinstance(canonical, dict), (before, after)
        # The step's own collection must be a composite one, and every key the
        # reduction adds one of that collection's declared-optional identity fields.
        collection = collection_at(original["layer"], path[:index])
        assert collection is not None and collection.member_identity == "composite", (original, index)
        added = set(canonical) - set(member)
        assert added and added <= set(collection.optional_identity_fields), (before, after)
        assert all(canonical[name] is None for name in added), (before, after)
        assert {name: canonical[name] for name in member} == member, (before, after)
        assert list(canonical) == list(collection.identity_fields), (before, after)
        spelled += 1
    return spelled


def test_every_committed_vector_address_is_its_own_reduction() -> None:
    """Every committed editing vector's address is its own reduction.

    One difference is sanctioned: a composite member step that leaves out an optional
    identity field, spelled out as ``null`` (:func:`_spelled_out_selectors`); some vectors
    carry that spelling on purpose, so it must occur.
    """
    checked = whole_value_steps = spelled_selectors = 0
    for path in sorted(VECTOR_ROOT.glob("*/*.json"), key=lambda path: path.as_posix()):
        for raw in _addresses(json.loads(path.read_text(encoding="utf-8"))):
            try:
                address = Address.from_json(raw)
            except AddressError:
                # A negative vector's malformed address is the vector's subject;
                # anywhere else an address that does not parse is a finding.
                assert path.parent.name == "negative", (path.name, raw)
                continue
            reduced = reduce_address(address)
            if reduced.to_json() != address.to_json():
                spelled_selectors += _spelled_out_selectors(address.to_json(), reduced.to_json())
            checked += 1
            whole_value_steps += sum(
                1 for step in address.path if isinstance(step, MemberStep) and isinstance(step.member, dict)
            )
    assert checked > 100, checked
    assert whole_value_steps > 0, "no vector spells an object member step, so this proves nothing"
    assert spelled_selectors > 0, "no vector spells a composite member with a selector left out"


def test_the_agent_s_deepest_address_is_its_own_reduction() -> None:
    """The address `packages/agent/tests/test_scope_proposal.py` spells for the deepest proposal it admits.

    Read out of that test's own source, never retyped here: the literal is the agent suite's, and
    a copy would go on passing after the original moved.
    """
    source = (REPO_ROOT / "packages" / "agent" / "tests" / "test_scope_proposal.py").read_text(encoding="utf-8")
    (case,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_the_deepest_address_the_model_admits_is_still_accepted"
    ]
    (assigned,) = [
        node.value
        for node in ast.walk(case)
        if isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets] == ["deepest"]
    ]
    deepest = Address.from_json(ast.literal_eval(assigned)["address"])
    assert any(isinstance(step, MemberStep) and isinstance(step.member, dict) for step in deepest.path)
    assert reduce_address(deepest) == deepest
    assert len(deepest.path) == reg.REGISTRY.max_address_depth == 5


# ---------------------------------------------------------------------------
# Values keep the full form; the depth and the interior
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot(tmp_path: Path) -> Any:
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    model = project / "model"
    component = model / "swc" / "example" / "ExampleSWC.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    document["runnables"][0]["events"].append({"type": "timing", "period": 0.02, "disabled_modes": [MODE]})
    component.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return load_snapshot(str(model))


class TestValuesAndTheInterior:
    def test_a_read_answers_the_full_value(self, snapshot: Any) -> None:
        assert read_value(snapshot, _events_address(PLAIN)) == FULL
        stated = {"type": "timing", "period": 0.02, "disabled_modes": [MODE]}
        assert read_value(snapshot, _events_address(stated)) == {**stated, "name": None}

    def test_the_depth_is_still_five_with_disabled_modes_declared(self) -> None:
        timing = reg.REGISTRY.element_kind("timing_event")
        assert timing.get_field("disabled_modes") is not None
        assert reg.REGISTRY.max_address_depth == 5

    def test_a_whole_value_member_s_collection_field_is_a_leaf(self, snapshot: Any) -> None:
        stated = {"type": "timing", "period": 0.02, "disabled_modes": [MODE]}
        resolved = resolve(_events_address(stated, FieldStep("disabled_modes")), snapshot)
        assert resolved.found
        assert [mode.model_dump() for mode in resolved.value] == [MODE]
        assert resolved.collection is None, "a collection inside a whole-value member has no addressable interior"

    def test_an_address_below_it_is_refused(self, snapshot: Any) -> None:
        stated = {"type": "timing", "period": 0.02, "disabled_modes": [MODE]}
        below = _events_address(stated, FieldStep("disabled_modes"), MemberStep(MODE))
        miss = resolve(below, snapshot)
        assert not miss.found
        assert miss.reason == "not_traversable"
        assert miss.step_index == 5
