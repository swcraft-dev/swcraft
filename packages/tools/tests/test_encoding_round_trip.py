"""A base type's ``encoding`` round-trips exactly, surrounding whitespace included.

The writer writes ``encoding`` as the model holds it (``_emit_sw_base_type``), and both
licensed schemas declare ``BASE-TYPE-ENCODING`` a plain string, not a collapsing token,
so the reader takes it as written. Pinned at both versions, against three defects of a
stripping reader: ``import model`` changed ``"  IEEE754  "``; ``import component`` of the
project's own export saw the base type diverge from itself; and a target ``IEEE754``
against an export's ``" IEEE754"`` was reused with no line.

**The merge row is the one place that strips**, since there the question is whether two
declarations are the same token. The merged base type keeps the primary's spelling
exactly, and a member spelled otherwise is named in the merged group's block.
``NATIVE-DECLARATION`` is named on the same terms.

What this does **not** reach: a foreign export that pretty-prints the element now imports
with that whitespace; a ``BASE-TYPE-ENCODING`` of blanks alone is still no encoding and
the base type is skipped; and the interface-compatibility rule compares encodings as held,
so a padded one and its plain twin are two encodings there.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.report import _merged_lines
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import ComponentImportConflictError, export_component, import_component
from swcraft_tools.validator import check_xml_representable_text
from swcraft_tools.writer.arxml_generation import generate_arxml

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")
BASE = "float32_base"

PADDED_ENCODINGS = [
    pytest.param("  IEEE754  ", id="spaces-both-sides"),
    pytest.param("IEEE754\n", id="trailing-newline"),
    pytest.param("\tIEEE754", id="leading-tab"),
]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _model(root: Path, text: str, field: str = "encoding") -> Path:
    """The ``component_exchange`` tree with ``float32_base``'s *field* (its encoding) set to *text*."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    path = model_element_path(root, BASE, kind="base_type")
    path.write_text(yaml.safe_dump({**_load(path), field: text}, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _base_nodes(root: etree._Element) -> list[etree._Element]:
    return [node for node in root.iter(f"{NS}SW-BASE-TYPE") if node.findtext(f"{NS}SHORT-NAME") == BASE]


def _encodings(arxml: Path) -> list[str | None]:
    return [node.findtext(f"{NS}BASE-TYPE-ENCODING") for node in _base_nodes(etree.parse(str(arxml)).getroot())]


def _with_a_twin(
    tmp_path: Path, version: str, primary: str, twin: str, *, field: str = "encoding", tag: str = "BASE-TYPE-ENCODING"
) -> Path:
    """SWCraft's own export, ``float32_base`` declared a second time in ``/Other`` with the *field* text *twin*."""
    source = tmp_path / "model.arxml"
    generate_arxml(str(_model(tmp_path / "source", primary, field)), str(source), version=version)
    tree = etree.parse(str(source))
    root = tree.getroot()
    (node,) = _base_nodes(root)
    copy = etree.fromstring(etree.tostring(node))
    copy.find(f"{NS}{tag}").text = twin
    package = etree.SubElement(root.find(f"{NS}AR-PACKAGES"), f"{NS}AR-PACKAGE")
    etree.SubElement(package, f"{NS}SHORT-NAME").text = "Other"
    etree.SubElement(package, f"{NS}ELEMENTS").append(copy)
    out = tmp_path / "twin.arxml"
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("encoding", PADDED_ENCODINGS)
class TestAPaddedEncodingRoundTrips:
    def test_through_generate_and_import_model(self, tmp_path: Path, version: str, encoding: str) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", encoding)), str(source), version=version)
        assert _encodings(source) == [encoding]

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", BASE, kind="base_type"))["encoding"] == encoding
        # Nothing was changed, so nothing is said about the base type.
        assert [entry for entry in report.skip_entries if BASE in entry.path] == []
        assert "encoding spelling not carried" not in report.render(verbose=True)
        # ... and the model the import wrote is written back as the same text.
        again = tmp_path / "again.arxml"
        generate_arxml(str(project / "model"), str(again), version=version)
        assert _encodings(again) == [encoding]

    def test_a_projects_own_component_export_is_reused(self, tmp_path: Path, version: str, encoding: str) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = _model(project / "model", encoding)
        before = files_of(model_root)
        out = tmp_path / "SensorSwc.arxml"
        export_component(str(model_root), "SensorSwc", str(out), version=version)
        assert _encodings(out) == [encoding], "the fixture's component must reach the base type"

        report = import_component(str(out), str(model_root), update=True)

        assert BASE in report.reused_data_types
        assert files_of(model_root) == before

    def test_the_text_rule_and_the_writer_judge_the_text_as_held(
        self, tmp_path: Path, version: str, encoding: str
    ) -> None:
        """Padding is whitespace XML carries: no finding, no refusal, and the exact text in the file."""
        model_root = _model(tmp_path / "source", encoding)
        assert check_xml_representable_text(str(model_root)).issues == []
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out), version=version)
        assert _encodings(out) == [encoding]


@pytest.mark.parametrize("version", VERSIONS)
class TestAnEncodingDifferingOnlyInPaddingIsADifferenceToImportComponent:
    def test_import_component_refuses_it(self, tmp_path: Path, version: str) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = _model(project / "model", "IEEE754")
        before = files_of(model_root)
        out = tmp_path / "SensorSwc.arxml"
        export_component(str(_model(tmp_path / "source", " IEEE754")), "SensorSwc", str(out), version=version)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(out), str(model_root), update=True)

        assert BASE in str(caught.value)
        assert files_of(model_root) == before


@pytest.mark.parametrize("version", VERSIONS)
class TestTheMergeRowStripsAndTheMergedGroupNamesTheSpellingItDidNotKeep:
    @pytest.mark.parametrize(
        ("primary", "twin"),
        [
            ("IEEE754", "\n                IEEE754\n              "),
            ("IEEE754", " IEEE754"),
            ("  IEEE754  ", "IEEE754"),
        ],
        ids=["pretty-printed-twin", "padded-twin", "padded-primary"],
    )
    def test_a_padding_only_difference_merges_keeps_the_primarys_and_is_named(
        self, tmp_path: Path, version: str, primary: str, twin: str
    ) -> None:
        source = _with_a_twin(tmp_path, version, primary, twin)

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert group.verdict == MERGED
        assert group.paths == (f"/DataTypes/Base/{BASE}", f"/Other/{BASE}")
        assert group.dropped_encodings == ((f"/Other/{BASE}", twin),)
        assert _merged_lines(group)[1:] == [f"      encoding spelling not carried, from /Other/{BASE}: {twin!r}"]

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        # The kept spelling is the primary's, exactly, and the report says what was not kept.
        assert _load(model_element_path(project / "model", BASE, kind="base_type"))["encoding"] == primary
        rendered = report.render(verbose=True)
        assert rendered.count(f"encoding spelling not carried, from /Other/{BASE}: {twin!r}") == 1

    def test_an_identical_twin_drops_nothing_and_says_nothing(self, tmp_path: Path, version: str) -> None:
        source = _with_a_twin(tmp_path, version, "IEEE754", "IEEE754")

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert (group.verdict, group.dropped_encodings) == (MERGED, ())
        assert len(_merged_lines(group)) == 1

    def test_another_token_is_still_a_conflict(self, tmp_path: Path, version: str) -> None:
        source = _with_a_twin(tmp_path, version, "IEEE754", "NONE")

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert (group.verdict, group.difference) == (CONFLICTING, "BASE-TYPE-ENCODING")
        assert group.dropped_encodings == ()


@pytest.mark.parametrize("version", VERSIONS)
class TestTheNativeDeclarationGetsTheEncodingsRule:
    """``NATIVE-DECLARATION`` is the row's other stripped string: same merge, same kept spelling, same line."""

    def _twin(self, tmp_path: Path, version: str, primary: str, twin: str) -> Path:
        return _with_a_twin(tmp_path, version, primary, twin, field="native_declaration", tag="NATIVE-DECLARATION")

    @pytest.mark.parametrize(
        ("primary", "twin"),
        [
            ("float", "\n                float\n              "),
            ("float", " float"),
            ("  float  ", "float"),
        ],
        ids=["pretty-printed-twin", "padded-twin", "padded-primary"],
    )
    def test_a_padding_only_difference_merges_keeps_the_primarys_and_is_named(
        self, tmp_path: Path, version: str, primary: str, twin: str
    ) -> None:
        source = self._twin(tmp_path, version, primary, twin)

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert group.verdict == MERGED
        assert group.dropped_native_declarations == ((f"/Other/{BASE}", twin),)
        assert group.dropped_encodings == ()
        line = f"native declaration spelling not carried, from /Other/{BASE}: {twin!r}"
        assert _merged_lines(group)[1:] == [f"      {line}"]

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", BASE, kind="base_type"))["native_declaration"] == primary
        assert report.render(verbose=True).count(line) == 1

    def test_both_spellings_differing_are_two_lines_the_encodings_first(self, tmp_path: Path, version: str) -> None:
        source = self._twin(tmp_path, version, "float", " float")
        tree = etree.parse(str(source))
        _base_nodes(tree.getroot())[1].find(f"{NS}BASE-TYPE-ENCODING").text = "IEEE754 "
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert _merged_lines(group)[1:] == [
            f"      encoding spelling not carried, from /Other/{BASE}: 'IEEE754 '",
            f"      native declaration spelling not carried, from /Other/{BASE}: ' float'",
        ]

    def test_an_identical_twin_drops_nothing_and_says_nothing(self, tmp_path: Path, version: str) -> None:
        source = self._twin(tmp_path, version, "float", "float")

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert (group.verdict, group.dropped_native_declarations) == (MERGED, ())
        assert len(_merged_lines(group)) == 1

    def test_another_name_is_still_a_conflict(self, tmp_path: Path, version: str) -> None:
        source = self._twin(tmp_path, version, "float", "double")

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == BASE]

        assert (group.verdict, group.difference) == (CONFLICTING, "NATIVE-DECLARATION")
        assert group.dropped_native_declarations == ()


class TestTheReaderTakesTheEncodingAsWritten:
    @pytest.mark.parametrize("text", ["IEEE754", "  IEEE754  ", "\n    IEEE754\n  "])
    def test_the_text_is_the_elements_exactly(self, tmp_path: Path, text: str) -> None:
        source = _with_a_twin(tmp_path, "4.2.2", "NONE", text)

        by_path = {info.path: info.encoding for info in read_arxml(str(source)).sw_base_types if info.name == BASE}

        assert by_path == {f"/DataTypes/Base/{BASE}": "NONE", f"/Other/{BASE}": text}
