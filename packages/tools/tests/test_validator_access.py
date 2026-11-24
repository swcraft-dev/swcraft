"""`check_runnable_access` — the five cross-element rules over a runnable's access lists.

Reads a runnable *against the rest of the model*: the named port exists on the
component, faces the right way for its list, has an interface of the kind the list
needs, and that interface declares the named element or operation.

**Two rule families, two protocol codes.** The three "no such thing" rules are
`broken-reference`; the two "resolves but faces the wrong way" rules are
`consistency-violation`. Every assertion names the rule id **and** reads the message,
which is what `swcraft check-model` prints.

**One finding per unresolved port, never one per entry**: one `swc.access.unknown-port`
record carries every entry naming the port, and nothing below the port is reported,
so one mistyped port is one repair.

**The maps are built once**: `swc/` and `interfaces/` are each read once however many
entries there are (a read *count*), and the walk is held linear by a growth ratio,
never by seconds.

What this module does **not** reach:

* **the port's own interface reference** — `port.undeclared-interface`'s finding
  (`check_port_interface_references`); accesses through such a port are skipped here
  (asserted below as a cascade guard);
* **the element-local duplicate rule** — refused at load by
  `Runnable.reads_must_not_have_duplicates`;
* **ordering** (not modelled), **the access form ``by`` and a call's ``timeout``** — other checks;
* **the aggregation path's other answers** — only the registration and one end-to-end
  break are asserted here; the rest are `test_validator.py`'s.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from conftest import GOLDEN_MODEL_ROOTS, write_model_element
from growth import LINEAR_GROWTH, growth
from pydantic import TypeAdapter
from swcraft_tools.diagnostics import Subject, SubjectStep, field_step, member_step
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, CONSISTENCY_VIOLATION, RULE_CODES, diagnose
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.schema import Interface, SWComponent
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    ConsistencyResult,
    access,
    check_cross_layer_consistency,
    check_port_interface_references,
    check_runnable_access,
    model_view,
    names,
    runnable_access_records,
)
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_binding,
    load_contract,
)

# ---------------------------------------------------------------------------
# The five rules and the code each was given
# ---------------------------------------------------------------------------

#: Written out rather than derived from the prefix: a rule moving between codes
#: changes what a client is told to repair, and a derivation would follow the move.
ACCESS_CODES: dict[str, str] = {
    "swc.access.unknown-port": BROKEN_REFERENCE,
    "swc.access.unknown-element": BROKEN_REFERENCE,
    "swc.access.unknown-operation": BROKEN_REFERENCE,
    "swc.access.direction-mismatch": CONSISTENCY_VIOLATION,
    "swc.access.interface-kind-mismatch": CONSISTENCY_VIOLATION,
}


# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------

SWC = "Ctl"
RUNNABLE = "Run"


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8", newline="\n")
    else:
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


#: The four ports of the fixture component: a sender/receiver pair and a
#: client/server pair, one of each direction, so every rule has a port that
#: satisfies it and a port that breaks it.
PORTS: list[dict[str, str]] = [
    {"name": "rIn", "direction": "require", "interface": "ISig"},
    {"name": "pOut", "direction": "provide", "interface": "ISig"},
    {"name": "rSrv", "direction": "require", "interface": "ICtl"},
    {"name": "pSrv", "direction": "provide", "interface": "ICtl"},
]


def _swc_document(runnables: list[dict[str, Any]], ports: list[dict[str, str]] = PORTS) -> dict[str, Any]:
    return {"name": SWC, "type": "application", "ports": ports, "runnables": runnables}


def _runnable(name: str = RUNNABLE, **lists: list[dict[str, str]]) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "init"}], **lists}


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """A three-layer model: one base type, one interface of each kind, `Ctl` with an access-free `Run`."""
    root = tmp_path / "model"
    write_model_element(root, {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})
    _write(
        root / "interfaces" / "ISig.yaml",
        {
            "name": "ISig",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "value", "implementation_type": "uint8"},
                {"name": "status", "implementation_type": "uint8"},
            ],
        },
    )
    _write(
        root / "interfaces" / "ICtl.yaml",
        {
            "name": "ICtl",
            "kind": "client_server",
            "operations": [{"name": "Start", "arguments": []}, {"name": "Stop", "arguments": []}],
        },
    )
    _write(root / "swc" / "ctl" / f"{SWC}.yaml", _swc_document([_runnable()]))
    return root


def _declare(model_root: Path, *runnables: dict[str, Any], ports: list[dict[str, str]] = PORTS) -> None:
    """Rewrite the fixture component with *runnables* (default: `Run` alone)."""
    _write(model_root / "swc" / "ctl" / f"{SWC}.yaml", _swc_document(list(runnables) or [_runnable()], ports))


def _records(model_root: Path) -> tuple[Any, ...]:
    return check_runnable_access(str(model_root)).records


def _record(model_root: Path, rule: str) -> Any:
    result = check_runnable_access(str(model_root))
    matching = [record for record in result.records if record.rule == rule]
    assert matching, f"expected {rule!r}, got {sorted({record.rule for record in result.records})}"
    assert len(matching) == 1, f"expected one {rule!r}, got {len(matching)}: {[r.message for r in matching]}"
    return matching[0]


def _entry(list_name: str, identity: dict[str, str], runnable: str = RUNNABLE, *steps: SubjectStep) -> Subject:
    """The subject of one access entry: the member of the runnable's list, by its composite identity."""
    return Subject(
        "swc",
        SWC,
        (field_step("runnables"), member_step(runnable), field_step(list_name), member_step(identity), *steps),
    )


def _port_field(port: str, field: str) -> Subject:
    return Subject("swc", SWC, (field_step("ports"), member_step(port), field_step(field)))


# ---------------------------------------------------------------------------
# 1. The positive vectors
# ---------------------------------------------------------------------------


class TestAConformingRunnableReportsNothing:
    def test_a_runnable_without_lists_is_not_a_finding(self, model_root: Path) -> None:
        assert check_runnable_access(str(model_root)) == ConsistencyResult(consistent=True)

    def test_every_list_populated_the_right_way_round(self, model_root: Path) -> None:
        _declare(
            model_root,
            _runnable(
                reads=[{"port": "rIn", "element": "value"}, {"port": "rIn", "element": "status", "by": "value"}],
                writes=[{"port": "pOut", "element": "value"}, {"port": "pOut", "element": "status"}],
                calls=[{"port": "rSrv", "operation": "Start"}, {"port": "rSrv", "operation": "Stop"}],
            ),
        )
        assert check_runnable_access(str(model_root)).consistent

    def test_by_value_is_no_part_of_the_verdict(self, model_root: Path) -> None:
        # The receive form is a fact about the entry and not about what it
        # resolves to: the same pair by argument and by value answers the same.
        _declare(model_root, _runnable(reads=[{"port": "rIn", "element": "value", "by": "value"}]))
        assert check_runnable_access(str(model_root)).consistent

    def test_one_pair_read_by_one_runnable_and_written_by_another(self, model_root: Path) -> None:
        # Two runnables of one component may see one pair from both sides —
        # nothing here is a rule about the pair, only about each entry.
        _declare(
            model_root,
            _runnable("Reader", reads=[{"port": "rIn", "element": "value"}]),
            _runnable("Writer", writes=[{"port": "pOut", "element": "value"}]),
        )
        assert check_runnable_access(str(model_root)).consistent

    def test_the_committed_example_model_passes(self) -> None:
        # No runnable of `examples/basic` declares an access: the pass must say nothing.
        assert check_runnable_access(str(GOLDEN_MODEL_ROOTS["basic-4.2.2"])).consistent


# ---------------------------------------------------------------------------
# 2. The negative vectors — one per rule and list, with the exact subjects
# ---------------------------------------------------------------------------


def _break(model_root: Path, list_name: str, entry: dict[str, str]) -> None:
    """Rewrite the fixture component with `Run` carrying *entry* in *list_name* alone."""
    _declare(model_root, _runnable(**{list_name: [entry]}))


_READ_GHOST = {"port": "ghost", "element": "value"}
_READ_NOTHING = {"port": "rIn", "element": "nothing"}
_WRITE_NOTHING = {"port": "pOut", "element": "nothing"}
_CALL_REBOOT = {"port": "rSrv", "operation": "Reboot"}
_READ_POUT = {"port": "pOut", "element": "value"}
_WRITE_RIN = {"port": "rIn", "element": "value"}
_CALL_PSRV = {"port": "pSrv", "operation": "Start"}
_READ_RSRV = {"port": "rSrv", "element": "value"}
_WRITE_PSRV = {"port": "pSrv", "element": "value"}
_CALL_RIN = {"port": "rIn", "operation": "Start"}

#: `(label, list, entry, rule, subjects, target, message)`; subjects are per record.
#: A reference rule's subjects are the referrer field alone; a mismatch rule names both
#: legal repairs — the entry that could move lists and the port field that could change.
NEGATIVES: tuple[tuple[str, str, dict[str, str], str, tuple[Subject, ...], str | None, str], ...] = (
    (
        "reads: a port the component does not declare",
        "reads",
        _READ_GHOST,
        "swc.access.unknown-port",
        (_entry("reads", _READ_GHOST, RUNNABLE, field_step("port")),),
        "Ctl.ghost",
        "SWC 'Ctl' declares no port 'ghost', which runnable 'Run' accesses (1 entry).",
    ),
    (
        "reads: an element the interface does not declare",
        "reads",
        _READ_NOTHING,
        "swc.access.unknown-element",
        (_entry("reads", _READ_NOTHING, RUNNABLE, field_step("element")),),
        "ISig.nothing",
        "SWC 'Ctl' runnable 'Run' reads element 'nothing' of port 'rIn', which interface 'ISig' does not declare.",
    ),
    (
        "writes: an element the interface does not declare",
        "writes",
        _WRITE_NOTHING,
        "swc.access.unknown-element",
        (_entry("writes", _WRITE_NOTHING, RUNNABLE, field_step("element")),),
        "ISig.nothing",
        "SWC 'Ctl' runnable 'Run' writes element 'nothing' of port 'pOut', which interface 'ISig' does not declare.",
    ),
    (
        "calls: an operation the interface does not declare",
        "calls",
        _CALL_REBOOT,
        "swc.access.unknown-operation",
        (_entry("calls", _CALL_REBOOT, RUNNABLE, field_step("operation")),),
        "ICtl.Reboot",
        "SWC 'Ctl' runnable 'Run' calls operation 'Reboot' of port 'rSrv', which interface 'ICtl' does not declare.",
    ),
    (
        "reads: a provide port",
        "reads",
        _READ_POUT,
        "swc.access.direction-mismatch",
        (_entry("reads", _READ_POUT), _port_field("pOut", "direction")),
        None,
        "SWC 'Ctl' runnable 'Run' reads port 'pOut', which is a provide port; `reads` needs a require port.",
    ),
    (
        "writes: a require port",
        "writes",
        _WRITE_RIN,
        "swc.access.direction-mismatch",
        (_entry("writes", _WRITE_RIN), _port_field("rIn", "direction")),
        None,
        "SWC 'Ctl' runnable 'Run' writes port 'rIn', which is a require port; `writes` needs a provide port.",
    ),
    (
        "calls: a provide port",
        "calls",
        _CALL_PSRV,
        "swc.access.direction-mismatch",
        (_entry("calls", _CALL_PSRV), _port_field("pSrv", "direction")),
        None,
        "SWC 'Ctl' runnable 'Run' calls port 'pSrv', which is a provide port; `calls` needs a require port.",
    ),
    (
        "reads: a client/server port",
        "reads",
        _READ_RSRV,
        "swc.access.interface-kind-mismatch",
        (_entry("reads", _READ_RSRV), _port_field("rSrv", "interface")),
        None,
        "SWC 'Ctl' runnable 'Run' reads port 'rSrv', whose interface 'ICtl' is client/server; "
        "`reads` needs a sender/receiver port.",
    ),
    (
        "writes: a client/server port",
        "writes",
        _WRITE_PSRV,
        "swc.access.interface-kind-mismatch",
        (_entry("writes", _WRITE_PSRV), _port_field("pSrv", "interface")),
        None,
        "SWC 'Ctl' runnable 'Run' writes port 'pSrv', whose interface 'ICtl' is client/server; "
        "`writes` needs a sender/receiver port.",
    ),
    (
        "calls: a sender/receiver port",
        "calls",
        _CALL_RIN,
        "swc.access.interface-kind-mismatch",
        (_entry("calls", _CALL_RIN), _port_field("rIn", "interface")),
        None,
        "SWC 'Ctl' runnable 'Run' calls port 'rIn', whose interface 'ISig' is sender/receiver; "
        "`calls` needs a client/server port.",
    ),
)

_NEGATIVE_IDS = [f"{entry[3]}[{entry[0].split(':', 1)[0]}]" for entry in NEGATIVES]


class TestEachRuleFiresAloneWithItsSubjectsAndItsCode:
    @pytest.mark.parametrize(
        ("label", "list_name", "entry", "rule", "subjects", "target", "message"), NEGATIVES, ids=_NEGATIVE_IDS
    )
    def test_the_rule_and_nothing_else(
        self,
        model_root: Path,
        label: str,
        list_name: str,
        entry: dict[str, str],
        rule: str,
        subjects: tuple[Subject, ...],
        target: str | None,
        message: str,
    ) -> None:
        # Equality, not membership: a fixture that stops isolating its rule must fail.
        _break(model_root, list_name, entry)
        result = check_runnable_access(str(model_root))
        assert not result.consistent, label
        assert [record.rule for record in result.records] == [rule], (
            f"{label}: expected {rule!r} and nothing else, got {[r.rule for r in result.records]}"
        )

    @pytest.mark.parametrize(
        ("label", "list_name", "entry", "rule", "subjects", "target", "message"), NEGATIVES, ids=_NEGATIVE_IDS
    )
    def test_the_record_names_its_places_its_target_and_its_message(
        self,
        model_root: Path,
        label: str,
        list_name: str,
        entry: dict[str, str],
        rule: str,
        subjects: tuple[Subject, ...],
        target: str | None,
        message: str,
    ) -> None:
        _break(model_root, list_name, entry)
        record = _record(model_root, rule)
        assert record.subjects == subjects, label
        assert record.target == target, label
        assert record.message == message, label
        assert check_runnable_access(str(model_root)).issues == [message], label

    @pytest.mark.parametrize(
        ("label", "list_name", "entry", "rule", "subjects", "target", "message"), NEGATIVES, ids=_NEGATIVE_IDS
    )
    def test_the_protocol_answers_with_the_code_the_rule_was_given_at_an_address(
        self,
        model_root: Path,
        label: str,
        list_name: str,
        entry: dict[str, str],
        rule: str,
        subjects: tuple[Subject, ...],
        target: str | None,
        message: str,
    ) -> None:
        _break(model_root, list_name, entry)
        violations = diagnose(check_runnable_access(str(model_root)))
        assert [violation.error for violation in violations] == [ACCESS_CODES[rule]], label
        assert all(violation.addresses for violation in violations), label


# ---------------------------------------------------------------------------
# 3. One finding per unresolved port, and the cascade guards
# ---------------------------------------------------------------------------


class TestAnUnresolvedPortIsOneFinding:
    def test_every_entry_naming_the_port_rides_on_one_record(self, model_root: Path) -> None:
        """Five entries across the three lists, one record, five subjects, in list order."""
        _declare(
            model_root,
            _runnable(
                reads=[
                    {"port": "ghost", "element": "a"},
                    {"port": "rIn", "element": "value"},
                    {"port": "ghost", "element": "b"},
                ],
                writes=[{"port": "ghost", "element": "c"}, {"port": "ghost", "element": "d"}],
                calls=[{"port": "ghost", "operation": "Op"}],
            ),
        )
        result = check_runnable_access(str(model_root))
        assert [record.rule for record in result.records] == ["swc.access.unknown-port"]
        record = result.records[0]
        assert record.target == "Ctl.ghost"
        assert record.subjects == (
            _entry("reads", {"port": "ghost", "element": "a"}, RUNNABLE, field_step("port")),
            _entry("reads", {"port": "ghost", "element": "b"}, RUNNABLE, field_step("port")),
            _entry("writes", {"port": "ghost", "element": "c"}, RUNNABLE, field_step("port")),
            _entry("writes", {"port": "ghost", "element": "d"}, RUNNABLE, field_step("port")),
            _entry("calls", {"port": "ghost", "operation": "Op"}, RUNNABLE, field_step("port")),
        )
        assert record.message == "SWC 'Ctl' declares no port 'ghost', which runnable 'Run' accesses (5 entries)."

    def test_two_runnables_naming_one_ghost_port_are_one_record_naming_both(self, model_root: Path) -> None:
        _declare(
            model_root,
            _runnable("First", reads=[{"port": "ghost", "element": "value"}]),
            _runnable("Second", writes=[{"port": "ghost", "element": "value"}]),
        )
        record = _record(model_root, "swc.access.unknown-port")
        assert record.subjects == (
            _entry("reads", {"port": "ghost", "element": "value"}, "First", field_step("port")),
            _entry("writes", {"port": "ghost", "element": "value"}, "Second", field_step("port")),
        )
        assert record.message == (
            "SWC 'Ctl' declares no port 'ghost', which runnables 'First' and 'Second' access (2 entries)."
        )

    def test_three_runnables_naming_one_ghost_port_render_the_three_name_join(self, model_root: Path) -> None:
        # The three-plus arm of `accessing_runnables`, the only one rendering the comma-joined head.
        _declare(
            model_root,
            _runnable("A", reads=[{"port": "ghost", "element": "value"}]),
            _runnable("B", writes=[{"port": "ghost", "element": "value"}]),
            _runnable("C", calls=[{"port": "ghost", "operation": "Op"}]),
        )
        record = _record(model_root, "swc.access.unknown-port")
        assert record.message == (
            "SWC 'Ctl' declares no port 'ghost', which runnables 'A', 'B' and 'C' access (3 entries)."
        )

    def test_two_ghost_ports_are_two_records(self, model_root: Path) -> None:
        _declare(
            model_root,
            _runnable(reads=[{"port": "ghost", "element": "value"}, {"port": "phantom", "element": "value"}]),
        )
        records = _records(model_root)
        assert [(record.rule, record.target) for record in records] == [
            ("swc.access.unknown-port", "Ctl.ghost"),
            ("swc.access.unknown-port", "Ctl.phantom"),
        ]

    def test_the_same_ghost_port_on_two_components_is_one_record_each(self, model_root: Path) -> None:
        # The finding is per (component, port): a second component's ghost
        # port is a different target and a different repair.
        _write(
            model_root / "swc" / "ctl" / "Other.yaml",
            {
                "name": "Other",
                "type": "application",
                "ports": [],
                "runnables": [_runnable("Run", reads=[{"port": "ghost", "element": "value"}])],
            },
        )
        _declare(model_root, _runnable(reads=[{"port": "ghost", "element": "value"}]))
        assert sorted(record.target for record in _records(model_root)) == ["Ctl.ghost", "Other.ghost"]

    def test_the_protocol_merges_the_referrers_under_the_one_target(self, model_root: Path) -> None:
        _declare(
            model_root,
            _runnable(reads=[{"port": "ghost", "element": "a"}, {"port": "ghost", "element": "b"}]),
        )
        violations = diagnose(check_runnable_access(str(model_root)))
        assert len(violations) == 1
        assert violations[0].error == BROKEN_REFERENCE
        assert len(violations[0].addresses) == 2


class TestTheCascadeGuards:
    def test_a_port_whose_interface_is_undeclared_is_the_port_rules_finding_and_not_this_passs(
        self, model_root: Path
    ) -> None:
        ports = [*PORTS, {"name": "rLost", "direction": "require", "interface": "IMissing"}]
        _declare(model_root, _runnable(reads=[{"port": "rLost", "element": "value"}]), ports=ports)

        assert check_runnable_access(str(model_root)).consistent
        assert [record.rule for record in check_port_interface_references(str(model_root)).records] == [
            "port.undeclared-interface"
        ]

    def test_an_interface_kind_mismatch_reports_no_element_finding(self, model_root: Path) -> None:
        # `value` is not an operation of `ICtl`, but a read through a
        # client/server port cannot resolve an element at all: one finding.
        _declare(model_root, _runnable(reads=[{"port": "rSrv", "element": "Start"}]))
        assert [record.rule for record in _records(model_root)] == ["swc.access.interface-kind-mismatch"]

    def test_an_entry_wrong_on_both_counts_reports_the_interface_kind_alone(self, model_root: Path) -> None:
        # The kind is decided before the direction; `arxml_import.swc_mapping`
        # decides in the same order (both consume ACCESS_LIST_NEEDS).
        _declare(model_root, _runnable(reads=[{"port": "pSrv", "element": "value"}]))
        assert [record.rule for record in _records(model_root)] == ["swc.access.interface-kind-mismatch"]

    def test_a_direction_mismatch_and_an_unknown_element_are_two_independent_findings(self, model_root: Path) -> None:
        # Wrong direction AND missing element: two facts (port, interface), two records.
        _declare(model_root, _runnable(reads=[{"port": "pOut", "element": "nothing"}]))
        assert [record.rule for record in _records(model_root)] == [
            "swc.access.direction-mismatch",
            "swc.access.unknown-element",
        ]

    def test_a_direction_mismatch_and_an_unknown_operation_are_two_independent_findings(self, model_root: Path) -> None:
        # The `calls` twin of the case above: a call through a provide port
        # of an operation the interface lacks.
        _declare(model_root, _runnable(calls=[{"port": "pSrv", "operation": "Reboot"}]))
        assert [record.rule for record in _records(model_root)] == [
            "swc.access.direction-mismatch",
            "swc.access.unknown-operation",
        ]

    def test_the_port_findings_of_a_component_trail_its_other_findings(self, model_root: Path) -> None:
        # `unknown-port` is emitted after the component's walk, after its other
        # findings, whatever the entry order in the file.
        _declare(
            model_root,
            _runnable(
                reads=[{"port": "ghost", "element": "value"}, {"port": "pOut", "element": "value"}],
            ),
        )
        assert [record.rule for record in _records(model_root)] == [
            "swc.access.direction-mismatch",
            "swc.access.unknown-port",
        ]

    def test_records_come_in_runnable_then_list_then_entry_order(self, model_root: Path) -> None:
        _declare(
            model_root,
            _runnable(
                "A",
                reads=[{"port": "pOut", "element": "value"}],
                calls=[{"port": "rSrv", "operation": "Reboot"}],
            ),
            _runnable("B", writes=[{"port": "rIn", "element": "value"}]),
        )
        assert [(record.rule, record.subjects[0].path[1].value) for record in _records(model_root)] == [
            ("swc.access.direction-mismatch", "A"),
            ("swc.access.unknown-operation", "A"),
            ("swc.access.direction-mismatch", "B"),
        ]

    def test_the_first_interface_of_a_duplicated_name_is_the_one_resolved(self, model_root: Path) -> None:
        # `name.duplicate-interface` owns the duplicate; the `setdefault` map never
        # consults the second `ISig` (a client/server one that would mismatch).
        _write(
            model_root / "interfaces" / "ISig_again.yaml",
            {"name": "ISig", "kind": "client_server", "operations": [{"name": "Start", "arguments": []}]},
        )
        _declare(model_root, _runnable(reads=[{"port": "rIn", "element": "value"}]))
        assert check_runnable_access(str(model_root)).consistent

    def test_the_first_port_of_a_duplicated_name_is_the_one_resolved(self, model_root: Path) -> None:
        # `name.duplicate-port` owns the duplicate; this pass reads the layer
        # in reader order and resolves the first, like every sibling map.
        ports = [
            {"name": "twin", "direction": "require", "interface": "ISig"},
            {"name": "twin", "direction": "provide", "interface": "ICtl"},
        ]
        _declare(model_root, _runnable(reads=[{"port": "twin", "element": "value"}]), ports=ports)
        assert check_runnable_access(str(model_root)).consistent


class TestTheInMemoryCoreIsTheDiskCheck:
    """The disk check is its in-memory core over what it reads.

    ``import component --update`` asks :func:`runnable_access_records`; ``check-model``
    asks :func:`check_runnable_access`. Every negative vector and the clean fixture
    answer the same both ways.
    """

    @staticmethod
    def _core(model_root: Path) -> list[Any]:
        return runnable_access_records(read_swc_model(str(model_root)), read_interfaces(str(model_root)))

    def test_the_clean_fixture(self, model_root: Path) -> None:
        assert self._core(model_root) == []
        assert list(check_runnable_access(str(model_root)).records) == self._core(model_root)

    @pytest.mark.parametrize(
        ("label", "list_name", "entry"),
        [(label, list_name, entry) for label, list_name, entry, *_ in NEGATIVES],
        ids=[label for label, *_ in NEGATIVES],
    )
    def test_each_negative_vector(self, model_root: Path, label: str, list_name: str, entry: dict[str, str]) -> None:
        _break(model_root, list_name, entry)
        core = self._core(model_root)
        assert core, label
        assert list(check_runnable_access(str(model_root)).records) == core

    def test_the_first_declared_interface_answers(self, model_root: Path) -> None:
        """The first-declaration precedence is the core's: a second interface of one name is not read."""
        _declare(model_root, _runnable(reads=[{"port": "rIn", "element": "value"}]))
        interfaces = read_interfaces(str(model_root))
        signals = next(interface for interface in interfaces if interface.name == "ISig")
        emptied = signals.model_copy(update={"data_elements": []})
        swcs = read_swc_model(str(model_root))

        assert runnable_access_records(swcs, [*interfaces, emptied]) == []
        assert [record.rule for record in runnable_access_records(swcs, [emptied, *interfaces])] == [
            "swc.access.unknown-element"
        ]


# ---------------------------------------------------------------------------
# 3a. The acceptance edge cases
# ---------------------------------------------------------------------------


class TestTheAcceptanceEdgeCases:
    """Three edge cases of this pass: one list alone, one pair on both sides, an empty port list.

    The on-disk half of "one list alone" is
    `test_writer_behavior.py::test_one_list_alone_is_written_alone`'s.
    """

    @pytest.mark.parametrize(
        ("list_name", "entry"),
        [
            ("reads", {"port": "rIn", "element": "value"}),
            ("writes", {"port": "pOut", "element": "value"}),
            ("calls", {"port": "rSrv", "operation": "Start"}),
        ],
        ids=["reads", "writes", "calls"],
    )
    def test_one_list_alone_is_no_finding(self, model_root: Path, list_name: str, entry: dict[str, str]) -> None:
        _declare(model_root, _runnable(**{list_name: [entry]}))
        assert check_runnable_access(str(model_root)) == ConsistencyResult(consistent=True)

    def test_the_same_pair_from_both_sides_is_one_direction_finding_on_the_write(self, model_root: Path) -> None:
        # One port faces one way, so the same `(port, element)` on both sides is
        # one finding — the entry on the wrong side — and the other entry is untouched.
        _declare(
            model_root,
            _runnable("Reader", reads=[{"port": "rIn", "element": "value"}]),
            _runnable("Writer", writes=[{"port": "rIn", "element": "value"}]),
        )
        records = _records(model_root)
        assert [record.rule for record in records] == ["swc.access.direction-mismatch"]
        assert records[0].subjects == (
            _entry("writes", {"port": "rIn", "element": "value"}, "Writer"),
            _port_field("rIn", "direction"),
        )
        assert records[0].message == (
            "SWC 'Ctl' runnable 'Writer' writes port 'rIn', which is a require port; `writes` needs a provide port."
        )

    def test_a_component_without_ports_reports_every_entry_under_the_port_rule_and_no_other(
        self, model_root: Path
    ) -> None:
        # With no ports, `unknown-port` is the only rule that can fire; three
        # entries naming three ports are three records.
        _declare(
            model_root,
            _runnable(
                reads=[{"port": "rIn", "element": "value"}],
                writes=[{"port": "pOut", "element": "value"}],
                calls=[{"port": "rSrv", "operation": "Start"}],
            ),
            ports=[],
        )
        records = _records(model_root)
        assert [(record.rule, record.target) for record in records] == [
            ("swc.access.unknown-port", "Ctl.rIn"),
            ("swc.access.unknown-port", "Ctl.pOut"),
            ("swc.access.unknown-port", "Ctl.rSrv"),
        ]
        assert [record.subjects for record in records] == [
            (_entry("reads", {"port": "rIn", "element": "value"}, RUNNABLE, field_step("port")),),
            (_entry("writes", {"port": "pOut", "element": "value"}, RUNNABLE, field_step("port")),),
            (_entry("calls", {"port": "rSrv", "operation": "Start"}, RUNNABLE, field_step("port")),),
        ]

    def test_a_component_without_ports_naming_one_port_three_times_is_one_record(self, model_root: Path) -> None:
        # One port name across the three lists: one record carrying the three entries.
        _declare(
            model_root,
            _runnable(
                reads=[{"port": "io", "element": "value"}],
                writes=[{"port": "io", "element": "value"}],
                calls=[{"port": "io", "operation": "Start"}],
            ),
            ports=[],
        )
        records = _records(model_root)
        assert len(records) == 1
        record = records[0]
        assert (record.rule, record.target) == ("swc.access.unknown-port", "Ctl.io")
        assert len(record.subjects) == 3
        assert record.message == "SWC 'Ctl' declares no port 'io', which runnable 'Run' accesses (3 entries)."


# ---------------------------------------------------------------------------
# 4. The maps are built once
# ---------------------------------------------------------------------------

#: Enough entries that a per-entry layer read would be a thousand reads.
_WIDE_PAIRS = 250
_WIDE_CALLS = 100


def _counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Route the view's *name* reader through a counter; the real reader still runs."""
    calls: list[str] = []
    real = getattr(model_view, name)

    def counted(model_root: str):  # type: ignore[no-untyped-def]
        calls.append(model_root)
        return real(model_root)

    monkeypatch.setattr(model_view, name, counted)
    return calls


def test_the_walk_reads_each_layer_exactly_once_however_many_entries(
    model_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [
        *(
            {"name": f"{prefix}{i}", "direction": direction, "interface": "ISig"}
            for i in range(_WIDE_PAIRS)
            for prefix, direction in (("r", "require"), ("p", "provide"))
        ),
        *({"name": f"s{i}", "direction": "require", "interface": "ICtl"} for i in range(_WIDE_CALLS)),
    ]
    _declare(
        model_root,
        _runnable(
            reads=[{"port": f"r{i}", "element": "value"} for i in range(_WIDE_PAIRS)],
            writes=[{"port": f"p{i}", "element": "status"} for i in range(_WIDE_PAIRS)],
            calls=[{"port": f"s{i}", "operation": "Start"} for i in range(_WIDE_CALLS)],
        ),
        ports=ports,
    )
    swc_reads = _counting(monkeypatch, "read_swc_model")
    interface_reads = _counting(monkeypatch, "read_interfaces")

    result = check_runnable_access(str(model_root))

    assert result.consistent, result.issues
    entries = 2 * _WIDE_PAIRS + _WIDE_CALLS
    assert entries == 600
    assert len(swc_reads) == 1, f"{len(swc_reads)} `swc/` reads for {entries} entries — a per-entry re-read"
    assert len(interface_reads) == 1, f"{len(interface_reads)} `interfaces/` reads for {entries} entries"


#: Components at scale 1 of the growth ratio below, three entries each: the smallest power-of-two
#: multiple of 100 whose walk takes at least 2 ms on the Mac the sizing was taken on.
_SCALED_COMPONENTS = 400

_INTERFACE = TypeAdapter(Interface)


def _scaled_model(components: int) -> tuple[list[SWComponent], list[Interface]]:
    """*components* components whose every access entry resolves, so nothing is reported.

    Each has a sender/receiver and a client/server interface of its own and one runnable that reads, writes and calls
    through three ports."""
    swcs: list[SWComponent] = []
    interfaces: list[Interface] = []
    for i in range(components):
        interfaces.append(
            _INTERFACE.validate_python(
                {
                    "name": f"ISig{i}",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "uint8"}],
                }
            )
        )
        interfaces.append(
            _INTERFACE.validate_python(
                {"name": f"ICtl{i}", "kind": "client_server", "operations": [{"name": "Start", "arguments": []}]}
            )
        )
        swcs.append(
            SWComponent.model_validate(
                {
                    "name": f"C{i}",
                    "type": "application",
                    "ports": [
                        {"name": "rIn", "direction": "require", "interface": f"ISig{i}"},
                        {"name": "pOut", "direction": "provide", "interface": f"ISig{i}"},
                        {"name": "rSrv", "direction": "require", "interface": f"ICtl{i}"},
                    ],
                    "runnables": [
                        _runnable(
                            reads=[{"port": "rIn", "element": "value"}],
                            writes=[{"port": "pOut", "element": "value"}],
                            calls=[{"port": "rSrv", "operation": "Start"}],
                        )
                    ],
                }
            )
        )
    return swcs, interfaces


def test_the_walk_is_linear_in_the_entries() -> None:
    """Four times the components and entries cost at most :data:`growth.LINEAR_GROWTH` times the time.

    The read count above holds that each layer is read once; this holds what the maps buy once read: an entry
    resolves its port and interface by lookup, never by a search over the sequences, which a per-entry
    ``next(...)`` would make quadratic. Each component has interfaces of its own, so such a search grows with
    the scale. The models are built outside the timed call.
    """
    models = {scale: _scaled_model(_SCALED_COMPONENTS * scale) for scale in (1, 4)}
    assert all(runnable_access_records(*model) == [] for model in models.values())

    ratio = growth(lambda scale: runnable_access_records(*models[scale]))

    assert ratio < LINEAR_GROWTH, f"the walk grew {ratio:.1f} times for four times the entries"


# ---------------------------------------------------------------------------
# 5. Wiring: contract, validator, codes, binding, aggregation — both directions
# ---------------------------------------------------------------------------


def _access_rules_of(keys: Any) -> set[str]:
    return {key for key in keys if key.startswith("swc.access.")}


#: An identifier under the `swc.access.` prefix that `check_runnable_access` does
#: **not** mint: it judges the *name* two entries derive, so it is
#: `check_name_uniqueness`'s. Named so no gate reads the prefix as the check.
COLLISION_RULE = "swc.access.name-collision"
#: The `[constr_1277]` rule under the prefix: minted by `queued_read_records` (through
#: `check_queued_reads`) over a read's receive form and the element's policy, never by
#: `check_runnable_access`.
QUEUED_READ_RULE = "swc.access.queued-read-by-value"

#: The implicit read's two rules, minted outside the access walk.
IMPLICIT_READ_RULES = frozenset({"swc.access.queued-implicit-read", "swc.access.implicit-read-enable-update"})


class TestTheFiveAreDeclaredEverywhereAndNowhereElse:
    """The five are pinned by name in contract, validator and `RULE_CODES`.

    The equality gates elsewhere would accept a rule dropped from every table at once.
    The `swc.access.` prefix is stated as a partition: the five, :data:`COLLISION_RULE`,
    :data:`QUEUED_READ_RULE` and :data:`IMPLICIT_READ_RULES`, and nothing else — an
    equality against the prefix would accept a new pass rule silently.
    """

    def test_the_validator_declares_exactly_the_five_beside_the_collision_rule(self) -> None:
        others = {COLLISION_RULE, QUEUED_READ_RULE} | IMPLICIT_READ_RULES
        assert _access_rules_of(CONSISTENCY_RULES) == set(ACCESS_CODES) | others
        assert _access_rules_of(CONSISTENCY_RULES) - set(ACCESS_CODES) == others

    def test_the_contract_declares_exactly_the_five_as_cross_element(self) -> None:
        declared = {
            rule["id"][len("rule:") :]: rule
            for rule in load_contract()["rules"]
            if rule["id"].startswith("rule:swc.access.")
        }
        assert set(declared) == set(ACCESS_CODES) | {COLLISION_RULE, QUEUED_READ_RULE} | IMPLICIT_READ_RULES
        assert {rule["family"] for rule in declared.values()} == {"cross-element"}
        # The summary is the validator's description verbatim, not a paraphrase.
        assert {key: rule["summary"] for key, rule in declared.items() if key in ACCESS_CODES} == {
            key: CONSISTENCY_RULES[key] for key in ACCESS_CODES
        }
        assert declared[COLLISION_RULE]["summary"] == CONSISTENCY_RULES[COLLISION_RULE]
        assert declared[QUEUED_READ_RULE]["summary"] == CONSISTENCY_RULES[QUEUED_READ_RULE]

    def test_the_protocol_table_declares_exactly_the_five_beside_the_collision_rule(self) -> None:
        assert (
            _access_rules_of(RULE_CODES) == set(ACCESS_CODES) | {COLLISION_RULE, QUEUED_READ_RULE} | IMPLICIT_READ_RULES
        )

    @pytest.mark.parametrize("rule", sorted(ACCESS_CODES))
    def test_each_one_is_answered_with_the_code_it_was_given(self, rule: str) -> None:
        assert RULE_CODES[rule].code == ACCESS_CODES[rule]

    def test_the_three_reference_rules_group_apart(self) -> None:
        """A port, an element and an operation are three repairs: each rule groups with itself alone.

        A shared group would merge a missing port with a missing element of the same target string.
        """
        groups = {rule: RULE_CODES[rule].group for rule in ACCESS_CODES if ACCESS_CODES[rule] == BROKEN_REFERENCE}
        assert len(set(groups.values())) == 3
        elsewhere = {mapping.group for rule, mapping in RULE_CODES.items() if rule not in ACCESS_CODES}
        assert set(groups.values()) & elsewhere == set()

    def test_every_one_is_bound_to_the_pass_that_mints_it(self) -> None:
        rules = load_binding()["rules"]
        for rule in ACCESS_CODES:
            # The walk mints the unknown port; its per-entry phase, which the walk calls, the other four.
            minted_by = "runnable_access_records" if rule == "swc.access.unknown-port" else "_access_entry_records"
            assert rules[f"rule:{rule}"] == {
                "module": "swcraft_tools.validator.access",
                "callable": minted_by,
            }
            assert minted_by in validator_package.reached_functions(access, "runnable_access_records")
        # The phase of `check_name_uniqueness` that mints it, which the check calls.
        assert rules[f"rule:{COLLISION_RULE}"] == {
            "module": "swcraft_tools.validator.names",
            "callable": "_component_member_name_records",
        }
        assert "_component_member_name_records" in validator_package.reached_functions(names, "check_name_uniqueness")
        assert rules[f"rule:{QUEUED_READ_RULE}"] == {
            "module": "swcraft_tools.validator.behaviour",
            "callable": "queued_read_records",
        }


@pytest.fixture(scope="module")
def observed() -> dict[str, list[frozenset[str] | None]]:
    """The contract's emission scan, run once for the module."""
    return emission_layers()


class TestTheRulesAreEmittedWhereTheContractSaysTheyAre:
    """The contract's own emission scan, narrowed to the five.

    A subject expression the scan cannot read (the grouped unknown-port finding is a
    comprehension) is named here rather than lost in the whole scan's diff.
    """

    @pytest.mark.parametrize("rule", sorted(ACCESS_CODES))
    def test_the_scan_reads_every_subject_of_the_rule(
        self, rule: str, observed: dict[str, list[frozenset[str] | None]]
    ) -> None:
        sites = observed.get(f"rule:{rule}")
        assert sites, f"{rule} is emitted nowhere the scan can see"
        assert all(site is not None for site in sites), f"{rule} has a subject expression the scan cannot read"
        # Every subject is the referring component; the interface a target is
        # missing from is named by the rule, never by a subject.
        assert all(site == frozenset({"swc"}) for site in sites), sites

    def test_no_access_rule_reports_at_a_layer_it_does_not_declare(
        self, observed: dict[str, list[frozenset[str] | None]]
    ) -> None:
        failures = [f for f in cross_element_layer_failures(load_contract(), observed) if "rule:swc.access." in f]
        assert failures == []

    def test_the_two_target_rules_declare_the_interface_side_the_subjects_cannot_reach(self) -> None:
        """The interface side of `appliesTo`, reachable from no subject, is hand-authored and pinned by name."""
        applies = {rule["id"]: set(rule["appliesTo"]) for rule in load_contract()["rules"]}
        assert {"field:interfaces.sender_receiver.data_elements", "field:interfaces.data_element.name"} <= applies[
            "rule:swc.access.unknown-element"
        ]
        assert {"field:interfaces.client_server.operations", "field:interfaces.operation.name"} <= applies[
            "rule:swc.access.unknown-operation"
        ]
        assert {"field:swc.port.name"} <= applies["rule:swc.access.unknown-port"]
        assert {"field:swc.port.direction"} <= applies["rule:swc.access.direction-mismatch"]
        assert {"field:interfaces.sender_receiver.kind", "field:interfaces.client_server.kind"} <= applies[
            "rule:swc.access.interface-kind-mismatch"
        ]


class TestThePassIsWiredIn:
    def test_it_runs_after_the_port_interface_check(self, tmp_path: Path) -> None:
        """The port's own finding precedes an access finding in the aggregate, so the report reads in cause order.

        Does not reach: the order on a model where only one of the two passes reports.
        """
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
        path = model_root / "swc" / "example" / "ExampleConsumerSWC.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["ports"].append({"name": "rLost", "direction": "require", "interface": "INoSuchInterface"})
        document["runnables"][0]["reads"] = [{"port": "ghost", "element": "value"}]
        _write(path, document)

        rules = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert "port.undeclared-interface" in rules and "swc.access.unknown-port" in rules, rules
        port = rules.index("port.undeclared-interface")
        assert all(port < index for index, rule in enumerate(rules) if rule.startswith("swc.access.")), rules

    def test_an_access_break_reaches_the_aggregate_and_the_protocol(self, tmp_path: Path) -> None:
        """End to end over the example model: one ghost port, one rule, one code."""
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
        path = model_root / "swc" / "example" / "ExampleConsumerSWC.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["runnables"][0]["reads"] = [
            {"port": "rIn", "element": "value"},
            {"port": "ghost", "element": "value"},
        ]
        _write(path, document)

        result = check_cross_layer_consistency(str(model_root))
        assert [record.rule for record in result.records] == ["swc.access.unknown-port"]
        violations = diagnose(result)
        assert [violation.error for violation in violations] == [BROKEN_REFERENCE]
        assert violations[0].addresses

    def test_a_valid_access_over_the_example_model_is_no_finding_at_all(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
        path = model_root / "swc" / "example" / "ExampleConsumerSWC.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["runnables"][0]["reads"] = [{"port": "rIn", "element": "value", "by": "value"}]
        _write(path, document)

        assert check_cross_layer_consistency(str(model_root)).consistent
