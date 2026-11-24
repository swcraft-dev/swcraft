"""The real tier's counting of ``check-model`` (``real_models.count_check_model``).

``check-model --format json`` is counted by occurrence, severity and rule, and judged nowhere; the
import report is never read. Held on synthetic documents carrying the name and text sentinels
wherever a real export's names would sit, so counts that copied any would show it:

- errors and warnings are counted apart, each by rule;
- one declaration path carrying four references counts four, not one;
- a rule outside the validator's vocabulary is ``unrecognised-rule``, except
  ``manifest.unknown-key``, counted by name;
- every unreadable document is a closed sentence;
- a message carrying an import report's heading with its figure counts nothing from it;
- no field, summary or count carries a sentinel or the prefix ``zqsen``.

Does not reach the documents a real ``check-model`` prints, which the real tier reads.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from tests.functional.real_models import (
    ANNOUNCED_RULES,
    UNRECOGNISED_RULE,
    CheckModelCounts,
    count_check_model,
)

NAME = "ZqSentinelName"
TEXT = "ZqSentinelText"
PREFIX = "zqsen"


def _finding(rule: str, severity: str = "error") -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "message": f"{TEXT}: {NAME}Port names {NAME}Interface, which is declared nowhere",
        "subjects": [{"layer": "swc", "element": f"{NAME}Swc", "path": f"ports[{NAME}Port]"}],
        "target": f"{NAME}Interface",
    }


def _document(*findings: dict[str, Any]) -> str:
    return json.dumps({"schemaVersion": 1, "findings": list(findings)}, indent=2)


def _names_free(counts: CheckModelCounts) -> CheckModelCounts:
    for rendering in [repr(counts), counts.summary(), *map(str, dataclasses.astuple(counts))]:
        assert PREFIX not in rendering.lower(), "the counts carried a sentinel"
    return counts


class TestCounts:
    def test_exit_zero_with_no_finding(self):
        counts = _names_free(count_check_model(_document(), exit_code=0))
        assert (counts.errors, counts.warnings, counts.problem) == (0, 0, None)
        assert counts.summary() == "exit 0; 0 error finding(s); 0 warning finding(s)"

    def test_errors_and_warnings_are_counted_apart_each_by_rule(self):
        document = _document(
            _finding("port.undeclared-interface"),
            _finding("port.undeclared-interface"),
            _finding("swc.access.unknown-port"),
            _finding("trace.requirement-unallocated", "warning"),
            _finding(f"{PREFIX}tinel.rule", "warning"),
        )
        counts = _names_free(count_check_model(document, exit_code=1))
        assert counts.errors_by_rule == {"port.undeclared-interface": 2, "swc.access.unknown-port": 1}
        assert counts.warnings_by_rule == {"trace.requirement-unallocated": 1, UNRECOGNISED_RULE: 1}
        assert counts.announced == 2
        assert counts.summary() == (
            "exit 1; 3 error finding(s) (port.undeclared-interface 2, swc.access.unknown-port 1); "
            f"2 warning finding(s) (trace.requirement-unallocated 1, {UNRECOGNISED_RULE} 1)"
        )

    def test_one_declaration_path_carrying_four_references_counts_four(self):
        """The mapping-set shape: four rows of one set, four findings, counted four, not one."""
        findings = [_finding("mapping-set.unknown-application")] * 2 + [
            _finding("mapping-set.unknown-implementation")
        ] * 2
        counts = _names_free(count_check_model(_document(*findings), exit_code=1))
        assert counts.errors == 4 and counts.announced == 4

    def test_a_rule_outside_the_validator_s_vocabulary_is_unrecognised(self):
        counts = _names_free(count_check_model(_document(_finding(f"{PREFIX}tinel.{NAME}")), exit_code=1))
        assert counts.errors_by_rule == {UNRECOGNISED_RULE: 1}

    def test_the_manifest_rule_outside_the_vocabulary_is_counted_by_name(self):
        """``check-model``'s one rule outside the contract is no unrecognised rule."""
        counts = _names_free(count_check_model(_document(_finding("manifest.unknown-key")), exit_code=1))
        assert counts.errors_by_rule == {"manifest.unknown-key": 1}

    def test_the_four_rules_are_the_unresolved_reference_rules(self):
        assert ANNOUNCED_RULES == {
            "port.undeclared-interface",
            "composition.port-undeclared-interface",
            "mapping-set.unknown-application",
            "mapping-set.unknown-implementation",
        }

    @pytest.mark.parametrize(
        "document",
        [
            "",
            f"Error: {NAME} could not be read",
            json.dumps([NAME]),
            json.dumps({"schemaVersion": 2, "findings": []}),
            json.dumps({"schemaVersion": 1, "findings": {NAME: TEXT}}),
            json.dumps({"schemaVersion": 1, "findings": [NAME]}),
            json.dumps({"schemaVersion": 1, "findings": [_finding("port.undeclared-interface", NAME)]}),
        ],
        ids=[
            "empty",
            "text",
            "a-list",
            "another-schema-version",
            "findings-not-a-list",
            "a-string-finding",
            "a-severity",
        ],
    )
    def test_an_unparseable_document_is_a_closed_sentence(self, document):
        counts = _names_free(count_check_model(document, exit_code=1))
        assert counts.problem is not None and counts.errors_by_rule == {} and counts.warnings_by_rule == {}


#: A heading of an import report with an injected figure, as a name or a file name could carry it.
INJECTED_HEADING = "\nUnresolved references (987654321 reference(s) to 0 target(s))"


def test_an_injected_report_heading_in_a_message_counts_nothing():
    """A report heading carried in a message or a name is never parsed for its figure."""
    finding = _finding("port.undeclared-interface")
    finding["message"] += INJECTED_HEADING
    finding["subjects"][0]["element"] += INJECTED_HEADING
    counts = _names_free(count_check_model(_document(finding), exit_code=1))
    assert counts.errors_by_rule == {"port.undeclared-interface": 1} and counts.announced == 1
    for rendering in (repr(counts), counts.summary()):
        assert "987654321" not in rendering.replace(",", "").replace("\u202f", "").replace(" ", "")
