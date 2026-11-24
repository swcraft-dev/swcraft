"""Tests for import component under the init-value judgement: every init kind exchanges, an uncarried one aborts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import write_model_element
from project_support import files_of, init_project
from swcraft_tools.arxml_import.type_mapping import VALUE_UNIT_SENTENCE
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import init_constant_name

# Component exchange under the init-value judgement: `import component` maps
# through the same `map_interfaces` / `map_swcs` as `import model`, so an init
# value is judged by the file's own types on this path too. A component whose
# values the writer wrote in the forms the standard asks exchanges with no
# line; a file with an init that is not carried aborts on that element's line,
# recorded with its category and rule and cited in the abort.


def _valued_component_root(tmp_path: Path) -> Path:
    """``SignalsSwc``'s model with an init value of every kind the writer emits.

    ``fixtures/init_values``' tree as ``test_application_value_form`` extends it, plus a
    value type referencing a texttable and a data element stating one of its entries by
    name. Built under *tmp_path*: the recorded tree is not extended.
    """
    from test_application_value_form import application_value_model_root

    model_root = application_value_model_root(tmp_path)
    write_model_element(
        model_root,
        {
            "name": "ExchangeModeTable",
            "kind": "texttable",
            "entries": [{"name": "IDLE", "value": 0}, {"name": "BUSY", "value": 1}],
        },
    )
    write_model_element(
        model_root,
        {"name": "ExchangeMode", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ExchangeModeTable"},
    )
    interface = model_root / "interfaces" / "ISignals.yaml"
    document = yaml.safe_load(interface.read_text(encoding="utf-8"))
    document["data_elements"].append({"name": "own_table", "implementation_type": "ExchangeMode", "init": "BUSY"})
    interface.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return model_root


_AR = "{http://autosar.org/schema/r4.0}"


def _value_of_constant(root: Any, constant: str) -> Any:
    """The value specification under the ``CONSTANT-SPECIFICATION`` named *constant*."""
    (specification,) = [
        node for node in root.iter(f"{_AR}CONSTANT-SPECIFICATION") if node.findtext(f"{_AR}SHORT-NAME") == constant
    ]
    (value,) = list(specification.find(f"{_AR}VALUE-SPEC"))
    return value


def _value_in_place(root: Any, port: str, element: str) -> Any:
    """The value specification a com-spec of *port* states in place for *element*."""
    (port_node,) = [node for node in root.iter(f"{_AR}P-PORT-PROTOTYPE") if node.findtext(f"{_AR}SHORT-NAME") == port]
    for com_spec in port_node.iter():
        reference = com_spec.find(f"{_AR}DATA-ELEMENT-REF")
        init_value = com_spec.find(f"{_AR}INIT-VALUE")
        if reference is not None and init_value is not None and reference.text.endswith(f"/{element}"):
            (value,) = list(init_value)
            return value
    raise AssertionError(f"no in-place value for {element!r} on {port!r}")


def _as_numerical(value: Any, number: str) -> None:
    """Rewrite *value* in place into a ``NUMERICAL-VALUE-SPECIFICATION`` of *number*, its label kept."""
    from lxml import etree

    label = value.find(f"{_AR}SHORT-LABEL")
    for child in list(value):
        value.remove(child)
    value.tag = f"{_AR}NUMERICAL-VALUE-SPECIFICATION"
    if label is not None:
        value.append(label)
    etree.SubElement(value, f"{_AR}VALUE").text = number


def _as_application(value: Any) -> None:
    """Rewrite a ``NUMERICAL-VALUE-SPECIFICATION`` in place into the application form of its number."""
    from lxml import etree

    label = value.find(f"{_AR}SHORT-LABEL")
    number = value.findtext(f"{_AR}VALUE")
    for child in list(value):
        value.remove(child)
    value.tag = f"{_AR}APPLICATION-VALUE-SPECIFICATION"
    if label is not None:
        value.append(label)
    etree.SubElement(value, f"{_AR}CATEGORY").text = "VALUE"
    content = etree.SubElement(value, f"{_AR}SW-VALUE-CONT")
    etree.SubElement(etree.SubElement(content, f"{_AR}SW-VALUES-PHYS"), f"{_AR}V").text = number


def _init_lines(lines: list[str]) -> list[str]:
    """The lines of a report or an abort that speak of an init value or its constant."""
    return [line for line in lines if line.startswith(("INIT-VALUE ", "VALUE-SPEC "))]


class TestEveryInitKindExchangesUnderTheJudgement:
    """A component whose init values SWCraft wrote exchanges with no line.

    Every kind :func:`_valued_component_root` states exports and imports into an empty
    project and into its own, at both versions, with no line and every value back.

    Does not reach: a component file an earlier SWCraft release wrote (an enumeration as a
    number, an application primitive's own value in the numerical form); it is the same
    refusal as :class:`TestAnInitThatIsNotCarriedAbortsTheComponentImport`'s, with no
    fixture of its own.
    """

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_into_an_empty_project_every_value_comes_back_with_no_line(self, tmp_path: Path, version: str) -> None:
        source_root = _valued_component_root(tmp_path)
        arxml = tmp_path / "SignalsSwc.arxml"
        export_component(str(source_root), "SignalsSwc", str(arxml), version=version)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert _init_lines([*report.not_carried_lines, *report.skip_lines]) == []
        source = {i.name: i for i in read_interfaces(str(source_root))}["ISignals"]
        imported = {i.name: i for i in read_interfaces(str(project / "model"))}["ISignals"]
        assert [(de.name, de.init) for de in imported.data_elements] == [
            (de.name, de.init) for de in source.data_elements
        ]
        assert sum(de.init is not None for de in imported.data_elements) == 22, "precondition: every kind states one"
        (swc,) = read_swc_model(str(project / "model"))
        (source_swc,) = read_swc_model(str(source_root))
        assert {p.name: p.com_specs for p in swc.ports} == {p.name: p.com_specs for p in source_swc.ports}
        # A component import brings no requirement, so the requirement-link finding stands; no
        # finding speaks of an init value.
        issues = check_cross_layer_consistency(str(project / "model")).issues
        assert [issue for issue in issues if "init" in issue.lower()] == []

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_into_its_own_project_everything_is_reused_and_nothing_is_written(
        self, tmp_path: Path, version: str
    ) -> None:
        source_root = _valued_component_root(tmp_path)
        arxml = tmp_path / "SignalsSwc.arxml"
        export_component(str(source_root), "SignalsSwc", str(arxml), version=version)
        before = files_of(source_root)

        report = import_component(str(arxml), str(source_root))

        assert report.swc_action == "unchanged"
        assert report.created_interfaces == []
        assert report.reused_interfaces == ["ISignals"]
        assert report.written_paths == []
        assert _init_lines([*report.not_carried_lines, *report.skip_lines]) == []
        assert files_of(source_root) == before

    def test_the_export_states_each_value_in_the_form_the_judgement_takes(self, tmp_path: Path) -> None:
        """The premise of the two cases above: the file really holds each form.

        A writer that fell back to the numerical form everywhere would fail here, not pass them vacuously."""
        from lxml import etree

        source_root = _valued_component_root(tmp_path)
        arxml = tmp_path / "SignalsSwc.arxml"
        export_component(str(source_root), "SignalsSwc", str(arxml))
        root = etree.parse(str(arxml)).getroot()

        def form(element: str) -> str:
            return etree.QName(_value_of_constant(root, init_constant_name("ISignals", element))).localname

        assert form("speed") == "NUMERICAL-VALUE-SPECIFICATION"
        assert form("own_unit") == "APPLICATION-VALUE-SPECIFICATION"
        assert form("mode") == "APPLICATION-VALUE-SPECIFICATION"
        assert form("own_table") == "TEXT-VALUE-SPECIFICATION"
        assert form("limits") == "RECORD-VALUE-SPECIFICATION"
        unit = _value_of_constant(root, init_constant_name("ISignals", "own_unit")).find(
            f"{_AR}SW-VALUE-CONT/{_AR}UNIT-REF"
        )
        assert unit is not None, "an application primitive with a unit states it in the value"


class TestAnInitThatIsNotCarriedAbortsTheComponentImport:
    """A file with an init that is not carried aborts on that element's line.

    Each case edits one value of :func:`_valued_component_root`'s export into a refused
    form: nothing written, the line in ``skip_lines`` at the ``INIT-VALUE`` key, cited
    under its section in the abort message. A member's number in the numerical or the
    application form imports; a port's own value is a com-spec value on a descriptive
    port, its line printed and the component imported.
    """

    _DEFECTS = "Defects of the export"

    @staticmethod
    def _edited(tmp_path: Path, edit: Any) -> Path:
        from lxml import etree

        source_root = _valued_component_root(tmp_path)
        arxml = tmp_path / "SignalsSwc.arxml"
        export_component(str(source_root), "SignalsSwc", str(arxml))
        tree = etree.parse(str(arxml))
        edit(tree.getroot())
        tree.write(str(arxml), xml_declaration=True, encoding="UTF-8")
        return arxml

    @staticmethod
    def _abort(tmp_path: Path, arxml: Path) -> LossyComponentImportError:
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(arxml), str(project / "model"))
        assert files_of(project / "model") == before
        return caught.value

    def test_an_enumeration_init_given_as_a_number_aborts_with_constr_1221(self, tmp_path: Path) -> None:
        # A number no scale names: one a scale names is read and carried
        # (`test_enum_number_init_import.py`).
        arxml = self._edited(
            tmp_path, lambda root: _as_numerical(_value_of_constant(root, init_constant_name("ISignals", "mode")), "7")
        )

        error = self._abort(tmp_path, arxml)

        (line,) = _init_lines(error.skip_lines)
        assert line.startswith("INIT-VALUE /PortInterfaces/ISignals/mode (R4.2.2 [constr_1221]): ")
        assert "the init value is not carried" in line
        assert "enumeration" in line
        assert self._DEFECTS in str(error)
        assert line in str(error)

    def test_an_application_primitives_own_value_in_the_numerical_form_aborts_with_constr_1221(
        self, tmp_path: Path
    ) -> None:
        arxml = self._edited(
            tmp_path,
            lambda root: _as_numerical(_value_of_constant(root, init_constant_name("ISignals", "own_unit")), "2.5"),
        )

        error = self._abort(tmp_path, arxml)

        (line,) = _init_lines(error.skip_lines)
        assert line.startswith("INIT-VALUE /PortInterfaces/ISignals/own_unit (R4.2.2 [constr_1221]): ")
        assert "a number in the numerical form where its application primitive type takes an application value" in line
        assert self._DEFECTS in str(error)

    def test_a_port_value_that_is_not_carried_is_a_line_with_its_category_and_rule(self, tmp_path: Path) -> None:
        """A port's own value not carried is a line under *Not carried*, its rule cited; the component imports."""
        arxml = self._edited(tmp_path, lambda root: _as_numerical(_value_in_place(root, "pSignals", "own_unit"), "7.5"))
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        (line,) = _init_lines(report.not_carried_lines)
        assert line.startswith("INIT-VALUE /ComponentTypes/SignalsSwc/pSignals/")
        assert "(R4.2.2 [constr_1221]): INIT-VALUE of a com-spec not carried: data element 'own_unit': " in line

    def test_an_enumeration_member_given_as_a_number_aborts_in_section_2(self, tmp_path: Path) -> None:
        """A member's enumeration as a number no scale names is SWCraft's own line (section 2, no rule).

        The whole init is not carried and the import aborts; a number a scale names is read
        (`test_enum_number_init_import.py`).
        """

        def edit(root: Any) -> None:
            deep = _value_of_constant(root, init_constant_name("ISignals", "deep"))
            (member,) = [
                node
                for node in deep.iter(f"{_AR}APPLICATION-VALUE-SPECIFICATION")
                if node.findtext(f"{_AR}SW-VALUE-CONT/{_AR}SW-VALUES-PHYS/{_AR}VT") == "OFF"
            ]
            _as_numerical(member, "9")

        error = self._abort(tmp_path, self._edited(tmp_path, edit))

        (line,) = _init_lines(error.skip_lines)
        assert line.startswith("INIT-VALUE /PortInterfaces/ISignals/deep: ")
        assert "[constr_" not in line
        assert "an enumeration member given as a number" in line
        assert self._DEFECTS not in str(error)
        assert "Not supported by SWCraft" in str(error)

    @pytest.mark.parametrize("form", ["numerical", "application"])
    def test_a_members_number_in_either_form_imports(self, tmp_path: Path, form: str) -> None:
        def edit(root: Any) -> None:
            limits = _value_of_constant(root, init_constant_name("ISignals", "limits"))
            members = list(limits.iter(f"{_AR}NUMERICAL-VALUE-SPECIFICATION"))
            assert len(members) == 4, "precondition: the writer gives a member's number the numerical form"
            if form == "application":
                for member in members:
                    _as_application(member)

        arxml = self._edited(tmp_path, edit)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert _init_lines(report.skip_lines) == []
        if form == "numerical":
            assert _init_lines(report.not_carried_lines) == []
        else:
            # A member in the application form here states no unit where its type states
            # one: the one descriptive line, at the constant's value; the value is carried.
            assert _init_lines(report.not_carried_lines) == [
                f"VALUE-SPEC /PortInterfaces/{init_constant_name('ISignals', 'limits')}: {VALUE_UNIT_SENTENCE}"
            ]
        imported = {i.name: i for i in read_interfaces(str(project / "model"))}["ISignals"]
        limits = next(de for de in imported.data_elements if de.name == "limits")
        assert limits.init == {"low": -1e20, "high": 1e20, "tiny": 1e-7, "whole": 100.0}
