"""The writer emits a runnable's execution-time budget (contract ``10.0.0``).

One ``SWC-TIMING`` ``{swc}_Timing`` per non-external component with a runnable declaring a ``wcet``, right after
its ``SWC-IMPLEMENTATION``; inside it one ``EXECUTION-TIME-CONSTRAINT`` ``{runnable}_Budget`` per budgeted runnable
under ``TIMING-REQUIREMENTS``, its ``MAXIMUM`` the unit's ``CSE-CODE`` and the integer as written. This module holds:

* **the structural test**: every metamodel-mandatory member present (``SHORT-NAME``, ``EXECUTABLE-REF``,
  ``EXECUTION-TIME-TYPE``, ``MAXIMUM/CSE-CODE``, ``MAXIMUM/CSE-CODE-FACTOR``, ``BEHAVIOR-REF``), because both XSDs
  declare the two ``MULTIDIMENSIONAL-TIME`` children ``minOccurs="0"`` while the metamodel says 1 (TPS GST R4.3.0
  Table 4.90, R4.2.2 Table 4.69); the XSD child order and ``DEST`` values; the code per unit as
  :data:`CSE_CODE_BY_UNIT`, **a literal typed from TPS GST R4.3.0 Table 4.92** (R4.2.2 ``[TPS_GST_00354]``), never
  imported from the module under test; and nothing the model does not hold;
* both references following a stated ``behavior_name`` and ``package``; no budget and an external stub emitting
  nothing; determinism;
* **the name guards agreeing with ``check-model``** (``rule:name.swc-timing-collision``,
  ``rule:name.composition-timing-collision``, their case-only spellings, ``{runnable}_Budget`` charged only where
  ``wcet`` is stated), the placement entry, and every refusal writing nothing;
* the 18-digit factor cap's top value at both versions, the component export, and the writer -> importer round
  trip, which is an additional check, **not the independent oracle**: both sides are SWCraft's.

The XSD half is ``test_arxml_xsd_conformance.py``'s; the additive byte gate is
``test_runnable_cost_byte_identity.py``'s.

**What this does not reach:** whether a downstream tool reads ``GROSS``/``NET`` or the codes as the standard says;
a task's ``wcet``, which is never emitted; and the 4.3.0-only members the writer never emits.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import export_component
from swcraft_tools.reader import read_compositions, read_swc_model
from swcraft_tools.schema import Runnable, SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.validator import emits_swc_timing as validator_emits_swc_timing
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import _package_placement, emits_swc_timing
from swcraft_tools.writer.elements import write_element
from swcraft_tools.writer.mutations import ReservedNameError, create_swc

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).parent / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")

#: TPS GST R4.3.0 Table 4.92 *CseCodeType in Time Domain* (R4.2.2 ``[TPS_GST_00354]``): ``0`` is
#: 1 µsec, ``3`` 1 msec, ``6`` 1 sec, ``26`` 1 nsec. Typed from the table, never imported.
CSE_CODE_BY_UNIT = {"ns": "26", "us": "0", "ms": "3", "s": "6"}

#: The budgeted runnables of :func:`timed_model_root`, in declared order: one per unit, both types.
BUDGETS = {
    "ActuatorMain": ("850ns", "gross"),
    "RunUs": ("400us", "net"),
    "RunMs": ("2ms", "gross"),
    "RunS": ("1s", "net"),
}

#: Where :func:`timed_model_root` places the component, and the behaviour name it states.
PACKAGE = "Actuation"
BEHAVIOUR = "ActuatorBehaviour"
COMPONENT_PATH = f"/ComponentTypes/{PACKAGE}/ActuatorSwc"
BEHAVIOUR_PATH = f"{COMPONENT_PATH}/{BEHAVIOUR}"

#: What the model holds no value for, so the writer must emit none of it.
NEVER_EMITTED = (
    "MINIMUM",
    "COMPONENT-IREF",
    "TIMING-CONDITION-REF",
    "TIMING-DESCRIPTIONS",
    "TIMING-GUARANTEES",
    "COMPONENT-REF",
    "VARIATION-POINT",
    "DESC",
    "TIMING-CONDITIONS",
    "TIMING-RESOURCE",
)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _swc_file(model_root: Path, name: str) -> Path:
    (path,) = (model_root / "swc").rglob(f"{name}.yaml")
    return path


def _edit_swc(model_root: Path, name: str, edit) -> None:
    path = _swc_file(model_root, name)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _write_swc(model_root: Path, document: dict, group: str = "extra") -> None:
    path = model_root / "swc" / group / f"{document['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _budgeted(name: str, wcet: str = "400us", wcet_type: str = "gross") -> dict:
    return {"name": name, "wcet": wcet, "wcet_type": wcet_type}


def timed_model_root(tmp_path: Path) -> Path:
    """The component-exchange tree whose ``ActuatorSwc`` carries a budget in every unit and both types.

    ``ActuatorSwc`` is placed in the sub-package :data:`PACKAGE`, states the behaviour name
    :data:`BEHAVIOUR`, and holds :data:`BUDGETS`' four budgeted runnables plus ``RunNone`` with no
    budget; ``SensorSwc`` declares no budget.
    """
    model_root = tmp_path / "timed" / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)

    def edit(document: dict) -> None:
        main = document["runnables"][0]
        main["wcet"], main["wcet_type"] = BUDGETS["ActuatorMain"]
        document["runnables"] += [_budgeted(name, *BUDGETS[name]) for name in ("RunUs", "RunMs", "RunS")]
        document["runnables"].append({"name": "RunNone"})
        document["behavior_name"] = BEHAVIOUR
        document["package"] = PACKAGE

    _edit_swc(model_root, "ActuatorSwc", edit)
    return model_root


def _plain_model_root(tmp_path: Path, name: str = "plain") -> Path:
    model_root = tmp_path / name / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    return model_root


def _give_budget(model_root: Path, swc: str = "ActuatorSwc") -> None:
    def edit(document: dict) -> None:
        document["runnables"][0]["wcet"] = "400us"
        document["runnables"][0]["wcet_type"] = "gross"

    _edit_swc(model_root, swc, edit)


# ---------------------------------------------------------------------------
# Reading the output
# ---------------------------------------------------------------------------


def _generate(model_root: Path, out: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _short_name(node: etree._Element) -> str | None:
    return node.findtext(f"{NS}SHORT-NAME")


def _timings(root: etree._Element) -> list[etree._Element]:
    return list(root.iter(f"{NS}SWC-TIMING"))


def _rules(model_root: Path) -> set[str]:
    return {record.rule for record in check_cross_layer_consistency(str(model_root)).records}


def _refused(model_root: Path, out: Path) -> str:
    with pytest.raises(ValueError) as raised:
        generate_arxml(str(model_root), str(out))
    assert not out.exists(), "a refused model left a file"
    return str(raised.value)


# ---------------------------------------------------------------------------
# The structural test
# ---------------------------------------------------------------------------


class TestTheStructure:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_every_mandatory_member_in_the_xsd_order(self, version: str, tmp_path: Path) -> None:
        root = _generate(timed_model_root(tmp_path), tmp_path / "out.arxml", version)
        (timing,) = _timings(root)

        assert [_local(child) for child in timing] == ["SHORT-NAME", "TIMING-REQUIREMENTS", "BEHAVIOR-REF"]
        assert _short_name(timing) == "ActuatorSwc_Timing"
        behavior_ref = timing.find(f"{NS}BEHAVIOR-REF")
        assert (behavior_ref.get("DEST"), behavior_ref.text) == ("SWC-INTERNAL-BEHAVIOR", BEHAVIOUR_PATH)

        constraints = list(timing.find(f"{NS}TIMING-REQUIREMENTS"))
        assert [(_local(c), _short_name(c)) for c in constraints] == [
            ("EXECUTION-TIME-CONSTRAINT", f"{runnable}_Budget") for runnable in BUDGETS
        ]
        for constraint, (runnable, (wcet, wcet_type)) in zip(constraints, BUDGETS.items(), strict=True):
            assert [_local(child) for child in constraint] == [
                "SHORT-NAME",
                "EXECUTABLE-REF",
                "EXECUTION-TIME-TYPE",
                "MAXIMUM",
            ]
            executable = constraint.find(f"{NS}EXECUTABLE-REF")
            assert (executable.get("DEST"), executable.text) == ("RUNNABLE-ENTITY", f"{BEHAVIOUR_PATH}/{runnable}")
            assert constraint.findtext(f"{NS}EXECUTION-TIME-TYPE") == wcet_type.upper()
            maximum = constraint.find(f"{NS}MAXIMUM")
            assert [_local(child) for child in maximum] == ["CSE-CODE", "CSE-CODE-FACTOR"]
            unit = wcet.lstrip("0123456789")
            factor = wcet[: -len(unit)]
            assert (maximum.findtext(f"{NS}CSE-CODE"), maximum.findtext(f"{NS}CSE-CODE-FACTOR")) == (
                CSE_CODE_BY_UNIT[unit],
                factor,
            )

    @pytest.mark.parametrize("version", VERSIONS)
    def test_nothing_the_model_does_not_hold_is_emitted(self, version: str, tmp_path: Path) -> None:
        root = _generate(timed_model_root(tmp_path), tmp_path / "out.arxml", version)
        (timing,) = _timings(root)

        assert sorted({_local(node) for node in timing.iter()} & set(NEVER_EMITTED)) == []

    def test_the_timing_follows_its_implementation_in_the_component_s_package(self, tmp_path: Path) -> None:
        root = _generate(timed_model_root(tmp_path), tmp_path / "out.arxml")
        (timing,) = _timings(root)
        previous = timing.getprevious()

        assert (_local(previous), _short_name(previous)) == ("SWC-IMPLEMENTATION", "ActuatorSwc_Implementation")
        package = timing.getparent().getparent()
        assert (_local(package), _short_name(package)) == ("AR-PACKAGE", PACKAGE)

    def test_the_derived_behaviour_name_and_the_root_package_are_followed_too(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        root = _generate(model_root, tmp_path / "out.arxml")
        (timing,) = _timings(root)
        behaviour = "/ComponentTypes/ActuatorSwc/ActuatorSwc_InternalBehavior"

        assert timing.findtext(f"{NS}BEHAVIOR-REF") == behaviour
        assert timing.findtext(f".//{NS}EXECUTABLE-REF") == f"{behaviour}/ActuatorMain"

    def test_each_budgeted_component_has_its_own_timing_after_its_own_implementation(self, tmp_path: Path) -> None:
        """Two budgeted components each get one ``SWC-TIMING`` right after their own ``SWC-IMPLEMENTATION``.

        One is in :data:`PACKAGE` and one at the root: each timing lies in its own package, with one placement entry
        each."""
        model_root = timed_model_root(tmp_path)
        _give_budget(model_root, "SensorSwc")
        root = _generate(model_root, tmp_path / "out.arxml")

        timings = {_short_name(timing): timing for timing in _timings(root)}
        assert sorted(timings) == ["ActuatorSwc_Timing", "SensorSwc_Timing"]
        for name, package, path in (
            ("ActuatorSwc", PACKAGE, COMPONENT_PATH),
            ("SensorSwc", "ComponentTypes", "/ComponentTypes/SensorSwc"),
        ):
            timing = timings[f"{name}_Timing"]
            previous = timing.getprevious()
            assert (_local(previous), _short_name(previous)) == ("SWC-IMPLEMENTATION", f"{name}_Implementation")
            assert _short_name(timing.getparent().getparent()) == package
            assert timing.findtext(f"{NS}BEHAVIOR-REF").startswith(f"{path}/")
        placement = _package_placement(
            read_swc_model(str(model_root)), [], [], read_compositions(str(model_root)), [], []
        )
        assert placement.package_of("ComponentTypes", "ActuatorSwc_Timing") == PACKAGE
        assert placement.knows("ComponentTypes", "SensorSwc_Timing")
        assert placement.package_of("ComponentTypes", "SensorSwc_Timing") is None

    def test_a_component_without_a_budget_emits_no_timing(self, tmp_path: Path) -> None:
        root = _generate(_plain_model_root(tmp_path), tmp_path / "out.arxml")

        assert _timings(root) == []

    def test_a_stub_emits_nothing(self, tmp_path: Path) -> None:
        model_root = timed_model_root(tmp_path)
        _write_swc(model_root, _stub("ForeignSwc"))
        root = _generate(model_root, tmp_path / "out.arxml")

        assert [_short_name(timing) for timing in _timings(root)] == ["ActuatorSwc_Timing"]

    def test_two_runs_are_byte_identical(self, tmp_path: Path) -> None:
        model_root = timed_model_root(tmp_path)
        generate_arxml(str(model_root), str(tmp_path / "a.arxml"))
        generate_arxml(str(model_root), str(tmp_path / "b.arxml"))

        assert (tmp_path / "a.arxml").read_bytes() == (tmp_path / "b.arxml").read_bytes()


class TestThePresenceRuleIsOneRule:
    """The writer's ``emits_swc_timing`` and the validator's restatement, pinned against each other."""

    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            ({"name": "A", "type": "application"}, False),
            ({"name": "A", "type": "application", "runnables": [{"name": "R"}]}, False),
            ({"name": "A", "type": "application", "runnables": [{"name": "R"}, _budgeted("S")]}, True),
            (
                {
                    "name": "A",
                    "type": "application",
                    "external": True,
                    "external_ref": "/Other/A",
                    "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                },
                False,
            ),
        ],
    )
    def test_both_answer_alike(self, document: dict, expected: bool) -> None:
        swc = SWComponent.model_validate(document)

        assert emits_swc_timing(swc) is expected
        assert validator_emits_swc_timing(swc) is expected


# ---------------------------------------------------------------------------
# The name guards and check-model
# ---------------------------------------------------------------------------


def _stub(name: str) -> dict:
    return {
        "name": name,
        "type": "application",
        "external": True,
        "external_ref": f"/Other/Project/{name}",
        "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
    }


def _component(name: str) -> dict:
    return {"name": name, "type": "application", "handle_termination_and_restart": "no_support"}


def _composition(model_root: Path, name: str) -> None:
    path = model_root / "compositions" / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"name": name, "title": name}, sort_keys=False), encoding="utf-8", newline="\n")


class TestTheTimingNameIsReservedWhereItIsEmitted:
    def test_a_component_named_like_the_timing_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        _write_swc(model_root, _component("ActuatorSwc_Timing"))

        assert "ActuatorSwc_Timing" in _refused(model_root, tmp_path / "out.arxml")
        assert "name.swc-timing-collision" in _rules(model_root)

    def test_its_case_only_spelling_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        _write_swc(model_root, _component("actuatorswc_timing"))

        assert "actuatorswc_timing" in _refused(model_root, tmp_path / "out.arxml").casefold()
        rules = _rules(model_root)
        assert "name.name-space-collision" in rules
        assert "name.swc-timing-collision" not in rules

    def test_a_composition_named_like_the_timing_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        _composition(model_root, "ActuatorSwc_Timing")

        assert "SWC-TIMING" in _refused(model_root, tmp_path / "out.arxml")
        assert "name.composition-timing-collision" in _rules(model_root)

    def test_a_composition_s_case_only_spelling_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        _composition(model_root, "ACTUATORSWC_TIMING")

        refusal = _refused(model_root, tmp_path / "out.arxml")
        assert "rule:name.name-space-collision" in refusal and "'ACTUATORSWC_TIMING'" in refusal, refusal
        rules = _rules(model_root)
        assert "name.name-space-collision" in rules
        assert "name.composition-timing-collision" not in rules

    def test_a_claimant_in_another_package_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        """The reservation is by name, whatever the packages.

        ``ActuatorSwc`` sits in :data:`PACKAGE`, the claimant at the root of ``ComponentTypes`` -- both gates refuse it.
        """
        model_root = timed_model_root(tmp_path)
        _write_swc(model_root, _component("ActuatorSwc_Timing"))

        assert "ActuatorSwc_Timing" in _refused(model_root, tmp_path / "out.arxml")
        assert "name.swc-timing-collision" in _rules(model_root)

    def test_beside_an_unbudgeted_component_the_name_is_free(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _write_swc(model_root, _component("ActuatorSwc_Timing"))
        _composition(model_root, "SensorSwc_Timing")

        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert not {"name.swc-timing-collision", "name.composition-timing-collision", "name.name-space-collision"} & (
            _rules(model_root)
        )

    def test_beside_a_stub_the_name_is_free(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _write_swc(model_root, _stub("ForeignSwc"))
        _write_swc(model_root, _component("ForeignSwc_Timing"))

        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert "name.swc-timing-collision" not in _rules(model_root)

    def test_the_placement_table_holds_the_entry_only_where_it_is_emitted(self, tmp_path: Path) -> None:
        model_root = timed_model_root(tmp_path)
        swcs = read_swc_model(str(model_root))
        placement = _package_placement(swcs, [], [], read_compositions(str(model_root)), [], [])

        assert placement.knows("ComponentTypes", "ActuatorSwc_Timing")
        assert placement.package_of("ComponentTypes", "ActuatorSwc_Timing") == PACKAGE
        assert not placement.knows("ComponentTypes", "SensorSwc_Timing")


class TestTheBudgetNameIsChargedWhereWcetIsStated:
    @staticmethod
    def _model(tmp_path: Path, length: int, *, budgeted: bool) -> tuple[Path, str]:
        model_root = _plain_model_root(tmp_path)
        name = "R" * length

        def edit(document: dict) -> None:
            document["runnables"].append(_budgeted(name) if budgeted else {"name": name})

        _edit_swc(model_root, "ActuatorSwc", edit)
        return model_root, name

    def test_121_characters_with_a_budget_is_legal(self, tmp_path: Path) -> None:
        model_root, name = self._model(tmp_path, 121, budgeted=True)
        root = _generate(model_root, tmp_path / "out.arxml")

        assert f"{name}_Budget" in [_short_name(node) for node in root.iter(f"{NS}EXECUTION-TIME-CONSTRAINT")]
        assert "identifier.generated" not in _rules(model_root)

    def test_122_characters_with_a_budget_is_refused_by_both_gates(self, tmp_path: Path) -> None:
        model_root, name = self._model(tmp_path, 122, budgeted=True)

        assert f"{name}_Budget" in _refused(model_root, tmp_path / "out.arxml")
        records = check_cross_layer_consistency(str(model_root)).records
        assert [r.rule for r in records if f"{name}_Budget" in r.message] == ["identifier.generated"]

    def test_122_characters_without_a_budget_is_legal(self, tmp_path: Path) -> None:
        model_root, _name = self._model(tmp_path, 122, budgeted=False)

        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert "identifier.generated" not in _rules(model_root)


# ---------------------------------------------------------------------------
# The cap, the component export and the round trip
# ---------------------------------------------------------------------------


def _imported_budgets(arxml: Path, tmp_path: Path, swc: str = "ActuatorSwc") -> dict[str, tuple[str, str]]:
    project = init_project(tmp_path / "project")
    run_import(str(arxml), str(project / "swcraft.yaml"))
    (path,) = (project / "model" / "swc").rglob(f"{swc}.yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {r["name"]: (r["wcet"], r["wcet_type"]) for r in document["runnables"] if "wcet" in r}


class TestTheFactorCap:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_18_digit_factor_is_written_and_read_back_equal(self, version: str, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)

        def edit(document: dict) -> None:
            document["runnables"][0]["wcet"] = "999999999999999999ns"
            document["runnables"][0]["wcet_type"] = "net"

        _edit_swc(model_root, "ActuatorSwc", edit)
        out = tmp_path / "out.arxml"
        root = _generate(model_root, out, version)

        assert root.findtext(f".//{NS}CSE-CODE-FACTOR") == "999999999999999999"
        assert _imported_budgets(out, tmp_path) == {"ActuatorMain": ("999999999999999999ns", "net")}


class TestTheComponentExport:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_component_export_carries_the_timing(self, version: str, tmp_path: Path) -> None:
        out = tmp_path / "ActuatorSwc.arxml"
        export_component(str(timed_model_root(tmp_path)), "ActuatorSwc", str(out), version=version)
        (timing,) = _timings(etree.parse(str(out)).getroot())

        assert _short_name(timing) == "ActuatorSwc_Timing"
        assert [_short_name(c) for c in timing.find(f"{NS}TIMING-REQUIREMENTS")] == [f"{r}_Budget" for r in BUDGETS]


class TestTheRoundTrip:
    """Writer -> importer: an additional check, not the independent oracle (both sides are SWCraft's)."""

    @pytest.mark.parametrize("version", VERSIONS)
    def test_every_unit_and_both_types_come_back_as_written(self, version: str, tmp_path: Path) -> None:
        out = tmp_path / "model.arxml"
        _generate(timed_model_root(tmp_path), out, version)

        assert _imported_budgets(out, tmp_path) == BUDGETS


# ---------------------------------------------------------------------------
# The write path's two guards
# ---------------------------------------------------------------------------


class TestTheWritePathRefusesWhatTheWriterRefuses:
    """``create_swc`` and ``write_element`` reach ``check_reserved_swc_name`` and the budget charge."""

    def test_create_swc_refuses_the_timing_name_beside_a_budgeted_component(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)

        with pytest.raises(ReservedNameError, match="SWC-TIMING"):
            create_swc(str(model_root), "ActuatorSwc_Timing", "extra")
        assert not (model_root / "swc" / "extra").exists()

    def test_create_swc_accepts_it_beside_a_component_file_that_does_not_load(self, tmp_path: Path) -> None:
        """``mutations._emits_timing``'s fallback: a file that does not load as a component emits no timing.

        ``check-model`` reports the broken file first; a file that is no YAML at all the layer scan refuses."""
        model_root = _plain_model_root(tmp_path)
        _give_budget(model_root)
        # Valid YAML (the layer scan reads it), budgeted, but no component: `type` names no SWC type.
        _swc_file(model_root, "ActuatorSwc").write_text(
            "name: ActuatorSwc\ntype: no-such-type\nrunnables:\n- name: R\n  wcet: 1us\n  wcet_type: gross\n",
            encoding="utf-8",
            newline="\n",
        )

        create_swc(str(model_root), "ActuatorSwc_Timing", "extra")
        assert (model_root / "swc" / "extra" / "ActuatorSwc_Timing.yaml").is_file()

    def test_create_swc_accepts_it_beside_an_unbudgeted_component(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)

        create_swc(str(model_root), "ActuatorSwc_Timing", "extra")
        assert (model_root / "swc" / "extra" / "ActuatorSwc_Timing.yaml").is_file()

    def test_write_element_refuses_a_budgeted_component_beside_its_timing_name(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        _write_swc(model_root, _component("ActuatorSwc_Timing"))
        swc = read_swc_model(str(model_root))
        (actuator,) = [component for component in swc if component.name == "ActuatorSwc"]
        budgeted = actuator.model_copy(
            update={"runnables": [actuator.runnables[0].model_copy(update={"wcet": "1us", "wcet_type": "gross"})]}
        )
        target = _swc_file(model_root, "ActuatorSwc").relative_to(model_root).as_posix()
        before = _swc_file(model_root, "ActuatorSwc").read_bytes()

        with pytest.raises(ReservedNameError, match="SWC-TIMING"):
            write_element(str(model_root), target, budgeted)
        assert _swc_file(model_root, "ActuatorSwc").read_bytes() == before

    def test_write_element_refuses_a_budget_name_over_the_limit(self, tmp_path: Path) -> None:
        model_root = _plain_model_root(tmp_path)
        swc = read_swc_model(str(model_root))
        (actuator,) = [component for component in swc if component.name == "ActuatorSwc"]
        long_name = "R" * 122
        budgeted = actuator.model_copy(
            update={"runnables": [*actuator.runnables, Runnable(name=long_name, wcet="1us", wcet_type="gross")]}
        )
        target = _swc_file(model_root, "ActuatorSwc").relative_to(model_root).as_posix()

        with pytest.raises(ValueError, match=f"{long_name}_Budget"):
            write_element(str(model_root), target, budgeted)


class TestTheExchangeAndTheProtocolChargeTheBudgetName:
    """``import component``'s identifier guard and the editing protocol's diagnosis, the same charge."""

    @staticmethod
    def _component_with(length: int, *, budgeted: bool) -> SWComponent:
        runnable = {"name": "R" * length, **({"wcet": "1us", "wcet_type": "gross"} if budgeted else {})}
        return SWComponent.model_validate({"name": "A", "type": "application", "runnables": [runnable]})

    @pytest.mark.parametrize(
        ("length", "budgeted", "charged"), [(122, True, True), (121, True, False), (122, False, False)]
    )
    def test_import_component_charges_it(self, length: int, budgeted: bool, charged: bool) -> None:
        from swcraft_tools.component_exchange.name_policies import illegal_identifiers

        violations = illegal_identifiers(self._component_with(length, budgeted=budgeted), [], [])

        assert any(f"{'R' * length}_Budget" in line for line in violations) is charged

    @pytest.mark.parametrize(
        ("length", "budgeted", "charged"), [(122, True, True), (121, True, False), (122, False, False)]
    )
    def test_the_protocol_diagnosis_charges_it(self, length: int, budgeted: bool, charged: bool) -> None:
        from swcraft_tools.editing.diagnosis import _swc_identifiers

        document = self._component_with(length, budgeted=budgeted).model_dump(exclude_none=True)
        violations = _swc_identifiers(document)

        assert any(f"{'R' * length}_Budget" in violation.message for violation in violations) is charged
