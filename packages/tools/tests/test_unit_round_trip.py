"""An application type's ``unit`` round-trips exactly, surrounding whitespace included.

The writer writes ``unit`` as the model holds it (``_emit_unit``); the reader takes the
``DISPLAY-NAME`` the same way, or the value is changed on the way in with no line. A stripped read
would break four paths, each pinned here at both versions:

* **import model** -- ``"  km/h  "`` imported as ``km/h`` and regenerated so;
* **import component of the project's own export** -- the shared type
  *diverged* from itself and the import was refused;
* **import component, a real difference** -- a target ``km/h`` against an export
  holding ``" km/h"`` was *reused* with no line;
* **the merge row** -- two same-named application types differing only in the
  unit's padding merged silently.

A ``DISPLAY-NAME`` of blanks alone is still no unit: that is the one
``strip()`` decision, the writer's own (``merge_verdict.carried_unit_text``).

What this does **not** reach: the compatibility rule compares two types' units
stripped (``validator._application_types_compatible``), a recorded limit; and a
foreign export that pretty-prints a ``DISPLAY-NAME`` onto its own line now
imports with that whitespace, because ``SINGLE-LANGUAGE-UNIT-NAMES`` is mixed
content the schema does not collapse -- the text is the source's.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from behaviour_losses_support import snippet
from conftest import model_element_path
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_import.merge_verdict import carried_unit_text, decide_merges
from swcraft_tools.arxml_reader import UnitInfo, read_arxml
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import ComponentImportConflictError, export_component, import_component
from swcraft_tools.writer.arxml_generation import generate_arxml

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")

#: Outer padding of three kinds, and one with the padding inside left alone.
PADDED_UNITS = [
    pytest.param("  km/h  ", id="spaces-both-sides"),
    pytest.param("km/h\n", id="trailing-newline"),
    pytest.param("\tdeg C", id="leading-tab"),
    pytest.param("m  /  s", id="inner-run"),
]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _model(root: Path, unit: str) -> Path:
    """The ``component_exchange`` tree with ``Velocity_T``'s unit set to *unit*."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    path = model_element_path(root, "Velocity_T", kind="application_type")
    path.write_text(yaml.safe_dump({**_load(path), "unit": unit}, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _display_names(arxml: Path) -> list[str | None]:
    return [node.findtext(f"{NS}DISPLAY-NAME") for node in etree.parse(str(arxml)).getroot().iter(f"{NS}UNIT")]


class TestTheReaderTakesADisplayNameAsWritten:
    def test_the_text_is_the_display_name_exactly(self) -> None:
        assert carried_unit_text(UnitInfo(path="/u", name="U", display_name="  km/h  ", description="")) == "  km/h  "

    def test_no_display_name_is_the_short_name(self) -> None:
        assert (
            carried_unit_text(UnitInfo(path="/u", name="KmPerHour", display_name=None, description="")) == "KmPerHour"
        )

    @pytest.mark.parametrize("blank", ["", " ", "\n\t  "])
    def test_a_display_name_of_blanks_is_no_unit(self, blank: str) -> None:
        assert carried_unit_text(UnitInfo(path="/u", name="U", display_name=blank, description="")) == ""


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("unit", PADDED_UNITS)
class TestAPaddedUnitRoundTrips:
    def test_through_generate_and_import_model(self, tmp_path: Path, version: str, unit: str) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", unit)), str(source), version=version)
        assert _display_names(source) == [unit]

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["unit"] == unit
        # Nothing was changed, so nothing is said about the unit.
        assert [entry for entry in report.skip_entries if "Velocity_T" in entry.path] == []
        # ... and the model the import wrote is written back as the same text.
        again = tmp_path / "again.arxml"
        generate_arxml(str(project / "model"), str(again), version=version)
        assert _display_names(again) == [unit]

    def test_a_projects_own_component_export_is_reused(self, tmp_path: Path, version: str, unit: str) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = _model(project / "model", unit)
        before = files_of(model_root)
        out = tmp_path / "SensorSwc.arxml"
        export_component(str(model_root), "SensorSwc", str(out), version=version)
        assert unit in _display_names(out), "the fixture's component must reach the application type"

        report = import_component(str(out), str(model_root), update=True)

        assert "Velocity_T" in report.reused_data_types
        assert files_of(model_root) == before


@pytest.mark.parametrize("version", VERSIONS)
class TestAUnitDifferingOnlyInPaddingIsADifference:
    def test_import_component_refuses_it(self, tmp_path: Path, version: str) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = _model(project / "model", "km/h")
        before = files_of(model_root)
        out = tmp_path / "SensorSwc.arxml"
        export_component(str(_model(tmp_path / "source", " km/h")), "SensorSwc", str(out), version=version)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(out), str(model_root), update=True)

        assert "Velocity_T" in str(caught.value)
        assert files_of(model_root) == before

    @pytest.mark.parametrize(
        ("twin_unit", "verdict"),
        [("km/h", MERGED), (" km/h", CONFLICTING), ("km/h\n", CONFLICTING)],
    )
    def test_the_merge_row(self, tmp_path: Path, version: str, twin_unit: str, verdict: str) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", "km/h")), str(source), version=version)
        tree = etree.parse(str(source))
        root = tree.getroot()

        def copy_of(tag: str, name: str) -> etree._Element:
            (node,) = [n for n in root.iter(f"{NS}{tag}") if n.findtext(f"{NS}SHORT-NAME") == name]
            return etree.fromstring(etree.tostring(node))

        velocity = copy_of("APPLICATION-PRIMITIVE-DATA-TYPE", "Velocity_T")
        unit = copy_of("UNIT", "Velocity_T_Unit")
        unit.find(f"{NS}DISPLAY-NAME").text = twin_unit
        next(velocity.iter(f"{NS}UNIT-REF")).text = "/Other/Velocity_T_Unit"
        package = snippet("<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS/></AR-PACKAGE>")[0]
        package.find(f"{NS}ELEMENTS").extend([velocity, unit])
        root.find(f"{NS}AR-PACKAGES").append(package)
        twin = tmp_path / "twin.arxml"
        twin.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))

        (group,) = [g for g in decide_merges(read_arxml(str(twin))).groups if g.key.name == "Velocity_T"]

        assert group.verdict == verdict
