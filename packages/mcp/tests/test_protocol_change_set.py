"""The `change_set` tool: the one mutation surface, driven over the wire.

Every exchange goes through `tools/call`: a *transport* error (an argument that cannot bind) is
FastMCP's and a *protocol* rejection (nested content pass A refuses) the engine's, and calling
`server.change_set()` would collapse the two into one Python exception.

`baseline` is required with no default: a client that has not called `discover` cannot state
one, and a token that no longer matches the model is refused rather than applied to a state its
author never saw.

`$defs/changeSetResponse` is closed and cannot carry one condition: a commit whose model landed
in full and whose audit append failed. The response stays `committed` with `audit_ref` withheld,
and the reason reaches the host as a separate text block after the body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
import swcraft_tools.editing.passes as passes_mod
import swcraft_tools.session as session_mod
from conftest import model_element_path
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from protocol_client import DECLINE, ScriptedHuman, audit_records, body_of, call, call_reviewed
from swcraft_tools.auditor import AuditLog
from swcraft_tools.diagnostics import Subject
from swcraft_tools.editing.audit import CHANGE_SET_COMMIT_INTENT, CHANGE_SET_COMMITTED, LIFECYCLE_OPERATIONS
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.protocol import ChangeSetResponse, ErrorResult, parse_response
from swcraft_tools.editing.read import compact_json
from swcraft_tools.session import CommitLogError, UnexplainedChangeError
from swcraft_tools.validator import ConsistencyIssue, ConsistencyResult

from tests.test_review_mode import PLAIN, arguments, set_mode, state, use_project


def audit_operations(project: Path) -> list[str]:
    """Every operation name in the project's real audit log, in order."""
    return [record["operation"] for record in audit_records(project)]


class TestAChangeSetIsTheEnginesOwnAnswer:
    """The tool's structured result is what `handle_request` answers for the reconstructed request."""

    def test_a_staged_change_set_matches_the_direct_engine_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)

        structured, _texts = call("change_set", **named)

        assert structured == handle_request({"operation": "change_set", **named}, project=str(project))
        assert structured["status"] == "staged"

    def test_the_structured_half_is_a_protocol_response(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)

        structured, _texts = call("change_set", **arguments(PLAIN, project))

        assert isinstance(parse_response(structured), ChangeSetResponse)

    def test_an_omitted_intent_and_an_explicit_null_are_the_same_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The schema admits no null intent, so the tool must not forward its default.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)
        named.pop("intent", None)

        assert call("change_set", **named).structured == call("change_set", intent=None, **named).structured


class TestTheBaselineIsUnavoidable:
    def test_a_change_set_without_a_baseline_never_reaches_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A transport error: the argument cannot bind, so there is no request for pass A to refuse.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)

        with pytest.raises(ToolError):
            call("change_set", operations=named["operations"])

    def test_a_stale_baseline_is_refused_before_anything_is_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)
        fixtures.make_stale(project)
        before = state(project)

        structured, _texts = call("change_set", **named)

        assert structured["response"] == "error"
        assert structured["error"] == "stale-baseline"
        assert state(project) == before


class TestTheTransportBoundaryAndThePassABoundary:
    """An argument that cannot bind is FastMCP's; malformed content is pass A's."""

    def test_operations_must_be_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        with pytest.raises(ToolError):
            call("change_set", baseline=arguments(PLAIN, project)["baseline"])

    def test_a_scalar_where_an_operations_array_belongs_is_a_transport_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        with pytest.raises(ToolError):
            call("change_set", baseline=arguments(PLAIN, project)["baseline"], operations="everything")

    def test_malformed_nested_content_reaches_pass_a_and_gets_a_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The nested annotations are permissive (`list[dict]`), so pass A stays the single
        # validator rather than a second Pydantic model restating the protocol.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)

        structured, _texts = call(
            "change_set", baseline=named["baseline"], operations=[{"operation": "rename", "address": {"layer": "swc"}}]
        )

        assert isinstance(parse_response(structured), ErrorResult)
        assert structured["error"] == "schema-violation"

    def test_an_empty_operations_array_is_refused_by_the_protocol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)

        structured, _texts = call("change_set", baseline=arguments(PLAIN, project)["baseline"], operations=[])

        assert structured["error"] == "schema-violation"


class TestTheHostNoteThatCannotGoInsideTheResponse:
    """A committed model whose audit trail is incomplete."""

    def test_the_note_travels_as_a_text_block_beside_a_valid_structured_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        def failing_append(self: Any, **kwargs: Any) -> None:
            raise CommitLogError(
                "dist/audit.jsonl is read-only", ["model/data_types/implementation/VehicleSpeed_kph.yaml"]
            )

        monkeypatch.setattr("swcraft_tools.editing.audit.ChangeSetAuditor.committed", failing_append)

        structured, texts = call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))

        # The model did land: the answer must not send the user looking for damage.
        assert structured["status"] == "committed"
        assert "audit_ref" not in structured
        assert isinstance(parse_response(structured), ChangeSetResponse)
        assert any("audit trail is incomplete" in text for text in texts), texts

    def test_a_change_set_with_no_note_carries_no_extra_text_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)

        structured, texts = call("change_set", **arguments(PLAIN, project))

        assert len(texts) == 1, "the ordinary result is the serialized response and nothing else"
        # The one body block is the compact serialization of a valid response, in either wire shape.
        assert texts[0] == compact_json(json.loads(texts[0]))
        assert isinstance(parse_response(json.loads(texts[0])), ChangeSetResponse)


class TestAChangeSetAddsNoSeventhLifecycleRecord:
    """Six lifecycle records and no seventh.

    Recording the review mode would not prove that a human answered: an environment variable is
    configuration, not evidence. `change_set_approved` is written from the boundary's decision.
    """

    @pytest.mark.parametrize(
        ("mode", "answers", "expected"),
        [
            (None, (), "staged"),
            ("elicit", (True,), "committed"),
            ("elicit", (DECLINE,), "abandoned"),
        ],
        ids=["staged", "committed", "abandoned"],
    )
    def test_no_record_outside_the_six_is_written(
        self,
        mode: str | None,
        answers: tuple[Any, ...],
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, mode)

        structured, _texts = call_reviewed(ScriptedHuman(*answers), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == expected
        recorded = {name for name in audit_operations(project) if name.startswith("change_set")}
        assert recorded, "a change set left no lifecycle record at all"
        assert recorded <= set(LIFECYCLE_OPERATIONS), sorted(recorded - set(LIFECYCLE_OPERATIONS))
        assert "change_set_review_channel" not in recorded

    def test_a_rejected_change_set_is_recorded_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)
        fixtures.make_stale(project)

        call("change_set", **named)

        recorded = {name for name in audit_operations(project) if name.startswith("change_set")}
        assert "change_set_rejected" in recorded
        assert recorded <= set(LIFECYCLE_OPERATIONS)


# The write path's own name guards.


def _plain_model(tmp_path: Path) -> Path:
    """The autouse conftest project's model root: a bare seed, so nothing in the model matters to the answer."""
    return tmp_path / "model"


def _bytes_of(model_root: Path) -> str:
    """The shared model-state digest, taken from a bare model root."""
    return fixtures.model_state_key(model_root)


def _create(layer: str, element: str, value: dict[str, Any]) -> dict[str, Any]:
    structured, _texts = call(
        "change_set",
        baseline=call("discover").structured["baseline"],
        operations=[{"operation": "create", "address": {"layer": layer, "element": element}, "value": value}],
        intent=f"Create {layer}/{element}.",
    )
    return structured


class TestTheIdentifierRuleRefusesThroughTheProtocol:
    """The identifier rule as a protocol exchange: `invalid-identifier`, addressed at the element.

    Both halves, because they fail in different places: a name that is not an `Identifier` at
    all, and a legal name whose emitted generated form overflows 128 characters.
    """

    def test_a_name_that_is_not_an_identifier_is_refused_with_its_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        model_root = _plain_model(tmp_path)
        before = _bytes_of(model_root)

        structured = _create("swc", "My SWC", {"name": "My SWC", "group": "body", "type": "application"})

        assert structured["response"] == "error"
        assert structured["error"] == "invalid-identifier"
        assert "AUTOSAR Identifier" in structured["message"]
        assert structured["addresses"] == [{"layer": "swc", "element": "My SWC"}]
        assert _bytes_of(model_root) == before

    def test_a_legal_name_whose_generated_form_overflows_is_refused_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The half a declared-name check would miss: `_InternalBehavior` costs 17 characters and
        # lands a legal 112-character name on 129.
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        model_root = _plain_model(tmp_path)
        name = "S" * 112
        before = _bytes_of(model_root)

        structured = _create("swc", name, {"name": name, "group": "body", "type": "application"})

        assert structured["error"] == "invalid-identifier"
        assert "_InternalBehavior" in structured["message"]
        assert _bytes_of(model_root) == before


class TestTheReservedNameGuardsRefuseThroughTheProtocol:
    """The exporter's reserved names, as the code pass D would have given them.

    `write_element`'s guard refuses during materialization, before pass D runs, so the engine
    carries the code over from `validator.CONSISTENCY_RULES`: a `consistency-violation` addressed
    at the element whose declared name collides. One case is a surviving collision
    (`{enum}_CompuMethod`), the other the inversion: retired reserved names are created freely.
    """

    def test_a_name_colliding_with_a_generated_compu_method_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name the writer generates for itself (`{enum}_CompuMethod`) is refused; the model is untouched.

        The occupant is a second enum: the form collides only with a type of its enum's package, so
        a base type of that name is legal.
        """
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        model_root = _plain_model(tmp_path)
        model_element_path(model_root, "Gear", kind="enum").parent.mkdir(parents=True, exist_ok=True)
        (model_element_path(model_root, "Gear", kind="enum")).write_text(
            "name: Gear\nkind: enum\nliterals:\n- name: PARK\n  value: 0\n", encoding="utf-8"
        )
        before = _bytes_of(model_root)

        structured = _create(
            "application_types",
            "Gear_CompuMethod",
            {"name": "Gear_CompuMethod", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
        )

        assert structured["error"] == "consistency-violation"
        assert "Gear_CompuMethod" in structured["message"]
        assert "COMPU-METHOD" in structured["message"]
        assert structured["addresses"] == [{"layer": "application_types", "element": "Gear_CompuMethod"}]
        assert _bytes_of(model_root) == before

    def test_the_two_retired_names_are_created_like_any_other(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retired reserved names `DataTypeMappingSet` and `{x}_AppView` are refused for nothing.

        A guard still refusing them would be stricter than the writer, on the one surface an LLM
        edits through.
        """
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        model_root = _plain_model(tmp_path)
        (model_root / "data_types").mkdir(parents=True, exist_ok=True)
        (model_element_path(model_root, "u8_base", kind="base_type")).write_text(
            "name: u8_base\nkind: base_type\nsize: 8\nencoding: none\nnative_declaration: uint8\n", encoding="utf-8"
        )
        (model_element_path(model_root, "u8", kind="value_type")).write_text(
            "name: u8\nkind: value_type\nbase: u8_base\n", encoding="utf-8"
        )

        for name in ("u8_AppView", "DataTypeMappingSet"):
            structured = _create("implementation_types", name, {"name": name, "kind": "value_type", "base": "u8_base"})

            # `_plain_model` declares no root composition, so pass D refuses with
            # `rule:composition.no-root` whatever the change set carries. Asserting that refusal
            # matters: "the name is absent from the message" alone passed on a success response,
            # an empty dict and any unrelated error.
            assert structured["response"] == "error", structured
            assert structured["error"] == "consistency-violation", structured
            assert "No composition declares 'root: true'" in structured["message"], structured

            # The name is not what is refused: neither the message nor an address names it.
            assert name not in structured["message"], structured
            assert not [address for address in structured["addresses"] if address.get("element") == name], structured


class TestTheDataDescriptionLayersThroughTheWire:
    """The type layers, committed and refused over `tools/call`, past what vector replay carries.

    The same-name pair is committed under a human's answer (one file in each layer's directory,
    each read back as its own kind), and the retired layout refuses a change set and leaves the
    model as it was.

    What they do not reach: the generated ARXML of the pair, and every other request a retired
    layout refuses.
    """

    PAIR = "create-same-name-application-and-implementation-types"
    RETIRED = "consistency-violation-retired-data-types-directory"

    @staticmethod
    def _serve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
        server._PROJECT = None
        set_mode(monkeypatch, server.REVIEW_ELICIT)

    def test_a_same_name_pair_commits_one_file_in_each_layer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[self.PAIR]
        project = fixtures.build_fixture(tmp_path / "project", self.PAIR)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=vector["input"]["operations"],
            intent=vector["input"]["intent"],
        )

        assert structured["status"] == "committed", structured
        assert human.questions, "the pair went through the review boundary"
        model_root = project / "model"
        assert (model_root / "data_types" / "application" / "SyntheticTorque.yaml").is_file()
        assert (model_root / "data_types" / "implementation" / "SyntheticTorque.yaml").is_file()
        assert (model_root / "data_types" / "mapping_sets" / "SyntheticTorqueMapping.yaml").is_file()
        kinds = {
            layer: call("read", address={"layer": layer, "element": "SyntheticTorque"}).structured["value"]["kind"]
            for layer in ("application_types", "implementation_types")
        }
        assert kinds == {"application_types": "application_type", "implementation_types": "value_type"}
        sides = call(
            "read", address={"layer": "interfaces", "element": "ISyntheticTorque", "path": [{"field": "data_elements"}]}
        ).structured["value"]
        # The member summary omits a reference that is not set rather than sending `null`.
        assert [(row["name"], row.get("application_type"), row.get("implementation_type")) for row in sides] == [
            ("physical", "SyntheticTorque", None),
            ("stored", None, "SyntheticTorque"),
        ]

    def test_a_retired_layout_refuses_a_change_set_and_moves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = fixtures.build_fixture(tmp_path / "project", self.RETIRED)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)
        before = fixtures.model_state_key(project / "model")

        # No `discover` answers a token on this model; the layout is refused before any comparison.
        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline="rev1:9f4c2a7be3d18056",
            operations=fixtures.VECTORS[self.PAIR]["input"]["operations"],
            intent="Declare the pair on a model kept in the retired layout.",
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == "consistency-violation"
        assert "addresses" not in structured, "a layout refusal names no element"
        assert "'data_types'" in structured["message"] and "'mapping_sets'" in structured["message"]
        assert str(project) not in structured["message"], "the refusal names model-root-relative directories only"
        assert human.questions == [], "a refused change set never reaches the review boundary"
        assert fixtures.model_state_key(project / "model") == before


class TestTheDataDescriptionElementsThroughTheWire:
    """The data description elements, committed and refused over `tools/call`.

    The elements are committed under a human's answer (one file per element in its layer's
    directory, each type reading back its references), and a removal that would leave two
    references naming nothing is refused before any question, with the model unchanged.

    What they do not reach: the generated ARXML of the elements, and the other element refusals
    (replayed by `test_protocol_vectors.py`).
    """

    UNIT_AND_LINEAR = "create-unit-linear-and-application-type"
    SHARED_TABLE = "create-texttable-enum-and-value-type"
    REMOVAL = "consistency-violation-remove-unit-elements-name"

    @staticmethod
    def _serve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
        server._PROJECT = None
        set_mode(monkeypatch, server.REVIEW_ELICIT)

    def test_the_elements_and_the_types_naming_them_commit_one_file_each(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = fixtures.build_fixture(tmp_path / "project", self.UNIT_AND_LINEAR)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True, True)

        for name in (self.UNIT_AND_LINEAR, self.SHARED_TABLE):
            vector = fixtures.VECTORS[name]
            structured, _texts = call_reviewed(
                human,
                "change_set",
                baseline=call("discover").structured["baseline"],
                operations=vector["input"]["operations"],
                intent=vector["input"]["intent"],
            )
            assert structured["status"] == "committed", (name, structured)

        assert len(human.questions) == 2, "each change set went through the review boundary"
        model_root = project / "model" / "data_types"
        for relative in (
            "units/ProbeNewtonMetre.yaml",
            "compu_methods/ProbeTorqueScale.yaml",
            "compu_methods/ProbeGearTable.yaml",
            "constraints/ProbeTorqueLimits.yaml",
            "application/ProbeTorque.yaml",
            "application/ProbeGear.yaml",
            "implementation/ProbeGearRaw.yaml",
        ):
            assert (model_root / relative).is_file(), relative
        names = {
            layer: [row["name"] for row in call("read", address={"layer": layer}).structured["value"]]
            for layer in ("units", "compu_methods", "data_constraints")
        }
        assert names == {
            "units": ["ProbeNewtonMetre"],
            "compu_methods": ["ProbeGearTable", "ProbeTorqueScale"],
            "data_constraints": ["ProbeTorqueLimits"],
        }
        torque = call("read", address={"layer": "application_types", "element": "ProbeTorque"}).structured["value"]
        assert (torque["unit_ref"], torque["compu_method_ref"], torque["data_constr_ref"]) == (
            "ProbeNewtonMetre",
            "ProbeTorqueScale",
            "ProbeTorqueLimits",
        )
        raw = call("read", address={"layer": "implementation_types", "element": "ProbeGearRaw"}).structured["value"]
        assert raw["compu_method_ref"] == "ProbeGearTable"

    def test_a_removal_leaving_two_references_is_refused_and_moves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = fixtures.build_fixture(tmp_path / "project", self.REMOVAL)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)
        before = fixtures.model_state_key(project / "model")

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=fixtures.VECTORS[self.REMOVAL]["input"]["operations"],
            intent="Drop the unit two elements still name.",
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == "consistency-violation"
        referrers = [structured["addresses"][0]] + [entry["addresses"][0] for entry in structured["additional"]]
        assert [(address["layer"], address["element"], address["path"]) for address in referrers] == [
            ("application_types", "ProbeTorque", [{"field": "unit_ref"}]),
            ("compu_methods", "ProbeTorqueScale", [{"field": "unit"}]),
        ]
        assert str(project) not in json.dumps(structured), "the refusal names layers and elements only"
        assert human.questions == [], "a refused change set never reaches the review boundary"
        assert fixtures.model_state_key(project / "model") == before


class TestATableTypedInitThroughTheWire:
    """The application value form's protocol pair, committed and refused over `tools/call`.

    An `init` under an implementation value type that references a table is the name of one of
    the table's entries (`[constr_1225]`); a number there is refused even where it equals an
    entry's value. The name is committed under a human's answer and written by `validate` as a
    `TEXT-VALUE-SPECIFICATION`; the number is refused before any question, at the value type's
    layer, with the model unchanged.

    What they do not reach: an `init` under an enumeration or an application primitive with a
    unit, and a reference type over the table-typed value type.
    """

    ENTRY = "create-interface-init-by-table-entry"
    NUMBER = "consistency-violation-interface-init-number-under-table"

    @staticmethod
    def _serve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
        server._PROJECT = None
        set_mode(monkeypatch, server.REVIEW_ELICIT)

    def test_an_entrys_name_commits_and_is_written_as_a_text_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lxml import etree

        vector = fixtures.VECTORS[self.ENTRY]
        project = fixtures.build_fixture(tmp_path / "project", self.ENTRY)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=vector["input"]["operations"],
            intent=vector["input"]["intent"],
        )

        assert structured["status"] == "committed", structured
        assert human.questions, "the change set went through the review boundary"
        stored = call(
            "read",
            address={
                "layer": "interfaces",
                "element": "IProbeGearStored",
                "path": [{"field": "data_elements"}, {"member": "gear"}, {"field": "init"}],
            },
        ).structured["value"]
        assert stored == "ProbeDrive"

        verdict = call("validate").structured
        assert verdict["success"] is True, verdict["errors"]
        (arxml,) = sorted((project / "dist").glob("*.arxml"), key=lambda path: path.as_posix())
        namespace = {"a": "http://autosar.org/schema/r4.0"}
        root = etree.parse(str(arxml)).getroot()
        (constant,) = [
            node
            for node in root.iterfind(".//a:CONSTANT-SPECIFICATION", namespace)
            if node.findtext("a:SHORT-NAME", namespaces=namespace) == "IProbeGearStored_gear_Init"
        ]
        (value,) = constant.find("a:VALUE-SPEC", namespace)
        assert etree.QName(value).localname == "TEXT-VALUE-SPECIFICATION"
        assert value.findtext("a:VALUE", namespaces=namespace) == "ProbeDrive"

    def test_a_number_is_refused_at_the_value_types_layer_and_moves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[self.NUMBER]
        project = fixtures.build_fixture(tmp_path / "project", self.NUMBER)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)
        before = fixtures.model_state_key(project / "model")

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=vector["input"]["operations"],
            intent=vector["input"]["intent"],
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == "consistency-violation"
        assert [(address["layer"], address["element"]) for address in structured["addresses"]] == [
            ("interfaces", "IProbeGearStored"),
            ("implementation_types", "ProbeGearRaw"),
        ]
        assert "names no entry of the table 'ProbeGearTable'" in structured["message"]
        assert str(project) not in json.dumps(structured), "the refusal names layers and elements only"
        assert human.questions == [], "a refused change set never reaches the review boundary"
        assert fixtures.model_state_key(project / "model") == before


class TestStatedNamesAndStubMembersThroughTheWire:
    """Stated names and stub members, committed and refused over `tools/call`.

    A component's `behavior_name`, an event's `name` and a call's `name` are written as the
    element's `SHORT-NAME`; an external interface stub lists the operations its ports use, so a
    call through it is referenced as the stub's path plus the name. The names and the member path
    are read back from the ARXML `validate` writes; the two negatives are refused before any
    question, with the model unchanged.

    What they do not reach: a name the import states, a sender-receiver stub's derived com-specs,
    and a build over the installed surface.
    """

    NAMES = "update-swc-stated-behaviour-and-event-names"
    STUB = "create-interface-stub-listing-operations-with-a-call"
    DUPLICATE = "consistency-violation-duplicate-event-name"
    WRONG_KIND = "schema-violation-external-interface-member-of-wrong-kind"
    NAMESPACE = {"a": "http://autosar.org/schema/r4.0"}

    @staticmethod
    def _serve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
        server._PROJECT = None
        set_mode(monkeypatch, server.REVIEW_ELICIT)

    def _commit_and_validate(self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Commit *name*'s change set under a human's yes, validate, and return the ARXML's root."""
        from lxml import etree

        vector = fixtures.VECTORS[name]
        project = fixtures.build_fixture(tmp_path / "project", name)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=vector["input"]["operations"],
            intent=vector["input"]["intent"],
        )

        assert structured["status"] == "committed", structured
        assert human.questions, "the change set went through the review boundary"
        verdict = call("validate").structured
        assert verdict["success"] is True, verdict["errors"]
        (arxml,) = sorted((project / "dist").glob("*.arxml"), key=lambda path: path.as_posix())
        return etree.parse(str(arxml)).getroot()

    def _short_names(self, root: Any, tag: str) -> list[str]:
        return [
            node.findtext("a:SHORT-NAME", namespaces=self.NAMESPACE)
            for node in root.iterfind(f".//a:{tag}", self.NAMESPACE)
        ]

    def test_the_stated_names_commit_and_are_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._commit_and_validate(self.NAMES, tmp_path, monkeypatch)

        stated = call(
            "read", address={"layer": "swc", "element": "ExampleSWC", "path": [{"field": "behavior_name"}]}
        ).structured["value"]
        assert stated == "ExampleBehaviour"
        assert "ExampleBehaviour" in self._short_names(root, "SWC-INTERNAL-BEHAVIOR")
        assert "ExampleSWC_InternalBehavior" not in self._short_names(root, "SWC-INTERNAL-BEHAVIOR")
        assert self._short_names(root, "INIT-EVENT") == ["ExampleStartup"]
        assert "TMT_ExampleRun" in self._short_names(root, "TIMING-EVENT")

    def test_a_call_through_a_listing_stub_commits_and_is_referenced_under_its_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._commit_and_validate(self.STUB, tmp_path, monkeypatch)

        listed = call(
            "read", address={"layer": "interfaces", "element": "IForeignCalibration", "path": [{"field": "operations"}]}
        ).structured["value"]
        assert listed == ["ReadOffset"]
        (point,) = root.iterfind(".//a:SYNCHRONOUS-SERVER-CALL-POINT", self.NAMESPACE)
        assert point.findtext("a:SHORT-NAME", namespaces=self.NAMESPACE) == "ReadForeignOffset"
        member = "/Foreign/PortInterfaces/IForeignCalibration/ReadOffset"
        assert point.findtext(".//a:TARGET-REQUIRED-OPERATION-REF", namespaces=self.NAMESPACE) == member
        (client,) = root.iterfind(".//a:CLIENT-COM-SPEC", self.NAMESPACE)
        assert client.findtext("a:OPERATION-REF", namespaces=self.NAMESPACE) == member

    @pytest.mark.parametrize(
        ("name", "code", "addressed"),
        [
            (DUPLICATE, "consistency-violation", [("swc", "ExampleSWC"), ("swc", "ExampleSWC")]),
            (WRONG_KIND, "schema-violation", [("interfaces", "IForeignSpeed")]),
        ],
    )
    def test_the_two_refusals_reach_no_question_and_move_nothing(
        self, name: str, code: str, addressed: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[name]
        project = fixtures.build_fixture(tmp_path / "project", name)
        self._serve(project, monkeypatch)
        human = ScriptedHuman(True)
        before = fixtures.model_state_key(project / "model")

        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            operations=vector["input"]["operations"],
            intent=vector["input"]["intent"],
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == code
        assert [(address["layer"], address["element"]) for address in structured["addresses"]] == addressed
        assert str(project) not in json.dumps(structured), "the refusal names layers and elements only"
        assert human.questions == [], "a refused change set never reaches the review boundary"
        assert fixtures.model_state_key(project / "model") == before


class TestADuplicateLinkIsRefusedBeforeTheReview:
    """``trace.duplicate-link`` over the wire, on ``satisfies`` and one ``derived_from`` case.

    ``ExampleSWC`` satisfies ``REQ_EXAMPLE_001@1``. A second member naming the same requirement
    (at another version, or the same member again: the list is a multiset) is refused by pass D
    with ``consistency-violation`` at the component; the human is never asked and nothing moves.
    Removing the old member in the same set (a link replacement) is no duplicate.

    What it does not reach: the other ``satisfies`` fields, and the CLI half.
    """

    LINK = "REQ_EXAMPLE_001@1"

    @staticmethod
    def _member(value: str) -> dict[str, Any]:
        return {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "satisfies"}, {"member": value}]}

    def _send(
        self, operations: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[dict[str, Any], ScriptedHuman, Path, str]:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True)
        before = state(project)
        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=call("discover").structured["baseline"],
            intent="Link ExampleSWC to REQ_EXAMPLE_001 once more.",
            operations=operations,
        )
        return structured, human, project, before

    @pytest.mark.parametrize("second", ["REQ_EXAMPLE_001@2", "REQ_EXAMPLE_001", LINK], ids=["@2", "bare", "same"])
    def test_a_second_member_of_the_requirement_is_refused_and_moves_nothing(
        self, second: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        structured, human, project, before = self._send(
            [{"operation": "create", "address": self._member(second), "value": second}], tmp_path, monkeypatch
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == "consistency-violation", structured
        assert [(address["layer"], address["element"]) for address in structured["addresses"]] == [
            ("swc", "ExampleSWC")
        ]
        assert "REQ_EXAMPLE_001" in structured["message"]
        assert human.questions == [], "a refused change set never reaches the review boundary"
        assert state(project) == before

    def test_a_requirement_deriving_from_one_parent_twice_is_refused_and_moves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule on ``derived_from``: a new requirement naming its parent at a version and bare."""
        requirement = {
            "id": "REQ_PROBE",
            "title": "Probe",
            "description": "A requirement this test creates.",
            "version": 1,
            "source": "architecture",
            "derived_from": [self.LINK, "REQ_EXAMPLE_001"],
        }
        structured, human, project, before = self._send(
            [
                {
                    "operation": "create",
                    "address": {"layer": "requirements", "element": "REQ_PROBE"},
                    "value": requirement,
                }
            ],
            tmp_path,
            monkeypatch,
        )

        assert structured["response"] == "error", structured
        assert structured["error"] == "consistency-violation", structured
        assert [(address["layer"], address["element"]) for address in structured["addresses"]] == [
            ("requirements", "REQ_PROBE")
        ]
        assert human.questions == []
        assert state(project) == before

    def test_a_replacement_in_one_set_is_no_duplicate_and_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        structured, human, project, before = self._send(
            [
                {"operation": "remove", "address": self._member(self.LINK)},
                {"operation": "create", "address": self._member("REQ_EXAMPLE_001@2"), "value": "REQ_EXAMPLE_001@2"},
            ],
            tmp_path,
            monkeypatch,
        )

        assert structured["status"] == "committed", structured
        assert human.questions, "the change set went through the review boundary"
        assert state(project) != before


class TestTheServerWithNoManifestRefusesEveryOperation:
    """With `SWCRAFT_MANIFEST` unset, every operation is a transport error naming the variable.

    Not a protocol code: the failure is the server's configuration, and an `errorResult` would
    tell a client to fix a correct request. The message names the variable because the person who
    can fix it reads a client's error line.
    """

    @pytest.mark.parametrize(
        ("tool", "named"),
        [
            ("discover", {}),
            ("read", {"address": {"layer": "swc"}}),
            (
                "change_set",
                {"baseline": "rev1:whatever", "operations": [{"operation": "remove", "address": {"layer": "swc"}}]},
            ),
        ],
    )
    def test_the_refusal_names_the_variable(
        self, tool: str, named: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SWCRAFT_MANIFEST")
        server._PROJECT = None

        with pytest.raises(ToolError, match="SWCRAFT_MANIFEST"):
            call(tool, **named)


#: FastMCP 3.4.7's whole tool error for an exception a tool did not mean to raise,
#: under `mask_error_details=True` (`FastMCP.call_tool`'s masking branch).
def masked(tool: str) -> str:
    return f"Error calling tool {tool!r}"


#: A POSIX path of two or more segments after a delimiter, or a drive letter.
ABSOLUTE_PATH = re.compile(r"(?:^|[\s'\"(\[=])(?:/[^\s/'\"]+){2,}|[A-Za-z]:[\\/]")


def wire_result(name: str, named: dict[str, Any], *, human: ScriptedHuman | None = None) -> CallToolResult:
    """The raw ``tools/call`` result, error flag included, rather than a raised ``ToolError``.

    ``protocol_client.call`` raises on a tool error, hiding the ``isError`` flag and the blocks.
    """

    async def go() -> CallToolResult:
        async with Client(server.mcp, elicitation_handler=human) as client:
            return await client.call_tool(name, named, raise_on_error=False)

    return asyncio.run(go())


def client_texts(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if getattr(block, "type", None) == "text"]


def strings_of(value: Any) -> Iterator[str]:
    """Every string in a decoded body, mapping keys included, depth first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)


def assert_no_storage_location(text: str, project: Path, *more: str) -> None:
    """Neither the project, the model root, a staging copy nor its parent is named in *text*."""
    locations = (str(project), str(project.resolve()), *more, *(str(Path(each).parent) for each in more))
    for location in locations:
        assert location not in text
    assert "swcraft-session-" not in text


@pytest.fixture
def server_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """What the server writes to its log, tracebacks included.

    The `fastmcp` logger does not propagate to the root logger, so the handler is attached to it.
    """
    logger = logging.getLogger("fastmcp")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


class ActingHuman(ScriptedHuman):
    """A scripted human who does one thing to the real project before the first answer."""

    def __init__(self, action: Callable[[], None], *answers: bool) -> None:
        super().__init__(*answers)
        self._action: Callable[[], None] | None = action

    async def __call__(self, message: str, response_type: Any, params: Any, context: Any) -> Any:
        if self._action is not None:
            self._action()
            self._action = None
        return await super().__call__(message, response_type, params, context)


class TestUnexpectedExceptionsAreMasked:
    """FastMCP's `mask_error_details`, proven in both directions over the wire.

    Masking reaches one channel: an exception a tool lets out that is not a `ToolError`. Every
    protocol refusal and host note is a tool result, and the one deliberate refusal (an unset
    `SWCRAFT_MANIFEST`) is a `ToolError`, which FastMCP re-raises before its masking branch.
    """

    def test_the_server_is_constructed_with_masking(self) -> None:
        assert server.mcp._mask_error_details is True

    def test_an_unexpected_exception_is_the_generic_line_to_the_client_and_whole_in_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_log: pytest.LogCaptureFixture
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        sentinel = "an unexpected fault nobody turned into a refusal"

        def _explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(sentinel)

        monkeypatch.setattr(server, "handle_request", _explode)
        result = wire_result("discover", {})

        assert result.is_error
        assert result.structured_content is None
        assert client_texts(result) == [masked("discover")]
        assert sentinel in server_log.text
        assert_no_storage_location(server_log.text, project)

    def test_the_deliberate_tool_error_reaches_the_client_whole(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SWCRAFT_MANIFEST")
        server._PROJECT = None

        result = wire_result("discover", {})

        assert result.is_error
        (text,) = client_texts(result)
        assert text.startswith("SWCRAFT_MANIFEST environment variable is not set. ")
        assert "export SWCRAFT_MANIFEST=/path/to/my-project/swcraft.yaml" in text

    def test_a_protocol_refusal_is_a_result_the_masking_never_sees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_project(PLAIN, tmp_path, monkeypatch)

        result = wire_result("discover", {"form": "index"})

        assert not result.is_error
        body = body_of(result)
        assert body["error"] == "schema-violation"
        assert client_texts(result)[0] == compact_json(body)


class TestTheSessionFaultsDiscloseNoPath:
    """The four session messages, each driven through the live client.

    Two reach the client by design and are held whole: the commit-time `stale-baseline` refusal
    and the tripwire's note, whose storage-relative file names are its content. The other two
    propagate, so the client gets the masked line and the server log, which names no path either,
    gets the message.
    """

    def test_a_commit_time_conflict_refusal_names_no_model_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        def _touch() -> None:
            # Between the staleness check at staging and the commit's own: the review.
            (project / "model" / "note.txt").write_text("a note\n", encoding="utf-8", newline="\n")

        result = wire_result("change_set", arguments(PLAIN, project), human=ActingHuman(_touch, True, True))

        assert not result.is_error
        body = body_of(result)
        message = body["message"]
        assert body["error"] == "stale-baseline"
        assert "every file under the model root counts" in message
        assert_no_storage_location("\n".join(client_texts(result)), project)
        # A path is JSON-escaped in the text block (a Windows separator doubles), so the decoded
        # body is searched too.
        assert_no_storage_location("\n".join(strings_of(body)), project)
        assert not ABSOLUTE_PATH.search(message), message
        assert not model_element_path(project / "model", "VehicleSpeed_kph", kind="value_type").exists()

    def test_a_vanished_model_root_is_masked_and_the_log_names_no_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_log: pytest.LogCaptureFixture
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        def _vanish() -> None:
            (project / "model").rename(project / "model-gone")

        result = wire_result("change_set", arguments(PLAIN, project), human=ActingHuman(_vanish, True, True))

        assert result.is_error
        assert client_texts(result) == [masked("change_set")]
        assert "the model root was not found or is not a directory" in server_log.text
        assert_no_storage_location(server_log.text, project)
        assert fixtures.model_state_key(project / "model-gone") == before

    def test_an_unrebaseable_audit_entry_is_masked_and_the_log_names_no_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_log: pytest.LogCaptureFixture
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)
        original = session_mod._rebase_audit_entries
        planted: list[str] = []

        def _with_a_stray_entry(entries: list[Any], real_model_location: Any) -> Any:
            # A tampered shadow log: an absolute location written past the AuditLog.
            stray = {key: "x" for key in AuditLog.ENTRY_KEYS}
            stray["result"] = str(project / "outside.yaml")
            planted.append(stray["result"])
            return original([*entries, stray], real_model_location)

        monkeypatch.setattr(session_mod, "_rebase_audit_entries", _with_a_stray_entry)
        result = wire_result("change_set", arguments(PLAIN, project), human=ScriptedHuman(True, True))

        assert planted, "the commit never rebased the shadow log, so nothing was measured"
        assert result.is_error
        assert client_texts(result) == [masked("change_set")]
        assert "outside the session's model root" in server_log.text
        assert_no_storage_location(server_log.text, project, *planted)
        assert state(project) == before

    def test_the_tripwire_note_reaches_the_client_whole_and_names_storage_files_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)
        original = session_mod._require_explained
        raised: list[UnexplainedChangeError] = []

        def _recording(*args: Any, **kwargs: Any) -> None:
            try:
                original(*args, **kwargs)
            except UnexplainedChangeError as failure:
                raised.append(failure)
                raise

        # The defect the tripwire is the net for: a reviewed diff that accounts for no file.
        monkeypatch.setattr(session_mod, "_touched_identities", lambda _diff, registry=None: {})
        monkeypatch.setattr(session_mod, "_require_explained", _recording)
        result = wire_result("change_set", arguments(PLAIN, project), human=ScriptedHuman(True, True))

        assert not result.is_error
        assert body_of(result)["status"] == "abandoned"
        (failure,) = raised
        texts = client_texts(result)
        # The note, byte for byte what the engine handed the host: masking touched nothing.
        assert texts[1:] == [f"the commit's byte-level tripwire refused it: {failure}"]
        names = str(failure).split("nobody saw: ", 1)[1].removesuffix(". Nothing was applied.").split(", ")
        assert names == ["data_types/implementation/VehicleSpeed_kph.yaml"]
        for name in names:
            # A storage file name: relative, and its first component is a directory INSIDE the model root.
            parts = PurePosixPath(name).parts
            assert not PurePosixPath(name).is_absolute() and ".." not in parts
            assert (project / "model" / parts[0]).is_dir(), name
        assert_no_storage_location(texts[1], project)
        assert not ABSOLUTE_PATH.search(texts[1]), texts[1]
        assert state(project) == before


class TestAnEngineFaultDisclosesNoStagingPath:
    """An engine fault discloses no staging path or storage location.

    A fault out of `handle_request` has no protocol code. The engine and the validator
    disagreeing about a staged change set (`UndiagnosedInconsistency`) once sent the client the
    temporary staging directory in the exception text; the client now gets FastMCP's generic line,
    and the diagnostic (rule, layer, element) is the server log's alone, still path-free.

    The disagreement cannot be built from a model, so only the validator pass D calls is
    replaced. What this does not reach: the engine's other faults, each with its own message.
    """

    def test_the_client_gets_the_generic_error_and_only_the_log_names_the_element(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_log: pytest.LogCaptureFixture
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        # A human who would say yes to everything: pass D refuses before the review.
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)
        before = state(project)
        staged_at: list[str] = []

        def _refuse(model_root: str, _arxml_version: str | None = None) -> ConsistencyResult:
            staged_at.append(model_root)
            return ConsistencyResult(
                consistent=False,
                issues=["a rule nobody assigned a code to"],
                records=(
                    ConsistencyIssue(
                        rule="check-nobody-mapped",
                        message="a rule nobody assigned a code to",
                        subjects=(Subject(layer="swc", element="ExampleSWC"),),
                    ),
                ),
            )

        monkeypatch.setattr(passes_mod, "check_cross_layer_consistency", _refuse)
        result = wire_result("change_set", arguments(PLAIN, project), human=human)

        assert staged_at, "pass D never ran, so nothing was measured"
        # The client: the MCP error flag, no structured half, and FastMCP's generic line alone.
        assert result.is_error
        assert result.structured_content is None
        assert client_texts(result) == [masked("change_set")]
        # Not applied: nobody was asked, no record claims a commit, the tree is byte-identical.
        assert human.questions == []
        assert not {CHANGE_SET_COMMIT_INTENT, CHANGE_SET_COMMITTED} & set(audit_operations(project))
        assert state(project) == before
        # The log: the whole diagnostic, and still no path.
        log = server_log.text
        assert "engine/validator disagreement" in log
        assert "check-nobody-mapped at 'ExampleSWC' in the swc layer" in log
        assert_no_storage_location(log, project, *staged_at)


class TestTheRetiredRequirementShapeIsRefusedOverTheWire:
    """A requirement's retired ``links`` and ``status``: the protocol refuses both shapes.

    A payload still carrying ``links`` is refused by ``_bind_payload`` as ``unknown-field``, naming
    the field; a requirement file on disk in the old shape makes the layer unreadable, a
    ``consistency-violation`` naming the element. The retired-key sentence itself is the YAML
    readers' and the CLI's; the protocol answers with its own codes and never a storage path.
    """

    def test_a_create_carrying_links_is_an_unknown_field(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)
        named["operations"] = [
            {
                "address": {"layer": "requirements", "element": "REQ_OLD"},
                "operation": "create",
                "value": {
                    "id": "REQ_OLD",
                    "title": "Old shape",
                    "description": "Carries the retired key.",
                    "version": 1,
                    "source": "architecture",
                    "links": ["ExampleSWC"],
                },
            }
        ]

        structured, texts = call("change_set", **named)

        assert (structured["response"], structured["error"]) == ("error", "unknown-field")
        assert structured["addresses"] == [
            {"layer": "requirements", "element": "REQ_OLD", "path": [{"field": "links"}]}
        ]
        assert str(project) not in "".join(texts)
        assert not (project / "model" / "requirements" / "REQ_OLD.yaml").exists()

    def test_a_requirement_file_still_carrying_links_makes_the_layer_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        requirement = project / "model" / "requirements" / "REQ_EXAMPLE_001.yaml"
        requirement.write_text(
            requirement.read_text(encoding="utf-8") + "links:\n- ExampleSWC\n", encoding="utf-8", newline="\n"
        )

        structured, texts = call("change_set", **arguments(PLAIN, project))

        assert (structured["response"], structured["error"]) == ("error", "consistency-violation")
        assert structured["addresses"] == [{"layer": "requirements", "element": "REQ_EXAMPLE_001"}]
        assert str(project) not in "".join(texts)
        assert ".yaml" not in structured["message"]
