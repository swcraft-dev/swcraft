"""The importers carry a runnable's ``SYMBOL``.

The reader reads ``SYMBOL`` as its raw text (``SwcRunnableInfo.symbol``) and the
mapper decides, in this order:

* **not a C identifier as written** -- surrounding whitespace, an inner space, a
  line break, an empty text, a leading digit -- the symbol is dropped and one
  section-1 line cites the ``CIdentifier`` primitive's class table
  (:attr:`StandardRule.C_IDENTIFIER_FORM`). Nothing is stripped first, also when
  stripping would give the name or another legal identifier;
* **equal to the runnable's name** -- ``symbol`` stays unset: the writer writes
  the name, which is the same text;
* **a C identifier that differs** -- ``symbol`` is the source's text, and no line;
* **absent** -- the reader's existing line, and ``symbol`` unset.

Then the runnables of one component are grouped, compared exactly: first by the
``SYMBOL`` text each **states** -- every member of a group of two or more loses
its symbol and gets one section-1 line citing ``[constr_2025]``
(:attr:`StandardRule.SYMBOL_UNIQUE`) -- then by the symbol each would be
**written** with, where a collision only SWCraft's fallback to the name made is a
section-2 line naming the sibling written with its name. One line per runnable.

``import component`` carries a stated symbol and compares it as it stands, and
aborts on every line above.

What this module does **not** reach: the real tool exports (the closed suites, ``dev.py test-internal``)
and an ECU-wide uniqueness, which SWCraft does not judge.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from behaviour_losses_support import MINIMAL_SWC, NS, RUNNABLE, SWC_PATH, add_xml, snippet
from conftest import ACCESS_POINTS_MODEL_ROOT
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs, run_import
from swcraft_tools.arxml_reader import (
    SkipCategory,
    SkipEntry,
    StandardRule,
    SwcRunnableInfo,
    cite,
    read_arxml,
)
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    LossyComponentImportError,
    export_component,
    import_component,
)
from swcraft_tools.reader import read_swc_model
from swcraft_tools.validator import runnable_symbol_records
from swcraft_tools.writer import generate_arxml

BEHAVIOUR_PATH = RUNNABLE.rsplit("/", 1)[0]
NAME = "WheelSpeedSensorRun"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runnable_xml(name: str, symbol: str | None) -> str:
    symbol_xml = "" if symbol is None else f"<SYMBOL>{symbol}</SYMBOL>"
    return (
        f"<RUNNABLE-ENTITY><SHORT-NAME>{name}</SHORT-NAME>"
        "<MINIMUM-START-INTERVAL>0.01</MINIMUM-START-INTERVAL>"
        f"<CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>{symbol_xml}</RUNNABLE-ENTITY>"
    )


def _source(tmp_path: Path, symbol: str | None, *others: tuple[str, str | None]) -> Path:
    """``minimal_swc.arxml`` with its runnable's ``SYMBOL`` set to *symbol* (``None``: removed) and *others* added.

    *symbol* is written as the element's text verbatim, so whitespace and line
    breaks reach the reader as stated.
    """
    tree = etree.parse(str(MINIMAL_SWC))
    runnable = next(tree.getroot().iter(f"{NS}RUNNABLE-ENTITY"))
    element = runnable.find(f"{NS}SYMBOL")
    if symbol is None:
        runnable.remove(element)
    else:
        element.text = symbol
    add_xml(runnable.getparent(), "".join(_runnable_xml(name, text) for name, text in others))
    # The contract refuses a component without it, and the fixture carries none.
    behaviour = runnable.getparent().getparent()
    (termination,) = snippet("<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>")
    behaviour.find(f"{NS}PORT-API-OPTIONS").addprevious(termination)
    path = tmp_path / "source.arxml"
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _map(path: Path):
    """Read and map *path*: ``{runnable name: symbol}`` and every entry of the reader and the mapper."""
    model = read_arxml(str(path))
    forms: list = []
    data_types, registry, _type_log = map_data_types(model, elements=forms)
    interfaces, iface_log = map_interfaces(model, registry, data_types, elements=forms)
    swcs, swc_log = map_swcs(
        model, interfaces, iface_skip_log=iface_log, port_inits=registry.port_inits, variable_types={}
    )
    (swc,) = [imported.swc for imported in swcs]
    entries = [*model.skip_log, *swc_log]
    return swc, {runnable.name: runnable.symbol for runnable in swc.runnables}, entries


def _symbol_lines(entries: list[SkipEntry]) -> dict[str, SkipEntry]:
    """The ``SYMBOL`` lines by runnable name; a runnable with two is a failure."""
    lines: dict[str, SkipEntry] = {}
    for entry in entries:
        if entry.tag == "SYMBOL":
            name = entry.path.rsplit("/", 1)[-1]
            assert name not in lines, f"two SYMBOL lines for {name}: {lines[name].reason!r}, {entry.reason!r}"
            lines[name] = entry
    return lines


# ---------------------------------------------------------------------------
# The reader reads the raw text
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_the_raw_text_is_read_unstripped(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_source(tmp_path, " Rte_Speed\n"))).swcs
        (runnable,) = swc.runnables
        assert isinstance(runnable, SwcRunnableInfo)
        assert runnable.symbol == " Rte_Speed\n"

    def test_an_empty_element_is_the_empty_text_and_an_absent_one_none(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_source(tmp_path, ""))).swcs
        assert swc.runnables[0].symbol == ""
        (swc,) = read_arxml(str(_source(tmp_path, None))).swcs
        assert swc.runnables[0].symbol is None

    def test_a_differing_symbol_is_no_reader_line(self, tmp_path: Path) -> None:
        model = read_arxml(str(_source(tmp_path, "Rte_Speed")))
        assert [entry for entry in model.skip_log if entry.tag == "SYMBOL"] == []


# ---------------------------------------------------------------------------
# One runnable: the raw text judged, the empty and the absent element
# ---------------------------------------------------------------------------


class TestOneRunnable:
    def test_a_symbol_equal_to_the_name_stays_unset(self, tmp_path: Path) -> None:
        _swc, symbols, entries = _map(_source(tmp_path, NAME))
        assert symbols == {NAME: None}
        assert _symbol_lines(entries) == {}

    def test_a_differing_c_identifier_is_carried_with_no_line(self, tmp_path: Path) -> None:
        _swc, symbols, entries = _map(_source(tmp_path, "Rte_WheelSpeed_Main"))
        assert symbols == {NAME: "Rte_WheelSpeed_Main"}
        assert _symbol_lines(entries) == {}

    def test_a_leading_underscore_is_a_c_identifier(self, tmp_path: Path) -> None:
        _swc, symbols, _entries = _map(_source(tmp_path, "_Speed"))
        assert symbols == {NAME: "_Speed"}

    @pytest.mark.parametrize(
        "text",
        [
            "1Speed",
            "Speed-Run",
            "",
            f" {NAME}",
            f"{NAME} ",
            f"{NAME}\n",
            "Rte Speed",
            "Rte\nSpeed",
            " Rte_Speed\n",
        ],
        ids=[
            "leading-digit",
            "hyphen",
            "empty",
            "leading-space-name",
            "trailing-space-name",
            "trailing-newline-name",
            "inner-space",
            "inner-line-break",
            "padded-identifier",
        ],
    )
    def test_a_text_that_is_not_a_c_identifier_as_written_is_dropped_and_named(self, text: str, tmp_path: Path) -> None:
        _swc, symbols, entries = _map(_source(tmp_path, text))

        assert symbols == {NAME: None}
        line = _symbol_lines(entries)[NAME]
        assert (line.path, line.tag) == (RUNNABLE, "SYMBOL")
        assert line.category is SkipCategory.EXPORT_DEFECT
        assert line.rule is StandardRule.C_IDENTIFIER_FORM
        assert line.effect == "abort"
        assert repr(text) in line.reason
        assert "[a-zA-Z_][a-zA-Z0-9_]*" in line.reason
        assert "written with its SHORT-NAME" in line.reason

    def test_an_absent_symbol_keeps_the_readers_line(self, tmp_path: Path) -> None:
        _swc, symbols, entries = _map(_source(tmp_path, None))

        assert symbols == {NAME: None}
        line = _symbol_lines(entries)[NAME]
        assert line.category is SkipCategory.NOT_SUPPORTED
        assert line.reason.startswith("SYMBOL is absent")


# ---------------------------------------------------------------------------
# Duplicate written symbols inside one component
# ---------------------------------------------------------------------------


class TestDuplicates:
    def test_two_runnables_stating_one_symbol_both_lose_it_with_a_section_one_line(self, tmp_path: Path) -> None:
        swc, symbols, entries = _map(_source(tmp_path, "Sym_SharedByTwo", ("BrakeRun", "Sym_SharedByTwo")))

        assert symbols == {NAME: None, "BrakeRun": None}
        lines = _symbol_lines(entries)
        assert sorted(lines) == ["BrakeRun", NAME]
        for name, other in ((NAME, "BrakeRun"), ("BrakeRun", NAME)):
            line = lines[name]
            assert line.category is SkipCategory.EXPORT_DEFECT
            assert line.rule is StandardRule.SYMBOL_UNIQUE
            assert line.effect == "abort"
            assert "'Sym_SharedByTwo'" in line.reason and f"'{other}'" in line.reason
            assert "[SWS_Rte_03527]" in line.reason
        assert runnable_symbol_records([swc]) == []

    def test_a_stated_symbol_equal_to_a_siblings_stated_name_is_a_section_one_group(self, tmp_path: Path) -> None:
        """The sibling states its own name as its symbol; the file gives one symbol to two runnables."""
        swc, symbols, entries = _map(_source(tmp_path, "BrakeRun", ("BrakeRun", "BrakeRun")))

        assert symbols == {NAME: None, "BrakeRun": None}
        lines = _symbol_lines(entries)
        assert {name: (line.category, line.rule) for name, line in lines.items()} == {
            NAME: (SkipCategory.EXPORT_DEFECT, StandardRule.SYMBOL_UNIQUE),
            "BrakeRun": (SkipCategory.EXPORT_DEFECT, StandardRule.SYMBOL_UNIQUE),
        }
        assert runnable_symbol_records([swc]) == []

    def test_the_chain_needs_a_second_pass(self, tmp_path: Path) -> None:
        """The chain: first states the second's name, second the third's symbol, third its own name.

        Pass 1 (stated texts): the second and the third collide -- both section 1.
        Pass 2: the second is now written with its name, which the first carries --
        the first loses its symbol with a section-2 line naming the second.
        """
        swc, symbols, entries = _map(_source(tmp_path, "BrakeRun", ("BrakeRun", "SteerRun"), ("SteerRun", "SteerRun")))

        assert symbols == {NAME: None, "BrakeRun": None, "SteerRun": None}
        lines = _symbol_lines(entries)
        assert {name: (line.category, line.rule) for name, line in lines.items()} == {
            NAME: (SkipCategory.NOT_SUPPORTED, None),
            "BrakeRun": (SkipCategory.EXPORT_DEFECT, StandardRule.SYMBOL_UNIQUE),
            "SteerRun": (SkipCategory.EXPORT_DEFECT, StandardRule.SYMBOL_UNIQUE),
        }
        first = lines[NAME]
        assert first.effect == "abort"
        assert "runnable 'BrakeRun'" in first.reason and "is shared with another runnable" in first.reason
        assert runnable_symbol_records([swc]) == []

    def test_a_carried_symbol_equal_to_the_name_of_a_sibling_stating_none_is_a_section_two_line(
        self, tmp_path: Path
    ) -> None:
        """The sibling's ``SYMBOL`` is absent, so the writer's fallback, not the file, makes the collision."""
        swc, symbols, entries = _map(_source(tmp_path, "BrakeRun", ("BrakeRun", None)))

        assert symbols == {NAME: None, "BrakeRun": None}
        lines = _symbol_lines(entries)
        assert lines[NAME].category is SkipCategory.NOT_SUPPORTED
        assert "runnable 'BrakeRun'" in lines[NAME].reason and "own SYMBOL is absent" in lines[NAME].reason
        assert lines["BrakeRun"].reason.startswith("SYMBOL is absent")
        assert runnable_symbol_records([swc]) == []

    def test_symbols_differing_only_in_case_are_two_symbols(self, tmp_path: Path) -> None:
        _swc, symbols, entries = _map(_source(tmp_path, "Rte_Speed", ("BrakeRun", "rte_speed")))
        assert symbols == {NAME: "Rte_Speed", "BrakeRun": "rte_speed"}
        assert _symbol_lines(entries) == {}


# ---------------------------------------------------------------------------
# The citation of a member that is not an id
# ---------------------------------------------------------------------------


def test_the_c_identifier_citation_names_the_class_table() -> None:
    assert cite(StandardRule.C_IDENTIFIER_FORM, ArxmlVersion.V4_3_0) == "R4.3.0 [GST primitive CIdentifier]"
    assert cite(StandardRule.SYMBOL_UNIQUE, ArxmlVersion.V4_2_2) == "R4.2.2 [constr_2025]"


# ---------------------------------------------------------------------------
# import model: the symbol reaches the YAML and the ARXML
# ---------------------------------------------------------------------------


def test_import_model_writes_the_symbol_and_the_project_generates_it(tmp_path: Path) -> None:
    project = init_project(tmp_path / "proj")
    report = run_import(str(_source(tmp_path, "Rte_WheelSpeed_Main")), str(project / "swcraft.yaml"))

    assert not [entry for entry in report.skip_entries if entry.tag == "SYMBOL"]
    (written,) = (project / "model").glob("swc/**/WheelSpeedSensorSwc.yaml")
    document = yaml.safe_load(written.read_text(encoding="utf-8"))
    assert [runnable.get("symbol") for runnable in document["runnables"]] == ["Rte_WheelSpeed_Main"]
    output = tmp_path / "out.arxml"
    generate_arxml(str(project / "model"), str(output), version=ArxmlVersion.V4_2_2)
    symbols = [node.text for node in etree.parse(str(output)).iter(f"{NS}SYMBOL")]
    assert symbols == ["Rte_WheelSpeed_Main"]


def test_import_model_reports_a_duplicate_in_section_one_with_its_citation(tmp_path: Path) -> None:
    project = init_project(tmp_path / "proj")
    report = run_import(
        str(_source(tmp_path, "Sym_SharedByTwo", ("BrakeRun", "Sym_SharedByTwo"))), str(project / "swcraft.yaml")
    )
    rendered = report.render()
    assert "  - 2 × SYMBOL (R4.2.2 [constr_2025]): " not in rendered  # one line per runnable: two reasons
    assert rendered.count("× SYMBOL (R4.2.2 [constr_2025]): ") == 2
    model_root = project / "model"
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output), version=ArxmlVersion.V4_2_2)
    assert sorted(node.text for node in etree.parse(str(output)).iter(f"{NS}SYMBOL")) == ["BrakeRun", NAME]


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


def _access_model(tmp_path: Path, symbol: str | None) -> Path:
    """``access_points`` with ``AccessMain`` stating *symbol* (``None``: no key)."""
    root = tmp_path / "source_model"
    shutil.copytree(ACCESS_POINTS_MODEL_ROOT, root)
    path = root / "swc" / "access" / "AccessSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if symbol is not None:
        document["runnables"][0]["symbol"] = symbol
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _runnable_symbols(model_root: Path) -> dict[str, str | None]:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == "AccessSwc"]
    return {runnable.name: runnable.symbol for runnable in swc.runnables}


def test_import_component_carries_a_stated_symbol(tmp_path: Path) -> None:
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(_access_model(tmp_path, "Rte_Access_Main")), "AccessSwc", str(arxml))
    project = init_project(tmp_path / "proj")

    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    assert _runnable_symbols(project / "model") == {"AccessMain": "Rte_Access_Main", "QuietRun": None}


def test_a_pre_change_component_file_imports_with_no_symbol_and_no_line(tmp_path: Path) -> None:
    """A file stating ``SYMBOL`` equal to the name imports with no symbol and no abort."""
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(_access_model(tmp_path, None)), "AccessSwc", str(arxml))
    assert [node.text for node in etree.parse(str(arxml)).iter(f"{NS}SYMBOL")] == ["AccessMain", "QuietRun"]
    project = init_project(tmp_path / "proj")

    report = import_component(str(arxml), str(project / "model"))

    assert not [line for line in report.skip_lines if "SYMBOL" in line]
    assert _runnable_symbols(project / "model") == {"AccessMain": None, "QuietRun": None}


def test_a_stated_symbol_is_compared_as_it_stands(tmp_path: Path) -> None:
    """Equal symbols are no difference; a target stating none against a file stating one is a conflict."""
    project = init_project(tmp_path / "proj")
    plain = tmp_path / "plain.arxml"
    export_component(str(_access_model(tmp_path / "a", None)), "AccessSwc", str(plain))
    stated = tmp_path / "stated.arxml"
    export_component(str(_access_model(tmp_path / "b", "Rte_Access_Main")), "AccessSwc", str(stated))
    import_component(str(plain), str(project / "model"))

    assert import_component(str(plain), str(project / "model")).swc_action == "unchanged"
    with pytest.raises(ComponentImportConflictError):
        import_component(str(stated), str(project / "model"))
    assert _runnable_symbols(project / "model") == {"AccessMain": None, "QuietRun": None}

    assert import_component(str(stated), str(project / "model"), update=True).swc_action == "updated"
    assert _runnable_symbols(project / "model") == {"AccessMain": "Rte_Access_Main", "QuietRun": None}
    assert import_component(str(stated), str(project / "model")).swc_action == "unchanged"


def test_import_component_aborts_on_a_duplicate_stated_symbol(tmp_path: Path) -> None:
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(_access_model(tmp_path, None)), "AccessSwc", str(arxml))
    tree = etree.parse(str(arxml))
    for node in tree.iter(f"{NS}SYMBOL"):
        node.text = "Sym_SharedByTwo"
    arxml.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    project = init_project(tmp_path / "proj")
    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))

    with pytest.raises(LossyComponentImportError) as raised:
        import_component(str(arxml), str(project / "model"))

    assert sum("[constr_2025]" in line for line in raised.value.skip_lines) == 2
    assert sorted(path.relative_to(project).as_posix() for path in project.rglob("*")) == before


def test_import_component_aborts_on_a_symbol_that_is_not_a_c_identifier(tmp_path: Path) -> None:
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(_access_model(tmp_path, None)), "AccessSwc", str(arxml))
    tree = etree.parse(str(arxml))
    next(tree.iter(f"{NS}SYMBOL")).text = "Access Main"
    arxml.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    project = init_project(tmp_path / "proj")

    with pytest.raises(LossyComponentImportError) as raised:
        import_component(str(arxml), str(project / "model"))

    assert sum("[GST primitive CIdentifier]" in line for line in raised.value.skip_lines) == 1


def test_the_fixture_component_path_is_the_one_the_lines_name() -> None:
    assert RUNNABLE == f"{SWC_PATH}/WheelSpeedSensorSwc_InternalBehavior/{NAME}"
    assert BEHAVIOUR_PATH == f"{SWC_PATH}/WheelSpeedSensorSwc_InternalBehavior"


# ---------------------------------------------------------------------------
# The written pass repeated, the sibling causes, the import component limit
# ---------------------------------------------------------------------------


class TestTheWrittenPass:
    def test_the_written_pass_repeats_and_names_a_sibling_whose_symbol_was_not_carried(self, tmp_path: Path) -> None:
        """The written-symbol pass needs a second iteration, and its reason names a sibling it dropped itself.

        ``BrakeRun`` states the fixture's name (which states none), ``SteerRun`` states ``BrakeRun``:
        ``BrakeRun`` drops first (*is absent*), then ``SteerRun`` (*was not carried*).
        """
        swc, symbols, entries = _map(_source(tmp_path, None, ("BrakeRun", NAME), ("SteerRun", "BrakeRun")))

        assert symbols == {NAME: None, "BrakeRun": None, "SteerRun": None}
        lines = _symbol_lines(entries)
        assert {name: (line.category, line.rule) for name, line in lines.items()} == {
            NAME: (SkipCategory.NOT_SUPPORTED, None),
            "BrakeRun": (SkipCategory.NOT_SUPPORTED, None),
            "SteerRun": (SkipCategory.NOT_SUPPORTED, None),
        }
        assert lines[NAME].reason.startswith("SYMBOL is absent")
        assert f"runnable '{NAME}'" in lines["BrakeRun"].reason
        assert "own SYMBOL is absent" in lines["BrakeRun"].reason
        assert "runnable 'BrakeRun'" in lines["SteerRun"].reason
        assert "own SYMBOL was not carried" in lines["SteerRun"].reason
        assert all(line.effect == "abort" for line in lines.values())
        assert runnable_symbol_records([swc]) == []

    def test_a_sibling_whose_symbol_is_not_a_c_identifier_is_named_with_that_cause(self, tmp_path: Path) -> None:
        """The fixture's runnable states ``1bad`` (dropped, section 1); ``BrakeRun`` states the fixture's name."""
        swc, symbols, entries = _map(_source(tmp_path, "1bad", ("BrakeRun", NAME)))

        assert symbols == {NAME: None, "BrakeRun": None}
        lines = _symbol_lines(entries)
        assert (lines[NAME].category, lines[NAME].rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.C_IDENTIFIER_FORM)
        assert lines["BrakeRun"].category is SkipCategory.NOT_SUPPORTED
        assert f"runnable '{NAME}'" in lines["BrakeRun"].reason
        assert "own SYMBOL is not a C identifier" in lines["BrakeRun"].reason
        assert runnable_symbol_records([swc]) == []


def test_a_target_symbol_equal_to_the_name_differs_from_its_own_export(tmp_path: Path) -> None:
    """A known limit: a stated ``symbol`` equal to the name reads back as none, so the target's own export conflicts."""
    target = _access_model(tmp_path / "target", "AccessMain")
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(target), "AccessSwc", str(arxml))
    assert [node.text for node in etree.parse(str(arxml)).iter(f"{NS}SYMBOL")] == ["AccessMain", "QuietRun"]

    with pytest.raises(ComponentImportConflictError):
        import_component(str(arxml), str(target))

    assert _runnable_symbols(target) == {"AccessMain": "AccessMain", "QuietRun": None}
