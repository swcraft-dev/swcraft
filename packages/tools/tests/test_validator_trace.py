"""The requirement traceability rules ``check_requirement_traceability`` mints.

The **errors** are ``trace.derived-from-unknown-target``, ``trace.derived-from-cycle``,
``trace.decomposition-context-mismatch`` and ``trace.duplicate-link`` (one ``satisfies``
list naming one requirement twice, on each of the twelve ``satisfies`` fields); the
**warnings** ``trace.requirement-unallocated``, ``trace.link-version-not-stated``,
``trace.link-needs-confirmation``, ``trace.requirement-stub`` (reported once per stub
id, abstaining from ``trace.requirement-unallocated``), ``trace.link-unpublishable-id``
and ``trace.requirement-blank-text``. Every case runs the composed gate ``check-model``
runs over a copy of ``examples/basic`` (``REQ_EXAMPLE_001`` at version 1, satisfied
``@1`` by both components), so it shows where its finding lands -- ``records`` or
``warnings`` -- and that nothing else moved.

* **the errors** -- each ``derived_from`` member naming nothing (no requirement, no
  element, a token of no linkable layer, a port, a malformed suffix); a two-member
  cycle, a self-cycle and a cycle entered from outside, each reported once with its
  members in id order from the smallest; the decomposition rule's cases; and, per
  rule, a refusing ``check-model`` and the protocol code pass D answers;
* **the warnings** -- the version warnings over ``satisfies`` and ``derived_from``,
  their abstention (an id two files declare, a suffix that is not a version), the
  unallocated requirement a ``derived_from`` does not rescue, and, per rule, a
  ``check-model`` that lists it and exits 0 and a change set pass D commits.

**What this module does not reach.** The ordering of warnings across layers is
asserted over one element per layer, not every kind (the sender-receiver interface
and the core stand for theirs); the unknown-requirement error is executed on every
interface and hardware kind. The cycle rule's own limit -- a cycle met only through a
finished requirement surfaces once the reported one is broken -- is pinned, not
lifted. The decomposition itself (the pair a level was split into) is checked nowhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import validator_package
import yaml
from swcraft_tools.cli import main
from swcraft_tools.contract import load_contract
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.passes import pass_d
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.validator import WARNING_RULES, ConsistencyResult, check_cross_layer_consistency

_REQUIREMENT = "REQ_EXAMPLE_001"
_PROVIDER = "swc/example/ExampleSWC.yaml"
_CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
_ROOT = "compositions/Root.yaml"

ERROR_RULES = (
    "trace.derived-from-unknown-target",
    "trace.derived-from-cycle",
    "trace.decomposition-context-mismatch",
    # One list naming one requirement twice.
    "trace.duplicate-link",
)
WARNING_TRACE_RULES = (
    "trace.requirement-unallocated",
    "trace.link-version-not-stated",
    "trace.link-needs-confirmation",
    "trace.requirement-stub",
    # What the published `needs.json` cannot carry.
    "trace.link-unpublishable-id",
    "trace.requirement-blank-text",
)
TRACE_RULES = (*ERROR_RULES, *WARNING_TRACE_RULES)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the reference model with its manifest: the project root."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _model(project: Path) -> Path:
    return project / "model"


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _edit(project: Path, relative: str, **changes: Any) -> None:
    path = _model(project) / relative
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    _write(path, document)


def _requirement(project: Path, requirement_id: str, *, file_name: str | None = None, **fields: Any) -> None:
    """Write an architecture requirement at version 1 unless *fields* say otherwise."""
    document: dict[str, Any] = {
        "id": requirement_id,
        "title": "Probe",
        "description": "A requirement this module writes.",
        "version": 1,
        "source": "architecture",
        **fields,
    }
    _write(_model(project) / "requirements" / f"{file_name or requirement_id}.yaml", document)


def _check(project: Path) -> ConsistencyResult:
    return check_cross_layer_consistency(str(_model(project)))


def _of(records: tuple[Any, ...], rule: str) -> list[Any]:
    return [record for record in records if record.rule == rule]


def _trace(records: tuple[Any, ...]) -> list[Any]:
    return [record for record in records if record.rule in TRACE_RULES]


def _member(requirement_id: str, value: str) -> Subject:
    return Subject("requirements", requirement_id, (field_step("derived_from"), member_step(value)))


def _check_model(project: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    """Run ``check-model`` on *project* with *extra* flags; the exit code."""
    monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(project / "swcraft.yaml"), *extra])
    try:
        main()
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return 0


def test_the_reference_model_has_no_traceability_finding(project: Path) -> None:
    """Non-vacuity: every case below starts from a model no traceability rule reports."""
    result = _check(project)
    assert result.consistent is True
    assert _trace(result.records) == []
    assert _trace(result.warnings) == []


def test_the_warnings_are_warning_rules_and_have_no_code() -> None:
    """The warnings are warning rules with no protocol code; each error carries its code."""
    assert set(WARNING_TRACE_RULES) <= WARNING_RULES
    assert not set(ERROR_RULES) & WARNING_RULES
    assert not set(WARNING_TRACE_RULES) & set(RULE_CODES)
    assert {rule: RULE_CODES[rule].code for rule in ERROR_RULES} == {
        "trace.derived-from-unknown-target": "broken-reference",
        "trace.derived-from-cycle": "consistency-violation",
        "trace.decomposition-context-mismatch": "consistency-violation",
        "trace.duplicate-link": "consistency-violation",
    }


# ---------------------------------------------------------------------------
# The errors
# ---------------------------------------------------------------------------


class TestADerivedFromMemberNamingNothing:
    """``trace.derived-from-unknown-target``: the member is the subject, and the message says why."""

    @pytest.mark.parametrize(
        ("member", "reason"),
        [
            ("NOPE@1", "no requirement declares 'NOPE'"),
            ("hardware:NoCore", "no element of the 'hardware' layer is named 'NoCore'"),
            ("connector:Example_to_Consumer", "'connector' is the token of no linkable layer"),
            # A port is no top-level element: `swc:` resolves a component by name.
            ("swc:pOut", "no element of the 'swc' layer is named 'pOut'"),
            ("interface:NoSuch", "no element of the 'interfaces' layer is named 'NoSuch'"),
            ("task:NoSuch", "no element of the 'tasks' layer is named 'NoSuch'"),
            (f"{_REQUIREMENT}@x", "'x' after '@' is not a version"),
            (f"{_REQUIREMENT}@0", "'0' after '@' is not a version"),
        ],
    )
    def test_the_member_is_reported_with_its_reason(self, project: Path, member: str, reason: str) -> None:
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", derived_from=[member])

        result = _check(project)

        [record] = _of(result.records, "trace.derived-from-unknown-target")
        assert record.subjects == (_member(_REQUIREMENT, member),)
        assert record.message.startswith(f"Requirement '{_REQUIREMENT}' derives from '{member}': {reason}. ")
        assert _trace(result.warnings) == []

    @pytest.mark.parametrize(
        "member",
        [
            "hardware:CORE0",
            "swc:ExampleSWC",
            "composition:Root",
            "interface:IExample",
            "task:Task_10ms",
            "REQ_PARENT@1",
        ],
    )
    def test_a_member_naming_a_declared_target_is_no_finding(self, project: Path, member: str) -> None:
        _requirement(project, "REQ_PARENT")
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "REQ_PARENT@1"])
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", derived_from=[member])

        result = _check(project)

        assert _trace(result.records) == []
        assert _trace(result.warnings) == []


class TestADerivationCycle:
    """``trace.derived-from-cycle``: once per cycle, members in id order from the smallest."""

    def test_a_two_member_cycle_is_reported_once(self, project: Path) -> None:
        _requirement(project, "REQ_B", derived_from=["REQ_A@1"])
        _requirement(project, "REQ_A", derived_from=["REQ_B@1"])

        [record] = _of(_check(project).records, "trace.derived-from-cycle")

        assert [subject.element for subject in record.subjects] == ["REQ_A", "REQ_B"]
        assert "REQ_A -> REQ_B -> REQ_A" in record.message

    def test_a_self_derivation_is_a_cycle_of_one(self, project: Path) -> None:
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", derived_from=[f"{_REQUIREMENT}@1"])

        [record] = _of(_check(project).records, "trace.derived-from-cycle")

        assert record.subjects == (Subject("requirements", _REQUIREMENT, (field_step("derived_from"),)),)
        assert f"{_REQUIREMENT} -> {_REQUIREMENT}." in record.message

    def test_a_cycle_entered_from_outside_starts_at_its_smallest_member(self, project: Path) -> None:
        # The walk reaches the cycle from `REQ_A`, which is not on it, at `REQ_C`.
        _requirement(project, "REQ_A", derived_from=["REQ_C@1"])
        _requirement(project, "REQ_C", derived_from=["REQ_B@1"])
        _requirement(project, "REQ_B", derived_from=["REQ_C@1"])

        [record] = _of(_check(project).records, "trace.derived-from-cycle")

        assert [subject.element for subject in record.subjects] == ["REQ_B", "REQ_C"]
        assert "REQ_B -> REQ_C -> REQ_B" in record.message

    def test_a_cycle_closed_only_through_a_finished_requirement_surfaces_once_the_reported_one_is_broken(
        self, project: Path
    ) -> None:
        """The rule's stated limit: one finding per back edge the walk meets, not per elementary cycle.

        ``RA`` derives from ``RB`` and ``RC``, ``RB`` from ``RC``, ``RC`` from ``RA``: two
        elementary cycles, ``RA -> RB -> RC -> RA`` and ``RA -> RC -> RA``. The walk finishes
        ``RC`` through ``RB`` and meets ``RA -> RC`` at a finished requirement, so only the
        first is reported; removing ``RB`` from ``RA`` leaves the second, reported then.
        """
        _requirement(project, "RA", derived_from=["RB@1", "RC@1"])
        _requirement(project, "RB", derived_from=["RC@1"])
        _requirement(project, "RC", derived_from=["RA@1"])

        [record] = _of(_check(project).records, "trace.derived-from-cycle")
        assert "RA -> RB -> RC -> RA" in record.message
        assert [subject.element for subject in record.subjects] == ["RA", "RB", "RC"]

        _requirement(project, "RA", derived_from=["RC@1"])

        [record] = _of(_check(project).records, "trace.derived-from-cycle")
        assert "RA -> RC -> RA" in record.message

    def test_a_chain_deeper_than_the_recursion_limit_is_walked(self, project: Path) -> None:
        """The walk is iterative: a chain longer than the interpreter's recursion limit is no RecursionError."""
        depth = sys.getrecursionlimit() + 50
        for index in range(depth):
            parent = [f"RD{index + 1:05d}@1"] if index + 1 < depth else ["RD00000@1"]
            _requirement(project, f"RD{index:05d}", derived_from=parent)

        [record] = _of(_check(project).records, "trace.derived-from-cycle")
        assert len(record.subjects) == depth
        assert record.message.startswith("Requirements derive from each other in a cycle: RD00000 -> RD00001 -> ")

    def test_an_element_member_is_a_leaf_and_a_chain_is_no_cycle(self, project: Path) -> None:
        _requirement(project, "REQ_A", derived_from=["REQ_B@1", "hardware:CORE0"])
        _requirement(project, "REQ_B", derived_from=["hardware:CORE0"])

        assert _of(_check(project).records, "trace.derived-from-cycle") == []


class TestTheDecompositionContext:
    """``trace.decomposition-context-mismatch``: the child's ``(Y)`` against the parent's level before decomposition."""

    @pytest.mark.parametrize(
        ("child", "parent", "reported"),
        [
            ("B(D)", "D", False),
            ("B(D)", "C(D)", False),
            ("B(D)", "C", True),
            ("B(D)", "QM", True),
            # An unlevelled parent and a plain child abstain.
            ("B(D)", None, False),
            ("B", "C", False),
        ],
    )
    def test_the_bracket_letter_is_compared_with_the_parents_level(
        self, project: Path, child: str, parent: str | None, reported: bool
    ) -> None:
        _requirement(project, "REQ_PARENT", **({"asil": parent} if parent is not None else {}))
        _requirement(project, "REQ_CHILD", asil=child, derived_from=["REQ_PARENT@1"])

        records = _of(_check(project).records, "trace.decomposition-context-mismatch")

        if not reported:
            assert records == []
            return
        [record] = records
        assert record.subjects == (
            Subject("requirements", "REQ_CHILD", (field_step("asil"),)),
            _member("REQ_CHILD", "REQ_PARENT@1"),
        )
        assert f"rated '{child}'" in record.message
        assert f"rated '{parent}'" in record.message
        assert f"which is '{parent}' for that parent, not 'D'" in record.message

    def test_an_element_parent_abstains(self, project: Path) -> None:
        _requirement(project, "REQ_CHILD", asil="B(D)", derived_from=["hardware:CORE0"])

        assert _of(_check(project).records, "trace.decomposition-context-mismatch") == []

    def test_an_undeclared_parent_abstains_and_is_the_unknown_target_rule_alone(self, project: Path) -> None:
        _requirement(project, "REQ_CHILD", asil="B(D)", derived_from=["REQ_NOPE@1"])

        records = _check(project).records
        assert _of(records, "trace.decomposition-context-mismatch") == []
        assert [record.subjects for record in _of(records, "trace.derived-from-unknown-target")] == [
            (_member("REQ_CHILD", "REQ_NOPE@1"),)
        ]

    def test_a_parent_id_two_files_declare_is_compared_with_each_declaration(self, project: Path) -> None:
        """Each declaration is a levelled parent: the one that disagrees is reported, beside the duplicate."""
        _requirement(project, "REQ_PARENT", asil="D")
        _requirement(project, "REQ_PARENT", file_name="REQ_PARENT~1", asil="C")
        _requirement(project, "REQ_CHILD", asil="B(D)", derived_from=["REQ_PARENT@1"])

        records = _check(project).records
        [record] = _of(records, "trace.decomposition-context-mismatch")
        assert "rated 'C'" in record.message
        assert len(_of(records, "name.duplicate-requirement")) == 1

    def test_of_two_levelled_parents_only_the_disagreeing_one_is_reported(self, project: Path) -> None:
        _requirement(project, "REQ_P1", asil="D")
        _requirement(project, "REQ_P2", asil="C")
        _requirement(project, "REQ_CHILD", asil="B(D)", derived_from=["REQ_P1@1", "REQ_P2@1"])

        [record] = _of(_check(project).records, "trace.decomposition-context-mismatch")
        assert record.subjects == (
            Subject("requirements", "REQ_CHILD", (field_step("asil"),)),
            _member("REQ_CHILD", "REQ_P2@1"),
        )


_EXTERNAL_STUB = "interfaces/IStub.yaml"


class TestALinkNamingNoRequirementOnTheOtherSites:
    """``trace.link-unknown-requirement`` on each interface kind and each hardware kind."""

    @pytest.mark.parametrize(
        ("relative", "layer", "element", "label"),
        [
            ("interfaces/IExample.yaml", "interfaces", "IExample", "Interface"),
            ("interfaces/IDiagnosticService.yaml", "interfaces", "IDiagnosticService", "Interface"),
            (_EXTERNAL_STUB, "interfaces", "IStub", "Interface"),
            ("hardware/CPU.yaml", "hardware", "CPU", "Hardware element"),
            ("hardware/CORE0.yaml", "hardware", "CORE0", "Hardware element"),
            ("hardware/RAM0.yaml", "hardware", "RAM0", "Hardware element"),
            ("hardware/PARTITION0.yaml", "hardware", "PARTITION0", "Hardware element"),
        ],
        ids=["sender-receiver", "client-server", "external", "processor", "core", "memory", "partition"],
    )
    def test_a_ghost_is_reported_at_the_member(
        self, project: Path, relative: str, layer: str, element: str, label: str
    ) -> None:
        if relative == _EXTERNAL_STUB:
            _write(
                _model(project) / relative,
                {
                    "name": "IStub",
                    "kind": "external_interface",
                    "external_ref": "/Ext/IStub",
                    "external_dest": "SENDER-RECEIVER-INTERFACE",
                },
            )
        _edit(project, relative, satisfies=["REQ_GHOST@1"])

        [record] = _of(_check(project).records, "trace.link-unknown-requirement")

        [subject] = record.subjects
        assert (subject.layer, subject.element) == (layer, element)
        assert tuple(step.value for step in subject.path) == ("satisfies", "REQ_GHOST@1")
        # A well-formed member names no member grammar: the hint is for a malformed one alone.
        assert record.message == (f"{label} '{element}' satisfies 'REQ_GHOST@1': no requirement declares 'REQ_GHOST'.")


#: Every ``satisfies`` field of the contract -> where the reference model holds
#: one (the file, the element's layer and name, the label a message names it by,
#: and the port or runnable holding the list, ``None`` for the element's own).
_LINK_LISTS: dict[str, tuple[str, str, str, str, tuple[str, str] | None]] = {
    "field:swc.swc.satisfies": (_PROVIDER, "swc", "ExampleSWC", "SWC", None),
    "field:swc.port.satisfies": (_PROVIDER, "swc", "ExampleSWC", "SWC", ("ports", "pOut")),
    "field:swc.runnable.satisfies": (_PROVIDER, "swc", "ExampleSWC", "SWC", ("runnables", "ExampleRun")),
    "field:interfaces.sender_receiver.satisfies": (
        "interfaces/IExample.yaml",
        "interfaces",
        "IExample",
        "Interface",
        None,
    ),
    "field:interfaces.client_server.satisfies": (
        "interfaces/IDiagnosticService.yaml",
        "interfaces",
        "IDiagnosticService",
        "Interface",
        None,
    ),
    "field:interfaces.external_interface.satisfies": (_EXTERNAL_STUB, "interfaces", "IStub", "Interface", None),
    "field:compositions.composition.satisfies": (_ROOT, "compositions", "Root", "Composition", None),
    "field:tasks.task.satisfies": ("tasks/Task_10ms.yaml", "tasks", "Task_10ms", "Task", None),
    "field:hardware.processor.satisfies": ("hardware/CPU.yaml", "hardware", "CPU", "Hardware element", None),
    "field:hardware.core.satisfies": ("hardware/CORE0.yaml", "hardware", "CORE0", "Hardware element", None),
    "field:hardware.memory.satisfies": ("hardware/RAM0.yaml", "hardware", "RAM0", "Hardware element", None),
    "field:hardware.partition.satisfies": (
        "hardware/PARTITION0.yaml",
        "hardware",
        "PARTITION0",
        "Hardware element",
        None,
    ),
}


def _contract_link_fields() -> set[str]:
    """Every field of the shipped contract named ``satisfies``, by its id."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            identifier = node.get("id")
            if isinstance(identifier, str) and identifier.startswith("field:") and identifier.endswith(".satisfies"):
                found.add(identifier)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(load_contract()["layers"])
    return found


def _set_links(project: Path, field_id: str, members: list[str]) -> None:
    """Write *members* as the list *field_id* names in the reference model, and the other lists as they were."""
    relative, _layer, _element, _label, holder = _LINK_LISTS[field_id]
    if relative == _EXTERNAL_STUB:
        _write(
            _model(project) / relative,
            {
                "name": "IStub",
                "kind": "external_interface",
                "external_ref": "/Ext/IStub",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
            },
        )
    path = _model(project) / relative
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if holder is None:
        document["satisfies"] = members
    else:
        collection, name = holder
        [member] = [item for item in document[collection] if item["name"] == name]
        member["satisfies"] = members
    _write(path, document)


class TestADuplicateLink:
    """``trace.duplicate-link``: one ``satisfies`` list naming one requirement twice is an error.

    The same member twice and the same id at two versions alike, on every field the
    contract declares ``satisfies`` on.
    """

    def test_the_cases_cover_every_satisfies_field_of_the_contract(self) -> None:
        """A thirteenth ``satisfies`` field fails here until it has a case: the set is the contract's."""
        assert set(_LINK_LISTS) == _contract_link_fields()
        assert len(_LINK_LISTS) == 12
        [rule] = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:trace.duplicate-link"]
        # The twelve `satisfies` fields and the requirement's `derived_from` (`TestADuplicateParent`).
        assert set(rule["appliesTo"]) == {*_contract_link_fields(), "field:requirements.requirement.derived_from"}

    @pytest.mark.parametrize("field_id", sorted(_LINK_LISTS))
    @pytest.mark.parametrize(
        ("members", "named"),
        [
            ([f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@1"], f"'{_REQUIREMENT}@1' 2 times"),
            ([f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@2"], f"'{_REQUIREMENT}@1', '{_REQUIREMENT}@2'"),
            ([_REQUIREMENT, f"{_REQUIREMENT}@1"], f"'{_REQUIREMENT}', '{_REQUIREMENT}@1'"),
        ],
        ids=["same-member", "two-versions", "bare-and-versioned"],
    )
    def test_one_list_naming_one_requirement_twice_is_one_error_at_the_repeat(
        self, project: Path, field_id: str, members: list[str], named: str
    ) -> None:
        _relative, layer, element, label, holder = _LINK_LISTS[field_id]
        _set_links(project, field_id, members)

        result = _check(project)

        [record] = _of(result.records, "trace.duplicate-link")
        assert result.consistent is False
        assert record.target == _REQUIREMENT
        steps = () if holder is None else (field_step(holder[0]), member_step(holder[1]))
        assert record.subjects == (Subject(layer, element, (*steps, field_step("satisfies"), member_step(members[1]))),)
        where = f"{label} '{element}'" if holder is None else f"{label} '{element}' ({holder[0][:-1]} '{holder[1]}')"
        assert record.message == (
            f"{where} satisfies {named}: one list names requirement '{_REQUIREMENT}' more than once. Keep the one "
            "link confirmed against the requirement and remove the others."
        )
        assert not _of(result.warnings, "trace.duplicate-link")

    def test_three_members_of_one_id_are_one_finding_naming_all_three(self, project: Path) -> None:
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "REQ_OTHER@1", f"{_REQUIREMENT}@1", _REQUIREMENT])
        _requirement(project, "REQ_OTHER")

        [record] = _of(_check(project).records, "trace.duplicate-link")

        assert f"satisfies '{_REQUIREMENT}@1', '{_REQUIREMENT}@1', '{_REQUIREMENT}':" in record.message
        # Every member after the first, one subject per occurrence: removing one occurrence per subject repairs it.
        assert [subject.path[-1] for subject in record.subjects] == [
            member_step(f"{_REQUIREMENT}@1"),
            member_step(_REQUIREMENT),
        ]

    def test_a_repeat_occurring_twice_is_addressed_twice(self, project: Path) -> None:
        """``[R@1, R@2, R@2]``: two removes repair it, so ``R@2``'s address is a subject twice."""
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@2", f"{_REQUIREMENT}@2"])

        [record] = _of(_check(project).records, "trace.duplicate-link")

        repeat = Subject("swc", "ExampleSWC", (field_step("satisfies"), member_step(f"{_REQUIREMENT}@2")))
        assert record.subjects == (repeat, repeat)

    def test_two_ports_of_one_name_are_two_lists(self, project: Path) -> None:
        """Two ports named alike (``name.duplicate-port``'s finding) keep their own lists: no duplicate link."""
        path = _model(project) / _PROVIDER
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        [port] = document["ports"]
        port["satisfies"] = [f"{_REQUIREMENT}@1"]
        document["ports"].append({**port, "satisfies": [_REQUIREMENT]})
        _write(path, document)

        result = _check(project)

        assert _of(result.records, "trace.duplicate-link") == []
        assert _of(result.records, "name.duplicate-port")

    def test_two_repeated_ids_of_one_list_are_two_findings_in_first_member_order(self, project: Path) -> None:
        _requirement(project, "REQ_OTHER")
        _edit(project, _PROVIDER, satisfies=["REQ_OTHER@1", f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@1", "REQ_OTHER@1"])

        records = _of(_check(project).records, "trace.duplicate-link")

        assert [record.target for record in records] == ["REQ_OTHER", _REQUIREMENT]

    def test_the_component_and_its_port_naming_one_requirement_are_two_lists_and_no_finding(
        self, project: Path
    ) -> None:
        _set_links(project, "field:swc.port.satisfies", [f"{_REQUIREMENT}@1"])
        _set_links(project, "field:swc.runnable.satisfies", [f"{_REQUIREMENT}@1"])

        result = _check(project)

        assert _of(result.records, "trace.duplicate-link") == []
        assert result.consistent is True

    def test_two_distinct_requirements_in_one_list_are_no_finding(self, project: Path) -> None:
        _requirement(project, "REQ_OTHER")
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "REQ_OTHER@1"])

        assert _of(_check(project).records, "trace.duplicate-link") == []

    def test_an_element_form_member_is_skipped(self, project: Path) -> None:
        """A member holding ``:`` names no requirement: the unknown-requirement rule's, never this one.

        The two identical findings the two members earn are one after the composer's
        de-duplication.
        """
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "swc:Thing", "swc:Thing"])

        result = _check(project)

        assert _of(result.records, "trace.duplicate-link") == []
        assert len(_of(result.records, "trace.link-unknown-requirement")) == 1

    def test_a_malformed_suffix_counts_by_its_id(self, project: Path) -> None:
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@x"])

        result = _check(project)

        [record] = _of(result.records, "trace.duplicate-link")
        assert record.target == _REQUIREMENT
        assert len(_of(result.records, "trace.link-unknown-requirement")) == 1

    def test_an_id_naming_no_requirement_twice_is_both_findings(self, project: Path) -> None:
        """Two defects: the id is undeclared (one finding after the de-duplication) and the list repeats it."""
        _edit(project, _PROVIDER, satisfies=["REQ_GHOST@1", "REQ_GHOST@2"])

        result = _check(project)

        assert [record.target for record in _of(result.records, "trace.duplicate-link")] == ["REQ_GHOST"]
        assert len(_of(result.records, "trace.link-unknown-requirement")) == 2


class TestADuplicateParent:
    """``trace.duplicate-link`` on a requirement's ``derived_from``, one rule with ``satisfies``.

    A parent requirement is keyed by its id, as in a ``satisfies`` list; an
    element reference, which only ``derived_from`` may hold, by its text.
    """

    @pytest.mark.parametrize(
        ("members", "target", "named", "repair"),
        [
            (
                ["REQ_PARENT@1", "REQ_PARENT@2"],
                "REQ_PARENT",
                "'REQ_PARENT@1', 'REQ_PARENT@2'",
                "one list names requirement 'REQ_PARENT' more than once. Keep the one link confirmed against the "
                "requirement and remove the others.",
            ),
            (
                ["REQ_PARENT@1", "REQ_PARENT@1"],
                "REQ_PARENT",
                "'REQ_PARENT@1' 2 times",
                "one list names requirement 'REQ_PARENT' more than once. Keep the one link confirmed against the "
                "requirement and remove the others.",
            ),
            (
                ["swc:ExampleSWC", "REQ_PARENT@1", "swc:ExampleSWC"],
                "swc:ExampleSWC",
                "'swc:ExampleSWC' 2 times",
                "one list names element 'swc:ExampleSWC' more than once. Keep one and remove the others.",
            ),
        ],
        ids=["two-versions", "same-member", "element"],
    )
    def test_a_parent_named_twice_is_one_error_at_the_repeat(
        self, project: Path, members: list[str], target: str, named: str, repair: str
    ) -> None:
        _requirement(project, "REQ_PARENT")
        _requirement(project, "REQ_CHILD", derived_from=members)

        result = _check(project)

        [record] = _of(result.records, "trace.duplicate-link")
        assert result.consistent is False
        assert record.target == target
        # In every case the last member is the one repeat: the subject addresses it alone.
        assert record.subjects == (_member("REQ_CHILD", members[-1]),)
        assert record.message == f"Requirement 'REQ_CHILD' derives from {named}: {repair}"

    def test_two_parents_and_an_element_once_each_are_no_finding(self, project: Path) -> None:
        _requirement(project, "REQ_PARENT")
        _requirement(project, "REQ_CHILD", derived_from=["REQ_PARENT@1", _REQUIREMENT, "swc:ExampleSWC"])

        assert _of(_check(project).records, "trace.duplicate-link") == []

    def test_check_model_refuses_it_and_pass_d_answers_consistency_violation(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _requirement(project, "REQ_PARENT")
        _requirement(project, "REQ_CHILD", derived_from=["REQ_PARENT@1", "REQ_PARENT"])
        [record] = _of(_check(project).records, "trace.duplicate-link")

        assert _check_model(project, monkeypatch) == 1
        assert f"  ERROR: {record.message}\n" in capsys.readouterr().err
        assert [violation.error for violation in pass_d(str(_model(project)))] == ["consistency-violation"]


def _unknown_target(project: Path) -> None:
    _edit(project, f"requirements/{_REQUIREMENT}.yaml", derived_from=["NOPE@1"])


def _cycle(project: Path) -> None:
    _edit(project, f"requirements/{_REQUIREMENT}.yaml", derived_from=[f"{_REQUIREMENT}@1"])


def _mismatch(project: Path) -> None:
    _requirement(project, "REQ_PARENT", asil="C")
    _requirement(project, "REQ_CHILD", asil="B(D)", derived_from=["REQ_PARENT@1"])


def _duplicate(project: Path) -> None:
    _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", f"{_REQUIREMENT}@1"])


_ERROR_SEEDS = {
    "trace.derived-from-unknown-target": _unknown_target,
    "trace.derived-from-cycle": _cycle,
    "trace.decomposition-context-mismatch": _mismatch,
    "trace.duplicate-link": _duplicate,
}


@pytest.mark.parametrize("rule", ERROR_RULES)
def test_each_error_refuses_check_model_and_pass_d_answers_its_code(
    rule: str, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _ERROR_SEEDS[rule](project)
    result = _check(project)
    [record] = _of(result.records, rule)
    assert result.consistent is False

    assert _check_model(project, monkeypatch) == 1
    assert f"  ERROR: {record.message}\n" in capsys.readouterr().err

    violations = pass_d(str(_model(project)))
    assert [violation.error for violation in violations] == [RULE_CODES[rule].code]


# ---------------------------------------------------------------------------
# The warnings
# ---------------------------------------------------------------------------


class TestALinkToAnUnpublishableId:
    """``trace.link-unpublishable-id``: a link ``generate needs`` cannot publish.

    Sphinx-Needs 9.2.0 reads every link value up to its first ``.`` as the id
    and the rest as a part, under every configuration, so ``generate needs``
    refuses such a link; ``check-model`` warns first, on every site the export
    publishes as a link.
    """

    _ID = "SYS.REQ.12"

    def test_it_applies_where_the_duplicate_link_rule_does(self) -> None:
        rules = {rule["id"]: rule for rule in load_contract()["rules"]}
        assert rules["rule:trace.link-unpublishable-id"]["appliesTo"] == rules["rule:trace.duplicate-link"]["appliesTo"]
        assert rules["rule:trace.link-unpublishable-id"]["severity"] == "warning"

    @pytest.mark.parametrize("field_id", sorted(_LINK_LISTS))
    def test_a_satisfies_member_naming_a_dotted_id_is_one_warning_at_the_member(
        self, project: Path, field_id: str
    ) -> None:
        _relative, layer, element, label, holder = _LINK_LISTS[field_id]
        _requirement(project, self._ID)
        _set_links(project, field_id, [f"{self._ID}@1"])

        result = _check(project)

        [record] = _of(result.warnings, "trace.link-unpublishable-id")
        assert not _of(result.records, "trace.link-unpublishable-id")
        assert record.target == self._ID
        steps = () if holder is None else (field_step(holder[0]), member_step(holder[1]))
        member = member_step(f"{self._ID}@1")
        assert record.subjects == (Subject(layer, element, (*steps, field_step("satisfies"), member)),)
        where = f"{label} '{element}'" if holder is None else f"{label} '{element}' ({holder[0][:-1]} '{holder[1]}')"
        assert record.message == (
            f"{where} satisfies '{self._ID}@1': the id '{self._ID}' holds '.', which Sphinx-Needs reads as an id and a "
            "part, so `swcraft generate needs` cannot publish this link. The model may keep it; SWCraft does not "
            "support such an id in a published link."
        )

    def test_a_derived_from_member_naming_a_dotted_id_is_one_warning_at_the_member(self, project: Path) -> None:
        _requirement(project, self._ID)
        _requirement(project, "AR_1", derived_from=[f"{self._ID}@1", "swc:ExampleSWC"])

        [record] = _of(_check(project).warnings, "trace.link-unpublishable-id")

        assert record.subjects == (_member("AR_1", f"{self._ID}@1"),)
        assert record.message.startswith(f"Requirement 'AR_1' derives from '{self._ID}@1': the id '{self._ID}' holds")

    def test_an_incoming_requirements_derived_from_is_not_judged(self, project: Path) -> None:
        """Never published (the export writes architecture requirements alone), so no warning."""
        _requirement(project, self._ID, source="external")
        _requirement(project, "SYS_2", source="external", derived_from=[f"{self._ID}@1"])

        assert not _of(_check(project).warnings, "trace.link-unpublishable-id")

    def test_an_undeclared_dotted_id_is_warned_too(self, project: Path) -> None:
        """The export refuses an unconfirmed link to it as well, so the warning does not wait for the id."""
        _edit(project, _PROVIDER, satisfies=["GHOST.1"])

        [record] = _of(_check(project).warnings, "trace.link-unpublishable-id")

        assert record.target == "GHOST.1"

    @pytest.mark.parametrize("value", ["SYS_REQ[12]@1", "REQ_PLAIN@1", "swc:ExampleSWC"])
    def test_a_bracket_a_plain_id_and_the_element_form_are_no_finding(self, project: Path, value: str) -> None:
        requirement_form = "@" in value
        if requirement_form:
            _requirement(project, value.partition("@")[0])
        _requirement(project, "AR_1", derived_from=[value])
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", *([value] if requirement_form else [])])

        result = _check(project)

        assert not _of(result.warnings, "trace.link-unpublishable-id")
        assert not _of(result.records, "trace.link-unpublishable-id")

    def test_check_model_prints_the_warning_and_exits_0(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _requirement(project, self._ID)
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", f"{self._ID}@1"])
        [record] = _of(_check(project).warnings, "trace.link-unpublishable-id")

        assert _check_model(project, monkeypatch) == 0
        assert f"  WARNING: {record.message}\n" in capsys.readouterr().err


class TestABlankArchitectureRequirementText:
    """``trace.requirement-blank-text``: published as it stands, refused by the import."""

    @pytest.mark.parametrize(
        ("fields", "blank"),
        [
            ({"title": ""}, ("title",)),
            ({"description": " \t\n"}, ("description",)),
            ({"title": " ", "description": ""}, ("title", "description")),
        ],
        ids=["empty-title", "whitespace-description", "both"],
    )
    def test_a_blank_text_is_one_warning_naming_each_blank_field(
        self, project: Path, fields: dict[str, str], blank: tuple[str, ...]
    ) -> None:
        _requirement(project, "AR_1", **fields)

        result = _check(project)

        [record] = _of(result.warnings, "trace.requirement-blank-text")
        assert not _of(result.records, "trace.requirement-blank-text")
        assert record.subjects == tuple(Subject("requirements", "AR_1", (field_step(name),)) for name in blank)
        assert record.message == (
            f"Architecture requirement 'AR_1' has an empty {' and an empty '.join(blank)}: `swcraft generate needs` "
            f"publishes it as it stands, and the requirements import refuses it. Write the requirement's "
            f"{' and '.join(blank)}."
        )

    def test_an_incoming_requirement_is_not_judged(self, project: Path) -> None:
        """Never published, and the import refuses a blank one: only a hand edit makes it."""
        _requirement(project, "SYS_1", source="external", title="", description="")

        assert not _of(_check(project).warnings, "trace.requirement-blank-text")

    def test_check_model_prints_the_warning_and_exits_0(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _requirement(project, "AR_1", description="")
        [record] = _of(_check(project).warnings, "trace.requirement-blank-text")

        assert _check_model(project, monkeypatch) == 0
        assert f"  WARNING: {record.message}\n" in capsys.readouterr().err


class TestTheVersionWarnings:
    """``trace.link-needs-confirmation`` and ``trace.link-version-not-stated``, on both lists."""

    def test_a_stale_version_on_satisfies(self, project: Path) -> None:
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", version=4)
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@3"])
        _edit(project, _CONSUMER, satisfies=[f"{_REQUIREMENT}@4"])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            (
                "trace.link-needs-confirmation",
                (Subject("swc", "ExampleSWC", (field_step("satisfies"), member_step(f"{_REQUIREMENT}@3"))),),
            )
        ]
        assert "is at version 4" in result.warnings[-1].message

    def test_a_version_wider_than_the_int_digit_limit_is_a_finding_and_no_crash(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schema-valid 4 301-digit suffix: ``int()`` would refuse it, the gate reports it."""
        stale = f"{_REQUIREMENT}@1{'0' * 4300}"
        _edit(project, _PROVIDER, satisfies=[stale])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            (
                "trace.link-needs-confirmation",
                (Subject("swc", "ExampleSWC", (field_step("satisfies"), member_step(stale))),),
            )
        ]
        assert _check_model(project, monkeypatch) == 0

    def test_a_requirement_version_too_wide_to_print_is_rendered(self, project: Path) -> None:
        """A hexadecimal ``version: 0x…`` loads as an integer no decimal string holds; the messages describe it."""
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", version=0)
        _requirement(project, "REQ_UNLINKED", version=0)
        for requirement_id in (_REQUIREMENT, "REQ_UNLINKED"):
            path = _model(project) / "requirements" / f"{requirement_id}.yaml"
            text = path.read_text(encoding="utf-8").replace("version: 0", "version: 0x" + "f" * 4000)
            path.write_text(text, encoding="utf-8", newline="\n")
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1"])

        result = _check(project)

        rules = {record.rule for record in _trace(result.warnings)}
        assert rules == {"trace.link-needs-confirmation", "trace.requirement-unallocated"}
        assert all("too wide to render" in record.message for record in _trace(result.warnings))

    def test_a_stale_version_on_derived_from(self, project: Path) -> None:
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", version=4)
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@4", "REQ_CHILD@1"])
        _edit(project, _CONSUMER, satisfies=[f"{_REQUIREMENT}@4"])
        _requirement(project, "REQ_CHILD", derived_from=[f"{_REQUIREMENT}@3"])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.link-needs-confirmation", (_member("REQ_CHILD", f"{_REQUIREMENT}@3"),))
        ]

    def test_a_bare_id_on_satisfies_of_a_component_and_a_composition(self, project: Path) -> None:
        _edit(project, _PROVIDER, satisfies=[_REQUIREMENT])
        _edit(project, _ROOT, satisfies=[_REQUIREMENT])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            (
                "trace.link-version-not-stated",
                (Subject("swc", "ExampleSWC", (field_step("satisfies"), member_step(_REQUIREMENT))),),
            ),
            (
                "trace.link-version-not-stated",
                (Subject("compositions", "Root", (field_step("satisfies"), member_step(_REQUIREMENT))),),
            ),
        ]
        assert f"write '{_REQUIREMENT}@1'" in result.warnings[-1].message

    def test_the_interface_composition_task_and_hardware_sites_in_layer_order(self, project: Path) -> None:
        _edit(project, f"requirements/{_REQUIREMENT}.yaml", version=2)
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@2"])
        _edit(project, _CONSUMER, satisfies=[f"{_REQUIREMENT}@2"])
        # Written in reverse layer order: the findings come out in the contract's.
        _edit(project, "hardware/CORE0.yaml", satisfies=[f"{_REQUIREMENT}@1"])
        _edit(project, "tasks/Task_10ms.yaml", satisfies=[_REQUIREMENT])
        _edit(project, _ROOT, satisfies=[f"{_REQUIREMENT}@1"])
        _edit(project, "interfaces/IExample.yaml", satisfies=[_REQUIREMENT])

        result = _check(project)

        assert result.consistent is True

        def at(layer: str, element: str, member: str) -> tuple[Subject, ...]:
            return (Subject(layer, element, (field_step("satisfies"), member_step(member))),)

        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.link-version-not-stated", at("interfaces", "IExample", _REQUIREMENT)),
            ("trace.link-needs-confirmation", at("compositions", "Root", f"{_REQUIREMENT}@1")),
            ("trace.link-version-not-stated", at("tasks", "Task_10ms", _REQUIREMENT)),
            ("trace.link-needs-confirmation", at("hardware", "CORE0", f"{_REQUIREMENT}@1")),
        ]
        assert [record.message.split(" satisfies ")[0] for record in _trace(result.warnings)] == [
            "Interface 'IExample'",
            "Composition 'Root'",
            "Task 'Task_10ms'",
            "Hardware element 'CORE0'",
        ]

    def test_a_bare_id_on_derived_from(self, project: Path) -> None:
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "REQ_CHILD@1"])
        _requirement(project, "REQ_CHILD", derived_from=[_REQUIREMENT])

        result = _check(project)

        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.link-version-not-stated", (_member("REQ_CHILD", _REQUIREMENT),))
        ]

    def test_an_id_two_files_declare_raises_no_version_warning(self, project: Path) -> None:
        # Version 2 in the second file: without the abstention `@1` would need confirmation.
        _requirement(project, _REQUIREMENT, file_name=f"{_REQUIREMENT}~1", version=2)
        _edit(project, _CONSUMER, satisfies=[_REQUIREMENT])

        result = _check(project)

        assert [record.rule for record in result.records] == ["name.duplicate-requirement"]
        assert _trace(result.warnings) == []

    @pytest.mark.parametrize("where", ["satisfies", "derived_from"])
    def test_a_malformed_suffix_on_a_declared_id_raises_no_version_warning(self, project: Path, where: str) -> None:
        member = f"{_REQUIREMENT}@x"
        if where == "satisfies":
            _edit(project, _PROVIDER, satisfies=[member])
            expected = "trace.link-unknown-requirement"
        else:
            _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@1", "REQ_CHILD@1"])
            _requirement(project, "REQ_CHILD", derived_from=[member])
            expected = "trace.derived-from-unknown-target"

        result = _check(project)

        assert [record.rule for record in result.records] == [expected]
        assert _trace(result.warnings) == []


class TestTheUnallocatedRequirement:
    """``trace.requirement-unallocated``: no ``satisfies`` names it, whatever derives from it."""

    def test_a_requirement_nothing_satisfies_is_reported_once_in_id_order(self, project: Path) -> None:
        _requirement(project, "REQ_ORPHAN")
        # A derivation is not an allocation: REQ_ORPHAN is still satisfied by nothing.
        _requirement(project, "REQ_CHILD", derived_from=["REQ_ORPHAN@1"])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.requirement-unallocated", (Subject("requirements", "REQ_CHILD"),)),
            ("trace.requirement-unallocated", (Subject("requirements", "REQ_ORPHAN"),)),
        ]
        assert "add `satisfies: [REQ_ORPHAN@1]`" in result.warnings[-1].message

    @pytest.mark.parametrize(
        "relative",
        [_PROVIDER, "interfaces/IExample.yaml", _ROOT, "tasks/Task_10ms.yaml", "hardware/CORE0.yaml"],
        ids=["swc", "interfaces", "compositions", "tasks", "hardware"],
    )
    def test_a_link_on_any_linkable_element_allocates(self, project: Path, relative: str) -> None:
        _requirement(project, "REQ_LINKED")
        document = yaml.safe_load((_model(project) / relative).read_text(encoding="utf-8"))
        _edit(project, relative, satisfies=[*document.get("satisfies", []), "REQ_LINKED@1"])

        assert _trace(_check(project).warnings) == []


class TestTheStubRequirement:
    """``trace.requirement-stub``: a placeholder named as a parent and never imported.

    One warning per stub, in id order, on the requirement itself; a stub
    abstains from ``trace.requirement-unallocated`` (nothing satisfies a
    placeholder, and one placeholder is reported once). A warning refuses
    nothing, so ``check-model`` exits 0 and refuses only under
    ``--warnings-as-errors``.
    """

    def test_one_stub_gives_one_warning_naming_it_and_no_unallocated_finding(self, project: Path) -> None:
        _requirement(project, "SG-1", stub=True, source="external")

        result = _check(project)

        assert result.consistent is True
        assert result.records == ()
        assert [(record.rule, record.subjects, record.message) for record in _trace(result.warnings)] == [
            (
                "trace.requirement-stub",
                (Subject("requirements", "SG-1"),),
                "Requirement 'SG-1' is a stub: named as a parent, never imported.",
            )
        ]

    def test_two_stubs_give_two_warnings_in_id_order(self, project: Path) -> None:
        _requirement(project, "SG-2", stub=True, source="external")
        _requirement(project, "SG-1", stub=True, source="external")
        # A real requirement deriving from both: its own allocation warning stays.
        _requirement(project, "REQ_CHILD", derived_from=["SG-1@1", "SG-2@1"])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.requirement-unallocated", (Subject("requirements", "REQ_CHILD"),)),
            ("trace.requirement-stub", (Subject("requirements", "SG-1"),)),
            ("trace.requirement-stub", (Subject("requirements", "SG-2"),)),
        ]

    def test_a_stub_something_satisfies_is_still_one_stub_warning_and_no_unallocated_finding(
        self, project: Path
    ) -> None:
        _requirement(project, "SG-1", stub=True, source="external")
        _edit(project, _PROVIDER, satisfies=["SG-1@1"])

        result = _check(project)

        assert result.consistent is True
        assert [(record.rule, record.subjects) for record in _trace(result.warnings)] == [
            ("trace.requirement-stub", (Subject("requirements", "SG-1"),)),
        ]

    def test_an_architecture_stub_is_reported_like_an_external_one(self, project: Path) -> None:
        """Nothing ties `stub` to `source`: an authored stub is one warning all the same."""
        _requirement(project, "SG-1", stub=True)

        assert [(record.rule, record.subjects) for record in _trace(_check(project).warnings)] == [
            ("trace.requirement-stub", (Subject("requirements", "SG-1"),)),
        ]

    def test_a_stub_false_requirement_is_no_stub(self, project: Path) -> None:
        _requirement(project, "REQ_REAL", stub=False, source="external")

        assert [record.rule for record in _trace(_check(project).warnings)] == ["trace.requirement-unallocated"]

    def test_check_model_lists_the_stub_and_refuses_it_only_under_warnings_as_errors(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _requirement(project, "SG-1", stub=True, source="external")
        message = "Requirement 'SG-1' is a stub: named as a parent, never imported."

        assert _check_model(project, monkeypatch) == 0
        captured = capsys.readouterr()
        assert captured.err == f"  WARNING: {message}\n0 error(s), 1 warning(s).\n"
        assert captured.out == "Validation passed (no artifacts generated).\n"

        assert _check_model(project, monkeypatch, "--warnings-as-errors") == 1
        captured = capsys.readouterr()
        assert captured.err == (
            f"  WARNING: {message}\n0 error(s), 1 warning(s).\n"
            "Error: 1 warning(s) treated as errors (--warnings-as-errors).\n"
        )


def _operation(layer: str, element: str, **kwargs: Any) -> dict[str, Any]:
    address: dict[str, Any] = {"layer": layer, "element": element}
    if "path" in kwargs:
        address["path"] = kwargs.pop("path")
    return {"address": address, **kwargs}


_WARNING_CHANGES: dict[str, list[dict[str, Any]]] = {
    "trace.requirement-unallocated": [
        _operation(
            "requirements",
            "REQ_PROBE",
            operation="create",
            value={"id": "REQ_PROBE", "title": "Probe", "description": "d", "version": 1, "source": "architecture"},
        )
    ],
    "trace.link-version-not-stated": [
        _operation(
            "requirements",
            "REQ_PROBE",
            operation="create",
            value={
                "id": "REQ_PROBE",
                "title": "Probe",
                "description": "d",
                "version": 1,
                "source": "architecture",
                "derived_from": [_REQUIREMENT],
            },
        )
    ],
    "trace.link-needs-confirmation": [
        _operation("requirements", _REQUIREMENT, operation="update", path=[{"field": "version"}], value=2)
    ],
    "trace.requirement-stub": [
        _operation(
            "requirements",
            "SG-1",
            operation="create",
            value={
                "id": "SG-1",
                "title": "Probe",
                "description": "d",
                "version": 1,
                "source": "external",
                "stub": True,
            },
        )
    ],
    # A warning carries no protocol code, so the change set commits.
    "trace.link-unpublishable-id": [
        _operation(
            "requirements",
            "SYS.REQ.12",
            operation="create",
            value={"id": "SYS.REQ.12", "title": "Probe", "description": "d", "version": 1, "source": "external"},
        ),
        _operation(
            "requirements",
            "REQ_PROBE",
            operation="create",
            value={
                "id": "REQ_PROBE",
                "title": "Probe",
                "description": "d",
                "version": 1,
                "source": "architecture",
                "derived_from": ["SYS.REQ.12@1"],
            },
        ),
    ],
    "trace.requirement-blank-text": [
        _operation(
            "requirements",
            "REQ_PROBE",
            operation="create",
            value={"id": "REQ_PROBE", "title": "Probe", "description": " ", "version": 1, "source": "architecture"},
        )
    ],
}


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)


@pytest.mark.parametrize("rule", WARNING_TRACE_RULES)
def test_each_warning_is_listed_by_check_model_and_committed_by_pass_d(
    rule: str, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = {
        "operation": "change_set",
        "baseline": baseline_token(_model(project)),
        "intent": f"a change set leaving a {rule} warning",
        "operations": _WARNING_CHANGES[rule],
    }

    response = handle_request(request, project=project, approved_scope=None, review=_approve)

    assert response.get("status") == "committed", response
    result = _check(project)
    assert result.consistent is True
    assert result.records == ()
    [record, *_] = _of(result.warnings, rule)
    assert _check_model(project, monkeypatch) == 0
    captured = capsys.readouterr()
    assert f"  WARNING: {record.message}\n" in captured.err
    assert captured.out == "Validation passed (no artifacts generated).\n"


@pytest.mark.parametrize(("field", "value"), [("revision", "1.3"), ("stub", True)])
def test_an_update_of_revision_or_stub_commits_through_the_protocol_and_version_does_not_move(
    field: str, value: Any, project: Path
) -> None:
    """An edit of the provenance or the flag is an ordinary field update.

    It commits through the editing protocol like an edit of `title` (no safety act),
    the file carries the new key, and `version` stays: nothing in SWCraft raises it on
    an edit.
    """
    path = _model(project) / "requirements" / f"{_REQUIREMENT}.yaml"
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    request = {
        "operation": "change_set",
        "baseline": baseline_token(_model(project)),
        "intent": f"record the requirement's {field}",
        "operations": [
            _operation("requirements", _REQUIREMENT, operation="update", path=[{"field": field}], value=value)
        ],
    }

    response = handle_request(request, project=project, approved_scope=None, review=_approve)

    assert response.get("status") == "committed", response
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after == {**before, field: value}
    assert after["version"] == before["version"]


class TestTheClassificationIsShared:
    """The rules classify through ``stale_link`` and ``requirement_allocated`` and mint every finding themselves.

    Those are the helpers the requirements layer's computed values read.

    Replacing a helper changes what the rule reports, which witnesses that the rule reads it;
    the conformance gate (``test_model_contract_conformance.py``) holds that the emission
    stayed in the bound callable.
    """

    def test_the_version_warnings_read_stale_link(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _edit(project, _PROVIDER, satisfies=[f"{_REQUIREMENT}@3"])
        assert _of(_check(project).warnings, "trace.link-needs-confirmation")
        validator_package.patch_everywhere(monkeypatch, "stale_link", lambda _value, _versions: None)
        assert not _of(_check(project).warnings, "trace.link-needs-confirmation")

    def test_the_allocation_warning_reads_requirement_allocated(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not _of(_check(project).warnings, "trace.requirement-unallocated")
        validator_package.patch_everywhere(monkeypatch, "requirement_allocated", lambda _id, _satisfied, _stubs: False)
        assert [record.subjects for record in _of(_check(project).warnings, "trace.requirement-unallocated")] == [
            (Subject("requirements", _REQUIREMENT),)
        ]
