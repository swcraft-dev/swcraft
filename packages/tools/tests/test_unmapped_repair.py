"""The repair ``data-type.application-type-unmapped``'s message names, and the one ``complete: true`` adds.

The message names the one declared set that repairs the component, or the candidates per type when
there is no single one. The repair is decided per component (``_repair_carriers``), so the message and
``change_set``'s ``complete: true`` (:func:`~swcraft_tools.validator.unmapped_type_repairs`) cannot
disagree. Prose only — the error shape is closed.
"""

from __future__ import annotations

from pathlib import Path

from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import MappingSet, SWComponent
from swcraft_tools.validator import (
    APPLICATION_TYPE_UNMAPPED,
    MemberRepair,
    check_application_type_mapping,
    unmapped_type_repairs,
)
from swcraft_tools.validator.data_types import _fix_of, _repair_carriers


def _set(name: str, *applications: str, implementation: str = "uint16") -> MappingSet:
    return MappingSet(
        name=name,
        maps=[{"application": application, "implementation": implementation} for application in applications],
    )


SWC = SWComponent(name="Logger", group="application", type="application")


def test_one_carrier_is_named_as_the_repair() -> None:
    sets = {"Thermal": _set("Thermal", "Temp_C"), "Other": _set("Other", "Speed")}
    carriers = _repair_carriers(SWC, ["Temp_C"], sets, frozenset())
    assert carriers == ["Thermal"]
    repair = _fix_of(SWC, "Temp_C", carriers, sets, frozenset())
    assert "Set 'Thermal' is the one declared set that carries every type SWC 'Logger' leaves unmapped" in repair
    assert repair.endswith("name it in the component's data_type_mappings.")


def test_several_carriers_are_named_as_candidates() -> None:
    sets = {"A": _set("A", "Temp_C"), "B": _set("B", "Temp_C")}
    carriers = _repair_carriers(SWC, ["Temp_C"], sets, frozenset())
    assert carriers == ["A", "B"]
    assert _fix_of(SWC, "Temp_C", carriers, sets, frozenset()) == " Declared sets that carry it: 'A', 'B'."


def test_no_carrier_adds_nothing_and_a_duplicated_set_is_no_carrier() -> None:
    sets = {"A": _set("A", "Temp_C")}
    assert _fix_of(SWC, "Speed", [], sets, frozenset()) == ""
    assert _repair_carriers(SWC, ["Temp_C"], sets, frozenset({"A"})) == []
    assert _fix_of(SWC, "Temp_C", [], sets, frozenset({"A"})) == ""


def test_a_repair_split_over_two_sets_names_no_unique_one() -> None:
    sets = {"SetT": _set("SetT", "Temp_C"), "SetP": _set("SetP", "Press_P")}
    carriers = _repair_carriers(SWC, ["Temp_C", "Press_P"], sets, frozenset())
    assert carriers == []
    # Each type's message names its own candidate and never calls it the one repair.
    assert _fix_of(SWC, "Temp_C", carriers, sets, frozenset()) == " Declared sets that carry it: 'SetT'."
    assert _fix_of(SWC, "Press_P", carriers, sets, frozenset()) == " Declared sets that carry it: 'SetP'."


def test_a_set_the_component_already_names_is_no_carrier() -> None:
    swc = SWComponent(name="Logger", group="application", type="application", data_type_mappings=["A"])
    sets = {"A": _set("A", "Speed"), "B": _set("B", "Temp_C")}
    assert _repair_carriers(swc, ["Temp_C"], sets, frozenset()) == ["B"]
    assert _repair_carriers(swc, ["Temp_C"], {**sets, "A": _set("A", "Temp_C", "Speed")}, frozenset()) == ["B"]


def test_a_set_pairing_a_mapped_type_with_a_second_implementation_is_no_carrier() -> None:
    swc = SWComponent(name="Logger", group="application", type="application", data_type_mappings=["A"])
    sets = {"A": _set("A", "Temp_C", implementation="float32"), "B": _set("B", "Press_P", "Temp_C")}
    carriers = _repair_carriers(swc, ["Press_P"], sets, frozenset())
    assert carriers == []
    assert _fix_of(swc, "Press_P", carriers, sets, frozenset()) == (
        " Declared sets that carry it: 'B'. Naming 'B' would pair a type the component maps with a second "
        "implementation type."
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _model(root: Path) -> Path:
    """Three components speaking Temp_C: one repairable, one naming an undeclared set, one naming a duplicated set."""
    model = root / "model"
    _write(model / "data_types/application/Temp_C.yaml", "name: Temp_C\nkind: application_type\n")
    _write(
        model / "interfaces/ITemp.yaml",
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n",
    )
    _write(
        model / "data_types/mapping_sets/Thermal.yaml",
        "name: Thermal\nmaps:\n  - application: Temp_C\n    implementation: float32\n",
    )
    for name in ("Dup", "Dup~1"):
        _write(model / f"data_types/mapping_sets/{name}.yaml", "name: Dup\nmaps: []\n")
    port = "ports:\n  - name: rTemp\n    direction: require\n    interface: ITemp\n"
    for name, mappings in (
        ("Plain", ""),
        ("Typo", "data_type_mappings:\n  - Nowhere\n"),
        ("Twice", "data_type_mappings:\n  - Dup\n"),
    ):
        _write(model / f"swc/example/{name}.yaml", f"name: {name}\ntype: application\n{port}{mappings}")
    return model


def test_the_completion_repairs_only_the_named_components_and_abstains_where_the_rule_does(tmp_path: Path) -> None:
    model = str(_model(tmp_path))
    plain, typo, twice = (_subject(name) for name in ("Plain", "Typo", "Twice"))
    assert unmapped_type_repairs(model, {plain, typo, twice}) == {
        plain: MemberRepair(subject=plain.at(member_step("Thermal")), value="Thermal")
    }
    assert unmapped_type_repairs(model, {typo}) == {}
    assert unmapped_type_repairs(model, set()) == {}
    # A subject that is not the rule's own (the element itself, another layer) has no repair: the
    # completion composes addresses beside the rule's subject only.
    assert unmapped_type_repairs(model, {Subject("swc", "Plain"), Subject("interfaces", "Plain")}) == {}


def test_the_rule_reports_the_subject_the_completion_repairs_under_the_exported_rule_name(tmp_path: Path) -> None:
    """The rule's finding carries :data:`APPLICATION_TYPE_UNMAPPED` and the subject the completion takes back."""
    model = str(_model(tmp_path))
    findings = check_application_type_mapping(model).records
    assert {finding.rule for finding in findings} == {APPLICATION_TYPE_UNMAPPED}
    subjects = {subject for finding in findings for subject in finding.subjects}
    assert _subject("Plain") in subjects
    assert set(unmapped_type_repairs(model, subjects)) == {_subject("Plain")}


def _subject(component: str) -> Subject:
    """The rule's finding subject for *component*: its ``data_type_mappings``."""
    return Subject("swc", component, (field_step("data_type_mappings"),))
