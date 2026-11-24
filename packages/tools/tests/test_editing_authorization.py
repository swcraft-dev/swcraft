"""The two authorization gates and the safety guard's trigger.

1. **Gate 1 and gate 2**, over the operations the request states and the delta
   the staged result produced: *every* uncovered entry is reported, and there is
   no partial approval.
2. **The gates' deliberate asymmetry.** An out-of-scope no-op ``update`` is
   refused by gate 1 while gate 2 would admit it; the in-scope and unscoped
   no-op cases are closed upstream by the ``touched`` rule.
3. **The one import boundary**, read off the import graph of
   ``editing/safety_guard.py``: the four classifiers, their types and their error
   are imported from ``authorization.py``, never forked, and nothing else is.
4. **Safety-guard parity.** The address predicate fires on exactly the changes
   ``classify_safety_act`` fires on, in both directions, and the delta route's
   verdict equals the call route's.

The parity elements are read out of a real snapshot, because the classifier
compares against model objects and a hand-made literal would let this file
agree with itself while disagreeing with the reader.
"""

from __future__ import annotations

import ast
import json
import shutil
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import REPO_ROOT, SAFETY_MODEL_ROOT, write_model_element
from rename_pairing_support import guarded_element_renames, spy_on_the_two_pairings, staged_renames_of
from swcraft_tools import authorization as legacy_authorization
from swcraft_tools.authorization import SafetyVerdict
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import safety_guard
from swcraft_tools.editing.address import Address, FieldStep, KeyStep, MemberStep, ScopeEntry
from swcraft_tools.editing.authorization import (
    HARDWARE_LAYER,
    SAFETY_FIELDS,
    SAFETY_MEMBER_FIELDS,
    UNAUTHORIZED_DELTA,
    ApprovalDecision,
    ApprovalMismatchError,
    gate_delta,
    gate_requested_operations,
    parse_approved_scope,
)
from swcraft_tools.editing.delta import project_delta, summarize_removed_elements
from swcraft_tools.editing.protocol import (
    CreateOperation,
    DeltaEntry,
    RemoveOperation,
    UpdateOperation,
)
from swcraft_tools.editing.safety_guard import is_safety_relevant, safety_verdict
from swcraft_tools.schema import (
    Composition,
    Core,
    DataElement,
    DataTypeMap,
    Hardware,
    MappingSet,
    Memory,
    Partition,
    Port,
    Processor,
    Requirement,
    SRInterface,
    SWComponent,
    Task,
)
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import (
    SafetyReading,
    check_cross_layer_consistency,
    hardware_capability_records,
    safety_reading,
)

# Helpers

GUARD_MODULE = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "editing" / "safety_guard.py"


def _step(step: str | dict[str, Any]) -> Any:
    """One terse path step: a string is a field, ``{key}`` a map key, ``{member}`` a list member."""
    if isinstance(step, str):
        return FieldStep(step)
    if "member" in step:
        return MemberStep(step["member"])
    return KeyStep(step["key"])


def _address(layer: str, element: str | None = None, *path: str | dict[str, Any]) -> Address:
    """``{layer, element?, path?}`` from a terse spelling: a string is a field."""
    return Address(layer=layer, element=element, path=tuple(_step(step) for step in path))


def _scope(*entries: tuple[str, Address]) -> tuple[ScopeEntry, ...]:
    return tuple(ScopeEntry(operation=operation, address=address) for operation, address in entries)


def _entry(operation: str, address: Address, *, before: Any = None, after: Any = None) -> DeltaEntry:
    return DeltaEntry(operation=operation, address=address, before=before, after=after)


#: `rule:safety.component-below-requirements`, spelled once for the class that
#: measures it going quiet.
RULE_BELOW_REQUIREMENTS = "safety.component-below-requirements"

SWC = _address("swc", "ExampleSWC")
SWC_DESCRIPTION = _address("swc", "ExampleSWC", "description")
CONNECTOR = _address("connectors", "Example_to_Consumer")

#: The address one admitted core has: the field, then the member's own value
#: (``accessible`` is a ``whole_value`` collection). Asserted against the
#: projection by
#: :meth:`TestTheAdmissionsDeltaShape.test_admitting_a_core_is_a_create_at_the_members_own_address`.
ACCESSIBLE_MEMBER = _address(HARDWARE_LAYER, "RAM0", "accessible", {"member": "CORE1"})

#: The vector whose seed declares ``CORE1`` beside ``CORE0``, so a widening of
#: ``RAM0.accessible`` is legal rather than ``hardware.accessible-undeclared-core``.
ACCESSIBLE_VECTOR = "update-memory-accessible-and-threshold"


def _snapshot(hardware: Iterable[Hardware] = (), **layers: Any) -> ModelSnapshot:
    """A baseline snapshot carrying *hardware* and whatever else a case declares."""
    empty: dict[str, Any] = {
        name: []
        for name in (
            "compositions",
            "swcs",
            "interfaces",
            "implementation_types",
            "connectors",
            "requirements",
            "tasks",
            "sequences",
            "mapping_sets",
            "base_types",
            "application_types",
            "units",
            "compu_methods",
            "data_constraints",
        )
    }
    return ModelSnapshot(**{**empty, **layers, "hardware": hardware})  # type: ignore[arg-type]


def _requirement(requirement_id: str, *, asil: str | None = None) -> Requirement:
    """A baseline requirement, for the two shapes that resolve one.

    The guard reads a requirement's own level, and an unlevelled one allocates
    nothing, so a case expecting a verdict puts the requirement in the baseline.
    """
    return Requirement(
        id=requirement_id,
        title=f"{requirement_id} title",
        description=f"The system shall satisfy {requirement_id}.",
        asil=asil,
        version=1,
        source="architecture",
    )


def _component(
    name: str, *, asil: str | None = None, ports: Iterable[Port] = (), satisfies: Iterable[str] = ()
) -> SWComponent:
    """A baseline component carrying its links in *satisfies*.

    A link on no declared component allocates nothing, so an unlink case puts
    the component in the baseline.
    """
    return SWComponent(name=name, type="application", asil=asil, ports=list(ports), satisfies=list(satisfies))


#: The committed demo project's model root, which has not adopted ASIL: no
#: ``asil``, no ``measures`` and no ``coexistence``, so it is the oracle for the
#: guard's silence rather than a fixture built to be silent.
EXAMPLES_BASIC_MODEL = REPO_ROOT / "examples" / "basic" / "model"


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _root_members(root: Path, mutate: Callable[[list[dict[str, Any]]], None]) -> None:
    """Edit the root composition's ``members``, wherever the root composition is.

    A component added or removed without it leaves a ``check-model`` finding.
    """
    for path in sorted((root / "compositions").glob("*.yaml"), key=lambda p: p.as_posix()):
        document = _read_yaml(path)
        if document.get("root"):
            mutate(document.setdefault("members", []))
            _write_yaml(path, document)
            return
    raise AssertionError(f"no root composition under {root.as_posix()}")


def _write_component(root: Path, document: dict[str, Any]) -> None:
    """Write a new component into ``swc/safety/`` and stand it in the root."""
    _write_yaml(root / "swc" / "safety" / f"{document['name']}.yaml", document)
    _root_members(root, lambda members: members.append({"name": document["name"], "type": document["name"]}))


def _remove_component(name: str) -> Callable[[Path], None]:
    """Delete a component file and withdraw its member from the root composition."""

    def edit(root: Path) -> None:
        for path in (root / "swc").rglob(f"{name}.yaml"):
            path.unlink()
        _root_members(root, lambda members: members.__setitem__(slice(None), [m for m in members if m["name"] != name]))

    return edit


def _remove_file(relative: str) -> Callable[[Path], None]:
    """Delete one element file, named by its POSIX path under the model root."""

    def edit(root: Path) -> None:
        (root / relative).unlink()

    return edit


def _delta_over(tmp_path: Path, source: Path, edit: Callable[[Path], None]) -> tuple[list[DeltaEntry], ModelSnapshot]:
    """The delta *edit* produces over a copy of *source*, and the baseline it is classified against."""
    root = tmp_path / "model"
    shutil.copytree(source, root)
    baseline = load_snapshot(str(root))
    edit(root)
    staged = load_snapshot(str(root))
    delta = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
    return delta, baseline, staged


def _safety_fixture_delta(
    tmp_path: Path, edit: Callable[[Path], None]
) -> tuple[list[DeltaEntry], ModelSnapshot, ModelSnapshot]:
    """:func:`_delta_over` against the committed coexistence fixture."""
    return _delta_over(tmp_path, SAFETY_MODEL_ROOT, edit)


def _interface(name: str, **levels: str | None) -> SRInterface:
    """A sender-receiver interface declaring one ``uint8`` element per keyword, valued its ``asil``."""
    return SRInterface(
        name=name,
        kind="sender_receiver",
        data_elements=[
            DataElement(name=element, implementation_type="uint8", asil=level) for element, level in levels.items()
        ],
    )


def _port(name: str, direction: str, interface: str, measures: dict[str, str] | None = None) -> Port:
    """One port of a hand-built component."""
    return Port(name=name, direction=direction, interface=interface, measures=dict(measures or {}))


def _task(name: str, *, coexistence: str = "", runnables: Iterable[dict[str, str]] = ()) -> Task:
    """One task of a hand-built model, with or without its argument."""
    return Task(
        name=name,
        period=0.01,
        priority=1,
        partition="PARTITION0",
        wcet="100us",
        runnables=list(runnables),
        coexistence=coexistence,
    )


def _findings_after(
    tmp_path: Path, edit: Callable[[Path], None], prepare: Callable[[Path], None] | None = None
) -> list[str]:
    """The cross-layer rule ids a copy of the safety fixture mints after *prepare*, then *edit*."""
    root = tmp_path / "model"
    shutil.copytree(SAFETY_MODEL_ROOT, root)
    if prepare is not None:
        prepare(root)
    edit(root)
    return [record.rule for record in check_cross_layer_consistency(str(root)).records]


@pytest.fixture(scope="module")
def hardware() -> tuple[Hardware, ...]:
    """The baseline the guard reads: one processor, one core, one memory.

    The demo model's values, stated here: the claim is that two pieces of code
    agree, not about one project's numbers. The processor carries no budget.
    Both budgeted elements declare ``asil_capability: B``, the middle of the
    order, so a raise, a clear and a lowering are all within reach.
    """
    return (
        Processor(name="CPU", kind="processor"),
        Core(name="CORE0", kind="core", threshold=0.7, enforcement="strict", asil_capability="B"),
        Memory(
            name="RAM0",
            kind="memory",
            type="RAM",
            size=262144,
            accessible=["CORE0"],
            threshold=0.8,
            enforcement="strict",
            asil_capability="B",
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real project on disk, with ``CORE1`` declared beside ``CORE0``.

    The admission tests need the delta ``editing/delta.py`` really produces: a
    hand-built entry cannot check its own shape.
    """
    return fixtures.build_fixture(tmp_path / "project", ACCESSIBLE_VECTOR)


def _ram0_delta(
    project_root: Path,
    edit: Callable[[dict[str, Any]], None],
    *,
    prepare: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[DeltaEntry], ModelSnapshot]:
    """The delta *edit* produces over ``RAM0``, and the baseline snapshot it is classified against.

    *prepare* runs before the baseline is taken, so a narrowing or a reordering
    has something to narrow.
    """
    model_root = project_root / "model"
    document_path = model_root / "hardware" / "RAM0.yaml"

    def _write(document: dict[str, Any]) -> None:
        document_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    def _read() -> dict[str, Any]:
        return yaml.safe_load(document_path.read_text(encoding="utf-8"))

    if prepare is not None:
        document = _read()
        prepare(document)
        _write(document)
    baseline = load_snapshot(str(model_root))
    document = _read()
    edit(document)
    _write(document)
    staged = load_snapshot(str(model_root))
    delta = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
    return delta, baseline


def _accessible(*cores: str) -> Callable[[dict[str, Any]], None]:
    """An edit that sets ``RAM0.accessible`` to exactly *cores*, in that order."""

    def edit(document: dict[str, Any]) -> None:
        document["accessible"] = list(cores)

    return edit


# The scope itself


class TestApprovedScope:
    """``None`` and ``[]`` are two different statements, and stay two."""

    def test_absent_is_unscoped(self) -> None:
        assert parse_approved_scope(None) is None

    def test_empty_is_nothing_approved(self) -> None:
        # Not `None`: an empty list states that nothing is approved.
        assert parse_approved_scope([]) == ()

    def test_entries_are_parsed_into_the_address_model(self) -> None:
        parsed = parse_approved_scope([{"operation": "update", "address": {"layer": "swc", "element": "ExampleSWC"}}])
        assert parsed == _scope(("update", SWC))


# Gate 1 — step 0, over the operations the request states


class TestGateOne:
    """Over the ``{operation, address}`` list the request states, reading nothing."""

    REMOVE_CONNECTOR = RemoveOperation(address=CONNECTOR)
    REMOVE_SWC = RemoveOperation(address=SWC)

    def test_an_unscoped_run_is_not_gated(self) -> None:
        # Without a scope the user's standing authority over their own files applies.
        assert gate_requested_operations([self.REMOVE_SWC], None) is None

    def test_an_empty_scope_covers_nothing(self) -> None:
        violation = gate_requested_operations([self.REMOVE_CONNECTOR, self.REMOVE_SWC], ())
        assert violation is not None
        assert violation.error == UNAUTHORIZED_DELTA
        assert violation.failure_class == "unauthorized"
        assert violation.addresses == (CONNECTOR, SWC)

    def test_every_uncovered_operation_is_named_by_index(self) -> None:
        # The user declines the change as a whole, so the report names all three.
        violation = gate_requested_operations(
            [
                UpdateOperation(address=SWC_DESCRIPTION, value="a"),
                self.REMOVE_CONNECTOR,
                self.REMOVE_SWC,
            ],
            _scope(("update", SWC)),
        )
        assert violation is not None
        assert "operation 1" in violation.message and "operation 2" in violation.message
        assert "operation 0" not in violation.message
        assert violation.addresses == (CONNECTOR, SWC)

    def test_an_exactly_covered_request_passes(self) -> None:
        scope = _scope(("remove", CONNECTOR), ("remove", SWC))
        assert gate_requested_operations([self.REMOVE_CONNECTOR, self.REMOVE_SWC], scope) is None

    def test_an_update_approval_does_not_authorize_a_remove(self) -> None:
        # The operation is part of the key: "fix the description" must not
        # authorize deleting the element.
        scope = _scope(("update", _address("swc", "ExampleSWC", "description")))
        assert gate_requested_operations([self.REMOVE_SWC], scope) is not None

    def test_a_remove_approval_does_not_authorize_a_create_at_the_same_address(self) -> None:
        assert gate_requested_operations([CreateOperation(address=SWC, value={})], _scope(("remove", SWC))) is not None

    def test_an_omitted_element_is_the_layer_wildcard(self) -> None:
        scope = _scope(("remove", Address(layer="swc")))
        assert gate_requested_operations([self.REMOVE_SWC], scope) is None

    def test_an_omitted_path_covers_every_change_inside_the_element(self) -> None:
        deep = UpdateOperation(address=_address("swc", "ExampleSWC", "memory", {"key": "RAM0"}), value=4096)
        assert gate_requested_operations([deep], _scope(("update", SWC))) is None

    def test_a_path_prefix_covers_and_a_sibling_does_not(self) -> None:
        memory = _address("swc", "ExampleSWC", "memory")
        deep = UpdateOperation(address=_address("swc", "ExampleSWC", "memory", {"key": "RAM0"}), value=4096)
        assert gate_requested_operations([deep], _scope(("update", memory))) is None
        assert gate_requested_operations([deep], _scope(("update", _address("swc", "ExampleSWC", "ports")))) is not None

    def test_a_longer_scope_path_does_not_cover_a_shorter_delta(self) -> None:
        # Approving one map entry is not approving the whole map.
        operation = UpdateOperation(address=_address("swc", "ExampleSWC", "memory"), value={})
        scope = _scope(("update", _address("swc", "ExampleSWC", "memory", {"key": "RAM0"})))
        assert gate_requested_operations([operation], scope) is not None

    def test_a_different_layer_never_covers(self) -> None:
        scope = _scope(("remove", Address(layer="connectors")))
        assert gate_requested_operations([self.REMOVE_SWC], scope) is not None


# Gate 2 — step 5, the authority


class TestGateTwo:
    """Over the delta the staged result actually produced."""

    REMOVED_CONNECTOR = _entry("remove", CONNECTOR, before={"name": "Example_to_Consumer"})
    REMOVED_SWC = _entry("remove", SWC, before={"name": "ExampleSWC"})

    def test_an_unscoped_run_is_not_gated(self) -> None:
        assert gate_delta([self.REMOVED_SWC], None) is None

    def test_an_empty_delta_is_covered_by_an_empty_scope(self) -> None:
        # The no-op change set is closed upstream by the `touched` rule.
        assert gate_delta([], ()) is None

    def test_an_empty_scope_covers_nothing_and_names_every_entry(self) -> None:
        violation = gate_delta([self.REMOVED_CONNECTOR, self.REMOVED_SWC], ())
        assert violation is not None
        assert violation.error == UNAUTHORIZED_DELTA
        assert violation.addresses == (CONNECTOR, SWC)

    def test_there_is_no_partial_approval(self) -> None:
        # The commit boundary applies the reviewed delta whole or not at all.
        violation = gate_delta([self.REMOVED_CONNECTOR, self.REMOVED_SWC], _scope(("remove", CONNECTOR)))
        assert violation is not None
        assert violation.addresses == (SWC,)

    def test_an_unmatched_scope_entry_is_unused_and_not_an_error(self) -> None:
        # Refusing a change set for doing less than approved protects nothing.
        scope = _scope(("remove", SWC), ("create", _address("implementation_types", "uint16")))
        assert gate_delta([self.REMOVED_SWC], scope) is None

    def test_an_exact_nested_address_covers(self) -> None:
        entry = _entry("update", _address("swc", "ExampleSWC", "memory", {"key": "RAM0"}), before=2048, after=4096)
        assert gate_delta([entry], _scope(("update", entry.address))) is None

    def test_a_whole_value_change_needs_both_verbs(self) -> None:
        # A change inside a `whole_value` collection member projects as a remove
        # plus a create, so an `update` approval does not cover it.
        removed = _entry("remove", _address("swc", "ExampleSWC", "satisfies"))
        created = _entry("create", _address("swc", "ExampleSWC", "satisfies"))
        update_only = _scope(("update", _address("swc", "ExampleSWC", "satisfies")))
        assert gate_delta([removed, created], update_only) is not None
        assert gate_delta([removed, created], update_only + _scope(("remove", removed.address))) is not None
        both = update_only + _scope(("remove", removed.address), ("create", created.address))
        assert gate_delta([removed, created], both) is None


class TestTheGatesAreDeliberatelyAsymmetric:
    """An out-of-scope no-op ``update``: gate 1 refuses it, gate 2 would admit it.

    A no-op update can still rewrite a file not in the writer's form, which the
    commit would refuse as an ``UnexplainedChangeError``; gate 1 refusing first
    names the operation instead. The in-scope and unscoped no-op cases are
    closed upstream by the ``touched`` rule.
    """

    NO_OP = UpdateOperation(address=SWC_DESCRIPTION, value="unchanged")
    OUT_OF_SCOPE = _scope(("update", _address("swc", "ExampleSWC", "ports")))

    def test_gate_one_refuses_it(self) -> None:
        violation = gate_requested_operations([self.NO_OP], self.OUT_OF_SCOPE)
        assert violation is not None
        assert violation.addresses == (SWC_DESCRIPTION,)

    def test_gate_two_would_admit_it(self) -> None:
        # A no-op produces no delta entry at all, so gate 2 has nothing to judge.
        assert gate_delta([], self.OUT_OF_SCOPE) is None


# The approval binding


class TestTheApprovalBinding:
    """An approval binds to one change-set id AND one baseline token."""

    DECISION = ApprovalDecision(
        change_set_id="cs-20260816T102241Z-4f19c2", baseline="rev1:9f4c2a7be3d18056", approved=True
    )

    def test_it_binds_to_its_own_change_set_and_baseline(self) -> None:
        self.DECISION.require_binding(self.DECISION.change_set_id, self.DECISION.baseline)
        assert self.DECISION.binds_to(self.DECISION.change_set_id, self.DECISION.baseline)

    def test_another_change_set_is_a_replay(self) -> None:
        with pytest.raises(ApprovalMismatchError, match="binds to one"):
            self.DECISION.require_binding("cs-20260816T102242Z-000000", self.DECISION.baseline)

    def test_a_re_authored_baseline_is_a_replay(self) -> None:
        # A stale change set is never re-based, and its approval goes with it.
        with pytest.raises(ApprovalMismatchError):
            self.DECISION.require_binding(self.DECISION.change_set_id, "rev1:0000000000000000")

    def test_a_declined_decision_is_still_bound(self) -> None:
        declined = ApprovalDecision(self.DECISION.change_set_id, self.DECISION.baseline, approved=False)
        assert declined.binds_to(self.DECISION.change_set_id, self.DECISION.baseline)
        assert declined.approved is False


# The one import boundary, read off the import graph


class TestTheSafetyClassifierIsTheOnlyImport:
    """What ``editing/safety_guard.py`` takes from ``authorization.py``, stated exactly.

    The import is required: the classifier is the one always-on safety-critical
    check, and a fork would be a second, untested copy free to drift.
    """

    #: The classifiers, the types they answer in, and the one error they refuse
    #: with — all this module may take and all it must take; a new classifier
    #: without an edit here would be an unreviewed widening. The layer dispatch
    #: fails closed on a layer with no classifier by raising the classifier's
    #: own error type.
    PERMITTED = frozenset(
        {
            "classify_safety_act",
            "classify_argument_act",
            "classify_allocation_act",
            "classify_trust_act",
            "SafetyChange",
            "SafetyClassificationError",
            "SafetyVerdict",
            "SafetyChangeKind",
        }
    )

    @staticmethod
    @pytest.fixture(scope="class")
    def tree() -> ast.Module:
        return ast.parse(GUARD_MODULE.read_text(encoding="utf-8"))

    @staticmethod
    def _imported_from_the_classifier_module(tree: ast.Module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.authorization":
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools":
                names.update("swcraft_tools.authorization" for alias in node.names if alias.name == "authorization")
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names if alias.name == "swcraft_tools.authorization")
        return names

    def test_the_safety_classifier_is_imported_not_forked(self, tree: ast.Module) -> None:
        # Required rather than tolerated: a fork would be an untested copy.
        assert self._imported_from_the_classifier_module(tree) == self.PERMITTED

    def test_the_whole_module_is_imported_wholesale_nowhere(self, tree: ast.Module) -> None:
        # Either wholesale form would satisfy the name check above while making
        # every remaining name reachable as an attribute.
        assert "swcraft_tools.authorization" not in self._imported_from_the_classifier_module(tree)

    def test_the_imported_names_really_resolve(self) -> None:
        # A renamed symbol would leave the AST assertions passing on a stale name.
        for name in self.PERMITTED:
            assert getattr(safety_guard, name, None) is getattr(legacy_authorization, name)

    def test_the_import_scan_sees_every_form_it_claims_to(self, tmp_path: Path) -> None:
        """Positive control: the scan sees all three import forms in a synthetic module."""
        offender = tmp_path / "forked.py"
        offender.write_text(
            "from swcraft_tools.authorization import classify_safety_act\n"
            "import swcraft_tools.authorization\n"
            "from swcraft_tools import authorization\n",
            encoding="utf-8",
        )

        found = self._imported_from_the_classifier_module(ast.parse(offender.read_text(encoding="utf-8")))

        assert found == {"classify_safety_act", "swcraft_tools.authorization"}


# The safety guard: the address predicate


#: Every field a budgeted hardware element declares, off ``schema.py``, so the
#: "another field" cases are the complement of both safety sets over the real
#: model. The ``processor`` declares no safety field and adds nothing.
HARDWARE_BUDGET_FIELDS: frozenset[str] = frozenset(name for model in (Memory, Core) for name in model.model_fields)


class TestTheTriggerAndTheClassifierShareOneFieldSet:
    """The trigger's two field sets equal the classifier's ``_SAFETY_FIELD_ORDER``.

    They are written down twice, so the equality is asserted: a hardware kind
    added on a new field would otherwise never be asked about in the editing
    path. Does **not** say each field is on the right side of the split;
    :class:`TestTheAddressPredicate` asserts each shape.
    """

    def test_the_two_field_sets_are_the_same_set(self) -> None:
        assert SAFETY_FIELDS | SAFETY_MEMBER_FIELDS == set(legacy_authorization._SAFETY_FIELD_ORDER), (
            "the editing trigger and the classifier disagree about which hardware fields are safety-relevant; "
            "a field only the classifier knows about is a weakening the editing path never asks about"
        )

    def test_the_two_shapes_are_disjoint(self) -> None:
        # A field in both would be triggered on a shape the differ never emits for it.
        assert not SAFETY_FIELDS & SAFETY_MEMBER_FIELDS

    def test_every_safety_field_is_a_field_a_budgeted_element_really_has(self) -> None:
        # Both sides could otherwise agree on a name neither model declares.
        assert SAFETY_FIELDS | SAFETY_MEMBER_FIELDS <= HARDWARE_BUDGET_FIELDS

    def test_accessible_is_the_member_shaped_safety_field(self) -> None:
        """`accessible` is member-shaped, not in `SAFETY_FIELDS`.

        The differ emits an admission as a `create` at the member's address; in
        `SAFETY_FIELDS` it would classify reorderings and miss every widening.
        """
        assert "accessible" in HARDWARE_BUDGET_FIELDS
        assert "accessible" in SAFETY_MEMBER_FIELDS
        assert "accessible" not in SAFETY_FIELDS
        assert not is_safety_relevant("update", _address(HARDWARE_LAYER, "RAM0", "accessible"))
        assert is_safety_relevant("create", ACCESSIBLE_MEMBER)


class TestTheAddressPredicate:
    """The classifier's two call names, re-expressed over addresses."""

    @pytest.mark.parametrize("field", sorted(set(legacy_authorization._SAFETY_FIELD_ORDER) - SAFETY_MEMBER_FIELDS))
    def test_an_update_entering_a_safety_field_fires(self, field: str) -> None:
        # Over the classifier's table, never `SAFETY_FIELDS`, which would be a
        # tautology. The list-shaped fields are subtracted: an `update` at the
        # list is a reordering, which weakens nothing.
        assert is_safety_relevant("update", _address(HARDWARE_LAYER, "RAM0", field))

    @pytest.mark.parametrize("field", sorted(SAFETY_MEMBER_FIELDS))
    def test_a_create_at_a_member_of_a_list_shaped_safety_field_fires(self, field: str) -> None:
        assert is_safety_relevant("create", _address(HARDWARE_LAYER, "RAM0", field, {"member": "CORE1"}))

    @pytest.mark.parametrize("field", sorted(SAFETY_MEMBER_FIELDS))
    def test_a_remove_at_that_member_does_not(self, field: str) -> None:
        # Withdrawing a core can only make `hardware.unreachable-memory` refuse
        # more: a tightening, read off the operation.
        assert not is_safety_relevant("remove", _address(HARDWARE_LAYER, "RAM0", field, {"member": "CORE0"}))

    @pytest.mark.parametrize("field", sorted(SAFETY_MEMBER_FIELDS))
    def test_an_update_at_the_list_itself_does_not(self, field: str) -> None:
        # The reordering shape.
        assert not is_safety_relevant("update", _address(HARDWARE_LAYER, "RAM0", field))

    @pytest.mark.parametrize("field", sorted(SAFETY_FIELDS))
    def test_a_create_at_a_member_of_a_scalar_safety_field_does_not(self, field: str) -> None:
        # The member branch is keyed on the field, not on the path's shape.
        assert not is_safety_relevant("create", _address(HARDWARE_LAYER, "RAM0", field, {"member": "0.9"}))

    def test_a_remove_of_a_resource_fires(self) -> None:
        assert is_safety_relevant("remove", _address(HARDWARE_LAYER, "RAM0"))

    @pytest.mark.parametrize("field", sorted(HARDWARE_BUDGET_FIELDS - SAFETY_FIELDS - SAFETY_MEMBER_FIELDS))
    def test_an_update_of_another_field_does_not(self, field: str) -> None:
        # Both safety sets are subtracted.
        assert not is_safety_relevant("update", _address(HARDWARE_LAYER, "RAM0", field))

    def test_a_create_of_an_element_never_fires(self) -> None:
        # Declaring a resource adds a check. A `create` at a member of a
        # list-shaped safety field fires; at the element address it does not.
        assert not is_safety_relevant("create", _address(HARDWARE_LAYER, "NewPool"))

    def test_a_layer_outside_the_table_never_fires(self) -> None:
        """A layer outside :data:`_LAYER_BRANCHES` never fires, in the two operations that fire elsewhere."""
        assert not is_safety_relevant("remove", CONNECTOR)
        assert not is_safety_relevant("create", CONNECTOR)
        assert not is_safety_relevant("update", _address("connectors", "Example_to_Consumer", "description"))

    def test_a_field_no_branch_reads_never_fires_in_a_layer_that_has_one(self) -> None:
        # `tasks` IS in the table; `threshold` is not one of its argument fields.
        assert not is_safety_relevant("update", _address("tasks", "Task_10ms", "threshold"))

    def test_a_layer_wide_address_never_fires(self) -> None:
        # A delta entry never omits `element`, and a classification needs a resource to name.
        assert not is_safety_relevant("remove", Address(layer=HARDWARE_LAYER))

    def test_a_remove_inside_a_resource_is_not_a_removal_of_it(self) -> None:
        assert not is_safety_relevant("remove", _address(HARDWARE_LAYER, "RAM0", "threshold"))


class TestTheCompositionEditsAreNotWeakenings:
    """Composition edits narrow what is wired and widen no permission, so none is a weakening.

    A weakening makes a check that refused a model stop refusing it; every
    composition rule can only refuse more after these. Asserted, because an
    absent branch is what a later widening would silently fill in.

    **Does not reach** whether the edit is a good idea: ``check-model`` and the
    validation passes say that.
    """

    MEMBER = _address("compositions", "Root", "members", {"member": "sensor"})
    DELEGATION = _address("compositions", "Root", "delegations", {"member": "dSpeed"})
    MEMBER_TYPE = _address("compositions", "Root", "members", {"member": "sensor"}, "type")

    def test_removing_a_member_is_not_a_weakening(self) -> None:
        assert not is_safety_relevant("remove", self.MEMBER)

    def test_removing_a_delegation_is_not_a_weakening(self) -> None:
        assert not is_safety_relevant("remove", self.DELEGATION)

    def test_retargeting_a_members_type_is_not_a_weakening(self) -> None:
        assert not is_safety_relevant("update", self.MEMBER_TYPE)

    def test_removing_the_whole_composition_is_not_a_weakening_either(self) -> None:
        # A `remove` at `{hardware, <name>}` fires: the layer decides, not the address shape.
        assert not is_safety_relevant("remove", _address("compositions", "Root"))

    def test_the_classifier_answers_the_delete_rather_than_raising(self) -> None:
        """The classifier answers a composition delete with an empty verdict, not a raise.

        Without the ``DELETE_ELEMENT_LAYERS`` row it raises ``SafetyClassificationError``.
        """
        verdict = legacy_authorization.classify_safety_act(
            "delete_element", {"element_type": "composition", "name": "Root"}, ()
        )

        assert verdict.changes == ()
        assert verdict.is_safety_act is False

    def test_a_composition_declares_none_of_the_fields_the_classifier_compares(self) -> None:
        """A composition declares no safety field, so the answers above are structural."""
        declared = set(Composition.model_fields)

        assert declared & (set(SAFETY_FIELDS) | set(SAFETY_MEMBER_FIELDS)) == set()


class TestTheMappingEditsAreNotWeakenings:
    """Mapping edits are no safety act: a mapping is about generated code, and no safety rule reads it.

    **Does not reach** whether the edit is a good idea: pass D refuses a change
    set leaving ``rule:data-type.application-type-unmapped`` standing.
    """

    SET = _address("mapping_sets", "PhysicalTypes")
    PAIR = _address("mapping_sets", "PhysicalTypes", "maps", {"member": "Speed_Kph"})
    PAIR_TARGET = _address("mapping_sets", "PhysicalTypes", "maps", {"member": "Speed_Kph"}, "implementation")
    SWC_MAPPINGS = _address("swc", "ExampleSWC", "data_type_mappings")
    SWC_MAPPING_ENTRY = _address("swc", "ExampleSWC", "data_type_mappings", {"member": "PhysicalTypes"})

    @pytest.mark.parametrize(
        ("operation", "address"),
        [
            ("create", SET),
            ("remove", SET),
            ("create", PAIR),
            ("remove", PAIR),
            ("update", PAIR_TARGET),
            ("update", SWC_MAPPINGS),
            ("create", SWC_MAPPING_ENTRY),
            ("remove", SWC_MAPPING_ENTRY),
        ],
    )
    def test_no_mapping_edit_is_safety_relevant(self, operation: str, address: Address) -> None:
        assert not is_safety_relevant(operation, address)

    def test_the_layer_is_what_decides_it_and_not_the_address_shape(self) -> None:
        """A ``remove`` at ``{hardware, <name>}`` fires; at ``{mapping_sets, <name>}`` the layer declines."""
        assert is_safety_relevant("remove", _address("hardware", "RAM0"))
        assert not is_safety_relevant("remove", self.SET)

    def test_no_field_of_either_new_kind_is_a_safety_field(self) -> None:
        """Neither the set, a pair nor the component's list declares a safety field."""
        safety = set(SAFETY_FIELDS) | set(SAFETY_MEMBER_FIELDS)
        declared = set(MappingSet.model_fields) | set(DataTypeMap.model_fields) | {"data_type_mappings"}

        assert declared & safety == set()


class TestEveryLevelIsClassifiedWhenItDropsAndNotOtherwise:
    """A level a bearer declares is classified when it drops, and not otherwise.

    A weakening makes a check that refused a model stop refusing it:

    * **lowering a component's level, or raising a requirement's**, can only make
      ``rule:safety.component-below-requirements`` refuse more (where a linked requirement
      declares a level), and pass D refuses a change set leaving such a finding standing;
    * **raising a component's level** closes the finding by strengthening it;
    * **lowering a requirement's level**, or removing a ``satisfies`` member,
      weakens the allocation: ``LOWER_ALLOCATION``.

    The component is a bearer too: other safety rules read its effective level,
    so a declaration the requirements hold up is no drop when withdrawn.

    **Does not reach** whether a level is *right*.
    """

    SWC_ASIL = _address("swc", "ExampleSWC", "asil")
    REQUIREMENT_ASIL = _address("requirements", "REQ_EXAMPLE_001", "asil")

    @pytest.mark.parametrize("operation", ["create", "remove"])
    def test_neither_other_verb_at_a_components_level_fires_the_predicate(self, operation: str) -> None:
        """A ``create`` (a first declaration) or ``remove`` at a component's level does not fire.

        Clearing a scalar is an ``update`` to ``None``, never a ``remove``.
        """
        assert not is_safety_relevant(operation, self.SWC_ASIL)

    def test_an_update_at_a_components_level_fires_the_predicate(self) -> None:
        assert is_safety_relevant("update", self.SWC_ASIL)

    @pytest.mark.parametrize("operation", ["create", "remove"])
    def test_only_an_update_at_a_requirements_level_fires_the_predicate(self, operation: str) -> None:
        """A ``create`` or ``remove`` at a requirement's level does not fire; an ``update`` does.

        A first declaration can only make the rule refuse more; clearing is an
        ``update`` to ``None``.
        """
        assert not is_safety_relevant(operation, self.REQUIREMENT_ASIL)

    def test_an_update_at_a_requirements_level_fires_the_predicate(self) -> None:
        assert is_safety_relevant("update", self.REQUIREMENT_ASIL)

    def test_removing_the_bearer_whole_fires_in_every_layer_that_has_one(self) -> None:
        """Removing a bearer whole fires in every bearer-carrying layer: same outcome as its level cleared.

        Firing is not a verdict: :class:`TestRemovingABearerWholeWithdrawsWhatItCarried`
        holds what each removal withdraws.
        """
        for layer, element in (
            ("requirements", "REQ_EXAMPLE_001"),
            ("swc", "ExampleSWC"),
            ("tasks", "Task_10ms"),
            ("interfaces", "ISpeed"),
        ):
            assert is_safety_relevant("remove", _address(layer, element)), layer
        # A layer with no bearer in it stays outside, in the same operation.
        assert not is_safety_relevant("remove", CONNECTOR)

    def test_an_unlevelled_requirement_removed_whole_is_no_weakening(self, hardware: tuple[Hardware, ...]) -> None:
        """An unlevelled requirement removed whole withdraws nothing, so it is no weakening.

        Otherwise every requirement deletion on a project without ASIL would ask
        a second review question. The baseline holds the requirement and the
        component, because the removal runs the unlink's maximum test, which
        resolves both.
        """
        removed = _address("requirements", "REQ_1")
        levelled = [_entry("remove", removed, before={"id": "REQ_1", "asil": "D"})]
        unlevelled = [_entry("remove", removed, before={"id": "REQ_1"})]
        linked = [_component("Ctl", satisfies=["REQ_1@1"])]
        held = _snapshot(hardware, swcs=linked, requirements=[_requirement("REQ_1", asil="D")])
        unadopted = _snapshot(hardware, swcs=linked, requirements=[_requirement("REQ_1")])

        gone = _snapshot(hardware, swcs=linked)

        assert safety_verdict(levelled, held, staged=gone).is_safety_act is True
        assert safety_verdict(unlevelled, unadopted, staged=gone).is_safety_act is False

    @pytest.mark.parametrize(
        ("before", "after", "weakens"),
        [("D", "QM", True), ("QM", "D", False), (None, "D", False), ("D", None, True)],
        ids=["lowered", "raised", "first-declaration", "cleared"],
    )
    def test_a_components_own_level_is_classified_when_it_DROPS(
        self, hardware: tuple[Hardware, ...], before: str | None, after: str | None, weakens: bool
    ) -> None:
        """The classifier, over a delta carrying the values, in every direction of a component's level.

        Nothing allocates a level to ``ExampleSWC`` here, so a cleared
        declaration is a drop.
        """
        delta = [_entry("update", self.SWC_ASIL, before=before, after=after)]

        verdict = safety_verdict(
            delta,
            _snapshot(hardware, swcs=[_component("ExampleSWC", asil=before)]),
            staged=_snapshot(hardware, swcs=[_component("ExampleSWC", asil=after)]),
        )

        assert verdict.is_safety_act is weakens
        if weakens:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
            assert change.resource == "ExampleSWC"

    @pytest.mark.parametrize(
        ("allocated", "weakens"),
        [("D", False), ("A", True)],
        ids=["the-requirements-hold-it-up", "the-requirements-are-lower"],
    )
    def test_withdrawing_a_components_declaration_is_measured_against_what_it_would_derive(
        self, hardware: tuple[Hardware, ...], allocated: str, weakens: bool
    ) -> None:
        """``D`` -> nothing is a drop only where nothing holds the level up.

        Under an ASIL-``D`` requirement the component derives ``D`` and is rated
        as in the baseline.
        """
        requirement = _requirement("REQ_1", asil=allocated)
        baseline = _snapshot(
            hardware, swcs=[_component("ExampleSWC", asil="D", satisfies=["REQ_1@1"])], requirements=[requirement]
        )
        staged = _snapshot(hardware, swcs=[_component("ExampleSWC", satisfies=["REQ_1@1"])], requirements=[requirement])
        delta = [_entry("update", self.SWC_ASIL, before="D", after=None)]

        assert safety_verdict(delta, baseline, staged=staged).is_safety_act is weakens

    @pytest.mark.parametrize(
        ("before", "after", "weakens"),
        [("D", "QM", True), ("D", None, True), ("QM", "D", False), (None, "D", False)],
        ids=["lowered", "cleared", "raised", "first-declaration"],
    )
    def test_a_requirements_level_is_classified_when_it_DROPS_and_not_otherwise(
        self, hardware: tuple[Hardware, ...], before: str | None, after: str | None, weakens: bool
    ) -> None:
        """The same four directions on the requirement, where two of them are acts."""
        delta = [_entry("update", self.REQUIREMENT_ASIL, before=before, after=after)]

        verdict = safety_verdict(
            delta,
            _snapshot(
                hardware,
                swcs=[_component("Ctl", satisfies=["REQ_EXAMPLE_001@1"])],
                requirements=[_requirement("REQ_EXAMPLE_001", asil=before)],
            ),
            staged=_snapshot(
                hardware,
                swcs=[_component("Ctl", satisfies=["REQ_EXAMPLE_001@1"])],
                requirements=[_requirement("REQ_EXAMPLE_001", asil=after)],
            ),
        )

        assert verdict.is_safety_act is weakens
        if weakens:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
            assert change.resource == "REQ_EXAMPLE_001"
            assert (change.old, change.new) == (before, after)

    def test_lowering_a_requirement_closes_the_finding_and_is_classified_as_a_lowered_allocation(
        self, tmp_path: Path, hardware: tuple[Hardware, ...]
    ) -> None:
        """Lowering a requirement under two `QM` components ends two findings, and is classified.

        The rule's direction lives in the counterparty, which the value cases
        above never move. Both halves are asserted: the rule stops refusing,
        and the classifier answers a lowered allocation.
        """
        from swcraft_tools.validator import check_component_asil

        model_root = tmp_path / "model"
        for name in ("Ctl", "Mon"):
            path = model_root / "swc" / "group" / f"{name}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(
                    {"name": name, "type": "application", "asil": "QM", "satisfies": ["REQ_1@1"]}, sort_keys=False
                ),
                encoding="utf-8",
                newline="\n",
            )
        requirement = model_root / "requirements" / "REQ_1.yaml"
        requirement.parent.mkdir(parents=True, exist_ok=True)

        def write_requirement(level: str) -> None:
            requirement.write_text(
                yaml.safe_dump(
                    {
                        "id": "REQ_1",
                        "title": "REQ_1 title",
                        "description": "The system shall satisfy REQ_1.",
                        "version": 1,
                        "source": "architecture",
                        "asil": level,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
                newline="\n",
            )

        write_requirement("D")
        before = [r for r in check_component_asil(str(model_root)).records if r.rule == RULE_BELOW_REQUIREMENTS]
        write_requirement("QM")
        after = [r for r in check_component_asil(str(model_root)).records if r.rule == RULE_BELOW_REQUIREMENTS]

        assert len(before) == 2, [record.message for record in before]
        assert after == []

        # The components' effective level does not move; what drops is the
        # maximum allocated to them, which the rule compares a declaration against.
        components = [
            _component("Ctl", asil="QM", satisfies=["REQ_1@1"]),
            _component("Mon", asil="QM", satisfies=["REQ_1@1"]),
        ]
        verdict = safety_verdict(
            [_entry("update", _address("requirements", "REQ_1", "asil"), before="D", after="QM")],
            _snapshot(hardware, swcs=components, requirements=[_requirement("REQ_1", asil="D")]),
            staged=_snapshot(hardware, swcs=components, requirements=[_requirement("REQ_1", asil="QM")]),
        )

        (change,) = verdict.changes
        assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
        assert change.resource == "REQ_1"
        assert (change.old, change.new) == ("D", "QM")
        assert verdict.is_safety_act is True

    def test_removing_a_satisfies_member_is_the_one_classified_removal_inside_an_element(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """Removing a ``satisfies`` member is the one classified removal inside an element.

        It withdraws an allocation, so it is keyed on the field; every other
        removal inside an element narrows.
        """
        removed = _address("swc", "Ctl", "satisfies", {"member": "REQ_1@1"})
        kept = _address("swc", "Ctl", "description")

        assert is_safety_relevant("remove", removed)
        assert not is_safety_relevant("remove", kept)
        assert not is_safety_relevant("remove", _address("requirements", "REQ_1", "description"))
        assert not is_safety_relevant("create", removed), "admitting a link allocates MORE"

        baseline = _snapshot(
            hardware, requirements=[_requirement("REQ_1", asil="D")], swcs=[_component("Ctl", satisfies=["REQ_1@1"])]
        )
        staged = _snapshot(hardware, requirements=[_requirement("REQ_1", asil="D")], swcs=[_component("Ctl")])
        (change,) = safety_verdict([_entry("remove", removed, before="REQ_1@1")], baseline, staged=staged).changes

        assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
        assert (change.resource, change.field, change.old, change.new) == ("Ctl", "satisfies", "REQ_1@1", None)

    @pytest.mark.parametrize(
        ("allocated", "weakens"),
        [("D", True), (None, False)],
        ids=["levelled", "unlevelled"],
    )
    def test_an_unlink_is_measured_against_the_requirements_own_level(
        self, hardware: tuple[Hardware, ...], allocated: str | None, weakens: bool
    ) -> None:
        """An unlevelled requirement allocates nothing, so unlinking one takes nothing.

        An unlink on a project without ASIL raises no second review question
        naming a level the requirement does not have.
        """
        removed = _address("swc", "Ctl", "satisfies", {"member": "REQ_1@1"})
        baseline = _snapshot(
            hardware,
            requirements=[_requirement("REQ_1", asil=allocated)],
            swcs=[_component("Ctl", satisfies=["REQ_1@1"])],
        )
        staged = _snapshot(hardware, requirements=[_requirement("REQ_1", asil=allocated)], swcs=[_component("Ctl")])

        assert (
            safety_verdict([_entry("remove", removed, before="REQ_1@1")], baseline, staged=staged).is_safety_act
            is weakens
        )

    def test_a_requirement_the_baseline_does_not_carry_allocates_nothing(self, hardware: tuple[Hardware, ...]) -> None:
        """A requirement absent from the baseline allocates nothing: an answer, not a raise.

        Fail-closed stops at ``hardware``, whose classifier needs the element's
        current values.
        """
        removed = _address("swc", "Ctl", "satisfies", {"member": "REQ_ABSENT@1"})
        neither = _snapshot(hardware, swcs=[_component("Ctl", satisfies=["REQ_ABSENT@1"])])

        assert safety_verdict([_entry("remove", removed, before="REQ_ABSENT@1")], neither, staged=neither).changes == ()

    def test_neither_kind_declares_a_field_the_classifier_compares(self) -> None:
        """Neither a component nor a requirement declares a budget safety field; ``asil`` is an allocation field."""
        assert "asil" in SWComponent.model_fields
        assert "asil" in Requirement.model_fields
        assert "asil" not in (set(SAFETY_FIELDS) | set(SAFETY_MEMBER_FIELDS))
        for model in (SWComponent, Requirement):
            assert set(model.model_fields) & (set(SAFETY_FIELDS) | set(SAFETY_MEMBER_FIELDS)) == set()

    def test_the_budget_guard_sets_are_unmoved_by_the_asil_fields(self) -> None:
        """The hardware guard's two field sets hold no ``asil`` field.

        ``asil_capability`` is the level a core or memory may host (ISO 26262-1's
        *ASIL capability*), not the level a bearer carries.
        """
        assert set(SAFETY_FIELDS) == {"threshold", "size", "enforcement", "asil_capability"}
        assert "asil" not in SAFETY_FIELDS
        assert set(SAFETY_MEMBER_FIELDS) == {"accessible"}


# Safety-guard parity: the trigger against the classifier it feeds


class Candidate:
    """One change, spelled both ways: as a classifier call and as a delta.

    ``weakening`` and ``tightening`` are its two directions, so the trigger
    question needs no statement of which direction weakens.
    """

    def __init__(
        self,
        label: str,
        tool: str,
        weakening_args: dict[str, Any],
        tightening_args: dict[str, Any],
        weakening_delta: list[DeltaEntry],
        tightening_delta: list[DeltaEntry],
    ) -> None:
        self.label = label
        self.tool = tool
        self.weakening_args = weakening_args
        self.tightening_args = tightening_args
        self.weakening_delta = weakening_delta
        self.tightening_delta = tightening_delta

    def __repr__(self) -> str:  # pragma: no cover - pytest ids only
        return self.label


def _resource_update(name: str, field: str, before: Any, after: Any) -> list[DeltaEntry]:
    return [_entry("update", _address(HARDWARE_LAYER, name, field), before=before, after=after)]


def _member_change(name: str, field: str, before: list[str], after: list[str]) -> list[DeltaEntry]:
    """The entries the differ emits for a ``whole_value`` list: one per member out, then in.

    A member is addressed by its own value; a departure is a ``remove``, an
    admission a ``create``. :class:`TestTheAdmissionsDeltaShape` holds this to
    the projection.
    """
    entries = [
        _entry("remove", _address(HARDWARE_LAYER, name, field, {"member": member}), before=member)
        for member in before
        if member not in after
    ]
    entries.extend(
        _entry("create", _address(HARDWARE_LAYER, name, field, {"member": member}), after=member)
        for member in after
        if member not in before
    )
    return entries


#: Per safety field, the ``(resource, baseline, weakening, tightening)`` rows the
#: candidates are generated from, keyed by field so a new field fails
#: :meth:`TestSafetyGuardParity.test_every_safety_field_has_candidate_rows` by
#: name. ``size`` has one row (a ``core`` has no size); ``accessible`` one (only
#: a ``memory`` declares it), narrowing to the empty list from one core. A
#: cleared ``asil_capability`` refuses nothing, so ``None`` is a weakening.
SAFETY_FIELD_ROWS: dict[str, tuple[tuple[str, Any, Any, Any], ...]] = {
    "threshold": (("RAM0", 0.8, 0.95, 0.5), ("CORE0", 0.7, 0.95, 0.4)),
    "size": (("RAM0", 262144, 524288, 131072),),
    "enforcement": (("RAM0", "strict", "advisory", "strict"), ("CORE0", "strict", "advisory", "strict")),
    "accessible": (("RAM0", ["CORE0"], ["CORE0", "CORE1"], []),),
    "asil_capability": (("CORE0", "B", "D", "A"), ("RAM0", "B", None, "QM")),
}


def _safety_field_candidates() -> list[Candidate]:
    """One candidate per ``(safety field, resource)`` row, driven by ``_SAFETY_FIELD_ORDER``.

    A field with no row yields no candidate; the row test reports it by name.
    """
    candidates: list[Candidate] = []
    for field in legacy_authorization._SAFETY_FIELD_ORDER:
        rows = SAFETY_FIELD_ROWS.get(field, ())
        # The builder follows the field's shape, read off `SAFETY_MEMBER_FIELDS`.
        build = _member_change if field in SAFETY_MEMBER_FIELDS else _resource_update
        for resource, baseline, weakening, tightening in rows:
            candidates.append(
                Candidate(
                    f"{resource}-{field}",
                    "update_resource",
                    {"name": resource, field: weakening},
                    {"name": resource, field: tightening},
                    build(resource, field, baseline, weakening),
                    build(resource, field, baseline, tightening),
                )
            )
    return candidates


#: Every axis the trigger could get wrong: each safety field on each budgeted
#: kind (generated), a non-safety field of a memory, a removal of a memory, of a
#: core, of the processor and of something else, and a create.
CANDIDATES = [
    *_safety_field_candidates(),
    Candidate(
        "memory-description",
        "update_resource",
        {"name": "RAM0", "description": "changed"},
        {"name": "RAM0", "description": "reverted"},
        _resource_update("RAM0", "description", "old", "changed"),
        _resource_update("RAM0", "description", "old", "reverted"),
    ),
    Candidate(
        "remove-memory",
        "delete_element",
        {"element_type": "hardware", "name": "RAM0"},
        {"element_type": "hardware", "name": "RAM0"},
        [_entry("remove", _address(HARDWARE_LAYER, "RAM0"), before={"name": "RAM0"})],
        [_entry("remove", _address(HARDWARE_LAYER, "RAM0"), before={"name": "RAM0"})],
    ),
    Candidate(
        "remove-core",
        "delete_element",
        {"element_type": "hardware", "name": "CORE0"},
        {"element_type": "hardware", "name": "CORE0"},
        [_entry("remove", _address(HARDWARE_LAYER, "CORE0"), before={"name": "CORE0"})],
        [_entry("remove", _address(HARDWARE_LAYER, "CORE0"), before={"name": "CORE0"})],
    ),
    # A processor carries no budget. The predicate fires on it (an address has
    # no kind) and the classifier answers an empty verdict: the one row where
    # the two readings of "fires" disagree, asserted separately below.
    Candidate(
        "remove-processor",
        "delete_element",
        {"element_type": "hardware", "name": "CPU"},
        {"element_type": "hardware", "name": "CPU"},
        [_entry("remove", _address(HARDWARE_LAYER, "CPU"), before={"name": "CPU"})],
        [_entry("remove", _address(HARDWARE_LAYER, "CPU"), before={"name": "CPU"})],
    ),
    Candidate(
        "remove-swc",
        "delete_element",
        {"element_type": "swc", "name": "ExampleSWC"},
        {"element_type": "swc", "name": "ExampleSWC"},
        [_entry("remove", SWC, before={"name": "ExampleSWC"})],
        [_entry("remove", SWC, before={"name": "ExampleSWC"})],
    ),
    Candidate(
        "create-memory",
        "add_resource",
        {"name": "ROM", "kind": "memory", "size": 4096, "threshold": 0.9},
        {"name": "ROM", "kind": "memory", "size": 4096, "threshold": 0.1},
        [_entry("create", _address(HARDWARE_LAYER, "ROM"), after={"name": "ROM"})],
        [_entry("create", _address(HARDWARE_LAYER, "ROM"), after={"name": "ROM"})],
    ),
    Candidate(
        "update-unrelated-element",
        "set_swc_description",
        {"name": "ExampleSWC", "description": "changed"},
        {"name": "ExampleSWC", "description": "reverted"},
        [_entry("update", SWC_DESCRIPTION, before="old", after="changed")],
        [_entry("update", SWC_DESCRIPTION, before="old", after="reverted")],
    ),
]


class TestSafetyGuardParity:
    """The trigger fires on exactly what the classifier classifies, both ways.

    * **trigger parity** — some direction of a change yields a non-empty verdict
      exactly when the address predicate fires on it;
    * **verdict parity** — the delta route and the call route produce the same
      ``SafetyChange`` tuple, so the directions are the imported ones.
    """

    @staticmethod
    def _classifier_fires(candidate: Candidate, hardware: tuple[Hardware, ...]) -> bool:
        return any(
            legacy_authorization.classify_safety_act(candidate.tool, args, hardware).is_safety_act
            for args in (candidate.weakening_args, candidate.tightening_args)
        )

    @staticmethod
    def _predicate_fires(delta: list[DeltaEntry]) -> bool:
        return any(is_safety_relevant(entry.operation, entry.address) for entry in delta)

    #: The candidates where the two readings of "fires" disagree, named so each
    #: is a row, not a hole in the claim. ``remove-processor``: an address has no
    #: kind, and the classifier finds no budget. ``remove-swc``: a component
    #: removed whole may withdraw a level, but ``classify_safety_act`` is the
    #: hardware classifier; this row's ``before`` carries no level and no port,
    #: so the guard answers empty too.
    PREDICATE_FIRES_WITHOUT_A_VERDICT = {"remove-processor", "remove-swc"}

    @pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c.label)
    def test_trigger_parity(self, candidate: Candidate, hardware: tuple[Hardware, ...]) -> None:
        classifier = self._classifier_fires(candidate, hardware)
        predicate = self._predicate_fires(candidate.weakening_delta)
        if candidate.label in self.PREDICATE_FIRES_WITHOUT_A_VERDICT:
            assert predicate and not classifier, (
                f"{candidate.label}: this row exists because the predicate fires and the classifier "
                "returns an empty verdict; if either half changed, the exception is no longer one"
            )
            return
        assert predicate == classifier, (
            f"{candidate.label}: the address predicate says {predicate} and the classifier says "
            f"{classifier}. The trigger must not drift from the classifier it feeds."
        )

    def test_the_exceptions_are_the_reads_this_classifier_cannot_make(self, hardware: tuple[Hardware, ...]) -> None:
        """Firing is not a verdict: the two exception rows yield no weakening through :func:`safety_verdict`.

        The predicate is address-shaped and reads no model, so "worth
        classifying" is wider than "weakens something".
        """
        exceptions = {
            c.label
            for c in CANDIDATES
            if self._predicate_fires(c.weakening_delta) and not self._classifier_fires(c, hardware)
        }
        assert exceptions == self.PREDICATE_FIRES_WITHOUT_A_VERDICT
        for candidate in CANDIDATES:
            if candidate.label in self.PREDICATE_FIRES_WITHOUT_A_VERDICT:
                assert (
                    safety_verdict(candidate.weakening_delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
                    == ()
                ), candidate.label

    #: Candidates whose direction is in the operation: a list member is admitted
    #: by a ``create`` and withdrawn by a ``remove``. A scalar field's directions
    #: are both an ``update``, which only the classifier tells apart.
    DIRECTION_IS_IN_THE_OPERATION = {f"RAM0-{field}" for field in SAFETY_MEMBER_FIELDS}

    @pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c.label)
    def test_the_predicate_is_direction_independent(self, candidate: Candidate) -> None:
        # The predicate asks "is this worth classifying", not "does this weaken".
        if candidate.label in self.DIRECTION_IS_IN_THE_OPERATION:
            assert self._predicate_fires(candidate.weakening_delta), (
                f"{candidate.label}: an admitted member is the widening, and the predicate must fire on it"
            )
            assert not self._predicate_fires(candidate.tightening_delta), (
                f"{candidate.label}: a withdrawn member is a tightening, and the predicate reads that "
                "off the operation rather than asking the classifier"
            )
            return
        assert self._predicate_fires(candidate.weakening_delta) == self._predicate_fires(candidate.tightening_delta)

    def test_every_member_shaped_candidate_is_declared_asymmetric(self) -> None:
        labels = {c.label for c in CANDIDATES}
        assert self.DIRECTION_IS_IN_THE_OPERATION <= labels
        assert self.DIRECTION_IS_IN_THE_OPERATION == {
            c.label for c in CANDIDATES if c.label.rsplit("-", 1)[1] in SAFETY_MEMBER_FIELDS
        }

    @pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c.label)
    @pytest.mark.parametrize("direction", ["weakening", "tightening"])
    def test_verdict_parity(self, candidate: Candidate, direction: str, hardware: tuple[Hardware, ...]) -> None:
        args = getattr(candidate, f"{direction}_args")
        delta = getattr(candidate, f"{direction}_delta")
        expected = legacy_authorization.classify_safety_act(candidate.tool, args, hardware).changes
        assert safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes == expected

    def test_every_safety_field_has_candidate_rows(self) -> None:
        assert set(SAFETY_FIELD_ROWS) == set(legacy_authorization._SAFETY_FIELD_ORDER), (
            "a safety field has no candidate row, so no parity case exercises it"
        )
        exercised = {label.split("-", 1)[1] for label in (c.label for c in CANDIDATES if c.tool == "update_resource")}
        assert set(legacy_authorization._SAFETY_FIELD_ORDER) <= exercised

    def test_at_least_one_candidate_fires_and_one_does_not(self, hardware: tuple[Hardware, ...]) -> None:
        # Otherwise parity would hold for a predicate answering a constant.
        fires = {c.label for c in CANDIDATES if self._classifier_fires(c, hardware)}
        assert fires and fires != {c.label for c in CANDIDATES}


class TestTheActsOverTheHardwareLayer:
    """Each hardware weakening kind, named: parity would pass if both routes agreed on a wrong kind.

    `WEAKEN_ENFORCEMENT` is asserted by kind in `test_authorization_safety.py`.
    """

    def test_raising_a_cores_threshold_is_a_raise_threshold(self, hardware: tuple[Hardware, ...]) -> None:
        delta = _resource_update("CORE0", "threshold", 0.7, 0.9)
        (change,) = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.RAISE_THRESHOLD
        assert (change.resource, change.field, change.old, change.new) == ("CORE0", "threshold", 0.7, 0.9)

    def test_enlarging_a_memorys_size_is_an_enlarge_pool(self, hardware: tuple[Hardware, ...]) -> None:
        delta = _resource_update("RAM0", "size", 262144, 524288)
        (change,) = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.ENLARGE_POOL
        assert (change.resource, change.field, change.old, change.new) == ("RAM0", "size", 262144, 524288)

    def test_removing_a_memory_is_a_delete_resource(self, hardware: tuple[Hardware, ...]) -> None:
        delta = [_entry("remove", _address(HARDWARE_LAYER, "RAM0"), before={"name": "RAM0"})]
        (change,) = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.DELETE_RESOURCE
        # A rated element's removal takes its capability with it, named last.
        assert change.old == "kind=memory, size=262144, threshold=0.8, enforcement=strict, asil_capability=B"

    def test_admitting_a_core_to_a_memorys_accessible_list_is_a_widen_access(self, project: Path) -> None:
        """Admitting a core makes `hardware.unreachable-memory` stop refusing, so it is a weakening.

        Run over the delta `editing/delta.py` really produces; a narrowing is not
        an act.
        """
        widening, baseline = _ram0_delta(project, _accessible("CORE0", "CORE1"))
        (change,) = safety_verdict(widening, baseline, staged=baseline).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.WIDEN_ACCESS
        assert (change.resource, change.field) == ("RAM0", "accessible")
        assert (change.old, change.new) == (["CORE0"], ["CORE0", "CORE1"])
        assert change.describe().startswith("hardware element 'RAM0': accessible admits CORE1 — ")

    def test_withdrawing_a_core_from_it_is_not(self, project: Path) -> None:
        """Withdrawing a core can only make `hardware.unreachable-memory` refuse more."""
        narrowing, baseline = _ram0_delta(
            project,
            _accessible("CORE0"),
            prepare=_accessible("CORE0", "CORE1"),
        )
        assert [entry.operation for entry in narrowing] == ["remove"]
        assert not any(is_safety_relevant(entry.operation, entry.address) for entry in narrowing)
        assert safety_verdict(narrowing, baseline, staged=baseline).changes == ()

    # `asil_capability`

    @pytest.mark.parametrize("element", ["CORE0", "RAM0"])
    def test_an_update_entering_a_capability_fires_on_a_core_and_on_a_memory(self, element: str) -> None:
        assert is_safety_relevant("update", _address(HARDWARE_LAYER, element, "asil_capability"))

    def test_raising_a_cores_capability_is_a_raise_capability(self, hardware: tuple[Hardware, ...]) -> None:
        delta = _resource_update("CORE0", "asil_capability", "B", "D")
        (change,) = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.RAISE_CAPABILITY
        assert (change.resource, change.field, change.old, change.new) == ("CORE0", "asil_capability", "B", "D")

    def test_clearing_a_memorys_capability_below_d_is_a_raise_capability(self, hardware: tuple[Hardware, ...]) -> None:
        """`B -> None` reaches the classifier as `{"asil_capability": None}`, key present: a clear.

        For the budget fields that `None` would mean "leave alone".
        """
        delta = _resource_update("RAM0", "asil_capability", "B", None)
        (change,) = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.RAISE_CAPABILITY
        assert (change.resource, change.old, change.new) == ("RAM0", "B", None)

    @pytest.mark.parametrize(
        ("baseline_level", "staged_level"),
        [(None, "A"), ("D", None), ("C", "A"), (None, "D"), ("B", "B")],
        ids=["declared-where-none-stood", "D-cleared", "lowered", "D-declared-where-none-stood", "rewritten"],
    )
    def test_the_moves_that_refuse_no_less_are_not_asked_about(
        self, baseline_level: str | None, staged_level: str | None
    ) -> None:
        hardware = (
            Processor(name="CPU", kind="processor"),
            Core(name="CORE0", kind="core", threshold=0.7, enforcement="strict", asil_capability=baseline_level),
        )
        delta = _resource_update("CORE0", "asil_capability", baseline_level, staged_level)

        assert is_safety_relevant(delta[0].operation, delta[0].address), "firing is not a verdict"
        assert safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes == ()

    def test_creating_a_rated_core_is_not_asked_about(self, hardware: tuple[Hardware, ...]) -> None:
        # A `create` of an element is not in the hardware predicate.
        delta = [
            _entry(
                "create",
                _address(HARDWARE_LAYER, "CORE9"),
                after={"name": "CORE9", "kind": "core", "threshold": 0.7, "asil_capability": "D"},
            )
        ]
        assert not is_safety_relevant(delta[0].operation, delta[0].address)
        assert safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes == ()

    def test_a_capability_on_the_processor_answers_an_empty_verdict(self, hardware: tuple[Hardware, ...]) -> None:
        # An address carries no kind; the classifier reads it.
        delta = _resource_update("CPU", "asil_capability", None, "D")
        assert is_safety_relevant(delta[0].operation, delta[0].address)
        assert safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes == ()

    def test_a_capability_outside_the_five_levels_fails_closed(self, hardware: tuple[Hardware, ...]) -> None:
        delta = _resource_update("CORE0", "asil_capability", "B", "E")
        with pytest.raises(legacy_authorization.SafetyClassificationError, match="asil_capability"):
            safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware))

    def test_a_threshold_and_a_capability_raised_on_one_core_are_one_question_with_two_changes(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        delta = [
            *_resource_update("CORE0", "asil_capability", "B", "D"),
            *_resource_update("CORE0", "threshold", 0.7, 0.9),
        ]
        changes = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert {change.resource for change in changes} == {"CORE0"}
        # The classifier's field order, the capability last, whatever the delta's order.
        assert [change.field for change in changes] == ["threshold", "asil_capability"]

    def test_the_deletion_prompt_names_a_rated_elements_capability_and_not_an_unrated_ones(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        remove_core = [_entry("remove", _address(HARDWARE_LAYER, "CORE0"), before={"name": "CORE0"})]
        (rated,) = safety_verdict(remove_core, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert rated.old == "kind=core, threshold=0.7, enforcement=strict, asil_capability=B"

        unrated = (Core(name="CORE0", kind="core", threshold=0.7, enforcement="strict"),)
        (plain,) = safety_verdict(remove_core, _snapshot(unrated), staged=_snapshot(unrated)).changes
        assert plain.old == "kind=core, threshold=0.7, enforcement=strict"

    def test_a_clear_through_the_real_differ_is_asked_and_a_declaration_is_not(self, project: Path) -> None:
        """Over the real differ, a clear is an `update` to `None` and asked; a declaration is silent.

        The differ never emits a `remove` at the field.
        """

        def rate(level: str) -> Callable[[dict[str, Any]], None]:
            def edit(document: dict[str, Any]) -> None:
                document["asil_capability"] = level

            return edit

        def clear(document: dict[str, Any]) -> None:
            del document["asil_capability"]

        cleared, baseline = _ram0_delta(project, clear, prepare=rate("A"))
        assert [(entry.operation, entry.address, entry.before, entry.after) for entry in cleared] == [
            ("update", _address(HARDWARE_LAYER, "RAM0", "asil_capability"), "A", None)
        ]
        (change,) = safety_verdict(cleared, baseline, staged=baseline).changes
        assert (change.kind, change.old, change.new) == (
            legacy_authorization.SafetyChangeKind.RAISE_CAPABILITY,
            "A",
            None,
        )

        declared, baseline = _ram0_delta(project, rate("A"))
        assert [(entry.before, entry.after) for entry in declared] == [(None, "A")]
        assert safety_verdict(declared, baseline, staged=baseline).changes == ()


class TestTheAdmissionsDeltaShape:
    """What `editing/delta.py` really emits for a widened `accessible`, over two real snapshots.

    A hand-built entry could pin a verdict over a shape the projection never
    produces. Does **not** reach the applied result: the writer's bytes are
    `test_editing_apply.py`'s, the staged model's legality `check-model`'s.
    """

    def test_admitting_a_core_is_a_create_at_the_members_own_address(self, project: Path) -> None:
        delta, _baseline = _ram0_delta(project, _accessible("CORE0", "CORE1"))
        assert len(delta) == 1, f"expected one entry for one admitted core, got {[e.summary for e in delta]}"
        (entry,) = delta
        assert entry.operation == "create"
        assert entry.address == ACCESSIBLE_MEMBER
        assert entry.after == "CORE1"

    def test_the_guard_fires_on_that_entry(self, project: Path) -> None:
        delta, _baseline = _ram0_delta(project, _accessible("CORE0", "CORE1"))
        assert is_safety_relevant(delta[0].operation, delta[0].address)

    def test_a_reordering_is_an_update_at_the_field_and_is_not_a_safety_act(self, project: Path) -> None:
        """A reordering is one `update` at the collection's address and admits nobody: no weakening."""
        delta, baseline = _ram0_delta(
            project,
            _accessible("CORE1", "CORE0"),
            prepare=_accessible("CORE0", "CORE1"),
        )
        assert [entry.operation for entry in delta] == ["update"]
        assert delta[0].address == _address(HARDWARE_LAYER, "RAM0", "accessible")
        assert (delta[0].before, delta[0].after) == (["CORE0", "CORE1"], ["CORE1", "CORE0"])
        assert not is_safety_relevant(delta[0].operation, delta[0].address)
        assert safety_verdict(delta, baseline, staged=baseline).changes == ()

    def test_two_admitted_cores_are_one_question_about_one_memory(self, project: Path) -> None:
        """Two admissions fold into one verdict entry: the classifier is asked once per element."""
        # Declared before the baseline is taken, so the delta carries only the
        # two admissions.
        (project / "model" / "hardware" / "CORE2.yaml").write_text(
            yaml.safe_dump({"name": "CORE2", "kind": "core", "threshold": 0.7, "enforcement": "strict"}),
            encoding="utf-8",
            newline="\n",
        )
        delta, baseline = _ram0_delta(project, _accessible("CORE0", "CORE1", "CORE2"))
        assert [entry.operation for entry in delta] == ["create", "create"]
        (change,) = safety_verdict(delta, baseline, staged=baseline).changes
        assert change.kind is legacy_authorization.SafetyChangeKind.WIDEN_ACCESS
        assert change.new == ["CORE0", "CORE1", "CORE2"]
        assert "admits CORE1, CORE2" in change.describe()

    def test_an_admission_bundled_with_a_threshold_raise_is_two_weakenings_in_the_declared_order(
        self, project: Path
    ) -> None:
        """The shipped vector's change set: two weakenings, in `_SAFETY_FIELD_ORDER`, not the delta's order."""

        def edit(document: dict[str, Any]) -> None:
            document["accessible"] = ["CORE0", "CORE1"]
            document["threshold"] = 0.9

        delta, baseline = _ram0_delta(project, edit)
        kinds = [change.kind for change in safety_verdict(delta, baseline, staged=baseline).changes]
        assert kinds == [
            legacy_authorization.SafetyChangeKind.RAISE_THRESHOLD,
            legacy_authorization.SafetyChangeKind.WIDEN_ACCESS,
        ]

    def test_the_hand_built_member_delta_is_the_one_the_projection_produces(self, project: Path) -> None:
        # Holds the parity table's `_member_change` to the projection.
        derived, _baseline = _ram0_delta(project, _accessible("CORE0", "CORE1"))
        built = _member_change("RAM0", "accessible", ["CORE0"], ["CORE0", "CORE1"])
        assert [(entry.operation, entry.address, entry.after) for entry in derived] == [
            (entry.operation, entry.address, entry.after) for entry in built
        ]


class TestTheSafetyVerdictOverAChangeSet:
    """What reading the delta gains over reading a call's arguments."""

    def test_a_change_set_with_no_hardware_edit_is_not_a_safety_act(self, hardware: tuple[Hardware, ...]) -> None:
        empty = _snapshot(hardware, swcs=[_component("ExampleSWC")])
        verdict = safety_verdict([_entry("update", SWC_DESCRIPTION, before="a", after="b")], empty, staged=empty)
        assert not verdict.is_safety_act

    def test_a_weakening_bundled_with_unrelated_edits_is_still_classified(self, hardware: tuple[Hardware, ...]) -> None:
        # Classified on its outcome, so a weakening cannot hide behind other operations.
        delta = [
            _entry("update", SWC_DESCRIPTION, before="a", after="b"),
            _entry("update", _address(HARDWARE_LAYER, "RAM0", "threshold"), before=0.8, after=0.95),
            _entry("create", _address("implementation_types", "uint16"), after={"name": "uint16"}),
        ]
        empty = _snapshot(hardware, swcs=[_component("ExampleSWC")])
        verdict = safety_verdict(delta, empty, staged=empty)
        assert [change.kind for change in verdict.changes] == [legacy_authorization.SafetyChangeKind.RAISE_THRESHOLD]
        assert verdict.changes[0].resource == "RAM0"

    def test_every_weakening_is_carried_not_the_first(self, hardware: tuple[Hardware, ...]) -> None:
        # A verdict reporting the first match would under-describe what the user approves.
        delta = [
            _entry("update", _address(HARDWARE_LAYER, "RAM0", "threshold"), before=0.8, after=0.95),
            _entry("update", _address(HARDWARE_LAYER, "RAM0", "size"), before=262144, after=524288),
            _entry("update", _address(HARDWARE_LAYER, "CORE0", "enforcement"), before="strict", after="advisory"),
        ]
        kinds = [
            change.kind for change in safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        ]
        assert kinds == [
            legacy_authorization.SafetyChangeKind.RAISE_THRESHOLD,
            legacy_authorization.SafetyChangeKind.ENLARGE_POOL,
            legacy_authorization.SafetyChangeKind.WEAKEN_ENFORCEMENT,
        ]

    def test_old_and_new_are_the_delta_entrys_before_and_after(self, hardware: tuple[Hardware, ...]) -> None:
        # `old` is read off the baseline resource, not off `before`; this pins
        # that the two are the same value.
        delta = _resource_update("RAM0", "threshold", 0.8, 0.95)
        change = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes[0]
        assert (change.old, change.new) == (delta[0].before, delta[0].after)

    def test_removing_a_memory_renders_the_whole_of_it(self, hardware: tuple[Hardware, ...]) -> None:
        delta = [_entry("remove", _address(HARDWARE_LAYER, "RAM0"), before={"name": "RAM0"})]
        change = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes[0]
        assert change.kind is legacy_authorization.SafetyChangeKind.DELETE_RESOURCE
        assert "size=262144" in str(change.old) and change.new is None

    def test_an_element_the_baseline_does_not_hold_fails_closed(self, hardware: tuple[Hardware, ...]) -> None:
        # A guard that cannot read the current values must not report "not weakening".
        delta = [_entry("remove", _address(HARDWARE_LAYER, "NoSuchPool"), before={"name": "NoSuchPool"})]
        with pytest.raises(legacy_authorization.SafetyClassificationError):
            safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware))

    def test_the_verdict_is_labelled_with_the_change_set(self, hardware: tuple[Hardware, ...]) -> None:
        delta = _resource_update("RAM0", "threshold", 0.8, 0.95)
        assert safety_verdict(
            delta, _snapshot(hardware), staged=_snapshot(hardware), change_set_id="cs-20260816T102241Z-4f19c2"
        ).tool == ("cs-20260816T102241Z-4f19c2")

    def test_one_element_touched_twice_is_one_question(self, hardware: tuple[Hardware, ...]) -> None:
        # Grouping is per element: one question about RAM0, one line per weakening.
        delta = [
            _entry("update", _address(HARDWARE_LAYER, "RAM0", "size"), before=262144, after=524288),
            _entry("update", _address(HARDWARE_LAYER, "RAM0", "threshold"), before=0.8, after=0.95),
        ]
        changes = safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        assert {change.resource for change in changes} == {"RAM0"}
        # The classifier's own field order, not the delta's.
        assert [change.field for change in changes] == ["threshold", "size"]

    def test_a_one_shot_iterator_of_hardware_is_read_as_often_as_the_verdict_needs(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """The annotation admits a generator, so the verdict reads the baseline as often as it needs.

        An exhausted iterator raised a `SafetyClassificationError` indistinguishable
        from a real one. The delta exercises both readers.
        """
        delta = [
            *_member_change("RAM0", "accessible", ["CORE0"], ["CORE0", "CORE1"]),
            _entry("update", _address(HARDWARE_LAYER, "CORE0", "enforcement"), before="strict", after="advisory"),
        ]
        expected = [
            (change.kind, change.resource, change.new)
            for change in safety_verdict(delta, _snapshot(hardware), staged=_snapshot(hardware)).changes
        ]

        from_iterator = safety_verdict(delta, _snapshot(iter(hardware)), staged=_snapshot(hardware))

        assert [(change.kind, change.resource, change.new) for change in from_iterator.changes] == expected
        assert len(expected) == 2

    def test_a_one_shot_iterator_of_staged_hardware_is_read_as_often_as_the_verdict_needs(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """The staged model's ``hardware`` is materialized once too, as the baseline's is.

        A partition entry reads the staged layer more than once; an exhausted
        iterator read the lowered level as withdrawn.
        """
        from swcraft_tools.schema import Partition

        before = (*hardware, Partition(kind="partition", name="P_Rated", core="CORE0", asil="D"))
        after = (*hardware, Partition(kind="partition", name="P_Rated", core="CORE0", asil="B"))
        delta = [_entry("update", _address(HARDWARE_LAYER, "P_Rated", "asil"), before="D", after="B")]
        expected = [
            (change.kind, change.resource, change.new)
            for change in safety_verdict(delta, _snapshot(before), staged=_snapshot(after)).changes
        ]

        from_iterator = safety_verdict(delta, _snapshot(before), staged=_snapshot(iter(after)))

        assert [(change.kind, change.resource, change.new) for change in from_iterator.changes] == expected
        assert expected == [(legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION, "P_Rated", "B")]


# A declared argument that answers a finding, and the layer partition in front of the fold


class TestTheDeclaredArgumentsAreTheSixthDirection:
    """A `measures` entry and a `coexistence` text make a check stop refusing.

    * ``measures`` is the operation shape: a ``create`` at a four-step address
      ending in a key (a map entry is addressed by its key);
    * ``coexistence`` is the value shape: an ``update`` whose direction is
      ``"" -> text``, decided by the classifier.

    Removing a measure, blanking an argument and rewording either can only make
    a check refuse more.
    """

    MEASURE = _address("swc", "Dst", "ports", {"member": "rIn"}, "measures", {"key": "value"})
    COEXISTENCE = _address("tasks", "Task_10ms", "coexistence")

    def test_creating_a_measure_entry_fires_the_predicate(self) -> None:
        assert is_safety_relevant("create", self.MEASURE)

    def test_removing_one_does_not(self) -> None:
        """Withdrawing a measure makes `safety.consumer-above-element` refuse again."""
        assert not is_safety_relevant("remove", self.MEASURE)

    def test_updating_an_existing_entrys_text_does_not(self) -> None:
        """Rewording an argument removes no check; only declaring one does."""
        assert not is_safety_relevant("update", self.MEASURE)

    @pytest.mark.parametrize(
        "address",
        [
            _address("swc", "Dst", "ports", {"member": "rIn"}, "measures"),
            _address("swc", "Dst", "ports", {"member": "rIn"}, "measures", {"member": "value"}),
            _address("swc", "Dst", "runnables", {"member": "R"}, "reads", {"key": "value"}),
        ],
        ids=["the-field", "a-member-step-not-a-key", "another-collection"],
    )
    def test_no_neighbouring_address_shape_fires_it(self, address: Address) -> None:
        """Four steps, a key and the admitted pair are each required.

        A ``member`` step in place of the key is a shape the differ never emits
        for a map.
        """
        assert not is_safety_relevant("create", address)

    @pytest.mark.parametrize(
        ("direction", "declared", "weakens"),
        [
            ("require", {"speed": "E2E profile 4"}, True),
            ("require", {}, False),
            ("provide", {"speed": "E2E profile 4"}, False),
        ],
        ids=["carrying-a-measure", "carrying-none", "a-provide-port-carrying-one"],
    )
    def test_a_port_that_arrives_carrying_a_measure_is_the_same_declaration(
        self, hardware: tuple[Hardware, ...], direction: str, declared: dict[str, str], weakens: bool
    ) -> None:
        """An argument does not escape the question by arriving inside a created port.

        The guard classifies by the entry's outcome. The direction is read off
        the entry: a ``provide`` port's measure answers nothing. The four-step key
        ``create`` does not read it: only the baseline's direction, possibly
        stale, is in reach there.
        """
        port = _address("swc", "Dst", "ports", {"member": "rIn"})
        after = {"name": "rIn", "direction": direction, "interface": "ISpeed", "measures": declared}
        interfaces = [_interface("ISpeed", speed="A")]

        verdict = safety_verdict(
            [_entry("create", port, after=after)],
            _snapshot(hardware, swcs=[_component("Dst", asil="D")], interfaces=interfaces),
            staged=_snapshot(
                hardware,
                swcs=[_component("Dst", asil="D", ports=[_port("rIn", direction, "ISpeed", declared)])],
                interfaces=interfaces,
            ),
        )

        assert verdict.is_safety_act is weakens
        if weakens:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE
            assert change.resource == "Dst.rIn"

    @pytest.mark.parametrize(
        ("direction", "declared", "weakens"),
        [
            ("require", {"speed": "E2E"}, True),
            ("provide", {"speed": "E2E"}, False),
            ("require", {}, False),
        ],
        ids=["the-require-port-that-carried-one", "a-provide-port", "a-require-port-carrying-none"],
    )
    def test_removing_the_require_port_that_carried_a_measure_is_classified(
        self, hardware: tuple[Hardware, ...], direction: str, declared: dict[str, str], weakens: bool
    ) -> None:
        """Removing a ``require`` port that carried a measure takes the consumer rule's question away.

        A ``provide`` port, or one carrying nothing, withdraws nothing.
        """
        port = _address("swc", "Dst", "ports", {"member": "rIn"})
        before = {"name": "rIn", "direction": direction, "interface": "ISpeed", "measures": declared}
        interfaces = [_interface("ISpeed", speed="A")]

        verdict = safety_verdict(
            [_entry("remove", port, before=before)],
            _snapshot(
                hardware,
                swcs=[_component("Dst", asil="D", ports=[_port("rIn", direction, "ISpeed", declared)])],
                interfaces=interfaces,
            ),
            staged=_snapshot(hardware, swcs=[_component("Dst", asil="D")], interfaces=interfaces),
        )

        assert verdict.is_safety_act is weakens
        if weakens:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
            assert (change.resource, change.field) == ("Dst.rIn", "measures")

    def test_a_declared_measure_is_classified_and_names_the_port_and_the_element(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        delta = [_entry("create", self.MEASURE, after="E2E profile 4")]
        interfaces = [_interface("IExample", value="A")]

        (change,) = safety_verdict(
            delta,
            _snapshot(
                hardware,
                swcs=[_component("Dst", asil="D", ports=[_port("rIn", "require", "IExample")])],
                interfaces=interfaces,
            ),
            staged=_snapshot(
                hardware,
                swcs=[
                    _component(
                        "Dst",
                        asil="D",
                        ports=[_port("rIn", "require", "IExample", {"value": "E2E profile 4"})],
                    )
                ],
                interfaces=interfaces,
            ),
        ).changes

        assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE
        # The resource names the port and the signal, not only the component.
        assert change.resource == "Dst.rIn[value]"
        assert change.new == "E2E profile 4"
        assert "value" in change.describe()

    @pytest.mark.parametrize("operation", ["create", "remove"])
    def test_no_other_verb_at_coexistence_fires_the_predicate(self, operation: str) -> None:
        assert not is_safety_relevant(operation, self.COEXISTENCE)

    @pytest.mark.parametrize(
        ("before", "after", "weakens"),
        [("", "argued", True), ("argued", "", False), ("argued", "reworded", False)],
        ids=["declared", "withdrawn", "reworded"],
    )
    def test_only_declaring_a_coexistence_argument_is_a_weakening(
        self, hardware: tuple[Hardware, ...], before: str, after: str, weakens: bool
    ) -> None:
        delta = [_entry("update", self.COEXISTENCE, before=before, after=after)]
        # Two levels, so the argument really answers a mixed task.
        swcs = [_component("Hi", asil="D"), _component("Lo", asil="QM")]
        runnables = [{"swc": "Hi", "runnable": "R"}, {"swc": "Lo", "runnable": "R"}]

        verdict = safety_verdict(
            delta,
            _snapshot(hardware, swcs=swcs, tasks=[_task("Task_10ms", coexistence=before, runnables=runnables)]),
            staged=_snapshot(hardware, swcs=swcs, tasks=[_task("Task_10ms", coexistence=after, runnables=runnables)]),
        )

        assert verdict.is_safety_act is weakens
        if weakens:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_COEXISTENCE
            assert change.resource == "Task_10ms"

    def test_a_data_elements_lowered_level_is_the_seventh_direction_one_layer_down(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """A data element's lowered level is a weakening: three steps, the level the leaf."""
        address = _address("interfaces", "IExample", "data_elements", {"member": "value"}, "asil")

        assert is_safety_relevant("update", address)

        (change,) = safety_verdict(
            [_entry("update", address, before="D", after="A")],
            _snapshot(hardware, interfaces=[_interface("IExample", value="D")]),
            staged=_snapshot(hardware, interfaces=[_interface("IExample", value="A")]),
        ).changes

        assert change.kind is legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION
        assert change.resource == "IExample.value"

    def test_raising_a_data_elements_level_is_not(self, hardware: tuple[Hardware, ...]) -> None:
        address = _address("interfaces", "IExample", "data_elements", {"member": "value"}, "asil")

        assert (
            safety_verdict(
                [_entry("update", address, before="A", after="D")],
                _snapshot(hardware, interfaces=[_interface("IExample", value="A")]),
                staged=_snapshot(hardware, interfaces=[_interface("IExample", value="D")]),
            ).changes
            == ()
        )

    def test_an_interface_field_that_is_not_a_level_does_not_fire(self) -> None:
        """The branch reads the whole path, so a neighbouring member field is out."""
        assert not is_safety_relevant(
            "update", _address("interfaces", "IExample", "data_elements", {"member": "value"}, "init")
        )


class TestAnArgumentInsideItsOwnerIsFoundAtANYDepth:
    """The guard reads a created value's arguments at any depth (`_declared_arguments`).

    An added element is one `create` carrying its whole value, so a component
    created with its measure never commits a `DECLARE_MEASURE` unasked. Matching
    one more nesting level would reproduce the defect at the next. Driven by the
    delta the differ really produces.
    """

    @staticmethod
    def _new_component(measured: bool) -> dict[str, Any]:
        port: dict[str, Any] = {"name": "rSpeed", "direction": "require", "interface": "ISafetySignals"}
        if measured:
            port["measures"] = {"speed": "End-to-end protection profile 4 on the speed signal."}
        return {
            "name": "NewConsumer",
            "type": "application",
            "description": "A new consumer created whole, carrying its own measure.",
            "asil": "D",
            "ports": [port],
            "runnables": [{"name": "Consume", "events": [{"type": "timing", "period": 0.01}]}],
        }

    @pytest.mark.parametrize("measured", [True, False], ids=["carrying-a-measure", "carrying-none"])
    def test_a_whole_component_created_with_a_nested_measure_is_the_same_declaration(
        self, tmp_path: Path, measured: bool
    ) -> None:
        """A whole component created with a nested measure is a ``DECLARE_MEASURE``, over a real projection."""
        delta, baseline, staged = _safety_fixture_delta(
            tmp_path, lambda root: _write_component(root, self._new_component(measured))
        )

        assert [(entry.operation, entry.address.layer, entry.address.path) for entry in delta][0] == (
            "create",
            "swc",
            (),
        ), "the projection must still carry a new element as ONE create with an empty path"

        verdict = safety_verdict(delta, baseline, staged=staged)

        assert verdict.is_safety_act is measured
        if measured:
            (change,) = verdict.changes
            assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE
            assert change.resource == "NewConsumer.rSpeed"

    def test_the_measure_is_what_answers_the_finding(self, tmp_path: Path) -> None:
        """The nested map really closes a ``safety.consumer-above-element`` finding, so the question is owed."""
        with_measure = _findings_after(
            tmp_path / "with", lambda root: _write_component(root, self._new_component(True))
        )
        without = _findings_after(tmp_path / "without", lambda root: _write_component(root, self._new_component(False)))

        # Counted: the fixture already mints this finding, so a set comparison
        # would not see one closed.
        closed = Counter(without) - Counter(with_measure)
        assert dict(closed) == {"safety.consumer-above-element": 1}, (with_measure, without)
        assert not Counter(with_measure) - Counter(without), "the measure must close a finding and create none"

    def test_a_task_created_with_a_coexistence_text_is_classified_the_same_way(self, tmp_path: Path) -> None:
        """The sibling shape, over the same projection rather than a built entry."""
        task = {
            "name": "Task_New",
            "period": 0.02,
            "priority": 6,
            "partition": "PARTITION0",
            "wcet": "200us",
            "description": "A new task spanning two levels.",
            "coexistence": "Partitioned, with timing protection.",
            "runnables": [
                {"swc": "TorqueLimiter", "runnable": "Calibrate"},
                {"swc": "PlainWorker", "runnable": "WorkSlow"},
            ],
        }
        delta, baseline, staged = _safety_fixture_delta(
            tmp_path, lambda root: _write_yaml(root / "tasks/Task_New.yaml", task)
        )

        (change,) = safety_verdict(delta, baseline, staged=staged).changes

        assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_COEXISTENCE
        assert change.resource == "Task_New"

    def test_the_walk_reaches_a_depth_no_shipped_projection_produces(self, hardware: tuple[Hardware, ...]) -> None:
        """An attribution that cannot answer costs the wording, never the question.

        A port nested deeper than any published element is attributed to no
        owner; the real owner in the staged model still asks, with
        :func:`_decide`'s own sentence. Attribution names the entry and never
        decides.
        """
        after = {
            "name": "Deep",
            "variants": [
                {
                    "name": "vA",
                    "ports": [
                        {"name": "rIn", "direction": "require", "interface": "ISpeed", "measures": {"speed": "E2E"}}
                    ],
                }
            ],
        }

        # The decision is the port's owner in the staged model; the synthetic
        # value is what the attribution reads.
        interfaces = [_interface("ISpeed", speed="A")]
        (change,) = safety_verdict(
            [_entry("create", SWC, after=after)],
            _snapshot(hardware, interfaces=interfaces),
            staged=_snapshot(
                hardware,
                swcs=[
                    _component(
                        "ExampleSWC",
                        asil="D",
                        ports=[_port("rIn", "require", "ISpeed", {"speed": "E2E"})],
                    )
                ],
                interfaces=interfaces,
            ),
        ).changes

        assert change.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE
        assert change.resource == "ExampleSWC.rIn", (
            "the arm named `ExampleSWC.vA.rIn`, which resolves to no owner; the question must "
            "survive that with the model's own name on it"
        )
        assert change.new == {"speed": "E2E"}

    def test_the_argument_fields_are_declared_on_exactly_three_kinds(self) -> None:
        """What the walk does not reach, gated: arguments live on exactly three kinds.

        The walk covers the ``swc`` and ``tasks`` element-level ``create``; the
        partition's ``coexistence`` is decided by comparing two models. A fourth
        kind gaining an argument would need its own predicate.
        """
        contract = json.loads(
            (
                REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "model_contract" / "model-contract.json"
            ).read_text(encoding="utf-8")
        )
        declared = {
            field["id"]
            for layer in contract["layers"]
            for kind in layer["kinds"]
            for field in kind["fields"]
            if field["id"].rsplit(".", 1)[-1] in {"measures", "coexistence"}
        }

        assert declared == {
            "field:swc.port.measures",
            "field:tasks.task.coexistence",
            "field:hardware.partition.coexistence",
        }

    def test_the_walks_layer_tables_are_the_contract_s_own_placement(self) -> None:
        """The walk's layer tables equal the contract's placement of each argument.

        An argument in one layer is data in another: a data element named
        ``coexistence`` is never read as a task's argument. Derived from the
        contract, so a new layer gaining an argument fails here.
        """
        from swcraft_tools.editing.authorization import (
            MAPPING_ARGUMENTS_BY_LAYER,
            SCALAR_ARGUMENTS_BY_LAYER,
        )

        contract = json.loads(
            (
                REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "model_contract" / "model-contract.json"
            ).read_text(encoding="utf-8")
        )
        placement: dict[str, set[str]] = {}
        for layer in contract["layers"]:
            for kind in layer["kinds"]:
                for field in kind["fields"]:
                    name = field["id"].rsplit(".", 1)[-1]
                    if name in {"measures", "coexistence"}:
                        placement.setdefault(layer["name"], set()).add(name)

        assert placement == {"swc": {"measures"}, "tasks": {"coexistence"}, "hardware": {"coexistence"}}
        # No walk reads `hardware`: the partition's argument is the comparison's.
        assert {layer: set(fields) for layer, fields in SCALAR_ARGUMENTS_BY_LAYER.items()} == {
            "tasks": {"coexistence"},
            "hardware": {"coexistence"},
        }
        assert {layer: set(fields) for layer, fields in MAPPING_ARGUMENTS_BY_LAYER.items()} == {"swc": {"measures"}}


def _satisfies_at(
    component: str, site: tuple[str, str] | None, edit: Callable[[list[str]], list[str]]
) -> Callable[[Path], None]:
    """Rewrite one ``satisfies`` list of a safety-fixture component: its own, or a port's or a runnable's.

    *site* is ``None`` for the component's own list, else ``("ports", name)`` or
    ``("runnables", name)``; *edit* maps the list as it stands to the list written.
    """

    def apply(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        held = _read_yaml(path)
        if site is None:
            owner = held
        else:
            owner = next(member for member in held[site[0]] if member["name"] == site[1])
        owner["satisfies"] = edit(list(owner.get("satisfies", [])))
        if not owner["satisfies"]:
            del owner["satisfies"]
        _write_yaml(path, held)

    return apply


def _edit_requirement(requirement: str, **fields: Any) -> Callable[[Path], None]:
    """Set *fields* on one requirement file of the safety fixture; a ``None`` value drops the key."""

    def apply(root: Path) -> None:
        path = root / "requirements" / f"{requirement}.yaml"
        held = _read_yaml(path)
        for field, value in fields.items():
            if value is None:
                held.pop(field, None)
            else:
                held[field] = value
        _write_yaml(path, held)

    return apply


#: ``SpeedSensor`` declares no level and derives ``B`` from ``REQ-SAFETY-002``
#: alone, which makes it the component on which moving that one link moves the
#: maximum: the fixture's other bearer of the id, ``PlainWorker``, declares ``B``.
_SPEED_LINK = "REQ-SAFETY-002@1"

#: The two member sites a ``satisfies`` list can sit on below the component.
_MEMBER_SITES = [("ports", "pSafety"), ("runnables", "SampleSpeed")]


def _moved_to(site: tuple[str, str]) -> Callable[[Path], None]:
    """``SpeedSensor``'s link moved from the component to *site*, before the baseline."""
    return _both(
        _satisfies_at("SpeedSensor", None, lambda held: [link for link in held if link != _SPEED_LINK]),
        _satisfies_at("SpeedSensor", site, lambda held: [*held, _SPEED_LINK]),
    )


class TestRemovingABearerWholeWithdrawsWhatItCarried:
    """An element-level ``remove`` classifies in every bearer layer, only where the element was one.

    A bearer: a component whose effective level is an ASIL (not ``QM``) or with
    a ``require`` port carrying a map; a task whose ``coexistence`` covers
    runnables spanning two levels; an interface's rated data elements.
    Everything else is silent, as ``examples/basic`` asserts.
    """

    def _fixture(self, tmp_path: Path, relative: str) -> tuple[list[DeltaEntry], ModelSnapshot, ModelSnapshot]:
        return _safety_fixture_delta(tmp_path, lambda root: (root / relative).unlink())

    def test_a_component_that_declares_a_level_and_carries_measures_withdraws_all_three(self, tmp_path: Path) -> None:
        """``TorqueLimiter`` (``asil: D``, two ``require`` ports carrying maps) withdraws three changes."""
        delta, baseline, staged = _safety_fixture_delta(tmp_path, _remove_component("TorqueLimiter"))

        changes = safety_verdict(delta, baseline, staged=staged).changes

        assert [(change.resource, change.field) for change in changes] == [
            ("TorqueLimiter", "asil"),
            ("TorqueLimiter.rPlain", "measures"),
            ("TorqueLimiter.rPlainMeasured", "measures"),
        ]
        assert {change.kind for change in changes} == {legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION}

    def test_a_component_that_carries_nothing_a_rule_reads_withdraws_nothing(self, tmp_path: Path) -> None:
        """``DiagnosticsAgent`` derives ``QM`` and no rule can read either of its maps: nothing withdrawn.

        The withdrawal is keyed on
        :meth:`~swcraft_tools.validator.SafetyReading.readable`, not on what the
        port declares.
        """
        delta, baseline, staged = _safety_fixture_delta(tmp_path, _remove_component("DiagnosticsAgent"))

        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_only_the_ARGUED_task_that_really_spans_two_levels_is_a_bearer(self, tmp_path: Path) -> None:
        """Of the fixture's three tasks, only ``Task_Argued`` (a text over two levels) is a bearer.

        ``Task_Homogeneous``'s text answered nothing; deleting ``Task_Mixed``
        (no text) escapes a finding rather than withdrawing protection.
        """
        argued, baseline, staged = self._fixture(tmp_path / "argued", "tasks/Task_Argued.yaml")
        homogeneous, homogeneous_baseline, homogeneous_staged = self._fixture(
            tmp_path / "homogeneous", "tasks/Task_Homogeneous.yaml"
        )
        mixed, mixed_baseline, mixed_staged = self._fixture(tmp_path / "mixed", "tasks/Task_Mixed.yaml")

        (change,) = safety_verdict(argued, baseline, staged=staged).changes
        assert (change.kind, change.resource, change.field) == (
            legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION,
            "Task_Argued",
            "coexistence",
        )
        assert safety_verdict(homogeneous, homogeneous_baseline, staged=homogeneous_staged).changes == ()
        assert safety_verdict(mixed, mixed_baseline, staged=mixed_staged).changes == ()

    def test_an_interface_withdraws_one_change_per_RATED_element(self, tmp_path: Path) -> None:
        """``ISafetySignals`` withdraws its two rated elements; ``IDiagnostics`` declares none."""
        rated, baseline, staged = self._fixture(tmp_path / "rated", "interfaces/ISafetySignals.yaml")
        client_server, cs_baseline, cs_staged = self._fixture(tmp_path / "cs", "interfaces/IDiagnostics.yaml")

        assert [(c.resource, c.old) for c in safety_verdict(rated, baseline, staged=staged).changes] == [
            ("ISafetySignals.torque", "D"),
            ("ISafetySignals.speed", "B"),
        ]
        assert safety_verdict(client_server, cs_baseline, staged=cs_staged).changes == ()

    @pytest.mark.parametrize(
        "removal",
        [
            _remove_component("ExampleConsumerSWC"),
            _remove_file("interfaces/IExample.yaml"),
            _remove_file("tasks/Task_10ms.yaml"),
            _remove_file("requirements/REQ_EXAMPLE_001.yaml"),
        ],
        ids=["a-component", "an-interface", "a-task", "a-requirement"],
    )
    def test_a_project_that_has_not_adopted_ASIL_is_asked_NOTHING(
        self, tmp_path: Path, removal: Callable[[Path], None]
    ) -> None:
        """On ``examples/basic``, which declares no level, measure or argument, every removal is silent."""
        delta, baseline, staged = _delta_over(tmp_path, EXAMPLES_BASIC_MODEL, removal)

        assert delta, "the removal must actually produce a delta, or the silence proves nothing"
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    @pytest.mark.parametrize("site", _MEMBER_SITES, ids=["a-port", "a-runnable"])
    def test_a_component_carrying_its_link_on_a_member_withdraws_it_when_removed_whole(
        self, tmp_path: Path, site: tuple[str, str]
    ) -> None:
        """``SpeedSensor`` removed whole withdraws its derived ``B`` wherever in it the link sat."""
        assert _verdict(tmp_path, _remove_component("SpeedSensor"), prepare=_moved_to(site)) == (
            ("lower_allocation", "SpeedSensor", "asil", "B", None),
        )


class TestTheElisionAtTheEdgeNeverReachesTheGuard:
    """A summarised ``before`` is the edge's; the guard reads the engine's whole delta.

    The summary turns ports and data elements into counts, which the guard's
    removal arms read. Each case first asserts the edge really elided them.
    """

    def test_a_component_with_measured_require_ports_still_asks(self, tmp_path: Path) -> None:
        delta, baseline, staged = _safety_fixture_delta(tmp_path, _remove_component("TorqueLimiter"))
        (removed,) = [entry for entry in summarize_removed_elements(delta) if entry.address.element == "TorqueLimiter"]
        assert removed.before_form == "summary" and isinstance(removed.before["ports"], int)

        changes = safety_verdict(delta, baseline, staged=staged).changes

        assert [(change.resource, change.field) for change in changes] == [
            ("TorqueLimiter", "asil"),
            ("TorqueLimiter.rPlain", "measures"),
            ("TorqueLimiter.rPlainMeasured", "measures"),
        ]

    def test_an_interface_with_rated_data_elements_still_asks(self, tmp_path: Path) -> None:
        delta, baseline, staged = _safety_fixture_delta(tmp_path, _remove_file("interfaces/ISafetySignals.yaml"))
        (removed,) = summarize_removed_elements(delta)
        assert removed.before_form == "summary" and isinstance(removed.before["data_elements"], int)

        changes = safety_verdict(delta, baseline, staged=staged).changes

        assert [(change.resource, change.old) for change in changes] == [
            ("ISafetySignals.torque", "D"),
            ("ISafetySignals.speed", "B"),
        ]

    def test_the_engines_delta_is_whole_after_the_edge_ran(self, tmp_path: Path) -> None:
        delta, _baseline, _staged = _safety_fixture_delta(tmp_path, _remove_component("TorqueLimiter"))
        whole = [entry.to_json() for entry in delta]

        summarize_removed_elements(delta)

        assert [entry.to_json() for entry in delta] == whole
        assert all(entry.before_form is None for entry in delta)


class TestAnUnlinkIsMeasuredAgainstWhatTheComponentKEEPS:
    """An unlink is a weakening only where the maximum allocated to the component drops."""

    REMOVED = _address("swc", "Ctl", "satisfies", {"member": "REQ_1@1"})

    def _verdict(self, hardware: tuple[Hardware, ...], **layers: Any) -> SafetyVerdict:
        """The verdict for dropping ``REQ_1@1`` from ``Ctl``'s ``satisfies`` over *layers*."""
        baseline = _snapshot(hardware, **layers)
        staged = _snapshot(
            hardware,
            **{
                **layers,
                "swcs": [
                    held.model_copy(update={"satisfies": [link for link in held.satisfies if link != "REQ_1@1"]})
                    if held.name == "Ctl"
                    else held
                    for held in layers.get("swcs", [])
                ],
            },
        )
        return safety_verdict([_entry("remove", self.REMOVED, before="REQ_1@1")], baseline, staged=staged)

    def test_a_link_naming_no_declared_requirement_allocates_nothing(self, hardware: tuple[Hardware, ...]) -> None:
        """That dangling id is ``trace.link-unknown-requirement``'s finding."""
        verdict = self._verdict(hardware, requirements=[], swcs=[_component("Ctl", satisfies=["REQ_1@1"])])

        assert verdict.changes == ()

    def test_another_requirement_holding_the_same_level_keeps_the_maximum(self, hardware: tuple[Hardware, ...]) -> None:
        """Two requirements allocate ``D``; withdrawing one leaves the maximum at ``D``."""
        verdict = self._verdict(
            hardware,
            requirements=[_requirement("REQ_1", asil="D"), _requirement("REQ_2", asil="D")],
            swcs=[_component("Ctl", satisfies=["REQ_1@1", "REQ_2@1"])],
        )

        assert verdict.changes == ()

    def test_a_requirement_holding_a_LOWER_level_does_not_keep_it(self, hardware: tuple[Hardware, ...]) -> None:
        """The positive control: ``D`` withdrawn over a remaining ``A`` really is a drop."""
        verdict = self._verdict(
            hardware,
            requirements=[_requirement("REQ_1", asil="D"), _requirement("REQ_2", asil="A")],
            swcs=[_component("Ctl", satisfies=["REQ_1@1", "REQ_2@1"])],
        )

        (change,) = verdict.changes
        assert (change.resource, change.field, change.old, change.new) == ("Ctl", "satisfies", "REQ_1@1", None)


class TestTheGuardsEffectiveLevelIsTheVALIDATORS:
    """``_derived_component_level`` answers what ``validator.derived_asil_levels`` answers.

    Where nothing allocates a level the guard answers the validator's ``"QM"``,
    never ``None``, and never asks about withdrawing a declared ``QM``.
    """

    @pytest.mark.parametrize(
        ("declared", "allocated", "weakens"),
        [
            ("QM", None, False),
            ("D", None, True),
            ("D", "D", False),
            ("D", "A", True),
        ],
        ids=["QM-over-nothing", "D-over-nothing", "D-over-D", "D-over-A"],
    )
    def test_withdrawing_a_declaration_is_a_drop_only_where_the_effective_level_drops(
        self, hardware: tuple[Hardware, ...], declared: str, allocated: str | None, weakens: bool
    ) -> None:
        requirements = [_requirement("REQ_1", asil=allocated)] if allocated else []
        linked = ["REQ_1@1"] if allocated else []
        baseline = _snapshot(
            hardware, requirements=requirements, swcs=[_component("ExampleSWC", asil=declared, satisfies=linked)]
        )
        staged = _snapshot(hardware, requirements=requirements, swcs=[_component("ExampleSWC", satisfies=linked)])
        delta = [_entry("update", _address("swc", "ExampleSWC", "asil"), before=declared, after=None)]

        assert safety_verdict(delta, baseline, staged=staged).is_safety_act is weakens

    def test_the_two_readings_agree_on_every_component_of_the_fixture(self) -> None:
        """The two readings agree on every component of the committed safety fixture."""
        from swcraft_tools.editing.safety_guard import _derived_component_level, _effective_level
        from swcraft_tools.validator import effective_asil_levels

        baseline = load_snapshot(str(SAFETY_MODEL_ROOT))
        validator_answer = effective_asil_levels(list(baseline.swcs), list(baseline.requirements))
        guard_answer = {
            swc.name: _effective_level(swc.asil, _derived_component_level(baseline, swc.name)) for swc in baseline.swcs
        }

        assert guard_answer == validator_answer


class TestADecomposedLevelIsReadByX:
    """The guard's derived level and its span read a decomposed value ``X(Y)`` as the rules do.

    The maximum goes through :func:`~swcraft_tools.safety.max_asil`;
    ``_levels_spanned`` counts distinct ``X``.
    """

    @pytest.mark.parametrize("order", [("REQ_1", "REQ_2"), ("REQ_2", "REQ_1")], ids=["B(C)-first", "B(D)-first"])
    def test_the_derived_level_is_the_validators_on_a_tie_of_x_in_either_order(
        self, hardware: tuple[Hardware, ...], order: tuple[str, str]
    ) -> None:
        from swcraft_tools.editing.safety_guard import _derived_component_level
        from swcraft_tools.validator import effective_asil_levels

        levels = {"REQ_1": "B(C)", "REQ_2": "B(D)"}
        requirements = [_requirement(identity, asil=levels[identity]) for identity in order]
        baseline = _snapshot(
            hardware, requirements=requirements, swcs=[_component("Ctl", satisfies=["REQ_1@1", "REQ_2@1"])]
        )

        assert _derived_component_level(baseline, "Ctl") == "B(D)"
        assert effective_asil_levels(list(baseline.swcs), list(baseline.requirements)) == {"Ctl": "B(D)"}

    def test_a_plain_level_wins_the_tie_for_the_guard_as_for_the_validator(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        from swcraft_tools.editing.safety_guard import _derived_component_level
        from swcraft_tools.validator import effective_asil_levels

        requirements = [_requirement("REQ_1", asil="B(D)"), _requirement("REQ_2", asil="B")]
        baseline = _snapshot(
            hardware, requirements=requirements, swcs=[_component("Ctl", satisfies=["REQ_1@1", "REQ_2@1"])]
        )

        assert _derived_component_level(baseline, "Ctl") == "B"
        assert effective_asil_levels(list(baseline.swcs), list(baseline.requirements)) == {"Ctl": "B"}

    def _remove_argued_task(self, hardware: tuple[Hardware, ...], first: str, second: str) -> Any:
        swcs = [_component("One", asil=first), _component("Two", asil=second)]
        task = _task(
            "Task_10ms",
            coexistence="argued",
            runnables=[{"swc": "One", "runnable": "R"}, {"swc": "Two", "runnable": "R"}],
        )
        delta = [_entry("remove", _address("tasks", "Task_10ms"), before=task.model_dump())]
        return safety_verdict(
            delta, _snapshot(hardware, swcs=swcs, tasks=[task]), staged=_snapshot(hardware, swcs=swcs)
        )

    def test_removing_the_argued_task_of_a_b_and_a_b_d_component_asks_nothing(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """One level by ``X``: the argument answered no finding, so nothing is withdrawn."""
        assert self._remove_argued_task(hardware, "B", "B(D)").changes == ()

    def test_removing_the_argued_task_of_a_b_and_an_a_d_component_still_asks(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        (change,) = self._remove_argued_task(hardware, "B", "A(D)").changes

        assert (change.kind, change.resource, change.field) == (
            legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION,
            "Task_10ms",
            "coexistence",
        )

    @pytest.mark.parametrize(
        ("before", "after", "lowers"),
        [("B", "B(D)", False), ("B(D)", "B", False), ("D(D)", "C(D)", True), ("A(D)", "B", False)],
    )
    def test_a_components_own_level_moves_by_x(
        self, hardware: tuple[Hardware, ...], before: str, after: str, lowers: bool
    ) -> None:
        """``_decide``'s effective and allocated comparisons, fed a decomposed value."""
        delta = [_entry("update", _address("swc", "ExampleSWC", "asil"), before=before, after=after)]

        changes = safety_verdict(
            delta,
            _snapshot(hardware, swcs=[_component("ExampleSWC", asil=before)]),
            staged=_snapshot(hardware, swcs=[_component("ExampleSWC", asil=after)]),
        ).changes

        assert [(change.kind, change.resource) for change in changes] == (
            [(legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION, "ExampleSWC")] if lowers else []
        )

    @pytest.mark.parametrize(
        ("before", "after", "lowers"), [("B", "B(D)", False), ("B(D)", "B(C)", False), ("B(D)", "A(D)", True)]
    )
    def test_a_data_elements_level_moves_by_x(
        self, hardware: tuple[Hardware, ...], before: str, after: str, lowers: bool
    ) -> None:
        address = _address("interfaces", "IExample", "data_elements", {"member": "value"}, "asil")

        changes = safety_verdict(
            [_entry("update", address, before=before, after=after)],
            _snapshot(hardware, interfaces=[_interface("IExample", value=before)]),
            staged=_snapshot(hardware, interfaces=[_interface("IExample", value=after)]),
        ).changes

        assert [(change.kind, change.resource) for change in changes] == (
            [(legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION, "IExample.value")] if lowers else []
        )

    @pytest.mark.parametrize(
        ("before", "after", "lowers"), [("B(D)", "B", False), ("B", "B(B)", False), ("B(D)", "A(D)", True)]
    )
    def test_a_partitions_level_moves_by_x(
        self, hardware: tuple[Hardware, ...], before: str, after: str, lowers: bool
    ) -> None:
        """Family 7's comparison on a partition's declared level, fed a decomposed value."""
        from swcraft_tools.schema import Partition

        baseline = (*hardware, Partition(kind="partition", name="P_Rated", core="CORE0", asil=before))
        staged = (*hardware, Partition(kind="partition", name="P_Rated", core="CORE0", asil=after))
        delta = [_entry("update", _address(HARDWARE_LAYER, "P_Rated", "asil"), before=before, after=after)]

        changes = safety_verdict(delta, _snapshot(baseline), staged=_snapshot(staged)).changes

        assert [(change.kind, change.resource, change.new) for change in changes] == (
            [(legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION, "P_Rated", after)] if lowers else []
        )

    def test_the_span_counts_distinct_x(self, hardware: tuple[Hardware, ...]) -> None:
        from swcraft_tools.editing.safety_guard import _levels_spanned

        swcs = [_component("One", asil="B"), _component("Two", asil="B(D)"), _component("Three", asil="A(D)")]
        baseline = _snapshot(hardware, swcs=swcs)
        task = {"runnables": [{"swc": name, "runnable": "R"} for name in ("One", "Two")]}

        assert len(_levels_spanned(task, baseline)) == 1
        task["runnables"].append({"swc": "Three", "runnable": "R"})
        assert len(_levels_spanned(task, baseline)) == 2


class TestThePartitionKeepsTheLayersApart:
    """The fold is hardware-shaped, so the entries that cannot survive it are routed round it.

    ``_group_by_element`` keys on the element name without a layer, looks every
    ``create`` up in the hardware layer and discards a ``remove``'s path; each
    case here is a defect that would cause.
    """

    def test_a_task_and_a_component_of_one_name_are_two_changes(self, hardware: tuple[Hardware, ...]) -> None:
        """A task and a component of one name stay two changes: the fold's key carries no layer."""
        delta = [
            _entry(
                "create",
                _address("swc", "Shared", "ports", {"member": "rIn"}, "measures", {"key": "value"}),
                after="measure",
            ),
            _entry("update", _address("tasks", "Shared", "coexistence"), before="", after="argued"),
        ]

        interfaces = [_interface("ISpeed", value="A")]
        runnables = [{"swc": "Hi", "runnable": "R"}, {"swc": "Lo", "runnable": "R"}]
        swcs = [_component("Hi", asil="D"), _component("Lo", asil="QM")]
        before_port = _port("rIn", "require", "ISpeed")
        after_port = _port("rIn", "require", "ISpeed", {"value": "measure"})
        changes = safety_verdict(
            delta,
            _snapshot(
                hardware,
                interfaces=interfaces,
                swcs=[*swcs, _component("Shared", asil="D", ports=[before_port])],
                tasks=[_task("Shared", runnables=runnables)],
            ),
            staged=_snapshot(
                hardware,
                interfaces=interfaces,
                swcs=[*swcs, _component("Shared", asil="D", ports=[after_port])],
                tasks=[_task("Shared", coexistence="argued", runnables=runnables)],
            ),
        ).changes

        assert [change.kind for change in changes] == [
            legacy_authorization.SafetyChangeKind.DECLARE_MEASURE,
            legacy_authorization.SafetyChangeKind.DECLARE_COEXISTENCE,
        ]

    def test_a_measures_create_never_reaches_the_hardware_lookup(self, hardware: tuple[Hardware, ...]) -> None:
        """A ``measures`` create answers a verdict rather than failing closed in the hardware lookup."""
        delta = [
            _entry(
                "create",
                _address("swc", "NotHardware", "ports", {"member": "rIn"}, "measures", {"key": "value"}),
                after="measure",
            )
        ]

        interfaces = [_interface("ISpeed", value="A")]
        staged_port = _port("rIn", "require", "ISpeed", {"value": "measure"})

        assert (
            len(
                safety_verdict(
                    delta,
                    _snapshot(
                        hardware,
                        interfaces=interfaces,
                        swcs=[_component("NotHardware", asil="D", ports=[_port("rIn", "require", "ISpeed")])],
                    ),
                    staged=_snapshot(
                        hardware,
                        interfaces=interfaces,
                        swcs=[_component("NotHardware", asil="D", ports=[staged_port])],
                    ),
                ).changes
            )
            == 1
        )

    def test_an_unlink_keeps_its_path(self, hardware: tuple[Hardware, ...]) -> None:
        """An unlink keeps its path, which the fold's `DELETE_ELEMENT` routing would discard."""
        delta = [_entry("remove", _address("swc", "Ctl", "satisfies", {"member": "REQ_1@1"}), before="REQ_1@1")]
        baseline = _snapshot(
            hardware, requirements=[_requirement("REQ_1", asil="D")], swcs=[_component("Ctl", satisfies=["REQ_1@1"])]
        )

        staged = _snapshot(hardware, requirements=[_requirement("REQ_1", asil="D")], swcs=[_component("Ctl")])

        (change,) = safety_verdict(delta, baseline, staged=staged).changes

        assert (change.field, change.old) == ("satisfies", "REQ_1@1")

    def test_a_hardware_act_and_a_declared_argument_are_both_reported(self, hardware: tuple[Hardware, ...]) -> None:
        """One change set, two layers, and neither hides the other."""
        delta = [
            _entry(
                "create",
                _address("swc", "Dst", "ports", {"member": "rIn"}, "measures", {"key": "value"}),
                after="measure",
            ),
            _entry("update", _address(HARDWARE_LAYER, "CORE0", "enforcement"), before="strict", after="advisory"),
        ]

        interfaces = [_interface("ISpeed", value="A")]
        kinds = [
            change.kind
            for change in safety_verdict(
                delta,
                _snapshot(
                    hardware,
                    interfaces=interfaces,
                    swcs=[_component("Dst", asil="D", ports=[_port("rIn", "require", "ISpeed")])],
                ),
                staged=_snapshot(
                    hardware,
                    interfaces=interfaces,
                    swcs=[_component("Dst", asil="D", ports=[_port("rIn", "require", "ISpeed", {"value": "measure"})])],
                ),
            ).changes
        ]

        assert legacy_authorization.SafetyChangeKind.DECLARE_MEASURE in kinds
        assert legacy_authorization.SafetyChangeKind.WEAKEN_ENFORCEMENT in kinds


# The outcome gate: what the model says, not what an address looks like


def _outcome_delta(
    tmp_path: Path,
    source: Path,
    edit: Callable[[Path], None],
    *,
    prepare: Callable[[Path], None] | None = None,
) -> tuple[list[DeltaEntry], ModelSnapshot, ModelSnapshot]:
    """:func:`_delta_over`, plus the staged snapshot: what the engine holds at step 5.

    *prepare* runs before the baseline snapshot, to give a case a baseline shape
    the committed fixture does not carry.
    """
    root = tmp_path / "model"
    shutil.copytree(source, root)
    if prepare is not None:
        prepare(root)
    baseline = load_snapshot(str(root))
    edit(root)
    staged = load_snapshot(str(root))
    return project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged), baseline, staged


def _prepared_rules(tmp_path: Path, prepare: Callable[[Path], None]) -> list[str]:
    """The safety rules the prepared baseline mints, over a copy of its own."""
    root = tmp_path / "prepared"
    shutil.copytree(SAFETY_MODEL_ROOT, root)
    prepare(root)
    return _safety_rules(root)


def _safety_rules(model_root: Path) -> list[str]:
    """Every ``safety.*`` rule the two safety checks mint over *model_root*."""
    return [
        record.rule
        for record in check_cross_layer_consistency(str(model_root)).records
        if record.rule.startswith("safety.")
    ]


def _rename_component(old: str, new: str) -> Callable[[Path], None]:
    """The shipped rename shape over the safety fixture: a ``remove``, a ``create``, every referrer.

    The protocol has no ``rename`` operation
    (``examples/rename-swc-via-remove-create-and-reference-updates.json``).
    """

    def edit(root: Path) -> None:
        document = _read_yaml(root / "swc" / "safety" / f"{old}.yaml")
        document["name"] = new
        _write_yaml(root / "swc" / "safety" / f"{new}.yaml", document)
        (root / "swc" / "safety" / f"{old}.yaml").unlink()
        for task in sorted((root / "tasks").glob("*.yaml"), key=lambda path: path.as_posix()):
            held = _read_yaml(task)
            for reference in held.get("runnables", []):
                if reference["swc"] == old:
                    reference["swc"] = new
            _write_yaml(task, held)
        composition = root / "compositions" / "Root.yaml"
        held = _read_yaml(composition)
        for member in held["members"]:
            if member["type"] == old:
                member["name"] = member["type"] = new
        _write_yaml(composition, held)

    return edit


class TestTheGuardAsksTheModelAndNotTheAddress:
    """The model decides, not the address: a withdrawal the model does not suffer asks nothing.

    :func:`~swcraft_tools.editing.safety_guard._decide` compares the rules'
    conditions on the baseline and the staged model owner by owner; the cases
    assert the property, not the mechanism. A level moved in place is decided
    as a value: ``pass_d`` leaves the staged model no finding to read.
    """

    def test_the_shipped_rename_shape_asks_nothing(self, tmp_path: Path) -> None:
        """Renaming ``TorqueLimiter`` with every referrer carried asks nothing.

        No false question is asked (a wrong reading yields six); false questions
        get a safety guard ignored.
        """
        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, _rename_component("TorqueLimiter", "TL2"))
        before = _safety_rules(SAFETY_MODEL_ROOT)
        after = _safety_rules(tmp_path / "model")

        assert sorted(before) == sorted(after), "the rename is a nil outcome or this case proves nothing"
        # The silence is structural: a `remove`/`create` pair identical up to
        # identity is a rename (`_detect_renames`), compared under one owner.
        assert _renames_of(delta) == {("element", "swc", "TL2"): "TorqueLimiter"}

    def test_a_removal_the_model_really_suffers_still_asks(self, tmp_path: Path) -> None:
        """The other side of the same test, so the gate is not just "always silent"."""
        delta, baseline, staged = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, _remove_file("swc/safety/TorqueLimiter.yaml")
        )

        kinds = {change.kind for change in safety_verdict(delta, baseline, staged=staged).changes}

        assert kinds == {legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION}

    def test_a_level_lowered_in_place_asks_although_no_finding_moves(self, tmp_path: Path) -> None:
        """Lowering ``ISafetySignals.torque`` from D to B moves no finding, and still asks.

        A level is compared as a value; a finding-based reading would be silent.
        """

        def lower(root: Path) -> None:
            document = _read_yaml(root / "interfaces" / "ISafetySignals.yaml")
            document["data_elements"][0]["asil"] = "B"
            _write_yaml(root / "interfaces" / "ISafetySignals.yaml", document)

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, lower)

        (change,) = safety_verdict(delta, baseline, staged=staged).changes
        assert (change.resource, change.field, change.old, change.new) == ("ISafetySignals.torque", "asil", "D", "B")

    def test_the_engine_hands_the_guard_the_staged_model(self) -> None:
        """The engine passes the staged snapshot to :func:`safety_verdict`, read off the source.

        A keyword the engine stopped passing would silently reopen every case above.
        """
        source = (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "editing" / "engine.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "safety_verdict"
        ]

        assert calls, "the engine no longer calls the guard at all"
        for call in calls:
            assert "staged" in {keyword.arg for keyword in call.keywords}


class TestASwitchMayNotAnswerASafetyFindingEither:
    """A port edit that makes an inert or unreadable ``measures`` map live is a weakening.

    Updating ``direction`` or ``interface`` never commits with an empty
    verdict. The importer holds the same rule (``component_exchange/import_.py``).
    The trigger admits every update inside a port bar the argument's text, and
    ``_classify_port_interior`` compares what a rule reads before and after. The
    shapes on the far side of the relation are
    ``TestTheDiscriminatorMintsWhatNoAddressArmProduces``'.
    """

    @staticmethod
    def _flip(
        tmp_path: Path, prepare: Callable[[Path], None], edit: Callable[[Path], None]
    ) -> tuple[list[str], list[str], SafetyVerdict]:
        """The safety rules the baseline mints, the ones the staged model mints, and the verdict."""
        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, edit, prepare=prepare)
        return (
            _prepared_rules(tmp_path, prepare),
            _safety_rules(tmp_path / "model"),
            safety_verdict(delta, baseline, staged=staged),
        )

    def test_flipping_a_provide_port_to_require_declares_the_map_it_carried(self, tmp_path: Path) -> None:
        """Flipping a ``provide`` port to ``require`` makes its standing map answer a finding."""

        def prepare(root: Path) -> None:
            document = _read_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml")
            port = next(candidate for candidate in document["ports"] if candidate["name"] == "rPlainMeasured")
            port["direction"] = "provide"
            _write_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml", document)

        def flip(root: Path) -> None:
            document = _read_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml")
            port = next(candidate for candidate in document["ports"] if candidate["name"] == "rPlainMeasured")
            port["direction"] = "require"
            _write_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml", document)

        before, after, verdict = self._flip(tmp_path, prepare, flip)

        # Counted: the fixture carries a second, unrelated one.
        assert before.count("safety.measure-on-provide-port") == after.count("safety.measure-on-provide-port") + 1
        (change,) = [c for c in verdict.changes if c.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE]
        assert (change.resource, change.field) == ("TorqueLimiter.rPlainMeasured", "measures")
        assert change.new == {"status": "End-to-end protection profile 4 on the status signal."}

    def test_repointing_the_interface_so_a_dangling_key_resolves_declares_it(self, tmp_path: Path) -> None:
        """Re-pointing a port so its dangling map key resolves makes the map answer a finding."""

        def prepare(root: Path) -> None:
            _write_yaml(
                root / "interfaces" / "IOtherSignals.yaml",
                {
                    "name": "IOtherSignals",
                    "kind": "sender_receiver",
                    "description": "Declares the element TorqueLimiter's rPlain port measures under another name.",
                    "data_elements": [{"name": "debug", "implementation_type": "uint8"}],
                },
            )
            document = _read_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml")
            port = next(candidate for candidate in document["ports"] if candidate["name"] == "rPlainMeasured")
            port["interface"] = "IOtherSignals"
            _write_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml", document)

        def repoint(root: Path) -> None:
            document = _read_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml")
            port = next(candidate for candidate in document["ports"] if candidate["name"] == "rPlainMeasured")
            port["interface"] = "IPlainSignals"
            _write_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml", document)

        before, after, verdict = self._flip(tmp_path, prepare, repoint)

        assert "safety.measure-unknown-element" in before
        assert [
            change.resource
            for change in verdict.changes
            if change.kind is legacy_authorization.SafetyChangeKind.DECLARE_MEASURE
        ] == ["TorqueLimiter.rPlainMeasured"]

    def test_rewording_a_measure_is_still_not_a_weakening(self, tmp_path: Path) -> None:
        """Rewording a measure is no weakening, asserted on the verdict."""

        def reword(root: Path) -> None:
            document = _read_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml")
            port = next(candidate for candidate in document["ports"] if candidate["name"] == "rPlainMeasured")
            port["measures"]["status"] = "End-to-end protection profile 8 on the status signal."
            _write_yaml(root / "swc" / "safety" / "TorqueLimiter.yaml", document)

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, reword)

        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_data_element_named_coexistence_invents_no_task_argument(self, tmp_path: Path) -> None:
        """A data element named ``coexistence`` invents no task argument: the walk keys on the layer.

        Keyed on the field name, it rendered a false task line about a port.
        """

        def declare(root: Path) -> None:
            interfaces = root / "interfaces" / "IPlainSignals.yaml"
            document = _read_yaml(interfaces)
            document["data_elements"].append({"name": "coexistence", "implementation_type": "uint8", "asil": "A"})
            _write_yaml(interfaces, document)
            _write_yaml(
                root / "swc" / "safety" / "Watcher.yaml",
                {
                    "name": "Watcher",
                    "type": "application",
                    "asil": "D",
                    "ports": [
                        {
                            "name": "rNew",
                            "direction": "require",
                            "interface": "IPlainSignals",
                            "measures": {
                                "status": "E2E on status.",
                                "coexistence": "watchdog on the coexistence signal",
                            },
                        }
                    ],
                    "runnables": [{"name": "Watch", "events": [{"type": "timing", "period": 0.02}]}],
                    "satisfies": ["REQ-SAFETY-001@1"],
                },
            )
            composition = root / "compositions" / "Root.yaml"
            held = _read_yaml(composition)
            held["members"].append({"name": "Watcher", "type": "Watcher"})
            _write_yaml(composition, held)

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, declare)

        changes = safety_verdict(delta, baseline, staged=staged).changes
        assert [change.kind for change in changes] == [legacy_authorization.SafetyChangeKind.DECLARE_MEASURE]
        assert changes[0].resource == "Watcher.rNew"


class TestTheTwoWAYSToWithdrawAnAllocationAgree:
    """A requirement removed whole and its one link removed are one outcome, with one verdict.

    The two arms answer it alike.
    """

    @staticmethod
    def _prepare(keep_a_duplicate: bool) -> Callable[[Path], None]:
        """A baseline where ``PlainWorker`` derives its level from ``REQ-PW``, and optionally a duplicate.

        Its own ``asil`` is stripped so the requirement is the only bearer; a
        second requirement holding the level makes both ways silent.
        """

        def prepare(root: Path) -> None:
            identities = ("REQ-PW", *(("REQ-PW-DUP",) if keep_a_duplicate else ()))
            worker = root / "swc" / "safety" / "PlainWorker.yaml"
            document = _read_yaml(worker)
            del document["asil"]
            # The component names what it satisfies, no longer REQ-SAFETY-002.
            document["satisfies"] = [f"{identity}@1" for identity in identities]
            _write_yaml(worker, document)
            for requirement_id in identities:
                _write_yaml(
                    root / "requirements" / f"{requirement_id}.yaml",
                    {
                        "id": requirement_id,
                        "title": f"{requirement_id} allocates ASIL B to PlainWorker",
                        "description": "The component declares no level of its own and derives this one.",
                        "req_type": "req",
                        "version": 1,
                        "source": "architecture",
                        "asil": "B",
                    },
                )

        return prepare

    @classmethod
    def _both_ways(cls, tmp_path: Path, keep_a_duplicate: bool) -> tuple[SafetyVerdict, SafetyVerdict]:
        prepare = cls._prepare(keep_a_duplicate)

        def removed_whole(root: Path) -> None:
            (root / "requirements" / "REQ-PW.yaml").unlink()

        def unlinked(root: Path) -> None:
            worker = root / "swc" / "safety" / "PlainWorker.yaml"
            held = _read_yaml(worker)
            held["satisfies"] = [link for link in held["satisfies"] if link != "REQ-PW@1"]
            _write_yaml(worker, held)

        whole = _outcome_delta(tmp_path / "whole", SAFETY_MODEL_ROOT, removed_whole, prepare=prepare)
        link = _outcome_delta(tmp_path / "link", SAFETY_MODEL_ROOT, unlinked, prepare=prepare)
        return (
            safety_verdict(whole[0], whole[1], staged=whole[2]),
            safety_verdict(link[0], link[1], staged=link[2]),
        )

    def test_neither_asks_when_another_requirement_holds_the_level(self, tmp_path: Path) -> None:
        whole, link = self._both_ways(tmp_path, keep_a_duplicate=True)

        assert whole.changes == ()
        assert link.changes == ()

    def test_both_ask_when_nothing_else_holds_it(self, tmp_path: Path) -> None:
        whole, link = self._both_ways(tmp_path, keep_a_duplicate=False)

        assert {change.kind for change in whole.changes} == {legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION}
        assert {change.kind for change in link.changes} == {legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION}


# The gate mints, and it asks per candidate


def _rate_element(interface: str, element: str, level: str) -> Callable[[Path], None]:
    """Give one data element a level it did not carry."""

    def edit(root: Path) -> None:
        document = _read_yaml(root / "interfaces" / f"{interface}.yaml")
        for member in document["data_elements"]:
            if member["name"] == element:
                member["asil"] = level
        _write_yaml(root / "interfaces" / f"{interface}.yaml", document)

    return edit


def _drop_measure(component: str, port: str, key: str) -> Callable[[Path], None]:
    """Take one key out of one port's ``measures`` map."""

    def edit(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        document = _read_yaml(path)
        for member in document["ports"]:
            if member["name"] == port:
                member["measures"].pop(key, None)
                if not member["measures"]:
                    del member["measures"]
        _write_yaml(path, document)

    return edit


def _remove_port(component: str, port: str) -> Callable[[Path], None]:
    """Take one whole port off one component."""

    def edit(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        document = _read_yaml(path)
        document["ports"] = [member for member in document["ports"] if member["name"] != port]
        _write_yaml(path, document)

    return edit


def _repoint_port(component: str, port: str, **fields: Any) -> Callable[[Path], None]:
    """Write one port's own fields — its ``direction``, its ``interface`` — and drop its map."""

    def edit(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        document = _read_yaml(path)
        for member in document["ports"]:
            if member["name"] == port:
                member.update(fields)
                member.pop("measures", None)
        _write_yaml(path, document)

    return edit


def _join_task(task: str, component: str, runnable: str) -> Callable[[Path], None]:
    """Schedule one more runnable in one task."""

    def edit(root: Path) -> None:
        path = root / "tasks" / f"{task}.yaml"
        document = _read_yaml(path)
        document["runnables"].append({"swc": component, "runnable": runnable})
        _write_yaml(path, document)

    return edit


def _leave_task(task: str, component: str, runnable: str) -> Callable[[Path], None]:
    """Take one runnable out of one task's schedule."""

    def edit(root: Path) -> None:
        path = root / "tasks" / f"{task}.yaml"
        document = _read_yaml(path)
        document["runnables"] = [
            reference
            for reference in document["runnables"]
            if (reference["swc"], reference["runnable"]) != (component, runnable)
        ]
        _write_yaml(path, document)

    return edit


def _second_runnable(component: str, runnable: str) -> Callable[[Path], None]:
    """Give one component one more runnable, scheduled nowhere, which mints nothing."""

    def edit(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        document = _read_yaml(path)
        document["runnables"].append({"name": runnable, "events": [{"type": "timing", "period": 0.2}]})
        _write_yaml(path, document)

    return edit


def _both(*edits: Callable[[Path], None]) -> Callable[[Path], None]:
    """One change set carrying every one of *edits*."""

    def edit(root: Path) -> None:
        for one in edits:
            one(root)

    return edit


#: The two readable measures ``TorqueLimiter`` carries in the committed fixture,
#: spelled once for the cases that assert them re-declared under a new name.
_DEBUG_MEASURE = "Watchdog on the debug channel — a measure declared ahead of need."
_STATUS_MEASURE = "End-to-end protection profile 4 on the status signal."

#: The argument ``Task_Argued`` carries in the committed fixture. Free prose
#: names no level pair, so the text does not record its members' levels.
_ARGUED_TEXT = (
    "The two components run in separate memory partitions with timing protection; "
    "the argument is recorded in the item's safety case."
)

#: The safety fixture's partition argument, on ``P_Argued``.
_PARTITION_ARGUED_TEXT = (
    "The partition's components are placed in separate memory regions under memory protection; the argument "
    "is recorded in the item's safety case."
)


def _renames_of(delta: Iterable[DeltaEntry]) -> dict[tuple[Any, ...], str]:
    """The rename pairs :func:`_detect_renames` reads out of *delta*, staged name to baseline name.

    The rename is the guard's only exemption, so it is asserted directly, not
    only through a silence.
    """
    from swcraft_tools.editing.safety_guard import _detect_renames

    return dict(_detect_renames(list(delta)).backward)


def _verdict(
    tmp_path: Path,
    edit: Callable[[Path], None],
    *,
    prepare: Callable[[Path], None] | None = None,
) -> tuple[tuple[str, str, Any, Any], ...]:
    """The shipped verdict over the safety fixture, as ``(kind, resource, old, new)`` rows.

    *prepare* is :func:`_outcome_delta`'s.
    """
    delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, edit, prepare=prepare)
    return tuple(
        (change.kind.value, change.resource, change.field, change.old, change.new)
        for change in safety_verdict(delta, baseline, staged=staged).changes
    )


class TestTheDiscriminatorMintsWhatNoAddressArmProduces:
    """An outcome may mint a candidate no address arm produces: a standing argument going live.

    Without it such a change set committed with an empty verdict. The two
    families are not one rule: a ``measures`` map is compared as the set of
    keys it answers for, so answering one more element asks; a task has no
    level of its own, so a mix moving under a live ``coexistence`` asks nothing.
    """

    def test_rating_an_element_under_a_standing_measure_asks(self, tmp_path: Path) -> None:
        """Rating ``debug``, which ``TorqueLimiter.rPlain`` already measures, asks.

        The one entry, a raised data-element level, is no weakening itself and
        no address arm produces a candidate.
        """
        assert _verdict(tmp_path, _rate_element("IPlainSignals", "debug", "A")) == (
            (
                "declare_measure",
                "TorqueLimiter.rPlain",
                "measures",
                None,
                {"debug": "Watchdog on the debug channel — a measure declared ahead of need."},
            ),
        )

    def test_the_control_shows_the_standing_measure_is_what_answers_it(self, tmp_path: Path) -> None:
        """Without the map the same rating mints the finding, so the map really answers one."""
        with_map = _findings_after(tmp_path / "with", _rate_element("IPlainSignals", "debug", "A"))
        without = _findings_after(
            tmp_path / "without",
            _both(_rate_element("IPlainSignals", "debug", "A"), _drop_measure("TorqueLimiter", "rPlain", "debug")),
        )

        closed = Counter(without) - Counter(with_map)
        assert dict(closed) == {"safety.consumer-above-element": 1}, (with_map, without)

    def test_a_QM_component_joining_an_ARGUED_task_asks(self, tmp_path: Path) -> None:
        """A ``QM`` component joining ``Task_Homogeneous`` makes it mixed, and the standing text goes live.

        The argument going live and the argument declared are the family's two
        questions; a task mixed on both sides has not changed.
        """
        assert _verdict(tmp_path, _join_task("Task_Homogeneous", "DiagnosticsAgent", "Diagnose")) == (
            (
                "declare_coexistence",
                "Task_Homogeneous",
                "coexistence",
                "",
                "Written ahead of a re-rating; nothing reads it while the task is homogeneous.",
            ),
        )

    def test_the_control_shows_the_standing_text_is_what_answers_it(self, tmp_path: Path) -> None:
        """Without the text the same movement mints ``safety.task-mixed-asil``."""

        def without_the_text(root: Path) -> None:
            _join_task("Task_Homogeneous", "DiagnosticsAgent", "Diagnose")(root)
            document = _read_yaml(root / "tasks" / "Task_Homogeneous.yaml")
            del document["coexistence"]
            _write_yaml(root / "tasks" / "Task_Homogeneous.yaml", document)

        with_text = _findings_after(tmp_path / "with", _join_task("Task_Homogeneous", "DiagnosticsAgent", "Diagnose"))
        without = _findings_after(tmp_path / "without", without_the_text)

        closed = Counter(without) - Counter(with_text)
        assert dict(closed) == {"safety.task-mixed-asil": 1}, (with_text, without)

    def test_a_level_ADDED_to_an_already_argued_mix_asks_NOTHING(self, tmp_path: Path) -> None:
        """A level added to an already argued mix asks nothing.

        ``safety.task-mixed-asil`` judges only whether the task is mixed and
        argued; the model does not record which levels an argument was written
        about, so the reviewer could not be shown the change.
        """
        rows = _verdict(
            tmp_path,
            _join_task("Task_Argued", "DiagnosticsAgent", "DiagnoseSlow"),
            prepare=_second_runnable("DiagnosticsAgent", "DiagnoseSlow"),
        )

        assert rows == ()

    def test_the_argued_PAIR_REPLACED_asks_NOTHING_EITHER(self, tmp_path: Path) -> None:
        """The argued task's B member replaced by a ``QM`` one asks nothing.

        The text names no level pair, so the task is mixed and argued before
        and after, which is all rule 5 can see.
        """
        rows = _verdict(
            tmp_path,
            _both(
                _join_task("Task_Argued", "DiagnosticsAgent", "DiagnoseSlow"),
                _leave_task("Task_Argued", "SpeedSensor", "SampleSpeed"),
            ),
            prepare=_second_runnable("DiagnosticsAgent", "DiagnoseSlow"),
        )

        assert rows == ()

    def test_a_members_level_RAISED_inside_an_argued_mix_asks_NOTHING(self, tmp_path: Path) -> None:
        """Raising ``SpeedSensor``'s level inside ``Task_Argued`` asks nothing.

        A task has no level of its own. Lowering a member does ask, at the
        component's own owner (the neighbouring class).
        """
        assert _verdict(tmp_path, _declare_component_level("SpeedSensor", "C")) == ()

    def test_a_member_that_adds_no_LEVEL_to_the_mix_asks_nothing(self, tmp_path: Path) -> None:
        """``PlainWorker`` (B, which the mix spans) joining ``Task_Argued`` asks nothing.

        The four member movements are enumerated so a change names which one asks.
        """
        assert _verdict(tmp_path, _join_task("Task_Argued", "PlainWorker", "WorkSlow")) == ()

    def test_a_declaration_an_address_arm_DOES_attribute_is_asked_once(self, tmp_path: Path) -> None:
        """A new measure the shape arm attributes is asked once: the mint is a remainder."""

        def declare(root: Path) -> None:
            path = root / "swc" / "safety" / "PlainWorker.yaml"
            document = _read_yaml(path)
            for member in document["ports"]:
                if member["name"] == "rPlain":
                    member["measures"] = {"status": "A newly written end-to-end profile."}
            _write_yaml(path, document)

        assert _verdict(tmp_path, declare) == (
            (
                "declare_measure",
                "PlainWorker.rPlain[status]",
                "measures",
                None,
                "A newly written end-to-end profile.",
            ),
        )


class TestBundlingCanNeitherCancelAQuestionNorInventOne:
    """The comparison is per candidate, so bundling neither cancels nor invents a question.

    Model-wide counters would let an unrelated removal or raise cancel a real
    question, and a nil rename invent one. Each bundle must say what its two
    movements say apart.
    """

    def test_an_unrelated_withdrawal_does_not_cancel_a_mint(self, tmp_path: Path) -> None:
        rated = _rate_element("IPlainSignals", "debug", "A")
        withdrawn = _remove_port("PlainWorker", "rPlainBlank")

        alone = _verdict(tmp_path / "alone", rated)
        other = _verdict(tmp_path / "other", withdrawn)
        bundled = _verdict(tmp_path / "bundled", _both(rated, withdrawn))

        assert alone == (
            (
                "declare_measure",
                "TorqueLimiter.rPlain",
                "measures",
                None,
                {"debug": "Watchdog on the debug channel — a measure declared ahead of need."},
            ),
        )
        assert other == (("lower_allocation", "PlainWorker.rPlainBlank", "measures", ["status"], None),)
        assert sorted(bundled) == sorted(alone + other)

    def test_an_unrelated_rating_does_not_cancel_a_withdrawal(self, tmp_path: Path) -> None:
        """An unclassified level raise beside a withdrawal does not silence the withdrawal."""
        withdrawn = _remove_port("TorqueLimiter", "rPlainMeasured")
        raised = _rate_element("ISafetySignals", "raw", "D")

        alone = _verdict(tmp_path / "alone", withdrawn)
        bundled = _verdict(tmp_path / "bundled", _both(withdrawn, raised))

        assert alone == (("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),)
        assert bundled == alone

    def test_a_nil_rename_bundled_with_a_real_withdrawal_invents_no_line(self, tmp_path: Path) -> None:
        """A nil rename of ``PlainWorker`` beside a real withdrawal: only the departed port is named."""
        renamed = _rename_component("PlainWorker", "PW2")
        withdrawn = _remove_port("TorqueLimiter", "rPlainMeasured")

        assert _verdict(tmp_path / "rename", renamed) == ()
        assert _verdict(tmp_path / "bundled", _both(renamed, withdrawn)) == (
            ("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),
        )

    def test_a_rename_bundled_with_a_new_declaration_asks_once(self, tmp_path: Path) -> None:
        """A renamed port re-declares the map it already had; only the new one is a declaration."""

        def declare(root: Path) -> None:
            path = root / "swc" / "safety" / "PlainWorker.yaml"
            document = _read_yaml(path)
            for member in document["ports"]:
                if member["name"] == "rPlain":
                    member["measures"] = {"status": "A newly written end-to-end profile."}
            _write_yaml(path, document)

        renamed = _rename_component("TorqueLimiter", "TL2")

        assert _verdict(tmp_path / "rename", renamed) == ()
        assert _verdict(tmp_path / "bundled", _both(renamed, declare)) == (
            ("declare_measure", "PlainWorker.rPlain[status]", "measures", None, "A newly written end-to-end profile."),
        )


class TestABearerRemovedWholeIsAnACTAndNotACounter:
    """A bearer removed whole asks, answered from the removed element, not from finding counts.

    The model is asked only whether an element carrying the same thing is still
    there, which tells a withdrawal from a rename.
    """

    @staticmethod
    def _with_a_plain_bearer(root: Path) -> None:
        """A component that declares ASIL D, carries no port and answers no rule."""
        _write_component(
            root,
            {
                "name": "Auxiliary",
                "type": "application",
                "description": "Declares ASIL D and consumes nothing, so it contributes no finding.",
                "asil": "D",
                "runnables": [{"name": "AuxWork", "events": [{"type": "timing", "period": 0.02}]}],
                "handle_termination_and_restart": "no_support",
                "satisfies": ["REQ-SAFETY-001@1"],
            },
        )
        composition = _read_yaml(root / "compositions" / "Root.yaml")
        composition["members"].append({"name": "Auxiliary", "type": "Auxiliary"})
        _write_yaml(root / "compositions" / "Root.yaml", composition)

    def test_a_bearer_that_moves_no_finding_still_asks(self, tmp_path: Path) -> None:
        delta, baseline, staged = _outcome_delta(
            tmp_path,
            SAFETY_MODEL_ROOT,
            _remove_component("Auxiliary"),
            prepare=self._with_a_plain_bearer,
        )
        before = _prepared_rules(tmp_path / "before", self._with_a_plain_bearer)
        after = _safety_rules(tmp_path / "model")

        assert sorted(before) == sorted(after), "the removal must move no finding, or this case proves nothing"
        assert [
            (change.resource, change.field, change.old)
            for change in safety_verdict(delta, baseline, staged=staged).changes
        ] == [("Auxiliary", "asil", "D")]

    def test_lowering_a_components_own_level_asks_although_it_is_renamed_in_the_same_change_set(
        self, tmp_path: Path
    ) -> None:
        """A level lowered in place and lowered under a rename both ask.

        Decided by whether an element carrying ASIL D is still there.
        """

        def rename_and_lower(root: Path) -> None:
            _rename_component("TorqueLimiter", "TL2")(root)
            document = _read_yaml(root / "swc" / "safety" / "TL2.yaml")
            document["asil"] = "QM"
            _write_yaml(root / "swc" / "safety" / "TL2.yaml", document)

        # One line, the component's: the mix of `Task_Argued` moves under a
        # live argument, which asks nothing.
        in_place = _verdict(tmp_path / "in-place", _declare_component_level("TorqueLimiter", "QM"))
        assert in_place == (("lower_allocation", "TorqueLimiter", "asil", "D", "QM"),)
        # A rename bundled with an edit is a removal and a creation, so every
        # owner the removal empties asks (the fail-safe direction): the level,
        # both readable maps, and `TL2`'s two declarations.
        assert _verdict(tmp_path / "renamed", rename_and_lower) == (
            ("lower_allocation", "TorqueLimiter", "asil", "D", None),
            ("lower_allocation", "TorqueLimiter.rPlain", "measures", ["debug"], None),
            ("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),
            ("declare_measure", "TL2.rPlain", "measures", None, {"debug": _DEBUG_MEASURE}),
            ("declare_measure", "TL2.rPlainMeasured", "measures", None, {"status": _STATUS_MEASURE}),
        )


def _declare_component_level(component: str, level: str) -> Callable[[Path], None]:
    """Write one component's own ``asil``, in either direction."""

    def edit(root: Path) -> None:
        path = root / "swc" / "safety" / f"{component}.yaml"
        document = _read_yaml(path)
        document["asil"] = level
        _write_yaml(path, document)

    return edit


class TestTheThreeAddressesOfOneWithdrawalAgree:
    """A measured ``require`` port stops being one: three addresses, one answer.

    Removing, flipping to ``provide`` and re-pointing all withdraw what the
    removal withdraws, compared on what a rule reads off the port both ways.
    """

    EXPECTED = (("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),)

    def test_removing_the_port(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _remove_port("TorqueLimiter", "rPlainMeasured")) == self.EXPECTED

    def test_flipping_it_to_provide(self, tmp_path: Path) -> None:
        assert (
            _verdict(tmp_path, _repoint_port("TorqueLimiter", "rPlainMeasured", direction="provide")) == self.EXPECTED
        )

    def test_re_pointing_it_at_an_interface_that_declares_nothing(self, tmp_path: Path) -> None:
        assert (
            _verdict(tmp_path, _repoint_port("TorqueLimiter", "rPlainMeasured", interface="IDiagnostics"))
            == self.EXPECTED
        )


class TestAnUnlinkAsksWhateverTheComponentDECLARES:
    """An unlink asks even where the component declares its own level at or above the withdrawn one.

    The allocation is an act, not re-decided against a finding set.
    """

    def test_unlinking_a_component_that_declares_the_same_level_asks(self, tmp_path: Path) -> None:
        def unlink(root: Path) -> None:
            worker = root / "swc" / "safety" / "PlainWorker.yaml"
            held = _read_yaml(worker)
            held["satisfies"] = [link for link in held["satisfies"] if link != "REQ-SAFETY-002@1"]
            _write_yaml(worker, held)

        assert _verdict(tmp_path, unlink) == (
            ("lower_allocation", "PlainWorker", "satisfies", "REQ-SAFETY-002@1", None),
        )

    def test_a_component_the_staged_model_no_longer_holds_withdraws_nothing_through_its_links(
        self, tmp_path: Path
    ) -> None:
        """An allocation to a component gone from the staged model protects nothing.

        A deleted component is asked about as the component, not per requirement.
        """
        assert _verdict(tmp_path / "renamed", _rename_component("SpeedSensor", "SS2")) == ()
        assert [row[1] for row in _verdict(tmp_path / "deleted", _remove_component("TorqueLimiter"))] == [
            "TorqueLimiter",
            "TorqueLimiter.rPlain",
            "TorqueLimiter.rPlainMeasured",
        ]


class TestAPortsAndARunnablesUnlinkAskLikeTheComponents:
    """A link on a component, a port or a runnable asks the same question.

    :func:`~swcraft_tools.trace.component_requirement_ids` reads all three sites;
    only the resource differs, naming the list that lost the link.
    """

    @pytest.mark.parametrize("site", _MEMBER_SITES, ids=["a-port", "a-runnable"])
    def test_the_member_sites_unlink_asks_where_the_maximum_drops(self, tmp_path: Path, site: tuple[str, str]) -> None:
        """``SpeedSensor`` derives ``B`` from the one link, so removing it drops the maximum to ``QM``."""
        resource = f"SpeedSensor.{site[0]}[{site[1]}]"
        delta, baseline, staged = _outcome_delta(
            tmp_path,
            SAFETY_MODEL_ROOT,
            _satisfies_at("SpeedSensor", site, lambda held: [link for link in held if link != _SPEED_LINK]),
            prepare=_moved_to(site),
        )

        (change,) = safety_verdict(delta, baseline, staged=staged).changes

        assert (change.kind, change.resource, change.field, change.old, change.new) == (
            legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION,
            resource,
            "satisfies",
            _SPEED_LINK,
            None,
        )
        assert change.describe().startswith(f"'{resource}': satisfies {_SPEED_LINK} -> none — ")

    @pytest.mark.parametrize("site", _MEMBER_SITES, ids=["a-port", "a-runnable"])
    def test_the_member_sites_unlink_asks_nothing_where_another_requirement_keeps_the_level(
        self, tmp_path: Path, site: tuple[str, str]
    ) -> None:
        """The negative half: a second requirement at ``B`` on the component's own list keeps the maximum."""

        def second_requirement(root: Path) -> None:
            _write_yaml(
                root / "requirements" / "REQ-SAFETY-004.yaml",
                {
                    "id": "REQ-SAFETY-004",
                    "title": "Speed plausibility",
                    "description": "Allocates ASIL B to the speed sensor a second time.",
                    "req_type": "req",
                    "version": 1,
                    "source": "architecture",
                    "asil": "B",
                },
            )
            _satisfies_at("SpeedSensor", None, lambda held: [*held, "REQ-SAFETY-004@1"])(root)

        assert (
            _verdict(
                tmp_path,
                _satisfies_at("SpeedSensor", site, lambda held: [link for link in held if link != _SPEED_LINK]),
                prepare=_both(_moved_to(site), second_requirement),
            )
            == ()
        )

    @pytest.mark.parametrize("site", _MEMBER_SITES, ids=["a-port", "a-runnable"])
    def test_removing_the_whole_member_that_carries_the_only_link_asks(
        self, tmp_path: Path, site: tuple[str, str]
    ) -> None:
        """The port or the runnable itself removed, its link with it: the maximum drops as for the member alone."""

        def drop_the_member(root: Path) -> None:
            path = root / "swc" / "safety" / "SpeedSensor.yaml"
            held = _read_yaml(path)
            held[site[0]] = [member for member in held[site[0]] if member["name"] != site[1]]
            if not held[site[0]]:
                del held[site[0]]
            _write_yaml(path, held)

        verdict = _verdict(tmp_path, drop_the_member, prepare=_moved_to(site))

        assert [
            (kind, field, old, new)
            for kind, resource, field, old, new in verdict
            if resource.startswith("SpeedSensor")
            and kind == legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION.value
        ] == [(legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION.value, "asil", "B", "QM")], verdict


#: One element of each linkable layer that allocates nothing, with its file.
_NON_COMPONENT_BEARERS = [
    "tasks/Task_Homogeneous.yaml",
    "compositions/Root.yaml",
    "interfaces/ISafetySignals.yaml",
    "hardware/CORE0.yaml",
]


@pytest.mark.parametrize("relative", _NON_COMPONENT_BEARERS, ids=["task", "composition", "interface", "hardware"])
def test_removing_a_link_from_an_element_that_allocates_nothing_asks_nothing(tmp_path: Path, relative: str) -> None:
    """A link removed from a non-component element asks nothing: only components allocate a level."""

    def set_links(links: list[str]) -> Callable[[Path], None]:
        def apply(root: Path) -> None:
            document = _read_yaml(root / relative)
            if links:
                document["satisfies"] = links
            else:
                document.pop("satisfies", None)
            _write_yaml(root / relative, document)

        return apply

    assert _verdict(tmp_path, set_links([]), prepare=set_links([_SPEED_LINK])) == ()


class TestAMovementThatWithdrawsNoAllocationAsksNothing:
    """``satisfies`` movements that leave the allocated maximum unchanged ask nothing."""

    def test_a_confirmation_edit_asks_nothing(self, tmp_path: Path) -> None:
        """``@1`` -> ``@2`` on each bearer, with the requirement's ``version``: the allocation is unchanged."""
        bump = _both(
            _edit_requirement("REQ-SAFETY-002", version=2),
            *(
                _satisfies_at(
                    bearer, None, lambda held: ["REQ-SAFETY-002@2" if link == _SPEED_LINK else link for link in held]
                )
                for bearer in ("SpeedSensor", "PlainWorker")
            ),
        )
        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, bump)

        assert {(entry.operation, str(entry.address.element)) for entry in delta if entry.address.layer == "swc"} == {
            ("remove", "SpeedSensor"),
            ("create", "SpeedSensor"),
            ("remove", "PlainWorker"),
            ("create", "PlainWorker"),
        }, "the confirmation must reach the guard as the unlink-shaped remove it is"
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_renamed_requirement_with_every_bearers_member_moved_asks_nothing(self, tmp_path: Path) -> None:
        """A renamed requirement with every bearer's member moved: the staged allocation still holds ``B``."""

        def rename(root: Path) -> None:
            held = _read_yaml(root / "requirements" / "REQ-SAFETY-002.yaml")
            held["id"] = "REQ-SAFETY-020"
            _write_yaml(root / "requirements" / "REQ-SAFETY-020.yaml", held)
            (root / "requirements" / "REQ-SAFETY-002.yaml").unlink()
            for bearer in ("SpeedSensor", "PlainWorker"):
                _satisfies_at(
                    bearer, None, lambda links: ["REQ-SAFETY-020@1" if link == _SPEED_LINK else link for link in links]
                )(root)

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, rename)

        assert ("element", "requirements", "REQ-SAFETY-020") in _renames_of(delta)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_the_same_id_left_on_another_site_of_the_component_asks_nothing(self, tmp_path: Path) -> None:
        """The id kept on a port: the remaining level is the staged allocation, not the baseline minus the id."""
        assert (
            _verdict(
                tmp_path,
                _satisfies_at("SpeedSensor", None, lambda held: [link for link in held if link != _SPEED_LINK]),
                prepare=_satisfies_at("SpeedSensor", ("ports", "pSafety"), lambda held: [*held, _SPEED_LINK]),
            )
            == ()
        )

    @pytest.mark.parametrize(
        ("edit", "prepare"),
        [
            (_edit_requirement("REQ-SAFETY-002", version=2), None),
            (_edit_requirement("REQ-SAFETY-002", source="external"), None),
            (_edit_requirement("REQ-SAFETY-002", derived_from=["REQ-SAFETY-001@1"]), None),
            (
                _edit_requirement("REQ-SAFETY-002", derived_from=None),
                _edit_requirement("REQ-SAFETY-002", derived_from=["REQ-SAFETY-001@1"]),
            ),
        ],
        ids=["version", "source", "derived-from-added", "derived-from-withdrawn"],
    )
    def test_a_derived_from_version_or_source_edit_asks_nothing(
        self, tmp_path: Path, edit: Callable[[Path], None], prepare: Callable[[Path], None] | None
    ) -> None:
        """No ``derived_from``, ``version`` or ``source`` edit is a safety act: only the ASIL allocation is."""
        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, edit, prepare=prepare)

        assert delta, "the edit must actually produce a delta, or the silence proves nothing"
        assert safety_verdict(delta, baseline, staged=staged).changes == ()


#: Every element of the committed safety fixture, removed whole one at a time,
#: with the verdict written out by hand. Three rows worth naming:
#:
#: * ``interfaces/IPlainSignals.yaml`` withdraws the rated element and the three
#:   measure maps readable through it — lines no delta entry addresses, so
#:   :func:`_decide`'s own;
#: * ``swc/safety/DiagnosticsAgent.yaml`` is silent: it derives ``QM`` and both
#:   its maps are unreadable;
#: * ``tasks/Task_Mixed.yaml`` is silent although its deletion silences a
#:   ``safety.task-mixed-asil`` finding: it carried no argument (the limit in
#:   ``_withdrawn_by_removing_task``).
FIXTURE_REMOVALS: dict[str, list[tuple[str, str, str, Any, Any]]] = {
    "compositions/Root.yaml": [],
    "data_types/implementation/uint8.yaml": [],
    "data_types/base/uint8_base.yaml": [],
    "hardware/CPU.yaml": [],
    # Both rated `B`; a rated element's removal renders its capability after the budget.
    "hardware/CORE0.yaml": [
        ("delete_resource", "CORE0", "", "kind=core, threshold=0.7, enforcement=strict, asil_capability=B", None)
    ],
    "hardware/RAM0.yaml": [
        (
            "delete_resource",
            "RAM0",
            "",
            "kind=memory, size=4096, threshold=0.5, enforcement=strict, asil_capability=B",
            None,
        )
    ],
    # One partition per task, each rated `D`. The hardware fold decides only a
    # `Core` or a `Memory`; the comparison asks a partition's level withdrawn,
    # and its argument where it really was mixed (`P_Argued`: D and derived B).
    "hardware/P_Argued.yaml": [
        ("lower_allocation", "P_Argued", "asil", "D", None),
        ("lower_allocation", "P_Argued", "coexistence", _PARTITION_ARGUED_TEXT, None),
    ],
    "hardware/P_Homogeneous.yaml": [("lower_allocation", "P_Homogeneous", "asil", "D", None)],
    "hardware/P_Mixed.yaml": [("lower_allocation", "P_Mixed", "asil", "D", None)],
    "interfaces/IDiagnostics.yaml": [],
    "interfaces/IPlainSignals.yaml": [
        ("lower_allocation", "IPlainSignals.status", "asil", "A", None),
        ("lower_allocation", "PlainWorker.rPlainBlank", "measures", ["status"], None),
        ("lower_allocation", "TorqueLimiter.rPlain", "measures", ["debug"], None),
        ("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),
    ],
    "interfaces/ISafetySignals.yaml": [
        ("lower_allocation", "ISafetySignals.torque", "asil", "D", None),
        ("lower_allocation", "ISafetySignals.speed", "asil", "B", None),
    ],
    "requirements/REQ-SAFETY-001.yaml": [("lower_allocation", "REQ-SAFETY-001", "asil", "D", None)],
    # One line: `SpeedSensor` drops from B to QM. The task mix it moves adds
    # nothing: a task has no level of its own.
    "requirements/REQ-SAFETY-002.yaml": [("lower_allocation", "REQ-SAFETY-002", "asil", "B", None)],
    "requirements/REQ-SAFETY-003.yaml": [],
    "swc/safety/DiagnosticsAgent.yaml": [],
    "swc/safety/PlainWorker.yaml": [
        ("lower_allocation", "PlainWorker", "asil", "B", None),
        ("lower_allocation", "PlainWorker.rPlainBlank", "measures", ["status"], None),
    ],
    "swc/safety/SpeedSensor.yaml": [("lower_allocation", "SpeedSensor", "asil", "B", None)],
    "swc/safety/TorqueLimiter.yaml": [
        ("lower_allocation", "TorqueLimiter", "asil", "D", None),
        ("lower_allocation", "TorqueLimiter.rPlain", "measures", ["debug"], None),
        ("lower_allocation", "TorqueLimiter.rPlainMeasured", "measures", ["status"], None),
    ],
    "tasks/Task_Argued.yaml": [("lower_allocation", "Task_Argued", "coexistence", _ARGUED_TEXT, None)],
    "tasks/Task_Homogeneous.yaml": [],
    "tasks/Task_Mixed.yaml": [],
}


class TestEveryElementRemovedWholeIsAskedAboutWhatItCarried:
    """The broad replay: every element of both fixtures, removed one at a time.

    The expectations in :data:`FIXTURE_REMOVALS` are written by hand: an
    independent oracle, as a derived table would not be.
    """

    @pytest.mark.parametrize("relative", sorted(FIXTURE_REMOVALS), ids=lambda name: name)
    def test_the_safety_fixture(self, tmp_path: Path, relative: str) -> None:
        assert _verdict(tmp_path, _remove_file(relative)) == tuple(tuple(row) for row in FIXTURE_REMOVALS[relative])

    def test_the_fixture_table_names_every_element_of_the_fixture(self) -> None:
        """A file added to the fixture and not to the table fails here, by name."""
        held = {path.relative_to(SAFETY_MODEL_ROOT).as_posix() for path in SAFETY_MODEL_ROOT.rglob("*.yaml")}

        assert held == set(FIXTURE_REMOVALS)

    def test_a_project_that_has_not_adopted_ASIL_is_asked_NOTHING_about_any_element(self, tmp_path: Path) -> None:
        """``examples/basic``: every non-hardware removal is silent, one file at a time.

        ``hardware`` is excluded: its budgeted elements carry a check, and their
        deletion is ``DELETE_RESOURCE``.
        """
        for path in sorted(EXAMPLES_BASIC_MODEL.rglob("*.yaml"), key=lambda one: one.as_posix()):
            relative = path.relative_to(EXAMPLES_BASIC_MODEL).as_posix()
            if relative.startswith("hardware/"):
                continue
            delta, baseline, staged = _delta_over(tmp_path / path.stem, EXAMPLES_BASIC_MODEL, _remove_file(relative))

            assert safety_verdict(delta, baseline, staged=staged).changes == (), relative


# The four cancellations, end to end


#: A project that has adopted ASIL and reports nothing, so a change set can be
#: measured committing (pass D refuses every change set on the safety fixture,
#: which carries findings on purpose). It carries two rated elements and one
#: unrated across two interfaces; a component with a declared ASIL and one with
#: a derived one; a ``require`` port whose measure answers a finding and one
#: written ahead of need; and a task of one level.
CLEAN_PROJECT: dict[str, Any] = {
    "swcraft.yaml": {
        "name": "clean",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
        "arxml_version": "4.2.2",
    },
    "model/data_types/base/uint8_base.yaml": {
        "name": "uint8_base",
        "kind": "base_type",
        "size": 8,
        "encoding": "NONE",
        "native_declaration": "uint8",
    },
    "model/data_types/implementation/uint8.yaml": {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
    "model/hardware/CPU.yaml": {"name": "CPU", "kind": "processor", "description": "The one processor."},
    "model/hardware/CORE0.yaml": {
        "name": "CORE0",
        "kind": "core",
        "threshold": 0.7,
        "enforcement": "strict",
        "description": "The one core.",
    },
    "model/hardware/PARTITION0.yaml": {
        "name": "PARTITION0",
        "kind": "partition",
        "core": "CORE0",
        "description": "The one partition.",
    },
    "model/interfaces/ISig.yaml": {
        "name": "ISig",
        "kind": "sender_receiver",
        "description": "One high element, one low, one unrated.",
        "data_elements": [
            {"name": "hi", "implementation_type": "uint8", "asil": "D"},
            {"name": "lo", "implementation_type": "uint8", "asil": "A"},
            {"name": "plain", "implementation_type": "uint8"},
        ],
    },
    "model/interfaces/ISig2.yaml": {
        "name": "ISig2",
        "kind": "sender_receiver",
        "description": "One unrated element only.",
        "data_elements": [{"name": "plain", "implementation_type": "uint8"}],
    },
    "model/requirements/REQ-1.yaml": {
        "id": "REQ-1",
        "title": "The levelled requirement",
        "description": "Allocates ASIL D to the three consuming components.",
        "req_type": "req",
        "version": 1,
        "source": "architecture",
        "asil": "D",
    },
    "model/requirements/REQ-2.yaml": {
        "id": "REQ-2",
        "title": "The unlevelled requirement",
        "description": "Allocates no level; it exists so Aux1 is linked.",
        "req_type": "req",
        "version": 1,
        "source": "architecture",
    },
    "model/swc/app/Prod.yaml": {
        "name": "Prod",
        "type": "application",
        "description": "Produces at ASIL D.",
        "asil": "D",
        "ports": [{"name": "pSig", "direction": "provide", "interface": "ISig"}],
        "runnables": [{"name": "Produce", "events": [{"type": "timing", "period": 0.01}]}],
        "handle_termination_and_restart": "no_support",
        "satisfies": ["REQ-1@1"],
    },
    "model/swc/app/Cons.yaml": {
        "name": "Cons",
        "type": "application",
        "description": "Consumes the low element under a declared measure.",
        "asil": "D",
        "ports": [
            {
                "name": "rSig",
                "direction": "require",
                "interface": "ISig",
                "measures": {"lo": "End-to-end protection profile 4."},
            }
        ],
        "runnables": [{"name": "Consume", "events": [{"type": "timing", "period": 0.01}]}],
        "handle_termination_and_restart": "no_support",
        "satisfies": ["REQ-1@1"],
    },
    "model/swc/app/Cons2.yaml": {
        "name": "Cons2",
        "type": "application",
        "description": "Carries a measure written ahead of need on an unrated element.",
        "asil": "D",
        "ports": [
            {
                "name": "rSig2",
                "direction": "require",
                "interface": "ISig2",
                "measures": {"plain": "A watchdog on the plain channel."},
            }
        ],
        "runnables": [{"name": "Consume2", "events": [{"type": "timing", "period": 0.01}]}],
        "handle_termination_and_restart": "no_support",
        "satisfies": ["REQ-1@1"],
    },
    "model/swc/app/Aux1.yaml": {
        "name": "Aux1",
        "type": "application",
        "description": "A component with no ports, declaring ASIL D.",
        "asil": "D",
        "runnables": [{"name": "AuxWork", "events": [{"type": "timing", "period": 0.02}]}],
        "handle_termination_and_restart": "no_support",
        "satisfies": ["REQ-2@1"],
    },
    "model/tasks/T.yaml": {
        "name": "T",
        "period": 0.01,
        "priority": 1,
        "partition": "PARTITION0",
        "wcet": "100us",
        "description": "One task, three runnables, one level.",
        "runnables": [
            {"swc": "Prod", "runnable": "Produce"},
            {"swc": "Cons", "runnable": "Consume"},
            {"swc": "Cons2", "runnable": "Consume2"},
        ],
    },
    "model/compositions/Root.yaml": {
        "name": "Root",
        "title": "Root",
        "root": True,
        "description": "The root composition.",
        "members": [{"name": name, "type": name} for name in ("Prod", "Cons", "Cons2", "Aux1")],
    },
}

#: ``Cons2.rSig2``'s standing measure, the text an identical declaration elsewhere
#: must not cancel.
_STANDING_MEASURE = "A watchdog on the plain channel."


def _clean_project(root: Path, prepare: Callable[[Path], None] | None = None) -> Path:
    """:data:`CLEAN_PROJECT` on disk at *root*, optionally edited before the baseline."""
    for relative, document in CLEAN_PROJECT.items():
        _write_yaml(root / relative, document)
    if prepare is not None:
        prepare(root / "model")
    return root


def _through_the_engine(
    project: Path, operations: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[list[tuple[str, str, str, Any, Any]]], list[tuple[str, ...]]]:
    """Stage *operations* against *project* as it stands on disk, answering both questions yes.

    Returns the response, the safety changes the host was shown at each review
    call in rendered order, and each call's change-set-level notes.
    """
    from swcraft_tools.editing.engine import handle_request

    questions: list[list[tuple[str, str, str, Any, Any]]] = []
    notes: list[tuple[str, ...]] = []

    def review(proposal: Any) -> ApprovalDecision:
        questions.append(
            [
                (change.kind.value, change.resource, change.field, change.old, change.new)
                for change in proposal.safety.changes
            ]
        )
        notes.append(tuple(proposal.safety.notes))
        return ApprovalDecision(
            change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
        )

    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    response = handle_request(
        {"operation": "change_set", "baseline": token, "intent": "measured", "operations": operations},
        project=str(project),
        review=review,
    )
    return response, questions, notes


def _committed_with(
    root: Path, operations: list[dict[str, Any]], prepare: Callable[[Path], None] | None = None
) -> tuple[str | None, list[tuple[str, str, str, Any, Any]]]:
    """Stage *operations* through ``handle_request`` and answer both questions yes.

    Returns the status and the safety changes the host was shown, in rendered
    order; ``("committed", [])`` is a change set nobody was asked about.
    """
    status, shown, _ = _committed_with_notes(root, operations, prepare)
    return status, shown


def _committed_with_notes(
    root: Path, operations: list[dict[str, Any]], prepare: Callable[[Path], None] | None = None
) -> tuple[str | None, list[tuple[str, str, str, Any, Any]], tuple[str, ...]]:
    """:func:`_committed_with`, and the last review call's change-set-level notes beside it."""
    _clean_project(root, prepare)
    assert list(check_cross_layer_consistency(str(root / "model")).records) == [], (
        "the baseline must report nothing, or pass D would refuse the change set before the guard is reached"
    )
    response, questions, notes = _through_the_engine(root, operations)
    return response.get("status"), (questions[-1] if questions else []), (notes[-1] if notes else ())


def _unlink(component: str, requirement: str) -> dict[str, Any]:
    """The removal of a component's own ``satisfies`` member naming *requirement* at version 1."""
    return {
        "operation": "remove",
        "address": {
            "layer": "swc",
            "element": component,
            "path": [{"field": "satisfies"}, {"member": f"{requirement}@1"}],
        },
    }


def _rate(interface: str, element: str, level: str | None) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {
            "layer": "interfaces",
            "element": interface,
            "path": [{"field": "data_elements"}, {"member": element}, {"field": "asil"}],
        },
        "value": level,
    }


def _drop_component(name: str) -> list[dict[str, Any]]:
    return [
        {"operation": "remove", "address": {"layer": "swc", "element": name}},
        {
            "operation": "remove",
            "address": {"layer": "compositions", "element": "Root", "path": [{"field": "members"}, {"member": name}]},
        },
    ]


def _add_component(name: str, **fields: Any) -> list[dict[str, Any]]:
    return [
        {
            "operation": "create",
            "address": {"layer": "swc", "element": name},
            "value": {
                "name": name,
                "group": "app",
                "type": "application",
                "description": f"{name}, created whole.",
                "runnables": [{"name": "Work", "events": [{"type": "timing", "period": 0.02}]}],
                "handle_termination_and_restart": "no_support",
                "satisfies": ["REQ-2@1"],
                **fields,
            },
        },
        {
            "operation": "create",
            "address": {"layer": "compositions", "element": "Root", "path": [{"field": "members"}, {"member": name}]},
            "value": {"name": name, "type": name},
        },
    ]


def _add_measured_port(component: str, port: str, interface: str, measures: dict[str, str]) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "swc", "element": component, "path": [{"field": "ports"}, {"member": port}]},
        "value": {"name": port, "direction": "require", "interface": interface, "measures": measures},
    }


_DROP_CONS_PORT = {
    "operation": "remove",
    "address": {"layer": "swc", "element": "Cons", "path": [{"field": "ports"}, {"member": "rSig"}]},
}


class TestNothingCancelsAWeakeningExceptARename:
    """Nothing cancels a weakening except a rename, measured through ``handle_request``.

    Model-wide totals let an unrelated element carrying the same key spend a
    question (F1-F4 each committed with the host shown nothing). The comparison
    is per owner, and only a ``remove``/``create`` pair identical up to identity
    is exempt. Each case states its control and its bundle, so a uniformly
    silent guard fails.
    """

    @staticmethod
    def _two_requirements_over_one_deriving_component(model: Path) -> None:
        """``Cons`` derives its level from two requirements, at D and B, in a task of its own.

        Its own task keeps the withdrawal from also making ``T`` mixed.
        """
        component = _read_yaml(model / "swc" / "app" / "Cons.yaml")
        del component["asil"]
        # Cons names the two requirements, and no longer REQ-1.
        component["satisfies"] = ["REQ-A@1", "REQ-B@1"]
        _write_yaml(model / "swc" / "app" / "Cons.yaml", component)
        for requirement_id, level in (("REQ-A", "D"), ("REQ-B", "B")):
            _write_yaml(
                model / "requirements" / f"{requirement_id}.yaml",
                {
                    "id": requirement_id,
                    "title": f"{requirement_id} allocates ASIL {level} to Cons",
                    "description": "Cons declares no level of its own and derives the maximum of these two.",
                    "req_type": "req",
                    "version": 1,
                    "source": "architecture",
                    "asil": level,
                },
            )
        task = _read_yaml(model / "tasks" / "T.yaml")
        task["runnables"] = [held for held in task["runnables"] if held["swc"] != "Cons"]
        _write_yaml(model / "tasks" / "T.yaml", task)
        _write_yaml(
            model / "tasks" / "T2.yaml",
            {
                "name": "T2",
                "period": 0.01,
                "priority": 2,
                "partition": "PARTITION0",
                "wcet": "100us",
                "description": "Cons alone.",
                "runnables": [{"swc": "Cons", "runnable": "Consume"}],
            },
        )

    def test_F1_a_withdrawal_that_leaves_a_LOWER_allocation_standing(self, tmp_path: Path) -> None:
        """Unlinking ``REQ-A`` drops ``Cons`` from D to B with ``REQ-B`` still standing, and asks.

        Neither model reports a finding, so pass D admits it.
        """
        status, shown = self._committed(tmp_path, [_unlink("Cons", "REQ-A")])

        assert status == "committed"
        assert shown == [("lower_allocation", "Cons", "satisfies", "REQ-A@1", None)]

    def test_F1_control_the_same_withdrawal_where_nothing_else_allocates(self, tmp_path: Path) -> None:
        """With ``REQ-B`` absent the identical edit commits too, and is asked about the same way.

        Nothing else refuses the removal (the model carries only
        ``trace.requirement-unallocated``, a warning), so the question is the
        only thing between the withdrawal and the disk.
        """

        def prepare(model: Path) -> None:
            self._two_requirements_over_one_deriving_component(model)
            (model / "requirements" / "REQ-B.yaml").unlink()
            component = _read_yaml(model / "swc" / "app" / "Cons.yaml")
            component["satisfies"] = ["REQ-A@1"]
            _write_yaml(model / "swc" / "app" / "Cons.yaml", component)

        status, shown = _committed_with(tmp_path, [_unlink("Cons", "REQ-A")], prepare)

        assert (status, shown) == ("committed", [("lower_allocation", "Cons", "satisfies", "REQ-A@1", None)])
        after = check_cross_layer_consistency(str(tmp_path / "model"))
        assert after.consistent is True
        assert after.records == ()
        assert [(record.rule, [subject.element for subject in record.subjects]) for record in after.warnings] == [
            ("trace.requirement-unallocated", ["REQ-A"]),
        ]

    def test_F2_a_withdrawal_bundled_with_an_unrelated_STRENGTHENING(self, tmp_path: Path) -> None:
        """Un-rating one element beside rating another at the same level renders the withdrawal alone."""
        alone = _committed_with(tmp_path / "alone", [_rate("ISig", "hi", None)])
        bundled = _committed_with(tmp_path / "bundled", [_rate("ISig", "hi", None), _rate("ISig2", "plain", "D")])

        assert alone == ("committed", [("lower_allocation", "ISig.hi", "asil", "D", None)])
        assert bundled == alone

    def test_F3_a_removed_bearer_bundled_with_an_unrelated_creation_of_the_same_level(self, tmp_path: Path) -> None:
        """Removing an ASIL-D component beside creating a different one at D still asks.

        The two differ beyond their names, so :func:`_detect_renames` pairs nothing.
        """
        alone = _committed_with(tmp_path / "alone", _drop_component("Aux1"))
        bundled = _committed_with(tmp_path / "bundled", _drop_component("Aux1") + _add_component("Fresh", asil="D"))

        assert alone == ("committed", [("lower_allocation", "Aux1", "asil", "D", None)])
        assert bundled == alone

    def test_F4_a_measured_consumption_moved_from_one_component_to_another(self, tmp_path: Path) -> None:
        """A measured consumption moved from ``Cons`` to ``Prod`` asks about both ends."""
        measure = {"lo": "End-to-end protection profile 4."}
        added = _add_measured_port("Prod", "rSigNew", "ISig", measure)
        withdrawn = _committed_with(tmp_path / "withdrawn", [_DROP_CONS_PORT])
        declared = _committed_with(tmp_path / "declared", [added])
        both = _committed_with(tmp_path / "both", [_DROP_CONS_PORT, added])

        assert withdrawn == ("committed", [("lower_allocation", "Cons.rSig", "measures", ["lo"], None)])
        assert declared == ("committed", [("declare_measure", "Prod.rSigNew", "measures", None, measure)])
        assert both == ("committed", [withdrawn[1][0], declared[1][0]])

    def test_a_switch_bundled_with_an_identically_TEXTED_declaration(self, tmp_path: Path) -> None:
        """Rating ``ISig2.plain`` makes ``Cons2.rSig2``'s map live; a same-text declaration elsewhere cancels nothing.

        The identical-text bundle renders what the different-text control renders.
        """
        switch = ("declare_measure", "Cons2.rSig2", "measures", None, {"plain": _STANDING_MEASURE})
        alone = _committed_with(tmp_path / "alone", [_rate("ISig2", "plain", "A")])
        same = _committed_with(
            tmp_path / "same",
            [_rate("ISig2", "plain", "A"), _add_measured_port("Prod", "r2", "ISig2", {"plain": _STANDING_MEASURE})],
        )
        other = _committed_with(
            tmp_path / "other",
            [_rate("ISig2", "plain", "A"), _add_measured_port("Prod", "r2", "ISig2", {"plain": "Another text."})],
        )

        assert alone == ("committed", [switch])
        assert same == (
            "committed",
            [("declare_measure", "Prod.r2", "measures", None, {"plain": _STANDING_MEASURE}), switch],
        )
        assert [row[:3] for row in same[1]] == [row[:3] for row in other[1]]

    def test_a_QM_component_joining_a_task_that_already_carries_an_argument(self, tmp_path: Path) -> None:
        """The coexistence half of the same switch, per task rather than per port."""

        def prepare(model: Path) -> None:
            task = _read_yaml(model / "tasks" / "T.yaml")
            task["coexistence"] = "Written ahead of a re-rating; partitioned by the OS."
            _write_yaml(model / "tasks" / "T.yaml", task)
            _write_yaml(
                model / "swc" / "app" / "Plain.yaml",
                {
                    "name": "Plain",
                    "type": "application",
                    "description": "A QM component.",
                    "asil": "QM",
                    "runnables": [{"name": "PlainWork", "events": [{"type": "timing", "period": 0.01}]}],
                    "handle_termination_and_restart": "no_support",
                    "satisfies": ["REQ-2@1"],
                },
            )
            composition = _read_yaml(model / "compositions" / "Root.yaml")
            composition["members"].append({"name": "Plain", "type": "Plain"})
            _write_yaml(model / "compositions" / "Root.yaml", composition)

        status, shown = _committed_with(
            tmp_path,
            [
                {
                    "operation": "create",
                    "address": {
                        "layer": "tasks",
                        "element": "T",
                        "path": [
                            {"field": "runnables"},
                            {"member": {"swc": "Plain", "runnable": "PlainWork"}},
                        ],
                    },
                    "value": {"swc": "Plain", "runnable": "PlainWork"},
                }
            ],
            prepare,
        )

        assert status == "committed"
        assert [row[:3] for row in shown] == [("declare_coexistence", "T", "coexistence")]

    def test_the_one_exemption_is_a_rename_and_it_survives_bundling(self, tmp_path: Path) -> None:
        """A nil rename asks nothing and cancels nothing: it exempts itself alone."""
        # `Aux1`'s own document under a new name, with the `group` every created
        # component needs. Any other difference makes the pair two movements.
        carried = {**CLEAN_PROJECT["model/swc/app/Aux1.yaml"], "name": "AuxRenamed", "group": "app"}
        renamed = _drop_component("Aux1") + [
            {"operation": "create", "address": {"layer": "swc", "element": "AuxRenamed"}, "value": carried},
            {
                "operation": "create",
                "address": {
                    "layer": "compositions",
                    "element": "Root",
                    "path": [{"field": "members"}, {"member": "AuxRenamed"}],
                },
                "value": {"name": "AuxRenamed", "type": "AuxRenamed"},
            },
        ]
        alone = _committed_with(tmp_path / "alone", renamed)
        bundled = _committed_with(tmp_path / "bundled", renamed + [_DROP_CONS_PORT])

        assert alone == ("committed", [])
        assert bundled == ("committed", [("lower_allocation", "Cons.rSig", "measures", ["lo"], None)])

    def _committed(
        self, tmp_path: Path, operations: list[dict[str, Any]]
    ) -> tuple[str | None, list[tuple[str, str, str, Any, Any]]]:
        return _committed_with(tmp_path, operations, self._two_requirements_over_one_deriving_component)


class TestAnAmbiguousRenamePairingAsks:
    """An ambiguous rename pairing is no rename: both removals ask.

    Pairing greedily in delta order would let a crosswise rename withdrawing a
    level commit with the host shown nothing. Uniqueness is decidable from the
    delta alone (:func:`~swcraft_tools.editing.safety_guard._detect_renames`);
    it over-asks on a crosswise rename and never goes quiet. Here two twins,
    identical including their links, are both renamed while the two
    requirements swap levels.
    """

    #: The two twins' shared body, links included: identical apart from the
    #: `name` that `_identity_stripped` removes.
    _BODY = {
        "type": "application",
        "description": "An identical twin.",
        "runnables": [{"name": "Run", "events": [{"type": "timing", "period": 0.05}]}],
        "handle_termination_and_restart": "no_support",
        "satisfies": ["RA@1", "RB@1"],
    }

    @classmethod
    def _twins(cls, model: Path) -> None:
        for name, requirement, level in (("A", "RA", "D"), ("B", "RB", "B")):
            _write_yaml(model / "swc" / "app" / f"{name}.yaml", {"name": name, **cls._BODY})
            _write_yaml(
                model / "requirements" / f"{requirement}.yaml",
                {
                    "id": requirement,
                    "title": f"{requirement} allocates ASIL {level}",
                    "description": "Both twins satisfy this requirement.",
                    "req_type": "req",
                    "version": 1,
                    "source": "architecture",
                    "asil": level,
                },
            )
            composition = _read_yaml(model / "compositions" / "Root.yaml")
            composition["members"].append({"name": name, "type": name})
            _write_yaml(model / "compositions" / "Root.yaml", composition)

    @classmethod
    def _crosswise(cls) -> list[dict[str, Any]]:
        """``A``/``B`` out, ``C``/``D`` in, with the two requirements' levels swapped."""
        operations: list[dict[str, Any]] = [
            {"operation": "remove", "address": {"layer": "swc", "element": "A"}},
            {"operation": "remove", "address": {"layer": "swc", "element": "B"}},
        ]
        for name in ("C", "D"):
            operations.append(
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": name},
                    "value": {"name": name, "group": "app", **cls._BODY},
                }
            )
        # `RA` drops to B and `RB` rises to D: the maximum each twin derives stays D.
        for requirement, level in (("RA", "B"), ("RB", "D")):
            operations.append(
                {
                    "operation": "update",
                    "address": {"layer": "requirements", "element": requirement, "path": [{"field": "asil"}]},
                    "value": level,
                }
            )
        for name in ("A", "B"):
            operations.append(
                {
                    "operation": "remove",
                    "address": {
                        "layer": "compositions",
                        "element": "Root",
                        "path": [{"field": "members"}, {"member": name}],
                    },
                }
            )
        for name in ("C", "D"):
            operations.append(
                {
                    "operation": "create",
                    "address": {
                        "layer": "compositions",
                        "element": "Root",
                        "path": [{"field": "members"}, {"member": name}],
                    },
                    "value": {"name": name, "type": name},
                }
            )
        return operations

    def test_the_crosswise_rename_asks_about_both_removals_and_never_nothing(self, tmp_path: Path) -> None:
        """The crosswise rename asks about both component removals.

        Asserted on the two as a set: ``RA``'s own D -> B line would make a
        non-empty check pass without them.
        """
        status, shown, _ = _committed_with_notes(tmp_path, self._crosswise(), self._twins)

        assert status == "committed"
        components = {row for row in shown if row[1] in {"A", "B"}}
        assert components == {
            ("lower_allocation", "A", "asil", "D", None),
            ("lower_allocation", "B", "asil", "D", None),
        }, f"the crosswise rename must ask about BOTH removed twins; the host was shown {shown}"

    def test_the_verdict_says_the_pairing_was_ambiguous(self, tmp_path: Path) -> None:
        """The verdict notes that the guard could not tell which renamed element became which."""
        _, _, notes = _committed_with_notes(tmp_path, self._crosswise(), self._twins)

        assert len(notes) == 1
        assert "no rename was recognised" in notes[0]
        assert "A, B were removed and C, D created" in notes[0]
        assert "layer 'swc'" in notes[0]

    def test_the_note_is_rendered_under_the_weakenings_and_never_instead_of_them(self, tmp_path: Path) -> None:
        """``render_safety_review`` puts it below the list, neutralized like every other value."""
        from swcraft_tools.editing.review import render_safety_review

        captured: list[str] = []

        def review(proposal: Any) -> ApprovalDecision:
            captured.append(render_safety_review(proposal))
            return ApprovalDecision(
                change_set_id=proposal.change_set_id,
                baseline=proposal.baseline,
                approved=True,
                safety_approved=False,
            )

        from swcraft_tools.editing.engine import handle_request

        _clean_project(tmp_path, self._twins)
        token = handle_request({"operation": "discover"}, project=str(tmp_path))["baseline"]
        handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "crosswise",
                "operations": self._crosswise(),
            },
            project=str(tmp_path),
            review=review,
        )

        message = captured[0]
        lines = message.splitlines()
        note_at = next(index for index, line in enumerate(lines) if line.startswith("  note: "))
        weakenings = [index for index, line in enumerate(lines) if line.startswith("  - ")]
        assert weakenings and note_at > max(weakenings)
        assert lines[-1] == "Approve the weakening as well?"

    def test_a_UNIQUE_pairing_is_still_a_rename(self, tmp_path: Path) -> None:
        """One twin renamed and the other left alone pairs uniquely: no question and no note."""
        operations: list[dict[str, Any]] = [
            {"operation": "remove", "address": {"layer": "swc", "element": "A"}},
            {
                "operation": "create",
                "address": {"layer": "swc", "element": "C"},
                "value": {"name": "C", "group": "app", **self._BODY},
            },
            {
                "operation": "remove",
                "address": {
                    "layer": "compositions",
                    "element": "Root",
                    "path": [{"field": "members"}, {"member": "A"}],
                },
            },
            {
                "operation": "create",
                "address": {
                    "layer": "compositions",
                    "element": "Root",
                    "path": [{"field": "members"}, {"member": "C"}],
                },
                "value": {"name": "C", "type": "C"},
            },
        ]
        status, shown, notes = _committed_with_notes(tmp_path, operations, self._twins)

        assert (status, shown, notes) == ("committed", [], ())

    def test_the_pairing_itself_refuses_the_ambiguous_shape_and_keeps_the_unique_one(self) -> None:
        """``_detect_renames`` pairs nothing in an ambiguous group and still pairs a unique one beside it.

        Every host calls ``check_change_set_safety`` with a delta, so the rule is
        asserted on the function; ambiguity is per content shape.
        """
        from swcraft_tools.editing.safety_guard import _detect_renames

        twin = {"type": "application", "description": "twin", "runnables": []}
        other = {"type": "application", "description": "not a twin", "runnables": []}
        delta = [
            _entry("remove", _address("swc", "A"), before={"name": "A", **twin}),
            _entry("remove", _address("swc", "B"), before={"name": "B", **twin}),
            _entry("remove", _address("swc", "X"), before={"name": "X", **other}),
            _entry("create", _address("swc", "C"), after={"name": "C", **twin}),
            _entry("create", _address("swc", "D"), after={"name": "D", **twin}),
            _entry("create", _address("swc", "Y"), after={"name": "Y", **other}),
        ]

        renames = _detect_renames(delta)

        assert dict(renames.forward) == {("element", "swc", "X"): "Y"}
        assert dict(renames.backward) == {("element", "swc", "Y"): "X"}
        assert len(renames.ambiguities) == 1
        ambiguity = renames.ambiguities[0]
        assert (ambiguity.removals, ambiguity.creations) == (("A", "B"), ("C", "D"))
        assert "A, B were removed and C, D created" in ambiguity.note()

    # -- the note is scoped to groups a question consults ---------------------

    @staticmethod
    def _twin_types(model: Path) -> None:
        """Two data types identical but for the name — an ambiguous group nothing consults."""
        for name in ("t1", "t2"):
            write_model_element(
                model,
                {"name": name, "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16_t"},
            )

    @staticmethod
    def _swap_the_twin_types() -> list[dict[str, Any]]:
        """``t1``/``t2`` out, ``t3``/``t4`` in: a pairing that is ambiguous and irrelevant."""
        operations: list[dict[str, Any]] = [
            {"operation": "remove", "address": {"layer": "base_types", "element": name}} for name in ("t1", "t2")
        ]
        operations.extend(
            {
                "operation": "create",
                "address": {"layer": "base_types", "element": name},
                "value": {
                    "name": name,
                    "kind": "base_type",
                    "size": 16,
                    "encoding": "NONE",
                    "native_declaration": "uint16_t",
                },
            }
            for name in ("t3", "t4")
        )
        return operations

    def test_an_ambiguous_group_no_owner_is_keyed_on_says_nothing(self, tmp_path: Path) -> None:
        """An ambiguous ``base_types`` group, which no safety reading consults, renders no note.

        The note never claims removals were asked about when none was. The
        bundled real weakening is still reported.
        """

        def prepare(model: Path) -> None:
            self._twins(model)
            self._twin_types(model)

        operations = [
            {
                "operation": "update",
                "address": {"layer": "requirements", "element": "RA", "path": [{"field": "asil"}]},
                "value": "B",
            },
            *self._swap_the_twin_types(),
        ]
        status, shown, notes = _committed_with_notes(tmp_path, operations, prepare)

        assert status == "committed"
        assert shown == [("lower_allocation", "RA", "asil", "D", "B")], (
            f"the bundled weakening must still be asked about; the host was shown {shown}"
        )
        assert notes == (), f"an ambiguity no owner is keyed on must say nothing; the host was shown {notes}"

    def test_the_two_directions_in_one_change_set(self, tmp_path: Path) -> None:
        """Of two ambiguous groups, only the ``swc`` one that suppressed an exemption is noted."""

        def prepare(model: Path) -> None:
            self._twins(model)
            self._twin_types(model)

        operations = [*self._crosswise(), *self._swap_the_twin_types()]
        status, shown, notes = _committed_with_notes(tmp_path, operations, prepare)

        assert status == "committed"
        assert {row for row in shown if row[1] in {"A", "B"}} == {
            ("lower_allocation", "A", "asil", "D", None),
            ("lower_allocation", "B", "asil", "D", None),
        }
        assert len(notes) == 1, f"exactly one of the two ambiguous groups is worth a note; got {notes}"
        assert "layer 'swc'" in notes[0]
        assert "base_types" not in notes[0]

    def test_the_owner_family_table_names_every_shape_the_accessors_are_asked_about(self) -> None:
        """``_AMBIGUITY_OWNER_FAMILIES`` equals the ``_Renames`` accessor call sites, read off the AST.

        A new consulted shape adds a call, not a row, and would silence the note.
        **Does not reach** non-literal arguments: a call built from a variable
        fails here rather than passing.
        """
        from swcraft_tools.editing.safety_guard import _AMBIGUITY_OWNER_FAMILIES

        source = Path(safety_guard.__file__).read_text(encoding="utf-8")
        element_accessors = {"canonical_element", "staged_element"}
        member_accessors = {"canonical_member", "staged_member"}
        called: set[tuple[str, ...]] = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in element_accessors | member_accessors:
                continue
            literals = [arg.value if isinstance(arg, ast.Constant) else None for arg in node.args]
            if node.func.attr in element_accessors:
                assert isinstance(literals[0], str), f"{node.func.attr} is called with a non-literal layer"
                called.add(("element", literals[0]))
            else:
                assert isinstance(literals[0], str) and isinstance(literals[2], str), (
                    f"{node.func.attr} is called with a non-literal layer or field"
                )
                called.add(("member", literals[0], literals[2]))

        assert called == set(_AMBIGUITY_OWNER_FAMILIES), (
            "_AMBIGUITY_OWNER_FAMILIES and the shapes the comparison actually consults have drifted: "
            f"called {sorted(called)}, declared {sorted(_AMBIGUITY_OWNER_FAMILIES)}"
        )

    def test_the_connection_witnessed_family_reads_no_rename(self) -> None:
        """``_stubbed_connections`` and its note take no ``renames`` and call no ``_Renames`` method.

        An exact connector or delegation rename keeps its ports, so the family
        needs no rename reading. **Does not reach** a rename reading arriving
        other than by a method call, a parameter or a closure.
        """
        from swcraft_tools.editing.safety_guard import _Renames

        accessors = {name for name in vars(_Renames) if not name.startswith("__")}
        tree = ast.parse(Path(safety_guard.__file__).read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"_stubbed_connections", "_connection_note"}
        }
        assert set(functions) == {"_stubbed_connections", "_connection_note"}
        for name, function in functions.items():
            parameters = {argument.arg for argument in function.args.args + function.args.kwonlyargs}
            assert "renames" not in parameters, name
            named = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
            assert "renames" not in named, name
            called = {
                node.func.attr
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            assert not called & accessors, (name, sorted(called & accessors))


class TestAttributionNamesTheEntryAndNeverDecides:
    """The comparison decides; attribution only names the entry, and fails closed without a branch.

    Answering "no owner" would silently drop a candidate.
    """

    @staticmethod
    def _owners(change: Any, layer: str = "swc") -> tuple[Any, ...]:
        from swcraft_tools.editing.safety_guard import _owners_of

        entry = _entry("update", _address(layer, "Whatever", "asil"), before="D", after="QM")
        empty = _snapshot(())
        return _owners_of(entry, change, empty, empty)

    def test_a_kind_the_dispatch_has_no_branch_for_fails_closed(self) -> None:
        """A kind the dispatch has no branch for raises.

        A ``hardware`` kind is a real enum member this dispatch has no branch
        for, since ``hardware`` never reaches the comparison.
        """
        change = legacy_authorization.SafetyChange(
            legacy_authorization.SafetyChangeKind.RAISE_THRESHOLD, "RAM0", "threshold", 0.8, 0.95
        )

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="no owner for safety change kind"):
            self._owners(change)

    def test_a_sixth_classified_layer_fails_closed_too(self) -> None:
        """The other half of the same rule: a layer added to one table and not the other."""
        change = legacy_authorization.SafetyChange(
            legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION, "Whatever", "asil", "D", "QM"
        )

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="no owner for a lowered allocation"):
            self._owners(change, layer="connectors")


class TestNoVerdictIsDecidedWithoutTheStagedModel:
    """No verdict is decided without the staged model, and no case asserts an arm alone.

    The guard refuses a delta with any entry outside ``hardware`` without the
    staged model. Every guard behaviour is asserted through
    :func:`safety_verdict` with ``staged=`` or through ``handle_request``: an arm
    asserted alone can hide a gate overruling it. This class, about the refusal
    itself, is the one exemption.

    **Reach.** The first gate reads every :func:`safety_verdict` call in this
    module, in both spellings. The second forbids the per-layer arm classifiers,
    their dispatch and ``_decide``. Deliberately **outside** it:
    ``_detect_renames`` (the one exemption, asserted directly), ``_owners_of``
    (whose cases assert it raises) and the ``validator`` record builders (the
    rules, not the guard).
    """

    def test_a_non_hardware_entry_without_the_staged_model_is_refused(self, hardware: tuple[Hardware, ...]) -> None:
        delta = [_entry("update", _address("swc", "ExampleSWC", "asil"), before="D", after="QM")]

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="staged model"):
            safety_verdict(delta, _snapshot(hardware))

    def test_an_entry_no_arm_looks_at_is_refused_just_the_same(self, hardware: tuple[Hardware, ...]) -> None:
        """The refusal is keyed on the layer, not on whether an arm fires.

        An entry no arm looks at can still make a standing argument live.
        """
        delta = [_entry("update", _address("tasks", "Task_10ms", "priority"), before=1, after=2)]

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="staged model"):
            safety_verdict(delta, _snapshot(hardware))

    def test_a_hardware_only_delta_still_needs_none(self, hardware: tuple[Hardware, ...]) -> None:
        """A hardware-only delta needs no staged model: no ``hardware`` value reaches a safety rule."""
        delta = [_entry("update", _address("hardware", "CORE0", "threshold"), before=0.7, after=0.9)]

        assert safety_verdict(delta, _snapshot(hardware)).is_safety_act is True

    def test_a_partition_entry_without_the_staged_model_is_refused(self, hardware: tuple[Hardware, ...]) -> None:
        """A partition's level and argument are the comparison's, which needs both models."""
        baseline = _snapshot((*hardware, Partition(name="PARTITION0", kind="partition", core="CORE0", asil="B")))
        delta = [_entry("update", _address("hardware", "PARTITION0", "asil"), before="B", after="A")]

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="staged model"):
            safety_verdict(delta, baseline)

    def test_a_hardware_entry_at_an_element_the_baseline_lacks_is_refused_without_it(
        self, hardware: tuple[Hardware, ...]
    ) -> None:
        """A created element may be a partition, and only the staged model can say."""
        delta = [_entry("create", _address("hardware", "P_New"), after={"name": "P_New", "kind": "partition"})]

        with pytest.raises(legacy_authorization.SafetyClassificationError, match="staged model"):
            safety_verdict(delta, _snapshot(hardware))

    def test_no_case_in_this_module_asks_for_a_verdict_without_it(self) -> None:
        """Every :func:`safety_verdict` call outside this class passes ``staged=``, hardware ones included.

        Both spellings are read: the bare name and the attribute form, which
        reading ``ast.Name`` alone would miss.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        offenders: list[str] = []
        # Top-level nodes only: the exemption is the class's.
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == type(self).__name__:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                # The bare name AND the attribute form: `_m.safety_verdict(...)`
                # is a real spelling here, and `ast.Name` alone missed it.
                called = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if called != "safety_verdict":
                    continue
                if "staged" not in {keyword.arg for keyword in call.keywords}:
                    offenders.append(f"{getattr(node, 'name', '<module>')}:{call.lineno}")

        assert offenders == []

    def test_no_case_reaches_past_the_verdict_to_an_arm(self) -> None:
        """No case names an arm classifier, their dispatch or ``_decide``: no arm is tested alone.

        The deliberate exceptions are listed in the class docstring.
        """
        forbidden = {
            "_shape_verdict",
            "_classify_outside_hardware",
            "classify_outside_hardware",
            "_classify_swc",
            "_classify_port_interior",
            "_classify_tasks",
            "_classify_requirements",
            "_classify_interfaces",
            *(name for name in vars(safety_guard) if name.startswith("_decide")),
        }
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        # Read off the AST and not off the text, so the prose above may name
        # what the code may not reach for.
        reached = sorted(
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in forbidden}
            | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in forbidden}
            | {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if (alias.asname or alias.name) in forbidden
            }
        )

        assert reached == [], (
            "an arm's answer is not a guard behaviour: assert `safety_verdict(..., staged=...)` "
            "or drive the case through `handle_request`"
        )


class TestAStructuralMoveThatRemovesTheHazardIsNotAskedAbout:
    """A structural move that removes the hazard itself closes a finding and asks nothing.

    The named limit, S1-S5: the component no longer produces or consumes the
    signal, or the task no longer mixes levels. Asking here would fire on
    ordinary refactoring. Each case asserts the finding closes, none is
    created and the verdict is empty. Does **not** say the moves are safe:
    ``check-model`` judges the model that remains.
    """

    @staticmethod
    def _edit_port(component: str, port: str, **fields: Any) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "swc" / "safety" / f"{component}.yaml"
            document = _read_yaml(path)
            for candidate in document["ports"]:
                if candidate["name"] == port:
                    candidate.update(fields)
            _write_yaml(path, document)

        return edit

    @staticmethod
    def _remove_port(component: str, port: str) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "swc" / "safety" / f"{component}.yaml"
            document = _read_yaml(path)
            document["ports"] = [candidate for candidate in document["ports"] if candidate["name"] != port]
            _write_yaml(path, document)

        return edit

    @staticmethod
    def _keep_only_runnable(task: str, swc: str) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "tasks" / f"{task}.yaml"
            document = _read_yaml(path)
            document["runnables"] = [ref for ref in document["runnables"] if ref["swc"] == swc]
            _write_yaml(path, document)

        return edit

    @pytest.mark.parametrize(
        ("edit", "closes"),
        [
            pytest.param(
                _edit_port.__func__("SpeedSensor", "pSafety", direction="require"),
                "safety.provider-below-element",
                id="S1-provide-port-flipped-to-require",
            ),
            pytest.param(
                _edit_port.__func__("SpeedSensor", "pSafety", interface="IPlainSignals"),
                "safety.provider-below-element",
                id="S2-provide-port-re-pointed-at-a-lower-rated-interface",
            ),
            pytest.param(
                _remove_port.__func__("SpeedSensor", "pSafety"),
                "safety.provider-below-element",
                id="S3-unmeasured-provide-port-removed",
            ),
            pytest.param(
                _keep_only_runnable.__func__("Task_Mixed", "TorqueLimiter"),
                # The move also empties `P_Mixed` of its two lower components, so
                # their two partition findings close with the task's.
                {"safety.task-mixed-asil": 1, "safety.component-below-partition": 2},
                id="S4-runnable-references-removed-until-one-level-remains",
            ),
            pytest.param(
                _edit_port.__func__("PlainWorker", "rPlain", direction="provide"),
                "safety.consumer-above-element",
                id="S5-unmeasured-require-port-flipped-to-provide",
            ),
        ],
    )
    def test_the_finding_closes_and_the_guard_is_silent(
        self, tmp_path: Path, edit: Callable[[Path], None], closes: str | dict[str, int]
    ) -> None:
        before = _findings_after(tmp_path / "before", lambda root: None)
        after = _findings_after(tmp_path / "after", edit)

        # Counted: the fixture mints two findings of some of these rules.
        expected = {closes: 1} if isinstance(closes, str) else closes
        assert dict(Counter(before) - Counter(after)) == expected, (before, after)
        assert not Counter(after) - Counter(before), "the move must remove the hazard and create nothing"

        delta, baseline, staged = _safety_fixture_delta(tmp_path / "delta", edit)
        verdict = safety_verdict(delta, baseline, staged=staged)

        assert verdict.changes == (), verdict.changes
        assert not verdict.is_safety_act


# A core's or a memory's `asil_capability` raised, or cleared below D, against the
# placement rules it weakens

#: The rules the capability is compared by, the only ones a capability move can
#: close: a core's against the partitions on it, a memory's against the
#: components and partitions budgeting it.
PLACEMENT_RULES = frozenset(
    {
        "safety.partition-on-unqualified-core",
        "safety.memory-below-component",
        "safety.partition-memory-below-level",
    }
)

#: The five levels in increasing order, written out rather than imported, so the
#: sweep below is an oracle independent of the classifier.
_CAPABILITY_ORDER = ("QM", "A", "B", "C", "D")
_CAPABILITY_STATES = (None, *_CAPABILITY_ORDER)


def _refused_under(capability: str | None) -> frozenset[str]:
    """The levels a placement rule reports under *capability*; none for an unrated element."""
    if capability is None:
        return frozenset()
    return frozenset(_CAPABILITY_ORDER[_CAPABILITY_ORDER.index(capability) + 1 :])


def _with_capability(snapshot: ModelSnapshot, element: str, level: str | None) -> ModelSnapshot:
    """*snapshot* with one hardware element's ``asil_capability`` set to *level*."""
    return replace(
        snapshot,
        hardware=[
            item.model_copy(update={"asil_capability": level}) if item.name == element else item
            for item in snapshot.hardware
        ],
    )


def _placement_findings(snapshot: ModelSnapshot) -> Counter[tuple[str, tuple[tuple[str, Any], ...]]]:
    """The placement rules' findings over *snapshot*, keyed by rule and place, not by message.

    The message quotes the capability, so it would split one finding in two.
    """
    records = hardware_capability_records(
        list(snapshot.hardware),
        list(snapshot.tasks),
        list(snapshot.swcs),
        list(snapshot.requirements),
        list(snapshot.compositions),
    )
    return Counter((record.rule, tuple((s.layer, s.element) for s in record.subjects)) for record in records)


@pytest.fixture(scope="module")
def safety_snapshot() -> ModelSnapshot:
    """The committed safety fixture as a snapshot, read once per module and never at import.

    At import, a broken fixture file would be a collection error for the whole module.
    """
    return load_snapshot(str(SAFETY_MODEL_ROOT))


def _every_level_placed(snapshot: ModelSnapshot) -> ModelSnapshot:
    """*snapshot* with a partition at every level on ``CORE0`` and a component at every level budgeting ``RAM0``.

    On it a move that makes an element refuse less always closes a finding, so
    rule and guard are held equal in both directions.
    """
    edits: dict[str, dict[str, Any]] = {
        "DiagnosticsAgent": {"asil": "A"},
        "SpeedSensor": {"asil": "C", "memory": {"RAM0": 64}},
    }
    levels = {"P_Argued": "A", "P_Homogeneous": "B", "P_Mixed": "C"}
    hardware = [
        item.model_copy(update={"asil": levels[item.name]}) if item.name in levels else item
        for item in snapshot.hardware
    ]
    hardware.append(Partition(name="P_Level_D", kind="partition", core="CORE0", asil="D"))
    return replace(
        snapshot,
        hardware=hardware,
        swcs=[swc.model_copy(update=edits[swc.name]) if swc.name in edits else swc for swc in snapshot.swcs],
    )


class TestTheCapabilityGuardAndThePlacementRulesAgree:
    """``hardware_capability_records`` and ``safety_verdict`` agree on the same two snapshots.

    What a core or memory refuses depends on that element alone, so the
    hardware fold decides it. Not through ``handle_request``: pass D refuses
    every change set on this fixture (the engine half is
    :class:`TestTheCapabilityIsDecidedEndToEndOnACleanSeed`).
    """

    def test_raising_core0_closes_its_three_findings_and_is_one_question(self, safety_snapshot: ModelSnapshot) -> None:
        baseline = _with_capability(safety_snapshot, "CORE0", "B")
        staged = _with_capability(safety_snapshot, "CORE0", "D")

        before = _placement_findings(baseline)
        after = _placement_findings(staged)
        closed = before - after
        # One per partition on the core: the fixture's three `D` partitions.
        assert sum(n for (rule, _), n in closed.items() if rule == "safety.partition-on-unqualified-core") == 3, closed
        assert not after - before

        delta = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
        (change,) = safety_verdict(delta, baseline, staged=staged).changes
        assert (change.kind.value, change.resource, change.field, change.old, change.new) == (
            "raise_capability",
            "CORE0",
            "asil_capability",
            "B",
            "D",
        )

    def test_raising_ram0_closes_the_memory_finding_and_is_one_question(self, safety_snapshot: ModelSnapshot) -> None:
        # To D, not C: `TorqueLimiter` at D and the three `D` partitions budgeting
        # `RAM0` are refused by a `C` memory.
        baseline = _with_capability(safety_snapshot, "RAM0", "B")
        staged = _with_capability(safety_snapshot, "RAM0", "D")

        closed = _placement_findings(baseline) - _placement_findings(staged)
        assert sorted(closed.elements()) == [
            (
                "safety.memory-below-component",
                (("swc", "TorqueLimiter"), ("swc", "TorqueLimiter"), ("hardware", "RAM0")),
            ),
            (
                "safety.partition-memory-below-level",
                (("hardware", "RAM0"), ("hardware", "P_Argued"), ("swc", "TorqueLimiter")),
            ),
            (
                "safety.partition-memory-below-level",
                (("hardware", "RAM0"), ("hardware", "P_Homogeneous"), ("swc", "TorqueLimiter")),
            ),
            (
                "safety.partition-memory-below-level",
                (
                    ("hardware", "RAM0"),
                    ("hardware", "P_Mixed"),
                    ("swc", "DiagnosticsAgent"),
                    ("swc", "PlainWorker"),
                    ("swc", "TorqueLimiter"),
                ),
            ),
        ], closed

        delta = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
        (change,) = safety_verdict(delta, baseline, staged=staged).changes
        assert (change.kind.value, change.resource, change.old, change.new) == ("raise_capability", "RAM0", "B", "D")

    @pytest.mark.parametrize("before", _CAPABILITY_STATES, ids=lambda s: f"from-{s}")
    @pytest.mark.parametrize("after", _CAPABILITY_STATES, ids=lambda s: f"to-{s}")
    @pytest.mark.parametrize("element", ["CORE0", "RAM0"])
    @pytest.mark.parametrize("placed", ["as-committed", "every-level-placed"])
    def test_the_guard_asks_whenever_the_rule_stops_refusing_and_decides_by_the_refused_set(
        self, safety_snapshot: ModelSnapshot, placed: str, element: str, before: str | None, after: str | None
    ) -> None:
        """Every capability move of both elements, through the real differ, on two models.

        A move that closes a placement finding is always asked about, and the
        classifier follows the refused set. The converse holds only on
        ``every-level-placed``: on the committed fixture a raise that closes
        nothing is still an act.
        """
        model = safety_snapshot if placed == "as-committed" else _every_level_placed(safety_snapshot)
        baseline = _with_capability(model, element, before)
        staged = _with_capability(model, element, after)
        delta = project_delta(diff_snapshots(baseline, staged), baseline=baseline, staged=staged)
        verdict = safety_verdict(delta, baseline, staged=staged)

        closed = _placement_findings(baseline) - _placement_findings(staged)
        if closed:
            assert verdict.is_safety_act, f"{element} {before} -> {after} closed {dict(closed)} and asked nothing"
        assert verdict.is_safety_act is (_refused_under(after) < _refused_under(before)), (before, after)
        if placed == "every-level-placed":
            assert verdict.is_safety_act is bool(closed), f"{element} {before} -> {after} closed {dict(closed)}"

    def test_every_level_is_really_placed_on_both_elements_in_the_second_model(
        self, safety_snapshot: ModelSnapshot
    ) -> None:
        """Rated `QM`, each element reports every level `A`-`D`, so the equivalence is not vacuous."""
        model = _every_level_placed(safety_snapshot)
        levels = {swc.name: swc.asil for swc in model.swcs}
        levels |= {item.name: item.asil for item in model.hardware if isinstance(item, Partition)}
        for element, rule, owner in (
            ("CORE0", "safety.partition-on-unqualified-core", "hardware"),
            ("RAM0", "safety.memory-below-component", "swc"),
        ):
            findings = _placement_findings(_with_capability(model, element, "QM"))
            named = {
                places[[layer for layer, _ in places].index(owner)][1]
                for (found, places), _count in findings.items()
                if found == rule
            }
            assert {levels[name] for name in named} >= {"A", "B", "C", "D"}, (element, named, levels)


class TestThePlacementMovesThatRemoveTheSubjectAreNotAskedAbout:
    """Placement moves close a placement finding and ask nothing — the named limit.

    Re-pointing a partition's ``core``, moving a component's ``memory`` budget
    or a task's ``partition`` moves the subject of a finding; no declaration
    stops covering what it covered, and neither ``core`` nor ``memory`` is in a
    hardware trigger set. The cost: on an unrated core or memory a rated
    component is never compared. Does **not** say the moves are safe.
    """

    @staticmethod
    def _declare_core1(capability: str | None) -> Callable[[Path], None]:
        """A second core ``RAM0`` admits, rated *capability* or unrated, with an unrated partition ``P_Core1`` on it."""

        def prepare(root: Path) -> None:
            document: dict[str, Any] = {"name": "CORE1", "kind": "core", "threshold": 0.7, "enforcement": "strict"}
            if capability is not None:
                document["asil_capability"] = capability
            _write_yaml(root / "hardware" / "CORE1.yaml", document)
            _write_yaml(root / "hardware" / "P_Core1.yaml", {"name": "P_Core1", "kind": "partition", "core": "CORE1"})
            memory = _read_yaml(root / "hardware" / "RAM0.yaml")
            memory["accessible"] = ["CORE0", "CORE1"]
            _write_yaml(root / "hardware" / "RAM0.yaml", memory)

        return prepare

    @staticmethod
    def _declare_unrated_ram1(root: Path) -> None:
        """A second memory, UNRATED, that every component budgets ``0`` — declared, and used by nobody."""
        _write_yaml(
            root / "hardware" / "RAM1.yaml",
            {
                "name": "RAM1",
                "kind": "memory",
                "type": "RAM",
                "size": 4096,
                "accessible": ["CORE0"],
                "threshold": 0.5,
                "enforcement": "strict",
            },
        )
        for path in sorted((root / "swc" / "safety").glob("*.yaml"), key=lambda candidate: candidate.as_posix()):
            document = _read_yaml(path)
            document["memory"]["RAM1"] = 0
            _write_yaml(path, document)

    @staticmethod
    def _move_task(task: str, partition: str) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "tasks" / f"{task}.yaml"
            document = _read_yaml(path)
            document["partition"] = partition
            _write_yaml(path, document)

        return edit

    @staticmethod
    def _move_partition(partition: str, core: str) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "hardware" / f"{partition}.yaml"
            document = _read_yaml(path)
            document["core"] = core
            _write_yaml(path, document)

        return edit

    @staticmethod
    def _budget(component: str, value: int | None) -> Callable[[Path], None]:
        def edit(root: Path) -> None:
            path = root / "swc" / "safety" / f"{component}.yaml"
            document = _read_yaml(path)
            if value is None:
                del document["memory"]["RAM0"]
            else:
                document["memory"]["RAM0"] = value
            _write_yaml(path, document)

        return edit

    @staticmethod
    def _closed_and_created(
        tmp_path: Path, edit: Callable[[Path], None], prepare: Callable[[Path], None] | None
    ) -> tuple[Counter[str], Counter[str]]:
        before = Counter(_findings_after(tmp_path / "before", lambda _root: None, prepare))
        after = Counter(_findings_after(tmp_path / "after", edit, prepare))
        return before - after, after - before

    def test_a_partition_re_pointed_onto_a_more_capable_core_closes_its_finding_and_asks_nothing(
        self, tmp_path: Path
    ) -> None:
        prepare = self._declare_core1("D")
        edit = self._move_partition("P_Mixed", "CORE1")
        closed, created = self._closed_and_created(tmp_path / "findings", edit, prepare)

        assert dict(closed) == {"safety.partition-on-unqualified-core": 1}, closed
        assert not created, created

        delta, baseline, staged = _outcome_delta(tmp_path / "delta", SAFETY_MODEL_ROOT, edit, prepare=prepare)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_partition_re_pointed_onto_an_unrated_core_closes_its_finding_and_asks_nothing(
        self, tmp_path: Path
    ) -> None:
        """The same move onto a core with no capability, compared against nothing, asks nothing.

        ``swcraft query hardware --exists asil_capability`` shows which cores are rated.
        """
        prepare = self._declare_core1(None)
        edit = self._move_partition("P_Mixed", "CORE1")
        closed, created = self._closed_and_created(tmp_path / "findings", edit, prepare)

        assert dict(closed) == {"safety.partition-on-unqualified-core": 1}, closed
        assert not created, created

        delta, baseline, staged = _outcome_delta(tmp_path / "delta", SAFETY_MODEL_ROOT, edit, prepare=prepare)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_task_re_pointed_into_an_unrated_partition_closes_its_per_type_findings_and_asks_nothing(
        self, tmp_path: Path
    ) -> None:
        """``Task_Mixed`` moved into the unrated ``P_Core1`` closes ``P_Mixed``'s per-type findings and asks nothing.

        ``partition-on-unqualified-core`` reads the partition's level only, so it
        does not move. A task's ``partition`` is a placement change pass D
        re-judges, not a declaration.
        """
        prepare = self._declare_core1(None)
        edit = self._move_task("Task_Mixed", "P_Core1")
        closed, created = self._closed_and_created(tmp_path / "findings", edit, prepare)

        assert {rule: n for rule, n in closed.items() if rule.startswith("safety.")} == {
            "safety.component-below-partition": 2,
            "safety.partition-memory-below-level": 1,
        }, closed
        assert not [rule for rule in created if rule.startswith("safety.")], created

        delta, baseline, staged = _outcome_delta(tmp_path / "delta", SAFETY_MODEL_ROOT, edit, prepare=prepare)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_budget_moved_from_a_rated_memory_onto_an_unrated_one_closes_its_finding_and_asks_nothing(
        self, tmp_path: Path
    ) -> None:
        """``TorqueLimiter``'s budget moved from ``RAM0`` (`B`) to the unrated ``RAM1`` asks nothing."""

        def edit(root: Path) -> None:
            path = root / "swc" / "safety" / "TorqueLimiter.yaml"
            document = _read_yaml(path)
            document["memory"]["RAM0"] = 0
            document["memory"]["RAM1"] = 512
            _write_yaml(path, document)

        closed, created = self._closed_and_created(tmp_path / "findings", edit, self._declare_unrated_ram1)

        assert dict(closed) == {"safety.memory-below-component": 1, "safety.partition-memory-below-level": 2}, closed
        assert not created, created

        delta, baseline, staged = _outcome_delta(
            tmp_path / "delta", SAFETY_MODEL_ROOT, edit, prepare=self._declare_unrated_ram1
        )
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_components_budget_zeroed_closes_the_memory_finding_and_asks_nothing(self, tmp_path: Path) -> None:
        edit = self._budget("TorqueLimiter", 0)
        closed, created = self._closed_and_created(tmp_path / "findings", edit, None)

        assert dict(closed) == {"safety.memory-below-component": 1, "safety.partition-memory-below-level": 2}, closed
        assert not created, created

        delta, baseline, staged = _safety_fixture_delta(tmp_path / "delta", edit)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()

    def test_a_components_budget_entry_dropped_closes_the_memory_finding_and_asks_nothing(self, tmp_path: Path) -> None:
        """A dropped budget entry closes the memory finding; the guard is silent and no safety finding appears.

        The missing budget is a budget finding pass D would refuse, not a safety one.
        """
        edit = self._budget("TorqueLimiter", None)
        closed, created = self._closed_and_created(tmp_path / "findings", edit, None)

        assert dict(closed) == {"safety.memory-below-component": 1, "safety.partition-memory-below-level": 2}, closed
        assert not [rule for rule in created if rule.startswith("safety.")], created

        delta, baseline, staged = _safety_fixture_delta(tmp_path / "delta", edit)
        assert safety_verdict(delta, baseline, staged=staged).changes == ()


def _capability(element: str, level: str | None) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {"layer": "hardware", "element": element, "path": [{"field": "asil_capability"}]},
        "value": level,
    }


#: The published example's seed: ``examples/basic`` with both components
#: deriving ``B`` and ``CORE0`` declaring ``asil_capability: B``. It reports
#: nothing, so a change set can be observed committing.
CAPABILITY_VECTOR = "update-core-asil-capability"


class TestTheCapabilityIsDecidedEndToEndOnACleanSeed:
    """Pass D and the guard agree through ``handle_request``, on a clean seed.

    A move the rules would refuse is refused by pass D; one that makes them
    refuse less commits after the second question; one that refuses no less
    commits unasked.
    """

    @pytest.fixture
    def seeded(self, tmp_path: Path) -> Path:
        project = fixtures.build_fixture(tmp_path / "project", CAPABILITY_VECTOR)
        assert list(check_cross_layer_consistency(str(project / "model")).records) == []
        return project

    def test_raising_the_capability_commits_after_the_second_question(self, seeded: Path) -> None:
        response, questions, _ = _through_the_engine(seeded, [_capability("CORE0", "D")])

        assert response.get("status") == "committed", response
        assert questions == [[("raise_capability", "CORE0", "asil_capability", "B", "D")]]

    def test_clearing_it_below_d_commits_after_the_second_question(self, seeded: Path) -> None:
        response, questions, _ = _through_the_engine(seeded, [_capability("CORE0", None)])

        assert response.get("status") == "committed", response
        assert questions == [[("raise_capability", "CORE0", "asil_capability", "B", None)]]

    def test_lowering_it_under_a_component_above_it_is_pass_ds_and_the_guard_is_never_reached(
        self, seeded: Path
    ) -> None:
        response, questions, _ = _through_the_engine(seeded, [_capability("CORE0", "A")])

        assert response.get("error") == "consistency-violation", response
        assert questions == []

    def test_a_threshold_and_a_capability_raised_on_one_core_are_one_question_through_the_engine(
        self, seeded: Path
    ) -> None:
        """One review call, one verdict, two changes — observed at the host and not only at the fold."""
        threshold = {
            "operation": "update",
            "address": {"layer": "hardware", "element": "CORE0", "path": [{"field": "threshold"}]},
            "value": 0.9,
        }
        response, questions, _ = _through_the_engine(seeded, [_capability("CORE0", "D"), threshold])

        assert response.get("status") == "committed", response
        assert [[shown[:3] for shown in question] for question in questions] == [
            [("raise_threshold", "CORE0", "threshold"), ("raise_capability", "CORE0", "asil_capability")]
        ]

    def test_declaring_a_memorys_capability_at_the_components_level_commits_unasked(self, seeded: Path) -> None:
        response, questions, _ = _through_the_engine(seeded, [_capability("RAM0", "B")])

        assert response.get("status") == "committed", response
        assert questions == [[]]

    def test_declaring_one_below_a_component_budgeting_it_is_pass_ds(self, seeded: Path) -> None:
        response, questions, _ = _through_the_engine(seeded, [_capability("RAM0", "A")])

        assert response.get("error") == "consistency-violation", response
        assert questions == []

    def test_clearing_a_d_capability_commits_unasked(self, tmp_path: Path) -> None:
        """`D -> None` on a project whose components are all ASIL D: nothing refused either side."""

        def rate_core_d(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "CORE0.yaml")
            document["asil_capability"] = "D"
            _write_yaml(model_root / "hardware" / "CORE0.yaml", document)

        assert _committed_with(tmp_path, [_capability("CORE0", None)], rate_core_d) == ("committed", [])


# The partition's level and argument are decided by the comparison, families 7 and 8

#: A partition argument the cases below declare; client text, as any argument is.
_PARTITION_TEXT = "The partition's lower components sit in their own memory regions under memory protection."


def _edit_hardware(name: str, **fields: Any) -> Callable[[Path], None]:
    """Set (or, with ``None``, remove) top-level *fields* of ``hardware/<name>.yaml``."""

    def edit(model_root: Path) -> None:
        path = model_root / "hardware" / f"{name}.yaml"
        document = _read_yaml(path)
        for field, value in fields.items():
            if value is None:
                document.pop(field, None)
            else:
                document[field] = value
        _write_yaml(path, document)

    return edit


def _new_partition(name: str, **fields: Any) -> Callable[[Path], None]:
    """Write a partition on ``CORE0`` carrying *fields*."""

    def edit(model_root: Path) -> None:
        document = {"name": name, "kind": "partition", "core": "CORE0", **fields}
        _write_yaml(model_root / "hardware" / f"{name}.yaml", document)

    return edit


def _repoint_task(task: str, partition: str) -> Callable[[Path], None]:
    """Move ``tasks/<task>.yaml`` into *partition*."""

    def edit(model_root: Path) -> None:
        path = model_root / "tasks" / f"{task}.yaml"
        document = _read_yaml(path)
        document["partition"] = partition
        _write_yaml(path, document)

    return edit


class TestThePartitionIsDecidedByTheComparison:
    """Families 7 and 8 over the committed safety fixture, every direction and its negative.

    The fixture's three partitions are rated ``D`` on ``CORE0``: ``P_Mixed``
    runs ``D``, ``B`` and a derived ``QM`` with no argument, ``P_Argued`` runs
    ``D`` and a derived ``B`` under an argument, and ``P_Homogeneous`` runs
    ``TorqueLimiter`` alone. The verdicts are asserted through
    :func:`safety_verdict` with the staged model, never through an arm.
    """

    # Family 7: the level

    def test_a_lowered_level_is_asked(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _edit_hardware("P_Mixed", asil="B")) == (
            ("lower_allocation", "P_Mixed", "asil", "D", "B"),
        )

    def test_a_cleared_level_is_asked(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", asil=None)) == (
            ("lower_allocation", "P_Homogeneous", "asil", "D", None),
        )

    def test_a_raised_level_is_not_asked(self, tmp_path: Path) -> None:
        """A raised rating refuses more and is not asked.

        Its limit: a raise can close ``safety.component-above-partition`` with
        nothing in exchange where the core and memories are unrated.
        """
        prepare = _edit_hardware("P_Homogeneous", asil="B")
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", asil="D"), prepare=prepare) == ()

    def test_rating_an_unrated_partition_is_not_asked(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", asil=None)
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", asil="A"), prepare=prepare) == ()

    def test_a_rated_partition_created_is_not_asked(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _new_partition("P_New", asil="D")) == ()

    def test_an_unrated_partition_removed_is_not_asked(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", asil=None)
        assert _verdict(tmp_path, _remove_file("hardware/P_Homogeneous.yaml"), prepare=prepare) == ()

    # Family 8: the argument

    def test_a_declared_argument_is_asked(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _edit_hardware("P_Mixed", coexistence=_PARTITION_TEXT)) == (
            ("declare_partition_coexistence", "P_Mixed", "coexistence", "", _PARTITION_TEXT),
        )

    def test_a_withdrawn_argument_is_not_asked(self, tmp_path: Path) -> None:
        """Text -> nothing makes the rule refuse again: a tightening."""
        assert _verdict(tmp_path, _edit_hardware("P_Argued", coexistence=None)) == ()

    def test_a_reworded_argument_is_not_asked(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _edit_hardware("P_Argued", coexistence=_PARTITION_TEXT)) == ()

    def test_bypass_a_a_partition_created_whole_carrying_an_argument_over_a_mixed_level_is_asked(
        self, tmp_path: Path
    ) -> None:
        """A partition created carrying an argument over a mixed level is asked: the comparison reads it."""

        def edit(model_root: Path) -> None:
            _new_partition("P_New", asil="D", coexistence=_PARTITION_TEXT)(model_root)
            _repoint_task("Task_Mixed", "P_New")(model_root)

        assert _verdict(tmp_path, edit) == (
            ("declare_partition_coexistence", "P_New", "coexistence", "", _PARTITION_TEXT),
        )

    def test_bypass_b_a_task_repointed_into_an_argued_rated_partition_is_asked(self, tmp_path: Path) -> None:
        """``Task_Mixed`` moving into the argued ``P_Homogeneous`` makes the standing argument live."""
        prepare = _edit_hardware("P_Homogeneous", coexistence=_PARTITION_TEXT)
        assert _verdict(tmp_path, _repoint_task("Task_Mixed", "P_Homogeneous"), prepare=prepare) == (
            ("declare_partition_coexistence", "P_Homogeneous", "coexistence", "", _PARTITION_TEXT),
        )

    def test_a_mix_moving_under_an_argument_already_live_is_not_asked(self, tmp_path: Path) -> None:
        """Family 4's rule for the partition: ``P_Argued`` is mixed and argued on both sides."""
        assert _verdict(tmp_path, _repoint_task("Task_Mixed", "P_Argued")) == ()

    def test_a_repoint_that_leaves_an_argued_partition_homogeneous_is_not_asked(self, tmp_path: Path) -> None:
        """The going-live shape's negative: ``P_Homogeneous`` is argued and nothing joins it."""
        prepare = _edit_hardware("P_Homogeneous", coexistence=_PARTITION_TEXT)
        assert _verdict(tmp_path, _repoint_task("Task_Argued", "P_Mixed"), prepare=prepare) == ()

    def test_the_argued_mixed_partition_removed_whole_withdraws_its_level_and_its_argument(
        self, tmp_path: Path
    ) -> None:
        assert _verdict(tmp_path, _remove_file("hardware/P_Argued.yaml")) == (
            ("lower_allocation", "P_Argued", "asil", "D", None),
            ("lower_allocation", "P_Argued", "coexistence", _PARTITION_ARGUED_TEXT, None),
        )

    def test_an_argued_homogeneous_partition_removed_withdraws_only_its_level(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", coexistence=_PARTITION_TEXT)
        assert _verdict(tmp_path, _remove_file("hardware/P_Homogeneous.yaml"), prepare=prepare) == (
            ("lower_allocation", "P_Homogeneous", "asil", "D", None),
        )

    def test_a_renamed_partition_asks_nothing(self, tmp_path: Path) -> None:
        """A content-identical ``remove`` + ``create`` is one owner under two names (the one exemption)."""

        def edit(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "P_Argued.yaml")
            (model_root / "hardware" / "P_Argued.yaml").unlink()
            _write_yaml(model_root / "hardware" / "P_Renamed.yaml", {**document, "name": "P_Renamed"})
            _repoint_task("Task_Argued", "P_Renamed")(model_root)

        assert _verdict(tmp_path, edit) == ()

    # Going live by a change other than a task re-point

    def test_an_argued_partition_raised_over_a_lower_component_goes_live_and_is_asked(self, tmp_path: Path) -> None:
        """``P_Argued`` at ``B`` runs ``D`` and ``B``: not mixed. Raised to ``D``, the ``B`` is below it.

        The raise itself asks nothing (a rating's raise never does); the argument
        that stood unread starts answering ``safety.component-below-partition``,
        and that is the question.
        """
        prepare = _edit_hardware("P_Argued", asil="B")
        assert _verdict(tmp_path, _edit_hardware("P_Argued", asil="D"), prepare=prepare) == (
            ("declare_partition_coexistence", "P_Argued", "coexistence", "", _PARTITION_ARGUED_TEXT),
        )

    def test_a_component_lowered_inside_an_argued_homogeneous_partition_is_asked_twice(self, tmp_path: Path) -> None:
        """The component's own lowering, and the partition's argument going live under it."""
        prepare = _edit_hardware("P_Homogeneous", coexistence=_PARTITION_TEXT)

        def edit(model_root: Path) -> None:
            path = model_root / "swc" / "safety" / "TorqueLimiter.yaml"
            _write_yaml(path, {**_read_yaml(path), "asil": "C"})

        assert _verdict(tmp_path, edit, prepare=prepare) == (
            ("lower_allocation", "TorqueLimiter", "asil", "D", "C"),
            ("declare_partition_coexistence", "P_Homogeneous", "coexistence", "", _PARTITION_TEXT),
        )

    def test_a_partition_renamed_and_lowered_in_one_change_set_still_asks_the_lowering(self, tmp_path: Path) -> None:
        """Not a rename — the content differs — so the rated, argued ``P_Argued`` is removed whole.

        The level ``D`` it carried and its argument are asked about as withdrawn,
        and the argument the new partition arrives carrying is asked as declared:
        renaming cannot carry a lowering past the question.
        """

        def edit(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "P_Argued.yaml")
            (model_root / "hardware" / "P_Argued.yaml").unlink()
            _write_yaml(model_root / "hardware" / "P_Renamed.yaml", {**document, "name": "P_Renamed", "asil": "B"})
            _repoint_task("Task_Argued", "P_Renamed")(model_root)

        assert _verdict(tmp_path, edit) == (
            ("lower_allocation", "P_Argued", "asil", "D", None),
            ("lower_allocation", "P_Argued", "coexistence", _PARTITION_ARGUED_TEXT, None),
            ("declare_partition_coexistence", "P_Renamed", "coexistence", "", _PARTITION_ARGUED_TEXT),
        )

    # Which hardware entries reach the comparison

    def test_a_baseline_core_whose_name_becomes_a_partition_is_touched(self, tmp_path: Path) -> None:
        from swcraft_tools.editing.safety_guard import _touches_a_partition

        def edit(model_root: Path) -> None:
            _write_yaml(model_root / "hardware" / "RAM0.yaml", {"name": "RAM0", "kind": "partition", "core": "CORE0"})

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, edit)
        hardware = list(baseline.hardware)

        assert {entry.address.element for entry in delta} == {"RAM0"}
        # Only the staged model says RAM0 is a partition now.
        assert _touches_a_partition(delta, hardware, tuple(staged.hardware)) is True

    def test_a_core_and_a_partition_sharing_a_name_count_as_a_partition(self, tmp_path: Path) -> None:
        """Any element of the entry's name that is a partition counts: a duplicate cannot hide one.

        The differ refuses duplicate names, so the trigger is asked directly.
        """
        from swcraft_tools.editing.safety_guard import _touches_a_partition
        from swcraft_tools.schema import Partition

        delta, baseline, staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, _edit_hardware("CORE0", threshold=0.9))
        hardware = [*baseline.hardware, Partition(kind="partition", name="CORE0", core="CORE0")]

        assert {entry.address.element for entry in delta} == {"CORE0"}
        assert _touches_a_partition(delta, hardware, tuple(staged.hardware)) is True
        with pytest.raises(legacy_authorization.SafetyClassificationError, match="needs the staged model"):
            _touches_a_partition(delta, hardware, None)

    # Ordering, and what the comparison is not run for

    def test_the_partition_lines_follow_every_other_owner_and_precede_the_hardware_fold(self, tmp_path: Path) -> None:
        """The partition's owners are never attributed, so they render in the second pass."""

        def edit(model_root: Path) -> None:
            _edit_hardware("CORE0", threshold=0.9)(model_root)
            _edit_hardware("P_Mixed", asil="C")(model_root)
            document = _read_yaml(model_root / "requirements" / "REQ-SAFETY-002.yaml")
            document["asil"] = "A"
            _write_yaml(model_root / "requirements" / "REQ-SAFETY-002.yaml", document)

        assert [row[:2] for row in _verdict(tmp_path, edit)] == [
            ("lower_allocation", "REQ-SAFETY-002"),
            ("lower_allocation", "P_Mixed"),
            ("raise_threshold", "CORE0"),
        ]

    def test_a_change_set_on_cores_and_memories_alone_does_not_run_the_comparison(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fast path survives the partition: the safety rules' readings are not even asked for."""
        delta, baseline, staged = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, _edit_hardware("RAM0", threshold=0.9, asil_capability="D")
        )

        def refuse(*_: Any, **__: Any) -> Any:
            raise AssertionError("the comparison ran for a change set on cores and memories alone")

        monkeypatch.setattr(safety_guard, "safety_reading", refuse)

        assert [change.kind.value for change in safety_verdict(delta, baseline, staged=staged).changes] == [
            "raise_threshold",
            "raise_capability",
        ]

    def test_a_partition_entry_alone_does_run_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other direction: one ``hardware`` entry at a partition reaches the comparison."""
        delta, baseline, staged = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, _edit_hardware("P_Mixed", description="Reworded.")
        )
        assert {entry.address.layer for entry in delta} == {"hardware"}
        calls: list[int] = []
        real = safety_guard.safety_reading

        def counted(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(safety_guard, "safety_reading", counted)

        assert safety_verdict(delta, baseline, staged=staged).changes == ()
        assert calls, "a change set at a partition alone did not reach the comparison"

    def test_a_project_that_has_not_adopted_ASIL_is_asked_nothing_about_its_partition(self, tmp_path: Path) -> None:
        """``examples/basic``: its one partition is unrated and unargued; every edit to it is silent."""
        for name, edit in (
            ("describe", _edit_hardware("PARTITION0", description="Reworded.")),
            ("remove", _remove_file("hardware/PARTITION0.yaml")),
            ("rate", _edit_hardware("PARTITION0", asil="QM")),
        ):
            delta, baseline, staged = _delta_over(tmp_path / name, EXAMPLES_BASIC_MODEL, edit)
            assert safety_verdict(delta, baseline, staged=staged).changes == (), name


# The partition's trust class, family 9


class TestThePartitionsTrustIsDecidedByTheComparison:
    """Family 9 over the committed safety fixture: a movement across ``trusted``, and nothing else.

    The fixture's three partitions are rated ``D`` on ``CORE0`` and declare no
    ``trust``, so every one of them is ``non_trusted``. Making one ``trusted``
    beside two other ``D`` partitions is a tie, which neither trust rule judges,
    so each case is about the act alone; the verdicts are asserted through
    :func:`safety_verdict` with the staged model, never through an arm.
    """

    def test_non_trusted_to_trusted_is_the_grant(self, tmp_path: Path) -> None:
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", trust="trusted")) == (
            ("grant_partition_trust", "P_Homogeneous", "trust", "non_trusted", "trusted"),
        )

    def test_trusted_with_protection_to_trusted_is_the_grant(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", trust="trusted_with_protection")
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", trust="trusted"), prepare=prepare) == (
            ("grant_partition_trust", "P_Homogeneous", "trust", "trusted_with_protection", "trusted"),
        )

    def test_a_partition_created_trusted_is_the_grant(self, tmp_path: Path) -> None:
        """An absent partition reads as the default class, so creating one ``trusted`` is the same act."""
        assert _verdict(tmp_path, _new_partition("P_New", asil="D", trust="trusted")) == (
            ("grant_partition_trust", "P_New", "trust", "non_trusted", "trusted"),
        )

    def test_trusted_to_trusted_with_protection_is_the_protection(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", trust="trusted")
        assert _verdict(
            tmp_path, _edit_hardware("P_Homogeneous", trust="trusted_with_protection"), prepare=prepare
        ) == (("declare_partition_protection", "P_Homogeneous", "trust", "trusted", "trusted_with_protection"),)

    def test_trusted_to_non_trusted_is_the_protection(self, tmp_path: Path) -> None:
        """The key dropped is the default written back: the same declaration as writing ``non_trusted``."""
        prepare = _edit_hardware("P_Homogeneous", trust="trusted")
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", trust=None), prepare=prepare) == (
            ("declare_partition_protection", "P_Homogeneous", "trust", "trusted", "non_trusted"),
        )

    @pytest.mark.parametrize(
        ("was", "now"), [("non_trusted", "trusted_with_protection"), ("trusted_with_protection", "non_trusted")]
    )
    def test_a_movement_between_the_confined_classes_is_not_asked(self, tmp_path: Path, was: str, now: str) -> None:
        prepare = _edit_hardware("P_Homogeneous", trust=was)
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", trust=now), prepare=prepare) == ()

    def test_an_unrated_trusted_partition_removed_is_not_asked(self, tmp_path: Path) -> None:
        """What family 9 does not reach: removing an unrated ``trusted`` partition asks nothing.

        It closes ``safety.trusted-partition-unrated``; family 7 walks rated
        partitions only and family 9 reads a removal as a loss, not a declaration.
        """
        prepare = _new_partition("P_Trusted", trust="trusted")
        # Precondition: the baseline really carries the finding.
        assert "safety.trusted-partition-unrated" in _prepared_rules(tmp_path, prepare)
        assert _verdict(tmp_path, _remove_file("hardware/P_Trusted.yaml"), prepare=prepare) == ()

    def test_a_rated_trusted_partition_removed_asks_only_its_level(self, tmp_path: Path) -> None:
        prepare = _edit_hardware("P_Homogeneous", trust="trusted")
        assert _verdict(tmp_path, _remove_file("hardware/P_Homogeneous.yaml"), prepare=prepare) == (
            ("lower_allocation", "P_Homogeneous", "asil", "D", None),
        )

    def test_a_trusted_partition_removed_and_a_confined_one_created_under_another_name_is_not_asked(
        self, tmp_path: Path
    ) -> None:
        """The removal gap in its remove-and-recreate shape: no trust question.

        A changed partition under a new name is no rename: a removal plus a
        confined creation, neither asking on trust (under one name the edit asks
        ``declare_partition_protection``). Families 7 and 8 still ask the
        withdrawn level and argument.
        """
        prepare = _edit_hardware("P_Argued", trust="trusted")

        def edit(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "P_Argued.yaml")
            (model_root / "hardware" / "P_Argued.yaml").unlink()
            renamed = {key: value for key, value in document.items() if key not in ("asil", "coexistence")}
            _write_yaml(
                model_root / "hardware" / "P_Renamed.yaml",
                {**renamed, "name": "P_Renamed", "trust": "trusted_with_protection"},
            )
            _repoint_task("Task_Argued", "P_Renamed")(model_root)

        assert _verdict(tmp_path, edit, prepare=prepare) == (
            ("lower_allocation", "P_Argued", "asil", "D", None),
            ("lower_allocation", "P_Argued", "coexistence", _PARTITION_ARGUED_TEXT, None),
        )

    def test_a_trusted_partition_renamed_keeping_its_class_asks_nothing(self, tmp_path: Path) -> None:
        """A content-identical ``remove`` + ``create`` is one owner under two names, its class included."""
        prepare = _edit_hardware("P_Argued", trust="trusted")

        def edit(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "P_Argued.yaml")
            (model_root / "hardware" / "P_Argued.yaml").unlink()
            _write_yaml(model_root / "hardware" / "P_Renamed.yaml", {**document, "name": "P_Renamed"})
            _repoint_task("Task_Argued", "P_Renamed")(model_root)

        assert _verdict(tmp_path, edit, prepare=prepare) == ()

    def test_a_partition_renamed_and_made_trusted_asks_the_grant_after_every_other_partition_line(
        self, tmp_path: Path
    ) -> None:
        """Not a rename — the content differs — so ``P_Argued`` is removed whole and ``P_Renamed`` created.

        The withdrawn level and argument and the declared argument are families 7
        and 8; the grant is family 9 and renders after them, in ``_decide``'s order.
        """

        def edit(model_root: Path) -> None:
            document = _read_yaml(model_root / "hardware" / "P_Argued.yaml")
            (model_root / "hardware" / "P_Argued.yaml").unlink()
            _write_yaml(
                model_root / "hardware" / "P_Renamed.yaml", {**document, "name": "P_Renamed", "trust": "trusted"}
            )
            _repoint_task("Task_Argued", "P_Renamed")(model_root)

        assert _verdict(tmp_path, edit) == (
            ("lower_allocation", "P_Argued", "asil", "D", None),
            ("lower_allocation", "P_Argued", "coexistence", _PARTITION_ARGUED_TEXT, None),
            ("declare_partition_coexistence", "P_Renamed", "coexistence", "", _PARTITION_ARGUED_TEXT),
            ("grant_partition_trust", "P_Renamed", "trust", "non_trusted", "trusted"),
        )

    def test_the_trust_line_follows_the_partition_s_level_line(self, tmp_path: Path) -> None:
        """One partition lowered and made ``trusted`` in one edit: family 7's line, then family 9's."""
        assert _verdict(tmp_path, _edit_hardware("P_Homogeneous", asil="B", trust="trusted")) == (
            ("lower_allocation", "P_Homogeneous", "asil", "D", "B"),
            ("grant_partition_trust", "P_Homogeneous", "trust", "non_trusted", "trusted"),
        )

    def test_the_owner_family_table_names_the_trust_family_for_a_hardware_rename(self) -> None:
        from swcraft_tools.editing.safety_guard import _AMBIGUITY_OWNER_FAMILIES

        assert "partition-trust" in _AMBIGUITY_OWNER_FAMILIES[("element", "hardware")]

    @staticmethod
    def _twin_trusted_partitions(model_root: Path) -> None:
        for name in ("T_A", "T_B"):
            _new_partition(name, trust="trusted", description="An identical trusted twin.")(model_root)

    @staticmethod
    def _crosswise_twins(model_root: Path) -> None:
        for name in ("T_A", "T_B"):
            (model_root / "hardware" / f"{name}.yaml").unlink()
        for name in ("T_C", "T_D"):
            _new_partition(name, trust="trusted", description="An identical trusted twin.")(model_root)

    def test_an_ambiguous_rename_pairing_on_trusted_partitions_asks_with_the_note(self, tmp_path: Path) -> None:
        """Two identical ``trusted`` partitions out, two in: no rename, each creation a grant, and the note.

        The ambiguity bears on the owners through the ``partition-trust`` row of
        ``_AMBIGUITY_OWNER_FAMILIES``.
        """
        delta, baseline, staged = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, self._crosswise_twins, prepare=self._twin_trusted_partitions
        )
        verdict = safety_verdict(delta, baseline, staged=staged)

        assert [(change.kind.value, change.resource) for change in verdict.changes] == [
            ("grant_partition_trust", "T_C"),
            ("grant_partition_trust", "T_D"),
        ]
        assert len(verdict.notes) == 1, verdict.notes
        assert "layer 'hardware'" in verdict.notes[0]
        assert "T_A, T_B were removed and T_C, T_D created" in verdict.notes[0]

    def test_a_unique_rename_pairing_on_a_trusted_partition_asks_nothing(self, tmp_path: Path) -> None:
        """The contrast: one twin out, one in, so the pairing is unique and the class travels with the name."""

        def edit(model_root: Path) -> None:
            (model_root / "hardware" / "T_A.yaml").unlink()
            _new_partition("T_C", trust="trusted", description="An identical trusted twin.")(model_root)

        delta, baseline, staged = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, edit, prepare=self._twin_trusted_partitions
        )
        verdict = safety_verdict(delta, baseline, staged=staged)

        assert verdict.changes == ()
        assert verdict.notes == ()

    @staticmethod
    def _reading_of(snapshot: ModelSnapshot) -> SafetyReading:
        """*snapshot*'s :class:`SafetyReading`, every layer the reader reads passed in."""
        return safety_reading(
            snapshot.swcs,
            snapshot.interfaces,
            snapshot.tasks,
            snapshot.requirements,
            tuple(snapshot.hardware),
            snapshot.compositions,
        )

    def test_the_reading_holds_the_declared_class_off_the_default_only(self, tmp_path: Path) -> None:
        """``SafetyReading.partition_trust``: first wins, ``non_trusted`` absent, every other class present."""

        def prepare(model_root: Path) -> None:
            _edit_hardware("P_Homogeneous", trust="trusted")(model_root)
            _edit_hardware("P_Argued", trust="trusted_with_protection")(model_root)
            _edit_hardware("P_Mixed", trust="non_trusted")(model_root)

        _, baseline, _ = _outcome_delta(
            tmp_path, SAFETY_MODEL_ROOT, _edit_hardware("CORE0", threshold=0.9), prepare=prepare
        )
        reading = self._reading_of(baseline)

        assert dict(reading.partition_trust) == {"P_Argued": "trusted_with_protection", "P_Homogeneous": "trusted"}

    def test_the_reading_s_first_file_of_a_name_wins(self, tmp_path: Path) -> None:
        """Two files of one partition name: the first in reader order is the class read, ``trusted`` here.

        The second, ``non_trusted``, is ``hardware.duplicate-name``'s; were the
        last to win, the reading would drop the partition as the default class.
        """

        def prepare(model_root: Path) -> None:
            for file_name, trust in (("a_first", "trusted"), ("b_second", "non_trusted")):
                document = {"name": "P_Dup", "kind": "partition", "core": "CORE0", "asil": "D", "trust": trust}
                _write_yaml(model_root / "hardware" / f"{file_name}.yaml", document)

        # A snapshot and no delta: the differ refuses two elements of one key.
        root = tmp_path / "model"
        shutil.copytree(SAFETY_MODEL_ROOT, root)
        prepare(root)
        baseline = load_snapshot(str(root))
        reading = self._reading_of(baseline)

        assert reading.partition_trust.get("P_Dup") == "trusted"

    def test_an_illegal_grant_is_refused_by_pass_d_and_never_asked(self, tmp_path: Path) -> None:
        """A grant below the processor's highest level is refused by pass D and never asked.

        It is ``safety.trusted-partition-below-processor-level`` in the staged result.
        """

        def prepare(model_root: Path) -> None:
            _edit_hardware("PARTITION0", asil="D")(model_root)
            _new_partition("P_Low", asil="B")(model_root)

        project = _clean_project(tmp_path, prepare)
        operation = {
            "operation": "update",
            "address": {"layer": "hardware", "element": "P_Low", "path": [{"field": "trust"}]},
            "value": "trusted",
        }
        response, questions, _ = _through_the_engine(project, [operation])

        assert response["error"] == "consistency-violation", response
        assert response["message"].startswith(
            "Partition 'P_Low' (trusted, ASIL B) shares the processor with partition 'PARTITION0' (ASIL D)"
        )
        assert "additional" not in response, response
        assert questions == []
        assert "trust" not in _read_yaml(project / "model" / "hardware" / "P_Low.yaml")

    def test_a_project_that_declares_no_trust_is_asked_nothing_about_it(self, tmp_path: Path) -> None:
        """``examples/basic``: one partition, ``non_trusted`` by default; confining it further asks nothing."""
        delta, baseline, staged = _delta_over(
            tmp_path, EXAMPLES_BASIC_MODEL, _edit_hardware("PARTITION0", trust="trusted_with_protection")
        )
        assert safety_verdict(delta, baseline, staged=staged).changes == ()


# The guard reads an `instance` entry as the type it resolves to, as the same entry
# written with `swc`


def _argued_task_written_with_instances(root: Path) -> None:
    """``Task_Argued``'s two entries rewritten to ``instance``; each component is the root member of its name."""
    path = root / "tasks" / "Task_Argued.yaml"
    document = _read_yaml(path)
    document["runnables"] = [
        {"instance": entry["swc"], "runnable": entry["runnable"]} for entry in document["runnables"]
    ]
    _write_yaml(path, document)


def _join_task_by_instance(task: str, member: str, runnable: str) -> Callable[[Path], None]:
    """Schedule one more runnable in one task, naming the occurrence by ``instance``."""

    def edit(root: Path) -> None:
        path = root / "tasks" / f"{task}.yaml"
        document = _read_yaml(path)
        document["runnables"].append({"instance": member, "runnable": runnable})
        _write_yaml(path, document)

    return edit


class TestTheGuardReadsTheResolvedType:
    def test_a_QM_component_joining_an_ARGUED_task_by_instance_asks_as_by_swc(self, tmp_path: Path) -> None:
        """``test_a_QM_component_joining_an_ARGUED_task_asks``, the entry written with ``instance``."""
        by_instance = _verdict(
            tmp_path / "instance", _join_task_by_instance("Task_Homogeneous", "DiagnosticsAgent", "Diagnose")
        )
        by_swc = _verdict(tmp_path / "swc", _join_task("Task_Homogeneous", "DiagnosticsAgent", "Diagnose"))
        assert by_instance == by_swc
        assert [row[:3] for row in by_instance] == [("declare_coexistence", "Task_Homogeneous", "coexistence")]

    def test_removing_an_argued_task_whose_span_comes_through_instance_entries_is_a_withdrawal(
        self, tmp_path: Path
    ) -> None:
        """``_levels_spanned`` reads ``instance`` itself and resolves it over the baseline's compositions."""
        delta, baseline, staged = _outcome_delta(
            tmp_path,
            SAFETY_MODEL_ROOT,
            lambda root: (root / "tasks" / "Task_Argued.yaml").unlink(),
            prepare=_argued_task_written_with_instances,
        )
        assert any(entry.get("instance") for entry in baseline_task_runnables(baseline, "Task_Argued"))
        (change,) = safety_verdict(delta, baseline, staged=staged).changes
        assert (change.kind, change.resource, change.field) == (
            legacy_authorization.SafetyChangeKind.LOWER_ALLOCATION,
            "Task_Argued",
            "coexistence",
        )


def baseline_task_runnables(snapshot: ModelSnapshot, task: str) -> list[dict[str, Any]]:
    """One task's runnable entries as the snapshot dumps them."""
    (held,) = [candidate for candidate in snapshot.tasks if candidate.name == task]
    return [entry.model_dump() for entry in held.runnables]


# The rows a `confirm_links` update adds, under the two gates


class TestConfirmedLinksAreJudgedByBothGates:
    """The server's link rows are the delta's like the client's: an approved scope must cover them.

    On ``update-requirement-version-confirming-links``'s seed: ``REQ_EXAMPLE_001`` raised to version 2 confirms
    four component links (two own, one port's bare id) and the interface's ``@1``.
    """

    VECTOR = "update-requirement-version-confirming-links"
    VERSION = {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"field": "version"}]}

    @pytest.fixture
    def seeded(self, tmp_path: Path) -> Path:
        return fixtures.build_fixture(tmp_path / "project", self.VECTOR)

    def _ask(self, project: Path, scope: list[dict[str, Any]] | None) -> dict[str, Any]:
        from swcraft_tools.editing.engine import handle_request

        token = handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]
        return handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "operations": [{"operation": "update", "address": self.VERSION, "value": 2, "confirm_links": True}],
            },
            project=project,
            approved_scope=scope,
        )

    def test_a_scope_covering_the_update_alone_refuses_the_servers_rows(self, seeded: Path) -> None:
        answer = self._ask(seeded, [{"operation": "update", "address": self.VERSION}])
        assert answer["error"] == UNAUTHORIZED_DELTA
        named = {(address["layer"], address["element"]) for address in answer["addresses"]}
        assert named == {("swc", "ExampleSWC"), ("swc", "ExampleConsumerSWC"), ("interfaces", "IExample")}

    def test_a_scope_covering_every_row_admits_the_set(self, seeded: Path) -> None:
        scope = [{"operation": "update", "address": self.VERSION}] + [
            {"operation": operation, "address": {"layer": layer}}
            for operation in ("remove", "create")
            for layer in ("swc", "interfaces")
        ]
        answer = self._ask(seeded, scope)
        assert answer["status"] == "staged", answer
        assert {entry.get("origin") for entry in answer["delta"]} == {None, "server"}

    def test_a_client_operation_outside_the_scope_is_refused_before_the_model_is_read(
        self, seeded: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import swcraft_tools.editing.engine as engine

        def load(model_root: str) -> Any:
            raise AssertionError("the model was read before gate 1 refused the client's own operation")

        token = engine.handle_request({"operation": "discover", "form": "baseline"}, project=seeded)["baseline"]
        monkeypatch.setattr(engine, "load_snapshot", load)
        answer = engine.handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "operations": [{"operation": "update", "address": self.VERSION, "value": 2, "confirm_links": True}],
            },
            project=seeded,
            approved_scope=[],
        )
        assert answer["error"] == UNAUTHORIZED_DELTA


class TestStagingPairsWhatTheGuardPairs:
    """The guard's whole-element renames are staging's, case by case.

    :func:`_detect_renames` and staging's ``_rename_pairs`` call one function
    (``editing/renames.py``); these cases hold that both are handed the same
    values.
    """

    @staticmethod
    def _renamed_requirement(root: Path) -> None:
        held = _read_yaml(root / "requirements" / "REQ-SAFETY-002.yaml")
        held["id"] = "REQ-SAFETY-020"
        _write_yaml(root / "requirements" / "REQ-SAFETY-020.yaml", held)
        (root / "requirements" / "REQ-SAFETY-002.yaml").unlink()
        for bearer in ("SpeedSensor", "PlainWorker"):
            _satisfies_at(
                bearer, None, lambda links: ["REQ-SAFETY-020@1" if link == _SPEED_LINK else link for link in links]
            )(root)

    @staticmethod
    def _renamed_and_lowered(root: Path) -> None:
        _rename_component("TorqueLimiter", "TL2")(root)
        document = _read_yaml(root / "swc" / "safety" / "TL2.yaml")
        document["asil"] = "QM"
        _write_yaml(root / "swc" / "safety" / "TL2.yaml", document)

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("TorqueLimiter", {("swc", "TorqueLimiter"): "TL2"}),
            ("PlainWorker", {("swc", "PlainWorker"): "PW2"}),
            ("SpeedSensor", {("swc", "SpeedSensor"): "SS2"}),
            ("requirement", {("requirements", "REQ-SAFETY-002"): "REQ-SAFETY-020"}),
            ("renamed-and-lowered", {}),
        ],
    )
    def test_over_an_edited_model(self, case: str, expected: dict[tuple[str, str], str], tmp_path: Path) -> None:
        edits: dict[str, Callable[[Path], None]] = {
            "TorqueLimiter": _rename_component("TorqueLimiter", "TL2"),
            "PlainWorker": _rename_component("PlainWorker", "PW2"),
            "SpeedSensor": _rename_component("SpeedSensor", "SS2"),
            "requirement": self._renamed_requirement,
            "renamed-and-lowered": self._renamed_and_lowered,
        }
        from swcraft_tools.editing.safety_guard import _detect_renames

        delta, _baseline, _staged = _outcome_delta(tmp_path, SAFETY_MODEL_ROOT, edits[case])

        guarded = guarded_element_renames(_detect_renames(delta))

        assert guarded == expected
        assert staged_renames_of(delta) == guarded

    def test_through_the_engine_on_the_one_exemption(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        staged, guarded = spy_on_the_two_pairings(monkeypatch)
        carried = {**CLEAN_PROJECT["model/swc/app/Aux1.yaml"], "name": "AuxRenamed", "group": "app"}
        renamed = _drop_component("Aux1") + [
            {"operation": "create", "address": {"layer": "swc", "element": "AuxRenamed"}, "value": carried},
            {
                "operation": "create",
                "address": {
                    "layer": "compositions",
                    "element": "Root",
                    "path": [{"field": "members"}, {"member": "AuxRenamed"}],
                },
                "value": {"name": "AuxRenamed", "type": "AuxRenamed"},
            },
        ]

        assert _committed_with(tmp_path, renamed) == ("committed", [])
        assert staged == [{("swc", "Aux1"): "AuxRenamed"}]
        assert guarded and all(decided == staged[0] for decided in guarded)

    def test_through_the_engine_on_the_crosswise_twins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        staged, guarded = spy_on_the_two_pairings(monkeypatch)

        status, _shown = _committed_with(
            tmp_path, TestAnAmbiguousRenamePairingAsks._crosswise(), TestAnAmbiguousRenamePairingAsks._twins
        )

        assert status == "committed"
        assert staged == [{}]
        assert guarded and all(decided == {} for decided in guarded)
