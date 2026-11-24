"""`check_component_asil` — the one cross-element safety rule.

`rule:safety.component-below-requirements` reports a component that **declares** an
`asil` below the maximum `asil` of the requirements it satisfies — through its own
`satisfies`, a port's or a runnable's. It is `[constr_6201]`'s direction and not a
symmetry: equal or higher is legitimate and only lower is the violation.

**An absent field and a written `QM` are two states.** Neither field has a default:
a component declaring nothing loads as `None` and *derives* its level (the maximum of
the requirements allocated to it, so never reported); one writing `asil: QM` has
**declared** that no ASIL is allocated to it and **is** reported under a requirement
carrying one — `[constr_6201]`'s own case.

**An external stub is judged like any other component.** Its neighbours
(`resource_analysis.py`'s per-memory walk, the ARXML writer) skip it because this project
builds no stub: its ports carry no interface and it has no runnables or budget; `asil` is
a value the project wrote into the stub's file, and a stub may satisfy a requirement too.

What this module does **not** reach:

* **the unknown-link case is not its finding.** A `satisfies` member naming no
  declared requirement is `trace.link-unknown-requirement`'s, and this check abstains
  (asserted as a cascade guard below);
* **the enum itself.** A value outside `QM`/`A`…`D` and the thirteen decomposed `X(Y)` forms
  is a *load* failure and never reaches a check;
* **whether the allocation is right.** The rule compares two of the project's own
  declarations; a model that passes it is not thereby safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_tools.diagnostics import Subject, field_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    ConsistencyResult,
    check_component_asil,
    check_cross_layer_consistency,
    effective_asil_levels,
    model_view,
)
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

RULE = "safety.component-below-requirements"


# ---------------------------------------------------------------------------
# Model fixtures — two layers and nothing else
# ---------------------------------------------------------------------------


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _swc(model_root: Path, name: str, **extra: Any) -> None:
    """A component file; a ``satisfies`` an earlier :func:`_requirement` wrote into it is kept."""
    path = model_root / "swc" / "group" / f"{name}.yaml"
    kept = yaml.safe_load(path.read_text(encoding="utf-8")).get("satisfies") if path.is_file() else None
    _write(path, {"name": name, "type": "application", **({"satisfies": kept} if kept else {}), **extra})


def _requirement(model_root: Path, req_id: str, links: list[str], **extra: Any) -> None:
    """A requirement, and *links* -- the components that satisfy it -- written where the link lives.

    The link is the component's own ``satisfies`` member ``<req_id>@1``, appended to
    every component file declaring each named component (a repeated name appends
    twice). A name no file declares is skipped, so the requirement allocates to nothing.
    """
    _write(
        model_root / "requirements" / f"{req_id}.yaml",
        {
            "id": req_id,
            "title": f"{req_id} title",
            "description": f"The system shall satisfy {req_id}.",
            "version": 1,
            "source": "architecture",
            **extra,
        },
    )
    for name in links:
        for path in sorted((model_root / "swc").rglob("*.yaml"), key=lambda candidate: candidate.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if document.get("name") == name:
                document["satisfies"] = [*document.get("satisfies", []), f"{req_id}@1"]
                _write(path, document)


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """One component and one requirement linking it, neither declaring a level."""
    root = tmp_path / "model"
    _swc(root, "Ctl")
    _requirement(root, "REQ_1", ["Ctl"])
    return root


def _records(model_root: Path) -> tuple[Any, ...]:
    return check_component_asil(str(model_root)).records


def _record(model_root: Path) -> Any:
    records = [record for record in _records(model_root) if record.rule == RULE]
    assert len(records) == 1, f"expected one {RULE!r}, got {[r.message for r in records]}"
    return records[0]


# ---------------------------------------------------------------------------
# 1. The seven cases of the rule
# ---------------------------------------------------------------------------


class TestTheSevenCases:
    def test_declared_below_the_maximum_is_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        record = _record(model_root)

        assert record.rule == RULE
        assert "Ctl" in record.message
        assert "ASIL B" in record.message
        assert "ASIL D" in record.message
        assert "REQ_1" in record.message

    def test_declared_equal_to_the_maximum_is_not(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="D")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

    def test_declared_above_the_maximum_is_not(self, model_root: Path) -> None:
        """`[constr_6201]`'s legitimate direction: a component may be built higher."""
        _swc(model_root, "Ctl", asil="D")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="A")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

    def test_an_absent_level_is_not_a_finding_because_it_derives_the_maximum(self, model_root: Path) -> None:
        """The derivation, not an exemption: the component's level *is* `D` here."""
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)
        assert effective_asil_levels(*_layers(model_root))["Ctl"] == "D"

    def test_a_component_no_requirement_links_is_not_a_finding(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        _swc(root, "Lonely", asil="QM")

        assert check_component_asil(str(root)) == ConsistencyResult(consistent=True)

    def test_a_link_naming_no_declared_requirement_is_another_rules_finding(self, model_root: Path) -> None:
        """The cascade guard: `trace.link-unknown-requirement` owns it."""
        _swc(model_root, "Ctl", asil="QM", satisfies=["REQ_1@1", "REQ_GHOST@1"])

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

        aggregate = check_cross_layer_consistency(str(model_root))
        reported = {record.rule for record in aggregate.records}
        assert "trace.link-unknown-requirement" in reported
        assert RULE not in reported

    def test_an_external_stub_below_its_requirements_is_a_finding(self, model_root: Path) -> None:
        """The one place a stub is *not* skipped: `asil` is the project's own word."""
        _swc(
            model_root,
            "Ctl",
            external=True,
            external_ref="/Other/Ctl",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
            asil="B",
        )
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        record = _record(model_root)

        assert "Ctl" in record.message


# ---------------------------------------------------------------------------
# 2. An absent level and a written QM are two states
# ---------------------------------------------------------------------------


class TestADeclaredQmIsADeclaration:
    def test_a_component_declaring_qm_under_an_asil_d_requirement_is_a_finding(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="QM")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        record = _record(model_root)

        assert "ASIL QM" in record.message
        assert "ASIL D" in record.message

    def test_requirements_declaring_nothing_leave_no_maximum_to_be_below(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="QM")
        _requirement(model_root, "REQ_1", ["Ctl"])
        _requirement(model_root, "REQ_2", ["Ctl"])

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)


# ---------------------------------------------------------------------------
# 3. One finding per component, naming every requirement above it
# ---------------------------------------------------------------------------


class TestTheFindingNamesEveryRequirementAboveIt:
    def test_two_requirements_above_produce_one_finding_naming_both(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="A")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="C")
        _requirement(model_root, "REQ_2", ["Ctl"], asil="D")

        record = _record(model_root)

        assert "REQ_1" in record.message
        assert "REQ_2" in record.message
        assert "ASIL D" in record.message

    def test_a_requirement_at_or_below_the_declared_level_is_not_named(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B")
        _requirement(model_root, "REQ_2", ["Ctl"], asil="D")

        record = _record(model_root)

        assert "REQ_2" in record.message
        assert "REQ_1" not in record.message

    def test_the_subjects_are_the_two_legal_repairs(self, model_root: Path) -> None:
        """Raise the component, or lower the requirements above it. The rule chooses neither."""
        _swc(model_root, "Ctl", asil="A")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        record = _record(model_root)

        assert record.subjects == (
            Subject("swc", "Ctl", (field_step("asil"),)),
            Subject("requirements", "REQ_1", (field_step("asil"),)),
        )

    def test_a_second_component_is_its_own_finding(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="A")
        _swc(model_root, "Mon", asil="A")
        _requirement(model_root, "REQ_1", ["Ctl", "Mon"], asil="D")

        records = [record for record in _records(model_root) if record.rule == RULE]

        assert len(records) == 2
        assert {"Ctl", "Mon"} == {record.subjects[0].element for record in records}


# ---------------------------------------------------------------------------
# 4. The effective level
# ---------------------------------------------------------------------------


def _layers(model_root: Path) -> tuple[Any, Any]:
    from swcraft_tools.reader import read_requirements, read_swc_model

    return read_swc_model(str(model_root)), read_requirements(str(model_root))


class TestTheEffectiveLevel:
    def test_a_declared_level_is_the_effective_one(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "B"}

    def test_an_undeclared_level_is_the_maximum_allocated(self, model_root: Path) -> None:
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B")
        _requirement(model_root, "REQ_2", ["Ctl"], asil="D")

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "D"}

    def test_no_allocation_at_all_is_qm(self, model_root: Path) -> None:
        """`max_asil`'s empty answer, and the reason it is load-bearing rather than a default."""
        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "QM"}

    def test_a_link_naming_no_declared_requirement_contributes_nothing(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", satisfies=["REQ_1@1", "REQ_GHOST@1"])

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "QM"}

    def test_a_ports_and_a_runnables_link_allocate_to_their_component(self, model_root: Path) -> None:
        """The allocation is the component's own links, its ports' and its runnables'."""
        _swc(
            model_root,
            "Ctl",
            asil="A",
            ports=[{"name": "pIn", "direction": "require", "interface": "I", "satisfies": ["REQ_P@1"]}],
            runnables=[{"name": "Run", "satisfies": ["REQ_R@2"]}],
        )
        _requirement(model_root, "REQ_P", [], asil="C")
        _requirement(model_root, "REQ_R", [], asil="D")

        record = _record(model_root)

        assert "ASIL A" in record.message and "ASIL D" in record.message
        assert {subject.element for subject in record.subjects[1:]} == {"REQ_P", "REQ_R"}

    def test_a_link_on_a_composition_allocates_nothing(self, model_root: Path) -> None:
        """A link on any other linkable kind is recorded and checked, and allocates no level."""
        _write(
            model_root / "compositions" / "Top.yaml",
            {"name": "Top", "title": "Top", "root": True, "satisfies": ["REQ_D@1"]},
        )
        _requirement(model_root, "REQ_D", [], asil="D")

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "QM"}

    def test_two_files_of_one_name_answer_the_FIRST_ones_declaration(self) -> None:
        """The name-keyed view picks the same file the rules do.

        `check_safety_coexistence` resolves a task's component reference to the FIRST
        file, so this map must too (a LAST-wins dict comprehension would take one
        project's level from one file and the requirements explaining it from another).

        **What this does not claim.** Where two files of one name declare
        different levels there is no single right answer, and this returns the
        first element of the sequence it is given; it is not independent of a
        sequence a direct caller composes by hand. A model in that state is
        `name.duplicate-swc`'s finding and is rejected either way.
        """
        from swcraft_tools.schema import Requirement, SWComponent

        requirement = Requirement(
            id="REQ_1", title="t", description="The system shall do it.", asil="C", version=1, source="architecture"
        )
        first = SWComponent(name="Dup", type="application", asil="QM", satisfies=["REQ_1@1"])
        second = SWComponent(name="Dup", type="application", asil="D", satisfies=["REQ_1@1"])

        assert effective_asil_levels([first, second], [requirement]) == {"Dup": "QM"}
        assert effective_asil_levels([second, first], [requirement]) == {"Dup": "D"}

    def test_a_component_linked_twice_by_one_requirement_is_allocated_once(self, model_root: Path) -> None:
        """`satisfies` accepts a repeated value, and one allocation is one allocation.

        A per-occurrence append would name the same requirement twice in one message,
        with two identical subjects. The repair is in the index, not a load-time
        refusal: a repeated link is not a second allocation.
        """
        from swcraft_tools.validator.asil import asil_allocation_index

        _swc(model_root, "Ctl", asil="QM")
        _requirement(model_root, "REQ_D", ["Ctl", "Ctl"], asil="D")

        record = _record(model_root)
        allocated = asil_allocation_index(*_layers(model_root))

        assert [requirement.id for requirement in allocated["Ctl"]] == ["REQ_D"]
        assert record.message.count("REQ_D") == 1
        assert record.subjects == (
            Subject("swc", "Ctl", (field_step("asil"),)),
            Subject("requirements", "REQ_D", (field_step("asil"),)),
        )


class TestADecomposedLevelIsComparedByX:
    """The rule ranks by `X`; `(Y)` is carried.

    Only `trace.decomposition-context-mismatch` compares `(Y)` with the parent's level before decomposition, and this
    rule is not it."""

    def test_a_decomposed_component_under_a_plain_requirement_of_its_x_is_not_below(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B(D)")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

    def test_a_plain_component_under_a_decomposed_requirement_of_its_x_is_not_below(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B(D)")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

    def test_a_decomposed_qm_requirement_allocates_qm(self, model_root: Path) -> None:
        """`QM(A)` ranks with `QM`: a component declaring `QM` under it is not below."""
        _swc(model_root, "Ctl", asil="QM")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="QM(A)")

        assert check_component_asil(str(model_root)) == ConsistencyResult(consistent=True)

    def test_an_undeclared_component_takes_the_value_as_written_y_included(self, model_root: Path) -> None:
        _requirement(model_root, "REQ_1", ["Ctl"], asil="QM(A)")

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "QM(A)"}

    def test_the_effective_level_carries_the_winners_y_by_the_tie_rule(self, model_root: Path) -> None:
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B(C)")
        _requirement(model_root, "REQ_2", ["Ctl"], asil="B(D)")
        _requirement(model_root, "REQ_3", ["Ctl"], asil="A")

        assert effective_asil_levels(*_layers(model_root)) == {"Ctl": "B(D)"}

    def test_a_decomposed_x_above_the_declaration_is_a_finding_naming_the_value_as_written(
        self, model_root: Path
    ) -> None:
        _swc(model_root, "Ctl", asil="A(D)")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="B(D)")

        record = _record(model_root)

        assert "declares ASIL A(D), below the ASIL B(D)" in record.message
        assert "'REQ_1' (ASIL B(D))" in record.message


# ---------------------------------------------------------------------------
# 5. The rule's registration — the vocabulary, the code, the composition
# ---------------------------------------------------------------------------


class TestTheRuleIsRegistered:
    def test_the_rule_is_in_the_closed_inventory(self) -> None:
        assert RULE in CONSISTENCY_RULES

    def test_the_protocol_answers_a_consistency_violation_in_its_own_group(self) -> None:
        mapping = RULE_CODES[RULE]
        assert mapping.code == CONSISTENCY_VIOLATION
        assert mapping.group == RULE

    def test_the_contract_declares_it_cross_element(self) -> None:
        contract = load_contract()
        declared = {rule["id"]: rule for rule in contract["rules"]}
        rule = declared[f"rule:{RULE}"]
        assert rule["family"] == "cross-element"
        assert rule["summary"] == CONSISTENCY_RULES[RULE]

    def test_the_emission_site_yields_both_layers_the_contract_declares(self) -> None:
        """The `Subject(...)` tuple has to be readable by the scan, not merely correct."""
        observed = emission_layers()
        sites = observed[f"rule:{RULE}"]
        assert sites and all(site == frozenset({"swc", "requirements"}) for site in sites)
        assert cross_element_layer_failures(load_contract(), observed) == []

    def test_the_aggregate_reports_it(self, model_root: Path) -> None:
        _swc(model_root, "Ctl", asil="B")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")

        reported = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

        assert RULE in reported


# ---------------------------------------------------------------------------
# 6. The relation is read once, and reader order decides nothing
# ---------------------------------------------------------------------------
#
# The check reads the component's OWN declaration, not a name-keyed effective map
# (which would make the finding depend on reader order where two files declare one
# name), and reads "allocated to this component" from the one allocation index, so
# a finding's `maximum` and its named requirements come from the same reading.


class TestTheRelationIsReadOnce:
    def _duplicate_name_model(self, tmp_path: Path) -> Path:
        """Two component files declaring one name: one at `QM`, one declaring nothing.

        `name.duplicate-swc` refuses this model, but a direct caller of the check still
        gets what it mints: exactly one record, for the copy that declares `QM`, never
        none and never a second one attributed to the file declaring no `asil`.
        """
        root = tmp_path / "model"
        _write(root / "swc" / "aaa" / "Dup.yaml", {"name": "Dup", "type": "application", "asil": "QM"})
        _write(root / "swc" / "zzz" / "Dup.yaml", {"name": "Dup", "type": "application"})
        _requirement(root, "REQ_DUP", ["Dup"], asil="D")
        return root

    @pytest.mark.parametrize("reversed_order", [False, True], ids=["reader order", "reversed"])
    def test_two_files_of_one_name_mint_exactly_one_finding_in_either_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reversed_order: bool
    ) -> None:
        """One finding, for the copy that DECLARED the level, whichever order the reader yields.

        The order is varied at the reader rather than by renaming directories, so the
        claim is about the check, not a filesystem walk that happens to sort.
        """
        model_root = self._duplicate_name_model(tmp_path)
        from swcraft_tools.reader import read_swc_model as real

        def ordered(root: str) -> Any:
            swcs = list(real(root))
            return list(reversed(swcs)) if reversed_order else swcs

        monkeypatch.setattr(model_view, "read_swc_model", ordered)

        records = [record for record in _records(model_root) if record.rule == RULE]

        assert len(records) == 1, [record.message for record in records]
        assert "ASIL QM" in records[0].message
        assert records[0].subjects[0] == Subject("swc", "Dup", (field_step("asil"),))

    def test_the_maximum_and_the_named_requirements_come_from_the_same_set(self, model_root: Path) -> None:
        """The maximum the message reports is that of the requirements the same record names, from one index."""
        from swcraft_tools.safety import max_asil
        from swcraft_tools.validator.asil import asil_allocation_index

        _swc(model_root, "Ctl", asil="A")
        _requirement(model_root, "REQ_1", ["Ctl"], asil="D")
        _requirement(model_root, "REQ_2", ["Ctl"], asil="C")
        # Allocated but not above, and allocated to nothing: neither may reach the
        # maximum or the named set.
        _requirement(model_root, "REQ_3", ["Ctl"], asil="QM")
        _requirement(model_root, "REQ_4", ["Ghost"], asil="D")

        record = _record(model_root)
        swcs, requirements = _layers(model_root)
        allocated = asil_allocation_index(swcs, requirements)
        expected = {requirement.id for requirement in allocated["Ctl"] if requirement.asil in {"D", "C"}}

        named = {subject.element for subject in record.subjects[1:]}
        assert named == expected == {"REQ_1", "REQ_2"}
        maximum = max_asil(r.asil for r in allocated["Ctl"] if r.id in named and r.asil is not None)
        assert f"ASIL {maximum}" in record.message
        assert maximum == "D"
        assert "REQ_3" not in record.message and "REQ_4" not in record.message
