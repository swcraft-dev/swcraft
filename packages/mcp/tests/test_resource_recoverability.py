"""The recoverability invariant, over the editing protocol.

*No single call creates a state no call can undo.* One test per ``error``-capable
finding code ``analyze_resource_budgets`` can emit, as protocol exchanges. The
claim splits in two:

1. **The state cannot be created at all.** A change set that would leave a
   resource ``error`` is refused with ``consistency-violation`` and the model
   stays byte-identical.
2. **A state already there is left by ONE change set.** Each finding is seeded
   as YAML on disk and cleared by a single all-or-nothing change set, after which
   the *whole* model validates clean (``_assert_clean``), so a recovery that
   trades a resource error for another finding is not counted.

The structural states (a duplicate name, an undeclared memory or partition) are
``hardware.*`` contract rules, keyed on the rule ``check-model`` reports. A
duplicated identity has no protocol recovery: an address is ``{layer, element}``,
so the engine refuses a change set over an ambiguous name before staging, and a
payload ``name`` disagreeing with its address is a ``schema-violation``; both
halves are pinned.

The ``_COVERED_CODES`` pin fails when a ``FINDING_*`` constant is added or
removed without updating the set; it cannot verify that the new entry got a
real test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
import yaml
from protocol_client import ScriptedHuman, call, call_reviewed
from swcraft_tools import resource_analysis
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    FINDING_RATE_MONOTONIC_VIOLATION,
    FINDING_THRESHOLD_BREACH,
    MEMORY_BUDGET_EXAMPLE,
    SEVERITY_ERROR,
    WCET_BUDGET_EXAMPLE,
    analyze_resource_budgets,
)
from swcraft_tools.validator import check_cross_layer_consistency

#: Every finding code with a recovery test in this file; the structural states are
#: keyed on `hardware.*` rules instead.
_COVERED_CODES = {
    FINDING_MISSING_BUDGET,
    FINDING_THRESHOLD_BREACH,
    FINDING_RATE_MONOTONIC_VIOLATION,
}

#: The `hardware.*` rules asserted by the exchanges in § *The states the contract rules own*.
_CONTRACT_RULES = (
    "hardware.duplicate-name",
    "hardware.undeclared-memory",
    "hardware.undeclared-partition",
)


# ---------------------------------------------------------------------------
# The exchange helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _elicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every exchange here has a human behind it who answers (``stage-only`` would never move the model)."""
    monkeypatch.setenv("SWCRAFT_REVIEW", server.REVIEW_ELICIT)
    server._configure_review_mode()


def _baseline() -> str:
    """The token ``discover`` answers with — the only way to state a change set."""
    structured, _texts = call("discover")
    assert structured.get("response") == "discover", structured
    return structured["baseline"]


def _submit(operations: list[dict[str, Any]], intent: str) -> dict[str, Any]:
    """One ``change_set`` exchange with a human who approves what is asked.

    Two scripted answers: a recovery that raises a threshold or drops a pool also
    asks the weakening question.
    """
    human = ScriptedHuman(True, True)
    structured, _texts = call_reviewed(human, "change_set", baseline=_baseline(), operations=operations, intent=intent)
    return structured


def _recover(model_root: Path, operations: list[dict[str, Any]], intent: str) -> None:
    """Submit *operations*, require the commit, and require the model clean after it."""
    response = _submit(operations, intent)
    assert response.get("status") == "committed", response
    _assert_clean(model_root)


def _model_state(model_root: Path) -> str:
    """The model tree's digest (``editing_vector_fixtures``), for the byte-identity assertions."""
    return fixtures.model_state_key(model_root)


def _error_codes(model_root: Path) -> list[str]:
    analysis = analyze_resource_budgets(str(model_root))
    return [f.code for f in analysis.error_findings()]


def _assert_clean(model_root: Path) -> None:
    """The whole model, as ``validate``/``build`` check it, not only its resource findings."""
    result = check_cross_layer_consistency(str(model_root))
    assert result.consistent, result.issues


# ---------------------------------------------------------------------------
# The seeds — the states a change set cannot produce but a model can hold
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _seed_hardware(model_root: Path, filename: str, data: dict[str, Any]) -> None:
    _write(model_root / "hardware" / f"{filename}.yaml", data)


def _seed_topology(model_root: Path) -> None:
    """The smallest mandatory-layer content a fixture needs before it declares what it is about.

    One processor (`hardware.no-processor`), one `CORE0` (every memory names a core
    it is accessible from), `PARTITION0` on it for every seeded task, and a
    member-less root composition (`composition.no-root`). A case that declares
    `CORE0` itself overwrites this one, which is how the CPU cases set their own
    threshold and enforcement.
    """
    _seed_hardware(model_root, "CPU", {"name": "CPU", "kind": "processor"})
    _seed_hardware(model_root, "CORE0", _core("CORE0"))
    _seed_hardware(model_root, "PARTITION0", _partition("PARTITION0", "CORE0"))
    _write(
        model_root / "compositions" / "Root.yaml",
        {"name": "Root", "title": "Root composition", "root": True},
    )


def _requirement(model_root: Path, rid: str) -> None:
    """A requirement for a component a change set creates to satisfy (the seed's twin, without the SWC)."""
    _write(
        model_root / "requirements" / f"{rid}.yaml",
        {
            "id": rid,
            "title": rid,
            "description": f"{rid} exists.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )


def _core(name: str, threshold: float = 0.7, **extra: Any) -> dict[str, Any]:
    return {"name": name, "kind": "core", "threshold": threshold, **extra}


def _partition(name: str, core: str) -> dict[str, Any]:
    """An unrated partition on *core*."""
    return {"name": name, "kind": "partition", "core": core}


def _memory(name: str, size: int, threshold: float = 0.8, accessible: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "memory",
        "type": "RAM",
        "size": size,
        "accessible": list(accessible) if accessible is not None else ["CORE0"],
        "threshold": threshold,
    }


def _rule_ids(model_root: Path) -> list[str]:
    """Every rule `check-model` reports for the model on disk."""
    return [record.rule for record in check_cross_layer_consistency(str(model_root)).records]


def _seed_task(model_root: Path, name: str, **fields: Any) -> None:
    _write(model_root / "tasks" / f"{name}.yaml", {"name": name, **fields})


def _seed_traced_swc(model_root: Path, name: str, **fields: Any) -> None:
    """An SWC plus a requirement it satisfies through its own ``satisfies``.

    The link and ``handle_termination_and_restart: no_support`` keep the fixture
    free of findings unrelated to resources; a field of *fields* overrides it.
    """
    _write(
        model_root / "swc" / "body" / f"{name}.yaml",
        {
            "name": name,
            "type": "application",
            "handle_termination_and_restart": "no_support",
            "satisfies": [f"REQ_{name}@1"],
            **fields,
        },
    )
    _write(
        model_root / "requirements" / f"REQ_{name}.yaml",
        {
            "id": f"REQ_{name}",
            "title": name,
            "description": f"{name} exists.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )


# ---------------------------------------------------------------------------
# The enumeration itself
# ---------------------------------------------------------------------------


def test_every_error_capable_finding_code_has_a_recovery_test() -> None:
    """A new ``FINDING_*`` constant must arrive with its recovery sequence (the enumeration, not the behaviour)."""
    declared = {
        value
        for name, value in vars(resource_analysis).items()
        if name.startswith("FINDING_") and isinstance(value, str)
    }
    assert declared == _COVERED_CODES


# ---------------------------------------------------------------------------
# The states the contract rules own
# ---------------------------------------------------------------------------
#
# Keyed on the `hardware.*` rule `check-model` reports. Both halves hold: the state cannot be created through
# the protocol, and a state already on disk is left by ONE change set.


def test_a_create_cannot_give_a_second_memory_an_existing_memorys_name(tmp_path: Path) -> None:
    """The address and the payload both name the element; a disagreement is a ``schema-violation`` before staging."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 1024))
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "hardware", "element": "RAM_b"},
                "value": _memory("RAM", 2048),
            }
        ],
        "Declare a second RAM memory.",
    )

    assert response["response"] == "error"
    assert response["error"] == "schema-violation"
    assert _model_state(model_root) == before


def test_a_duplicated_identity_is_refused_at_the_address_and_named_as_such(tmp_path: Path) -> None:
    """The one state with no protocol recovery: refused before staging, naming the doubled identity."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM_a", _memory("RAM", 1024))
    _seed_hardware(model_root, "RAM_b", _memory("RAM", 2048))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512})
    before = _model_state(model_root)

    assert "hardware.duplicate-name" in _rule_ids(model_root)

    response = _submit(
        [{"operation": "remove", "address": {"layer": "hardware", "element": "RAM"}}],
        "Drop one of the two RAM memories.",
    )

    assert response["response"] == "error"
    assert response["error"] == "consistency-violation"
    assert response["addresses"] == [{"layer": "hardware", "element": "RAM"}]
    assert _model_state(model_root) == before


def test_a_duplicate_core_id_is_not_a_state_the_model_can_hold(tmp_path: Path) -> None:
    """A core is identified by its ``name``, so a duplicate core id is the duplicate name above.

    ``hw_id`` is the only field that could still repeat, and nothing validates it, on purpose.
    """
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", hw_id=0))
    _seed_hardware(model_root, "CORE1", _core("CORE1", hw_id=0))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")

    _assert_clean(model_root)


def test_a_change_set_cannot_budget_an_undeclared_memory(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 4096))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512})
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "FLASH"}]},
                "value": 256,
            }
        ],
        "Budget the FLASH memory.",
    )

    assert response["response"] == "error"
    # A budget against an undeclared memory is a reference to nothing: `broken-reference`.
    assert response["error"] == "broken-reference"
    assert _model_state(model_root) == before


def test_a_change_set_cannot_remove_a_memory_its_referrers_still_budget(tmp_path: Path) -> None:
    """The cascade the removal has to carry: the staged result fails, so no hand-written precondition is needed."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 4096))
    _seed_hardware(model_root, "ROM", _memory("ROM", 4096))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512, "ROM": 512})
    before = _model_state(model_root)

    response = _submit(
        [{"operation": "remove", "address": {"layer": "hardware", "element": "RAM"}}],
        "Retire the RAM memory.",
    )

    assert response["response"] == "error"
    assert _model_state(model_root) == before


def test_a_memory_and_its_referrers_are_removed_together_in_one_change_set(tmp_path: Path) -> None:
    """A memory and its referrers' budgets go in one all-or-nothing change set, reviewed together."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 4096))
    _seed_hardware(model_root, "ROM", _memory("ROM", 4096))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512, "ROM": 512})

    _recover(
        model_root,
        [
            {
                "operation": "remove",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]},
            },
            {"operation": "remove", "address": {"layer": "hardware", "element": "RAM"}},
        ],
        "Retire the RAM memory and the budget that names it.",
    )

    assert not (model_root / "hardware" / "RAM.yaml").exists()


def test_an_undeclared_memory_is_cleared_by_dropping_the_budget(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "ROM", _memory("ROM", 4096))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512, "ROM": 512})

    assert "hardware.undeclared-memory" in _rule_ids(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "remove",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]},
            }
        ],
        "Drop the budget for a memory that no longer exists.",
    )


def test_an_undeclared_memory_is_also_cleared_by_redeclaring_it(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "ROM", _memory("ROM", 4096))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 512, "ROM": 512})

    assert "hardware.undeclared-memory" in _rule_ids(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "create",
                "address": {"layer": "hardware", "element": "RAM"},
                "value": _memory("RAM", 4096),
            }
        ],
        "Re-declare the RAM memory the budgets name.",
    )


def test_a_change_set_cannot_put_a_task_in_an_undeclared_partition(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0"))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "tasks", "element": "T7"},
                "value": {"name": "T7", "period": 0.02, "priority": 4, "partition": "PARTITION7", "wcet": "100us"},
            }
        ],
        "Add a task in PARTITION7.",
    )

    assert response["response"] == "error"
    # `Task.partition` names a hardware element that does not exist.
    assert response["error"] == "broken-reference"
    assert _model_state(model_root) == before


def test_an_undeclared_partition_is_cleared_by_moving_the_task(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0"))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
    _seed_task(model_root, "T7", period=0.02, priority=4, partition="PARTITION7", wcet="100us")

    assert "hardware.undeclared-partition" in _rule_ids(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "T7", "path": [{"field": "partition"}]},
                "value": "PARTITION0",
            }
        ],
        "Move T7 into a declared partition.",
    )


def test_an_undeclared_partition_is_also_cleared_by_declaring_the_partition(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0"))
    _seed_task(model_root, "T7", period=0.02, priority=4, partition="PARTITION7", wcet="100us")

    assert "hardware.undeclared-partition" in _rule_ids(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "create",
                "address": {"layer": "hardware", "element": "PARTITION7"},
                "value": _partition("PARTITION7", "CORE0"),
            }
        ],
        "Declare PARTITION7 on CORE0.",
    )


# ---------------------------------------------------------------------------
# 5. missing_budget
# ---------------------------------------------------------------------------


def test_a_change_set_cannot_add_a_task_with_no_cpu_budget(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "tasks", "element": "T"},
                "value": {"name": "T", "period": 0.01, "priority": 5, "partition": "PARTITION0"},
            }
        ],
        "Add a task.",
    )

    assert response["response"] == "error"
    assert response["error"] == "consistency-violation"
    assert response["addresses"][0]["layer"] == "tasks"
    assert _model_state(model_root) == before


def test_a_task_and_its_budget_are_created_in_one_change_set(tmp_path: Path) -> None:
    """The one-call route in: the budget travels with the element that needs it."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))

    _recover(
        model_root,
        [
            {
                "operation": "create",
                "address": {"layer": "tasks", "element": "T"},
                "value": {"name": "T", "period": 0.01, "priority": 5, "partition": "PARTITION0", "wcet": "100us"},
            }
        ],
        "Add a task with its CPU budget.",
    )


def test_missing_cpu_budget_is_cleared_by_setting_the_wcet(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0")

    assert FINDING_MISSING_BUDGET in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "T", "path": [{"field": "wcet"}]},
                "value": "100us",
            }
        ],
        "Budget T's worst-case execution time.",
    )


def test_missing_memory_budget_is_cleared_by_creating_the_map_entry(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 4096, 0.8))
    _seed_traced_swc(model_root, "Alpha")

    assert FINDING_MISSING_BUDGET in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]},
                "value": 512,
            }
        ],
        "Budget Alpha's RAM.",
    )


def test_a_missing_memory_budget_refusal_states_the_create_that_clears_it(tmp_path: Path) -> None:
    """The refusal names the ``create`` and an example value, and adding it commits clean."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 4096, 0.8))
    _requirement(model_root, "REQ_Alpha")
    before = _model_state(model_root)
    create = {
        "operation": "create",
        "address": {"layer": "swc", "element": "Alpha"},
        "value": {
            "name": "Alpha",
            "group": "body",
            "type": "application",
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ_Alpha@1"],
        },
    }

    refused = _submit([create], "Add a component.")

    assert refused["error"] == "consistency-violation"
    assert f"RAM: {MEMORY_BUDGET_EXAMPLE}" in re.findall(r"`([^`]+)`", refused["message"])
    address = {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]}
    assert refused["addresses"] == [address]
    assert _model_state(model_root) == before

    _recover(
        model_root,
        [create, {"operation": "create", "address": address, "value": MEMORY_BUDGET_EXAMPLE}],
        "Add a component with its RAM budget.",
    )


def test_a_missing_wcet_refusal_states_the_update_that_clears_it(tmp_path: Path) -> None:
    """The CPU twin: the refusal carries the ``wcet`` example, and an ``update`` of the field with it commits.

    An ``update``, not a ``create``: a field cannot be created, which is why the sentence says *set*.
    """
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    before = _model_state(model_root)
    create = {
        "operation": "create",
        "address": {"layer": "tasks", "element": "T"},
        "value": {"name": "T", "period": 0.01, "priority": 5, "partition": "PARTITION0"},
    }

    refused = _submit([create], "Add a task.")

    assert refused["error"] == "consistency-violation"
    assert f"wcet: {WCET_BUDGET_EXAMPLE}" in re.findall(r"`([^`]+)`", refused["message"])
    address = {"layer": "tasks", "element": "T", "path": [{"field": "wcet"}]}
    assert refused["addresses"] == [address]
    assert _model_state(model_root) == before

    _recover(
        model_root,
        [create, {"operation": "update", "address": address, "value": WCET_BUDGET_EXAMPLE}],
        "Add a task with its CPU budget.",
    )


# ---------------------------------------------------------------------------
# 6. threshold_breach
# ---------------------------------------------------------------------------


def test_a_change_set_cannot_commit_a_cpu_threshold_breach(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "tasks", "element": "T"},
                "value": {"name": "T", "period": 0.01, "priority": 5, "partition": "PARTITION0", "wcet": "9000us"},
            }
        ],
        "Add a task that eats the core.",
    )

    assert response["response"] == "error"
    assert response["error"] == "consistency-violation"
    assert _model_state(model_root) == before


def test_cpu_threshold_breach_is_cleared_by_lowering_the_wcet(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="9000us")

    assert FINDING_THRESHOLD_BREACH in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "T", "path": [{"field": "wcet"}]},
                "value": "1000us",
            }
        ],
        "Bring T back inside the core budget.",
    )


def test_cpu_threshold_breach_is_also_cleared_by_raising_the_threshold(tmp_path: Path) -> None:
    """The weakening direction: raising a threshold relaxes the check, so it is asked as the second question."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="9000us")

    assert FINDING_THRESHOLD_BREACH in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "hardware", "element": "CORE0", "path": [{"field": "threshold"}]},
                "value": 0.95,
            }
        ],
        "Raise the core-0 threshold.",
    )


def test_memory_threshold_breach_is_cleared_by_lowering_the_budget(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 1024, 0.8))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 1000})

    assert FINDING_THRESHOLD_BREACH in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]},
                "value": 512,
            }
        ],
        "Cut Alpha's RAM budget.",
    )


def test_memory_threshold_breach_is_also_cleared_by_growing_the_pool(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 1024, 0.8))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 1000})

    assert FINDING_THRESHOLD_BREACH in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "hardware", "element": "RAM", "path": [{"field": "size"}]},
                "value": 8192,
            }
        ],
        "Grow the RAM pool.",
    )


def test_dropping_the_budget_is_not_a_recovery_for_a_strict_threshold_breach(tmp_path: Path) -> None:
    """Dropping the budget trades the breach for ``missing_budget`` under ``strict``, so it is refused outright."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "RAM", _memory("RAM", 1024, 0.8))
    _seed_traced_swc(model_root, "Alpha", memory={"RAM": 1000})
    before = _model_state(model_root)

    assert FINDING_THRESHOLD_BREACH in _error_codes(model_root)

    response = _submit(
        [
            {
                "operation": "remove",
                "address": {"layer": "swc", "element": "Alpha", "path": [{"field": "memory"}, {"key": "RAM"}]},
            }
        ],
        "Drop Alpha's RAM budget.",
    )

    assert response["response"] == "error"
    assert _error_codes(model_root) == [FINDING_THRESHOLD_BREACH]
    assert _model_state(model_root) == before


# ---------------------------------------------------------------------------
# 7. rate_monotonic_violation
# ---------------------------------------------------------------------------


def test_a_change_set_cannot_invert_the_priorities_on_a_core(tmp_path: Path) -> None:
    """An inverted priority never lands: the staged result is validated before anything is applied."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")
    before = _model_state(model_root)

    response = _submit(
        [
            {
                "operation": "create",
                "address": {"layer": "tasks", "element": "Task_10ms"},
                "value": {
                    "name": "Task_10ms",
                    "period": 0.01,
                    "priority": 5,
                    "partition": "PARTITION0",
                    "wcet": "1000us",
                },
            }
        ],
        "Add a 10 ms task.",
    )

    assert response["response"] == "error"
    assert response["error"] == "consistency-violation"
    assert _model_state(model_root) == before


def test_rate_monotonic_violation_is_cleared_by_one_change_set(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")
    _seed_task(model_root, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="1000us")

    findings = list(analyze_resource_budgets(str(model_root)).error_findings())
    assert [f.code for f in findings] == [FINDING_RATE_MONOTONIC_VIOLATION]
    assert findings[0].severity == SEVERITY_ERROR

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                "value": 30,
            }
        ],
        "Give the shorter period the higher priority.",
    )

    # Without touching enforcement: relaxing the check is not what fixed it.
    assert analyze_resource_budgets(str(model_root)).resources[0].resource.enforcement == "strict"


def test_a_systematically_inverted_core_is_recovered_in_one_unit(tmp_path: Path) -> None:
    """Four tasks, four updates, one all-or-nothing change set: no intermediate state is left red."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.9))
    inverted = (("T1ms", 0.001, 1), ("T5ms", 0.005, 2), ("T10ms", 0.01, 3), ("T50ms", 0.05, 4))
    for name, period, priority in inverted:
        _seed_task(model_root, name, period=period, priority=priority, partition="PARTITION0", wcet="100us")

    assert FINDING_RATE_MONOTONIC_VIOLATION in _error_codes(model_root)

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": name, "path": [{"field": "priority"}]},
                "value": priority,
            }
            for name, priority in (("T1ms", 40), ("T5ms", 30), ("T10ms", 20), ("T50ms", 10))
        ],
        "Restore rate-monotonic priorities across core 0.",
    )


# ---------------------------------------------------------------------------
# Core removal
# ---------------------------------------------------------------------------
#
# The staged result is validated, so the model's own rule decides: removing a core something still runs on
# leaves an unresolved reference, whatever the surviving cores' `enforcement` says and whether any survive.


def test_removing_the_last_core_is_refused_while_a_task_runs_on_it(tmp_path: Path) -> None:
    """Removing the last core is refused: the partition a task runs in still names it."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
    before = _model_state(model_root)

    response = _submit(
        [{"operation": "remove", "address": {"layer": "hardware", "element": "CORE0"}}],
        "Stop budgeting CPU on this project.",
    )

    assert response["response"] == "error"
    assert response["error"] == "broken-reference"
    assert _model_state(model_root) == before


def test_removing_an_advisory_core_is_refused_just_the_same(tmp_path: Path) -> None:
    """`enforcement` decides severities, not references: an all-`advisory` core removal is refused too."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7, enforcement="advisory"))
    _seed_hardware(model_root, "CORE1", _core("CORE1", 0.7, enforcement="advisory"))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
    before = _model_state(model_root)

    response = _submit(
        [{"operation": "remove", "address": {"layer": "hardware", "element": "CORE0"}}],
        "Retire the advisory CORE0.",
    )

    assert response["response"] == "error"
    assert response["error"] == "broken-reference"
    assert _model_state(model_root) == before


def test_removing_a_core_nothing_runs_on_commits(tmp_path: Path) -> None:
    """The removal is refused by the referrers, never by the removal itself."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_hardware(model_root, "CORE1", _core("CORE1", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")

    _recover(
        model_root,
        [{"operation": "remove", "address": {"layer": "hardware", "element": "CORE1"}}],
        "Retire the core nothing runs on.",
    )

    assert not (model_root / "hardware" / "CORE1.yaml").exists()


def test_the_partition_moves_and_the_core_goes_in_the_same_change_set(tmp_path: Path) -> None:
    """The remedy as one unit: move the partition (its tasks go with it), then remove the core."""
    model_root = tmp_path / "model"
    _seed_topology(model_root)
    _seed_hardware(model_root, "CORE0", _core("CORE0", 0.7))
    _seed_hardware(model_root, "CORE1", _core("CORE1", 0.7))
    _seed_task(model_root, "T", period=0.01, priority=5, partition="PARTITION0", wcet="100us")

    _recover(
        model_root,
        [
            {
                "operation": "update",
                "address": {"layer": "hardware", "element": "PARTITION0", "path": [{"field": "core"}]},
                "value": "CORE1",
            },
            {"operation": "remove", "address": {"layer": "hardware", "element": "CORE0"}},
        ],
        "Move PARTITION0, and T with it, onto CORE1 and retire CORE0.",
    )

    assert not (model_root / "hardware" / "CORE0.yaml").exists()
