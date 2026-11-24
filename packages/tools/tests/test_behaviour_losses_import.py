"""The behaviour reader's unread entries in the ``import model`` report and in ``import component``.

The entries flow through ``model.skip_log`` like every other reader entry
(``test_reader_behaviour_losses.py`` holds the reader's tables):

* **the report** -- ``import model --dry-run`` prints one grouped line per ``(tag, reason)``,
  and ``--verbose`` lists every item's path under its line;
* **``import component`` aborts** -- a foreign component export carrying a per-instance
  parameter, or a behaviour ``LONG-NAME``, is refused as lossy and nothing is written;
* **the documentation group** -- a ``LONG-NAME`` is one report line per child, not per ``L-4``;
* **``HANDLE-TERMINATION-AND-RESTART`` does not abort** -- the field itself is
  ``test_handle_termination_and_restart.py``'s; this only holds that the reader's table does not
  undo it.

Does not reach the real tool exports (the closed suites), or any count of the report beyond the
one fixture built here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from behaviour_losses_support import (
    BEHAVIOUR,
    CONTAINER_ROWS,
    MINIMAL_SWC,
    NS,
    RUNNABLE,
    add_xml,
    items_xml,
    option_xml,
    unread_reason,
)
from conftest import HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT, declare_no_support
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import.report import SKIPPED_HEADING, SKIPPED_VERBOSE_HEADING
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component

_BEHAVIOUR_TAG = "SWC-INTERNAL-BEHAVIOR"
_RUNNABLE_TAG = "RUNNABLE-ENTITY"


# Helpers


def _write(tree: etree._ElementTree, path: Path) -> Path:
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _prefix(index: int) -> str:
    """A per-row name prefix, so no two rows' named items share an AUTOSAR path."""
    return f"row{index}_"


def _one_item_per_row(tmp_path: Path) -> Path:
    """``minimal_swc.arxml`` with two items of every container row and one of each compared attribute."""
    tree = etree.parse(str(MINIMAL_SWC))
    behaviour = next(tree.getroot().iter(f"{NS}{_BEHAVIOUR_TAG}"))
    runnable = next(behaviour.iter(f"{NS}{_RUNNABLE_TAG}"))
    for index, (owner, container, item, named) in enumerate(CONTAINER_ROWS):
        parent = behaviour if owner == "behaviour" else runnable
        add_xml(parent, f"<{container}>{items_xml(item, named, _prefix(index))}</{container}>")
    # The compared group: each differs from what the writer regenerates.
    behaviour.find(f"{NS}SUPPORTS-MULTIPLE-INSTANTIATION").text = "true"
    # A differing SYMBOL is carried, so it adds no line.
    runnable.find(f"{NS}SYMBOL").text = "Foreign_Symbol"
    options = behaviour.find(f"{NS}PORT-API-OPTIONS")
    for child in list(options):
        options.remove(child)
    add_xml(options, option_xml(extra="<PORT-ARG-VALUES><PORT-DEFINED-ARGUMENT-VALUE/></PORT-ARG-VALUES>"))
    return _write(tree, tmp_path / "one_item_per_row.arxml")


def _dry_run(
    source: Path,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> str:
    argv = ["swcraft", "import", "model", "--source", str(source), "--project", str(project / "swcraft.yaml")]
    monkeypatch.setattr("sys.argv", [*argv, "--dry-run", *extra])
    main()
    return capsys.readouterr().out


def _skip_section(output: str, heading: str) -> list[str]:
    """The lines under *heading*, up to the next blank line."""
    lines = output.splitlines()
    start = lines.index(heading) + 1
    end = lines.index("", start)
    return lines[start:end]


def _grouped(section: list[str]) -> list[str]:
    """The grouped lines of a skip section -- its ``  - N × TAG: reason`` lines only."""
    return [line for line in section if line.startswith("  - ")]


# import model --dry-run


class TestTheReport:
    """One grouped line per ``(tag, reason)``; ``--verbose`` lists the paths."""

    def test_one_line_per_tag_and_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _one_item_per_row(tmp_path)
        baseline_project = init_project(tmp_path / "baseline")
        project = init_project(tmp_path / "proj")
        capsys.readouterr()

        baseline = _dry_run(MINIMAL_SWC, baseline_project, monkeypatch, capsys)
        output = _dry_run(source, project, monkeypatch, capsys)

        before = _grouped(_skip_section(baseline, SKIPPED_HEADING)) if SKIPPED_HEADING in baseline else []
        added = [line for line in _grouped(_skip_section(output, SKIPPED_HEADING)) if line not in before]
        expected_rows = [
            f"  - 2 × {item}: {unread_reason(container, _BEHAVIOUR_TAG if owner == 'behaviour' else _RUNNABLE_TAG)}"
            for owner, container, item, _named in CONTAINER_ROWS
        ]
        # Every container row is its own line, and the compared group adds one line per
        # attribute but SYMBOL, which is carried.
        assert sorted(line for line in added if " is not read by SWCraft" in line) == sorted(expected_rows)
        compared = [line for line in added if " is not read by SWCraft" not in line]
        # The option carrying PORT-ARG-VALUES is one line for the whole option, keyed at its PORT-REF.
        assert sorted(line.split(":")[0] for line in compared) == [
            "  - 1 × PORT-API-OPTION",
            "  - 1 × SUPPORTS-MULTIPLE-INSTANTIATION",
        ]
        assert len(added) == len(CONTAINER_ROWS) + 2

    def test_verbose_lists_every_item_under_its_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _one_item_per_row(tmp_path)
        project = init_project(tmp_path / "proj")
        capsys.readouterr()

        plain = _dry_run(source, project, monkeypatch, capsys)
        verbose = _dry_run(source, project, monkeypatch, capsys, "--verbose")

        section = _skip_section(verbose, SKIPPED_VERBOSE_HEADING)
        for index, (owner, container, item, named) in enumerate(CONTAINER_ROWS):
            parent_path = BEHAVIOUR if owner == "behaviour" else RUNNABLE
            parent_tag = _BEHAVIOUR_TAG if owner == "behaviour" else _RUNNABLE_TAG
            line = section.index(f"  - 2 × {item}: {unread_reason(container, parent_tag)}")
            paths = (
                [f"{parent_path}/{_prefix(index)}1", f"{parent_path}/{_prefix(index)}2"]
                if named
                else [parent_path, parent_path]
            )
            assert section[line + 1 : line + 3] == [f"      {path}" for path in paths], container
            assert f"  - 2 × {item}: {unread_reason(container, parent_tag)}" in plain
        assert not any(line.startswith("      /") for line in _skip_section(plain, SKIPPED_HEADING))


# import component


def _foreign_export(tmp_path: Path, *, termination: str | None, unread_child: bool, long_name: bool = False) -> Path:
    """A component export with SWCraft's own marker removed, so nothing in it vouches for SWCraft.

    Built from ``SilentSwc``, exported with ``no_support`` (the writer refuses a component without
    ``handle_termination_and_restart``) and the element removed again. *termination* is inserted
    after ``EVENTS``; *unread_child* adds one ``PER-INSTANCE-PARAMETERS``; *long_name* adds a
    behaviour ``LONG-NAME`` after its ``SHORT-NAME``.
    """
    source_root = tmp_path / "source_model"
    shutil.copytree(HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT, source_root)
    declare_no_support(next(source_root.glob("swc/*/SilentSwc.yaml")))
    exported = tmp_path / "exported.arxml"
    export_component(str(source_root), "SilentSwc", str(exported))
    tree = etree.parse(str(exported))
    for behaviour in tree.getroot().iter(f"{NS}{_BEHAVIOUR_TAG}"):
        for admin in behaviour.findall(f"{NS}ADMIN-DATA"):
            behaviour.remove(admin)
        for declared in behaviour.findall(f"{NS}HANDLE-TERMINATION-AND-RESTART"):
            behaviour.remove(declared)
        events = behaviour.find(f"{NS}EVENTS")
        if unread_child:
            (parameters,) = etree.fromstring(
                f'<W xmlns="{NAMESPACE}"><PER-INSTANCE-PARAMETERS><PARAMETER-DATA-PROTOTYPE>'
                "<SHORT-NAME>PIP_Speed</SHORT-NAME></PARAMETER-DATA-PROTOTYPE></PER-INSTANCE-PARAMETERS></W>".encode()
            )
            events.addnext(parameters)
        if long_name:
            (element,) = etree.fromstring(
                f'<W xmlns="{NAMESPACE}"><LONG-NAME><L-4 L="EN">Silent behaviour</L-4></LONG-NAME></W>'.encode()
            )
            behaviour.find(f"{NS}SHORT-NAME").addnext(element)
        if termination is not None:
            element = etree.SubElement(behaviour, f"{NS}HANDLE-TERMINATION-AND-RESTART")
            element.text = termination
            events.addnext(element)
    return _write(tree, tmp_path / "foreign.arxml")


class TestImportComponent:
    def test_a_foreign_unread_behaviour_child_aborts_and_writes_nothing(self, tmp_path: Path) -> None:
        foreign = _foreign_export(tmp_path, termination="NO-SUPPORT", unread_child=True)
        project = init_project(tmp_path / "proj")
        before = files_of(project)

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(foreign), str(project / "model"))

        line = (
            "PARAMETER-DATA-PROTOTYPE /ComponentTypes/SilentSwc/SilentSwc_InternalBehavior/PIP_Speed: "
            f"{unread_reason('PER-INSTANCE-PARAMETERS', _BEHAVIOUR_TAG)}"
        )
        assert line in str(caught.value)
        assert files_of(project) == before

    @pytest.mark.parametrize("literal", ["NO-SUPPORT", "CAN-BE-TERMINATED", "CAN-BE-TERMINATED-AND-RESTARTED"])
    def test_the_termination_element_alone_does_not_abort(self, literal: str, tmp_path: Path) -> None:
        foreign = _foreign_export(tmp_path, termination=literal, unread_child=False)
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert report.swc_action == "created"
        assert "HANDLE-TERMINATION-AND-RESTART" not in "\n".join(report.skip_lines)

    def test_a_foreign_behaviour_long_name_aborts_and_writes_nothing(self, tmp_path: Path) -> None:
        """A documentation line aborts like every other unread line: a lossy foreign component is refused."""
        foreign = _foreign_export(tmp_path, termination="NO-SUPPORT", unread_child=False, long_name=True)
        project = init_project(tmp_path / "proj")
        before = files_of(project)

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(foreign), str(project / "model"))

        line = (
            "LONG-NAME /ComponentTypes/SilentSwc/SilentSwc_InternalBehavior: "
            "LONG-NAME of a SWC-INTERNAL-BEHAVIOR is documentation SWCraft does not carry -- not imported or preserved"
        )
        assert line in str(caught.value)
        assert files_of(project) == before


# import model --dry-run: the documentation group


def test_the_documentation_group_is_one_report_line_per_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two items in one ``LONG-NAME`` are one entry; the report groups the behaviour's and the runnable's apart."""
    tree = etree.parse(str(MINIMAL_SWC))
    behaviour = next(tree.getroot().iter(f"{NS}{_BEHAVIOUR_TAG}"))
    runnable = next(behaviour.iter(f"{NS}{_RUNNABLE_TAG}"))
    long_name = '<LONG-NAME><L-4 L="EN">Speed</L-4><L-4 L="DE">Drehzahl</L-4></LONG-NAME>'
    add_xml(behaviour, long_name + '<DESC><L-2 L="EN">About the behaviour.</L-2></DESC>')
    add_xml(runnable, long_name)
    source = _write(tree, tmp_path / "documented.arxml")
    project = init_project(tmp_path / "proj")
    capsys.readouterr()

    output = _dry_run(source, project, monkeypatch, capsys)

    reason = "{tag} of a {parent} is documentation SWCraft does not carry -- not imported or preserved"
    assert sorted(line for line in _grouped(_skip_section(output, SKIPPED_HEADING)) if "documentation" in line) == [
        f"  - 1 × DESC: {reason.format(tag='DESC', parent=_BEHAVIOUR_TAG)}",
        f"  - 1 × LONG-NAME: {reason.format(tag='LONG-NAME', parent=_RUNNABLE_TAG)}",
        f"  - 1 × LONG-NAME: {reason.format(tag='LONG-NAME', parent=_BEHAVIOUR_TAG)}",
    ]
