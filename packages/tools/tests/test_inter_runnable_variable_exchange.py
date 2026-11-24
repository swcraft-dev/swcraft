"""Component exchange: the inter-runnable variables survive the round trip.

``swcraft generate arxml --component`` writes a component's variables and its runnables' variable
accesses through the writer, carries each variable's type in its closure, and writes the capability
marker ``SD GID="INTER-RUNNABLE-VARIABLES"`` into the behaviour's ``SWCRAFT`` group. ``swcraft
import component`` reads them back through ``map_swcs`` and decides, by that marker and by what the
file states, which regime governs an ``--update``:

* **marked** -- the file is the truth for the variables and every runnable's variable accesses,
  absence included: they are compared, and ``--update`` replaces them;
* **stated** -- no marker, and the file states a variable or an access (a third-party export): what
  it states is the truth, as under ``marked``;
* **carried** -- no marker and nothing stated (an export written before contract 12.1.0): both sides
  leave the variables out of the comparison and ``--update`` keeps the project's own.

A marker value the writer does not mint is refused. The subject is the shared fixture
(``inter_runnable_variable_support``), exported by the real exporter and edited as text where a test
needs an older, a third-party or a damaged file.

**Not reached:** a file an earlier SWCraft wrote itself (the *unmarked* file is this export with the
marker removed); the real tool exports; the MCP surface's rendering of the same
:class:`ComponentImportReport`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from external_stubs_support import change
from inter_runnable_variable_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTERED,
    LOGGER,
    MONITOR,
    PEAK,
    SPARE,
    WRITER,
    variable_model,
)
from project_support import init_project
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.capability_markers import INTER_RUNNABLE_VARIABLES
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    ComponentImportConflictError,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    ReservedNameError,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.capabilities import CARRIED, MARKED, NONE, STATED
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.component_exchange.name_policies import illegal_identifiers
from swcraft_tools.component_exchange.resulting_behaviour import _behaviour_findings
from swcraft_tools.data_description import read_data_description
from swcraft_tools.differ import diff_model_states
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import read_mapping_sets, read_swc_model
from swcraft_tools.schema import Runnable, SWComponent, Variable, VariableReadRef, VariableWriteRef
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    generate_arxml,
)

MARKER_LINE = re.compile(r'\n *<SD GID="INTER-RUNNABLE-VARIABLES">[^<]*</SD>')


# Helpers


def _export(model_root: Path, arxml: Path) -> Path:
    export_component(str(model_root), COMPONENT, str(arxml))
    return arxml


def _component(model_root: Path) -> SWComponent:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
    return swc


def _variables(swc: SWComponent) -> dict[str, Variable]:
    return {variable.name: variable for variable in swc.variables}


def _accesses(swc: SWComponent) -> dict[str, tuple[list[Any], list[Any]]]:
    return {r.name: (list(r.reads_variables), list(r.writes_variables)) for r in swc.runnables}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _without_variables(model_root: Path) -> None:
    """Drop every variable and every variable access from the component, in place."""

    def strip(document: dict[str, Any]) -> None:
        document.pop("variables", None)
        for runnable in document["runnables"]:
            runnable.pop("reads_variables", None)
            runnable.pop("writes_variables", None)

    change(model_root, COMPONENT_FILE, strip)


def _set_period(model_root: Path, runnable: str, period: float) -> None:
    def edit(document: dict[str, Any]) -> None:
        (entry,) = [r for r in document["runnables"] if r["name"] == runnable]
        entry["events"] = [{"type": "timing", "period": period}]

    change(model_root, COMPONENT_FILE, edit)


def _set_variable(model_root: Path, name: str, **fields: Any) -> None:
    def edit(document: dict[str, Any]) -> None:
        (entry,) = [v for v in document["variables"] if v["name"] == name]
        entry.update(fields)

    change(model_root, COMPONENT_FILE, edit)


def _unmark(arxml: Path) -> Path:
    """Remove the inter-runnable variables marker, which every export before contract 12.1.0 lacked."""
    text = arxml.read_text(encoding="utf-8")
    assert len(MARKER_LINE.findall(text)) == 1
    arxml.write_text(MARKER_LINE.sub("", text), encoding="utf-8", newline="\n")
    assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">' not in arxml.read_text(encoding="utf-8")
    return arxml


def _replace_text(arxml: Path, old: str, new: str) -> Path:
    text = arxml.read_text(encoding="utf-8")
    assert text.count(old) == 1, (old, text.count(old))
    arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


def _copy_export(tmp_path: Path, source: Path, name: str, *edits: Any, marked: bool = True) -> Path:
    """An export of a copy of *source*, each of *edits* applied to the copy first, unmarked unless *marked*."""
    copy = tmp_path / f"{name}-model"
    shutil.copytree(source, copy)
    for edit in edits:
        edit(copy)
    arxml = _export(copy, tmp_path / f"{name}.arxml")
    return arxml if marked else _unmark(arxml)


@pytest.fixture
def exchanged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The fixture model, its marked export, and a project holding the component imported from it.

    Returns ``(source model root, marked export, target model root)``.
    """
    source = variable_model(tmp_path / "source")
    arxml = _export(source, tmp_path / "marked.arxml")
    target = init_project(tmp_path / "target") / "model"
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, arxml, target


# The closure


class TestTheExportCarriesTheVariablesTypes:
    def test_the_closure_of_a_portless_component_holds_its_variables_types_and_their_map(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        closure = compute_closure(str(source), COMPONENT)
        assert sorted(dt.name for dt in closure.data_types) == [
            "VehicleSpeed",
            "uint16",
            "uint16_base",
            "uint8",
            "uint8_base",
        ]
        (mapping_set,) = closure.mapping_sets
        assert [(entry.application, entry.implementation) for entry in mapping_set.maps] == [("VehicleSpeed", "uint16")]

    def test_the_export_writes_every_variable_and_access(self, tmp_path: Path) -> None:
        """Guards a defect: the writer refused the export for the types the closure left out."""
        source = variable_model(tmp_path / "source")
        text = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        for wrapper in ("EXPLICIT-INTER-RUNNABLE-VARIABLES", "IMPLICIT-INTER-RUNNABLE-VARIABLES"):
            assert f"<{wrapper}>" in text
        for name in (FILTERED, PEAK, SPARE):
            assert f"<SHORT-NAME>{name}</SHORT-NAME>" in text
        assert text.count("<LOCAL-VARIABLE-REF ") == 5
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">' in text

    def test_an_access_to_no_variable_is_the_closure_check_s_refusal(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")

        def dangling(document: dict[str, Any]) -> None:
            (writer,) = [r for r in document["runnables"] if r["name"] == WRITER]
            writer["writes_variables"] = [{"variable": "Missing"}]

        change(source, COMPONENT_FILE, dangling)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert any("Missing" in issue for issue in refusal.value.issues)
        assert not (tmp_path / "component.arxml").exists()

    def test_a_read_without_init_warns_and_refuses_nothing(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        _set_variable(source, PEAK, init=None)
        assert [warning.rule for warning in check_cross_layer_consistency(str(source)).warnings] == [
            "swc.variable.read-without-init"
        ]
        assert "<SHORT-NAME>PeakSpeed</SHORT-NAME>" in _export(source, tmp_path / "c.arxml").read_text(encoding="utf-8")


# The marker


class TestTheExportCarriesTheMarker:
    def test_every_component_export_carries_it_and_the_whole_model_does_not(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        component = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">{INTER_RUNNABLE_VARIABLES.text}</SD>' in component
        assert component.index('<SD GID="OPERATION-EVENTS">') < component.index(
            f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">'
        ), "after the operation-events marker"
        whole = tmp_path / "whole.arxml"
        generate_arxml(str(source), str(whole))
        assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">' not in whole.read_text(encoding="utf-8")

    def test_a_component_with_no_variable_carries_it_too(self, tmp_path: Path) -> None:
        """A statement about the exporter: a marked file with no variable says *none*."""
        source = variable_model(tmp_path / "source")
        _without_variables(source)
        text = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert "-INTER-RUNNABLE-VARIABLES>" not in text
        assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">' in text

    def test_the_reader_admits_the_group_and_reads_the_marker(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        model = read_arxml(str(_export(source, tmp_path / "component.arxml")))
        markers = model.swcs[0].swcraft_markers
        assert markers[INTER_RUNNABLE_VARIABLES.sd_gid] == INTER_RUNNABLE_VARIABLES.text
        assert not [entry for entry in model.skip_log if "ADMIN-DATA" in entry.reason]

    def test_the_marker_is_a_capability_token_never_a_version(self) -> None:
        """One copy in the leaf ``capability_markers``, which the writer mints from and the reader admits."""
        assert INTER_RUNNABLE_VARIABLES.sd_gid == "INTER-RUNNABLE-VARIABLES"
        assert not re.search(r"\d", INTER_RUNNABLE_VARIABLES.text), "a capability, never a version"

    def test_a_value_the_writer_does_not_mint_is_refused_with_nothing_written(self, exchanged) -> None:
        _source, arxml, target = exchanged
        _replace_text(arxml, f">{INTER_RUNNABLE_VARIABLES.text}<", ">something else<")
        before = _tree(target)
        with pytest.raises(InvalidComponentArxmlError) as refusal:
            import_component(str(arxml), str(target), update=True)
        assert "INTER-RUNNABLE-VARIABLES marker with the value 'something else'" in str(refusal.value)
        assert _tree(target) == before


# The round trip


class TestTheVariablesSurviveTheRoundTrip:
    def test_the_component_exports_and_reimports_unchanged(self, exchanged) -> None:
        source, arxml, target = exchanged
        assert _variables(_component(target)) == _variables(_component(source))
        assert _accesses(_component(target)) == _accesses(_component(source))
        before = _tree(target)
        report = import_component(str(arxml), str(target))
        assert report.swc_action == "unchanged"
        assert report.variable_regime == NONE
        assert _tree(target) == before

    def test_the_imported_project_checks_clean(self, exchanged) -> None:
        _source, _arxml, target = exchanged
        result = check_cross_layer_consistency(str(target))
        assert result.consistent, result.issues

    def test_the_import_s_own_export_is_unchanged_too(self, exchanged, tmp_path: Path) -> None:
        """The variables come back explicit first (the writer's wrapper order); the comparison sorts them."""
        _source, _arxml, target = exchanged
        again = _export(target, tmp_path / "again.arxml")
        assert import_component(str(again), str(target)).swc_action == "unchanged"


# The three regimes


class TestAnUnmarkedFileStatingNoneCarriesTheProjectsVariables:
    def test_an_otherwise_identical_unmarked_file_is_unchanged(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(tmp_path, source, "older", _without_variables, marked=False)
        before = _tree(target)
        assert import_component(str(older), str(target)).swc_action == "unchanged"
        assert _tree(target) == before

    def test_an_update_keeps_the_variables_and_the_accesses(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(
            tmp_path, source, "older", _without_variables, lambda copy: _set_period(copy, WRITER, 0.02), marked=False
        )
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "inter-runnable variables and the runnables' variable accesses are not compared either" in str(
            conflict.value
        )
        expected_variables = _variables(_component(target))
        expected_accesses = _accesses(_component(target))

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.variable_regime == CARRIED
        kept = _component(target)
        assert _variables(kept) == expected_variables
        assert _accesses(kept) == expected_accesses
        assert next(r for r in kept.runnables if r.name == WRITER).events[0].period == 0.02
        assert report.preserved_variables == list(expected_variables), "the target's, in its order"
        assert report.preserved_variable_accesses == {
            WRITER: {"writes_variables": 1},
            LOGGER: {"reads_variables": 1},
            MONITOR: {"reads_variables": 2, "writes_variables": 1},
        }
        rendered = report.render()
        assert "VARIABLES regime: carried -- the export carries no SWCraft inter-runnable variables marker" in rendered
        assert (
            "VARIABLES preserved from the target project (not carried by the source ARXML): "
            + ", ".join(expected_variables)
            in rendered
        )
        assert f"  - {MONITOR}: reads_variables=2, writes_variables=1" in rendered
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_runnable_the_file_removed_takes_its_accesses_with_it(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop_logger(copy: Path) -> None:
            change(
                copy,
                COMPONENT_FILE,
                lambda document: document.update(runnables=[r for r in document["runnables"] if r["name"] != LOGGER]),
            )

        older = _copy_export(tmp_path, source, "older", _without_variables, drop_logger, marked=False)
        report = import_component(str(older), str(target), update=True)

        assert LOGGER not in _accesses(_component(target))
        assert LOGGER not in report.preserved_variable_accesses
        assert set(_variables(_component(target))) == {FILTERED, PEAK, SPARE}

    def test_the_mapping_set_a_carried_variable_needs_is_kept(self, exchanged, tmp_path: Path) -> None:
        """An earlier export names no mapping set its ports do not need; the carried variable still needs one.

        Without the carry the update would write the carried variable unmapped, a project ``check-model``
        refuses (``data-type.application-type-unmapped``).
        """
        source, _arxml, target = exchanged

        def drop_mappings(copy: Path) -> None:
            change(copy, COMPONENT_FILE, lambda document: document.pop("data_type_mappings"))

        older = _copy_export(
            tmp_path,
            source,
            "older",
            _without_variables,
            drop_mappings,
            lambda copy: _set_period(copy, WRITER, 0.02),
            marked=False,
        )
        assert "DATA-TYPE-MAPPING-REF" not in older.read_text(encoding="utf-8")

        report = import_component(str(older), str(target), update=True)

        assert report.variable_regime == CARRIED
        assert _component(target).data_type_mappings == ["SpeedMapping"]
        assert report.preserved_variable_mappings == ["SpeedMapping"]
        assert (
            "VARIABLES' MAPPING SETS preserved from the target project (not carried by the source ARXML): "
            "SpeedMapping" in report.render()
        )
        assert check_cross_layer_consistency(str(target)).consistent

    def test_an_otherwise_identical_file_without_the_variables_mapping_set_is_unchanged(
        self, exchanged, tmp_path: Path
    ) -> None:
        """The set kept only for the carried variables is left out of the comparison, as the variables are.

        Otherwise the re-import of an earlier export is a conflict, and ``--update`` rewrites the same
        component with an empty semantic diff every time.
        """
        source, _arxml, target = exchanged

        def drop_mappings(copy: Path) -> None:
            change(copy, COMPONENT_FILE, lambda document: document.pop("data_type_mappings"))

        older = _copy_export(tmp_path, source, "older", _without_variables, drop_mappings, marked=False)
        before = _tree(target)

        assert import_component(str(older), str(target)).swc_action == "unchanged"
        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "unchanged"
        assert report.preserved_variable_mappings == []
        assert _tree(target) == before

    def test_a_mapping_set_the_file_still_names_is_not_reported_as_kept(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(
            tmp_path, source, "older", _without_variables, lambda copy: _set_period(copy, WRITER, 0.02), marked=False
        )
        report = import_component(str(older), str(target), update=True)

        assert _component(target).data_type_mappings == ["SpeedMapping"]
        assert report.preserved_variable_mappings == []
        assert "MAPPING SETS preserved" not in report.render()


class TestAnUnmarkedFileStatingVariablesIsTheTruth:
    def test_a_changed_init_is_compared_and_updated(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        third_party = _copy_export(
            tmp_path, source, "third", lambda copy: _set_variable(copy, PEAK, init=7), marked=False
        )
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(third_party), str(target))
        assert "not compared either" not in str(conflict.value)

        report = import_component(str(third_party), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.variable_regime == STATED
        assert report.preserved_variables == []
        assert _variables(_component(target))[PEAK].init == 7
        assert "VARIABLES regime: stated -- " in report.render()

    def test_a_variable_the_file_omits_is_removed(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop_spare(copy: Path) -> None:
            change(
                copy,
                COMPONENT_FILE,
                lambda document: document.update(variables=[v for v in document["variables"] if v["name"] != SPARE]),
            )

        third_party = _copy_export(tmp_path, source, "third", drop_spare, marked=False)
        import_component(str(third_party), str(target), update=True)
        assert set(_variables(_component(target))) == {FILTERED, PEAK}


class TestAMarkedFileReplacesTheVariables:
    def test_a_removed_variable_and_access_are_removed(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop(document: dict[str, Any]) -> None:
            document["variables"] = [v for v in document["variables"] if v["name"] != SPARE]
            (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
            monitor["reads_variables"] = [{"variable": FILTERED}]

        newer = _copy_export(tmp_path, source, "newer", lambda copy: change(copy, COMPONENT_FILE, drop))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))

        report = import_component(str(newer), str(target), update=True)

        assert report.variable_regime == MARKED
        kept = _component(target)
        assert set(_variables(kept)) == {FILTERED, PEAK}
        assert _accesses(kept)[MONITOR] == (
            [VariableReadRef(variable=FILTERED)],
            [VariableWriteRef(variable=PEAK)],
        )
        assert report.preserved_variables == []
        assert "VARIABLES regime: marked -- " in report.render()

    def test_a_marked_file_stating_none_removes_them_all(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, source, "newer", _without_variables)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))
        report = import_component(str(newer), str(target), update=True)
        assert report.variable_regime == MARKED
        kept = _component(target)
        assert kept.variables == []
        assert all(not reads and not writes for reads, writes in _accesses(kept).values())


# A withheld behaviour


def _with_an_unanswered_mapping_set(arxml: Path) -> Path:
    """*arxml* with a second ``DATA-TYPE-MAPPING-REF``, onto a set neither the file nor the target declares."""
    return _replace_text(
        arxml,
        "</DATA-TYPE-MAPPING-REFS>",
        '<DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps</DATA-TYPE-MAPPING-REF>'
        "</DATA-TYPE-MAPPING-REFS>",
    )


class TestAWithheldBehaviourKeepsTheVariablesWithTheRunnables:
    def test_an_update_keeps_the_project_s_variables_and_runnables(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        expected = _component(target)

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."
            document.pop("variables")
            for runnable in document["runnables"]:
                runnable.pop("reads_variables", None)
                runnable.pop("writes_variables", None)

        newer = _with_an_unanswered_mapping_set(
            _copy_export(tmp_path, source, "newer", lambda copy: change(copy, COMPONENT_FILE, describe))
        )

        report = import_component(str(newer), str(target), update=True)

        assert report.behaviour_kept
        assert report.swc_action == "updated"
        assert report.variable_regime == NONE
        kept = _component(target)
        assert kept.description == "A new description."
        assert kept.variables == expected.variables
        assert kept.runnables == expected.runnables

    def test_a_withheld_create_writes_neither_runnables_nor_variables(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        arxml = _with_an_unanswered_mapping_set(_export(source, tmp_path / "component.arxml"))
        target = init_project(tmp_path / "target") / "model"

        report = import_component(str(arxml), str(target))

        assert report.swc_action == "created"
        assert report.missing_references
        written = _component(target)
        assert written.runnables == []
        assert written.variables == []

    def test_the_kept_behaviour_is_judged_by_the_variable_records_too(self, tmp_path: Path) -> None:
        """An overlay whose kept access names no variable is a break; the read-without-init warning is not."""
        source = variable_model(tmp_path / "source")
        swc = _component(source)
        description = read_data_description(str(source))
        sets = read_mapping_sets(str(source))
        assert _behaviour_findings(swc, [], description, sets) == []

        unread_init = swc.model_copy(
            update={"variables": [v.model_copy(update={"init": None}) if v.name == PEAK else v for v in swc.variables]}
        )
        assert _behaviour_findings(unread_init, [], description, sets) == [], "a warning breaks nothing"

        dangling = swc.model_copy(update={"variables": [v for v in swc.variables if v.name != PEAK]})
        (finding,) = _behaviour_findings(dangling, [], description, sets)
        assert PEAK in finding

    def test_a_kept_variable_whose_type_the_file_withdrew_is_a_break(self, tmp_path: Path) -> None:
        """The overlay keeps the project's variables, judged against the file's types."""
        source = variable_model(tmp_path / "source")
        swc = _component(source)
        withdrawn = swc.model_copy(
            update={
                "variables": [
                    v.model_copy(update={"implementation_type": "Withdrawn_T", "init": None}) if v.name == PEAK else v
                    for v in swc.variables
                ]
            }
        )

        (finding,) = _behaviour_findings(
            withdrawn, [], read_data_description(str(source)), read_mapping_sets(str(source))
        )

        assert (
            finding == f"SWC '{COMPONENT}' variable '{PEAK}' references undeclared implementation type 'Withdrawn_T'."
        )


# A skip the import cannot carry


class TestTheIdentifierGuardJudgesTheVariables:
    """``illegal_identifiers`` asks a variable's name as declared, and its ``RV_``/``WV_`` form as generated.

    The generated form is charged only where the entry states no name and the variable's own name
    conforms -- ``check-model``'s rule.
    """

    @pytest.mark.parametrize(
        ("name", "why"),
        [("Bad Variable", "AUTOSAR Identifier matching"), ("S" * 129, "at most 128 characters")],
        ids=["pattern", "length"],
    )
    def test_a_variable_name_that_is_no_identifier_aborts_with_nothing_written(
        self, tmp_path: Path, name: str, why: str
    ) -> None:
        source = variable_model(tmp_path / "source")
        arxml = _replace_text(
            _export(source, tmp_path / "bad.arxml"),
            f"<SHORT-NAME>{SPARE}</SHORT-NAME>",
            f"<SHORT-NAME>{name}</SHORT-NAME>",
        )
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)
        with pytest.raises(ReservedNameError) as aborted:
            import_component(str(arxml), str(target))
        assert f"SWC '{COMPONENT}' variable name '{name}' is not a legal AUTOSAR Identifier: " in str(aborted.value)
        assert why in str(aborted.value)
        assert _tree(target) == before

    def test_a_generated_access_name_over_the_limit_aborts(self) -> None:
        swc = SWComponent(
            name=COMPONENT,
            type="application",
            variables=[Variable(name="V" * 126, access="explicit", implementation_type="uint8")],
            runnables=[
                Runnable(name="Run", reads_variables=[VariableReadRef(variable="V" * 126)]),
                Runnable(name="Named", reads_variables=[VariableReadRef(variable="V" * 126, name="RV_Short")]),
            ],
        )
        (violation,) = illegal_identifiers(swc, [], [])
        assert violation.startswith(
            f"SWC '{COMPONENT}' runnable 'Run' reads_variables variable name '{'V' * 126}' generates the SHORT-NAME "
            f"'RV_{'V' * 126}' (129 characters)"
        )


class TestANonDeferredSkipAbortsTheImport:
    def test_an_access_by_another_form_aborts_with_its_reason_and_nothing_written(self, tmp_path: Path) -> None:
        source = variable_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "component.arxml")
        text = arxml.read_text(encoding="utf-8")
        local = re.search(r"<LOCAL-VARIABLE-REF [^>]*>[^<]*/FilteredSpeed</LOCAL-VARIABLE-REF>", text)
        assert local is not None
        arxml.write_text(text.replace(local.group(0), "<AUTOSAR-VARIABLE-IREF/>", 1), encoding="utf-8", newline="\n")
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(arxml), str(target))

        assert "AUTOSAR-VARIABLE-IREF" in str(refusal.value)
        assert _tree(target) == before


# The differ


_COUNTER = Variable(name="Counter", access="explicit", implementation_type="uint8", init=0)
_FLAG = Variable(name="Flag", access="implicit", implementation_type="uint8")


def _state(
    variables: list[Variable], reads: list[VariableReadRef], writes: list[VariableWriteRef] | None = None
) -> ModelState:
    runnable = Runnable(name=WRITER, reads_variables=reads, writes_variables=writes or [])
    return ModelState(layers={"swc": [SWComponent(name=COMPONENT, variables=variables, runnables=[runnable])]})


def _collection(old: ModelState, new: ModelState, field: str):
    (swc,) = diff_model_states(old, new)["swc"].changed
    if field == "variables":
        (entry,) = [entry for entry in swc.collection_changes if entry.field == field]
        return entry
    (runnables,) = [entry for entry in swc.collection_changes if entry.field == "runnables"]
    (runnable,) = runnables.changed
    (entry,) = [entry for entry in runnable.collection_changes if entry.field == field]
    return entry


class TestTheDifferReportsVariablesByIdentity:
    """``swcraft diff`` reads the contract's identity through ``diff_model_states``; no code of its own.

    The identity is ``name`` on a variable, ``variable`` on an access; ``diff_model_states`` is the generic half
    it reaches.
    """

    def test_an_added_and_a_removed_variable(self) -> None:
        entry = _collection(_state([_COUNTER], []), _state([_FLAG], []), "variables")
        assert [item["name"] for item in entry.added] == ["Flag"]
        assert [item["name"] for item in entry.removed] == ["Counter"]
        assert entry.changed == []

    def test_a_changed_variable(self) -> None:
        entry = _collection(
            _state([_COUNTER], []),
            _state([_COUNTER.model_copy(update={"init": 1, "access": "implicit"})], []),
            "variables",
        )
        (changed,) = entry.changed
        assert changed.key == "Counter"
        assert sorted(changed.changed_fields) == [("access", "explicit", "implicit"), ("init", 0, 1)]

    def test_an_added_a_removed_and_a_changed_access(self) -> None:
        old = _state([_COUNTER, _FLAG], [VariableReadRef(variable="Counter")])
        added = _collection(old, _state([_COUNTER, _FLAG], [VariableReadRef(variable="Flag")]), "reads_variables")
        assert ([item["variable"] for item in added.added], [item["variable"] for item in added.removed]) == (
            ["Flag"],
            ["Counter"],
        )
        renamed = _collection(
            old, _state([_COUNTER, _FLAG], [VariableReadRef(variable="Counter", name="RV_Count")]), "reads_variables"
        )
        (changed,) = renamed.changed
        assert (changed.key, changed.changed_fields) == ("Counter", [("name", None, "RV_Count")])

    def test_an_added_a_removed_and_a_changed_write(self) -> None:
        old = _state([_COUNTER, _FLAG], [], [VariableWriteRef(variable="Counter")])
        moved = _collection(old, _state([_COUNTER, _FLAG], [], [VariableWriteRef(variable="Flag")]), "writes_variables")
        assert ([item["variable"] for item in moved.added], [item["variable"] for item in moved.removed]) == (
            ["Flag"],
            ["Counter"],
        )
        renamed = _collection(
            old,
            _state([_COUNTER, _FLAG], [], [VariableWriteRef(variable="Counter", name="WV_Count")]),
            "writes_variables",
        )
        (changed,) = renamed.changed
        assert (changed.key, changed.changed_fields) == ("Counter", [("name", None, "WV_Count")])
