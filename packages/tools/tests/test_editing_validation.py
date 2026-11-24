"""Pass D over the materialized staged result.

1. **The split holds.** ``validator.check_cross_layer_consistency`` decides pass/fail and
   ``editing/diagnosis.py`` decides where, from the staged snapshot. No rule reads a validator
   message: codes are contract, messages are not.
2. **The addresses are the referrers.** A ``remove`` of a still-referenced interface names the
   ports that point at it, never the removed element: that is what the client must change.
3. **Nothing is waived.** A change set is accepted when the whole staged result is valid; a
   pre-existing violation is never waived, and may be repaired by the same change set.
4. **Validation runs over the assembled result.** A target and its referrer created together are
   accepted in either order; clearing the referrers and removing the target is accepted as a whole.
5. **A disagreement is loud.** When the validator refuses and no rule explains it, the engine
   raises.
6. **The engine never refuses a name ``check-model`` accepts.** The generated forms are
   kind-scoped, read from ``arxml_generation.RESERVED_DATA_TYPE_RULES``;
   ``TestKindScopeMatchesCheckModel`` asserts both directions over one 117-character name.
7. **The rule's layer scope is the contract's**: a projection of ``layerDecl.arxmlBound``, equal to
   the one ``validator._identifier_layer_tokens`` projects for ``check-model``. Section 3b holds it
   four ways (the pinned pre-migration set, that equality, a measurement of the layers the walk
   reaches, an AST scan).
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.diagnosis as diagnosis_mod
import swcraft_tools.editing.passes as passes_mod
import swcraft_tools.editing.registry as reg
import swcraft_tools.validator as validator_mod
import validator_package
import yaml
from conftest import model_element_path, model_element_storage_name, write_model_element
from filesystem_support import case_sensitive_under
from model_contract_fixtures import (
    ARXML_BOUND_LAYER_NAMES,
    ARXML_BOUND_LAYER_ORDER,
    ARXML_BOUND_LAYERS_ADDED_SINCE,
    merged_binding,
    merged_contract,
)
from swcraft_tools.diagnostics import Subject
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import (
    UndiagnosedInconsistency,
    diagnose,
    emitted_identifier_violations,
)
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.passes import pass_d
from swcraft_tools.editing.protocol import Violation
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.identifier import REASON_PATTERN
from swcraft_tools.model_registry import (
    ModelRegistry,
    build_model_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.reader import find_swc_file
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.validator import (
    ConsistencyIssue,
    ConsistencyResult,
    check_cross_layer_consistency,
    check_identifier_conformance,
)
from swcraft_tools.validator.identifiers import _identifier_layer_tokens

# Fixtures and helpers

#: The 117-character name of ``negative/invalid-identifier-emitted-compu-method-overflow.json``:
#: legal as declared and as a value type (which generates no form), but not as an enum, whose
#: ``{name}_CompuMethod`` has 129 characters. The kind is what decides.
OVERFLOWING_NAME = (
    "VehicleSpeedSignalScalingFactorForHighResolutionWheelSpeedSensorFrontLeftAxle"
    "MeasurementCalibrationValueRawUnfiltered"
)

#: The five validator rules that answer "a type reference names no declared data type". They share
#: one ``RULE_CODES`` group, so a missing type is one violation naming every referring field.
_DATA_TYPE_REFERENCE_RULES = (
    "interface.data-element-undeclared-type",
    "interface.argument-undeclared-type",
    "data-type.value-type-undeclared-base",
    "data-type.reference-type-undeclared-base",
    "data-type.record-field-undeclared-type",
    "data-type.array-undeclared-element-type",
)

#: The three rules `check_sequence_consistency`' steps can break, all of which a
#: sequence whose own `task` does not resolve must leave untouched.
_SEQUENCE_STEP_RULES = (
    "sequence.step-not-mapped",
    "sequence.step-unknown-swc",
    "sequence.step-unknown-runnable",
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real, valid project on disk — the vector harness's base model, unseeded."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


@pytest.fixture
def hardware_absent(tmp_path: Path) -> Path:
    """The same project with its mandatory ``hardware/`` layer missing (``hardware.layer-absent``).

    The one model in which the change set that declares the first core can also be refused for the
    tasks it does not cover.
    """
    return fixtures.build_fixture(tmp_path / "project", "discover-layer-hardware-absent")


def _staged(project_root: Path) -> StagedModel:
    """The project's model as the staged tree the materialization path works on."""
    return StagedModel.from_snapshot(load_snapshot(str(project_root / "model")))


def _diagnose(project_root: Path) -> tuple[Violation, ...]:
    """Every violation pass D would report for the model on disk."""
    return diagnose(check_cross_layer_consistency(str(project_root / "model")))


def _set_satisfies(project_root: Path, component: str, members: list[str]) -> None:
    """Write *members* as *component*'s own ``satisfies``, dropping it when empty."""
    path = Path(find_swc_file(str(project_root / "model"), component))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.pop("satisfies", None)
    if members:
        document["satisfies"] = members
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _add_satisfies(project_root: Path, component: str, member: str) -> None:
    path = Path(find_swc_file(str(project_root / "model"), component))
    current = yaml.safe_load(path.read_text(encoding="utf-8")).get("satisfies", [])
    _set_satisfies(project_root, component, [*current, member])


def _rule_violations(project_root: Path, *rules: str) -> tuple[Violation, ...]:
    """Only the violations *rules* produce, translated exactly as pass D would.

    Filtering the validator's records by rule id keeps each case about the rule under test rather
    than everything else the broken fixture also trips.
    """
    result = check_cross_layer_consistency(str(project_root / "model"))
    records = tuple(record for record in result.records if record.rule in rules)
    return diagnose(ConsistencyResult(consistent=not records, issues=[], records=records))


def _dispatch(project_root: Path, *operations: dict[str, Any]) -> dict[str, Any]:
    """One change set through the whole engine, against a freshly read baseline."""
    return handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project_root / "model"),
            "intent": "a test change set",
            "operations": list(operations),
        },
        project=project_root,
    )


def _assert_accepted(project_root: Path, *operations: dict[str, Any]) -> None:
    """Assert the ``staged`` status: "no error" is also true of a response that never reached validation."""
    response = _dispatch(project_root, *operations)
    assert response["response"] == "change_set", response
    assert response["status"] == "staged", response


def _enum(name: str) -> dict[str, Any]:
    """The vector's payload: an enum, whose ``{name}_CompuMethod`` is emitted."""
    return {
        "name": name,
        "kind": "enum",
        "literals": [{"name": "Uncalibrated", "value": 0}, {"name": "Calibrated", "value": 1}],
        "description": "a calibration state",
    }


def _alias(name: str) -> dict[str, Any]:
    """The same declared name as a value type, which generates no form at all."""
    return {
        "name": name,
        "kind": "value_type",
        "base": "float32_base",
        "unit": "kph",
        "value_min": 0.0,
        "value_max": 300.0,
        "description": "a scaling factor",
    }


#: The application kinds (the ``application_types`` layer).
_APPLICATION_KINDS = frozenset({"application_type", "enum", "record", "array"})


def _create_data_type(value: dict[str, Any]) -> dict[str, Any]:
    layer = "application_types" if value["kind"] in _APPLICATION_KINDS else "implementation_types"
    return {
        "operation": "create",
        "address": {"layer": layer, "element": value["name"]},
        "value": value,
    }


_REMOVE_IEXAMPLE = {"operation": "remove", "address": {"layer": "interfaces", "element": "IExample"}}
#: A second composition declaring `root: true` beside `examples/basic`'s `Root`, which breaks
#: `composition.multiple-roots`.
_SECOND_ROOT = {"name": "Root2", "title": "A second root", "root": True}
_PORT_ADDRESSES = [
    {
        "layer": "swc",
        "element": "ExampleSWC",
        "path": [{"field": "ports"}, {"member": "pOut"}, {"field": "interface"}],
    },
    {
        "layer": "swc",
        "element": "ExampleConsumerSWC",
        "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "interface"}],
    },
]


def _addresses(violation: Any) -> list[dict[str, Any]]:
    return [address.to_json() for address in violation.addresses]


def _tree_state(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path — byte-identity, not a digest."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


# 1. The split: the validator decides pass/fail, diagnosis decides where


class TestTheSplitHolds:
    def test_a_valid_staged_result_yields_no_violation(self, project: Path) -> None:
        assert pass_d(str(project / "model")) == ()

    def test_a_valid_result_never_consults_the_validator_owned_rules(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A consistent result has no violation to locate, so the walk must not run.
        def _explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("diagnosis ran over a result the validator called consistent")

        monkeypatch.setattr(passes_mod, "diagnose", _explode)
        assert pass_d(str(project / "model")) == ()

    def test_no_rule_reads_a_validator_message(self) -> None:
        # Codes are contract, messages are not: matching validator text would pin every sentence
        # `validator/` emits.
        tree = ast.parse(Path(diagnosis_mod.__file__).read_text(encoding="utf-8"))
        imported = {
            name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for name in node.names
            if node.module
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert "re" not in imported, "a code and an address come from the rule id and the subjects, never from prose"

    def test_only_one_function_touches_a_validator_message_at_all(self) -> None:
        # A message is carried into the response by exactly one function, which only concatenates;
        # anything else reading `.message` would be a branch on prose.
        tree = ast.parse(Path(diagnosis_mod.__file__).read_text(encoding="utf-8"))
        readers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for child in ast.walk(node)
            if isinstance(child, ast.Attribute) and child.attr == "message"
        }
        assert readers == {"_violation_message"}, f"{sorted(readers)} read a validator message"

    def test_the_validator_owns_pass_fail_for_its_own_rules(self, project: Path) -> None:
        # The rule says nothing until the interface is really gone.
        assert _rule_violations(project, "port.undeclared-interface") == ()
        (project / "model" / "interfaces" / "IExample.yaml").unlink()
        assert len(_rule_violations(project, "port.undeclared-interface")) == 1


# 2. The port -> interface rule


class TestPortInterfaceReferences:
    def test_the_addresses_are_the_referrers_not_the_removed_element(self, project: Path) -> None:
        response = _dispatch(project, _REMOVE_IEXAMPLE)
        assert response["error"] == "broken-reference"
        assert response["failure_class"] == "invalid"
        assert sorted(map(repr, response["addresses"])) == sorted(map(repr, _PORT_ADDRESSES))
        assert "additional" not in response

    def test_every_referrer_of_one_target_is_one_violation(self, project: Path) -> None:
        # A client fixing one dangling name fixes every referrer of it together.
        (project / "model" / "interfaces" / "IExample.yaml").unlink()
        violations = _rule_violations(project, "port.undeclared-interface")
        assert len(violations) == 1
        assert len(violations[0].addresses) == 2

    def test_two_missing_targets_are_two_violations(self, project: Path) -> None:
        (project / "model" / "interfaces" / "IExample.yaml").unlink()
        (project / "model" / "swc" / "example" / "ExampleSWC.yaml").write_text(
            "name: ExampleSWC\ntype: application\nports:\n"
            "  - name: pOut\n    direction: provide\n    interface: IGone\n",
            encoding="utf-8",
        )
        violations = _rule_violations(project, "port.undeclared-interface")
        assert {address.element for violation in violations for address in violation.addresses} == {
            "ExampleSWC",
            "ExampleConsumerSWC",
        }
        assert len(violations) == 2

    def test_the_real_model_is_byte_identical_after_a_refusal(self, project: Path) -> None:
        model_root = project / "model"
        before = _tree_state(model_root)
        assert _dispatch(project, _REMOVE_IEXAMPLE)["response"] == "error"
        assert _tree_state(model_root) == before


# 3. The emitted-identifier rule


class TestEmittedIdentifiers:
    def test_a_legal_declared_name_with_an_illegal_emitted_form_is_refused(self, project: Path) -> None:
        response = _dispatch(project, _create_data_type(_enum(OVERFLOWING_NAME)))
        assert response["error"] == "invalid-identifier"
        assert response["addresses"] == [{"layer": "application_types", "element": OVERFLOWING_NAME}]

    def test_the_same_declared_name_as_an_alias_is_accepted(self, project: Path) -> None:
        # `_CompuMethod` is emitted for an enum only, so the identical name as an alias is accepted.
        _assert_accepted(project, _create_data_type(_alias(OVERFLOWING_NAME)))

    def test_a_conforming_model_has_nothing_to_report(self, project: Path) -> None:
        assert emitted_identifier_violations(_staged(project)) == ()

    def test_a_declared_member_name_is_addressed_at_the_member(self, project: Path) -> None:
        (project / "model" / "interfaces" / "IExample.yaml").write_text(
            "name: IExample\nkind: sender_receiver\ndata_elements:\n"
            "  - name: my value\n    implementation_type: float32\n",
            encoding="utf-8",
        )
        violations = emitted_identifier_violations(_staged(project))
        assert len(violations) == 1
        assert _addresses(violations[0]) == [
            {
                "layer": "interfaces",
                "element": "IExample",
                "path": [{"field": "data_elements"}, {"member": "my value"}, {"field": "name"}],
            }
        ]

    def test_one_bad_declared_name_is_reported_once_not_once_per_generated_form(self, project: Path) -> None:
        # One illegal declared name makes every generated form illegal; one report per root cause.
        (model_element_path(project / "model", "float32", kind="base_type")).write_text(
            "name: float 32\nkind: base_type\nsize: 32\nencoding: IEEE754\n", encoding="utf-8"
        )
        violations = emitted_identifier_violations(_staged(project))
        assert [violation.addresses[0].element for violation in violations] == ["float 32"]

    def test_the_rule_covers_the_whole_staged_result_and_not_only_what_moved(self, project: Path) -> None:
        # A name already illegal in the baseline is still a violation of the staged result.
        (project / "model" / "connectors" / "Example_to_Consumer.yaml").write_text(
            "name: Example to Consumer\nfrom:\n  swc: ExampleSWC\n  port: pOut\n"
            "to:\n  swc: ExampleConsumerSWC\n  port: rIn\n",
            encoding="utf-8",
        )
        violations = emitted_identifier_violations(_staged(project))
        assert [violation.error for violation in violations] == ["invalid-identifier"]

    def test_a_writer_refusal_during_materialization_becomes_the_same_code(self, project: Path) -> None:
        # `write_element`'s guards run before pass D, so the engine asks pass D's identifier rule the
        # same question rather than letting a `ValueError` escape a well-formed request.
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "implementation_types", "element": "My Type"},
                "value": {"name": "My Type", "kind": "value_type", "base": "float32_base"},
            },
        )
        assert response["error"] == "invalid-identifier"
        assert response["addresses"] == [{"layer": "implementation_types", "element": "My Type"}]

    def test_the_non_arxml_layers_are_out_of_scope(self, project: Path) -> None:
        # `Sequence.name` keeps its own LikeC4 rule and a requirement id its own shape; an AUTOSAR
        # constraint on either would be a rule the standard does not ask for.
        staged = _staged(project)
        staged.elements["tasks"][0]["name"] = "10 ms task"
        staged.elements["requirements"][0]["id"] = "REQ-EXAMPLE-001"
        assert emitted_identifier_violations(staged) == ()

    def test_the_init_constants_joint_budget_is_diagnosed_here_too(self, project: Path) -> None:
        """The init constant's joint name budget is diagnosed in validation, not at materialization.

        Its form is derived from two declared names; undiagnosed, `write_element`'s pair guard refuses during
        materialization and the `ValueError` escapes a well-formed change set.
        """
        interface_name, element_name = "I" * 62, "E" * 61
        staged = _staged(project)
        staged.elements["interfaces"].append(
            {
                "name": interface_name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element_name, "implementation_type": "float32"}],
            }
        )

        violations = emitted_identifier_violations(staged)

        assert len(violations) == 1, violations
        assert violations[0].error == "invalid-identifier"
        assert f"{interface_name}_{element_name}_Init" in violations[0].message
        assert _addresses(violations[0]) == [{"layer": "interfaces", "element": interface_name}]

    def test_an_over_budget_pair_answers_a_protocol_refusal_not_an_exception(self, project: Path) -> None:
        """End to end: the writer refuses the pair during materialization and the engine answers it."""
        interface_name, element_name = "I" * 62, "E" * 61
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "interfaces", "element": interface_name},
                "value": {
                    "name": interface_name,
                    "kind": "sender_receiver",
                    "data_elements": [{"name": element_name, "implementation_type": "float32"}],
                },
            },
        )

        assert response["error"] == "invalid-identifier", response
        assert response["addresses"] == [{"layer": "interfaces", "element": interface_name}]

    def test_a_pair_on_the_joint_budget_is_accepted(self, project: Path) -> None:
        """Non-vacuity: 122 between the two names is legal, 123 is not."""
        interface_name, element_name = "I" * 61, "E" * 61
        _assert_accepted(
            project,
            {
                "operation": "create",
                "address": {"layer": "interfaces", "element": interface_name},
                "value": {
                    "name": interface_name,
                    "kind": "sender_receiver",
                    "data_elements": [{"name": element_name, "implementation_type": "float32"}],
                },
            },
        )

    #: With ``IExample``'s five-character ``value``, overflows ``R_{port}_{element}``'s 125-character
    #: joint budget by one; legal on its own.
    OVER_BUDGET_PORT = "P" * 121
    #: The same name one character shorter — exactly on the budget.
    BUDGET_PORT = "P" * 120

    @staticmethod
    def _read_entry(port_name: str, element_name: str = "value") -> dict[str, Any]:
        """One ``reads`` member of ``ExampleConsumerRun``, created at its own address."""
        entry = {"port": port_name, "element": element_name}
        return {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [
                    {"field": "runnables"},
                    {"member": "ExampleConsumerRun"},
                    {"field": "reads"},
                    {"member": entry},
                ],
            },
            "value": entry,
        }

    @staticmethod
    def _require_port(port_name: str) -> dict[str, Any]:
        """One more require port on ``IExample``, created at its own address."""
        return {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [{"field": "ports"}, {"member": port_name}],
            },
            "value": {"name": port_name, "direction": "require", "interface": "IExample"},
        }

    @classmethod
    def _reading_consumer(cls, port_name: str) -> list[dict[str, Any]]:
        """The two operations that give ``ExampleConsumerSWC`` one access entry."""
        return [cls._require_port(port_name), cls._read_entry(port_name)]

    def test_the_access_points_joint_budget_is_diagnosed_here_too(self, project: Path) -> None:
        """The access-point forms' joint budget is diagnosed in validation too.

        They are pairs as well: ``write_element`` refuses an over-budget pair during materialization, before pass D, and
        `_materialization_refusal` re-asks this function.
        """
        staged = _staged(project)
        swc = next(element for element in staged.elements["swc"] if element["name"] == "ExampleConsumerSWC")
        swc["ports"].append({"name": self.OVER_BUDGET_PORT, "direction": "require", "interface": "IExample"})
        swc["runnables"][0]["reads"] = [{"port": self.OVER_BUDGET_PORT, "element": "value"}]

        violations = emitted_identifier_violations(staged)

        assert len(violations) == 1, violations
        assert violations[0].error == "invalid-identifier"
        assert f"R_{self.OVER_BUDGET_PORT}_value" in violations[0].message
        address = _addresses(violations[0])[0]
        assert address["layer"] == "swc" and address["element"] == "ExampleConsumerSWC"
        assert address["path"][-1] == {"member": {"port": self.OVER_BUDGET_PORT, "element": "value"}}

    def test_an_over_budget_access_pair_answers_a_protocol_refusal_not_an_exception(self, project: Path) -> None:
        """End to end: the writer refuses the pair during materialization and the engine answers it."""
        response = _dispatch(project, *self._reading_consumer(self.OVER_BUDGET_PORT))

        assert response["error"] == "invalid-identifier", response
        assert f"R_{self.OVER_BUDGET_PORT}_value" in response["message"], response

    def test_an_access_pair_on_the_budget_is_accepted(self, project: Path) -> None:
        """Non-vacuity: 125 between the two names is legal, 126 is not."""
        _assert_accepted(project, *self._reading_consumer(self.BUDGET_PORT))

    def test_two_entries_deriving_one_access_name_are_a_reserved_name(self, project: Path) -> None:
        """Two entries deriving one access name are answered with pass D's code.

        ``write_element`` refuses the underscore ambiguity as a ``ReservedNameError``: port ``a_b`` reading ``value``
        and port ``a`` reading ``b_value`` both derive ``R_a_b_value``.
        """
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {
                    "layer": "interfaces",
                    "element": "IExample",
                    "path": [{"field": "data_elements"}, {"member": "b_value"}],
                },
                "value": {"name": "b_value", "implementation_type": "float32"},
            },
            self._require_port("a_b"),
            self._require_port("a"),
            self._read_entry("a_b"),
            self._read_entry("a", "b_value"),
        )

        assert response["error"] == "consistency-violation", response
        assert "R_a_b_value" in response["message"], response

    def test_a_constant_colliding_with_an_interface_is_a_reserved_name(self, project: Path) -> None:
        """A collision half has no rule id: it is a ``ReservedNameError``, which the engine already translates."""
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "interfaces", "element": "IExample_value_Init"},
                "value": {
                    "name": "IExample_value_Init",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "other", "implementation_type": "float32"}],
                },
            },
        )

        assert response["error"] == "consistency-violation", response
        assert "IExample_value_Init" in response["message"], response


class TestKindScopeMatchesCheckModel:
    """One name, two kinds: the engine and ``check-model`` must agree on both.

    ``{x}_CompuMethod`` is emitted for an enum ``x`` only (``RESERVED_DATA_TYPE_RULES``, and
    ``check_identifier_conformance`` scopes it the same way), so a 117-character enum is illegal and the
    identical name as a value type, which generates no form, is legal. A guard that drops the kind
    scope still refuses the enum; only the accepting direction catches it (such a guard rejects a
    ``Pose_T_AppView`` beside a record ``Pose_T``). Both directions, against both authorities.
    """

    @staticmethod
    def _seed(root: Path, document: dict[str, Any]) -> Path:
        """A fresh project holding *document* as a declared data type."""
        project_root = fixtures.build_fixture(root, "read-swc-element")
        path = model_element_path(project_root / "model", document["name"], kind=document["kind"])
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return project_root

    @staticmethod
    def _engine_error(project_root: Path, document: dict[str, Any]) -> str | None:
        """The code the engine answers a ``create`` of *document* with, or ``None`` when it is staged."""
        return _dispatch(project_root, _create_data_type(document)).get("error")

    @pytest.mark.parametrize(
        ("build", "legal"),
        [(_enum, False), (_alias, True)],
        ids=["enum-129-character-CompuMethod", "value-type-no-generated-form"],
    )
    def test_check_model_and_the_engine_answer_the_same_way(self, tmp_path: Path, build: Any, legal: bool) -> None:
        document = build(OVERFLOWING_NAME)

        reported = self._seed(tmp_path / "check_model", document)
        assert check_identifier_conformance(str(reported / "model")).consistent is legal

        engine = fixtures.build_fixture(tmp_path / "engine", "read-swc-element")
        expected = None if legal else "invalid-identifier"
        assert self._engine_error(engine, document) == expected

    def test_no_kind_carries_a_generated_form_the_exporter_does_not_emit(self) -> None:
        # The writer's table against the engine's use of it: one row, every kind but `enum` absent.
        # The field-scoped forms are `_generated_form_suffixes`' business, asserted beside this.
        assert diagnosis_mod._GENERATED_DATA_TYPE_SUFFIXES == {"enum": ("_CompuMethod",)}

    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            ({"kind": "application_type"}, ()),
            ({"kind": "application_type", "unit": "  "}, ()),
            ({"kind": "application_type", "unit": "m"}, ("_Unit",)),
            ({"kind": "application_type", "conversion": {}}, ("_Compu",)),
            ({"kind": "application_type", "range": {"min": 0}}, ("_Range",)),
            (
                {"kind": "application_type", "unit": "m", "conversion": {}, "range": {"max": 1}},
                ("_Unit", "_Compu", "_Range"),
            ),
            ({"kind": "value_type", "unit": "m"}, ()),
            ({"kind": "value_type", "value_min": 0}, ("_Range",)),
            ({"kind": "value_type", "value_max": 1.5}, ("_Range",)),
            ({"kind": "value_type", "value_min": -1, "value_max": 1, "unit": "m"}, ("_Range",)),
            ({"kind": "value_type", "value_min": None}, ()),
            ({"kind": "reference_type", "value_min": 0}, ()),
        ],
        ids=repr,
    )
    def test_the_field_scoped_forms_are_the_writers_own_presence_rule(self, document, expected) -> None:
        """The field-scoped forms (the application type's three, a value type's ``_Range``) over the raw document.

        The engine sees a staged element as a ``Mapping``, so it restates the writer's presence predicate
        in the document's terms (the one restatement in ``diagnosis.py``); the matrix holds the two
        equal, including a blank ``unit`` and a bound key on a kind that declares none.
        """
        assert diagnosis_mod._generated_form_suffixes({"name": "X", **document}) == expected


# 3b. The scope of that rule is the contract's, not this module's.
#
# A scope silently widened or narrowed produces no finding on a valid model, so the scope itself
# is the oracle: pinned below as its pre-migration literal, compared with the derived one both ways,
# compared with the set `validator/` derives for `check-model`, and measured over a staged tree
# with one illegal name in every declared layer.


#: The four layers the walk covered as literal keys before it was derived from the contract, in
#: report order. A literal on purpose: recomputing it from the contract would compare the mechanism
#: with itself. Declared once, as ``ARXML_BOUND_LAYER_ORDER`` in ``model_contract_fixtures``.
PRE_MIGRATION_IDENTIFIER_LAYERS: tuple[str, ...] = ARXML_BOUND_LAYER_ORDER

#: Today's scope, in the order the derived walk reaches it: the pre-migration four plus every
#: ARXML-bound layer declared since. Not read off `layerDecl.arxmlBound`: an expectation derived from
#: the flag could not tell a layer that silently stopped being bound from one never meant to be.
#: Written in the contract's own layer order (the three type layers follow `connectors`); the
#: composition above is the SET it must equal.
IDENTIFIER_LAYERS: tuple[str, ...] = (
    "swc",
    "interfaces",
    "connectors",
    "application_types",
    "implementation_types",
    "base_types",
    "compositions",
    "mapping_sets",
    # Contract 8.0.0's elements, appended: the contract writes the three layers last.
    "units",
    "compu_methods",
    "data_constraints",
)
assert set(IDENTIFIER_LAYERS) == set(PRE_MIGRATION_IDENTIFIER_LAYERS + ARXML_BOUND_LAYERS_ADDED_SINCE)

#: An identity no layer's rule accepts, whatever the layer or the element kind.
BAD_IDENTIFIER = "bad name"


def _synthetic_registry(*, arxml_bound: bool) -> ModelRegistry:
    """The merged contract's synthetic layer, with its ``arxmlBound`` set either way."""
    contract = copy.deepcopy(merged_contract())
    layer = next(entry for entry in contract["layers"] if entry["name"] == "synthetic")
    layer["arxmlBound"] = arxml_bound
    return build_model_registry(contract=contract, binding=merged_binding())


def _bad_name_in_every_layer(registry: ModelRegistry) -> StagedModel:
    """A staged tree carrying one illegally named element in every declared layer.

    The reported set is the scope: a diagnosed layer answers with a violation, an undiagnosed one
    with nothing. Does not reach a member site (port, runnable, data element, composition member,
    delegation): those are ``test_validator_compositions.py``'s and the golden's below.
    """
    return StagedModel(
        registry=reg.current(registry),
        elements={layer.name: [{layer.identity_field: BAD_IDENTIFIER}] for layer in registry.layers},
    )


class TestTheIdentifierScopeIsTheContractsArxmlBoundLayers:
    def test_the_derived_scope_equals_the_pinned_pre_migration_one(self) -> None:
        """Both directions, and the order, because the order is the report order."""
        derived = tuple(layer.name for layer in diagnosis_mod._identifier_layers())
        assert derived == IDENTIFIER_LAYERS
        assert set(derived) == ARXML_BOUND_LAYER_NAMES

    def test_it_is_the_same_scope_check_model_derives(self) -> None:
        """The file-reading side (by layer token) and the staged side (by layer name) project one rule.

        A divergence would mean ``check-model`` and the pre-materialization path disagree about which
        names AUTOSAR constrains.
        """
        by_token = {layer.token: layer.name for layer in production_registry().layers}
        assert {by_token[token] for token in _identifier_layer_tokens()} == {
            layer.name for layer in diagnosis_mod._identifier_layers()
        }

    def test_the_layers_actually_diagnosed_are_exactly_the_derived_ones(self) -> None:
        """What the walk reaches, not what it declares.

        Declaring a layer ``arxmlBound`` can widen ``validator``'s file index and report nothing
        new, because the reader loops decide the diagnosis; so the reported set is measured.
        """
        registry = production_registry()
        violations = emitted_identifier_violations(_bad_name_in_every_layer(registry), registry=registry)
        reported = {address.layer for violation in violations for address in violation.addresses}
        assert reported == {layer.name for layer in diagnosis_mod._identifier_layers(registry)}
        assert reported == ARXML_BOUND_LAYER_NAMES

    def test_no_layer_name_is_spelled_out_beyond_its_own_specialization(self) -> None:
        """The AST half: a stale layer literal cannot survive anywhere else in the module.

        ``_MEMBERS_AND_GENERATED_FORMS`` is the one place a layer is named, and each entry may name only
        its own layer; everything else reaches a layer through ``_identifier_layers``.
        """
        tree = ast.parse(Path(diagnosis_mod.__file__).read_text(encoding="utf-8"))
        layers = {layer.name for layer in production_registry().layers}
        table = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_MEMBERS_AND_GENERATED_FORMS"
        )
        assert isinstance(table.value, ast.Dict)
        registered = {
            key.value: handler.id
            for key, handler in zip(table.value.keys, table.value.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(handler, ast.Name)
        }
        assert set(registered) == set(diagnosis_mod._MEMBERS_AND_GENERATED_FORMS), "the scan has gone stale"

        specialized = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in set(registered.values())
        }
        assert set(specialized) == set(registered.values()), "a registered handler is not a top-level function"

        for layer, function in registered.items():
            spelled = {
                child.value
                for child in ast.walk(specialized[function])
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in layers
            }
            assert spelled <= {layer}, f"{function} names a layer other than '{layer}'"

        scanned = {id(table), *(id(node) for node in specialized.values())}
        elsewhere = [
            (child.lineno, child.value)
            for node in tree.body
            if id(node) not in scanned
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in layers
        ]
        assert elsewhere == [], f"a layer name is spelled outside the specialization table: {elsewhere}"

    def test_every_specialized_layer_is_one_the_contract_declares_bound(self) -> None:
        """A handler for an unbound layer would be code the walk can never reach."""
        assert set(diagnosis_mod._MEMBERS_AND_GENERATED_FORMS) <= {
            layer.name for layer in diagnosis_mod._identifier_layers()
        }

    def test_a_bound_layer_with_no_specialization_is_still_diagnosed(self) -> None:
        """``connectors`` is the shipped proof of the generic arm an added layer takes.

        The arm reads the layer's ``identity_field`` and reports under its ``token``, so a layer with no
        element-shape code still has its declared name checked: the walk is total.
        """
        assert "connectors" not in diagnosis_mod._MEMBERS_AND_GENERATED_FORMS
        registry = production_registry()
        violations = emitted_identifier_violations(_bad_name_in_every_layer(registry), registry=registry)
        assert [violation.message for violation in violations if violation.addresses[0].layer == "connectors"] == [
            f"the connector name '{BAD_IDENTIFIER}' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}"
        ]

    def test_the_registry_argument_is_resolved_in_the_body(self) -> None:
        """Not as a default expression, which would bind the shipped contract forever."""
        registry = _synthetic_registry(arxml_bound=True)
        with use_registry(registry):
            assert "synthetic" in {layer.name for layer in diagnosis_mod._identifier_layers()}
        assert "synthetic" not in {layer.name for layer in diagnosis_mod._identifier_layers()}

    def test_the_reported_violations_are_unchanged_for_the_four_pre_migration_layers(self, project: Path) -> None:
        """One illegal name in each pre-migration ARXML-bound layer, against a captured golden.

        The messages and addresses were captured before the migration and are never regenerated from
        the code they grade; three come from the ``swc`` branch, so the golden also pins that the
        ``TMT_`` form of an illegal runnable name is reported once.

        Does not reach ``compositions`` (ARXML-bound, but not in the pre-migration golden), whose four name sites
        are pinned in ``test_validator_compositions.py::TestTheWritePathHalfOfTheIdentifierScope``.
        """
        (project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml").write_text(
            "name: My SWC\ntype: application\nports:\n  - name: my port\n    direction: require\n"
            "    interface: IExample\nrunnables:\n  - name: my runnable\n    events:\n"
            "      - type: timing\n        period: 0.01\n",
            encoding="utf-8",
        )
        (project / "model" / "interfaces" / "IExample.yaml").write_text(
            "name: IExample\nkind: sender_receiver\ndata_elements:\n"
            "  - name: my value\n    implementation_type: float32\n",
            encoding="utf-8",
        )
        (project / "model" / "connectors" / "Example_to_Consumer.yaml").write_text(
            "name: Example to Consumer\nfrom:\n  swc: ExampleSWC\n  port: pOut\n"
            "to:\n  swc: ExampleConsumerSWC\n  port: rIn\n",
            encoding="utf-8",
        )
        # The fourth pre-migration layer is `implementation_types`; its illegal name is written as
        # the value type `float32` is (a base type is now another layer's element).
        (model_element_path(project / "model", "float32", kind="value_type")).write_text(
            "name: float 32\nkind: value_type\nbase: float32_base\n", encoding="utf-8"
        )

        violations = emitted_identifier_violations(_staged(project))
        assert [violation.error for violation in violations] == ["invalid-identifier"] * 6
        assert [violation.message for violation in violations] == [
            f"the SWC name 'My SWC' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}",
            f"the SWC 'My SWC' port name 'my port' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}",
            f"the SWC 'My SWC' runnable name 'my runnable' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}",
            "the interface 'IExample' data element name 'my value' is emitted as an "
            f"AUTOSAR SHORT-NAME and {REASON_PATTERN}",
            f"the connector name 'Example to Consumer' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}",
            f"the data type name 'float 32' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}",
        ]
        assert [_addresses(violation) for violation in violations] == [
            [{"layer": "swc", "element": "My SWC"}],
            [
                {
                    "layer": "swc",
                    "element": "My SWC",
                    "path": [{"field": "ports"}, {"member": "my port"}, {"field": "name"}],
                }
            ],
            [
                {
                    "layer": "swc",
                    "element": "My SWC",
                    "path": [{"field": "runnables"}, {"member": "my runnable"}, {"field": "name"}],
                }
            ],
            [
                {
                    "layer": "interfaces",
                    "element": "IExample",
                    "path": [{"field": "data_elements"}, {"member": "my value"}, {"field": "name"}],
                }
            ],
            [{"layer": "connectors", "element": "Example to Consumer"}],
            [{"layer": "implementation_types", "element": "float 32"}],
        ]


class TestTheSyntheticLayerIsDiagnosedIffItIsArxmlBound:
    """The synthetic layer, through the rule a total consumer owes every layer.

    ``packages/tools/tests/fixtures/contracts/model-contract/fixtures/synthetic-layer.json`` declares
    ``arxmlBound: false``, the negative case as shipped; the positive case flips that one declaration.
    Both directions, because a walk that answers "nothing" whatever the contract says passes the
    negative one.
    """

    @pytest.mark.parametrize("arxml_bound", [False, True], ids=["not-bound", "bound"])
    def test_the_synthetic_layer_is_diagnosed_exactly_when_it_is_bound(self, arxml_bound: bool) -> None:
        registry = _synthetic_registry(arxml_bound=arxml_bound)
        with use_registry(registry):
            staged = StagedModel(
                registry=reg.current(),
                elements={"synthetic": [{"name": BAD_IDENTIFIER, "title": "One", "composition": "S"}]},
            )
            violations = emitted_identifier_violations(staged)
        expected = (
            [f"the synthetic name '{BAD_IDENTIFIER}' is emitted as an AUTOSAR SHORT-NAME and {REASON_PATTERN}"]
            if arxml_bound
            else []
        )
        assert [violation.message for violation in violations] == expected
        assert [address.layer for violation in violations for address in violation.addresses] == (
            ["synthetic"] if arxml_bound else []
        )

    def test_the_shipped_fixture_declares_the_layer_unbound(self) -> None:
        """The negative case above is the fixture as shipped, not a perturbation of it."""
        layer = next(entry for entry in merged_contract()["layers"] if entry["name"] == "synthetic")
        assert layer["arxmlBound"] is False


# 4. The task-partition rule


class TestTaskPartitionResolution:
    """``hardware.undeclared-partition`` is decided through the engine.

    An unconditional reference rule over a mandatory layer, keyed to the partition a task names.
    """

    @staticmethod
    def _second_task(project_root: Path, partition: str) -> None:
        (project_root / "model" / "tasks" / "Task_20ms.yaml").write_text(
            f"name: Task_20ms\nperiod: 0.02\npriority: 4\npartition: {partition}\nwcet: 900us\nrunnables: []\n",
            encoding="utf-8",
        )

    _CREATE_CPU = {
        "operation": "create",
        "address": {"layer": "hardware", "element": "CPU"},
        "value": {"name": "CPU", "kind": "processor", "description": "The example processor"},
    }
    _CREATE_CORE0 = {
        "operation": "create",
        "address": {"layer": "hardware", "element": "CORE0"},
        "value": {
            "name": "CORE0",
            "kind": "core",
            "threshold": 0.7,
            "enforcement": "strict",
            "description": "Application core 0",
        },
    }
    #: Both base components budget ``RAM0`` and the memory rule is unconditional, so the memory is
    #: created in the same change set, with a processor so ``hardware.no-processor`` does not answer.
    _CREATE_RAM0 = {
        "operation": "create",
        "address": {"layer": "hardware", "element": "RAM0"},
        "value": {
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0"],
            "threshold": 0.8,
            "enforcement": "strict",
            "description": "256 KiB application RAM pool",
        },
    }

    #: The partition the base model's task names, on ``CORE0``.
    _CREATE_PARTITION0 = {
        "operation": "create",
        "address": {"layer": "hardware", "element": "PARTITION0"},
        "value": {"name": "PARTITION0", "kind": "partition", "core": "CORE0"},
    }

    def _whole_layer(self):
        return (self._CREATE_CPU, self._CREATE_CORE0, self._CREATE_RAM0, self._CREATE_PARTITION0)

    def test_an_absent_layer_is_reported_as_absent_and_not_as_an_unresolved_partition(
        self, hardware_absent: Path
    ) -> None:
        """An absent ``hardware/`` is itself the finding, and no unresolved partition is reported.

        The layer rule short-circuits the reference rules: with no inventory the per-element rules have nothing to
        resolve against.
        """
        self._second_task(hardware_absent, partition="PARTITION1")
        result = check_cross_layer_consistency(str(hardware_absent / "model"))

        assert [record.rule for record in result.records] == ["hardware.layer-absent"]
        assert _rule_violations(hardware_absent, "hardware.undeclared-partition") == ()

    def test_declaring_the_layer_turns_the_reference_rule_on(self, hardware_absent: Path) -> None:
        # The operation that supplies the inventory is what can be refused, and
        # the addresses are the referrers it does not cover.
        self._second_task(hardware_absent, partition="PARTITION1")
        response = _dispatch(hardware_absent, *self._whole_layer())
        assert response["error"] == "broken-reference"
        assert response["failure_class"] == "invalid"
        assert response["addresses"] == [
            {"layer": "tasks", "element": "Task_20ms", "path": [{"field": "partition"}]},
            {"layer": "hardware"},
        ]

    def test_a_covered_model_is_accepted(self, hardware_absent: Path) -> None:
        self._second_task(hardware_absent, partition="PARTITION0")
        _assert_accepted(hardware_absent, *self._whole_layer())

    def test_an_advisory_core_no_longer_downgrades_the_rule(self, hardware_absent: Path) -> None:
        """An advisory ``enforcement`` never downgrades it: an unresolved reference is about the model, not a budget."""
        self._second_task(hardware_absent, partition="PARTITION1")
        advisory = {**self._CREATE_CORE0, "value": {**self._CREATE_CORE0["value"], "enforcement": "advisory"}}

        response = _dispatch(hardware_absent, self._CREATE_CPU, advisory, self._CREATE_RAM0, self._CREATE_PARTITION0)

        assert response["error"] == "broken-reference"
        assert response["addresses"][0] == {
            "layer": "tasks",
            "element": "Task_20ms",
            "path": [{"field": "partition"}],
        }


# 4b. The remaining reference rules


class TestTheRemainingReferenceRules:
    """The reference rules' short-circuits, invisible from outside the engine.

    ``test_editing_invariants.py`` asserts each through the whole engine; here each rule reproduces the
    short-circuits of the validator check it re-derives, because reporting more than the validator
    would refuse a change set ``check-model`` accepts.
    """

    @staticmethod
    def _write(project_root: Path, relative: str, text: str) -> None:
        path = project_root / "model" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_conforming_model_has_nothing_to_report(self, project: Path) -> None:
        assert _diagnose(project) == ()

    def test_every_referrer_of_one_missing_type_is_one_violation(self, project: Path) -> None:
        # A removed type is one repair however many kinds of field still name it: the data-type
        # reference rules share one group.
        (project / "model" / "data_types" / "implementation" / "uint16.yaml").unlink()
        violations = _rule_violations(project, *_DATA_TYPE_REFERENCE_RULES)
        assert len(violations) == 1
        assert len(violations[0].addresses) == 2

    def test_the_type_rule_is_kind_scoped(self, project: Path) -> None:
        # A kind-blind walk would read one element's field off another's kind.
        self._write(
            project,
            model_element_storage_name("ProbeRecord", kind="record"),
            "name: ProbeRecord\nkind: record\nfields:\n  - name: a\n    type: NoSuchType\n",
        )
        self._write(
            project,
            model_element_storage_name("ProbeAlias", kind="value_type"),
            "name: ProbeAlias\nkind: value_type\nbase: AlsoMissing\n",
        )
        addressed = {
            (address.element, tuple(str(step.to_json()) for step in address.path))
            for violation in _rule_violations(project, *_DATA_TYPE_REFERENCE_RULES)
            for address in violation.addresses
        }
        assert addressed == {
            ("ProbeRecord", ("{'field': 'fields'}", "{'member': 'a'}", "{'field': 'type'}")),
            ("ProbeAlias", ("{'field': 'base'}",)),
        }

    def test_an_endpoint_port_is_checked_only_once_its_swc_resolves(self, project: Path) -> None:
        # `_resolve_port` raises on the SWC first, so one endpoint never carries both findings.
        self._write(
            project,
            "connectors/Example_to_Consumer.yaml",
            "name: Example_to_Consumer\nfrom:\n  swc: NoSuchSWC\n  port: alsoMissing\n"
            "to:\n  swc: ExampleConsumerSWC\n  port: rIn\n",
        )
        violations = _rule_violations(project, "connector.endpoint-unknown-swc", "connector.endpoint-unknown-port")
        assert len(violations) == 1
        assert [address.to_json()["path"] for address in violations[0].addresses] == [
            [{"field": "from"}, {"field": "swc"}]
        ]

    def test_a_sequence_whose_task_is_missing_reports_no_step(self, project: Path) -> None:
        # `check_sequence_consistency` `continue`s past checks 3 to 5 when the
        # task does not resolve, so the step rule must stay silent there.
        self._write(
            project,
            "sequences/task10ms_consumer_cycle.yaml",
            "name: task10ms_consumer_cycle\ntask: NoSuchTask\ntitle: t\n"
            "steps:\n  - swc: NoSuchSWC\n    runnable: NoSuchRun\n",
        )
        assert len(_rule_violations(project, "sequence.unknown-task")) == 1
        assert _rule_violations(project, *_SEQUENCE_STEP_RULES) == ()

    def test_an_absent_hardware_layer_reports_itself_and_not_the_memory_rule(self, hardware_absent: Path) -> None:
        # With the layer absent, `hardware.layer-absent` is the whole answer.
        assert _rule_violations(hardware_absent, "hardware.undeclared-memory") == ()

    def test_an_advisory_memory_never_downgrades_the_memory_rule(self, project: Path) -> None:
        # A reference rule is about the model, not a budget: `enforcement` has no say on either kind.
        self._write(
            project,
            "hardware/RAM0.yaml",
            "name: RAM0\nkind: memory\ntype: RAM\nsize: 262144\naccessible: [CORE0]\n"
            "threshold: 0.8\nenforcement: advisory\n",
        )
        self._write(
            project,
            "hardware/CORE0.yaml",
            "name: CORE0\nkind: core\nthreshold: 0.7\nenforcement: advisory\n",
        )
        self._write(
            project,
            "swc/example/ExampleSWC.yaml",
            "name: ExampleSWC\ntype: application\nports:\n  - name: pOut\n    direction: provide\n"
            "    interface: IExample\nmemory:\n  NoSuchMemory: 2048\n",
        )
        violations = _rule_violations(project, "hardware.undeclared-memory")
        assert len(violations) == 1
        assert violations[0].addresses[0].to_json() == {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [{"field": "memory"}, {"key": "NoSuchMemory"}],
        }

    def test_a_requirement_link_is_addressed_at_the_link_not_the_component(self, project: Path) -> None:
        # The link lives in the element, so the dangling side is the component's `satisfies` member.
        _add_satisfies(project, "ExampleSWC", "NO_SUCH_REQ@1")
        violations = _rule_violations(project, "trace.link-unknown-requirement")
        assert len(violations) == 1
        assert violations[0].error == "broken-reference"
        assert violations[0].addresses[0].to_json() == {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [{"field": "satisfies"}, {"member": "NO_SUCH_REQ@1"}],
        }

    def test_a_task_mapping_runnable_is_checked_only_once_its_swc_resolves(self, project: Path) -> None:
        self._write(
            project,
            "tasks/Task_10ms.yaml",
            "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION0\nwcet: 1200us\n"
            "runnables:\n  - swc: NoSuchSWC\n    runnable: NoSuchRun\n",
        )
        violations = _rule_violations(project, "task.mapping-unknown-swc", "task.mapping-unknown-runnable")
        assert len(violations) == 1
        assert violations[0].addresses[0].to_json()["path"][-1] == {"field": "swc"}


# 5. Nothing is waived, and the staged-result semantics


class TestNothingIsWaived:
    """The baseline may be invalid; the *result* is what is judged."""

    @staticmethod
    def _break_the_baseline(project_root: Path) -> None:
        """Delete the interface two ports still name — an invalid starting point."""
        (project_root / "model" / "interfaces" / "IExample.yaml").unlink()

    _RESTORE_IEXAMPLE = {
        "operation": "create",
        "address": {"layer": "interfaces", "element": "IExample"},
        "value": {
            "name": "IExample",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        },
    }

    def test_a_pre_existing_violation_is_repairable_by_the_same_change_set(self, project: Path) -> None:
        self._break_the_baseline(project)
        _assert_accepted(project, self._RESTORE_IEXAMPLE)

    def test_a_pre_existing_violation_is_never_waived(self, project: Path) -> None:
        # The change set touches only a task and is still refused: subtracting the baseline's own
        # violations would accept an outcome `check-model` calls invalid.
        self._break_the_baseline(project)
        response = _dispatch(
            project,
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                "value": 6,
            },
        )
        assert response["error"] == "broken-reference"
        assert sorted(map(repr, response["addresses"])) == sorted(map(repr, _PORT_ADDRESSES))

    def test_an_invalid_baseline_alone_is_not_an_engine_error(self, project: Path) -> None:
        # A staged result still invalid for a reason a rule owns is a refusal, never a raise.
        self._break_the_baseline(project)
        assert len(_diagnose(project)) == 1


class TestStagedResultSemantics:
    """Validation runs over the assembled result."""

    _CREATE_INTERFACE = {
        "operation": "create",
        "address": {"layer": "interfaces", "element": "INew"},
        "value": {
            "name": "INew",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "float32"}],
        },
    }
    _CREATE_PORT = {
        "operation": "create",
        "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pNew"}]},
        "value": {"name": "pNew", "direction": "provide", "interface": "INew", "description": "a new port"},
    }

    def test_a_target_and_its_referrer_are_accepted_target_first(self, project: Path) -> None:
        _assert_accepted(project, self._CREATE_INTERFACE, self._CREATE_PORT)

    def test_a_target_and_its_referrer_are_accepted_referrer_first(self, project: Path) -> None:
        # The order of the operations is not pass D's question; the assembled result is.
        _assert_accepted(project, self._CREATE_PORT, self._CREATE_INTERFACE)

    def test_clearing_the_referrers_and_removing_the_target_is_accepted_as_a_whole(self, project: Path) -> None:
        _assert_accepted(
            project,
            {"operation": "remove", "address": {"layer": "connectors", "element": "Example_to_Consumer"}},
            {
                "operation": "remove",
                "address": {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [{"field": "ports"}, {"member": "pOut"}],
                },
            },
            {
                "operation": "remove",
                "address": {
                    "layer": "swc",
                    "element": "ExampleConsumerSWC",
                    "path": [{"field": "ports"}, {"member": "rIn"}],
                },
            },
            _REMOVE_IEXAMPLE,
        )


class TestAnInitValueIsDecidedOnTheWireToo:
    """`check_init_conformance`'s rules through the ENGINE, not through a dict lookup.

    ``RULE_CODES`` maps all eight ``interface.init-*`` rules to ``consistency-violation`` and the check
    is in pass D's composed tuple, so a non-conforming value is refused and a conforming one staged.
    The second case's addresses include a three-step path into a member's field (``data_elements`` ->
    ``p`` -> ``init``), which nothing else in the suite walks for this rule family.
    """

    # A record's members are the physical view and a record field's type resolves in
    # `application_types`, so the coordinates are an enumeration, whose member init is its literal's name.
    _CREATE_COORD_T = {
        "operation": "create",
        "address": {"layer": "application_types", "element": "Coord_T"},
        "value": {
            "name": "Coord_T",
            "kind": "enum",
            "literals": [{"name": "LOW", "value": 0}, {"name": "HIGH", "value": 1}],
        },
    }

    _CREATE_POINT_T = {
        "operation": "create",
        "address": {"layer": "application_types", "element": "Point_T"},
        "value": {
            "name": "Point_T",
            "kind": "record",
            "fields": [{"name": "x", "type": "Coord_T"}, {"name": "y", "type": "Coord_T"}],
        },
    }

    @staticmethod
    def _create_interface(init: Any) -> dict[str, Any]:
        return {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IPoint"},
            "value": {
                "name": "IPoint",
                "kind": "sender_receiver",
                "data_elements": [{"name": "p", "application_type": "Point_T", "init": init}],
            },
        }

    def test_a_conforming_value_is_staged_and_survives_into_the_delta(self, project: Path) -> None:
        response = _dispatch(
            project, self._CREATE_COORD_T, self._CREATE_POINT_T, self._create_interface({"x": "LOW", "y": "HIGH"})
        )

        assert response["status"] == "staged", response
        interface = next(entry for entry in response["delta"] if entry["address"]["element"] == "IPoint")
        assert interface["after"]["data_elements"][0]["init"] == {"x": "LOW", "y": "HIGH"}

    def test_a_non_conforming_value_is_refused_as_a_consistency_violation(self, project: Path) -> None:
        response = _dispatch(project, self._CREATE_COORD_T, self._CREATE_POINT_T, self._create_interface({"x": "LOW"}))

        assert response["error"] == "consistency-violation", response
        assert response["failure_class"] == "invalid"

    def test_the_refusal_addresses_the_value_and_the_type_that_refused_it(self, project: Path) -> None:
        """Both subjects, so a client is told where to write AND what to read.

        `consistency-violation`, not `broken-reference`: the check abstains where the type does not resolve.
        """
        response = _dispatch(project, self._CREATE_COORD_T, self._CREATE_POINT_T, self._create_interface({"x": "LOW"}))

        assert response["addresses"] == [
            {
                "layer": "interfaces",
                "element": "IPoint",
                "path": [{"field": "data_elements"}, {"member": "p"}, {"field": "init"}],
            },
            {"layer": "application_types", "element": "Point_T", "path": [{"field": "fields"}]},
        ]


class TestAnUnwritableInitValueIsRefusedRatherThanRaised:
    """The one ``interface.init-*`` rule the lifecycle cannot reach through pass D.

    The value that trips ``interface.init-unwritable-integer`` is an ``int`` no ``str()`` can spell, so
    materialization's ``yaml.safe_dump`` would raise ``ValueError`` out of ``handle_request`` on a
    change set touching only a ``description``. The writer's guard refuses it as a
    ``ReservedNameError`` with the rule's code. The value is authored as hexadecimal (the only way one
    reaches a project, ``swcraft_tools.rendering``) and written by hand. Both declared types are
    covered: ``check_init_conformance`` reports the rule on two paths, so a guard dispatching on the
    type would have to agree with both.
    """

    #: 5 000 hexadecimal digits, above the 4 300 decimal digits a default interpreter converts.
    UNWRITABLE_HEX = "f" * 5000

    _ENUM_MODE_T = "name: Mode_T\nkind: enum\nliterals:\n- name: 'OFF'\n  value: 0\n- name: 'ON'\n  value: 1\n"

    _TOUCH_THE_DESCRIPTION = {
        "operation": "update",
        "address": {"layer": "interfaces", "element": "IExample", "path": [{"field": "description"}]},
        "value": "a description this change set is the whole of",
    }

    def _seed(self, project_root: Path, type_name: str) -> None:
        (project_root / "model" / "interfaces" / "IExample.yaml").write_text(
            "name: IExample\nkind: sender_receiver\ndata_elements:\n"
            f"  - name: value\n    implementation_type: {type_name}\n    init: 0x{self.UNWRITABLE_HEX}\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_a_primitive_typed_value_answers_a_protocol_refusal(self, project: Path) -> None:
        self._seed(project, "float32")

        response = _dispatch(project, self._TOUCH_THE_DESCRIPTION)

        assert response["error"] == "consistency-violation", response
        assert response["failure_class"] == "invalid"
        assert "interface.init-unwritable-integer" in response["message"], response
        assert response["addresses"] == [{"layer": "interfaces", "element": "IExample"}]

    def test_an_enum_typed_value_answers_the_same_refusal(self, project: Path) -> None:
        (model_element_path(project / "model", "Mode_T", kind="enum")).write_text(
            self._ENUM_MODE_T, encoding="utf-8", newline="\n"
        )
        self._seed(project, "Mode_T")

        response = _dispatch(project, self._TOUCH_THE_DESCRIPTION)

        assert response["error"] == "consistency-violation", response
        assert "interface.init-unwritable-integer" in response["message"], response

    def test_the_untouched_model_is_left_exactly_as_it_was(self, project: Path) -> None:
        """A refusal during materialization leaves the untouched interface byte-identical."""
        self._seed(project, "float32")
        before = _tree_state(project / "model")

        _dispatch(project, self._TOUCH_THE_DESCRIPTION)

        assert _tree_state(project / "model") == before


class TestTheSafetyRuleIsDecidedOnTheWireToo:
    """`safety.component-below-requirements` through the ENGINE.

    ``RULE_CODES`` maps the rule to ``consistency-violation`` and ``check_component_asil`` is in pass D's
    tuple, so a component declared below its allocated requirements is refused, not approvable. Both
    directions (lowering the component, raising a requirement) reach the same violation with the same
    two addresses, the two legal repairs; the rule chooses neither.

    Does not reach the rule's own cases (``test_validator_safety.py``): here, only what the wire carries.
    """

    _REQUIREMENT = Path("model") / "requirements" / "REQ_EXAMPLE_001.yaml"

    @staticmethod
    def _component(project_root: Path) -> Path:
        """``ExampleSWC``'s file, found rather than spelled (``swc`` is stored by its ``group``)."""
        return next((project_root / "model" / "swc").rglob("ExampleSWC.yaml"))

    @staticmethod
    def _declare(path: Path, level: str) -> None:
        """Append ``asil: <level>`` to an element file that declares none.

        A text append, so the baseline differs from the result only in the field under test.
        """
        path.write_text(path.read_text(encoding="utf-8") + f"asil: {level}\n", encoding="utf-8", newline="\n")

    @staticmethod
    def _update_asil(layer: str, element: str, value: str) -> dict[str, Any]:
        return {
            "operation": "update",
            "address": {"layer": layer, "element": element, "path": [{"field": "asil"}]},
            "value": value,
        }

    _BOTH_ADDRESSES = [
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "asil"}]},
        {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"field": "asil"}]},
    ]

    def test_declaring_the_level_allocated_to_the_component_is_staged(self, project: Path) -> None:
        """Declaring the level allocated to the component is staged.

        Equal is legitimate (``[constr_6201]`` admits equal and higher), and ``ExampleConsumerSWC``, declaring nothing,
        derives the level rather than being reported.
        """
        _assert_accepted(
            project,
            self._update_asil("requirements", "REQ_EXAMPLE_001", "D"),
            self._update_asil("swc", "ExampleSWC", "D"),
        )

    def test_lowering_a_component_under_its_requirements_is_refused(self, project: Path) -> None:
        self._declare(project / self._REQUIREMENT, "D")

        response = _dispatch(project, self._update_asil("swc", "ExampleSWC", "B"))

        assert response["error"] == "consistency-violation", response
        assert response["failure_class"] == "invalid"
        assert "safety.component-below-requirements" not in response["message"]
        assert "declares ASIL B, below the ASIL D" in response["message"], response

    def test_the_refusal_addresses_the_component_and_every_requirement_above_it(self, project: Path) -> None:
        self._declare(project / self._REQUIREMENT, "D")

        response = _dispatch(project, self._update_asil("swc", "ExampleSWC", "B"))

        assert response["addresses"] == self._BOTH_ADDRESSES

    def test_raising_the_requirement_over_the_component_is_refused_identically(self, project: Path) -> None:
        """The change set touches the requirement; the refusal names the component first all the same."""
        self._declare(self._component(project), "B")

        response = _dispatch(project, self._update_asil("requirements", "REQ_EXAMPLE_001", "D"))

        assert response["error"] == "consistency-violation", response
        assert response["addresses"] == self._BOTH_ADDRESSES

    def test_a_declared_qm_under_an_allocated_requirement_is_refused_too(self, project: Path) -> None:
        """``QM`` is a declaration, not an absence: the same change set writing no level is staged."""
        self._declare(project / self._REQUIREMENT, "D")

        response = _dispatch(project, self._update_asil("swc", "ExampleSWC", "QM"))

        assert response["error"] == "consistency-violation", response
        assert response["addresses"] == self._BOTH_ADDRESSES

    def test_the_undeclared_safety_level_field_is_still_an_unknown_field(self, project: Path) -> None:
        """`negative/unknown-field-swc-safety-level.json` holds: ``safety_level`` is undeclared, ``asil`` declared."""
        declared = {name for kind in reg.REGISTRY.kind_names() for name in reg.REGISTRY.all_fields(kind)}
        assert "safety_level" not in declared
        assert "asil" in declared

        response = _dispatch(
            project,
            {
                "operation": "update",
                "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "safety_level"}]},
                "value": "ASIL_B",
            },
        )

        assert response["error"] == "unknown-field", response


# 6. Reporting, and the loud failure


class TestReporting:
    def test_every_violation_the_pass_found_is_in_one_round_trip(self, project: Path) -> None:
        # The illegal name is pre-existing: `write_element` refuses an illegal created name during
        # materialization, before pass D, so a `create` could never carry a second violation.
        (project / "model" / "connectors" / "Example_to_Consumer.yaml").write_text(
            "name: Example to Consumer\nfrom:\n  swc: ExampleSWC\n  port: pOut\n"
            "to:\n  swc: ExampleConsumerSWC\n  port: rIn\n",
            encoding="utf-8",
        )
        response = _dispatch(project, _REMOVE_IEXAMPLE)
        assert response["error"] == "invalid-identifier"
        assert [detail["error"] for detail in response["additional"]] == ["broken-reference"]
        assert sorted(map(repr, response["additional"][0]["addresses"])) == sorted(map(repr, _PORT_ADDRESSES))

    def test_an_unallocated_requirement_is_a_warning_pass_d_never_sees(self, project: Path) -> None:
        # Both components' links removed leave one warning, `trace.requirement-unallocated`; pass D
        # reads the error half alone and answers nothing. Deleting the requirement is the refusal below.
        for component in ("ExampleSWC", "ExampleConsumerSWC"):
            _set_satisfies(project, component, [])
        result = validator_mod.check_cross_layer_consistency(str(project / "model"))
        assert result.consistent is True
        assert result.records == ()
        assert [(record.rule, [subject.element for subject in record.subjects]) for record in result.warnings] == [
            ("trace.requirement-unallocated", ["REQ_EXAMPLE_001"])
        ]
        assert pass_d(str(project / "model")) == ()

    def test_deleting_the_linked_requirement_alone_is_a_broken_reference(self, project: Path) -> None:
        # The requirement removed with the `satisfies` members left in place: one `broken-reference`,
        # addressed at both members.
        (project / "model" / "requirements" / "REQ_EXAMPLE_001.yaml").unlink()
        violations = pass_d(str(project / "model"))
        assert [violation.error for violation in violations] == ["broken-reference"]
        member = [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}]
        assert sorted(repr(address.to_json()) for address in violations[0].addresses) == sorted(
            repr({"layer": "swc", "element": component, "path": member})
            for component in ("ExampleSWC", "ExampleConsumerSWC")
        )

    def test_a_second_root_is_answered_with_the_tenth_code(self, project: Path) -> None:
        # A second `root: true` composition breaks `composition.multiple-roots`, a cardinality over
        # the assembled layer, answered at both compositions (the rule chooses neither).
        write_model_element(project / "model", _SECOND_ROOT, layer="compositions")
        violations = pass_d(str(project / "model"))
        assert [violation.error for violation in violations] == ["consistency-violation"]
        assert sorted(address.element for violation in violations for address in violation.addresses) == [
            "Root",
            "Root2",
        ]

    def test_the_second_root_change_set_is_refused_through_the_engine(self, project: Path) -> None:
        # The same exchange as a change set over the `examples/basic` base, with the model untouched.
        before = _tree_state(project / "model")
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "compositions", "element": "Root2"},
                "value": _SECOND_ROOT,
            },
        )
        assert response["error"] == "consistency-violation"
        assert response["failure_class"] == "invalid"
        assert sorted(address["element"] for address in response["addresses"]) == ["Root", "Root2"]
        assert _tree_state(project / "model") == before

    def test_a_genuine_disagreement_is_raised_not_answered(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The validator refused with a rule the protocol does not name. `RULE_CODES` completeness is a
        # test, so this is an engine defect; a code would tell a client to fix a correct request.
        def _refuse(_model_root: str, _arxml_version: str | None = None) -> ConsistencyResult:
            return ConsistencyResult(
                consistent=False,
                issues=["a rule nobody assigned a code to"],
                records=(ConsistencyIssue(rule="check-nobody-mapped", message="a rule nobody assigned a code to"),),
            )

        monkeypatch.setattr(passes_mod, "check_cross_layer_consistency", _refuse)
        with pytest.raises(UndiagnosedInconsistency, match="engine/validator disagreement"):
            pass_d(str(project / "model"))

    def test_the_raise_carries_the_validator_issues_as_a_diagnostic(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(_model_root: str, _arxml_version: str | None = None) -> ConsistencyResult:
            return ConsistencyResult(
                consistent=False,
                issues=["the diagnostic sentence the traceback has to carry"],
                records=(ConsistencyIssue(rule="check-nobody-mapped", message="…"),),
            )

        monkeypatch.setattr(passes_mod, "check_cross_layer_consistency", _refuse)
        with pytest.raises(UndiagnosedInconsistency, match="the diagnostic sentence the traceback has to carry"):
            pass_d(str(project / "model"))

    def test_the_raise_names_the_layer_and_element_and_never_where_the_model_is_staged(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one engine exception pass D raises names no staging location.

        FastMCP returns an uncaught exception's text as the tool error, so this message reaches a
        client; ``TestAnEngineFaultDisclosesNoStagingPath`` (``packages/mcp``) holds it over the wire.
        """

        def _refuse(_model_root: str, _arxml_version: str | None = None) -> ConsistencyResult:
            return ConsistencyResult(
                consistent=False,
                issues=["a rule nobody assigned a code to"],
                records=(
                    ConsistencyIssue(
                        rule="check-nobody-mapped",
                        message="a rule nobody assigned a code to",
                        subjects=(Subject(layer="swc", element="ExampleSWC"), Subject(layer="hardware")),
                    ),
                ),
            )

        monkeypatch.setattr(passes_mod, "check_cross_layer_consistency", _refuse)
        model_root = project / "model"
        with pytest.raises(UndiagnosedInconsistency, match="engine/validator disagreement") as raised:
            pass_d(str(model_root))

        message = str(raised.value)
        assert "check-nobody-mapped at 'ExampleSWC' in the swc layer, the hardware layer" in message
        for location in (str(model_root), str(model_root.resolve()), model_root.as_posix(), str(project)):
            assert location not in message
        assert not re.search(r"(?:^|[\s'\"(\[=])(?:/[^\s/'\"]+){2,}|[A-Za-z]:[\\/]", message), message


class TestAnAddressRendersAModelValueThroughTheOnePolicy:
    """`passes._render` renders a member step's value through `rendering.render_model_scalar`.

    No model value reaches it today (a request's integers come through `json.loads`, and no
    value-keyed collection is declared over an integer field); the call keeps the one-policy rule
    without an exception. Both halves are asserted because they take different branches.
    """

    WIDE = int("f" * 4000, 16)

    def _address(self, member: object) -> Address:
        return Address.from_json(
            {
                "layer": "interfaces",
                "element": "IExample",
                "path": [{"field": "data_elements"}, {"member": member}],
            }
        )

    def test_an_ordinary_member_step_is_the_value_itself(self) -> None:
        rendered = passes_mod._render(self._address(7))

        assert rendered == "layer 'interfaces' element 'IExample' -> field='data_elements' -> member=7"

    def test_a_member_step_too_wide_to_render_is_described(self) -> None:
        rendered = passes_mod._render(self._address(self.WIDE))

        assert rendered.endswith("member=<an integer of 16000 bits, too wide to render>")


class TestTheStorageRuleIsDecidedOnTheWireToo:
    """``rule:storage.filename-case-collision`` through the ENGINE.

    On a case-sensitive host ``Spare.yaml`` beside ``spare.yaml`` refuses every change set until a file
    is renamed by hand (the protocol has no relocation primitive); on a case-insensitive host the pair
    cannot be written and the change set is accepted. The message crosses the protocol verbatim, so
    each refusal asserts it names the two elements and no file, separator or directory. The engine
    case is probe-gated; the second witnesses the boundary on every host with a synthesized listing.

    Does not reach the rule's own cases (``test_validator_storage.py``).
    """

    # The base types this class writes live in `data_types/base`.
    _CREATE = {
        "operation": "create",
        "address": {"layer": "base_types", "element": "Probe8"},
        "value": {
            "name": "Probe8",
            "kind": "base_type",
            "size": 8,
            "encoding": "NONE",
            "native_declaration": "uint8_t",
        },
    }

    @staticmethod
    def _primitive(project: Path, filename: str, name: str, package: str | None = None) -> None:
        document = {"name": name, "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"}
        if package is not None:
            # `Spare` and `spare` share `/DataTypes` ignoring case (`rule:name.name-space-collision`),
            # so the second is placed in a sub-package; the two FILES stay flat.
            document["package"] = package
        directory = project / "model" / "data_types" / "base"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    @staticmethod
    def _assert_no_path_in(message: str, project: Path) -> None:
        assert ".yaml" not in message, message
        assert "/" not in message and "\\" not in message, message
        assert str(project) not in message, message

    def test_a_case_only_pair_refuses_every_change_set_where_it_can_exist(self, project: Path, tmp_path: Path) -> None:
        self._primitive(project, "spare.yaml", "spare", package="Legacy")
        self._primitive(project, "Spare.yaml", "Spare")

        response = _dispatch(project, self._CREATE)

        if case_sensitive_under(tmp_path):
            assert response["error"] == "consistency-violation", response
            assert "Elements 'Spare' and 'spare' of layer `base_types`" in response["message"], response
            assert response["addresses"] == [
                {"layer": "base_types", "element": "Spare"},
                {"layer": "base_types", "element": "spare"},
            ], response
            self._assert_no_path_in(response["message"], project)
        else:
            # The second write replaced the first: one file, and nothing to report.
            spare = [
                path for path in (project / "model" / "data_types" / "base").iterdir() if path.stem.lower() == "spare"
            ]
            assert len(spare) == 1, spare
            assert response["response"] == "change_set" and response["status"] == "staged", response

    def test_the_refusal_names_the_elements_and_no_file_on_every_host(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the elements and no file where the tree cannot be built too.

        The real grouping and message run over a synthesized listing holding the pair a case-sensitive host would read.
        """
        self._primitive(project, "Spare.yaml", "Spare")
        self._primitive(project, "Spare_second.yaml", "spare", package="Legacy")
        listing = {"base_types": ("data_types/base/Spare.yaml", "data_types/base/spare.yaml")}
        identities = {"data_types/base/Spare.yaml": "Spare", "data_types/base/spare.yaml": "spare"}
        validator_package.patch_everywhere(
            monkeypatch,
            "check_storage_portability",
            lambda model_root: validator_mod.results.consistency_result(
                validator_mod.storage_portability_records(listing, lambda layer, path: identities[path])
            ),
        )

        response = _dispatch(project, self._CREATE)

        assert response["error"] == "consistency-violation", response
        assert response["addresses"] == [
            {"layer": "base_types", "element": "Spare"},
            {"layer": "base_types", "element": "spare"},
        ], response
        assert "rename one of the two files" in response["message"], response
        self._assert_no_path_in(response["message"], project)


class TestThePlacementRulesAreDecidedOnTheWireToo:
    """The two placement rules through the ENGINE, refused and then accepted with the repair.

    On ``examples/basic`` with ``asil: D`` on ``REQ_EXAMPLE_001`` both components derive ``D`` and the
    baseline is clean. The core rule judges the partition's declared level, so its cases rate
    ``PARTITION0`` ``D`` (a ``D`` partition with a derived-``QM`` component would also be
    ``safety.component-below-partition``'s, with no core repair as the exit).

    Does not reach each rule's own cases (``test_validator_capability.py``) or the editing guard's
    classification (``test_editing_authorization.py``); an accepted change set is only ``staged``.
    """

    _REQUIREMENT = Path("model") / "requirements" / "REQ_EXAMPLE_001.yaml"
    _CORE = Path("model") / "hardware" / "CORE0.yaml"
    _PARTITION = Path("model") / "hardware" / "PARTITION0.yaml"

    @staticmethod
    def _append(path: Path, text: str) -> None:
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")

    @staticmethod
    def _capability(element: str, level: str) -> dict[str, Any]:
        return {
            "operation": "update",
            "address": {"layer": "hardware", "element": element, "path": [{"field": "asil_capability"}]},
            "value": level,
        }

    @staticmethod
    def _budget(component: str, value: int) -> dict[str, Any]:
        return {
            "operation": "update",
            "address": {"layer": "swc", "element": component, "path": [{"field": "memory"}, {"key": "RAM0"}]},
            "value": value,
        }

    #: The three repairs a rated partition on ``CORE0`` names on this base: lower
    #: the partition, raise the core, or move the partition to another core.
    _CORE_REPAIRS = [
        {"layer": "hardware", "element": "PARTITION0", "path": [{"field": "asil"}]},
        {"layer": "hardware", "element": "CORE0", "path": [{"field": "asil_capability"}]},
        {"layer": "hardware", "element": "PARTITION0", "path": [{"field": "core"}]},
    ]

    def test_a_core_rated_below_its_partition_is_refused_and_raising_it_is_the_exit(self, project: Path) -> None:
        self._append(project / self._REQUIREMENT, "asil: D\n")
        self._append(project / self._PARTITION, "asil: D\n")

        refused = _dispatch(project, self._capability("CORE0", "B"))

        assert refused["error"] == "consistency-violation", refused
        assert "ASIL capability B" in refused["message"], refused
        assert refused["addresses"] == self._CORE_REPAIRS, refused

        _assert_accepted(project, self._capability("CORE0", "D"))

    def test_lowering_a_capability_under_a_partition_already_above_it_is_refused_identically(
        self, project: Path
    ) -> None:
        """The reverse of the raise: the same finding, reached by a tightening."""
        self._append(project / self._REQUIREMENT, "asil: D\n")
        self._append(project / self._PARTITION, "asil: D\n")
        self._append(project / self._CORE, "asil_capability: D\n")

        refused = _dispatch(project, self._capability("CORE0", "C"))

        assert refused["error"] == "consistency-violation", refused
        assert "ASIL capability C" in refused["message"], refused
        # The same three repairs as the raise direction.
        assert refused["addresses"] == self._CORE_REPAIRS, refused

    def test_a_memory_rated_below_a_component_budgeting_it_is_refused_and_raising_it_is_the_exit(
        self, project: Path
    ) -> None:
        self._append(project / self._REQUIREMENT, "asil: D\n")

        refused = _dispatch(project, self._capability("RAM0", "B"))

        assert refused["error"] == "consistency-violation", refused
        assert "budgets" in refused["message"] and "ASIL capability B" in refused["message"], refused
        assert {"layer": "hardware", "element": "RAM0", "path": [{"field": "asil_capability"}]} in refused["addresses"]

        _assert_accepted(project, self._capability("RAM0", "D"))

    def test_a_zero_budget_is_the_other_exit_on_the_wire(self, project: Path) -> None:
        """`0` declares no use, so the same rated memory is accepted once no component uses it."""
        self._append(project / self._REQUIREMENT, "asil: D\n")

        _assert_accepted(
            project,
            self._capability("RAM0", "B"),
            self._budget("ExampleSWC", 0),
            self._budget("ExampleConsumerSWC", 0),
        )


class TestTheCoexistenceRulesAreDecidedOnTheWireToo:
    """The coexistence rules through the ENGINE, one case per rule.

    The two rules with an exemption are asserted both ways over the same staged model: refused without
    the declared argument, accepted with it.

    Does not reach each rule's own cases (``test_validator_coexistence.py``).
    """

    _REQUIREMENT = Path("model") / "requirements" / "REQ_EXAMPLE_001.yaml"
    _INTERFACE = Path("model") / "interfaces" / "IExample.yaml"
    _TASK = Path("model") / "tasks" / "Task_10ms.yaml"

    @staticmethod
    def _append(path: Path, text: str) -> None:
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")

    def _rate_element(self, project: Path, level: str) -> None:
        """`IExample.value` rated by a minimal rewrite of the interface file (an append cannot reach a member)."""
        (project / self._INTERFACE).write_text(
            "name: IExample\nkind: sender_receiver\ndata_elements:\n"
            f"  - name: value\n    implementation_type: float32\n    asil: {level}\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _element_asil(value: str) -> dict[str, Any]:
        return {
            "operation": "update",
            "address": {
                "layer": "interfaces",
                "element": "IExample",
                "path": [{"field": "data_elements"}, {"member": "value"}, {"field": "asil"}],
            },
            "value": value,
        }

    @staticmethod
    def _measure(port: str, key: str, text: str) -> dict[str, Any]:
        return {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [{"field": "ports"}, {"member": port}, {"field": "measures"}, {"key": key}],
            },
            "value": text,
        }

    def test_a_provider_below_the_element_it_produces_is_refused(self, project: Path) -> None:
        """A derived `QM` provider of a rated element is the finding, not an exemption."""
        response = _dispatch(project, self._element_asil("D"))

        assert response["error"] == "consistency-violation", response
        assert "provides 'value'" in response["message"], response
        assert "ASIL QM" in response["message"]

    def test_a_consumer_above_the_element_is_refused_and_the_measure_is_the_exit(self, project: Path) -> None:
        """The one-exemption rule over the SAME staged model: refused without the argument, accepted with it."""
        self._append(project / self._REQUIREMENT, "asil: D\n")

        refused = _dispatch(project, self._element_asil("A"))

        assert refused["error"] == "consistency-violation", refused
        assert "declares no measure" in refused["message"], refused

        _assert_accepted(
            project,
            self._element_asil("A"),
            self._measure("rIn", "value", "End-to-end protection profile 4."),
        )

    def test_a_blank_measure_closes_the_consumer_rule_and_is_refused_by_its_own(self, project: Path) -> None:
        """A blank measure closes the consumer finding but is refused as ``safety.measure-blank`` at its own key.

        The authorization guard counts it as an argument all the same.
        """
        self._append(project / self._REQUIREMENT, "asil: D\n")

        response = _dispatch(project, self._element_asil("A"), self._measure("rIn", "value", ""))

        assert response["error"] == "consistency-violation", response
        assert "declares a blank measure for 'value'" in response["message"], response
        assert "declares no measure" not in response["message"], response
        assert response["addresses"] == [
            {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [{"field": "ports"}, {"member": "rIn"}, {"field": "measures"}, {"key": "value"}],
            }
        ]

    def test_a_measure_naming_no_element_of_the_interface_is_refused(self, project: Path) -> None:
        response = _dispatch(project, self._measure("rIn", "valu", "E2E."))

        assert response["error"] == "consistency-violation", response
        assert "'valu'" in response["message"], response
        assert response["addresses"] == [
            {
                "layer": "swc",
                "element": "ExampleConsumerSWC",
                "path": [
                    {"field": "ports"},
                    {"member": "rIn"},
                    {"field": "measures"},
                    {"key": "valu"},
                ],
            }
        ]

    def test_a_measure_on_a_provide_port_is_refused_and_the_file_still_loads(self, project: Path) -> None:
        """Pass D refuses, never the loader: the same map in the file leaves a project every read-only command opens."""
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {
                    "layer": "swc",
                    "element": "ExampleSWC",
                    "path": [
                        {"field": "ports"},
                        {"member": "pOut"},
                        {"field": "measures"},
                        {"key": "value"},
                    ],
                },
                "value": "E2E.",
            },
        )

        assert response["error"] == "consistency-violation", response
        assert "provide port" in response["message"], response

        component = next((project / "model" / "swc").rglob("ExampleSWC.yaml"))
        document = yaml.safe_load(component.read_text(encoding="utf-8"))
        document["ports"][0]["measures"] = {"value": "E2E."}
        component.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        from swcraft_tools.reader import read_swc_model

        loaded = {swc.name: swc for swc in read_swc_model(str(project / "model"))}
        assert loaded["ExampleSWC"].ports[0].measures == {"value": "E2E."}

    def test_a_mixed_task_is_refused_and_the_coexistence_argument_is_the_exit(self, project: Path) -> None:
        """The second exemption, both directions over the same staged model."""
        (project / self._TASK).write_text(
            "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION0\nwcet: 1200us\n"
            "description: 10 ms application task\nrunnables:\n"
            "- swc: ExampleConsumerSWC\n  runnable: ExampleConsumerRun\n"
            "- swc: ExampleSWC\n  runnable: ExampleRun\n",
            encoding="utf-8",
            newline="\n",
        )
        declare_d = {
            "operation": "update",
            "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "asil"}]},
            "value": "D",
        }

        refused = _dispatch(project, declare_d)

        assert refused["error"] == "consistency-violation", refused
        assert "different ASILs" in refused["message"], refused

        _assert_accepted(
            project,
            declare_d,
            {
                "operation": "update",
                "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "coexistence"}]},
                "value": "Separate partitions with timing protection.",
            },
        )


class TestTheWidenedInterfaceConditionIsDecidedOnTheWireToo:
    """The widened ``connector.interface-mismatch`` through the ENGINE.

    Every committed exchange joins two ports naming one interface, which the predicate short-circuits,
    so the wire was untested. A compatible pair under name equality was refused, and since pass D
    waives nothing an imported export of such pairs could commit no change set at all; it stages now,
    and the incompatible twin is still refused with the predicate's subject.

    Does not reach the predicate's row table (``test_interface_compatibility.py``) or the rule's
    validator cases (``test_validator_compositions.py``).
    """

    #: The provided side: two data elements, of which the required side asks for one. The two are
    #: joined by the standard's condition alone (different names, the shared element typed alike).
    _WIDE = {
        "name": "IStatusWide",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": "speed", "implementation_type": "float32"},
            {"name": "level", "implementation_type": "uint8"},
        ],
        "description": "Speed with the level beside it",
    }

    @staticmethod
    def _narrow(element: str) -> dict[str, Any]:
        """The required interface, asking for *element* alone."""
        return {
            "name": "IStatusNarrow",
            "kind": "sender_receiver",
            "data_elements": [{"name": element, "implementation_type": "float32"}],
            "description": "Just the speed",
        }

    @staticmethod
    def _port(component: str, port: str, direction: str, interface: str) -> dict[str, Any]:
        return {
            "operation": "create",
            "address": {"layer": "swc", "element": component, "path": [{"field": "ports"}, {"member": port}]},
            "value": {
                "name": port,
                "direction": direction,
                "interface": interface,
                "description": f"{direction}s the status",
            },
        }

    _CONNECTOR = {
        "operation": "create",
        "address": {"layer": "connectors", "element": "Status_Wide_to_Narrow"},
        "value": {
            "name": "Status_Wide_to_Narrow",
            "from": {"swc": "ExampleSWC", "port": "pStatus"},
            "to": {"swc": "ExampleConsumerSWC", "port": "rStatus"},
            "description": "ExampleSWC satisfies IStatusNarrow with IStatusWide",
        },
    }

    #: The two endpoints the refusal names: the two legal repairs.
    _ENDPOINTS = [
        {
            "layer": "connectors",
            "element": "Status_Wide_to_Narrow",
            "path": [{"field": "from"}, {"field": "port"}],
        },
        {
            "layer": "connectors",
            "element": "Status_Wide_to_Narrow",
            "path": [{"field": "to"}, {"field": "port"}],
        },
    ]

    @classmethod
    def _operations(cls, required_element: str) -> list[dict[str, Any]]:
        """The whole exchange (two interfaces, a port each, the connector), resolved against the staged result alone."""
        return [
            {"operation": "create", "address": {"layer": "interfaces", "element": "IStatusWide"}, "value": cls._WIDE},
            {
                "operation": "create",
                "address": {"layer": "interfaces", "element": "IStatusNarrow"},
                "value": cls._narrow(required_element),
            },
            cls._port("ExampleSWC", "pStatus", "provide", "IStatusWide"),
            cls._port("ExampleConsumerSWC", "rStatus", "require", "IStatusNarrow"),
            cls._CONNECTOR,
        ]

    def test_the_two_joined_ports_really_name_two_interfaces(self) -> None:
        """Non-vacuity: the same name is the short-circuit, so the fixture must keep two interfaces."""
        ports = {
            operation["value"]["name"]: operation["value"]["interface"]
            for operation in self._operations("speed")
            if operation["address"]["layer"] == "swc"
        }
        assert ports == {"pStatus": "IStatusWide", "rStatus": "IStatusNarrow"}
        assert ports["pStatus"] != ports["rStatus"]

    def test_a_connector_between_two_different_compatible_interfaces_is_staged(self, project: Path) -> None:
        """The compatible pair is staged on the wire."""
        _assert_accepted(project, *self._operations("speed"))

    def test_an_approving_host_commits_it_and_the_connector_lands_on_disk(self, project: Path) -> None:
        """An approving host commits it and the connector lands: the model really moves."""
        request = {
            "operation": "change_set",
            "baseline": baseline_token(project / "model"),
            "intent": "Join the narrow requirer to the wide provider.",
            "operations": self._operations("speed"),
        }

        response = handle_request(
            request,
            project=project,
            review=lambda proposal: ApprovalDecision(
                change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True
            ),
        )

        assert response["status"] == "committed", response
        connector = project / "model" / "connectors" / "Status_Wide_to_Narrow.yaml"
        assert connector.is_file()
        assert yaml.safe_load(connector.read_text(encoding="utf-8"))["from"]["port"] == "pStatus"

    def test_the_incompatible_twin_is_refused_with_the_predicates_subject(self, project: Path) -> None:
        """Renaming one element refuses the exchange again, the message asserted from its first byte.

        Its prefix is the one the unwidened condition writes.
        """
        response = _dispatch(project, *self._operations("torque"))

        assert response["error"] == "consistency-violation", response
        assert response["failure_class"] == "invalid"
        assert response["message"].startswith(
            "Connector 'Status_Wide_to_Narrow' interface mismatch: required 'IStatusNarrow' "
            "(ExampleConsumerSWC.rStatus) is not satisfied by provided 'IStatusWide' "
            "(ExampleSWC.pStatus): data element 'torque' has no counterpart in 'IStatusWide'."
        ), response["message"]
        assert response["addresses"] == self._ENDPOINTS, response
        # A protocol message names no rule id; the code and the two addresses are the client's keys.
        assert "connector.interface-mismatch" not in response["message"]

    def test_the_refused_model_is_byte_identical_afterwards(self, project: Path) -> None:
        before = _tree_state(project / "model")

        _dispatch(project, *self._operations("torque"))

        assert _tree_state(project / "model") == before

    def test_the_rule_pass_d_translates_is_the_connector_one(self, project: Path) -> None:
        """The same defect at pass D's own seam, under the rule id, over a committed model."""
        handle_request(
            {
                "operation": "change_set",
                "baseline": baseline_token(project / "model"),
                "intent": "Join the narrow requirer to the wide provider.",
                "operations": self._operations("speed"),
            },
            project=project,
            review=lambda proposal: ApprovalDecision(
                change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True
            ),
        )
        narrow = project / "model" / "interfaces" / "IStatusNarrow.yaml"
        narrow.write_text(narrow.read_text(encoding="utf-8").replace("speed", "torque"), encoding="utf-8", newline="\n")

        violations = _rule_violations(project, "connector.interface-mismatch")

        assert [violation.error for violation in violations] == ["consistency-violation"]
        assert _addresses(violations[0]) == self._ENDPOINTS

    def test_two_ports_naming_one_interface_are_staged_as_they_always_were(self, project: Path) -> None:
        """The short-circuit on the wire: the path every committed model takes."""
        _assert_accepted(
            project,
            self._port("ExampleSWC", "pStatus", "provide", "IExample"),
            self._port("ExampleConsumerSWC", "rStatus", "require", "IExample"),
            self._CONNECTOR,
        )
