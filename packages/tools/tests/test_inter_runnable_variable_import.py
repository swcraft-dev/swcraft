"""The reader and the importer of inter-runnable variables.

The synthetic export is the writer's own output of the shared fixture
(``inter_runnable_variable_support``), changed case by case by an ElementTree edit, so every case
starts from a document the exporter wrote and states the one difference it is about. Each case runs
the whole of ``import model`` (``run_import``) into a fresh project; the reader and the mapper are
asked directly only where the case is theirs alone.

**Not reached:** the real tool exports (the closed acceptance), and ``import component``'s three
regimes.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from inter_runnable_variable_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTERED,
    GEAR,
    LOGGER,
    MONITOR,
    PEAK,
    SPARE,
    WRITER,
    every_variable_shape_model,
    variable_model,
)
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import LAYER_APPLICATION_TYPES
from swcraft_tools.arxml_import.reconcile import reconcile_reserved_names
from swcraft_tools.arxml_import.registry import PathRegistry
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_import.swc_mapping import ImportedSwc
from swcraft_tools.arxml_reader import SkipCategory, StandardRule, read_arxml
from swcraft_tools.schema import ApplicationDataType, EnumDataType, SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

NS = "http://autosar.org/schema/r4.0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("xsi", XSI)

#: The behaviour the fixture's component is written with, and its variables' paths.
BEHAVIOUR_PATH = f"/ComponentTypes/{COMPONENT}/{COMPONENT}_InternalBehavior"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _named(root: ET.Element, tag: str, name: str) -> ET.Element:
    """The one *tag* element of *root* whose ``SHORT-NAME`` is *name*."""
    found = [el for el in root.iter(_q(tag)) if el.findtext(_q("SHORT-NAME")) == name]
    assert len(found) == 1, f"expected one {tag} named {name!r}, found {len(found)}"
    return found[0]


def _parent(root: ET.Element, child: ET.Element) -> ET.Element:
    return next(el for el in root.iter() if child in list(el))


def _export(
    tmp_path: Path,
    edit: Callable[[ET.Element], None] | None = None,
    *,
    model: Callable[[Path], Path] = variable_model,
    version: str = "4.2.2",
) -> Path:
    """The writer's output of *model*, changed by *edit* when one is given."""
    model_root = model(tmp_path / "model")
    source = tmp_path / "source.arxml"
    generate_arxml(str(model_root), str(source), version=version)
    if edit is not None:
        tree = ET.parse(source)
        edit(tree.getroot())
        tree.write(source, encoding="utf-8", xml_declaration=True)
    return source


def _import(tmp_path: Path, source: Path, packages: list[str] | None = None) -> tuple[ImportReport, Path]:
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"), packages=packages)
    return report, project / "model"


def _component(model_root: Path, name: str = COMPONENT) -> dict[str, Any]:
    (path,) = [path for path in (model_root / "swc").rglob(f"{name}.yaml")]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _variables(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {variable["name"]: variable for variable in document.get("variables", [])}


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return runnable


def _entries(report: ImportReport, tag: str | None = None) -> list[tuple[str, str, str]]:
    return [(entry.path, entry.tag, entry.reason) for entry in report.skip_entries if tag in (None, entry.tag)]


def _variable_entries(report: ImportReport) -> list[tuple[str, str, str]]:
    """Every entry the variables and their accesses took (the component's implementation aside)."""
    return [entry for entry in _entries(report) if entry[1] != "SWC-IMPLEMENTATION"]


def _committed_variables() -> dict[str, dict[str, Any]]:
    from inter_runnable_variable_support import VARIABLE_MODEL_ROOT

    document = yaml.safe_load((VARIABLE_MODEL_ROOT / COMPONENT_FILE).read_text(encoding="utf-8"))
    return _variables(document)


# The writer's own output comes back as it was written


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
class TestTheWritersOwnOutputComesBack:
    def test_every_variable_comes_back_with_its_role_type_value_description_and_access(
        self, tmp_path: Path, version: str
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert _variable_entries(report) == []
        assert _variables(_component(model_root)) == _committed_variables()

    def test_every_access_comes_back_in_its_list_with_a_stated_name_kept(self, tmp_path: Path, version: str) -> None:
        _report, model_root = _import(tmp_path, _export(tmp_path, version=version))
        document = _component(model_root)

        assert _runnable(document, WRITER)["writes_variables"] == [{"variable": FILTERED}]
        assert _runnable(document, LOGGER)["reads_variables"] == [{"variable": FILTERED, "name": "RV_Logged"}]
        monitor = _runnable(document, MONITOR)
        assert monitor["reads_variables"] == [{"variable": FILTERED}, {"variable": PEAK}]
        assert monitor["writes_variables"] == [{"variable": PEAK}]

    def test_the_report_counts_the_component_s_variables_and_accesses(self, tmp_path: Path, version: str) -> None:
        report, _model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert report.imported_variables == {COMPONENT: (3, 5)}
        assert f"  {COMPONENT}: 3 variables, 5 accesses" in report.render().splitlines()

    def test_the_imported_project_checks_clean_and_regenerates_the_variables_by_value(
        self, tmp_path: Path, version: str
    ) -> None:
        """The round trip: the import followed by ``generate arxml`` writes every variable and access back."""
        source = _export(tmp_path, version=version)
        _report, model_root = _import(tmp_path, source)
        assert check_cross_layer_consistency(str(model_root)).consistent
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)

        def facts(path: Path) -> tuple[Any, ...]:
            swc = read_arxml(str(path)).swcs[0]
            return (
                sorted(
                    (v.name, v.role, v.type_ref.rsplit("/", 1)[1], v.type_dest, v.description, v.sw_calibration_access)
                    for v in swc.variables
                ),
                sorted((v.name, repr(v.init_value)) for v in swc.variables),
                sorted(
                    (r.name, a.container, a.name, a.variable_ref.rsplit("/", 1)[1])
                    for r in swc.runnables
                    for a in r.local_accesses
                ),
            )

        assert facts(again) == facts(source)


def test_the_every_shape_model_s_values_come_back_by_value(tmp_path: Path) -> None:
    """A record, an array and an enumeration literal, written in place, read back in place."""
    report, model_root = _import(tmp_path, _export(tmp_path, model=every_variable_shape_model))
    variables = _variables(_component(model_root))

    assert _variable_entries(report) == []
    assert variables["SpeedFrameCache"]["init"] == {"count": 1, "peak": 2}
    assert variables["SpeedHistory"]["init"] == [3, 4]
    assert variables[GEAR] == {"name": GEAR, "access": "implicit", "application_type": "Gear", "init": "D"}
    # The port accesses beside the local ones are mapped as they always were.
    monitor = _runnable(_component(model_root), MONITOR)
    assert monitor["reads"] == [{"port": "rSpeed", "element": "value"}]
    assert monitor["calls"] == [{"port": "rReset", "operation": "Reset"}]


# The reader


class TestTheReader:
    def test_the_four_containers_are_read_and_report_nothing(self, tmp_path: Path) -> None:
        model = read_arxml(str(_export(tmp_path)))
        (swc,) = model.swcs

        assert [(v.path, v.role) for v in swc.variables] == [
            (f"{BEHAVIOUR_PATH}/{PEAK}", "EXPLICIT-INTER-RUNNABLE-VARIABLES"),
            (f"{BEHAVIOUR_PATH}/{SPARE}", "EXPLICIT-INTER-RUNNABLE-VARIABLES"),
            (f"{BEHAVIOUR_PATH}/{FILTERED}", "IMPLICIT-INTER-RUNNABLE-VARIABLES"),
        ]
        monitor = next(r for r in swc.runnables if r.name == MONITOR)
        assert [(a.container, a.name, a.reference_forms, a.scope) for a in monitor.local_accesses] == [
            ("READ-LOCAL-VARIABLES", f"RV_{FILTERED}", ("LOCAL-VARIABLE-REF",), None),
            ("READ-LOCAL-VARIABLES", f"RV_{PEAK}", ("LOCAL-VARIABLE-REF",), None),
            ("WRITTEN-LOCAL-VARIABLES", f"WV_{PEAK}", ("LOCAL-VARIABLE-REF",), None),
        ]
        # Kept apart from the port accesses, whose mapper resolves ports.
        assert monitor.accesses == []
        assert [entry for entry in model.skip_log if entry.tag != "SWC-IMPLEMENTATION"] == []

    def test_the_init_is_read_in_place_and_the_type_with_its_dest(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_export(tmp_path))).swcs
        filtered = next(v for v in swc.variables if v.name == FILTERED)

        assert (filtered.type_ref, filtered.type_dest) == (
            "/DataTypes/Application/VehicleSpeed",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        )
        assert filtered.init_value is not None and filtered.init_constant_ref is None
        assert (filtered.sw_calibration_access, filtered.sw_impl_policy) == ("READ-ONLY", None)

    def test_empty_wrappers_and_containers_write_and_report_nothing(self, tmp_path: Path) -> None:
        """An empty wrapper or container holds nothing to read or to lose."""

        def edit(root: ET.Element) -> None:
            behaviour = _named(root, "SWC-INTERNAL-BEHAVIOR", f"{COMPONENT}_InternalBehavior")
            for role in ("EXPLICIT-INTER-RUNNABLE-VARIABLES", "IMPLICIT-INTER-RUNNABLE-VARIABLES"):
                behaviour.remove(behaviour.find(_q(role)))
            for runnable in root.iter(_q("RUNNABLE-ENTITY")):
                for container in ("READ-LOCAL-VARIABLES", "WRITTEN-LOCAL-VARIABLES"):
                    for el in runnable.findall(_q(container)):
                        runnable.remove(el)
            # One empty wrapper and one empty container, where the writer would place them.
            behaviour.insert(3, ET.Element(_q("EXPLICIT-INTER-RUNNABLE-VARIABLES")))
            writer = _named(root, "RUNNABLE-ENTITY", WRITER)
            writer.append(ET.Element(_q("WRITTEN-LOCAL-VARIABLES")))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _variable_entries(report) == []
        document = _component(model_root)
        assert "variables" not in document
        assert "writes_variables" not in _runnable(document, WRITER)
        assert report.imported_variables == {}

    def test_a_child_of_a_wrapper_that_is_no_variable_is_the_selecting_container_s_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            wrapper = next(root.iter(_q("EXPLICIT-INTER-RUNNABLE-VARIABLES")))
            stranger = ET.SubElement(wrapper, _q("PARAMETER-DATA-PROTOTYPE"))
            ET.SubElement(stranger, _q("SHORT-NAME")).text = "Stranger"

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert (
            f"{BEHAVIOUR_PATH}/Stranger",
            "PARAMETER-DATA-PROTOTYPE",
            "unrecognized child inside EXPLICIT-INTER-RUNNABLE-VARIABLES (only VARIABLE-DATA-PROTOTYPE is read)",
        ) in _entries(report)

    def test_a_child_of_a_container_that_is_no_access_is_an_unrecognized_kind(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            container = _named(root, "RUNNABLE-ENTITY", WRITER).find(_q("WRITTEN-LOCAL-VARIABLES"))
            stranger = ET.SubElement(container, _q("PARAMETER-ACCESS"))
            ET.SubElement(stranger, _q("SHORT-NAME")).text = "Stranger"

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert (
            f"{BEHAVIOUR_PATH}/{WRITER}/Stranger",
            "PARAMETER-ACCESS",
            "unrecognized access kind inside WRITTEN-LOCAL-VARIABLES (only VARIABLE-ACCESS is read)",
        ) in _entries(report)


# The initial value


def _constant_reference_edit(root: ET.Element) -> None:
    """``PeakSpeed``'s value moved into a ``CONSTANT-SPECIFICATION`` it references (a referenced constant)."""
    variable = _named(root, "VARIABLE-DATA-PROTOTYPE", PEAK)
    init = variable.find(_q("INIT-VALUE"))
    (value,) = list(init)
    init.remove(value)
    reference = ET.SubElement(init, _q("CONSTANT-REFERENCE"))
    ET.SubElement(reference, _q("CONSTANT-REF"), DEST="CONSTANT-SPECIFICATION").text = "/Constants/PeakInit"
    packages = root.find(_q("AR-PACKAGES"))
    package = ET.SubElement(packages, _q("AR-PACKAGE"))
    ET.SubElement(package, _q("SHORT-NAME")).text = "Constants"
    elements = ET.SubElement(package, _q("ELEMENTS"))
    constant = ET.SubElement(elements, _q("CONSTANT-SPECIFICATION"))
    ET.SubElement(constant, _q("SHORT-NAME")).text = "PeakInit"
    spec = ET.SubElement(constant, _q("VALUE-SPEC"))
    numerical = ET.SubElement(spec, _q("NUMERICAL-VALUE-SPECIFICATION"))
    ET.SubElement(numerical, _q("VALUE")).text = "0x2A"


class TestTheInitialValue:
    def test_a_constant_reference_is_carried_by_value(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _constant_reference_edit))

        assert _variables(_component(model_root))[PEAK]["init"] == 42
        assert [entry for entry in _variable_entries(report) if entry[1] == "INIT-VALUE"] == []
        # The hexadecimal spelling is written decimal, and the report says so.
        assert f"{BEHAVIOUR_PATH}/{PEAK}: init read from 0x2A, written decimal" in report.init_radix_lines

    def test_an_in_place_value_is_carried_by_value(self, tmp_path: Path) -> None:
        _report, model_root = _import(tmp_path, _export(tmp_path))

        assert _variables(_component(model_root))[PEAK]["init"] == 0

    def test_a_reference_to_no_constant_is_a_loss_line_and_the_variable_is_kept(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _constant_reference_edit(root)
            constant = _named(root, "CONSTANT-SPECIFICATION", "PeakInit")
            _parent(root, constant).remove(constant)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "init" not in _variables(_component(model_root))[PEAK]
        assert _entries(report, "INIT-VALUE") == [
            (
                f"{BEHAVIOUR_PATH}/{PEAK}",
                "INIT-VALUE",
                "INIT-VALUE references '/Constants/PeakInit', which is not a CONSTANT-SPECIFICATION in this file",
            )
        ]

    def test_a_value_the_type_refuses_is_a_loss_line_and_the_variable_is_kept(self, tmp_path: Path) -> None:
        """A record value under a scalar type: the judge refuses it, and the read-without-init warning follows."""

        def edit(root: ET.Element) -> None:
            init = _named(root, "VARIABLE-DATA-PROTOTYPE", PEAK).find(_q("INIT-VALUE"))
            (value,) = list(init)
            init.remove(value)
            record = ET.SubElement(init, _q("RECORD-VALUE-SPECIFICATION"))
            fields = ET.SubElement(record, _q("FIELDS"))
            numerical = ET.SubElement(fields, _q("NUMERICAL-VALUE-SPECIFICATION"))
            ET.SubElement(numerical, _q("VALUE")).text = "1"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "init" not in _variables(_component(model_root))[PEAK]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "INIT-VALUE"]
        assert entry.path == f"{BEHAVIOUR_PATH}/{PEAK}"
        # The finding is not fixed by the importer: check-model warns on the read.
        result = check_cross_layer_consistency(str(model_root))
        assert result.consistent
        assert [issue.rule for issue in result.warnings] == ["swc.variable.read-without-init"]

    def test_an_init_value_holding_two_values_is_a_loss_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            init = _named(root, "VARIABLE-DATA-PROTOTYPE", PEAK).find(_q("INIT-VALUE"))
            init.append(copy.deepcopy(list(init)[0]))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "init" not in _variables(_component(model_root))[PEAK]
        ((path, _tag, reason),) = _entries(report, "INIT-VALUE")
        assert path == f"{BEHAVIOUR_PATH}/{PEAK}"
        assert reason.startswith("this reader could not follow it (INIT-VALUE is not a CONSTANT-REFERENCE")


# The type


def _retyped(type_ref: str, dest: str, variable: str = PEAK) -> Callable[[ET.Element], None]:
    def edit(root: ET.Element) -> None:
        tref = _named(root, "VARIABLE-DATA-PROTOTYPE", variable).find(_q("TYPE-TREF"))
        tref.text = type_ref
        tref.set("DEST", dest)

    return edit


class TestTheType:
    def test_an_implementation_type_declared_nowhere_becomes_a_stub_and_the_variable_is_kept(
        self, tmp_path: Path
    ) -> None:
        """A type declared nowhere becomes a stub; the ``init`` cannot be shaped against a stub and is one line."""
        report, model_root = _import(
            tmp_path, _export(tmp_path, _retyped("/Elsewhere/Types/Counter_t", "IMPLEMENTATION-DATA-TYPE"))
        )

        variable = _variables(_component(model_root))[PEAK]
        # The stub naming rule: `<leaf>_ext<X>`.
        assert variable == {"name": PEAK, "access": "explicit", "implementation_type": "Counter_t_ext0"}
        stub = yaml.safe_load(
            next((model_root / "data_types" / "implementation").rglob("Counter_t_ext0.yaml")).read_text(
                encoding="utf-8"
            )
        )
        assert (stub["kind"], stub["external_ref"]) == ("external_type", "/Elsewhere/Types/Counter_t")
        ((path, _tag, reason),) = _entries(report, "INIT-VALUE")
        assert path == f"{BEHAVIOUR_PATH}/{PEAK}"
        assert "external type stub 'Counter_t_ext0'" in reason
        assert check_cross_layer_consistency(str(model_root)).consistent
        # The accesses of a kept variable are kept with it.
        assert _runnable(_component(model_root), MONITOR)["writes_variables"] == [{"variable": PEAK}]

    def test_an_unreadable_init_on_a_type_declared_nowhere_is_one_line(self, tmp_path: Path) -> None:
        """The stub case's ``init`` line covers a value the reader could not follow as well."""

        def edit(root: ET.Element) -> None:
            _retyped("/Elsewhere/Types/Counter_t", "IMPLEMENTATION-DATA-TYPE")(root)
            init = _named(root, "VARIABLE-DATA-PROTOTYPE", PEAK).find(_q("INIT-VALUE"))
            (value,) = list(init)
            init.remove(value)
            ET.SubElement(init, _q("NUMERICAL-RULE-BASED-VALUE-SPECIFICATION"))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _variables(_component(model_root))[PEAK] == {
            "name": PEAK,
            "access": "explicit",
            "implementation_type": "Counter_t_ext0",
        }
        ((path, _tag, reason),) = _entries(report, "INIT-VALUE")
        assert path == f"{BEHAVIOUR_PATH}/{PEAK}"
        assert "external type stub 'Counter_t_ext0'" in reason

    def test_an_init_through_a_reference_chain_ending_at_a_stub_is_one_line(self, tmp_path: Path) -> None:
        """A declared ``TYPE_REFERENCE`` whose base becomes a stub: the ``init`` goes as a data element's does.

        Once the stub is minted the value meets ``rule:interface.init-on-external-type``, so the
        orchestrator drops it with one line, and its radix line with it.
        """

        def edit(root: ET.Element) -> None:
            uint16 = _named(root, "IMPLEMENTATION-DATA-TYPE", "uint16")
            alias = ET.Element(_q("IMPLEMENTATION-DATA-TYPE"))
            ET.SubElement(alias, _q("SHORT-NAME")).text = "LocalAlias"
            ET.SubElement(alias, _q("CATEGORY")).text = "TYPE_REFERENCE"
            conditional = ET.SubElement(
                ET.SubElement(ET.SubElement(alias, _q("SW-DATA-DEF-PROPS")), _q("SW-DATA-DEF-PROPS-VARIANTS")),
                _q("SW-DATA-DEF-PROPS-CONDITIONAL"),
            )
            ET.SubElement(
                conditional, _q("IMPLEMENTATION-DATA-TYPE-REF"), DEST="IMPLEMENTATION-DATA-TYPE"
            ).text = "/Elsewhere/Types/Counter_t"
            _parent(root, uint16).append(alias)
            _retyped("/DataTypes/Implementation/LocalAlias", "IMPLEMENTATION-DATA-TYPE")(root)
            # A hexadecimal spelling, so the radix line is asked about too.
            _named(root, "VARIABLE-DATA-PROTOTYPE", PEAK).find(f".//{_q('VALUE')}").text = "0x2A"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _variables(_component(model_root))[PEAK] == {
            "name": PEAK,
            "access": "explicit",
            "implementation_type": "LocalAlias",
        }
        assert _entries(report, "INIT-VALUE") == [
            (
                f"{BEHAVIOUR_PATH}/{PEAK}",
                "INIT-VALUE",
                "init: declared type 'LocalAlias' resolves through the reference chain 'LocalAlias' -> "
                "'Counter_t_ext0' to the external type stub 'Counter_t_ext0' (/Elsewhere/Types/Counter_t), whose "
                "content the export does not declare, so no init value can be shaped against it",
            )
        ]
        assert [line for line in report.init_radix_lines if PEAK in line] == []
        assert check_cross_layer_consistency(str(model_root)).consistent

    def test_an_application_type_declared_nowhere_is_a_skip_and_its_accesses_are_chained(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            _export(tmp_path, _retyped("/Elsewhere/Types/Speed_t", "APPLICATION-PRIMITIVE-DATA-TYPE", FILTERED)),
        )

        assert FILTERED not in _variables(_component(model_root))
        (skip,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-DATA-PROTOTYPE"]
        assert skip.path == f"{BEHAVIOUR_PATH}/{FILTERED}"
        assert skip.category == SkipCategory.OUTSIDE_FILE
        assert "no external stub exists for one" in skip.reason
        chained = [
            entry.path
            for entry in report.skip_entries
            if entry.tag == "VARIABLE-ACCESS"
            and entry.reason.startswith(f"chain-skipped: inter-runnable variable '{FILTERED}'")
        ]
        assert chained == [
            f"{BEHAVIOUR_PATH}/{WRITER}/WV_{FILTERED}",
            f"{BEHAVIOUR_PATH}/{LOGGER}/RV_Logged",
            f"{BEHAVIOUR_PATH}/{MONITOR}/RV_{FILTERED}",
        ]
        document = _component(model_root)
        assert "reads_variables" not in _runnable(document, LOGGER)
        assert _runnable(document, MONITOR)["reads_variables"] == [{"variable": PEAK}]

    def test_a_base_type_named_directly_is_the_file_s_defect(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path, _export(tmp_path, _retyped("/DataTypes/Base/uint16_base", "IMPLEMENTATION-DATA-TYPE"))
        )

        assert PEAK not in _variables(_component(model_root))
        (skip,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-DATA-PROTOTYPE"]
        assert (skip.category, skip.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.REFERENCE_DESTINATION_TYPE)

    def test_an_implementation_type_declared_and_not_kept_is_a_skip(self, tmp_path: Path) -> None:
        """A type the file declares and the type mapper skipped: no stub, a skip chained to nothing it re-words."""

        def edit(root: ET.Element) -> None:
            _retyped("/DataTypes/Implementation/Broken_t", "IMPLEMENTATION-DATA-TYPE")(root)
            uint16 = _named(root, "IMPLEMENTATION-DATA-TYPE", "uint16")
            broken = copy.deepcopy(uint16)
            broken.find(_q("SHORT-NAME")).text = "Broken_t"
            broken.find(_q("CATEGORY")).text = "NOT-A-CATEGORY"
            _parent(root, uint16).append(broken)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert PEAK not in _variables(_component(model_root))
        (skip,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-DATA-PROTOTYPE"]
        assert skip.reason == "unresolved reference /DataTypes/Implementation/Broken_t"

    def test_a_variable_typed_by_a_renamed_application_type_follows_the_rename(self) -> None:
        """Reserved-name reconciliation: ``Gear_CompuMethod`` beside the enumeration ``Gear`` is renamed.

        Asked of the pass directly: a file the writer wrote cannot hold the pair.
        """
        registry = PathRegistry()
        registry.register("/DataTypes/Application/Gear", "Gear", LAYER_APPLICATION_TYPES)
        registry.register("/DataTypes/Application/Gear_CompuMethod", "Gear_CompuMethod", LAYER_APPLICATION_TYPES)
        gear = EnumDataType.model_validate({"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]})
        reserved = ApplicationDataType.model_validate({"name": "Gear_CompuMethod", "kind": "application_type"})
        swc = SWComponent.model_validate(
            {
                "name": COMPONENT,
                "type": "application",
                "variables": [{"name": FILTERED, "access": "implicit", "application_type": "Gear_CompuMethod"}],
            }
        )

        reconciled = reconcile_reserved_names(
            [gear, reserved], [], [ImportedSwc(swc=swc, group="chassis", path="/C")], [], [], registry, []
        )

        renamed = {dt.name for dt in reconciled.data_types} - {"Gear"}
        assert renamed == {"Gear_CompuMethod_app"}
        assert [variable.application_type for variable in reconciled.swcs[0].swc.variables] == ["Gear_CompuMethod_app"]


# The losses of a kept variable or access


def _conditional(root: ET.Element, variable: str) -> ET.Element:
    """The variable's ``SW-DATA-DEF-PROPS-CONDITIONAL``, created when it has none."""
    vdp = _named(root, "VARIABLE-DATA-PROTOTYPE", variable)
    props = vdp.find(_q("SW-DATA-DEF-PROPS"))
    if props is None:
        props = ET.Element(_q("SW-DATA-DEF-PROPS"))
        vdp.insert(1, props)
        ET.SubElement(ET.SubElement(props, _q("SW-DATA-DEF-PROPS-VARIANTS")), _q("SW-DATA-DEF-PROPS-CONDITIONAL"))
    return props.find(f"{_q('SW-DATA-DEF-PROPS-VARIANTS')}/{_q('SW-DATA-DEF-PROPS-CONDITIONAL')}")


class TestTheLossLines:
    @pytest.mark.parametrize(
        ("policy", "effect"), [("STANDARD", "report"), ("QUEUED", "abort"), ("MEASUREMENT-POINT", "abort")]
    )
    def test_a_stated_sw_impl_policy_is_one_line(self, tmp_path: Path, policy: str, effect: str) -> None:
        """Every stated policy is the variable's own line, never the data element's."""

        def edit(root: ET.Element) -> None:
            ET.SubElement(_conditional(root, PEAK), _q("SW-IMPL-POLICY")).text = policy

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert PEAK in _variables(_component(model_root))
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "SW-IMPL-POLICY"]
        assert (entry.path, entry.effect) == (f"{BEHAVIOUR_PATH}/{PEAK}/SW-DATA-DEF-PROPS", effect)
        assert entry.reason.startswith(f"SW-IMPL-POLICY {policy} on an inter-runnable variable not carried")

    def test_a_queued_policy_beside_a_calibration_access_keeps_the_access(self, tmp_path: Path) -> None:
        """A variable holds no policy, so ``[constr_1017]``'s queued combinations never drop its access.

        The access is carried beside the standard's default policy the output writes, which every
        access may accompany (Table 5.50).
        """

        def edit(root: ET.Element) -> None:
            # After the variable's SW-CALIBRATION-ACCESS READ-ONLY, the schema's order.
            ET.SubElement(_conditional(root, FILTERED), _q("SW-IMPL-POLICY")).text = "QUEUED"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _variables(_component(model_root))[FILTERED]["sw_calibration_access"] == "read_only"
        assert [entry.reason.split(":")[0] for entry in report.skip_entries if entry.tag == "SW-IMPL-POLICY"] == [
            "SW-IMPL-POLICY QUEUED on an inter-runnable variable not carried"
        ]
        # The data element's [constr_1017] line ("the policy is carried and the calibration access is not")
        # would say the opposite of what the mapping keeps.
        assert _entries(report, "SW-CALIBRATION-ACCESS") == []

    def test_a_calibration_access_no_model_value_holds_names_the_variable(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _conditional(root, FILTERED).find(_q("SW-CALIBRATION-ACCESS")).text = "NOT-A-LITERAL"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "sw_calibration_access" not in _variables(_component(model_root))[FILTERED]
        assert _entries(report, "SW-CALIBRATION-ACCESS") == [
            (
                f"{BEHAVIOUR_PATH}/{FILTERED}/SW-DATA-DEF-PROPS",
                "SW-CALIBRATION-ACCESS",
                "SW-CALIBRATION-ACCESS NOT-A-LITERAL on an inter-runnable variable is not a value SWCraft carries -- "
                "not imported or preserved",
            )
        ]

    def test_other_sw_data_def_props_content_is_one_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            ET.SubElement(_conditional(root, FILTERED), _q("DISPLAY-FORMAT")).text = "%6.2f"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        variable = _variables(_component(model_root))[FILTERED]
        assert variable["sw_calibration_access"] == "read_only"
        assert [entry for entry in _entries(report, "DISPLAY-FORMAT")] == [
            (
                f"{BEHAVIOUR_PATH}/{FILTERED}/SW-DATA-DEF-PROPS",
                "DISPLAY-FORMAT",
                "DISPLAY-FORMAT in the SW-DATA-DEF-PROPS of a VARIABLE-DATA-PROTOTYPE is not read by SWCraft -- "
                "not imported or preserved",
            )
        ]

    def test_a_scope_on_an_access_is_one_line_and_the_access_is_kept(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            ET.SubElement(
                _named(root, "VARIABLE-ACCESS", f"WV_{FILTERED}"), _q("SCOPE")
            ).text = "COMMUNICATION-INTRA-PARTITION"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _runnable(_component(model_root), WRITER)["writes_variables"] == [{"variable": FILTERED}]
        assert _entries(report, "SCOPE") == [
            (
                f"{BEHAVIOUR_PATH}/{WRITER}/WV_{FILTERED}",
                "SCOPE",
                "SCOPE COMMUNICATION-INTRA-PARTITION of a VARIABLE-ACCESS in WRITTEN-LOCAL-VARIABLES not carried: "
                "SWCraft models no access scope and writes none",
            )
        ]


# A variation point on an access: no silent loss

#: The every-shape copy's behaviour, which its stated package and behaviour name place.
SHAPE_BEHAVIOUR_PATH = f"/ComponentTypes/Chassis/{COMPONENT}/SpeedBehaviour"


def _variation_point(owner: ET.Element, before: str | None = None) -> None:
    """A ``VARIATION-POINT`` with a system condition in *owner*, before its *before* child or last."""
    point = ET.Element(_q("VARIATION-POINT"))
    ET.SubElement(point, _q("SHORT-LABEL")).text = "Variant"
    condition = ET.SubElement(point, _q("SW-SYSCOND"), {"BINDING-TIME": "PRE-COMPILE-TIME"})
    condition.text = "0"
    children = list(owner)
    index = len(children) if before is None else children.index(owner.find(_q(before)))
    owner.insert(index, point)


def _unread(owner_tag: str) -> str:
    """The closed tables' sentence for an unread child (``arxml_reader._unread_reason``)."""
    return (
        f"VARIATION-POINT of a {owner_tag} is not read by SWCraft -- no SWCraft representation, "
        "not imported or preserved"
    )


class TestAVariationPointOnAnAccessIsOneLine:
    """A stated ``VARIATION-POINT`` on any access the reader reads is one line, the access kept.

    The access is written unconditionally, so the condition is lost: one line at the access, the closed
    tables' unread line a point on the variable itself takes. The schema places the point last in a
    ``VARIABLE-ACCESS`` and after ``TIMEOUT`` in a ``SYNCHRONOUS-SERVER-CALL-POINT`` (R4.2.2 and R4.3.0).
    """

    def test_on_a_local_access(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path, _export(tmp_path, lambda root: _variation_point(_named(root, "VARIABLE-ACCESS", "RV_Logged")))
        )

        assert _runnable(_component(model_root), LOGGER)["reads_variables"] == [
            {"variable": FILTERED, "name": "RV_Logged"}
        ]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIATION-POINT"]
        assert (entry.path, entry.reason) == (f"{BEHAVIOUR_PATH}/{LOGGER}/RV_Logged", _unread("VARIABLE-ACCESS"))
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "abort")

    def test_on_a_port_variable_access(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _variation_point(_named(root, "VARIABLE-ACCESS", "R_rSpeed_value"))

        report, model_root = _import(tmp_path, _export(tmp_path, edit, model=every_variable_shape_model))

        assert _runnable(_component(model_root), MONITOR)["reads"] == [{"port": "rSpeed", "element": "value"}]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIATION-POINT"]
        assert (entry.path, entry.reason) == (
            f"{SHAPE_BEHAVIOUR_PATH}/{MONITOR}/R_rSpeed_value",
            _unread("VARIABLE-ACCESS"),
        )
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "abort")

    def test_on_a_server_call_point(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            point = _named(root, "SYNCHRONOUS-SERVER-CALL-POINT", "Call_rReset_Reset")
            _variation_point(point, before="TIMEOUT" if point.find(_q("TIMEOUT")) is not None else None)

        report, model_root = _import(tmp_path, _export(tmp_path, edit, model=every_variable_shape_model))

        assert _runnable(_component(model_root), MONITOR)["calls"] == [{"port": "rReset", "operation": "Reset"}]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIATION-POINT"]
        assert (entry.path, entry.reason) == (
            f"{SHAPE_BEHAVIOUR_PATH}/{MONITOR}/Call_rReset_Reset",
            _unread("SYNCHRONOUS-SERVER-CALL-POINT"),
        )

    def test_an_empty_point_is_no_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            ET.SubElement(_named(root, "VARIABLE-ACCESS", "RV_Logged"), _q("VARIATION-POINT"))

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report, "VARIATION-POINT") == []

    def test_the_precedent_on_the_variable_itself(self, tmp_path: Path) -> None:
        """The closed table's line, one per item the point holds -- the sentence the access lines take."""
        report, _model_root = _import(
            tmp_path,
            _export(
                tmp_path, lambda root: _variation_point(_named(root, "VARIABLE-DATA-PROTOTYPE", PEAK), "TYPE-TREF")
            ),
        )

        reasons = {entry.reason for entry in report.skip_entries if entry.path.startswith(f"{BEHAVIOUR_PATH}/{PEAK}")}
        assert _unread("VARIABLE-DATA-PROTOTYPE") in reasons


# The accesses


def _accessed(root: ET.Element, access: str) -> ET.Element:
    return _named(root, "VARIABLE-ACCESS", access).find(_q("ACCESSED-VARIABLE"))


class TestTheAccesses:
    @pytest.mark.parametrize("form", ["AUTOSAR-VARIABLE-IREF", "AUTOSAR-VARIABLE-IN-IMPL-DATATYPE"])
    def test_an_access_by_another_form_is_the_file_s_defect(self, tmp_path: Path, form: str) -> None:
        def edit(root: ET.Element) -> None:
            accessed = _accessed(root, f"WV_{FILTERED}")
            for child in list(accessed):
                accessed.remove(child)
            ET.SubElement(accessed, _q(form))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "writes_variables" not in _runnable(_component(model_root), WRITER)
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-ACCESS"]
        assert entry.path == f"{BEHAVIOUR_PATH}/{WRITER}/WV_{FILTERED}"
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.LOCAL_VARIABLE_REFERENCE)
        assert form in entry.reason

    def test_a_local_reference_beside_another_form_is_judged_whole(self, tmp_path: Path) -> None:
        """``AUTOSAR-VARIABLE-REF`` is a sequence: the second form is not ignored."""

        def edit(root: ET.Element) -> None:
            accessed = _accessed(root, f"WV_{FILTERED}")
            accessed.insert(0, ET.Element(_q("AUTOSAR-VARIABLE-IREF")))

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-ACCESS"]
        assert entry.rule == StandardRule.LOCAL_VARIABLE_REFERENCE

    def test_a_reference_into_another_behaviour_is_the_file_s_defect(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            reference = _accessed(root, f"WV_{FILTERED}").find(_q("LOCAL-VARIABLE-REF"))
            reference.text = f"/ComponentTypes/Other/Other_InternalBehavior/{FILTERED}"

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-ACCESS"]
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.LOCAL_VARIABLE_OWNER)
        assert entry.reason == (
            f"its LOCAL-VARIABLE-REF /ComponentTypes/Other/Other_InternalBehavior/{FILTERED} names no "
            "inter-runnable variable of its own behaviour"
        )

    def test_an_access_without_a_local_reference_is_a_skip(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            accessed = _accessed(root, f"WV_{FILTERED}")
            for child in list(accessed):
                accessed.remove(child)

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert [entry[2] for entry in _entries(report, "VARIABLE-ACCESS")] == [
            "the local variable access states no LOCAL-VARIABLE-REF"
        ]

    def test_two_accesses_to_one_variable_in_one_container_collapse_with_a_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            access = _named(root, "VARIABLE-ACCESS", f"WV_{FILTERED}")
            again = copy.deepcopy(access)
            again.find(_q("SHORT-NAME")).text = "WV_Again"
            _parent(root, access).append(again)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _runnable(_component(model_root), WRITER)["writes_variables"] == [{"variable": FILTERED}]
        assert report.access_collapse_lines == [
            f"{COMPONENT}/{WRITER}: writes_variables access {FILTERED} declared twice -- "
            f"'WV_Again' collapsed into 'WV_{FILTERED}'"
        ]
        assert _variable_entries(report) == []

    def test_two_reads_of_one_variable_in_one_container_collapse_with_a_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            access = _named(root, "VARIABLE-ACCESS", f"RV_{FILTERED}")
            again = copy.deepcopy(access)
            again.find(_q("SHORT-NAME")).text = "RV_Again"
            _parent(root, access).append(again)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))
        _plain_report, plain_root = _import(tmp_path / "plain", _export(tmp_path / "plain"))

        reads = _runnable(_component(model_root), MONITOR)["reads_variables"]
        assert reads == _runnable(_component(plain_root), MONITOR)["reads_variables"]
        assert report.access_collapse_lines == [
            f"{COMPONENT}/{MONITOR}: reads_variables access {FILTERED} declared twice -- "
            f"'RV_Again' collapsed into 'RV_{FILTERED}'"
        ]
        assert _variable_entries(report) == []

    def test_a_reference_into_its_own_behaviour_naming_no_variable_is_the_file_s_defect(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _accessed(root, f"WV_{FILTERED}").find(_q("LOCAL-VARIABLE-REF")).text = f"{BEHAVIOUR_PATH}/NoSuchVariable"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "writes_variables" not in _runnable(_component(model_root), WRITER)
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIABLE-ACCESS"]
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.LOCAL_VARIABLE_OWNER)
        assert entry.reason == (
            f"its LOCAL-VARIABLE-REF {BEHAVIOUR_PATH}/NoSuchVariable names no inter-runnable variable of its own "
            "behaviour"
        )

    def test_a_skipped_runnable_chain_skips_its_local_accesses(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _named(root, "RUNNABLE-ENTITY", LOGGER).find(_q("MINIMUM-START-INTERVAL")).text = "-1"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert LOGGER not in [runnable["name"] for runnable in _component(model_root)["runnables"]]
        assert (
            f"{BEHAVIOUR_PATH}/{LOGGER}/RV_Logged",
            "VARIABLE-ACCESS",
        ) in [(path, tag) for path, tag, reason in _entries(report) if reason.startswith("chain-skipped: runnable")]

    def test_a_stated_name_equal_to_the_derived_one_is_not_stated(self, tmp_path: Path) -> None:
        _report, model_root = _import(tmp_path, _export(tmp_path))

        assert _runnable(_component(model_root), WRITER)["writes_variables"] == [{"variable": FILTERED}]

    def test_a_stated_name_colliding_with_a_port_access_is_not_stated_and_says_why(self, tmp_path: Path) -> None:
        """The local accesses share the runnable's name space with the port accesses (one settling scope)."""

        def edit(root: ET.Element) -> None:
            _named(root, "VARIABLE-ACCESS", f"RV_{FILTERED}").find(_q("SHORT-NAME")).text = "r_rspeed_value"

        report, model_root = _import(tmp_path, _export(tmp_path, edit, model=every_variable_shape_model))

        monitor = _runnable(_component(model_root), MONITOR)
        assert monitor["reads_variables"][0] == {"variable": FILTERED}
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "SHORT-NAME"]
        assert entry.path == f"/ComponentTypes/Chassis/{COMPONENT}/SpeedBehaviour/{MONITOR}/r_rspeed_value"
        assert "would collide with the access point SWCraft writes as 'R_rSpeed_value'" in entry.reason


# The identifier guard


def _renamed_variable(old: str, new: str) -> Callable[[ET.Element], None]:
    """The variable *old* declared as *new*, every ``LOCAL-VARIABLE-REF`` to it following."""

    def edit(root: ET.Element) -> None:
        _named(root, "VARIABLE-DATA-PROTOTYPE", old).find(_q("SHORT-NAME")).text = new
        for reference in root.iter(_q("LOCAL-VARIABLE-REF")):
            if reference.text == f"{BEHAVIOUR_PATH}/{old}":
                reference.text = f"{BEHAVIOUR_PATH}/{new}"

    return edit


class TestTheIdentifierGuard:
    """A variable's ``SHORT-NAME`` is a declared name, refused as a port's or a runnable's is.

    ``reconcile.identifier_violations`` aborts the import with nothing written. A stated access name
    that is no Identifier is the settling pass's, with family F19's line (``test_stated_names_import.py``).
    """

    @pytest.mark.parametrize(
        ("variable", "name", "why"),
        [(SPARE, "Bad Variable", "AUTOSAR Identifier matching"), (PEAK, "P" * 129, "at most 128 characters")],
        ids=["pattern", "length"],
    )
    def test_a_variable_name_that_is_no_identifier_aborts_with_nothing_written(
        self, tmp_path: Path, variable: str, name: str, why: str
    ) -> None:
        source = _export(tmp_path, _renamed_variable(variable, name))
        with pytest.raises(ValueError, match="Import aborted with nothing written") as aborted:
            _import(tmp_path, source)
        assert f"SWC '{COMPONENT}' variable '{name}': " in str(aborted.value)
        assert why in str(aborted.value)
        assert not list((tmp_path / "project" / "model" / "swc").rglob("*.yaml"))

    def test_a_stated_access_name_that_is_no_identifier_is_declined_with_its_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _named(root, "VARIABLE-ACCESS", "RV_Logged").find(_q("SHORT-NAME")).text = "Bad Access"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _runnable(_component(model_root), LOGGER)["reads_variables"] == [{"variable": FILTERED}]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "SHORT-NAME"]
        assert entry.path == f"{BEHAVIOUR_PATH}/{LOGGER}/Bad Access"
        assert "because the source's name is not a legal AUTOSAR Identifier" in entry.reason
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "report")


# Two declarations of one component


def _duplicated(change: Callable[[ET.Element], None] | None = None) -> Callable[[ET.Element], None]:
    """The component declared a second time in a package of its own, *change* applied to the copy."""

    def edit(root: ET.Element) -> None:
        component = _named(root, "APPLICATION-SW-COMPONENT-TYPE", COMPONENT)
        twin = copy.deepcopy(component)
        for reference in twin.iter(_q("LOCAL-VARIABLE-REF")):
            reference.text = reference.text.replace("/ComponentTypes/", "/ComponentTypes/Copy/")
        for reference in twin.iter(_q("START-ON-EVENT-REF")):
            reference.text = reference.text.replace("/ComponentTypes/", "/ComponentTypes/Copy/")
        if change is not None:
            change(twin)
        component_types = _named(root, "AR-PACKAGE", "ComponentTypes")
        packages = component_types.find(_q("AR-PACKAGES"))
        if packages is None:
            packages = ET.SubElement(component_types, _q("AR-PACKAGES"))
        package = ET.SubElement(packages, _q("AR-PACKAGE"))
        ET.SubElement(package, _q("SHORT-NAME")).text = "Copy"
        ET.SubElement(ET.SubElement(package, _q("ELEMENTS")), twin.tag).extend(list(twin))

    return edit


class TestTwoDeclarationsOfOneComponent:
    def test_two_equal_declarations_merge(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated()))

        assert [group for group in report.conflicting_groups] == []
        assert _variables(_component(model_root)) == _committed_variables()

    @pytest.mark.parametrize("difference", ["type", "role", "init", "calibration access", "policy"])
    def test_a_differing_variable_is_a_conflict(self, tmp_path: Path, difference: str) -> None:
        """Each fact the ``INTER-RUNNABLE-VARIABLES`` signature signs, changed alone in the second declaration."""

        def change(twin: ET.Element) -> None:
            peak = _named(twin, "VARIABLE-DATA-PROTOTYPE", PEAK)
            if difference == "type":
                peak.find(_q("TYPE-TREF")).text = "/DataTypes/Implementation/uint8"
            elif difference == "role":
                _parent(twin, peak).remove(peak)
                next(twin.iter(_q("IMPLICIT-INTER-RUNNABLE-VARIABLES"))).append(peak)
            elif difference == "init":
                peak.find(f"{_q('INIT-VALUE')}//{_q('VALUE')}").text = "7"
            elif difference == "calibration access":
                next(twin.iter(_q("SW-CALIBRATION-ACCESS"))).text = "READ-WRITE"
            else:
                ET.SubElement(_conditional(twin, PEAK), _q("SW-IMPL-POLICY")).text = "STANDARD"

        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated(change)))

        assert len(report.conflicting_groups) == 1
        assert not list((model_root / "swc").rglob(f"{COMPONENT}.yaml"))

    def test_a_differing_local_access_is_a_conflict(self, tmp_path: Path) -> None:
        def change(twin: ET.Element) -> None:
            reference = _named(twin, "VARIABLE-ACCESS", f"RV_{PEAK}").find(
                f"{_q('ACCESSED-VARIABLE')}/{_q('LOCAL-VARIABLE-REF')}"
            )
            reference.text = reference.text.replace(PEAK, SPARE)

        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated(change)))

        assert len(report.conflicting_groups) == 1
        assert not list((model_root / "swc").rglob(f"{COMPONENT}.yaml"))


# --packages


def test_packages_keeps_the_types_only_a_portless_component_s_variables_use(tmp_path: Path) -> None:
    """The closure is seeded from the variables: the fixture's component has no port at all."""
    report, model_root = _import(tmp_path, _export(tmp_path), packages=["ComponentTypes"])

    assert _variables(_component(model_root)) == _committed_variables()
    kept = {path.stem for path in (model_root / "data_types").rglob("*.yaml")}
    assert {"VehicleSpeed", "uint16", "uint8", "uint16_base", "uint8_base", "SpeedMapping"} <= kept
    assert check_cross_layer_consistency(str(model_root)).consistent
    assert _variable_entries(report) == []
