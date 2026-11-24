"""The acceptance ledger for the generic MCP protocol surface.

Every acceptance clause is stated in one module. A clause that cannot pass yet would be
``xfail(strict=True)`` naming the task that satisfies it, so an early pass fails; none is pending
now, and ``TestTheLedgerItself`` asserts that a clause put back under ``xfail`` keeps that shape.

The clauses are read from independent oracles: the tool disposition table, through its name sets
in ``tool-projections.json`` (``decision_table.load_projections``), and ``protocol.schema.json``.
The protocol tool names are the one literal here, and they are checked against the schema: each
must be an ``operation`` name or const of the request it serves.

Three clauses are "stays true" checks: the retained tools less the withdrawn are still
registered, ``LIFECYCLE_OPERATIONS`` keeps its six members, and the shared vector scaffolding is
importable. A removal of many tools goes wrong quietly by taking a retained one with them.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import swcraft_mcp.server as server
from decision_table import ADDED_QUESTIONS, load_projections, withdrawn
from protocol_client import list_tools, tool_names
from swcraft_tools.editing.audit import LIFECYCLE_OPERATIONS

# Not imported from conftest.py, which would pull the autouse scaffold into this import graph.
REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol"

PROJECTIONS: dict[str, list[str]] = load_projections()
PROTOCOL: dict[str, Any] = json.loads((CONTRACT_FIXTURES / "protocol.schema.json").read_text(encoding="utf-8"))


# The inventory, read out of the contracts.

#: The two request shapes whose names are also protocol *operation* names.
GENERIC_READ_TOOLS: tuple[str, str] = ("discover", "read")

#: The search, whose name is `$defs/findRequest`'s `operation` const.
FIND_TOOL = "find"

#: The fourth, whose name is `$defs/changeSetRequest`'s `operation` const.
CHANGE_SET_TOOL = "change_set"

PROTOCOL_TOOLS: tuple[str, ...] = (*GENERIC_READ_TOOLS, FIND_TOOL, CHANGE_SET_TOOL)


#: The retained tools: cross-layer analysis, build orchestration and external-format exchange.
#: None of them is addressable model content, so none is replaced by a protocol operation.
RETAINED_TOOLS: frozenset[str] = frozenset(PROJECTIONS["retained"])

#: The retained tools taken off the live surface: each row keeps its `retain-specialized`
#: disposition and carries a `withdrawn` record, so the surface is the retained tools minus these.
WITHDRAWN_TOOLS: frozenset[str] = withdrawn(PROJECTIONS)

#: The retained tools still registered.
LIVE_RETAINED_TOOLS: frozenset[str] = RETAINED_TOOLS - WITHDRAWN_TOOLS

#: The single `info` tool, folded into the discovery passport.
REPLACED_BY_DISCOVER: frozenset[str] = frozenset(PROJECTIONS["replaced_by_discover"])

#: The nine layer dumps.
REPLACED_BY_READ: frozenset[str] = frozenset(PROJECTIONS["replaced_by_read"])

#: The nineteen mutations: eighteen element-bound wrappers, and `delete_element`, the one
#: generic mutation that already existed.
REPLACED_BY_CHANGE_SET: frozenset[str] = frozenset(PROJECTIONS["replaced_by_change_set"])
ELEMENT_WRITE_TOOLS: frozenset[str] = REPLACED_BY_CHANGE_SET - {"delete_element"}

#: All twenty-nine schema-coupled tools, deleted outright: no deprecated wrapper, no dual surface.
REPLACED_TOOLS: frozenset[str] = REPLACED_BY_DISCOVER | REPLACED_BY_READ | REPLACED_BY_CHANGE_SET

#: The final surface: the protocol tools, the live retained ones and the added questions.
EXPECTED_INVENTORY: frozenset[str] = frozenset(PROTOCOL_TOOLS) | LIVE_RETAINED_TOOLS | ADDED_QUESTIONS

#: The model layers, a negative vocabulary: a layer name inside a protocol tool body is layer
#: knowledge accumulating in `server.py` again. Written out rather than derived, because a
#: vocabulary derived from the registry could not tell a broken registry from a broken gate; the
#: equality test below makes a missing layer fail by name.
LAYER_VOCABULARY: frozenset[str] = frozenset(
    {
        "swc",
        "interfaces",
        "connectors",
        "requirements",
        "compositions",
        "tasks",
        "sequences",
        "hardware",
        "mapping_sets",
        "base_types",
        "application_types",
        "implementation_types",
        "units",
        "compu_methods",
        "data_constraints",
    }
)


def test_the_layer_vocabulary_names_every_declared_layer() -> None:
    """The hand-written vocabulary names every declared layer, so a new layer is never silently unscanned."""
    from swcraft_tools.model_registry import production_registry

    assert LAYER_VOCABULARY == {layer.name for layer in production_registry().layers}


# Each `replace-*` disposition is covered by exactly one ledger clause: otherwise a disposition
# could go unaccepted, or two clauses could each be weakened assuming the other bites.

#: Test qualified name -> the decision-table tools that clause accepts.
LEDGER_COVERAGE: dict[str, tuple[str, ...]] = {}


def covers(*tools: str):
    """Declare which decision-table entries a ledger clause accepts."""

    def decorate(function):
        LEDGER_COVERAGE[function.__qualname__] = tuple(tools)
        return function

    return decorate


def _callee_name(call: ast.Call) -> str:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ast.dump(target)


def _definition(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The AST of ``server.<name>``, or ``None`` when there is no such function.

    ``@mcp.tool()`` returns the function unchanged, so ``inspect.getsource`` reaches its real body.
    """
    function = getattr(server, name, None)
    if not callable(function):
        return None
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    definition = tree.body[0]
    if not isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef):
        return None
    return definition


def _returns_the_handler_call(value: ast.expr | None) -> bool:
    if isinstance(value, ast.Await):
        value = value.value
    return isinstance(value, ast.Call) and _callee_name(value) == "_handle"


def pass_through_violations(name: str) -> list[str]:
    """Why *name* is not a pass-through, or an empty list.

    The body calls the one shared handler and nothing else, returns that call's result rather than
    a value composed from it, and names no layer. That the body is passed on unmodified is
    asserted behaviourally elsewhere: an AST cannot see equality of values.
    """
    definition = _definition(name)
    if definition is None:
        return [f"{name} is not a module-level function of swcraft_mcp.server"]

    violations: list[str] = []
    called = {_callee_name(node) for node in ast.walk(definition) if isinstance(node, ast.Call)}
    called -= {"tool"}  # the `@mcp.tool()` decorator call itself
    if called != {"_handle"}:
        violations.append(f"{name} calls {sorted(called)}; a pass-through calls only the shared _handle helper")

    returns = [node for node in ast.walk(definition) if isinstance(node, ast.Return)]
    if len(returns) != 1 or not _returns_the_handler_call(returns[0].value):
        violations.append(f"{name} does not return the _handle result directly")

    for node in ast.walk(definition):
        if isinstance(node, ast.Constant) and node.value in LAYER_VOCABULARY:
            violations.append(f"{name} names the layer {node.value!r} in its body")

    return violations


class TestTheInventory:
    """Thirteen tools, derived from protocol capabilities and questions, not from layers."""

    def test_tools_list_returns_exactly_the_thirteen_names(self) -> None:
        registered = tool_names()
        assert len(EXPECTED_INVENTORY) == 13
        assert registered == EXPECTED_INVENTORY, (
            f"missing: {sorted(EXPECTED_INVENTORY - registered)}; unexpected: {sorted(registered - EXPECTED_INVENTORY)}"
        )

    def test_discover_is_registered(self) -> None:
        assert "discover" in tool_names()

    def test_read_is_registered(self) -> None:
        assert "read" in tool_names()

    def test_change_set_is_registered(self) -> None:
        assert CHANGE_SET_TOOL in tool_names()

    def test_the_retained_minus_the_withdrawn_are_registered(self) -> None:
        # A removal goes wrong quietly by taking a retained tool with it; the withdrawn ones stay
        # rows of the table, and one still registered fails here too.
        assert len(RETAINED_TOOLS) == 10
        assert len(WITHDRAWN_TOOLS) == 3
        assert WITHDRAWN_TOOLS <= RETAINED_TOOLS
        assert LIVE_RETAINED_TOOLS <= tool_names()
        assert not (WITHDRAWN_TOOLS & tool_names()), sorted(WITHDRAWN_TOOLS & tool_names())


class TestTheToolNamesAreTheOperationNames:
    """The surface cannot drift from the schema it exposes.

    Named tools rather than one opaque envelope, because the tool name and its argument shape are
    what the LLM plans against and what a host renders in its confirmation dialog.
    """

    def test_discover_and_read_are_protocol_operation_names(self) -> None:
        assert set(GENERIC_READ_TOOLS) <= set(PROTOCOL["$defs"]["operationName"]["enum"])
        assert set(GENERIC_READ_TOOLS) <= tool_names()

    def test_find_is_the_find_request_discriminator(self) -> None:
        const = PROTOCOL["$defs"]["findRequest"]["properties"]["operation"]["const"]
        assert const == FIND_TOOL
        assert FIND_TOOL in tool_names()

    def test_finds_arguments_are_the_find_requests_forms(self) -> None:
        """One optional argument per key of `$defs/findRequest`.

        The tool passes on only the ones named, so the engine's refusal of none, two, or
        `candidates` beside another form than `for_requirement` is the tool's too.
        """
        keys = set(PROTOCOL["$defs"]["findRequest"]["properties"]) - {"operation"}
        parameters = inspect.signature(server.find).parameters
        assert set(parameters) == keys == {"names", "text", "for_requirement", "candidates"}
        assert all(parameter.default is None for parameter in parameters.values())
        branches = [set(branch["required"]) for branch in PROTOCOL["$defs"]["findRequest"]["oneOf"]]
        assert branches == [{"names"}, {"text"}, {"for_requirement"}]

    def test_change_set_is_the_change_set_request_discriminator(self) -> None:
        const = PROTOCOL["$defs"]["changeSetRequest"]["properties"]["operation"]["const"]
        assert const == CHANGE_SET_TOOL
        assert CHANGE_SET_TOOL in tool_names()


class TestTheThreeBodiesArePassThroughs:
    """No layer knowledge can accumulate in the tool bodies again (once every field got a function)."""

    def test_discover_is_a_pass_through(self) -> None:
        assert pass_through_violations("discover") == []

    def test_read_is_a_pass_through(self) -> None:
        assert pass_through_violations("read") == []

    def test_change_set_is_a_pass_through(self) -> None:
        assert pass_through_violations(CHANGE_SET_TOOL) == []

    def test_the_shared_handler_delegates_to_the_engine(self) -> None:
        definition = _definition("_handle")
        assert definition is not None, "swcraft_mcp.server defines no _handle helper"
        names = {node.id for node in ast.walk(definition) if isinstance(node, ast.Name)}
        assert "handle_request" in names, "_handle does not call the engine's one entry point"
        assert getattr(server, "handle_request", None) is not None, (
            "server.py does not import handle_request from swcraft_tools.editing.engine"
        )


class TestTheSchemaCoupledSurfaceIsGone:
    """Removal, not deprecation: no wrapper and no dual surface.

    Each clause names the group of decision-table entries it accepts; the groups partition the
    twenty-nine exactly.
    """

    @covers("project_info")
    def test_project_info_is_replaced_by_discover(self) -> None:
        registered = tool_names()
        assert REPLACED_BY_DISCOVER == {"project_info"}
        assert not (REPLACED_BY_DISCOVER & registered)
        assert "discover" in registered

    @covers(
        "read_systems",
        "read_containers",
        "read_swc",
        "read_interfaces",
        "read_connectors",
        "read_requirements",
        "read_tasks",
        "read_sequences",
        "read_resources",
    )
    def test_the_nine_layer_read_tools_are_replaced_by_read(self) -> None:
        registered = tool_names()
        assert len(REPLACED_BY_READ) == 9
        assert not (REPLACED_BY_READ & registered)
        assert "read" in registered

    @covers(
        "add_swc",
        "add_port",
        "add_runnable",
        "add_connector",
        "add_interface",
        "add_cs_interface",
        "add_requirement",
        "add_trace_link",
        "add_task",
        "map_runnable_to_task",
        "add_sequence",
        "add_resource",
        "update_resource",
        "set_task_core",
        "set_task_priority",
        "set_task_wcet",
        "set_swc_memory_budget",
        "unset_swc_memory_budget",
    )
    def test_the_eighteen_element_write_tools_are_replaced_by_change_set(self) -> None:
        registered = tool_names()
        assert len(ELEMENT_WRITE_TOOLS) == 18
        assert not (ELEMENT_WRITE_TOOLS & registered)
        assert CHANGE_SET_TOOL in registered

    @covers("delete_element")
    def test_delete_element_and_its_confirmation_token_are_gone(self) -> None:
        # A `remove` is reviewed as a delta like every other operation, which shows the cascade
        # the two-step token never did.
        registered = tool_names()
        assert "delete_element" not in registered
        assert CHANGE_SET_TOOL in registered
        assert not hasattr(server, "_PendingDelete")
        assert not hasattr(server, "_PENDING_DELETES")

    def test_every_disposition_of_the_decision_table_is_applied(self) -> None:
        # Stated against the table itself, so a row added to it cannot escape the ledger.
        registered = tool_names()
        assert len(REPLACED_TOOLS) == 29
        assert not (REPLACED_TOOLS & registered), sorted(REPLACED_TOOLS & registered)
        assert LIVE_RETAINED_TOOLS <= registered
        assert not (WITHDRAWN_TOOLS & registered), sorted(WITHDRAWN_TOOLS & registered)


class TestTheApprovedScopeChannel:
    """The host hands the scope in through the environment of a process the LLM does not spawn."""

    def test_neither_discover_nor_read_consults_the_scope(self) -> None:
        # The read operations are unscoped and leave no audit record. The channel is asserted to
        # exist first, so the clause cannot pass for want of a scope to consult.
        assert "SWCRAFT_APPROVED_SCOPE" in inspect.getsource(server)
        for name in (*GENERIC_READ_TOOLS, FIND_TOOL):
            assert pass_through_violations(name) == []
            source = inspect.getsource(getattr(server, name))
            assert "SWCRAFT_APPROVED_SCOPE" not in source


class TestTheReviewBoundaryIsHostSelected:
    """No protocol operation can approve anything."""

    def test_no_tool_argument_can_select_or_weaken_the_review_mode(self) -> None:
        """No protocol tool, as `tools/list` publishes it, takes an argument that names a review or an approval.

        What this does not reach: that the server reads its review mode from its start environment
        (the review-mode tests drive that); an argument under another name a tool would treat as an
        answer.
        """
        published = {tool.name: tool for tool in list_tools()}
        for name in PROTOCOL_TOOLS:
            assert name in published, f"{name} is not registered"
            arguments = set(published[name].inputSchema.get("properties", {}))
            assert not [argument for argument in arguments if "review" in argument or "approv" in argument], name


class TestTheAuditVocabularyStaysClosed:
    def test_lifecycle_operations_still_has_its_six_members(self) -> None:
        # No host-review record: recording an environment mode would not prove that a human answered.
        assert LIFECYCLE_OPERATIONS == (
            "change_set_staged",
            "change_set_rejected",
            "change_set_approved",
            "change_set_commit_intent",
            "change_set_committed",
            "change_set_abandoned",
        )
        assert len(LIFECYCLE_OPERATIONS) == 6


class TestTheSharedVectorScaffolding:
    def test_the_conftest_shim_makes_the_vector_fixtures_importable(self) -> None:
        # Not a skip: the vectors are an independent oracle, and a green run that never replayed
        # them would report the opposite of what it checked.
        import editing_vector_fixtures as fixtures

        # Written by hand when a vector is added or removed.
        assert len(fixtures.VECTORS) == 169
        assert callable(fixtures.build_fixture)
        assert callable(fixtures.dispatch)


class TestTheLedgerItself:
    """The ledger's two self-checks. Neither may be ``xfail``."""

    def test_every_replaced_tool_is_covered_by_exactly_one_clause(self) -> None:
        seen: dict[str, str] = {}
        for clause, tools in LEDGER_COVERAGE.items():
            for tool in tools:
                assert tool not in seen, f"{tool} is claimed by {seen[tool]} and by {clause}"
                seen[tool] = clause
        assert set(seen) == REPLACED_TOOLS, (
            f"uncovered dispositions: {sorted(REPLACED_TOOLS - set(seen))}; "
            f"covered but not in the table: {sorted(set(seen) - REPLACED_TOOLS)}"
        )

    def test_every_pending_clause_names_the_task_that_satisfies_it(self) -> None:
        # A non-strict xfail would let an early pass go unnoticed, and a reason without a task
        # number would leave nobody to flip it.
        pending = []
        for suite in [value for name, value in globals().items() if name.startswith("Test")]:
            for name, function in vars(suite).items():
                if not name.startswith("test_"):
                    continue
                for mark in getattr(function, "pytestmark", []):
                    if mark.name != "xfail":
                        continue
                    pending.append(f"{suite.__name__}.{name}")
                    assert mark.kwargs.get("strict") is True, f"{suite.__name__}.{name}"
                    reason = mark.kwargs.get("reason", "")
                    assert re.fullmatch(r"Task \d+", reason), (
                        f"{suite.__name__}.{name} has reason {reason!r}, not 'Task <n>'"
                    )
        # The ledger is satisfied: a clause put back under `xfail` means the surface regressed.
        assert pending == [], f"the ledger is satisfied; these clauses are pending again: {pending}"
