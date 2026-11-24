"""A component's ``handle_termination_and_restart``, end to end.

AUTOSAR's ``SwcInternalBehavior.handleTerminationAndRestart`` has multiplicity 1 in R4.2.2
(``SWCT:22932``) and R4.3.0 (``SWCT:25469``) and is ``0..1`` in R22-11 beside ``[constr_1934]``. A
tool export may carry it (``NO-SUPPORT``, for instance); it is an optional model field, and this
module holds each half of that:

* **the declaration** -- the contract's field, its three lower-snake literals, ``"default": null``
  with no ``omitAtDefault``, and the stub rule that names it;
* **the YAML** -- absent stays absent, a written ``no_support`` is kept, a fourth value is refused;
* **the reader** -- each of the schema's three XML literals is read with no skip entry, an absent
  element is ``None`` with none, and an unknown literal is one entry and ``None``;
* **the two importers** -- ``import model`` maps each literal and the merge compares it; ``import
  component`` does not abort on it, maps it, and carries the target's value over an export that
  states none, with a report line;
* **the writer** -- one element with the XML literal, after ``EVENTS`` and before ``RUNNABLES``, in
  the whole-model export and in ``export component``; never a synthesized ``NO-SUPPORT``. A component
  that declares none is refused (``rule:swc.undeclared-termination-and-restart``), so every document
  here is generated without the fixture's ``SilentSwc``, and ``SilentSwc``'s own export is the refusal;
* **byte identity** -- every recorded tree declares ``no_support`` on each component; a tree recorded
  as generated, after the fidelity reversals and **minus** that line per component, equals
  ``test_external_stubs_byte_identity.py``'s digest;
* **the stub** -- ``rule:swc.external-with-ports`` reports the field on a stub and the writer's twin
  refuses it.

**Not reached:** the XSD run (licensed schemas); the real exports; whether an RTE generator accepts a
model that omits the value -- no oracle here can see an omission the schemas admit.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml
from behaviour_losses_support import BEHAVIOUR, MINIMAL_SWC, NS
from conftest import (
    HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT,
    REPO_ROOT,
    handle_termination_and_restart_declared_copy,
)
from external_stubs_support import change, component_stub_ports_model_root, rules
from fidelity_strip_support import strip_fidelity
from lxml import etree
from model_contract_fixtures import load_contract
from project_support import files_of, init_project
from pydantic import ValidationError
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.pipeline import run_import
from swcraft_tools.arxml_reader import ImportModel, SwcInfo, read_arxml
from swcraft_tools.arxml_version import HANDLE_TERMINATION_AND_RESTART_LITERALS, ArxmlVersion
from swcraft_tools.component_exchange import ClosureValidationError, export_component, import_component
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.yaml_documents import swc_to_dict
from termination_strip_support import TERMINATION_ADDED, strip_added_termination
from test_external_stubs_byte_identity import WITHOUT_A_STUB

FIELD = "handle_termination_and_restart"
FIELD_ID = f"field:swc.swc.{FIELD}"
TAG = "HANDLE-TERMINATION-AND-RESTART"

#: The committed writer fixture that sets the field -- the one tree that does.
FIXTURE_MODEL_ROOT = HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT

#: The fixture's components, by the value each declares (``None``: declares none).
FIXTURE_COMPONENTS: dict[str, str | None] = {
    "NoSupportSwc": "no_support",
    "TerminatedSwc": "can_be_terminated",
    "RestartedSwc": "can_be_terminated_and_restarted",
    "SilentSwc": None,
}

#: The components that declare a value: every one the writer accepts.
DECLARED_COMPONENTS: dict[str, str] = {name: value for name, value in FIXTURE_COMPONENTS.items() if value}

VALUES = sorted(HANDLE_TERMINATION_AND_RESTART_LITERALS)


# Helpers


def _fixture_copy(tmp_path: Path) -> Path:
    root = tmp_path / "source_model"
    shutil.copytree(FIXTURE_MODEL_ROOT, root)
    return root


def _set_value(model_root: Path, component: str, value: str | None) -> None:
    """Rewrite *component*'s file with *value* (``None`` removes the key)."""
    (path,) = list(model_root.glob(f"swc/*/{component}.yaml"))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.pop(FIELD, None)
    if value is not None:
        document[FIELD] = value
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _with_literal(text: str, literal: str | None) -> str:
    """*text* with every behaviour's ``HANDLE-TERMINATION-AND-RESTART`` replaced by *literal*.

    The element is inserted where the schema sequence puts it -- after ``EVENTS``, before
    ``RUNNABLES`` or ``SUPPORTS-MULTIPLE-INSTANTIATION`` -- or removed when *literal* is ``None``.
    """
    root = etree.fromstring(text.encode("utf-8"))
    for behaviour in root.iter(f"{NS}SWC-INTERNAL-BEHAVIOR"):
        for existing in behaviour.findall(f"{NS}{TAG}"):
            behaviour.remove(existing)
        if literal is None:
            continue
        anchor = behaviour.find(f"{NS}RUNNABLES")
        if anchor is None:
            anchor = behaviour.find(f"{NS}SUPPORTS-MULTIPLE-INSTANTIATION")
        assert anchor is not None, "the injection needs a RUNNABLES or SUPPORTS-MULTIPLE-INSTANTIATION to precede"
        element = etree.Element(f"{NS}{TAG}")
        element.text = literal
        element.tail = anchor.tail
        anchor.addprevious(element)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def _write_arxml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _behaviours(arxml: Path) -> dict[str, list[str]]:
    """Each behaviour's child tags in order, keyed by the behaviour's own ``SHORT-NAME``."""
    root = etree.parse(str(arxml)).getroot()
    return {
        behaviour.findtext(f"{NS}SHORT-NAME"): [etree.QName(child).localname for child in behaviour]
        for behaviour in root.iter(f"{NS}SWC-INTERNAL-BEHAVIOR")
    }


def _emitted(arxml: Path) -> dict[str, list[str]]:
    """Each behaviour's ``HANDLE-TERMINATION-AND-RESTART`` texts, keyed by its ``SHORT-NAME``."""
    root = etree.parse(str(arxml)).getroot()
    return {
        behaviour.findtext(f"{NS}SHORT-NAME"): [node.text for node in behaviour.findall(f"{NS}{TAG}")]
        for behaviour in root.iter(f"{NS}SWC-INTERNAL-BEHAVIOR")
    }


def _swc_file(model_root: Path, name: str) -> Path:
    (path,) = list(model_root.glob(f"swc/**/{name}.yaml"))
    return path


# The declaration


class TestTheContractDeclaresTheField:
    @staticmethod
    def _swc_fields() -> list[dict]:
        (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
        (kind,) = [kind for kind in layer["kinds"] if kind["id"] == "kind:swc.swc"]
        return kind["fields"]

    def test_the_minor_is_the_shipped_version(self) -> None:
        # The contract version this module was checked against; any bump moves it.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_field_is_an_optional_nullable_enum_after_asil(self) -> None:
        fields = self._swc_fields()
        names = [field["name"] for field in fields]
        assert names[names.index("asil") + 1] == FIELD
        (field,) = [field for field in fields if field["id"] == FIELD_ID]
        assert (field["type"], field["required"], field["default"], field["immutable"]) == (
            "string",
            False,
            None,
            False,
        )
        assert "omitAtDefault" not in field
        assert field["enum"] == ["no_support", "can_be_terminated", "can_be_terminated_and_restarted"]
        assert "reference" not in field and "relation" not in field

    def test_the_enum_is_the_literal_table_the_reader_and_writer_share(self) -> None:
        (field,) = [field for field in self._swc_fields() if field["id"] == FIELD_ID]
        assert list(HANDLE_TERMINATION_AND_RESTART_LITERALS) == field["enum"]
        assert list(HANDLE_TERMINATION_AND_RESTART_LITERALS.values()) == [
            "NO-SUPPORT",
            "CAN-BE-TERMINATED",
            "CAN-BE-TERMINATED-AND-RESTARTED",
        ]

    def test_the_stub_rule_names_the_field(self) -> None:
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:swc.external-with-ports"]
        assert FIELD_ID in rule["appliesTo"]


# The YAML


class TestTheYaml:
    def test_an_absent_field_loads_as_none_and_is_written_back_absent(self) -> None:
        swc = SWComponent.model_validate({"name": "C"})
        assert swc.handle_termination_and_restart is None
        assert FIELD not in swc_to_dict(swc)

    @pytest.mark.parametrize("value", VALUES)
    def test_a_declared_value_is_kept_and_written(self, value: str) -> None:
        swc = SWComponent.model_validate({"name": "C", "asil": "B", FIELD: value, "data_type_mappings": ["Set"]})
        assert swc.handle_termination_and_restart == value
        written = swc_to_dict(swc)
        assert written[FIELD] == value
        keys = list(written)
        assert keys[keys.index("asil") + 1] == FIELD
        assert keys[keys.index(FIELD) + 1] == "data_type_mappings"

    @pytest.mark.parametrize("value", ["NO-SUPPORT", "noSupport", "restart", ""])
    def test_a_value_outside_the_three_is_refused_by_the_loader(self, value: str) -> None:
        with pytest.raises(ValidationError):
            SWComponent.model_validate({"name": "C", FIELD: value})

    def test_the_fixture_reads_as_declared(self) -> None:
        loaded = {swc.name: swc.handle_termination_and_restart for swc in read_swc_model(str(FIXTURE_MODEL_ROOT))}
        assert loaded == FIXTURE_COMPONENTS


# The reader


class TestTheReader:
    @pytest.mark.parametrize("literal", list(HANDLE_TERMINATION_AND_RESTART_LITERALS.values()))
    def test_each_literal_is_read_with_no_skip_entry(self, literal: str, tmp_path: Path) -> None:
        source = _write_arxml(tmp_path / "in.arxml", _with_literal(MINIMAL_SWC.read_text(encoding="utf-8"), literal))
        model = read_arxml(str(source))

        (swc,) = model.swcs
        assert swc.handle_termination_and_restart == literal
        assert [entry for entry in model.skip_log if entry.tag == TAG] == []

    def test_an_absent_element_reads_as_none_with_no_entry(self, tmp_path: Path) -> None:
        source = _write_arxml(tmp_path / "in.arxml", _with_literal(MINIMAL_SWC.read_text(encoding="utf-8"), None))
        model = read_arxml(str(source))

        (swc,) = model.swcs
        assert swc.handle_termination_and_restart is None
        assert [entry for entry in model.skip_log if entry.tag == TAG] == []

    def test_an_unknown_literal_is_one_entry_naming_the_tag_and_the_value(self, tmp_path: Path) -> None:
        source = _write_arxml(
            tmp_path / "in.arxml", _with_literal(MINIMAL_SWC.read_text(encoding="utf-8"), "SOMETIMES")
        )
        model = read_arxml(str(source))

        (swc,) = model.swcs
        assert swc.handle_termination_and_restart is None
        entries = [entry for entry in model.skip_log if entry.tag == TAG]
        assert len(entries) == 1
        assert entries[0].path == BEHAVIOUR
        assert "'SOMETIMES'" in entries[0].reason


# import model, and the merge's comparison


class TestImportModel:
    @pytest.mark.parametrize("value", VALUES)
    def test_each_literal_lands_in_the_components_yaml(self, value: str, tmp_path: Path) -> None:
        literal = HANDLE_TERMINATION_AND_RESTART_LITERALS[value]
        source = _write_arxml(tmp_path / "in.arxml", _with_literal(MINIMAL_SWC.read_text(encoding="utf-8"), literal))
        project = init_project(tmp_path / "proj")

        run_import(str(source), str(project / "swcraft.yaml"), force=True)

        written = yaml.safe_load(_swc_file(project / "model", "WheelSpeedSensorSwc").read_text(encoding="utf-8"))
        assert written[FIELD] == value

    def test_an_absent_element_writes_no_key(self, tmp_path: Path) -> None:
        source = _write_arxml(tmp_path / "in.arxml", _with_literal(MINIMAL_SWC.read_text(encoding="utf-8"), None))
        project = init_project(tmp_path / "proj")

        run_import(str(source), str(project / "swcraft.yaml"), force=True)

        written = yaml.safe_load(_swc_file(project / "model", "WheelSpeedSensorSwc").read_text(encoding="utf-8"))
        assert FIELD not in written


class TestTheDuplicateDeclarationComparison:
    """Two declarations of one component differing only in the value are two components."""

    KEY = "swc/APPLICATION-SW-COMPONENT-TYPE/Comp"

    @staticmethod
    def _pair(left: str | None, right: str | None) -> ImportModel:
        model = ImportModel(source_path="inline")
        for tree, value in (("Left", left), ("Right", right)):
            model.swcs.append(
                SwcInfo(
                    path=f"/Acme/{tree}/Components/Comp",
                    name="Comp",
                    description="",
                    ports=[],
                    runnables=[],
                    events=[],
                    handle_termination_and_restart=value,
                )
            )
        return model

    @staticmethod
    def _group(decisions, key: str):
        (group,) = [group for group in decisions.groups if str(group.key) == key]
        return group

    @pytest.mark.parametrize("literal", ["NO-SUPPORT", "CAN-BE-TERMINATED", None])
    def test_equal_values_merge(self, literal: str | None) -> None:
        group = self._group(decide_merges(self._pair(literal, literal)), self.KEY)
        assert group.verdict == MERGED

    @pytest.mark.parametrize(
        ("left", "right"),
        [("NO-SUPPORT", "CAN-BE-TERMINATED"), ("NO-SUPPORT", None), (None, "CAN-BE-TERMINATED-AND-RESTARTED")],
    )
    def test_differing_values_conflict_on_the_element(self, left: str | None, right: str | None) -> None:
        group = self._group(decide_merges(self._pair(left, right)), self.KEY)
        assert (group.verdict, group.difference) == (CONFLICTING, TAG)


# import component


def _export(model_root: Path, component: str, tmp_path: Path, name: str = "export.arxml") -> Path:
    arxml = tmp_path / name
    export_component(str(model_root), component, str(arxml))
    return arxml


class TestImportComponentOfAForeignExport:
    """The reference tools' shape: every behaviour carries the element. No abort."""

    @pytest.mark.parametrize("value", VALUES)
    def test_a_foreign_literal_imports_and_sets_the_value(self, value: str, tmp_path: Path) -> None:
        source_root = _fixture_copy(tmp_path)
        # Declared so the writer accepts it; the literal is replaced below, as a foreign tool would state it.
        _set_value(source_root, "SilentSwc", "no_support")
        exported = _export(source_root, "SilentSwc", tmp_path)
        foreign = _write_arxml(
            tmp_path / "foreign.arxml",
            _with_literal(exported.read_text(encoding="utf-8"), HANDLE_TERMINATION_AND_RESTART_LITERALS[value]),
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert report.swc_action == "created"
        written = yaml.safe_load(_swc_file(project / "model", "SilentSwc").read_text(encoding="utf-8"))
        assert written[FIELD] == value
        assert TAG not in "\n".join(report.skip_lines)


class TestImportComponentCarriesAnAbsentValue:
    """An absent source element does not clear the target's value."""

    LINE = (
        "HANDLE-TERMINATION-AND-RESTART preserved from target (no_support): source element absent; "
        "absence does not request clearing this field."
    )

    @staticmethod
    def _target(tmp_path: Path, value: str | None) -> Path:
        """A project holding the fixture's ``SilentSwc`` (and its closure) with *value*."""
        source_root = _fixture_copy(tmp_path)
        _set_value(source_root, "SilentSwc", "no_support")
        seed = _export(source_root, "SilentSwc", tmp_path, "seed.arxml")
        project = init_project(tmp_path / "proj")
        import_component(str(seed), str(project / "model"))
        _set_value(project / "model", "SilentSwc", value)
        return project

    @staticmethod
    def _incoming(tmp_path: Path, value: str | None, *, period: float | None = None) -> Path:
        """An export of ``SilentSwc`` stating *value*, optionally with another timing period.

        The writer refuses a component that states none, so an export stating none is SWCraft's export
        of a declaring copy with the element removed -- the shape a foreign tool that omits it writes.
        """
        source_root = tmp_path / "incoming_model"
        shutil.copytree(FIXTURE_MODEL_ROOT, source_root)
        _set_value(source_root, "SilentSwc", value or "no_support")
        if period is not None:
            path = _swc_file(source_root, "SilentSwc")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["runnables"][0]["events"][0]["period"] = period
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        exported = _export(source_root, "SilentSwc", tmp_path, "incoming.arxml")
        if value is None:
            _write_arxml(exported, _with_literal(exported.read_text(encoding="utf-8"), None))
        return exported

    def test_an_identical_export_stating_none_is_unchanged_and_says_the_value_was_kept(self, tmp_path: Path) -> None:
        project = self._target(tmp_path, "no_support")
        before = _swc_file(project / "model", "SilentSwc").read_bytes()

        report = import_component(str(self._incoming(tmp_path, None)), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert self.LINE in report.render()
        assert _swc_file(project / "model", "SilentSwc").read_bytes() == before

    def test_an_update_from_an_export_stating_none_carries_the_value(self, tmp_path: Path) -> None:
        project = self._target(tmp_path, "no_support")

        report = import_component(str(self._incoming(tmp_path, None, period=0.05)), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_handle_termination_and_restart == "no_support"
        assert self.LINE in report.render()
        written = yaml.safe_load(_swc_file(project / "model", "SilentSwc").read_text(encoding="utf-8"))
        assert written[FIELD] == "no_support"
        assert written["runnables"][0]["events"][0]["period"] == 0.05

    def test_an_export_stating_a_value_replaces_the_targets(self, tmp_path: Path) -> None:
        project = self._target(tmp_path, "no_support")

        report = import_component(
            str(self._incoming(tmp_path, "can_be_terminated")), str(project / "model"), update=True
        )

        assert report.swc_action == "updated"
        assert report.preserved_handle_termination_and_restart is None
        assert "preserved from target" not in report.render()
        written = yaml.safe_load(_swc_file(project / "model", "SilentSwc").read_text(encoding="utf-8"))
        assert written[FIELD] == "can_be_terminated"

    def test_a_stated_value_differing_from_the_targets_is_a_conflict_without_update(self, tmp_path: Path) -> None:
        from swcraft_tools.component_exchange import ComponentImportConflictError

        project = self._target(tmp_path, "no_support")
        incoming = self._incoming(tmp_path, "can_be_terminated")
        before = files_of(project)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(incoming), str(project / "model"))

        assert "SWC 'SilentSwc' already exists in the target project and diverges" in str(caught.value)
        # The conflict note names the one field an absent element does not compare.
        assert "a termination and restart handling it does not state" in str(caught.value)
        assert files_of(project) == before

    def test_both_conflict_notes_name_the_field(self) -> None:
        """The clause is in the note of every access regime, not only in the one the export above selects."""
        from swcraft_tools.component_exchange.capabilities import _COMPARED_ACCESS_NOTE, _UNREPRESENTABLE_NOTE

        for note in (_UNREPRESENTABLE_NOTE, _COMPARED_ACCESS_NOTE):
            assert "a termination and restart handling it does not state" in note

    def test_an_export_stating_none_over_an_unset_target_stays_unset_with_no_line(self, tmp_path: Path) -> None:
        project = self._target(tmp_path, None)

        report = import_component(str(self._incoming(tmp_path, None, period=0.05)), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_handle_termination_and_restart is None
        assert "HANDLE-TERMINATION-AND-RESTART" not in report.render()
        written = yaml.safe_load(_swc_file(project / "model", "SilentSwc").read_text(encoding="utf-8"))
        assert FIELD not in written


# The writer


def _assert_positioned(children: list[str]) -> None:
    """Assert the element sits where the writer places it.

    It follows what the writer emits before it and precedes ``RUNNABLES`` or ``SUPPORTS-MULTIPLE-INSTANTIATION``.
    """
    index = children.index(TAG)
    before = [tag for tag in ("SHORT-NAME", "ADMIN-DATA", "DATA-TYPE-MAPPING-REFS", "EVENTS") if tag in children]
    after = [tag for tag in ("RUNNABLES", "SUPPORTS-MULTIPLE-INSTANTIATION") if tag in children]
    assert "PORT-API-OPTIONS" not in children, children
    assert children[index - 1] == before[-1], children
    assert children[index + 1] == after[0], children


class TestTheWriter:
    """The declared values, emitted; a component that declares none is refused before anything is written."""

    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=str)
    def test_the_whole_model_export_emits_each_value_once(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = handle_termination_and_restart_declared_copy(tmp_path / "model")
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output), version=version)

        assert _emitted(output) == {
            f"{name}_InternalBehavior": [HANDLE_TERMINATION_AND_RESTART_LITERALS[value]]
            for name, value in DECLARED_COMPONENTS.items()
        }
        for name in DECLARED_COMPONENTS:
            _assert_positioned(_behaviours(output)[f"{name}_InternalBehavior"])

    def test_a_component_without_events_places_it_after_the_short_name(self, tmp_path: Path) -> None:
        model_root = handle_termination_and_restart_declared_copy(tmp_path / "model")
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))

        children = _behaviours(output)["RestartedSwc_InternalBehavior"]
        assert "EVENTS" not in children
        assert children == ["SHORT-NAME", TAG, "SUPPORTS-MULTIPLE-INSTANTIATION"]

    @pytest.mark.parametrize("name", sorted(DECLARED_COMPONENTS))
    def test_export_component_emits_the_declared_value(self, name: str, tmp_path: Path) -> None:
        arxml = _export(FIXTURE_MODEL_ROOT, name, tmp_path)

        assert _emitted(arxml) == {
            f"{name}_InternalBehavior": [HANDLE_TERMINATION_AND_RESTART_LITERALS[DECLARED_COMPONENTS[name]]]
        }
        _assert_positioned(_behaviours(arxml)[f"{name}_InternalBehavior"])

    def test_export_component_refuses_the_component_that_declares_none(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "out"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(FIXTURE_MODEL_ROOT), "SilentSwc", str(output_dir / "export.arxml"))

        assert "SWC 'SilentSwc' sets no handle_termination_and_restart" in str(caught.value)
        assert not output_dir.exists()


# Byte identity: the recorded bytes, minus the line each component gained


@pytest.mark.parametrize("tree", sorted(set(WITHOUT_A_STUB) & set(TERMINATION_ADDED)))
def test_every_recorded_tree_declares_the_field_and_strips_to_its_record(tree: str, tmp_path: Path) -> None:
    """Each recorded tree declares a value on every emitted component, and strips to its recorded digest.

    ``no_support`` sits on exactly the components ``TERMINATION_ADDED`` names, and the 4.2.2 document
    minus one ``NO-SUPPORT`` line each is the recorded digest. (The 4.3.0 document and the refusing
    ``recorded_oracles/impact_model`` are that suite's own cases.)
    """
    model_root = REPO_ROOT / tree
    swcs = [swc for swc in read_swc_model(str(model_root)) if not swc.external]
    assert all(swc.handle_termination_and_restart is not None for swc in swcs)
    assert {swc.name for swc in swcs if swc.handle_termination_and_restart == "no_support"} == TERMINATION_ADDED[tree]

    recorded = WITHOUT_A_STUB[tree][0]
    if recorded.startswith("refused: "):
        return
    output = tmp_path / "model.arxml"
    generate_arxml(str(model_root), str(output), version="4.2.2")
    # The writer-fidelity changes are mapped back first, as ``strip_recorded`` chains them.
    stripped = strip_added_termination(strip_fidelity(output.read_bytes()), TERMINATION_ADDED[tree])
    assert hashlib.sha256(stripped).hexdigest() == recorded


# The round trip


class TestTheRoundTrip:
    """Over the declaring components: ``SilentSwc`` is refused by the writer."""

    @pytest.mark.parametrize("name", sorted(DECLARED_COMPONENTS))
    def test_export_component_then_import_component_gives_the_same_yaml(self, name: str, tmp_path: Path) -> None:
        arxml = _export(FIXTURE_MODEL_ROOT, name, tmp_path)
        project = init_project(tmp_path / "proj")

        import_component(str(arxml), str(project / "model"))

        (source,) = [swc for swc in read_swc_model(str(FIXTURE_MODEL_ROOT)) if swc.name == name]
        expected = yaml.safe_dump(swc_to_dict(source), default_flow_style=False, sort_keys=False)
        assert _swc_file(project / "model", name).read_text(encoding="utf-8") == expected

    def test_the_whole_model_export_then_import_model_gives_the_same_yaml(self, tmp_path: Path) -> None:
        model_root = handle_termination_and_restart_declared_copy(tmp_path / "model")
        arxml = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(arxml))
        project = init_project(tmp_path / "proj")

        run_import(str(arxml), str(project / "swcraft.yaml"), force=True)

        for source in read_swc_model(str(model_root)):
            expected = yaml.safe_dump(swc_to_dict(source), default_flow_style=False, sort_keys=False)
            assert _swc_file(project / "model", source.name).read_text(encoding="utf-8") == expected, source.name


# The stub

STUB = "swc/external/ExternalStub.yaml"


class TestAStubMayNotDeclareIt:
    def test_check_model_reports_it_and_the_writer_refuses_it(self, tmp_path: Path) -> None:
        model_root = component_stub_ports_model_root(tmp_path / "model")
        assert rules(model_root) == []

        change(model_root, STUB, lambda document: document.update({FIELD: "no_support"}))

        from swcraft_tools.validator import check_cross_layer_consistency

        records = [
            record
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule == "swc.external-with-ports"
        ]
        assert len(records) == 1
        assert f"declares 'external: true' and also {FIELD}" in records[0].message

        output_dir = tmp_path / "fresh-output"
        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model_root), str(output_dir / "model.arxml"))
        assert f"external stub 'ExternalStub' declares {FIELD}" in str(caught.value)
        assert not output_dir.exists()

    def test_a_package_on_a_stub_is_reported_and_refused_like_the_field(self, tmp_path: Path) -> None:
        """A stub emits no component type, so a ``package`` there would be dropped: reported and refused."""
        model_root = component_stub_ports_model_root(tmp_path / "model")
        change(model_root, STUB, lambda document: document.update({"package": "Stubs"}))

        from swcraft_tools.validator import check_cross_layer_consistency

        (record,) = [
            record
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule == "swc.external-with-ports"
        ]
        assert "declares 'external: true' and also package" in record.message
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == "rule:swc.external-with-ports"]
        assert "field:swc.swc.package" in rule["appliesTo"]

        output_dir = tmp_path / "fresh-output"
        with pytest.raises(ValueError, match="external stub 'ExternalStub' declares package"):
            generate_arxml(str(model_root), str(output_dir / "model.arxml"))
        assert not output_dir.exists()


# The editing protocol's write of the field


def _approve(proposal):
    from swcraft_tools.editing.authorization import ApprovalDecision

    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


class TestTheEditingProtocol:
    """A change set sets the field like any optional enum, and pass D refuses it on a stub."""

    STUB_DOCUMENT = {
        "name": "StubSwc",
        "type": "application",
        "external": True,
        "external_ref": "/Foreign/StubSwc",
        "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
    }

    @classmethod
    def _project(cls, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
        stub = project / "model" / "swc" / "external" / "StubSwc.yaml"
        stub.parent.mkdir(parents=True)
        # Linked (the stub's own `satisfies` member), so the baseline carries no finding and pass D's
        # refusal is the field's alone.
        document = {**cls.STUB_DOCUMENT, "satisfies": ["REQ_EXAMPLE_001@1"]}
        stub.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        return project

    @staticmethod
    def _set(project: Path, element: str, value: object) -> dict:
        from swcraft_tools.editing.engine import handle_request

        token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
        return handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "set the termination handling",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": element, "path": [{"field": FIELD}]},
                        "value": value,
                    }
                ],
            },
            project=str(project),
            review=_approve,
        )

    def test_a_declared_component_takes_the_value(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        response = self._set(project, "ExampleSWC", "no_support")

        assert response["response"] != "error", response
        written = yaml.safe_load(_swc_file(project / "model", "ExampleSWC").read_text(encoding="utf-8"))
        assert written[FIELD] == "no_support"

    def test_a_fourth_value_is_refused_with_nothing_written(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        before = files_of(project / "model")

        response = self._set(project, "ExampleSWC", "NO-SUPPORT")

        assert response["response"] == "error", response
        assert response["error"] == "schema-violation", response
        assert files_of(project / "model") == before

    def test_setting_it_on_a_stub_is_a_consistency_violation_with_nothing_written(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        before = files_of(project / "model")

        response = self._set(project, "StubSwc", "no_support")

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        assert f"declares 'external: true' and also {FIELD}" in response["message"]
        assert files_of(project / "model") == before
