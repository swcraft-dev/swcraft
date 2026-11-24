"""Tests for authorization.py: safety-act classification.

The guard is always on, scoped session or not. Its direction comes from ``resource_analysis``'s
``limit = size x threshold``: raising a ``threshold``, enlarging a memory's ``size`` and going from ``strict``
to ``advisory`` weaken a check; their inverses do not. A verdict carries every weakening change a call makes,
not the first one found: an approval of a third of the change is not knowingly given.

- ``WIDEN_ACCESS`` is asymmetric: admitting a core to a memory's ``accessible`` weakens, withdrawing one
  tightens, reordering changes nothing. Its member-shaped delta is ``editing/safety_guard.py``'s; this
  module's input is the field's whole new value.
- ``RAISE_CAPABILITY`` (a core's or memory's ``asil_capability`` raised, or cleared below ``D``) weakens the
  placement rules, not the budget arithmetic. It compares the sets of levels an element refuses, so
  ``A -> None`` weakens and ``D -> None`` is silent. For this optional field a key present with ``None`` is a
  clear; the budget fields keep ``None`` as "leave alone".
- A ``processor`` declares no budget field, so ``TestTheProcessorIsNotABudget`` states that it weakens
  nothing rather than leaving it to an ``AttributeError``.
"""

from __future__ import annotations

from typing import Any

import pytest
from swcraft_tools.authorization import (
    _SAFETY_FIELD_ORDER,
    SafetyChange,
    SafetyChangeKind,
    SafetyClassificationError,
    SafetyVerdict,
    classify_safety_act,
)
from swcraft_tools.editing import authorization as editing_authorization
from swcraft_tools.editing import safety_guard as editing_safety_guard
from swcraft_tools.schema import Core, Hardware, Memory, Processor

# Fixtures — a `strict` memory and a `strict` core beside the processor, where the weakening acts are hardest.


def _pool(
    name: str = "RamPool",
    *,
    size: int = 65536,
    threshold: float = 0.8,
    enforcement: str = "strict",
    accessible: list[str] | None = None,
    asil_capability: str | None = None,
) -> Memory:
    # `accessible` is a parameter because a narrowing needs a baseline listing more than one core.
    return Memory(
        name=name,
        kind="memory",
        type="RAM",
        size=size,
        accessible=["CORE0"] if accessible is None else accessible,
        threshold=threshold,
        enforcement=enforcement,  # type: ignore[arg-type]
        asil_capability=asil_capability,  # type: ignore[arg-type]
    )


def _core(
    name: str = "CORE0",
    *,
    threshold: float = 0.7,
    enforcement: str = "strict",
    asil_capability: str | None = None,
) -> Core:
    return Core(
        name=name,
        kind="core",
        threshold=threshold,
        enforcement=enforcement,  # type: ignore[arg-type]
        asil_capability=asil_capability,  # type: ignore[arg-type]
    )


def _rated(core: str | None = None, memory: str | None = None) -> list[Hardware]:
    """:func:`_model` with a declared capability on the core and on the memory."""
    return [_processor(), _pool(asil_capability=memory), _core(asil_capability=core)]


#: The five levels in increasing order, spelled out HERE rather than imported
#: from `safety.py`: the sweep below is the classifier's oracle, and an oracle
#: that read the implementation's own tuple would agree with it by construction.
_LEVELS_IN_ORDER = ("QM", "A", "B", "C", "D")


def _refused_by(capability: str | None) -> frozenset[str]:
    """Every component level a placement rule reports under *capability*; an unrated element refuses none."""
    if capability is None:
        return frozenset()
    return frozenset(_LEVELS_IN_ORDER[_LEVELS_IN_ORDER.index(capability) + 1 :])


def _processor(name: str = "CPU") -> Processor:
    return Processor(name=name, kind="processor")


def _model() -> list[Hardware]:
    """The layer as a project really has it: one processor, one core, one memory."""
    return [_processor(), _pool(), _core()]


def _kinds(verdict: SafetyVerdict) -> list[SafetyChangeKind]:
    return [change.kind for change in verdict.changes]


def _by_field(verdict: SafetyVerdict, field: str) -> SafetyChange:
    matching = [change for change in verdict.changes if change.field == field]
    assert len(matching) == 1, f"expected exactly one '{field}' change, got {verdict.changes}"
    return matching[0]


# Weakening — each of the three, with its old/new values


class TestWeakening:
    def test_strict_to_advisory_is_weakening(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "enforcement": "advisory"},
            _model(),
        )

        assert verdict.is_safety_act
        assert _kinds(verdict) == [SafetyChangeKind.WEAKEN_ENFORCEMENT]
        change = _by_field(verdict, "enforcement")
        assert change.resource == "RamPool"
        assert change.old == "strict"
        assert change.new == "advisory"

    def test_raising_threshold_is_weakening(self) -> None:
        """limit = size x threshold, so a HIGHER threshold permits more load."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "threshold": 0.95},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_THRESHOLD]
        change = _by_field(verdict, "threshold")
        assert (change.old, change.new) == (0.8, 0.95)

    def test_raising_a_core_threshold_is_weakening(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {"name": "CORE0", "threshold": 0.99},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_THRESHOLD]
        assert (_by_field(verdict, "threshold").old, _by_field(verdict, "threshold").new) == (0.7, 0.99)

    def test_enlarging_a_memory_is_weakening(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "size": 131072},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.ENLARGE_POOL]
        change = _by_field(verdict, "size")
        assert (change.old, change.new) == (65536, 131072)

    def test_admitting_a_core_to_a_memory_is_weakening(self) -> None:
        """Admitting a core to a memory is a weakening: `hardware.unreachable-memory` stops refusing it."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "accessible": ["CORE0", "CORE1"]},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.WIDEN_ACCESS]
        change = _by_field(verdict, "accessible")
        assert (change.old, change.new) == (["CORE0"], ["CORE0", "CORE1"])

    def test_a_widening_renders_the_admitted_cores_and_not_the_two_lists(self) -> None:
        """`describe()` renders the admitted cores, not the two lists a reviewer would have to diff."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "accessible": ["CORE0", "CORE1", "CORE2"]},
            _model(),
        )

        rendered = verdict.changes[0].describe()
        assert rendered.startswith("hardware element 'RamPool': accessible admits CORE1, CORE2 — ")
        assert "hardware.unreachable-memory" in rendered

    def test_every_change_names_the_element_and_renders_old_to_new(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "size": 131072},
            _model(),
        )

        rendered = verdict.changes[0].describe()
        assert rendered.startswith("hardware element 'RamPool': size 65536 -> 131072 — ")
        assert "limit = size x threshold" in rendered

    def test_raising_a_cores_capability_is_a_raise_capability(self) -> None:
        """Raising a core's capability is a raise: `safety.partition-on-unqualified-core` reports fewer partitions."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": "C"}, _rated(core="A"))

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_CAPABILITY]
        change = _by_field(verdict, "asil_capability")
        assert (change.resource, change.old, change.new) == ("CORE0", "A", "C")

    def test_raising_a_memorys_capability_is_a_raise_capability(self) -> None:
        verdict = classify_safety_act(
            "update_resource", {"name": "RamPool", "asil_capability": "D"}, _rated(memory="B")
        )

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_CAPABILITY]
        change = _by_field(verdict, "asil_capability")
        assert (change.resource, change.old, change.new) == ("RamPool", "B", "D")

    def test_clearing_a_capability_below_d_is_a_raise_capability(self) -> None:
        """`A -> None` is a raise: an unrated element refuses nothing, and a present `None` is the clear."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": None}, _rated(core="A"))

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_CAPABILITY]
        change = _by_field(verdict, "asil_capability")
        assert (change.old, change.new) == ("A", None)

    def test_a_capability_change_names_both_placement_rules_and_reads_none_for_a_clear(self) -> None:
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": None}, _rated(core="A"))

        rendered = verdict.changes[0].describe()
        assert rendered.startswith("hardware element 'CORE0': asil_capability A -> none — "), rendered
        assert "safety.partition-on-unqualified-core" in rendered
        assert "safety.memory-below-component" in rendered


class TestTheCapabilityIsDecidedByTheSetOfLevelsItRefuses:
    """The capability's direction, swept over every pair of states on a core and a memory.

    A change weakens exactly when the set of levels the element refuses shrinks strictly. The oracle,
    :func:`_refused_by`, is written from the placement rules' wording, not imported.
    """

    STATES = (None, *_LEVELS_IN_ORDER)

    @pytest.mark.parametrize("before", STATES, ids=lambda s: f"from-{s}")
    @pytest.mark.parametrize("after", STATES, ids=lambda s: f"to-{s}")
    @pytest.mark.parametrize("element", ["CORE0", "RamPool"])
    def test_a_move_weakens_exactly_when_the_refused_set_shrinks(
        self, element: str, before: str | None, after: str | None
    ) -> None:
        model = _rated(core=before) if element == "CORE0" else _rated(memory=before)
        verdict = classify_safety_act("update_resource", {"name": element, "asil_capability": after}, model)

        expected = _refused_by(after) < _refused_by(before)
        assert verdict.is_safety_act is expected, (before, after)
        if expected:
            assert [(c.kind, c.old, c.new) for c in verdict.changes] == [
                (SafetyChangeKind.RAISE_CAPABILITY, before, after)
            ]


# The combined case — every change, not the first one found


class TestCombinedCall:
    def test_one_call_weakening_three_ways_reports_all_three(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {
                "name": "RamPool",
                "threshold": 0.95,
                "size": 131072,
                "enforcement": "advisory",
            },
            _model(),
        )

        assert set(_kinds(verdict)) == {
            SafetyChangeKind.RAISE_THRESHOLD,
            SafetyChangeKind.ENLARGE_POOL,
            SafetyChangeKind.WEAKEN_ENFORCEMENT,
        }
        assert len(verdict.changes) == 3
        assert (_by_field(verdict, "threshold").old, _by_field(verdict, "threshold").new) == (0.8, 0.95)
        assert (_by_field(verdict, "size").old, _by_field(verdict, "size").new) == (65536, 131072)
        assert (_by_field(verdict, "enforcement").old, _by_field(verdict, "enforcement").new) == (
            "strict",
            "advisory",
        )

    def test_the_weakening_half_of_a_mixed_call_is_reported(self) -> None:
        """Lowering the threshold does not excuse enlarging the pool."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "threshold": 0.5, "size": 131072},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.ENLARGE_POOL]

    def test_changes_are_ordered_stably(self) -> None:
        args: dict[str, Any] = {
            "name": "RamPool",
            "accessible": ["CORE0", "CORE1"],
            "enforcement": "advisory",
            "size": 131072,
            "threshold": 0.95,
        }
        first = classify_safety_act("update_resource", args, _model())
        second = classify_safety_act("update_resource", dict(reversed(list(args.items()))), _model())

        # Pinned against the literal order: two runs agreeing on a wrong order would still agree.
        # `accessible` is last because `_SAFETY_FIELD_ORDER` keeps the order a shipped prompt rendered in.
        expected = [
            SafetyChangeKind.RAISE_THRESHOLD,
            SafetyChangeKind.ENLARGE_POOL,
            SafetyChangeKind.WEAKEN_ENFORCEMENT,
            SafetyChangeKind.WIDEN_ACCESS,
        ]
        assert _kinds(first) == expected
        assert _kinds(second) == expected

    def test_a_threshold_and_a_capability_raised_on_one_core_are_two_changes_capability_last(self) -> None:
        """A raised threshold and capability on one core are two changes, the capability rendered last.

        A new field is appended to `_SAFETY_FIELD_ORDER`, never inserted; the argument order decides nothing.
        """
        args: dict[str, Any] = {"name": "CORE0", "asil_capability": "D", "threshold": 0.9}
        first = classify_safety_act("update_resource", args, _rated(core="A"))
        second = classify_safety_act("update_resource", dict(reversed(list(args.items()))), _rated(core="A"))

        expected = [SafetyChangeKind.RAISE_THRESHOLD, SafetyChangeKind.RAISE_CAPABILITY]
        assert _kinds(first) == expected
        assert _kinds(second) == expected


# The inverses — none of them is a safety act


class TestNotWeakening:
    def test_lowering_threshold_is_not_weakening(self) -> None:
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "threshold": 0.5}, _model())

        assert verdict.changes == ()
        assert not verdict.is_safety_act

    def test_shrinking_a_memory_is_not_weakening(self) -> None:
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "size": 4096}, _model())

        assert verdict.changes == ()

    def test_advisory_to_strict_is_not_weakening(self) -> None:
        model = [_pool(enforcement="advisory")]
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "enforcement": "strict"}, model)

        assert verdict.changes == ()

    def test_withdrawing_a_core_is_not_weakening(self) -> None:
        """Withdrawing a core is a tightening, not a weakening; the asymmetry is deliberate."""
        model = [_processor(), _pool(accessible=["CORE0", "CORE1"]), _core()]
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "accessible": ["CORE0"]}, model)

        assert verdict.changes == ()

    def test_reordering_the_accessible_list_is_not_weakening(self) -> None:
        # The same set, differently spelled: nobody is admitted, so no check
        # stops refusing anything.
        model = [_processor(), _pool(accessible=["CORE0", "CORE1"]), _core()]
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "accessible": ["CORE1", "CORE0"]}, model)

        assert verdict.changes == ()

    def test_accessible_on_a_core_claims_nothing(self) -> None:
        # A call naming the field on a core is refused by the writer; the classifier still answers.
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "accessible": ["CORE0"]}, _model())

        assert verdict.changes == ()

    def test_a_no_op_update_is_not_weakening(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {
                "name": "RamPool",
                "threshold": 0.8,
                "size": 65536,
                "enforcement": "strict",
                "accessible": ["CORE0"],
                "description": "unchanged",
            },
            _model(),
        )

        assert verdict.changes == ()

    def test_a_description_only_update_is_not_weakening(self) -> None:
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "description": "governs the diagnostic pool"},
            _model(),
        )

        assert verdict.changes == ()

    def test_none_arguments_are_unchanged_fields(self) -> None:
        """`None` is `update_resource`'s "leave alone", not a new value."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "RamPool", "threshold": None, "size": None, "enforcement": None},
            _model(),
        )

        assert verdict.changes == ()

    def test_an_already_advisory_element_going_advisory_is_not_weakening(self) -> None:
        model = [_pool(enforcement="advisory")]
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "enforcement": "advisory"}, model)

        assert verdict.changes == ()

    def test_declaring_a_capability_where_none_stood_is_not_weakening(self) -> None:
        """`None -> A`: nothing was refused before, and `B` to `D` are now. A tightening."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": "A"}, _rated())

        assert verdict.changes == ()

    def test_declaring_d_where_none_stood_is_not_weakening(self) -> None:
        """`None -> D`: nothing refused before, nothing refused after. Not a move at all."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": "D"}, _rated())

        assert verdict.changes == ()

    def test_clearing_a_d_capability_is_not_weakening(self) -> None:
        """`D -> None` is silent: a `D` capability and an unrated element both refuse nothing."""
        verdict = classify_safety_act(
            "update_resource", {"name": "RamPool", "asil_capability": None}, _rated(memory="D")
        )

        assert verdict.changes == ()

    def test_lowering_a_capability_is_not_weakening(self) -> None:
        """`C -> A`: the element refuses `B` and `C` as well now, so the rules refuse more."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": "A"}, _rated(core="C"))

        assert verdict.changes == ()

    def test_rewriting_the_same_capability_is_not_weakening(self) -> None:
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": "B"}, _rated(core="B"))

        assert verdict.changes == ()

    def test_an_absent_capability_key_leaves_a_rated_element_untouched(self) -> None:
        """An absent `asil_capability` key leaves a rated element untouched."""
        verdict = classify_safety_act(
            "update_resource",
            {"name": "CORE0", "threshold": None, "size": None, "enforcement": None, "accessible": None},
            _rated(core="A"),
        )

        assert verdict.changes == ()

    def test_an_explicit_none_capability_beside_the_budget_nones_is_a_clear(self) -> None:
        """A present `None` capability is a clear, while the budget `None`s stay "leave alone".

        The budget fields are required on the element, so `None` can only mean "not passed" for them.
        """
        verdict = classify_safety_act(
            "update_resource",
            {
                "name": "RamPool",
                "threshold": None,
                "size": None,
                "enforcement": None,
                "accessible": None,
                "asil_capability": None,
            },
            _rated(memory="B"),
        )

        assert _kinds(verdict) == [SafetyChangeKind.RAISE_CAPABILITY]
        assert (verdict.changes[0].old, verdict.changes[0].new) == ("B", None)


# Deletion


class TestDeletion:
    def test_deleting_a_memory_is_a_safety_act(self) -> None:
        verdict = classify_safety_act(
            "delete_element",
            {"element_type": "hardware", "name": "RamPool"},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.DELETE_RESOURCE]
        change = verdict.changes[0]
        assert change.resource == "RamPool"
        assert change.new is None
        assert change.old == "kind=memory, size=65536, threshold=0.8, enforcement=strict"

    def test_deleting_a_core_is_a_safety_act_and_renders_no_size(self) -> None:
        """Deleting a core renders no `size`, and no placeholder in its place."""
        verdict = classify_safety_act(
            "delete_element",
            {"element_type": "hardware", "name": "CORE0"},
            _model(),
        )

        assert _kinds(verdict) == [SafetyChangeKind.DELETE_RESOURCE]
        assert verdict.changes[0].old == "kind=core, threshold=0.7, enforcement=strict"

    def test_deleting_a_rated_core_renders_the_capability_that_goes_with_it(self) -> None:
        """A rated element's removal takes its declaration with it, so the prompt says which."""
        verdict = classify_safety_act("delete_element", {"element_type": "hardware", "name": "CORE0"}, _rated(core="B"))

        assert verdict.changes[0].old == "kind=core, threshold=0.7, enforcement=strict, asil_capability=B"

    def test_deleting_a_rated_memory_renders_the_capability_after_the_budget(self) -> None:
        verdict = classify_safety_act(
            "delete_element", {"element_type": "hardware", "name": "RamPool"}, _rated(memory="QM")
        )

        assert (
            verdict.changes[0].old == "kind=memory, size=65536, threshold=0.8, enforcement=strict, asil_capability=QM"
        )

    @pytest.mark.parametrize("element_type", ["swc", "requirement", "connector"])
    def test_deleting_a_non_hardware_element_is_not_a_safety_act(self, element_type: str) -> None:
        verdict = classify_safety_act(
            "delete_element",
            {"element_type": element_type, "name": "EngineController"},
            _model(),
        )

        assert verdict.changes == ()

    def test_both_steps_of_the_two_call_protocol_classify_the_same(self) -> None:
        """The two-call bookkeeping is the host's; the classifier is stateless."""
        step1 = classify_safety_act(
            "delete_element",
            {"element_type": "hardware", "name": "RamPool"},
            _model(),
        )
        step2 = classify_safety_act(
            "delete_element",
            {"element_type": "hardware", "name": "RamPool", "confirmation_token": "abc-123"},
            _model(),
        )

        assert step1.changes == step2.changes


# The kind the layer carries that is no budget


class TestTheProcessorIsNotABudget:
    """A processor declares no budget, and every branch answers for it.

    Otherwise an `AttributeError` would make the guard raise where it should say "nothing weakened".
    """

    def test_deleting_the_processor_is_not_a_safety_act(self) -> None:
        """Removing the processor is a model error (`hardware.no-processor`), not a weakened check."""
        verdict = classify_safety_act("delete_element", {"element_type": "hardware", "name": "CPU"}, _model())

        assert verdict.changes == ()
        assert not verdict.is_safety_act

    @pytest.mark.parametrize(
        "args",
        [
            {"threshold": 0.99},
            {"size": 131072},
            {"enforcement": "advisory"},
            {"threshold": 0.99, "size": 131072, "enforcement": "advisory"},
        ],
    )
    def test_updating_a_processor_with_budget_arguments_claims_nothing(self, args: dict[str, Any]) -> None:
        """Budget arguments on a processor give a verdict, not a raise; the writer refuses the call."""
        verdict = classify_safety_act("update_resource", {"name": "CPU", **args}, _model())

        assert verdict.changes == ()

    @pytest.mark.parametrize("value", ["D", None])
    def test_a_capability_on_a_processor_claims_nothing(self, value: str | None) -> None:
        """A capability on a processor, set or cleared, gives an empty verdict; the writer refuses the key."""
        verdict = classify_safety_act("update_resource", {"name": "CPU", "asil_capability": value}, _rated(core="A"))

        assert verdict.changes == ()

    def test_a_capability_outside_the_five_levels_fails_closed_on_a_processor_too(self) -> None:
        """An unknown capability on a processor fails closed: the value is validated before the kind is read."""
        with pytest.raises(SafetyClassificationError, match="asil_capability"):
            classify_safety_act("update_resource", {"name": "CPU", "asil_capability": "E"}, _rated(core="A"))

    def test_an_unknown_processor_still_fails_closed(self) -> None:
        """An unknown processor name still fails closed: the kind is read after the lookup."""
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("delete_element", {"element_type": "hardware", "name": "CPU2"}, _model())


# Tools that are never safety acts


class TestOtherTools:
    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("add_resource", {"name": "NewPool", "kind": "memory", "size": 1024, "threshold": 0.9}),
            ("set_task_wcet", {"task_name": "Task10ms", "wcet": "4000us"}),
            ("set_swc_memory_budget", {"swc_name": "EngineController", "pool": "RamPool", "size": 512}),
            ("add_swc", {"name": "EngineController"}),
            ("read_swc", {"name": "EngineController"}),
        ],
    )
    def test_non_weakening_tools_yield_an_empty_verdict(self, tool_name: str, args: dict[str, Any]) -> None:
        verdict = classify_safety_act(tool_name, args, _model())

        assert verdict.changes == ()
        assert verdict.tool == tool_name

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("set_task_wcet", {"task_name": "Task10ms", "wcet": "1us"}),
            ("set_swc_memory_budget", {"swc_name": "EngineController", "pool": "RamPool", "size": 1}),
        ],
    )
    def test_lowering_a_consumer_budget_is_deliberately_not_a_safety_act(
        self, tool_name: str, args: dict[str, Any]
    ) -> None:
        """Lowering a consumer budget is deliberately not a safety act.

        It can clear a ``threshold_breach``, but it changes an input, not the check: a budget is ordinary model
        content, visible in the diff and ``swcraft resource-report``, and too many edits change the arithmetic
        to enumerate. See ``classify_safety_act``'s docstring.
        """
        assert classify_safety_act(tool_name, args, _model()).changes == ()


# Fail closed


class TestFailsClosed:
    def test_an_unknown_element_raises_rather_than_reporting_no_weakening(self) -> None:
        with pytest.raises(SafetyClassificationError) as exc:
            classify_safety_act("update_resource", {"name": "NoSuchPool", "threshold": 0.99}, _model())

        assert "NoSuchPool" in str(exc.value)

    def test_an_unknown_element_on_delete_raises(self) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("delete_element", {"element_type": "hardware", "name": "NoSuchPool"}, _model())

    def test_an_empty_model_raises_for_a_named_element(self) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": "RamPool", "threshold": 0.99}, [])

    def test_a_duplicate_hardware_name_raises(self) -> None:
        model = [_pool(threshold=0.5), _pool(threshold=0.9)]

        with pytest.raises(SafetyClassificationError) as exc:
            classify_safety_act("update_resource", {"name": "RamPool", "threshold": 0.99}, model)

        assert "RamPool" in str(exc.value)

    @pytest.mark.parametrize("name", [None, "", "   ", 17])
    def test_a_missing_or_unusable_name_raises(self, name: Any) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": name, "threshold": 0.99}, _model())

    @pytest.mark.parametrize("threshold", ["0.99", True, object()])
    def test_a_non_numeric_threshold_raises(self, threshold: Any) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": "RamPool", "threshold": threshold}, _model())

    @pytest.mark.parametrize("size", ["131072", 131072.0, True])
    def test_a_non_integer_size_raises(self, size: Any) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": "RamPool", "size": size}, _model())

    def test_a_non_string_enforcement_raises(self) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": "RamPool", "enforcement": 1}, _model())

    @pytest.mark.parametrize("accessible", ["CORE0", {"member": "CORE0"}, ["CORE0", 1], 1])
    def test_a_non_list_of_names_accessible_raises(self, accessible: Any) -> None:
        """A non-list `accessible` raises: a bare string would compare characters against core names."""
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("update_resource", {"name": "RamPool", "accessible": accessible}, _model())

    @pytest.mark.parametrize("value", ["E", "qm", "d", "", 3, True, ["D"]])
    def test_a_capability_outside_the_five_levels_raises(self, value: Any) -> None:
        """A capability outside the five levels raises rather than ranking a typo as "not weakening"."""
        with pytest.raises(SafetyClassificationError, match="asil_capability"):
            classify_safety_act("update_resource", {"name": "CORE0", "asil_capability": value}, _rated(core="A"))

    def test_size_on_a_core_claims_nothing(self) -> None:
        """A core has no memory to enlarge, and the writer rejects the call."""
        verdict = classify_safety_act("update_resource", {"name": "CORE0", "size": 131072}, _model())

        assert verdict.changes == ()

    def test_delete_element_without_an_element_type_raises(self) -> None:
        with pytest.raises(SafetyClassificationError):
            classify_safety_act("delete_element", {"name": "RamPool"}, _model())

    def test_a_safety_field_with_no_branch_of_its_own_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A safety field in the order without a classifier branch fails closed.

        A trailing `else` serving as the `enforcement` arm would answer a silent "not weakening" for it.
        """
        monkeypatch.setattr("swcraft_tools.authorization._SAFETY_FIELD_ORDER", (*_SAFETY_FIELD_ORDER, "cache_policy"))

        with pytest.raises(SafetyClassificationError) as exc:
            classify_safety_act("update_resource", {"name": "RamPool", "cache_policy": "write-back"}, _model())

        assert "cache_policy" in str(exc.value)


# Value semantics


class TestVerdictShape:
    def test_a_verdict_is_frozen_and_its_changes_are_a_tuple(self) -> None:
        verdict = classify_safety_act("update_resource", {"name": "RamPool", "threshold": 0.95}, _model())

        assert isinstance(verdict.changes, tuple)
        with pytest.raises(Exception):
            verdict.changes = ()  # type: ignore[misc]
        with pytest.raises(Exception):
            verdict.changes[0].old = 0.1  # type: ignore[misc]

    def test_is_safety_act_is_the_one_accessor_for_an_empty_verdict(self) -> None:
        """``is_safety_act`` is the one accessor for an empty verdict; ``__bool__`` is deliberately gone."""
        assert not classify_safety_act("add_swc", {"name": "X"}, _model()).is_safety_act
        assert classify_safety_act("update_resource", {"name": "RamPool", "threshold": 0.95}, _model()).is_safety_act
        assert "__bool__" not in vars(SafetyVerdict), "a second spelling of is_safety_act is what this removed"


# The two call names the classifier dispatches on

#: The classifier's whole vocabulary, stated against the private pair ``editing/authorization.py`` defines:
#: ``editing/safety_guard.py`` synthesises these names, so a third weakening branch would never be asked about.
SAFETY_ACT_CALLS: frozenset[str] = frozenset({"update_resource", "delete_element"})

#: Representative non-weakening calls, one per shape (creation, nested setter, rename, delete of a
#: non-resource); they all reach the classifier's one `else` branch.
NON_SAFETY_CALLS: tuple[str, ...] = (
    "add_resource",
    "add_swc",
    "set_task_wcet",
    "set_swc_memory_budget",
    "rename_element",
    "create",
    "update",
    "remove",
    "",
)


class TestSafetyActCallsCompleteness:
    """The classifier branches on exactly two call names, and ``editing/safety_guard.py`` builds both.

    A third weakening branch would be a verdict the user is never asked about.
    """

    def test_the_editing_module_synthesises_exactly_these_two_call_names(self) -> None:
        assert {
            editing_authorization.UPDATE_RESOURCE,
            editing_authorization.DELETE_ELEMENT,
        } == SAFETY_ACT_CALLS

    def test_the_call_names_match_the_ones_the_classifier_branches_on(self) -> None:
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(classify_safety_act))
        branched: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "tool_name"
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)
            ):
                branched.add(node.comparators[0].value)

        assert branched, "the ast scan found no `tool_name == '...'` branch — did the shape change?"
        assert branched == set(SAFETY_ACT_CALLS), (
            "the classifier's branches and its consumers disagree; "
            f"unlisted: {sorted(branched - set(SAFETY_ACT_CALLS))}, "
            f"stale: {sorted(set(SAFETY_ACT_CALLS) - branched)}"
        )

    def test_no_call_outside_the_two_names_yields_a_non_empty_verdict(self) -> None:
        args = {"name": "RamPool", "threshold": 0.99, "size": 1 << 30, "enforcement": "advisory"}
        for call in NON_SAFETY_CALLS:
            assert call not in SAFETY_ACT_CALLS, call
            assert not classify_safety_act(call, args, _model()).is_safety_act, call

    def test_the_capability_is_classified_through_the_same_two_names_and_no_third(self) -> None:
        """The capability is classified through `update_resource`; every other name gives an empty verdict."""
        args = {"name": "RamPool", "asil_capability": None}
        assert classify_safety_act("update_resource", args, _rated(memory="A")).is_safety_act
        for call in NON_SAFETY_CALLS:
            assert not classify_safety_act(call, args, _rated(memory="A")).is_safety_act, call


# The prompt strings, one per kind — and the two fail-closed refusals


class TestEveryKindRendersAndHasAReason:
    """Every `SafetyChangeKind` renders and has a `_WEAKENING_REASONS` entry.

    A kind without a reason would raise `KeyError` inside a human approval prompt.
    """

    def test_every_kind_has_a_reason(self) -> None:
        from swcraft_tools.authorization import _WEAKENING_REASONS

        assert set(_WEAKENING_REASONS) == set(SafetyChangeKind)

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            (
                SafetyChange(SafetyChangeKind.WEAKEN_ENFORCEMENT, "CORE0", "enforcement", "strict", "advisory"),
                "hardware element 'CORE0': enforcement strict -> advisory",
            ),
            (
                SafetyChange(SafetyChangeKind.RAISE_THRESHOLD, "RAM0", "threshold", 0.8, 0.99),
                "hardware element 'RAM0': threshold 0.8 -> 0.99",
            ),
            (
                SafetyChange(SafetyChangeKind.ENLARGE_POOL, "RAM0", "size", 1024, 4096),
                "hardware element 'RAM0': size 1024 -> 4096",
            ),
            (
                SafetyChange(SafetyChangeKind.WIDEN_ACCESS, "RAM0", "accessible", ["CORE0"], ["CORE0", "CORE1"]),
                "hardware element 'RAM0': accessible admits CORE1",
            ),
            (
                SafetyChange(SafetyChangeKind.DELETE_RESOURCE, "RAM0", "", "memory RAM0", None),
                "delete hardware element 'RAM0' (memory RAM0)",
            ),
            (
                SafetyChange(SafetyChangeKind.DECLARE_MEASURE, "Ctl.rSpeed[speed]", "measures", None, "E2E profile 4"),
                "port measure 'Ctl.rSpeed[speed]': measures declares 'E2E profile 4'",
            ),
            (
                SafetyChange(SafetyChangeKind.DECLARE_COEXISTENCE, "Task_Mixed", "coexistence", "", "Separate cores."),
                "task 'Task_Mixed': coexistence declares 'Separate cores.'",
            ),
            (
                SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, "REQ_1", "asil", "D", None),
                "'REQ_1': asil D -> none",
            ),
            (
                SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, "Ctl", "satisfies", "REQ_1@1", None),
                "'Ctl': satisfies REQ_1@1 -> none",
            ),
            (
                SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, "Ctl.ports[pIn]", "satisfies", "REQ_1@1", None),
                "'Ctl.ports[pIn]': satisfies REQ_1@1 -> none",
            ),
            (
                SafetyChange(SafetyChangeKind.RAISE_CAPABILITY, "CORE0", "asil_capability", "B", "D"),
                "hardware element 'CORE0': asil_capability B -> D",
            ),
            (
                SafetyChange(SafetyChangeKind.RAISE_CAPABILITY, "RAM0", "asil_capability", "A", None),
                "hardware element 'RAM0': asil_capability A -> none",
            ),
            (
                SafetyChange(
                    SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE, "P_Body", "coexistence", "", "Separate regions."
                ),
                "partition 'P_Body': coexistence declares 'Separate regions.'",
            ),
            (
                SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, "P_Body", "asil", "B", "A"),
                "'P_Body': asil B -> A",
            ),
            (
                SafetyChange(SafetyChangeKind.GRANT_PARTITION_TRUST, "P_Body", "trust", "non_trusted", "trusted"),
                "partition 'P_Body': trust non_trusted -> trusted",
            ),
            (
                SafetyChange(
                    SafetyChangeKind.DECLARE_PARTITION_PROTECTION,
                    "P_Body",
                    "trust",
                    "trusted",
                    "trusted_with_protection",
                ),
                "partition 'P_Body': trust trusted -> trusted_with_protection",
            ),
        ],
        ids=lambda value: value.kind.value if isinstance(value, SafetyChange) else "",
    )
    def test_the_rendering_opens_with_what_the_change_is(self, change: SafetyChange, expected: str) -> None:
        rendered = change.describe()

        assert rendered.startswith(expected), rendered
        # The reason is what makes the line answerable, and it is the kind's own.
        from swcraft_tools.authorization import _WEAKENING_REASONS

        assert rendered.endswith(_WEAKENING_REASONS[change.kind])

    def test_a_cleared_level_reads_none_on_both_sides(self) -> None:
        """The reader is looking at YAML, where the key is simply absent."""
        change = SafetyChange(SafetyChangeKind.LOWER_ALLOCATION, "ISpeed.speed", "asil", None, None)

        assert change.describe().startswith("'ISpeed.speed': asil none -> none")


class TestTheTrustClassifier:
    """``classify_trust_act`` asks a movement across ``trusted`` and nothing else.

    The grant is the one kind that is not a weakening; its prompt names no rule.
    """

    @pytest.mark.parametrize(
        ("old", "new", "kind"),
        [
            ("non_trusted", "trusted", SafetyChangeKind.GRANT_PARTITION_TRUST),
            ("trusted_with_protection", "trusted", SafetyChangeKind.GRANT_PARTITION_TRUST),
            ("trusted", "trusted_with_protection", SafetyChangeKind.DECLARE_PARTITION_PROTECTION),
            ("trusted", "non_trusted", SafetyChangeKind.DECLARE_PARTITION_PROTECTION),
        ],
    )
    def test_a_movement_across_trusted_is_one_kind(self, old: str, new: str, kind: SafetyChangeKind) -> None:
        from swcraft_tools.authorization import classify_trust_act

        verdict = classify_trust_act("P_Body", old, new)

        assert verdict.tool == "P_Body"
        assert verdict.changes == (SafetyChange(kind, "P_Body", "trust", old, new),)

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("non_trusted", "trusted_with_protection"),
            ("trusted_with_protection", "non_trusted"),
            ("trusted", "trusted"),
            ("non_trusted", "non_trusted"),
        ],
    )
    def test_a_movement_between_the_confined_classes_asks_nothing(self, old: str, new: str) -> None:
        from swcraft_tools.authorization import classify_trust_act

        assert classify_trust_act("P_Body", old, new).changes == ()

    def test_the_class_the_classifier_turns_on_is_one_the_schema_declares(self) -> None:
        """``_TRUSTED`` is named once in the module; this holds it to ``Partition.trust``'s ``Literal``."""
        from typing import get_args

        from swcraft_tools.authorization import _TRUST_CLASSES, _TRUSTED
        from swcraft_tools.schema import Partition

        assert _TRUSTED == "trusted"
        assert _TRUSTED in get_args(Partition.model_fields["trust"].annotation)
        assert _TRUSTED in _TRUST_CLASSES

    @pytest.mark.parametrize(("old", "new"), [("trusted", "untrusted"), (None, "trusted"), ("trusted", "")])
    def test_a_value_outside_the_three_classes_is_refused(self, old: Any, new: Any) -> None:
        from swcraft_tools.authorization import classify_trust_act

        with pytest.raises(SafetyClassificationError, match="partition trust class"):
            classify_trust_act("P_Body", old, new)

    def test_the_two_kinds_are_appended_so_no_member_moved(self) -> None:
        members = list(SafetyChangeKind)

        assert len(members) == 12
        assert members[-2:] == [SafetyChangeKind.GRANT_PARTITION_TRUST, SafetyChangeKind.DECLARE_PARTITION_PROTECTION]
        assert members[9] is SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE

    def test_the_grant_s_prompt_names_no_rule_and_the_protection_s_names_both(self) -> None:
        """The grant fires no rule, so its reason names none; the protection silences two, and names them."""
        from swcraft_tools.authorization import _WEAKENING_REASONS

        grant = _WEAKENING_REASONS[SafetyChangeKind.GRANT_PARTITION_TRUST]
        protection = _WEAKENING_REASONS[SafetyChangeKind.DECLARE_PARTITION_PROTECTION]

        assert "safety." not in grant
        assert "widens what the partition may write" in grant
        assert "safety.trusted-partition-below-processor-level" in protection
        assert "safety.trusted-partition-unrated" in protection


class TestTheTwoNewClassifiersFailClosed:
    """The classifiers fail closed when called directly; `safety_verdict` admits only known fields."""

    def test_an_unknown_argument_field_is_refused(self) -> None:
        from swcraft_tools.authorization import classify_argument_act

        with pytest.raises(SafetyClassificationError, match="declared-argument field 'rationale'"):
            classify_argument_act("Task_Mixed", "rationale", "", "text")

    def test_a_coexistence_of_a_layer_that_declares_none_is_refused(self) -> None:
        """``tasks`` and ``hardware`` declare the coexistence argument, nothing else does."""
        from swcraft_tools.authorization import classify_argument_act

        with pytest.raises(SafetyClassificationError, match="coexistence argument in layer 'swc'"):
            classify_argument_act("Ctl", "coexistence", "", "text", layer="swc")

    def test_the_layer_keyword_chooses_the_kind_and_defaults_to_the_task(self) -> None:
        from swcraft_tools.authorization import classify_argument_act

        (task,) = classify_argument_act("Task_Mixed", "coexistence", "", "text").changes
        (partition,) = classify_argument_act("P_Body", "coexistence", "", "text", layer="hardware").changes

        assert task.kind is SafetyChangeKind.DECLARE_COEXISTENCE
        assert partition.kind is SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE
        # The value shape is the task's: withdrawing and rewording are not acts.
        assert classify_argument_act("P_Body", "coexistence", "text", "", layer="hardware").changes == ()
        assert classify_argument_act("P_Body", "coexistence", "a", "b", layer="hardware").changes == ()

    def test_the_walk_passes_its_layer_to_the_classifier(self) -> None:
        """``_declared_argument_acts`` passes its layer to ``classify_argument_act``.

        A first caller walking a ``hardware`` value must get the partition's kind, not a task's prompt.
        """
        from swcraft_tools.editing.safety_guard import _declared_argument_acts

        (task,) = _declared_argument_acts({"name": "T", "coexistence": "text"}, "T", layer="tasks")
        (partition,) = _declared_argument_acts({"name": "P_Body", "coexistence": "text"}, "P_Body", layer="hardware")

        assert task.kind is SafetyChangeKind.DECLARE_COEXISTENCE
        assert partition.kind is SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE

    def test_the_task_s_sentence_did_not_move_for_the_partition(self) -> None:
        """``DECLARE_COEXISTENCE``'s reason is byte-identical, so no shipped prompt moved for the partition."""
        from swcraft_tools.authorization import _WEAKENING_REASONS

        assert _WEAKENING_REASONS[SafetyChangeKind.DECLARE_COEXISTENCE] == (
            "a declared coexistence argument answers safety.task-mixed-asil, so the check stops "
            "refusing a task whose components carry different safety integrity levels"
        )
        assert "safety.component-below-partition" in _WEAKENING_REASONS[SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE]

    def test_a_partition_removed_through_the_hardware_classifier_is_no_budget_act(self) -> None:
        """``delete_element`` decides a ``Core`` or a ``Memory``; a partition's removal is the comparison's."""
        from swcraft_tools.schema import Partition

        hardware = (Partition(name="P_Body", kind="partition", core="CORE0", asil="D", coexistence="argued"),)

        assert (
            classify_safety_act("delete_element", {"element_type": "hardware", "name": "P_Body"}, hardware).changes
            == ()
        )

    def test_an_unknown_allocation_field_is_refused(self) -> None:
        from swcraft_tools.authorization import classify_allocation_act

        with pytest.raises(SafetyClassificationError, match="allocation field 'integrity'"):
            classify_allocation_act("REQ_1", "integrity", "D", "QM")

    def test_two_nones_are_nothing_withdrawn(self) -> None:
        """Two `None` levels are nothing withdrawn.

        Guards a past defect: a `None` level ranked below every level made each requirement deletion on a
        project without ASIL a second review question.
        """
        from swcraft_tools.authorization import classify_allocation_act

        assert classify_allocation_act("REQ_1", "asil", None, None).changes == ()


class TestADecomposedLevelMovesByX:
    """The allocation classifier's direction is decided by `X`; a change of `(Y)` alone is neither way."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [("B", "B(D)"), ("B(D)", "B"), ("B(C)", "B(D)"), ("B(D)", "B(C)"), ("QM", "QM(D)"), ("QM(A)", "QM")],
    )
    def test_a_change_of_y_alone_is_no_level_change(self, old: str, new: str) -> None:
        from swcraft_tools.authorization import classify_allocation_act

        assert classify_allocation_act("REQ_1", "asil", old, new).changes == ()

    @pytest.mark.parametrize(("old", "new"), [("B(D)", "A(D)"), ("B", "A(D)"), ("B(D)", "QM(D)"), ("A(B)", "QM")])
    def test_a_lower_x_is_a_lowering(self, old: str, new: str) -> None:
        from swcraft_tools.authorization import classify_allocation_act

        (change,) = classify_allocation_act("REQ_1", "asil", old, new).changes

        assert change.kind is SafetyChangeKind.LOWER_ALLOCATION
        assert (change.old, change.new) == (old, new)

    @pytest.mark.parametrize(("old", "new"), [("A(D)", "B"), ("QM(D)", "A(B)")])
    def test_a_higher_x_is_no_weakening(self, old: str, new: str) -> None:
        from swcraft_tools.authorization import classify_allocation_act

        assert classify_allocation_act("REQ_1", "asil", old, new).changes == ()


class TestTheLayerDispatchFailsClosed:
    """A layer in the predicate table with no classifier beside it raises.

    It is called directly, since `is_safety_relevant` cannot reach it.
    """

    def test_a_layer_with_no_classifier_raises(self) -> None:
        from swcraft_tools.editing.address import Address
        from swcraft_tools.editing.protocol import DeltaEntry

        entry = DeltaEntry(
            operation="update",
            address=Address(layer="connectors", element="C1", path=()),
            before="a",
            after="b",
        )

        empty = editing_safety_guard.ModelSnapshot(
            compositions=[],
            swcs=[],
            interfaces=[],
            implementation_types=[],
            connectors=[],
            requirements=[],
            tasks=[],
            sequences=[],
            hardware=[],
            mapping_sets=[],
            base_types=[],
            application_types=[],
            units=[],
            compu_methods=[],
            data_constraints=[],
        )

        with pytest.raises(SafetyClassificationError, match="no safety classifier for layer 'connectors'"):
            # Both models are the same empty snapshot: the dispatch raises before
            # either is read.
            editing_safety_guard._classify_outside_hardware(entry, empty, empty)
