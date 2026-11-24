"""The exact-repeat rules follow the packages.

The writer emits each data description layer into a package of its own
(``/DataTypes/Base``, ``Implementation``, ``Application``, ``MappingSets``), so an exact
repeat of a ``SHORT-NAME`` is a collision only inside one package:

* **the four generated-form collisions are scoped to the form's package**:
  ``rule:data-type.compu-method-name-collision``, ``…unit-name-collision``,
  ``…compu-name-collision`` and ``…range-name-collision`` report a generated form whose
  name a declared type of the same package holds. A private form lies in its owner's
  package, so a type of another package under its name is legal, and every site agrees:
  the validator, the ARXML writer, the ``create_*`` mutations (and the protocol's write
  path), ``import model``'s phase B and ``import component``;
* **a mapping set and a data type are not siblings**: a set may take any type's
  name or any name a type generates, accepted at every site (validator, writer,
  ``create_*`` guards, ``import model``, ``import component``).

What this does **not** reach: a case-only pair, which is
``rule:name.name-space-collision``'s, judged per package (``test_name_space_collisions.py``);
the package is read from the element's layer
(``validator.data_description_package_path``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.reconcile import reserved_data_type_names
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange.name_policies import reserved_name_violations
from swcraft_tools.contract import load_contract
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    EnumDataType,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.mutations import ReservedNameError, create_data_type, create_mapping_set

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
RETIRED_RULE = "mapping-set.data-type-name-collision"
FORM_RULES = (
    "data-type.compu-method-name-collision",
    "data-type.unit-name-collision",
    "data-type.compu-name-collision",
    "data-type.range-name-collision",
)

#: ``(form, the rule it is reported under, the owner's package)`` for the four forms the
#: ``component_exchange`` tree generates: the enum ``Gear_T``'s computation method, and
#: ``Velocity_T``'s unit, computation method and constraint, all in ``/DataTypes/Application``.
APPLICATION_FORMS = (
    ("Gear_T_CompuMethod", "data-type.compu-method-name-collision"),
    ("Velocity_T_Unit", "data-type.unit-name-collision"),
    ("Velocity_T_Compu", "data-type.compu-name-collision"),
    ("Velocity_T_Range", "data-type.range-name-collision"),
)


#: ``(form, owner, kind, fields)``: an owner absent from the ``component_exchange`` tree
#: that generates *form* in ``/DataTypes/Application`` when created with *fields*, for the
#: direction in which the form's name is already held.
REVERSE_FORMS = (
    ("ProbeMode_CompuMethod", "ProbeMode", "enum", {"literals": [{"name": "OFF", "value": 0}]}),
    ("ProbePhys_Unit", "ProbePhys", "application_type", {"unit": "km/h"}),
    ("ProbePhys_Compu", "ProbePhys", "application_type", {"conversion": {"factor": 0.1, "offset": 0.0}}),
    ("ProbePhys_Range", "ProbePhys", "application_type", {"range": {"min": 0.0, "max": 1.0}}),
)


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _model(tmp_path: Path, *types: dict[str, Any], sets: tuple[dict[str, Any], ...] = ()) -> Path:
    """The ``component_exchange`` tree with *types* added to ``data_types/`` and *sets* to ``mapping_sets/``."""
    model_root = tmp_path / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    for document in types:
        _dump(model_element_path(model_root, document["name"], kind=document["kind"]), document)
    for document in sets:
        _dump(model_element_path(model_root, document["name"], layer="mapping_sets"), document)
    return model_root


def _value_type(name: str, **fields: Any) -> dict[str, Any]:
    """A value type: ``/DataTypes/Implementation``."""
    return {"name": name, "kind": "value_type", "base": "uint8_base", **fields}


def _application_type(name: str, **fields: Any) -> dict[str, Any]:
    """An application type: ``/DataTypes/Application``."""
    return {"name": name, "kind": "application_type", **fields}


def _base_type(name: str) -> dict[str, Any]:
    """A base type: ``/DataTypes/Base``."""
    return {"name": name, "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in check_cross_layer_consistency(str(model_root)).records]


def _paths(out: Path, tag: str, name: str) -> list[str]:
    """The package path of every *tag* element named *name* in *out*."""
    paths = []
    for node in etree.parse(str(out)).getroot().iter(f"{NS}{tag}"):
        if node.findtext(f"{NS}SHORT-NAME") != name:
            continue
        segments = [name]
        package = node.getparent().getparent()
        while package is not None and etree.QName(package).localname == "AR-PACKAGE":
            segments.insert(0, package.findtext(f"{NS}SHORT-NAME"))
            package = package.getparent().getparent()
        paths.append("/" + "/".join(segments))
    return paths


# The four form rules, scoped to the form's package


class TestAFormIsClaimedInItsOwnersPackageOnly:
    """A declared type of another package under a form's name is legal at every site."""

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_check_model_is_silent_for_a_value_type_under_an_application_form(
        self, tmp_path: Path, form: str, rule: str
    ) -> None:
        model_root = _model(tmp_path, _value_type(form))

        assert rule not in _rules(model_root)

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_check_model_still_reports_an_application_type_under_it(self, tmp_path: Path, form: str, rule: str) -> None:
        model_root = _model(tmp_path, _application_type(form))

        assert rule in _rules(model_root)

    def test_a_value_types_range_is_free_for_an_application_type(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _value_type("Ratio", value_max=1.0), _application_type("Ratio_Range"))

        assert "data-type.range-name-collision" not in _rules(model_root)
        same_package = _model(tmp_path / "same", _value_type("Ratio", value_max=1.0), _value_type("Ratio_Range"))
        assert "data-type.range-name-collision" in _rules(same_package)

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_a_base_type_under_an_application_form_is_legal(self, tmp_path: Path, form: str, rule: str) -> None:
        model_root = _model(tmp_path, _base_type(form))

        assert rule not in _rules(model_root)

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_the_writer_emits_both_in_their_two_packages(self, tmp_path: Path, form: str, rule: str) -> None:
        model_root = _model(tmp_path, _value_type(form))
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out))

        assert _paths(out, "IMPLEMENTATION-DATA-TYPE", form) == [f"/DataTypes/Implementation/{form}"]
        emitted = [path for tag in ("UNIT", "COMPU-METHOD", "DATA-CONSTR") for path in _paths(out, tag, form)]
        assert emitted == [f"/DataTypes/Application/{form}"]

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_the_writer_still_refuses_the_same_package(self, tmp_path: Path, form: str, rule: str) -> None:
        model_root = _model(tmp_path, _application_type(form))

        with pytest.raises(ValueError, match="collide with auto-generated"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert not (tmp_path / "out.arxml").exists()

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_the_write_path_accepts_a_type_of_another_package_under_a_form(
        self, tmp_path: Path, form: str, rule: str
    ) -> None:
        """The form is claimed first, the occupant created second."""
        model_root = _model(tmp_path)

        written = create_data_type(str(model_root), form, "value_type", base="uint8_base")

        assert Path(written) == model_root / "data_types" / "implementation" / f"{form}.yaml"

    @pytest.mark.parametrize(("form", "owner", "kind", "fields"), REVERSE_FORMS)
    def test_the_write_path_accepts_an_owner_whose_form_another_package_holds(
        self, tmp_path: Path, form: str, owner: str, kind: str, fields: dict[str, Any]
    ) -> None:
        """The other direction, once per form: the occupant exists, the owner is created second."""
        model_root = _model(tmp_path, _value_type(form))

        written = create_data_type(str(model_root), owner, kind, **fields)

        assert Path(written) == model_root / "data_types" / "application" / f"{owner}.yaml"

    @pytest.mark.parametrize(("form", "owner", "kind", "fields"), REVERSE_FORMS)
    def test_the_write_path_refuses_an_owner_whose_form_its_own_package_holds(
        self, tmp_path: Path, form: str, owner: str, kind: str, fields: dict[str, Any]
    ) -> None:
        """The control of the row above: the same direction, the occupant in the owner's package."""
        model_root = _model(tmp_path, _application_type(form))

        with pytest.raises(ReservedNameError, match="auto-generated") as excinfo:
            create_data_type(str(model_root), owner, kind, **fields)
        assert (excinfo.value.layer, excinfo.value.element) == ("application_types", owner)
        assert not (model_root / "data_types" / "application" / f"{owner}.yaml").exists()

    @pytest.mark.parametrize(("form", "rule"), APPLICATION_FORMS)
    def test_the_write_path_still_refuses_the_same_package(self, tmp_path: Path, form: str, rule: str) -> None:
        model_root = _model(tmp_path)

        with pytest.raises(ReservedNameError, match="auto-generated") as excinfo:
            create_data_type(str(model_root), form, "application_type")
        assert (excinfo.value.layer, excinfo.value.element) == ("application_types", form)

    def test_import_model_renames_only_inside_the_owners_package(self) -> None:
        owner = ApplicationDataType(name="Speed", kind="application_type", unit="m/s")
        for occupant in (
            ValueDataType(name="Speed_Unit", kind="value_type", base="u8"),
            BaseDataType(name="Speed_Unit", kind="base_type", size=8, encoding="NONE"),
        ):
            assert reserved_data_type_names({"Speed": owner, "Speed_Unit": occupant}) == {}
        same = ApplicationDataType(name="speed_unit", kind="application_type")
        assert reserved_data_type_names({"Speed": owner, "speed_unit": same}) == {
            "speed_unit": "collides with the UNIT auto-generated for 'Speed'"
        }
        enum = EnumDataType(name="Mode", kind="enum", literals=[{"name": "OFF", "value": 0}])
        assert (
            reserved_data_type_names(
                {"Mode": enum, "Mode_CompuMethod": ValueDataType(name="Mode_CompuMethod", kind="value_type", base="u8")}
            )
            == {}
        )

    def test_import_component_refuses_only_inside_the_owners_package(self) -> None:
        def violations(incoming: Any) -> list[str]:
            return reserved_name_violations(
                SWComponent(name="Unrelated"),
                [incoming],
                {},
                set(),
                {
                    ("application_types", "Speed"): ApplicationDataType(
                        name="Speed", kind="application_type", unit="m/s"
                    )
                },
                [],
                {},
            )

        assert violations(ValueDataType(name="Speed_Unit", kind="value_type", base="u8")) == []
        refused = violations(ApplicationDataType(name="Speed_Unit", kind="application_type"))
        assert len(refused) == 1 and "'Speed_Unit'" in refused[0], refused


# A mapping set and a data type are not siblings


#: A set named after a declared type of each type package, and after a form.
SET_NAMES = ("Speed_T", "Velocity_T", "uint8_base", "Velocity_T_Unit", "Gear_T_CompuMethod")


def _set(name: str) -> dict[str, Any]:
    return {"name": name, "maps": [{"application": "Distance_T", "implementation": "float32"}]}


class TestTheMappingSetCollisionIsRetired:
    def test_the_rule_is_declared_nowhere(self) -> None:
        assert f"rule:{RETIRED_RULE}" not in {rule["id"] for rule in load_contract()["rules"]}
        assert RETIRED_RULE not in CONSISTENCY_RULES

    @pytest.mark.parametrize("name", SET_NAMES)
    def test_check_model_accepts_a_set_of_a_types_name(self, tmp_path: Path, name: str) -> None:
        model_root = _model(tmp_path, sets=(_set(name),))

        assert _rules(model_root) == _rules(EXCHANGE_MODEL_ROOT)

    @pytest.mark.parametrize("name", SET_NAMES)
    def test_the_writer_emits_the_set_beside_the_type_in_another_package(self, tmp_path: Path, name: str) -> None:
        model_root = _model(tmp_path, sets=(_set(name),))
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out))

        assert _paths(out, "DATA-TYPE-MAPPING-SET", name) == [f"/DataTypes/MappingSets/{name}"]

    @pytest.mark.parametrize("name", SET_NAMES)
    def test_create_mapping_set_accepts_a_types_name(self, tmp_path: Path, name: str) -> None:
        model_root = _model(tmp_path)

        create_mapping_set(str(model_root), name, maps=[{"application": "Distance_T", "implementation": "float32"}])

        assert (model_element_path(model_root, name, layer="mapping_sets")).is_file()

    def test_create_data_type_accepts_a_sets_name_and_a_form_that_is_one(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, sets=(_set("Brake_T"), _set("Torque_T_Unit")))

        create_data_type(str(model_root), "Brake_T", "value_type", base="uint8_base")
        create_data_type(str(model_root), "Torque_T", "application_type", unit="Nm")

        assert _rules(model_root) == _rules(EXCHANGE_MODEL_ROOT)

    def test_import_model_keeps_the_sets_name(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path / "source", sets=(_set("Speed_T"),))
        source = tmp_path / "source.arxml"
        generate_arxml(str(model_root), str(source))
        project = init_project(tmp_path / "project")

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert (model_element_path(project / "model", "Speed_T", layer="mapping_sets")).is_file()
        assert not [line for line in report.rename_lines if "Speed_T" in line]


# The editing protocol's pass D


class TestPassDAcceptsWhatCheckModelAccepts:
    """The two newly legal shapes, staged through the whole engine on a validation-vector fixture."""

    @staticmethod
    def _dispatch(tmp_path: Path, *operations: dict[str, Any]) -> dict[str, Any]:
        import editing_vector_fixtures as fixtures
        from test_editing_validation import _dispatch

        project_root = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        return _dispatch(project_root, *operations)

    @staticmethod
    def _create(layer: str, value: dict[str, Any]) -> dict[str, Any]:
        return {"operation": "create", "address": {"layer": layer, "element": value["name"]}, "value": value}

    def test_a_value_type_under_an_enums_compu_method_is_staged(self, tmp_path: Path) -> None:
        response = self._dispatch(
            tmp_path,
            self._create(
                "application_types", {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]}
            ),
            self._create("implementation_types", _value_type("Mode_CompuMethod", base="float32_base")),
        )

        assert response.get("error") is None and response["status"] == "staged", response

    def test_the_same_package_is_still_refused(self, tmp_path: Path) -> None:
        response = self._dispatch(
            tmp_path,
            self._create(
                "application_types", {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]}
            ),
            self._create("application_types", _application_type("Mode_CompuMethod")),
        )

        assert response.get("error") == "consistency-violation", response

    def test_a_mapping_set_named_like_a_type_is_staged(self, tmp_path: Path) -> None:
        response = self._dispatch(
            tmp_path,
            self._create("implementation_types", _value_type("Probe_T", base="float32_base")),
            self._create("mapping_sets", {"name": "Probe_T", "maps": []}),
        )

        assert response.get("error") is None and response["status"] == "staged", response
