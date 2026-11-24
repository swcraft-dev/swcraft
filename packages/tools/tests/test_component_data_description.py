"""Component exchange carries the units, compu methods and data constraints a component's types name.

The closure follows the reference fields -- an application type's ``unit_ref``,
``compu_method_ref`` and ``data_constr_ref``, an enumeration's, a value type's and a reference type's
``compu_method_ref``, and a ``linear``'s own ``unit`` -- into the three layers, and the file carries
each element once, in its layer's package. ``import component`` reads them back as ``import model``
does and compares a shared element as it stands: a divergent one is a conflict. SWCraft's own private
forms read back inline; an earlier release's layout, private forms directly under ``/DataTypes``,
imports them as elements.

What it does **not** reach: the editing protocol's side
(``packages/mcp/tests/test_component_exchange_tools.py``); the elements' XML
(``test_data_description_writer.py``).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from swcraft_tools.capability_markers import DATA_TYPES
from swcraft_tools.component_exchange import (
    AmbiguousReferenceError,
    ComponentImportConflictError,
    MissingReferenceError,
    compute_closure,
    export_component,
    import_component,
    plan_component_import,
)
from swcraft_tools.data_description import read_data_description
from swcraft_tools.validator import data_type_layer
from swcraft_tools.writer.arxml_xml import find_element

TESTS = Path(__file__).resolve().parent
PROBE_MODEL = TESTS / "fixtures" / "data_description" / "model"
INLINE_MODEL = TESTS / "fixtures" / "component_exchange" / "model"
SENDER = "ProbeSender"

#: Every element ``ProbeSender``'s closure names, ``(layer, name)`` -> its path in the file.
CLOSURE_ELEMENTS: dict[tuple[str, str], str] = {
    ("units", "ProbeUnitSpeed"): "/DataTypes/Units/ProbeUnitSpeed",
    ("units", "ProbeUnitPlaced"): "/DataTypes/Units/ProbeUnitPackage/ProbeUnitPlaced",
    ("units", "ProbeUnitBare"): "/DataTypes/Units/ProbeUnitBare",
    ("compu_methods", "ProbeLinear"): "/DataTypes/CompuMethods/ProbeLinear",
    ("compu_methods", "ProbeTable"): "/DataTypes/CompuMethods/ProbeTable",
    ("data_constraints", "ProbeLimits"): "/DataTypes/Constraints/ProbeLimits",
}

#: Per data element of ``IProbeSignals``, kept alone on the interface: the elements the closure
#: then names. Each reference field and each kind is reached by at least one case.
ELEMENT_CASES: dict[str, set[tuple[str, str]]] = {
    # an application type's `unit_ref`, beside an inline conversion and range
    "mixed": {("units", "ProbeUnitPlaced")},
    # an application type naming a unit, a linear (which names the same unit) and a data constraint
    "speed": {("units", "ProbeUnitSpeed"), ("compu_methods", "ProbeLinear"), ("data_constraints", "ProbeLimits")},
    # an enumeration's texttable, which its mapped implementation value type names too
    "gear": {("compu_methods", "ProbeTable")},
    # a reference type's texttable, and its value type's
    "raw": {("compu_methods", "ProbeTable")},
    # an inline unit alone: the declared element of its private form's name is not named
    "length": set(),
}

#: One differing copy per layer of an element the closure carries: (layer, name, changed fields).
DIFFERING: dict[str, tuple[str, str, dict[str, Any]]] = {
    "unit": ("units", "ProbeUnitSpeed", {"display_name": "mph"}),
    "texttable": (
        "compu_methods",
        "ProbeTable",
        {"entries": [{"name": "ProbeOff", "value": 0}, {"name": "ProbeOn", "value": 2}]},
    ),
    "linear": ("compu_methods", "ProbeLinear", {"factor": 0.25}),
    "data_constr": ("data_constraints", "ProbeLimits", {"max": 300}),
}

WHAT = {"units": "unit", "compu_methods": "compu method", "data_constraints": "data constraint"}


def _copy(source: Path, tmp_path: Path, name: str) -> Path:
    model_root = tmp_path / name / "model"
    shutil.copytree(source, model_root)
    return model_root


def _with_one_data_element(model_root: Path, element: str) -> None:
    """``IProbeSignals`` with one data element left: the closure then reaches only what that one names."""
    path = model_element_path(model_root, "IProbeSignals", layer="interfaces")
    interface = yaml.safe_load(path.read_text(encoding="utf-8"))
    interface["data_elements"] = [entry for entry in interface["data_elements"] if entry["name"] == element]
    path.write_text(yaml.safe_dump(interface, sort_keys=False), encoding="utf-8", newline="\n")


def _rewrite(model_root: Path, layer: str, name: str, changes: dict[str, Any]) -> Path:
    path = model_element_path(model_root, name, layer=layer)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def _export(model_root: Path, out: Path, swc: str = SENDER, version: str = "4.2.2") -> Path:
    export_component(str(model_root), swc, str(out), version=version)
    return out


def _init_project(tmp_path: Path, name: str = "empty") -> Path:
    """An empty project's model root: a directory holding no element."""
    model_root = tmp_path / name / "model"
    model_root.mkdir(parents=True)
    return model_root


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _elements(model_root: Path) -> dict[tuple[str, str], Any]:
    description = read_data_description(str(model_root))
    return {(data_type_layer(element), element.name): element for element in description.elements}


def _types(model_root: Path) -> dict[tuple[str, str], Any]:
    return {(data_type_layer(dt), dt.name): dt for dt in read_data_description(str(model_root)).data_types}


# The closure


class TestTheClosureFollowsTheReferenceFields:
    @pytest.mark.parametrize("element", sorted(ELEMENT_CASES))
    def test_each_reference_field_brings_its_element_once(self, tmp_path: Path, element: str) -> None:
        model_root = _copy(PROBE_MODEL, tmp_path, "source")
        _with_one_data_element(model_root, element)

        closure = compute_closure(str(model_root), SENDER)

        keys = [(data_type_layer(found), found.name) for found in closure.elements]
        assert len(keys) == len(set(keys))
        assert set(keys) == ELEMENT_CASES[element]

    def test_the_whole_component_names_each_element_once_and_no_other(self) -> None:
        closure = compute_closure(str(PROBE_MODEL), SENDER)

        keys = [(data_type_layer(found), found.name) for found in closure.elements]
        assert len(keys) == len(set(keys))
        # `ProbeLength_Unit`, `ProbeHalfOpen` and `ProbeNoLimit` are declared and named by no type of the closure.
        assert set(keys) == set(CLOSURE_ELEMENTS)
        assert keys == sorted(keys, key=lambda key: (key[1], key[0]))

    def test_an_undeclared_element_is_a_missing_reference(self, tmp_path: Path) -> None:
        model_root = _copy(PROBE_MODEL, tmp_path, "source")
        model_element_path(model_root, "ProbeUnitSpeed", layer="units").unlink()

        with pytest.raises(MissingReferenceError, match="ProbeUnitSpeed") as missing:
            compute_closure(str(model_root), SENDER)

        assert missing.value.missing == frozenset({"ProbeUnitSpeed"})

    def test_an_element_declared_in_two_files_is_ambiguous(self, tmp_path: Path) -> None:
        model_root = _copy(PROBE_MODEL, tmp_path, "source")
        twin = model_element_path(model_root, "ProbeUnitSpeedTwin", layer="units")
        twin.write_text("name: ProbeUnitSpeed\ndisplay_name: km/h\n", encoding="utf-8", newline="\n")

        with pytest.raises(AmbiguousReferenceError, match="ProbeUnitSpeed"):
            compute_closure(str(model_root), SENDER)


# The export


class TestTheExportCarriesTheElements:
    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_each_element_is_written_once_in_its_layer_package(self, tmp_path: Path, version: str) -> None:
        out = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml", version=version)
        text = out.read_text(encoding="utf-8")
        root = etree.parse(str(out)).getroot()

        for (_layer, name), path in CLOSURE_ELEMENTS.items():
            assert find_element(root, path) is not None, path
            assert text.count(f"<SHORT-NAME>{name}</SHORT-NAME>") == 1, name
            assert f">{path}</" in text, path

    def test_an_element_no_closure_type_names_stays_out(self, tmp_path: Path) -> None:
        out = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        text = out.read_text(encoding="utf-8")
        root = etree.parse(str(out)).getroot()

        assert "ProbeHalfOpen" not in text
        # The element `ProbeLength_Unit` is not exported; the private form of that name is,
        # beside its type, where the type's inline unit generates it.
        assert find_element(root, "/DataTypes/Units/ProbeLength_Unit") is None
        assert find_element(root, "/DataTypes/Application/ProbeLength_Unit") is not None

    def test_the_data_types_marker_keeps_its_meaning(self, tmp_path: Path) -> None:
        text = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml").read_text(encoding="utf-8")

        assert f'<SD GID="{DATA_TYPES.sd_gid}">' in text

    def test_the_export_report_names_the_closure_it_wrote(self, tmp_path: Path) -> None:
        report = export_component(str(PROBE_MODEL), SENDER, str(tmp_path / "ProbeSender.arxml"))

        assert report.ok
        assert "IProbeSignals" in report.interfaces


# The import


class TestTheImportPlansTheThreeLayers:
    def test_into_an_empty_project_each_element_is_created_through_the_registry(self, tmp_path: Path) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)

        report = import_component(str(arxml), str(target))

        source_elements = _elements(PROBE_MODEL)
        imported = _elements(target)
        assert set(imported) == set(CLOSURE_ELEMENTS)
        for key in CLOSURE_ELEMENTS:
            assert imported[key] == source_elements[key], key
            assert model_element_path(target, key[1], layer=key[0]).is_file()
        assert report.created_elements == {
            "units": ["ProbeUnitBare", "ProbeUnitPlaced", "ProbeUnitSpeed"],
            "compu_methods": ["ProbeLinear", "ProbeTable"],
            "data_constraints": ["ProbeLimits"],
        }
        assert report.reused_elements == {}

    def test_a_unit_and_a_data_constraint_are_written_without_a_kind(self, tmp_path: Path) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)

        import_component(str(arxml), str(target))

        unit = yaml.safe_load(model_element_path(target, "ProbeUnitSpeed", layer="units").read_text(encoding="utf-8"))
        constraint = yaml.safe_load(
            model_element_path(target, "ProbeLimits", layer="data_constraints").read_text(encoding="utf-8")
        )
        table = yaml.safe_load(
            model_element_path(target, "ProbeTable", layer="compu_methods").read_text(encoding="utf-8")
        )
        assert "kind" not in unit and "kind" not in constraint
        assert table["kind"] == "texttable"
        assert constraint["level"] == 0

    def test_every_type_reads_back_as_the_source_states_it(self, tmp_path: Path) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)

        import_component(str(arxml), str(target))

        source, imported = _types(PROBE_MODEL), _types(target)
        # The key set first: a closure type missing from the import fails here.
        closure = compute_closure(str(PROBE_MODEL), SENDER)
        assert set(imported) == {(data_type_layer(dt), dt.name) for dt in closure.data_types}
        for key in imported:
            assert imported[key] == source[key], key

    def test_the_rendered_report_names_layer_and_element_and_no_path(self, tmp_path: Path) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)

        rendered = import_component(str(arxml), str(target), dry_run=True).render()

        assert "CREATED 3 unit(s)\n  - ProbeUnitBare\n  - ProbeUnitPlaced\n  - ProbeUnitSpeed" in rendered
        assert "CREATED 2 compu method(s)\n  - ProbeLinear\n  - ProbeTable" in rendered
        assert "CREATED 1 data constraint(s)\n  - ProbeLimits" in rendered
        assert str(target) not in rendered
        assert ".yaml" not in rendered

    def test_the_reader_lines_the_import_carries_are_not_in_its_report(self, tmp_path: Path) -> None:
        """An implementation type's ``COMPU-METHOD-REF`` and a rule's ``CONSTR-LEVEL`` are carried, so not reported."""
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)

        report = plan_component_import(str(arxml), str(target)).report

        lines = [*report.not_carried_lines, *report.skip_lines]
        assert not [line for line in lines if line.startswith(("COMPU-METHOD-REF ", "CONSTR-LEVEL "))]

    def test_into_a_project_holding_equal_elements_nothing_is_rewritten(self, tmp_path: Path) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _init_project(tmp_path)
        for layer, name in CLOSURE_ELEMENTS:
            source = model_element_path(PROBE_MODEL, name, layer=layer)
            copied = model_element_path(target, name, layer=layer)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copied)
        before = {key: model_element_path(target, key[1], layer=key[0]).read_bytes() for key in CLOSURE_ELEMENTS}

        plan = plan_component_import(str(arxml), str(target))
        import_component(str(arxml), str(target))

        assert plan.report.created_elements == {}
        assert plan.report.reused_elements == {
            "units": ["ProbeUnitBare", "ProbeUnitPlaced", "ProbeUnitSpeed"],
            "compu_methods": ["ProbeLinear", "ProbeTable"],
            "data_constraints": ["ProbeLimits"],
        }
        assert not [target for target in plan.targets if target.layer in WHAT]
        assert {
            key: model_element_path(target, key[1], layer=key[0]).read_bytes() for key in CLOSURE_ELEMENTS
        } == before

    def test_a_project_imports_its_own_export_unchanged(self, tmp_path: Path) -> None:
        model_root = _copy(PROBE_MODEL, tmp_path, "own")
        arxml = _export(model_root, tmp_path / "ProbeSender.arxml")
        before = _tree(model_root)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert _tree(model_root) == before

    @pytest.mark.parametrize("case", sorted(DIFFERING))
    def test_a_differing_element_is_a_conflict_and_nothing_is_written(self, tmp_path: Path, case: str) -> None:
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _copy(PROBE_MODEL, tmp_path, "target")
        layer, name, changes = DIFFERING[case]
        _rewrite(target, layer, name, changes)
        before = _tree(target)

        with pytest.raises(ComponentImportConflictError) as refused:
            import_component(str(arxml), str(target))

        message = str(refused.value)
        assert f"shared {WHAT[layer]} '{name}' diverges from the target project's existing definition" in message
        assert str(target) not in message and str(tmp_path) not in message
        assert ".yaml" not in message
        assert _tree(target) == before

    def test_a_differing_description_alone_is_reused_and_listed(self, tmp_path: Path) -> None:
        """A shared element is reused, never replaced: a differing description is named, not taken."""
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _copy(PROBE_MODEL, tmp_path, "target")
        _rewrite(target, "compu_methods", "ProbeTable", {"description": "Another text."})
        before = _tree(target)

        report = import_component(str(arxml), str(target))

        assert "ProbeTable" in report.reused_elements["compu_methods"]
        assert any(line.startswith("DESC of ProbeTable not taken") for line in report.descriptions_not_taken_lines)
        assert _tree(target) == before


# SWCraft's own private forms read back inline


def _inline_model(tmp_path: Path, name: str) -> Path:
    """The component exchange tree, whose types state every property inline, with a bounded value type too."""
    model_root = _copy(INLINE_MODEL, tmp_path, name)
    _rewrite(model_root, "implementation_types", "Speed_T", {"value_min": 0, "value_max": 1000})
    return model_root


class TestInlineFormsReadBackInline:
    def test_a_project_stating_every_form_inline_imports_its_own_export_unchanged(self, tmp_path: Path) -> None:
        model_root = _inline_model(tmp_path, "own")
        arxml = _export(model_root, tmp_path / "SensorSwc.arxml", swc="SensorSwc")
        before = _tree(model_root)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.created_elements == {} and report.reused_elements == {}
        assert _tree(model_root) == before

    def test_a_range_stating_no_limit_reads_back_and_the_own_export_is_unchanged(self, tmp_path: Path) -> None:
        """``range: {}`` round-trips through an empty ``PHYS-CONSTRS`` in the type's own ``_Range``."""
        model_root = _inline_model(tmp_path, "own")
        _rewrite(model_root, "application_types", "Velocity_T", {"range": {}})
        arxml = _export(model_root, tmp_path / "SensorSwc.arxml", swc="SensorSwc")
        before = _tree(model_root)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert _tree(model_root) == before
        target = _init_project(tmp_path)
        import_component(str(arxml), str(target))
        velocity = _types(target)[("application_types", "Velocity_T")]
        assert velocity.range is not None and (velocity.range.min, velocity.range.max) == (None, None)
        assert _elements(target) == {}

    def test_into_an_empty_project_the_types_state_their_forms_inline(self, tmp_path: Path) -> None:
        source = _inline_model(tmp_path, "source")
        arxml = _export(source, tmp_path / "SensorSwc.arxml", swc="SensorSwc")
        target = _init_project(tmp_path)

        report = import_component(str(arxml), str(target))

        assert report.created_elements == {}
        assert _elements(target) == {}
        source_types, imported = _types(source), _types(target)
        for key in imported:
            assert imported[key] == source_types[key], key
        assert imported[("application_types", "Velocity_T")].unit == "km/h"
        assert imported[("application_types", "Gear_T")].literals

    def test_an_inline_target_and_an_element_in_the_file_of_equal_content_conflict(self, tmp_path: Path) -> None:
        """A stated limit: types compare as they stand, so an inline unit and a unit element are two types."""
        arxml = _export(PROBE_MODEL, tmp_path / "ProbeSender.arxml")
        target = _copy(PROBE_MODEL, tmp_path, "target")
        path = model_element_path(target, "ProbeMixed", layer="application_types")
        mixed = yaml.safe_load(path.read_text(encoding="utf-8"))
        mixed.pop("unit_ref")
        mixed["unit"] = "mm"  # ProbeUnitPlaced's display name
        path.write_text(yaml.safe_dump(mixed, sort_keys=False), encoding="utf-8", newline="\n")

        with pytest.raises(ComponentImportConflictError, match="shared data type 'ProbeMixed' diverges"):
            import_component(str(arxml), str(target))


# An export of an earlier release

#: The packages under ``/DataTypes`` this release writes; an earlier release wrote every data
#: type, its private forms and every mapping set directly into ``/DataTypes``.
LAYER_PACKAGES = ("Base", "Implementation", "Application", "Units", "CompuMethods", "Constraints", "MappingSets")


def _in_the_earlier_layout(text: str) -> str:
    """*text*, a component export of this release, in the layout an earlier release wrote.

    Every element of a layer package moves into ``/DataTypes`` and every reference follows it. It
    reaches no ``package`` sub-package, which neither fixture states.
    """
    root = etree.fromstring(text.encode("utf-8"))
    namespace = root.nsmap[None]

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    data_types = next(p for p in root.iter(tag("AR-PACKAGE")) if p.findtext(tag("SHORT-NAME")) == "DataTypes")
    layers = data_types.find(tag("AR-PACKAGES"))
    elements = data_types.find(tag("ELEMENTS"))
    if elements is None:
        elements = etree.Element(tag("ELEMENTS"))
        layers.addprevious(elements)
    for layer in list(layers):
        for element in list(layer.find(tag("ELEMENTS"))):
            elements.append(element)
    data_types.remove(layers)
    written = etree.tostring(root, encoding="unicode", xml_declaration=False)
    return re.sub(rf"/DataTypes/(?:{'|'.join(LAYER_PACKAGES)})/", "/DataTypes/", written)


class TestAnEarlierReleasesExport:
    def test_its_private_forms_arrive_as_elements(self, tmp_path: Path) -> None:
        source = _inline_model(tmp_path, "source")
        current = _export(source, tmp_path / "SensorSwc.arxml", swc="SensorSwc")
        earlier = tmp_path / "SensorSwcEarlier.arxml"
        earlier.write_text(_in_the_earlier_layout(current.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        target = _init_project(tmp_path)

        report = import_component(str(earlier), str(target))

        assert report.created_elements == {
            "units": ["Velocity_T_Unit"],
            "compu_methods": ["Gear_T_CompuMethod", "Velocity_T_Compu"],
            "data_constraints": ["Velocity_T_Range"],
        }
        types = _types(target)
        # A value type's bounds are internal limits, which no element holds: its `_Range` reads
        # back into `value_min` / `value_max` wherever it lies.
        speed = types[("implementation_types", "Speed_T")]
        assert (speed.value_min, speed.value_max) == (0, 1000)
        velocity = types[("application_types", "Velocity_T")]
        assert (velocity.unit, velocity.conversion, velocity.range) == ("", None, None)
        assert (velocity.unit_ref, velocity.compu_method_ref, velocity.data_constr_ref) == (
            "Velocity_T_Unit",
            "Velocity_T_Compu",
            "Velocity_T_Range",
        )
        gear = types[("application_types", "Gear_T")]
        assert (gear.literals, gear.compu_method_ref) == (None, "Gear_T_CompuMethod")
        assert _elements(target)[("compu_methods", "Velocity_T_Compu")].unit == "Velocity_T_Unit"
