"""The greenfield protocol acceptance gate: a scaffold authored, refused and built through the protocol alone.

1. From a clean scaffold, one protocol client discovers the contract and performs reads, creates, nested updates and
   an approved removal across the layers -- :class:`TestTheGreenfieldFlow`.
2. Without direct YAML writes, product-source access or a layer-specific MCP function --
   :class:`TestNothingButTheProtocolAuthoredThis`, which reads the scenario's source and the live tool inventory.
3. Invalid, unauthorized, stale and partially applicable change sets leave the model byte-identical --
   :class:`TestTheRefusedChangeSets`, one case per code of the closed refusal enum.
4. The reviewed diff, the audit evidence, validation, the build and deterministic outputs agree --
   :class:`TestTheFourObservationsAgree` and :class:`TestTheOutputsAreDeterministic`.
5. A supported schema-field extension needs no MCP registration --
   :class:`TestASupportedFieldExtensionNeedsNoRegistration`.

The scenario lives in `tests/support/greenfield_scenario.py`, shared with `scripts/tests/test_clean_install.py`;
this module holds the assertions.

The initial change set is atomic because pass D validates the assembled staged result, so there is no valid
intermediate state: an interface no data type backs is a `broken-reference`. The removal likewise carries the port
and the value type that referenced or served `IBrakeLoad`.

It is `slow`: it scaffolds projects, commits seven change sets, validates and builds -- twice, for determinism.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import greenfield_scenario as scenario
import pytest
from greenfield_scenario import (
    ALLOWED_TOOLS,
    ARTIFACTS,
    ELEMENTS_ENUM,
    ELEMENTS_LINEAR,
    ELEMENTS_TABLE,
    ELEMENTS_TYPE,
    ELEMENTS_UNIT,
    ELEMENTS_VALUE_TYPE,
    INVALID_CHANGE_SETS,
    NON_INVALID_REFUSALS,
    PARTIAL_OPERATIONS,
    PARTIAL_OPERATIONS_REORDERED,
    PROJECT_NAME,
    REMOVED_IDENTITIES,
    SAME_NAME_PAIR,
    STALE_OPERATIONS,
    STATED_BEHAVIOUR,
    STATED_CALL,
    STATED_STUB,
    STATED_STUB_OPERATION,
    STATED_STUB_REF,
    SURVIVORS,
    UNAUTHORIZED_ADDRESS,
    UNAUTHORIZED_OPERATIONS,
    UNAUTHORIZED_SCOPE,
    VALUE_FORM_INTERFACE,
    ScenarioRun,
    create,
    refused,
    run_scenario,
    scaffold,
    serve,
)
from protocol_client import ScriptedHuman, call, call_reviewed, list_tools, tool_names
from pydantic.fields import FieldInfo
from swcraft_tools import schema
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.audit import CHANGE_SET_ID_PATTERN
from swcraft_tools.model_registry import FieldFacts, production_registry

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# One run of the whole scenario, and what it observed
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def greenfield(tmp_path_factory: pytest.TempPathFactory) -> ScenarioRun:
    """One run of the scenario, shared by every clause that observes it.

    Module-scoped: the run is the expensive part, and every assertion is about the same run.
    """
    return run_scenario(tmp_path_factory.mktemp("greenfield-run") / "project")


# ---------------------------------------------------------------------------
# Clause 1: the flow itself
# ---------------------------------------------------------------------------


class TestTheGreenfieldFlow:
    """*Discover the contract; read, create, update nested, remove — approved*."""

    def test_discover_answers_the_empty_scaffold_with_the_whole_contract(self, greenfield: ScenarioRun) -> None:
        from swcraft_tools._version import __version__

        discovered = greenfield.responses["discover"]
        assert discovered["project"] == {
            "name": PROJECT_NAME,
            "arxml_version": "4.2.2",
            "swcraft_version": __version__,
        }
        assert [layer["layer"] for layer in discovered["layers"]] == [
            "swc",
            "interfaces",
            "connectors",
            "application_types",
            "implementation_types",
            "base_types",
            "requirements",
            "compositions",
            "tasks",
            "sequences",
            "hardware",
            "mapping_sets",
            "units",
            "compu_methods",
            "data_constraints",
        ]
        # No layer is optional, so `init` scaffolds all of them; a layer that stopped being scaffolded shows up here.
        assert {layer["layer"] for layer in discovered["layers"] if not layer["present"]} == set()

    def test_discover_publishes_no_storage(self, greenfield: ScenarioRun) -> None:
        """`discover` names no storage location, at the surface a host drives."""
        rendered = json.dumps(greenfield.responses["discover"])
        for leaked in ("model_root", "dist_root", "views_root", "project_root", "model_hash"):
            assert leaked not in rendered
        assert str(greenfield.project) not in rendered

    def test_one_atomic_change_set_creates_nine_layers(self, greenfield: ScenarioRun) -> None:
        seeded = greenfield.responses["seed"]
        assert seeded["status"] == "committed"
        touched = {entry["address"]["layer"] for entry in seeded["delta"]}
        assert touched == {
            "base_types",
            "implementation_types",
            "interfaces",
            "swc",
            # The scaffolded root composition, edited at a member address.
            "compositions",
            "connectors",
            "requirements",
            "tasks",
            "sequences",
        }
        assert seeded["baseline"] != greenfield.first_baseline, "a commit answers with the NEW baseline token"

    def test_the_same_name_pair_commits_as_two_identities_in_two_layers(self, greenfield: ScenarioRun) -> None:
        """One name, two creates, no `identity-conflict`: identity is `(layer, name)`.

        Each layer answers its own element under the shared name, and a data element states its type's side in one
        field of two. What it does not reach: a refusal of the pair's shapes (the shipped negatives
        `schema-violation-data-element-both-type-fields`, `-no-type-field` and
        `reference-role-data-element-type-in-another-layer`).
        """
        paired = greenfield.responses["pair"]
        assert paired["status"] == "committed", paired
        assert {(entry["address"]["layer"], entry["address"]["element"]) for entry in paired["delta"]} == {
            ("application_types", SAME_NAME_PAIR),
            ("implementation_types", SAME_NAME_PAIR),
            ("mapping_sets", "SyntheticTorqueMapping"),
            ("interfaces", "ISyntheticTorque"),
        }
        assert all(entry["operation"] == "create" for entry in paired["delta"])

        application = greenfield.reads["pair_application_types"]["value"]
        implementation = greenfield.reads["pair_implementation_types"]["value"]
        assert (application["name"], application["kind"]) == (SAME_NAME_PAIR, "application_type")
        assert (implementation["name"], implementation["kind"]) == (SAME_NAME_PAIR, "value_type")

        (interface,) = [entry for entry in paired["delta"] if entry["address"]["layer"] == "interfaces"]
        sides = [
            (element["name"], element["application_type"], element["implementation_type"])
            for element in interface["after"]["data_elements"]
        ]
        assert sides == [("physical", SAME_NAME_PAIR, None), ("stored", None, SAME_NAME_PAIR)]

        # One file per layer under the shared name, each in its layer's directory.
        for directory in ("data_types/application", "data_types/implementation"):
            assert f"{directory}/{SAME_NAME_PAIR}.yaml" in greenfield.model, directory

    def test_the_data_description_elements_commit_and_read_back(self, greenfield: ScenarioRun) -> None:
        """The data description elements commit, each a file of its own layer, and the types answer their references.

        What it does not reach: a refusal (the shipped negatives
        `schema-violation-application-type-unit-and-unit-ref`, `schema-violation-enum-literals-and-compu-method-ref`,
        `consistency-violation-undeclared-data-description-elements` and
        `consistency-violation-remove-unit-elements-name`).
        """
        elements = greenfield.responses["elements"]
        assert elements["status"] == "committed", elements
        assert {(entry["address"]["layer"], entry["address"]["element"]) for entry in elements["delta"]} == {
            ("units", ELEMENTS_UNIT),
            ("compu_methods", ELEMENTS_LINEAR),
            ("compu_methods", ELEMENTS_TABLE),
            ("application_types", ELEMENTS_TYPE),
            ("application_types", ELEMENTS_ENUM),
            ("base_types", "uint8_base"),
            ("implementation_types", ELEMENTS_VALUE_TYPE),
        }
        assert all(entry["operation"] == "create" for entry in elements["delta"])

        assert [entry["name"] for entry in greenfield.reads["elements_units"]["value"]] == [ELEMENTS_UNIT]
        # A summary of the layer: each kind with its own fields, a collection as a count.
        assert greenfield.reads["elements_compu_methods"]["value"] == [
            {"name": ELEMENTS_TABLE, "kind": "texttable", "entries": 2},
            {"name": ELEMENTS_LINEAR, "kind": "linear", "unit": ELEMENTS_UNIT},
        ]
        demand = greenfield.reads[f"elements_{ELEMENTS_TYPE}"]["value"]
        assert (demand["unit_ref"], demand["compu_method_ref"]) == (ELEMENTS_UNIT, ELEMENTS_LINEAR)
        assert (demand["unit"], demand["conversion"]) == ("", None), "the inline forms stay unset"
        gear = greenfield.reads[f"elements_{ELEMENTS_ENUM}"]["value"]
        assert (gear["compu_method_ref"], gear["literals"]) == (ELEMENTS_TABLE, None)

        for path in (
            f"data_types/units/{ELEMENTS_UNIT}.yaml",
            f"data_types/compu_methods/{ELEMENTS_LINEAR}.yaml",
            f"data_types/compu_methods/{ELEMENTS_TABLE}.yaml",
        ):
            assert path in greenfield.model, path

    def test_the_value_forms_commit_build_and_read_back(self, greenfield: ScenarioRun) -> None:
        """Two initial values read back from the model as stated and from the ARXML in each type's form.

        The application primitive's value is an `APPLICATION-VALUE-SPECIFICATION` of category `VALUE` whose `UNIT-REF`
        names the type's unit and whose one `V` is the number (`[constr_1221]`); the enumeration's has no unit and one
        `VT`, the entry's name. What it does not reach: a refusal
        (`consistency-violation-interface-init-number-under-table`) and a member of a record or array value.
        """
        values = greenfield.responses["values"]
        assert values["status"] == "committed", values
        assert [(entry["operation"], entry["address"]) for entry in values["delta"]] == [
            ("create", {"layer": "interfaces", "element": VALUE_FORM_INTERFACE})
        ]
        stated = [(row["name"], row["application_type"], row["init"]) for row in greenfield.reads["values"]["value"]]
        assert stated == [("demand", ELEMENTS_TYPE, 12.5), ("gear", ELEMENTS_ENUM, "SyntheticDrive")]

        assert greenfield.value_forms == {
            f"{VALUE_FORM_INTERFACE}_demand_Init": [
                "APPLICATION-VALUE-SPECIFICATION",
                "VALUE",
                f"/DataTypes/Units/{ELEMENTS_UNIT}",
                "V",
                "12.5",
            ],
            f"{VALUE_FORM_INTERFACE}_gear_Init": [
                "APPLICATION-VALUE-SPECIFICATION",
                "VALUE",
                None,
                "VT",
                "SyntheticDrive",
            ],
        }

    def test_the_stated_names_and_the_stub_member_commit_build_and_read_back(self, greenfield: ScenarioRun) -> None:
        """Stated names and a stub's listed operation read back from the model and from the ARXML.

        In the ARXML the behaviour and the call point carry the stated names, and the call point and the client
        com-spec name the operation as the stub's path plus its name. What it does not reach: a refusal
        (`consistency-violation-duplicate-event-name`, `schema-violation-external-interface-member-of-wrong-kind`), a
        stated event name, and a sender-receiver stub's derived com-specs.
        """
        names = greenfield.responses["names"]
        assert names["status"] == "committed", names
        assert sorted((entry["operation"], json.dumps(entry["address"])) for entry in names["delta"]) == sorted(
            (operation["operation"], json.dumps(operation["address"])) for operation in scenario.STATED_NAMES_OPERATIONS
        )
        assert greenfield.reads["names_stub"]["value"] == [STATED_STUB_OPERATION]
        assert greenfield.reads["names_behaviour"]["value"] == STATED_BEHAVIOUR
        # The whole member, its synchrony and result side at their defaults.
        assert greenfield.reads["names_call"]["value"] == {
            "port": "rDiag",
            "operation": STATED_STUB_OPERATION,
            "name": STATED_CALL,
            "asynchronous": False,
            "poll": False,
            "result": None,
            "timeout": None,
        }

        member = f"{STATED_STUB_REF}/{STATED_STUB_OPERATION}"
        assert greenfield.stated_names == {
            "behaviours": [[STATED_BEHAVIOUR], ["SpeedSensorSWC_InternalBehavior"]],
            "call_points": [[STATED_CALL, member]],
            "client_com_specs": [[member]],
        }
        assert f"<SHORT-NAME>{STATED_STUB}</SHORT-NAME>" not in greenfield.artifacts["dist/model.arxml"].decode(
            "utf-8"
        ), "a stub is a reference: the build writes no element for it"

    def test_the_reads_answer_a_layer_an_element_and_a_nested_field(self, greenfield: ScenarioRun) -> None:
        layer = greenfield.reads["layer"]
        # Asked for by name: a layer address answers a summary by default.
        assert layer["form"] == "full"
        assert {entry["name"] for entry in layer["value"]} == {"float32", "Percent", "SyntheticRef"}
        assert {"kind", "base", "unit"} <= set(next(e for e in layer["value"] if e["name"] == "Percent"))
        reference = next(e for e in layer["value"] if e["name"] == "SyntheticRef")
        assert reference["kind"] == "reference_type" and reference["base"] == "float32" and "unit" not in reference

        element = greenfield.reads["element"]
        assert element["form"] == "full", "an element address answers `full` by default and says so"
        assert element["value"]["name"] == "Task_20ms"
        # The `full` form publishes the absent selector as `null`.
        assert element["value"]["runnables"] == [{"swc": "BrakeCtrlSWC", "instance": None, "runnable": "BrakeCtrlRun"}]

        # `ports -> member rSpeed -> interface`: the nested address vocabulary,
        # answering with the scalar itself rather than the element around it.
        assert greenfield.reads["field"]["value"] == "IVehicleSpeed"

    def test_the_bounded_reads_answer_through_the_installed_surface(self, greenfield: ScenarioRun) -> None:
        """The bounded-read keys, each asked once: they survived the tool signature into a distribution.

        **What it does not reach:** the width of any answer, the refusal classes (an undeclared filter field, a cursor
        the slice does not carry, `members` at an element address) and the budget.
        """
        # A filter that dropped either clause would answer two elements.
        filtered = greenfield.reads["filtered"]
        assert filtered["form"] == "index"
        assert filtered["where"] == [{"field": "kind", "equals": "value_type"}, {"field": "unit", "exists": True}]
        assert filtered["value"] == {"count": 1, "identities": ["Percent"]}

        # Kind rank, then ordinal identity order ('P' < 'f').
        paged = greenfield.reads["paged"]
        assert paged["page"] == {"total": 3, "returned": 2, "truncated": True, "next": "float32"}
        assert paged["value"]["identities"] == ["Percent", "float32"]

        continued = greenfield.reads["paged_next"]
        assert continued["value"]["identities"] == ["SyntheticRef"], "the cursor continued past what page one served"
        assert continued["page"]["total"] == 3
        assert "next" not in continued["page"], "the last page offers no cursor to continue from"

        # The layer-wide member slice: each member carries its owner.
        members = greenfield.reads["members"]
        assert members["members"] == "ports"
        assert {(row["element"], row["member"]["name"]) for row in members["value"]} == {
            ("SpeedSensorSWC", "pSpeed"),
            ("BrakeCtrlSWC", "rSpeed"),
            ("BrakeCtrlSWC", "pLoad"),
        }

    def test_discover_answers_summary_by_default_and_full_when_asked(self, greenfield: ScenarioRun) -> None:
        """`discover` answers `summary` by default and says so; `form: "full"` brings the prose back.

        The summary strips the descriptor prose and leaves out a `false` boolean. The project-wide `full` is an
        addressless `over-budget` refusal hinting the summary and then one `full` per layer, so the prose is read off
        the first hinted step that answers.
        """
        default = greenfield.responses["discover"]
        refused = greenfield.responses["discover_full"]
        full = greenfield.responses["discover_layer_full"]
        assert default["form"] == "summary"
        assert all("description" not in layer for layer in default["layers"])
        assert refused["response"] == "error" and refused["error"] == "over-budget"
        assert "form" not in refused and "addresses" not in refused
        assert refused["hints"][:2] == [
            {"operation": "discover", "form": "summary"},
            {"operation": "discover", "layer": "swc", "form": "full"},
        ]
        assert full["form"] == "full"
        assert any("description" in layer for layer in full["layers"])
        assert [layer["layer"] for layer in full["layers"]] == ["swc"]
        assert "swc" in [layer["layer"] for layer in default["layers"]], "narrowing selects a ROW the summary lists"

    def test_the_nested_update_changes_exactly_one_field(self, greenfield: ScenarioRun) -> None:
        (entry,) = greenfield.responses["update"]["delta"]
        assert entry["operation"] == "update"
        assert entry["address"]["path"] == [{"field": "ports"}, {"member": "rSpeed"}, {"field": "description"}]
        assert entry["before"] == ""
        assert entry["after"] == "Receives the measured vehicle speed"

    def test_the_approved_removal_spans_three_layers_in_one_transaction(self, greenfield: ScenarioRun) -> None:
        removed = greenfield.responses["remove"]
        assert removed["status"] == "committed"
        addresses = {
            (entry["address"]["layer"], entry["address"].get("element"))
            for entry in removed["delta"]
            if entry["operation"] == "remove"
        }
        assert addresses == {
            ("swc", "BrakeCtrlSWC"),
            ("interfaces", "IBrakeLoad"),
            ("implementation_types", "Percent"),
        }

    def test_a_removal_without_its_cleanup_is_refused(self, greenfield: ScenarioRun) -> None:
        """The cleanup travels in the same change set: removing an interface alone leaves its ports dangling.

        Run against the committed model, which no longer holds `IBrakeLoad`, so `IVehicleSpeed` stands in.
        """
        serve(greenfield.project, review=None)
        baseline = call("discover").structured["baseline"]
        structured, _texts = call(
            "change_set",
            baseline=baseline,
            operations=[{"operation": "remove", "address": {"layer": "interfaces", "element": "IVehicleSpeed"}}],
            intent="Drop the speed interface without touching its referrers.",
        )
        assert structured["response"] == "error"
        assert structured["error"] == "broken-reference"

    def test_the_final_model_holds_the_survivors_and_none_of_the_removed(self, greenfield: ScenarioRun) -> None:
        stored = b"\n".join(greenfield.model.values())
        for identity in SURVIVORS:
            assert identity.encode() in stored, identity
        for identity in REMOVED_IDENTITIES:
            assert identity.encode() not in stored, identity


# ---------------------------------------------------------------------------
# Clause 2: no YAML writes, no product source, no layer-specific function
# ---------------------------------------------------------------------------


class TestNothingButTheProtocolAuthoredThis:
    """*Without direct YAML writes, product-source access, or a layer-specific MCP function*.

    Read off the scenario's own source and the live `tools/list`.
    """

    ALLOWED = ALLOWED_TOOLS

    def test_the_scenario_calls_only_the_protocol_and_the_two_gates(self, greenfield: ScenarioRun) -> None:
        assert set(greenfield.tool_calls) == self.ALLOWED
        assert greenfield.tool_calls.count("change_set") == 7

    def test_every_authoring_call_is_the_one_generic_change_set_tool(self, greenfield: ScenarioRun) -> None:
        """No `add_swc`, no `add_port`, no `create_data_type` — and none exist."""
        registered = tool_names()
        assert self.ALLOWED <= registered
        for removed in ("add_swc", "add_port", "create_data_type", "add_connector", "delete_element", "read_swc"):
            assert removed not in registered

    #: Both halves: the assertions here and the scenario that drives the run.
    SCANNED = (Path(__file__), Path(scenario.__file__))

    @pytest.mark.parametrize("module", SCANNED, ids=lambda path: path.stem)
    def test_the_scenario_imports_no_writer_and_opens_no_model_file(self, module: Path) -> None:
        """An AST scan of both modules: no writer or `yaml` import, and no write call.

        `swcraft_tools.cli` (the scaffold) and `schema`/`editing.registry` (the extension fixture's declaration)
        write no model file. Plain `Name` calls are scanned too, so a bare `open(path, "w")` is caught.
        """
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        forbidden = {name for name in imported if "writer" in name or name == "yaml"}
        assert forbidden == set(), f"the greenfield scenario must author nothing directly: {sorted(forbidden)}"

        called: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        offenders = {"write_text", "write_bytes", "safe_dump", "mkdir", "open"} & called
        assert not offenders, sorted(offenders)


# ---------------------------------------------------------------------------
# Clause 3: the refusals leave the real model byte-identical
# ---------------------------------------------------------------------------


class TestTheRefusedChangeSets:
    """*Invalid, unauthorized, stale, and partially applicable change sets leave the real model unchanged*.

    One case per code of the closed refusal enum, each leaving the committed model byte-identical.
    """

    @pytest.mark.parametrize("code", sorted(INVALID_CHANGE_SETS))
    def test_an_invalid_change_set_is_answered_with_its_own_code(self, greenfield: ScenarioRun, code: str) -> None:
        serve(greenfield.project, review="elicit")
        structured = refused(greenfield.project, INVALID_CHANGE_SETS[code])
        assert structured["response"] == "error"
        assert structured["error"] == code, structured

    def test_an_unauthorized_change_set_is_refused_before_it_is_staged(self, greenfield: ScenarioRun) -> None:
        """The host approved one address; the change set names another."""
        serve(greenfield.project, scope=UNAUTHORIZED_SCOPE, scope_id="greenfield-turn")
        structured = refused(greenfield.project, UNAUTHORIZED_OPERATIONS)
        assert structured["error"] == "unauthorized-delta"
        assert structured["addresses"] == [UNAUTHORIZED_ADDRESS]

    def test_a_stale_change_set_is_refused_rather_than_rebased(self, greenfield: ScenarioRun) -> None:
        """The token the run started from, seven commits later."""
        serve(greenfield.project)
        structured = refused(greenfield.project, STALE_OPERATIONS, baseline=greenfield.first_baseline)
        assert structured["error"] == "stale-baseline"

    def test_a_partially_applicable_change_set_is_refused_rather_than_reordered(self, greenfield: ScenarioRun) -> None:
        """Valid as a whole, inapplicable in the order given; applying it would commit a sequence nobody reviewed."""
        serve(greenfield.project)
        structured = refused(greenfield.project, PARTIAL_OPERATIONS)
        assert structured["error"] == "partial-change-set"

    def test_the_same_operations_reordered_commit_so_the_refusal_is_the_only_obstacle(
        self, greenfield: ScenarioRun, tmp_path: Path
    ) -> None:
        """The positive control: reordered, the operations land, so the order is the only obstacle.

        Without it, a set refused by another rule would keep the case green with the order refusal deleted. Run
        against a copy of the project; `serve` is re-pointed at the run afterwards.
        """
        import shutil

        copy = tmp_path / "reordered"
        shutil.copytree(greenfield.project, copy)
        try:
            serve(copy)
            token = call("discover").structured["baseline"]
            human = ScriptedHuman(True)
            structured, _texts = call_reviewed(
                human,
                "change_set",
                baseline=token,
                operations=PARTIAL_OPERATIONS_REORDERED,
                intent="The same operations, in an order that applies.",
            )

            assert structured["status"] == "committed", structured
            assert human.questions, "the commit went through the review boundary"
            assert (copy / "model" / "swc" / "control" / "LateSWC.yaml").is_file()
            assert (copy / "model" / "requirements" / "REQ_LATE_001.yaml").is_file()
        finally:
            serve(greenfield.project)

    def test_every_refusal_class_of_the_closed_enum_is_covered(self) -> None:
        """No refusal class is left untested; a new error code without a case fails here.

        `over-budget` is excluded by name: it refuses a read too large for the budget, which a greenfield project
        cannot produce; it is tested on a wide model elsewhere. The exclusion is an equality, so a second untested
        code still fails.
        """
        from swcraft_tools.editing.protocol import ERROR_CODES

        covered = set(INVALID_CHANGE_SETS) | set(NON_INVALID_REFUSALS)
        assert covered <= set(ERROR_CODES)
        assert sorted(set(ERROR_CODES) - covered) == ["over-budget"]


# ---------------------------------------------------------------------------
# Clause 4a: the four observations agree
# ---------------------------------------------------------------------------


class TestTheFourObservationsAgree:
    """*The reviewed semantic diff, audit evidence, validation and the canonical build agree*.

    Four independent observations of one change:

    * the **reviewed diff**: the elicitation message the human answered;
    * the **audit evidence**: `dist/audit.jsonl`, with storage targets no protocol response carries;
    * **validation**: the model re-read from disk, the cross-layer rules and the ARXML/XSD gate;
    * the **canonical build**: the generated ARXML, diagram and needs.json bytes.

    Each assertion fails on one way they could disagree: a partly applied delta, a removal that left a referrer, a
    build from a cached model.
    """

    def test_the_human_was_shown_the_exact_delta_that_committed(self, greenfield: ScenarioRun) -> None:
        """The exact delta, which means the **values** and not only the addresses.

        The defect: a per-value display bound elided part of the seed's first `create`, so the human approved content
        they had not been shown, and the audit records only addresses. The payload halves are asserted verbatim.
        """
        from swcraft_tools.editing.address import Address
        from swcraft_tools.editing.review import render_address

        assert len(greenfield.questions) == 7, "one exact-delta question per committed change set"
        halves = 0
        for key, question in zip(
            ("seed", "pair", "elements", "values", "names", "update", "remove"), greenfield.questions, strict=True
        ):
            response = greenfield.responses[key]
            for entry in response["delta"]:
                rendered = render_address(Address.from_json(entry["address"]))
                assert rendered in question, f"{rendered} was committed but never shown"
                for half in ("before", "after"):
                    if half not in entry:
                        continue
                    value = json.dumps(entry[half], ensure_ascii=False)
                    assert value in question, f"{rendered} {half} was committed but only partly shown"
                    halves += 1
            assert "commit" in question.lower()
        # The count keeps the loop load-bearing: an entry that stopped carrying `before`/`after` would assert nothing.
        assert halves == 36, halves
        # The only elision: the `remove` step shows its two whole removed elements' `before` in summary form, and
        # says so once per element.
        for key, question in zip(
            ("seed", "pair", "elements", "values", "names", "update", "remove"), greenfield.questions, strict=True
        ):
            assert "not shown" not in question
            expected = 2 if key == "remove" else 0
            assert question.count("the whole value is elided: ") == expected, key
            assert question.count("elided") == expected, key

    def test_the_audit_trail_names_the_same_change(self, greenfield: ScenarioRun) -> None:
        lifecycle = [record["operation"] for record in greenfield.audit if record["operation"].startswith("change_set")]
        assert (
            lifecycle
            == [
                "change_set_staged",
                "change_set_approved",
                "change_set_commit_intent",
                "change_set_committed",
            ]
            * 7
        )

        # One change-set id per commit, shared by that commit's four records.
        ids = [record["args"]["change_set_id"] for record in greenfield.audit if "change_set_id" in record["args"]]
        assert len(set(ids)) == 7
        assert all(CHANGE_SET_ID_PATTERN.match(value) for value in ids)

        # The writer half: removed storage targets are gone from the tree; written ones are in it.
        touched = {
            record["args"]["target"]: record["operation"]
            for record in greenfield.audit
            if record["operation"] in ("write_element", "remove_element")
        }
        for target, operation in touched.items():
            assert (target in greenfield.model) is (operation == "write_element"), target
        assert "interfaces/IBrakeLoad.yaml" in touched
        assert touched["interfaces/IBrakeLoad.yaml"] == "remove_element"

    def test_validation_agrees_with_the_committed_model(self, greenfield: ScenarioRun) -> None:
        verdict = greenfield.responses["validate"]
        assert verdict["success"] is True, verdict["errors"]
        assert verdict["errors"] == []
        # `validate` re-reads the model from disk, so a removal that left a referrer would fail here.
        assert "model.arxml" in verdict["output"]

    def test_the_built_artifacts_describe_the_same_model(self, greenfield: ScenarioRun) -> None:
        build = greenfield.responses["build"]
        assert build["success"] is True, build["errors"]

        arxml = greenfield.artifacts["dist/model.arxml"].decode("utf-8")
        diagram = greenfield.artifacts["views/ibd-Root.svg"].decode("utf-8")
        needs = json.loads(greenfield.artifacts["dist/needs.json"].decode("utf-8"))

        for identity in ("SpeedSensorSWC", "BrakeCtrlSWC", "IVehicleSpeed", "float32"):
            assert identity in arxml, identity
        # The same-name pair: each data element's type reference names its own side's package, and each type's
        # inline bounds generate a range of their own in that package.
        for package, dest in (
            ("Application", "APPLICATION-PRIMITIVE-DATA-TYPE"),
            ("Implementation", "IMPLEMENTATION-DATA-TYPE"),
        ):
            assert f'<TYPE-TREF DEST="{dest}">/DataTypes/{package}/{SAME_NAME_PAIR}</TYPE-TREF>' in arxml, package
            assert f">/DataTypes/{package}/{SAME_NAME_PAIR}_Range</DATA-CONSTR-REF>" in arxml, package
        # The data description elements, each written once into its own layer's package and referenced there; no
        # type generates a private form. The unit's third referrer is the initial value under the application type.
        for tag, package, name, referrers in (
            ("UNIT", "Units", ELEMENTS_UNIT, 3),
            ("COMPU-METHOD", "CompuMethods", ELEMENTS_LINEAR, 1),
            ("COMPU-METHOD", "CompuMethods", ELEMENTS_TABLE, 2),
        ):
            reference = f'<{tag}-REF DEST="{tag}">/DataTypes/{package}/{name}</{tag}-REF>'
            assert arxml.count(reference) == referrers, reference
            assert re.search(rf"<{tag}>\s*<SHORT-NAME>{name}</SHORT-NAME>", arxml), name
        for private in (f"{ELEMENTS_TYPE}_Unit", f"{ELEMENTS_TYPE}_Compu", f"{ELEMENTS_ENUM}_CompuMethod"):
            assert private not in arxml, private
        for name in (ELEMENTS_TYPE, ELEMENTS_ENUM, ELEMENTS_VALUE_TYPE):
            assert f"<SHORT-NAME>{name}</SHORT-NAME>" in arxml, name
        for identity in ("SpeedSensorSWC", "BrakeCtrlSWC"):
            assert identity in diagram, identity
        (published,) = needs["versions"].values()
        assert published["needs"]["REQ_BRAKE_001"]["type"] == "arch_req"

        for identity in REMOVED_IDENTITIES:
            assert identity not in arxml, identity
            assert identity not in diagram, identity


# ---------------------------------------------------------------------------
# Clause 4b: the outputs are deterministic
# ---------------------------------------------------------------------------

#: The writer's per-session correlation id, a uuid4 hex allocated per commit block, so it differs between runs.
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")


def normalize(value: object, root: str) -> object:
    """Replace everything a second identical run is *allowed* to differ in.

    Only the audit records, and only four things: the change-set id, the writer session id, the timestamps and the
    project's absolute path. Baseline tokens are content hashes and are **not** normalized. The model tree and the
    artifacts are compared with no normalization.
    """
    if isinstance(value, dict):
        return {key: normalize(item, root) for key, item in value.items() if key != "timestamp"}
    if isinstance(value, list):
        return [normalize(item, root) for item in value]
    if isinstance(value, str):
        if CHANGE_SET_ID_PATTERN.match(value):
            return "<change-set-id>"
        if _SESSION_ID.match(value):
            return "<session-id>"
        if value.startswith(root):
            return "<project>" + value[len(root) :]
    return value


class TestTheOutputsAreDeterministic:
    """*…and deterministic outputs shall agree*.

    The same scenario twice, into two directories, compared byte for byte.
    """

    def test_a_second_run_produces_the_same_model_and_the_same_artifacts(
        self, greenfield: ScenarioRun, tmp_path: Path
    ) -> None:
        second = run_scenario(tmp_path / "second" / "project")

        assert second.project != greenfield.project
        assert second.model == greenfield.model, "the same operations twice must produce the same model bytes"
        for name in ARTIFACTS:
            # Non-emptiness first, so two artifacts never generated cannot satisfy the equality.
            assert second.artifacts[name], name
            assert second.artifacts[name] == greenfield.artifacts[name], name

        # The normalization is needed: the raw logs really differ.
        raw_first = (greenfield.project / "dist" / "audit.jsonl").read_bytes()
        raw_second = (second.project / "dist" / "audit.jsonl").read_bytes()
        assert raw_first != raw_second
        assert normalize(second.audit, str(second.project)) == normalize(greenfield.audit, str(greenfield.project))


# ---------------------------------------------------------------------------
# Clause 5: a supported schema-field extension needs no MCP registration
# ---------------------------------------------------------------------------

#: The field the extension fixture declares; it exists in no `schema.py`, annotation table or MCP tool.
EXTENSION_FIELD = "telemetry_channel"


@pytest.fixture()
def extended_schema() -> Any:
    """Declare one new `SWComponent` field, the way `schema.py` would.

    The field is added to the live Pydantic model and the registry re-derived **in place**, so every default argument
    bound to the production registry sees it; no MCP registration, annotation row or tool signature is touched. Every
    change is made inside the ``try``, so the teardown restores everything even after a half-done setup.

    The model contract's ``swc`` kind gains the field too: an undeclared key is ``rule:model.unknown-key``, which
    stops every change set, and a real extension is contract-first.
    """
    contract = production_registry()
    original_swc_kind = contract.kind("swc")
    original_contract_layers = contract.layers
    original_fields = dict(schema.SWComponent.model_fields)
    original_layers = reg.REGISTRY.layers
    original_storage = dict(reg.REGISTRY._storage)
    original_kinds = dict(reg.REGISTRY._kinds)

    def install(registry: reg.Registry) -> None:
        object.__setattr__(reg.REGISTRY, "layers", registry.layers)
        reg.REGISTRY._storage.clear()
        reg.REGISTRY._storage.update(registry._storage)
        reg.REGISTRY._kinds.clear()
        reg.REGISTRY._kinds.update(registry._kinds)

    try:
        schema.SWComponent.model_fields[EXTENSION_FIELD] = FieldInfo.from_annotated_attribute(str | None, None)
        schema.SWComponent.model_rebuild(force=True)
        install(reg.build_registry())
        declared = FieldFacts(
            id=f"field:swc.swc.{EXTENSION_FIELD}",
            layer=original_swc_kind.layer,
            kind=original_swc_kind.kind,
            name=EXTENSION_FIELD,
            type="string",
            required=False,
            immutable=False,
            has_default=True,
        )
        extended_kind = replace(original_swc_kind, fields=(*original_swc_kind.fields, declared))
        contract._kinds["swc"] = extended_kind  # type: ignore[index]
        object.__setattr__(
            contract,
            "layers",
            tuple(
                replace(
                    layer, kinds=tuple(extended_kind if kind is original_swc_kind else kind for kind in layer.kinds)
                )
                for layer in original_contract_layers
            ),
        )
        yield
    finally:
        contract._kinds["swc"] = original_swc_kind  # type: ignore[index]
        object.__setattr__(contract, "layers", original_contract_layers)
        schema.SWComponent.model_fields.clear()
        schema.SWComponent.model_fields.update(original_fields)
        schema.SWComponent.model_rebuild(force=True)
        object.__setattr__(reg.REGISTRY, "layers", original_layers)
        reg.REGISTRY._storage.clear()
        reg.REGISTRY._storage.update(original_storage)
        reg.REGISTRY._kinds.clear()
        reg.REGISTRY._kinds.update(original_kinds)


def swc_field_names(discovered: dict[str, Any]) -> set[str]:
    """Every field name `discover` publishes for the `swc` layer's own kind.

    Asked of a `discover` naming the layer: a project-wide one with no form answers `baseline`, with no descriptor.
    """
    (layer,) = [entry for entry in discovered["layers"] if entry["layer"] == "swc"]
    (kind,) = [entry for entry in layer["element_kinds"] if entry["kind"] == "swc"]
    return {descriptor["name"] for descriptor in kind["fields"]}


class TestASupportedFieldExtensionNeedsNoRegistration:
    """*A supported schema-field extension does not require another MCP registration*.

    End to end at the MCP surface: `discover` publishes the field, `change_set` writes it, `read` answers it and the
    YAML holds it, while `tools/list` is unchanged and no tool names the field.
    """

    def test_the_field_is_absent_before_it_is_declared(self, tmp_path: Path) -> None:
        serve(scaffold(tmp_path / "plain"), review=None)
        assert EXTENSION_FIELD not in swc_field_names(call("discover", layer="swc").structured)

    def test_the_new_field_is_discoverable_readable_and_writable(self, tmp_path: Path, extended_schema: None) -> None:
        project = scaffold(tmp_path / "extended")
        serve(project)

        discovered = call("discover", layer="swc").structured
        assert EXTENSION_FIELD in swc_field_names(discovered), "a newly declared field must appear in `discover`"

        human = ScriptedHuman(True)
        committed = call_reviewed(
            human,
            "change_set",
            baseline=discovered["baseline"],
            operations=[
                create(
                    "swc",
                    "TelemetrySWC",
                    {
                        "name": "TelemetrySWC",
                        "group": "control",
                        "type": "application",
                        "ports": [],
                        "runnables": [],
                        "handle_termination_and_restart": "no_support",
                        "satisfies": ["REQ_TELEMETRY_001@1"],
                        EXTENSION_FIELD: "can0",
                    },
                ),
                create(
                    "requirements",
                    "REQ_TELEMETRY_001",
                    {
                        "id": "REQ_TELEMETRY_001",
                        "title": "Telemetry",
                        "description": "The component shall publish telemetry.",
                        "req_type": "req",
                        "version": 1,
                        "source": "architecture",
                    },
                ),
            ],
            intent="Add a component that declares the newly supported field.",
        ).structured
        assert committed["status"] == "committed", committed
        assert human.questions, "the new field's change set went through the same review boundary"

        answered = call(
            "read", address={"layer": "swc", "element": "TelemetrySWC", "path": [{"field": EXTENSION_FIELD}]}
        ).structured
        assert answered["value"] == "can0"

        stored = (project / "model" / "swc" / "control" / "TelemetrySWC.yaml").read_text(encoding="utf-8")
        assert f"{EXTENSION_FIELD}: can0" in stored

    def test_no_mcp_registration_was_added_for_it(self, tmp_path: Path, request: pytest.FixtureRequest) -> None:
        """The surface did not have to grow: the same tools before and after, none naming the field.

        What this does not reach: the field's name in the server's code where no client sees it (a comment, a branch
        that reads the field by name).
        """
        serve(scaffold(tmp_path / "surface"), review=None)
        before = {tool.name for tool in list_tools()}
        request.getfixturevalue("extended_schema")
        assert EXTENSION_FIELD in swc_field_names(call("discover", layer="swc").structured)
        tools = list_tools()
        assert {tool.name for tool in tools} == before
        for tool in tools:
            published = json.dumps([tool.name, tool.description, tool.inputSchema], sort_keys=True)
            assert EXTENSION_FIELD not in published, tool.name

    def test_the_extension_does_not_leak(self, tmp_path: Path) -> None:
        """The fixture's teardown really restores the production contract."""
        serve(scaffold(tmp_path / "restored"), review=None)
        assert EXTENSION_FIELD not in swc_field_names(call("discover", layer="swc").structured)
